import os
import subprocess
import threading
import time
import json
import re
import socket
import select

from .config_manager import config_manager
from .elgato_wave import elgato_manager
from wavecontroller.utils.logger import get_logger

log = get_logger("HardwareManager")

class USBHardwareManager:
    """
    Hardware integration layer managing physical Audio Input, Output, and Duplex devices.
    Groups composite USB microphones with monitoring headphone outputs (e.g. Wave XLR, Wave:3, Fifine).
    Provides user-curated device management (Add/Remove devices, persistent nicknames, volume, gain, DSP).
    """

    def __init__(self):
        self.device_name = "Elgato Wave XLR"
        self.device_type = "generic" # "generic" or "elgato"
        hw_init = config_manager.get("hardware_settings", {})
        self.led_colors = dict(hw_init.get("led_colors", {
            "gain": "#FFFFFF",
            "hp": "#2ECC71",
            "mix": "#FF9500",
            "mute": "#FF0000"
        }))
        self.led_colors["mute"] = "#FF0000"
        self.exclusive_mic_lock: bool = bool(config_manager.get("hardware_settings", {}).get("exclusive_mic_lock", True))
        self.exclusive_output_lock: bool = bool(config_manager.get("hardware_settings", {}).get("exclusive_output_lock", True))
        self.discovered_devices: dict[str, dict] = {} # {device_key: dev_info_dict}
        self.input_devices = [] # Legacy compatibility
        self.output_devices = [] # Legacy compatibility
        self.connected_audio_devices = [] # Legacy compatibility
        
        # Load saved hardware settings from ConfigManager
        hw_settings = config_manager.get("hardware_settings", {})
        self.hardware_gain_db = hw_settings.get("gain_db", 45)
        self.phantom_power_48v = hw_settings.get("phantom_power", False)
        self.clipguard_enabled = hw_settings.get("clipguard", True)
        self.low_cut_filter = hw_settings.get("low_cut", "80Hz")
        self.hardware_mute = False
        self.headphone_volume = hw_settings.get("headphone_volume", 70)
        self.monitor_mix = hw_settings.get("monitor_mix", 50)
        self.mic_pc_crossfade = self.monitor_mix
        self.low_impedance_mode = hw_settings.get("low_impedance", False)
        
        self.is_monitoring_mic = False
        self._loopback_proc = None
        self.pipewire_mgr = None
        self.on_device_renamed_callback = None
        self.on_devices_changed_callback = None
        self.on_new_device_detected_callback = None
        self._hardware_listeners = []
        self._elgato_initialized = False
        self._is_sleeping = False
        self._restoring_hardware = False

    def _ensure_elgato_card_profile(self, card_id: int):
        """Ensures the ALSA device profile is locked to 'Analog Stereo Output + Mono Input'."""
        try:
            out = subprocess.check_output(["pw-dump", str(card_id)], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            if not data:
                return
            params = data[0].get("info", {}).get("params", {})
            active_list = params.get("Profile", [])
            if active_list and active_list[0].get("name") == "output:analog-stereo+input:mono-fallback":
                return
            enum_profs = params.get("EnumProfile", [])
            for p in enum_profs:
                if p.get("name") == "output:analog-stereo+input:mono-fallback":
                    idx = p.get("index")
                    if idx is not None:
                        subprocess.run(["wpctl", "set-profile", str(card_id), str(idx)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
        except Exception:
            pass

    def set_pipewire_manager(self, pw_mgr):
        """Sets reference to PipeWireManager for bi-directional hardware/software mute sync."""
        self.pipewire_mgr = pw_mgr

        # Hook Elgato hardware dial & capacitive mute sync
        elgato_manager.on_state_changed = self._on_elgato_hardware_sync

        self.detect_connected_hardware()
        self._ensure_default_tracked_devices()
        self._start_hotplug_monitor()

    def add_hardware_listener(self, callback):
        """Registers a listener callback (curr, changed) for physical hardware events."""
        if callback and callback not in self._hardware_listeners:
            self._hardware_listeners.append(callback)

    def remove_hardware_listener(self, callback):
        """Unregisters a hardware listener callback."""
        if callback in self._hardware_listeners:
            self._hardware_listeners.remove(callback)

    def notify_hardware_listeners(self, curr: dict, changed: dict):
        """Dispatches hardware state updates to all registered listeners safely."""
        for cb in list(self._hardware_listeners):
            try:
                cb(curr, changed)
            except Exception:
                pass

    @property
    def on_hardware_state_changed_callback(self):
        return None

    @on_hardware_state_changed_callback.setter
    def on_hardware_state_changed_callback(self, cb):
        if cb:
            self.add_hardware_listener(cb)

    @property
    def is_connected(self) -> bool:
        elgato_dev = elgato_manager.get_device()
        if elgato_dev and elgato_dev.is_connected():
            return True
        info = self.get_elgato_device_info()
        if info and info.get("connected", False):
            return True
        return bool(self.device_name)

    @property
    def is_elgato(self) -> bool:
        elgato_dev = elgato_manager.get_device()
        if elgato_dev and elgato_dev.is_connected():
            return True
        return any(d.get("is_elgato", False) for d in self.discovered_devices.values())

    @property
    def current_dial_mode(self) -> str:
        elgato_dev = elgato_manager.get_device()
        if elgato_dev and elgato_dev.is_connected():
            return elgato_dev.get_dial_mode()
        return "gain"

    def _ensure_default_tracked_devices(self):
        """Initializes tracked devices list strictly after first-time setup."""
        if not config_manager.get("first_run_completed", False):
            return
        tracked = config_manager.get("tracked_devices", None)
        if tracked is None:
            config_manager.set("tracked_devices", [], immediate=True)

    def _on_elgato_hardware_sync(self, curr: dict, changed: dict):
        """Dispatched when physical dial, 2-sec 48V hold, or capacitive mute sensor changes on Elgato hardware."""
        if getattr(self, "_restoring_hardware", False) or getattr(self, "_is_sleeping", False):
            return

        if "phantom_power" in changed:
            self.phantom_power_48v = bool(changed["phantom_power"])
            hw = dict(config_manager.get("hardware_settings", {}))
            hw["phantom_power"] = self.phantom_power_48v
            config_manager.set("hardware_settings", hw, immediate=False)

        if "mute" in changed and "dial_mode" not in changed:
            self.hardware_mute = bool(changed["mute"])
            active_mode = curr.get("dial_mode", "gain")

            if active_mode == "gain":
                # Mode 1 (LED 1): Mute / Unmute Microphone ONLY
                target = self._resolve_source_target()
                try:
                    subprocess.Popen(["wpctl", "set-mute", target, "1" if self.hardware_mute else "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
                if getattr(self, "pipewire_mgr", None):
                    for ch_key in ("elgato_wave_xlr", "mic", "microphone"):
                        self.pipewire_mgr.set_channel_master_mute(ch_key, self.hardware_mute)

            elif active_mode == "hp":
                # Mode 2 (LED 2): Mute / Unmute Headphone Output Mix ONLY
                target = self._resolve_sink_target()
                try:
                    subprocess.Popen(["wpctl", "set-mute", target, "1" if self.hardware_mute else "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
                if getattr(self, "pipewire_mgr", None):
                    target_mix_id = self._get_elgato_output_mix_id()
                    self.pipewire_mgr.set_mix_master_mute(target_mix_id, self.hardware_mute)

            elif active_mode == "mix":
                # Mode 3 (LED 3): Mutes BOTH Microphone AND Headphone Output
                src_target = self._resolve_source_target()
                sink_target = self._resolve_sink_target()
                try:
                    subprocess.Popen(["wpctl", "set-mute", src_target, "1" if self.hardware_mute else "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    subprocess.Popen(["wpctl", "set-mute", sink_target, "1" if self.hardware_mute else "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
                if getattr(self, "pipewire_mgr", None):
                    for ch_key in ("elgato_wave_xlr", "mic", "microphone"):
                        self.pipewire_mgr.set_channel_master_mute(ch_key, self.hardware_mute)
                    target_mix_id = self._get_elgato_output_mix_id()
                    self.pipewire_mgr.set_mix_master_mute(target_mix_id, self.hardware_mute)

            self.notify_hardware_listeners(curr, changed)

        if "gain_db" in changed:
            recent_drag = (time.time() - getattr(self, "_last_gain_set_time", 0.0)) < 1.5
            if not recent_drag:
                self.hardware_gain_db = int(round(changed["gain_db"]))
                hw = dict(config_manager.get("hardware_settings", {}))
                hw["gain_db"] = self.hardware_gain_db
                config_manager.set("hardware_settings", hw, immediate=False)

        if "hp_volume_pct" in changed:
            log.info(f"[WaveController.Hardware] Hardware HP Volume changed to {changed['hp_volume_pct']}%")
            self.headphone_volume = int(round(changed["hp_volume_pct"]))
            hw = dict(config_manager.get("hardware_settings", {}))
            hw["headphone_volume"] = self.headphone_volume
            config_manager.set("hardware_settings", hw, immediate=False)

        if "monitor_mix_pct" in changed:
            self.monitor_mix = int(round(changed["monitor_mix_pct"]))
            self.mic_pc_crossfade = self.monitor_mix
            hw = dict(config_manager.get("hardware_settings", {}))
            hw["monitor_mix"] = self.monitor_mix
            config_manager.set("hardware_settings", hw, immediate=False)

        if "led_colors" in changed:
            self.led_colors.update(changed["led_colors"])

        if "clipguard" in changed:
            self.clipguard_enabled = bool(changed["clipguard"])

        if "low_cut" in changed:
            self.low_cut_filter = str(changed["low_cut"])

        if "low_impedance" in changed:
            self.low_impedance_mode = bool(changed["low_impedance"])

        self.notify_hardware_listeners(curr, changed)

    def detect_connected_hardware(self):
        """
        Discovers physical hardware devices and links duplex capture + playback endpoints.
        Uses pw-dump with fallback to wpctl status.
        """
        hw_map = {}
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            objects = json.loads(out)
        except Exception:
            objects = []

        devices = {}
        nodes = []

        for obj in objects:
            info = obj.get("info", {})
            props = info.get("props", {})
            obj_type = obj.get("type")
            obj_id = obj.get("id")
            
            if obj_type == "PipeWire:Interface:Device":
                bus_id = props.get("device.bus-id") or props.get("device.name") or str(obj_id)
                devices[obj_id] = {
                    "device_id": obj_id,
                    "device_key": bus_id,
                    "name": props.get("device.nick") or props.get("device.description") or props.get("device.name"),
                    "description": props.get("device.description", ""),
                    "bus": props.get("device.bus", ""),
                    "form_factor": props.get("device.form-factor", ""),
                    "icon_name": props.get("device.icon-name", ""),
                    "sources": [],
                    "sinks": []
                }
            elif obj_type == "PipeWire:Interface:Node":
                media_class = props.get("media.class", "")
                if media_class in ["Audio/Sink", "Audio/Source"]:
                    name = props.get("node.name", "")
                    desc = props.get("node.description", "")
                    dev_id = props.get("device.id")
                    if "wavecontroller" in name.lower() or "null" in name.lower() or "virtual" in name.lower():
                        continue
                    nodes.append({
                        "id": obj_id,
                        "name": name,
                        "description": desc,
                        "nick": props.get("node.nick", desc),
                        "media_class": media_class,
                        "device_id": dev_id
                    })

        for n in nodes:
            dev_id = n.get("device_id")
            if dev_id in devices:
                if n["media_class"] == "Audio/Source":
                    devices[dev_id]["sources"].append(n)
                elif n["media_class"] == "Audio/Sink":
                    devices[dev_id]["sinks"].append(n)

        # Build hardware device objects
        for dev_id, d in devices.items():
            if not d["sources"] and not d["sinks"]:
                continue
            num_in = len(d["sources"])
            num_out = len(d["sinks"])
            if num_in > 0 and num_out > 0:
                d["type"] = "duplex"
                d["badge"] = "In / Out"
            elif num_in > 0:
                d["type"] = "input"
                d["badge"] = "Input"
            else:
                d["type"] = "output"
                d["badge"] = "Output"
            
            d["icon"] = self._detect_smart_icon(
                name=d["name"],
                form_factor=d.get("form_factor", ""),
                icon_name=d.get("icon_name", ""),
                dev_type=d["type"]
            )
            
            d["primary_source_id"] = d["sources"][0]["id"] if d["sources"] else None
            d["primary_sink_id"] = d["sinks"][0]["id"] if d["sinks"] else None
            d["connected"] = True
            
            # Check Elgato hardware identity
            name_low = d["name"].lower()
            d["is_elgato"] = "wave" in name_low or "0fd9" in str(d["device_key"]).lower() or "elgato" in name_low
            hw_map[d["device_key"]] = d

        # Check any orphaned nodes without a parent device
        for n in nodes:
            dev_id = n.get("device_id")
            if not dev_id or dev_id not in devices:
                key = n["name"]
                dtype = "input" if n["media_class"] == "Audio/Source" else "output"
                smart_icon = self._detect_smart_icon(
                    name=n["description"] or n["name"],
                    dev_type=dtype
                )
                name_low = (n["description"] or n["name"]).lower()
                is_el = "wave" in name_low or "elgato" in name_low
                hw_map[key] = {
                    "device_id": n["id"],
                    "device_key": key,
                    "name": n["description"] or n["name"],
                    "description": n["description"] or n["name"],
                    "bus": "",
                    "form_factor": "",
                    "type": dtype,
                    "badge": "In" if dtype == "input" else "Out",
                    "icon": smart_icon,
                    "sources": [n] if dtype == "input" else [],
                    "sinks": [n] if dtype == "output" else [],
                    "primary_source_id": n["id"] if dtype == "input" else None,
                    "primary_sink_id": n["id"] if dtype == "output" else None,
                    "connected": True,
                    "is_elgato": is_el
                }

        self.discovered_devices = hw_map

        # Persist discovered device metadata for tracked devices so disconnected devices keep their friendly names & types
        tracked_keys = config_manager.get("tracked_devices", []) or []
        tracked_meta = dict(config_manager.get("tracked_device_metadata", {}) or {})
        meta_updated = False
        for k, dev in hw_map.items():
            dev_meta = {
                "name": dev["name"],
                "description": dev.get("description", dev["name"]),
                "type": dev.get("type", "duplex"),
                "badge": dev.get("badge", "In / Out"),
                "icon": dev.get("icon", "audio-headset-symbolic"),
                "is_elgato": dev.get("is_elgato", False)
            }
            if tracked_meta.get(k) != dev_meta:
                tracked_meta[k] = dev_meta
                meta_updated = True
        if meta_updated:
            config_manager.set("tracked_device_metadata", tracked_meta, immediate=False)

        # Legacy and direct list access
        inputs = []
        outputs = []
        for k, d in hw_map.items():
            if d.get("type") in ["duplex", "input"] or d.get("sources"):
                inputs.append(d)
            if d.get("type") in ["duplex", "output"] or d.get("sinks"):
                outputs.append(d)
        
        self.input_devices = inputs
        self.output_devices = outputs
        self.connected_audio_devices = list(hw_map.values())

        # Determine primary device & try connecting Elgato USB protocol
        has_elgato = False
        for k, d in hw_map.items():
            if d.get("is_elgato"):
                self.device_name = d["name"]
                self.device_type = "elgato"
                has_elgato = True
                dev_card_id = d.get("device_id")
                if dev_card_id:
                    self._ensure_elgato_card_profile(dev_card_id)
                break

        if has_elgato:
            dev = elgato_manager.get_device()
            if dev and dev.is_connected():
                if not self._elgato_initialized:
                    self.apply_saved_hardware_settings(dev)
                    self._elgato_initialized = True
                try:
                    if not getattr(self, "_restoring_hardware", False):
                        init_st = dev.get_all_state()
                        if init_st.get("connected"):
                            self.phantom_power_48v = bool(init_st.get("phantom_power", self.phantom_power_48v))
                            self.hardware_gain_db = int(round(init_st.get("gain_db", self.hardware_gain_db)))
                            self.headphone_volume = int(round(init_st.get("hp_volume_pct", self.headphone_volume)))
                            self.monitor_mix = int(round(init_st.get("monitor_mix_pct", self.monitor_mix)))
                            self.mic_pc_crossfade = self.monitor_mix
                            self.clipguard_enabled = bool(init_st.get("clipguard", self.clipguard_enabled))
                            self.low_cut_filter = str(init_st.get("low_cut", self.low_cut_filter))
                            self.low_impedance_mode = bool(init_st.get("low_impedance", self.low_impedance_mode))
                            self.hardware_mute = bool(init_st.get("mute", self.hardware_mute))
                except Exception:
                    pass
        else:
            self.device_type = "generic"
            self._elgato_initialized = False

    def apply_saved_hardware_settings(self, dev=None):
        """Applies all saved persistent hardware configurations (phantom power, gain, clipguard, low cut, LED colors, headphone volume, monitor mix) to the physical Elgato device."""
        if dev is None:
            dev = elgato_manager.get_device()
        if not dev or not dev.is_connected():
            return
        
        try:
            hw_settings = config_manager.get("hardware_settings", {})
            if hasattr(dev, "apply_full_config"):
                dev.apply_full_config(hw_settings, self.led_colors, self.hardware_mute)
                # Keep internal state cache in sync
                self.phantom_power_48v = bool(hw_settings.get("phantom_power", self.phantom_power_48v))
                self.hardware_gain_db = int(round(float(hw_settings.get("gain_db", self.hardware_gain_db))))
                self.clipguard_enabled = bool(hw_settings.get("clipguard", self.clipguard_enabled))
                self.low_cut_filter = str(hw_settings.get("low_cut", self.low_cut_filter))
                self.low_impedance_mode = bool(hw_settings.get("low_impedance", self.low_impedance_mode))
                self.headphone_volume = int(round(float(hw_settings.get("headphone_volume", self.headphone_volume))))
                self.monitor_mix = int(round(float(hw_settings.get("monitor_mix", self.monitor_mix))))
                self.mic_pc_crossfade = self.monitor_mix
            else:
                # Fallback for devices without atomic write
                saved_phantom = hw_settings.get("phantom_power", self.phantom_power_48v)
                self.phantom_power_48v = bool(saved_phantom)
                dev.set_phantom_power(self.phantom_power_48v)
                
                saved_gain = hw_settings.get("gain_db", self.hardware_gain_db)
                self.hardware_gain_db = int(round(float(saved_gain)))
                dev.set_gain_db(self.hardware_gain_db)
                
                saved_clip = hw_settings.get("clipguard", self.clipguard_enabled)
                self.clipguard_enabled = bool(saved_clip)
                dev.set_clipguard(self.clipguard_enabled)
                
                saved_lc = hw_settings.get("low_cut", self.low_cut_filter)
                self.low_cut_filter = str(saved_lc)
                dev.set_low_cut(self.low_cut_filter)
                
                saved_lz = hw_settings.get("low_impedance", self.low_impedance_mode)
                self.low_impedance_mode = bool(saved_lz)
                dev.set_low_impedance(self.low_impedance_mode)
                
                if self.led_colors:
                    dev.set_led_colors(self.led_colors)
                    
                saved_hp = hw_settings.get("headphone_volume", self.headphone_volume)
                self.headphone_volume = int(round(float(saved_hp)))
                dev.set_headphone_volume_pct(self.headphone_volume)

                saved_mix = hw_settings.get("monitor_mix", self.monitor_mix)
                self.monitor_mix = int(round(float(saved_mix)))
                self.mic_pc_crossfade = self.monitor_mix
                dev.set_monitor_mix(self.monitor_mix)
                
                dev.set_mode_mute("gain", self.hardware_mute)
                dev.set_mode_mute("hp", False)
                dev.set_mode_mute("mix", False)

            self._last_gain_set_time = time.time() + 2.0
            self._last_hp_set_time = time.time() + 2.0
        except Exception as e:
            log.warning(f"[WaveController.Hardware] apply_saved_hardware_settings failed: {e}")

    def on_system_suspend(self):
        """Prepares USB hardware manager for system sleep/suspend."""
        log.info("[WaveController.Hardware] System going to sleep: marking hardware as suspended...")
        self._is_sleeping = True
        self._elgato_initialized = False
        elgato_manager.on_system_suspend()

    def on_system_resume(self):
        """Restores physical USB hardware and applies saved configuration immediately after system wake."""
        log.info("[WaveController.Hardware] System resumed: fast-restoring USB audio hardware and saved configuration...")
        self._is_sleeping = False
        self._restoring_hardware = True
        self._last_gain_set_time = time.time() + 3.0
        self._last_hp_set_time = time.time() + 3.0
        self._elgato_initialized = False
        elgato_manager.on_system_resume()

        def _do_fast_restore():
            # Fast retry loop: attempt immediate connection every 35ms (up to 35 attempts = ~1.2s max)
            # Reconnects in <100ms on typical resume as soon as the USB hub is powered
            for attempt in range(35):
                if self._is_sleeping:
                    break
                dev = elgato_manager.get_device()
                if dev and dev.is_connected():
                    self.apply_saved_hardware_settings(dev)
                    self._elgato_initialized = True
                    log.info(f"[WaveController.Hardware] Successfully fast-restored settings to {dev.profile.display_name} on attempt {attempt + 1}")

                    # Notify listeners of current hardware state
                    try:
                        curr = dev.get_all_state()
                        self.notify_hardware_listeners(curr, {
                            "gain_db": self.hardware_gain_db,
                            "hp_volume_pct": self.headphone_volume,
                            "monitor_mix_pct": self.monitor_mix,
                            "mute": self.hardware_mute
                        })
                    except Exception:
                        pass
                    break
                time.sleep(0.035)

            try:
                self.detect_connected_hardware()
                if self.on_devices_changed_callback:
                    self.on_devices_changed_callback()
            except Exception:
                pass

            time.sleep(0.1)
            self._restoring_hardware = False

        threading.Thread(target=_do_fast_restore, daemon=True).start()

    def _start_hotplug_monitor(self):
        """Background worker checking for newly attached or detached hardware devices via kernel uevents."""
        def _monitor_loop():
            known_keys = set(self.discovered_devices.keys())
            nl_sock = None
            try:
                # NETLINK_KOBJECT_UEVENT (family=15) gives instant kernel hardware notifications (< 1ms)
                nl_sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 15)
                nl_sock.bind((0, 1))
                nl_sock.setblocking(False)
            except Exception:
                nl_sock = None

            while True:
                if self._is_sleeping:
                    time.sleep(0.5)
                    continue

                if nl_sock:
                    try:
                        # Wait up to 1.5s for kernel uevent or wake immediately on hardware hotplug
                        r, _, _ = select.select([nl_sock], [], [], 1.5)
                        if r:
                            while True:
                                try:
                                    data = nl_sock.recv(4096)
                                    if not data:
                                        break
                                except Exception:
                                    break
                            time.sleep(0.08) # Short debounce for ALSA/PipeWire node creation
                    except Exception:
                        time.sleep(0.4)
                else:
                    time.sleep(0.4)

                try:
                    self.detect_connected_hardware()
                    curr_keys = set(self.discovered_devices.keys())
                    if curr_keys != known_keys:
                        new_keys = curr_keys - known_keys
                        if new_keys:
                            tracked = set(config_manager.get("tracked_devices", []))
                            for nk in new_keys:
                                dev = self.discovered_devices.get(nk)
                                if dev and nk not in tracked:
                                    if self.on_new_device_detected_callback:
                                        self.on_new_device_detected_callback(dev)
                        known_keys = curr_keys
                        if self.on_devices_changed_callback:
                            self.on_devices_changed_callback()
                        if self.pipewire_mgr and hasattr(self.pipewire_mgr, "_sync_channel_audio_routing"):
                            self.pipewire_mgr._sync_channel_audio_routing()
                except Exception:
                    pass

        t = threading.Thread(target=_monitor_loop, daemon=True)
        t.start()

    def _detect_smart_icon(self, name: str, form_factor: str = "", icon_name: str = "", dev_type: str = "duplex") -> str:
        """Smart icon selector matching physical hardware form factors accurately."""
        name_low = name.lower()
        form_factor = form_factor.lower()
        icon_name = icon_name.lower()

        # 0. Elgato Wave XLR Dedicated Hardware
        if "wave xlr" in name_low or "wave_xlr" in name_low:
            return "elgato-wave-xlr-symbolic"

        # 1. Desktop Standalone Microphones (Fifine, Wave, Blue Yeti, Rode, Shure, QuadCast, etc.)
        if form_factor == "microphone" or any(x in name_low for x in ["mic", "fifine", "wave", "yeti", "shure", "rode", "quadcast", "seiren"]):
            return "audio-input-microphone-symbolic"

        # 2. Integrated Headsets (Gaming headphones with attached boom mic)
        if form_factor == "headset" or "headset" in name_low:
            return "audio-headset-symbolic"

        # 3. Headphones / In-Ear Monitors (Playback Only)
        if form_factor == "headphone" or any(x in name_low for x in ["headphone", "iem", "earphone", "airpods", "buds"]):
            return "audio-headphones-symbolic"

        # 4. Speakers / Studio Monitors / Soundbars
        if form_factor == "speaker" or any(x in name_low for x in ["speaker", "soundbar"]):
            return "audio-speakers-symbolic"

        # 5. Audio Interfaces / Dedicated Soundcards / DACs
        if "card" in icon_name or any(x in name_low for x in ["interface", "dac", "focusrite", "scarlett", "hd-audio", "starship", "realtek", "alc"]):
            return "audio-card-symbolic"

        # 6. Fallback based on device capability
        if dev_type == "input":
            return "audio-input-microphone-symbolic"
        elif dev_type == "output":
            return "audio-speakers-symbolic"
        return "audio-input-microphone-symbolic"

    def get_device_icon(self, dev_key_or_name: str) -> str:
        """Returns the custom icon if set by user, else the smart detected icon."""
        if not dev_key_or_name:
            return "elgato-wave-xlr-symbolic" if self.device_type == "elgato" else "audio-input-microphone-symbolic"
        k_str = str(dev_key_or_name)
        custom_icons = config_manager.get("device_icons", {})
        if k_str in custom_icons and custom_icons[k_str]:
            return custom_icons[k_str]
        if k_str in self.discovered_devices:
            return self.discovered_devices[k_str].get("icon", "audio-input-microphone-symbolic")
        for k, dev in self.discovered_devices.items():
            if dev.get("name") == k_str or k_str.lower() in dev.get("name", "").lower():
                return dev.get("icon", "audio-input-microphone-symbolic")
        if any(w in k_str.lower() for w in ("wave xlr", "wave_xlr", "wave:3", "wave_3", "wave:1", "wave_1", "wave neo", "wave_neo", "elgato wave")) or (k_str == self.device_name and self.device_type == "elgato") or "0fd9" in k_str.lower():
            return "elgato-wave-xlr-symbolic"
        if any(m in k_str.lower() for m in ("mic", "fefine", "fifine", "capture", "input")):
            return "audio-input-microphone-symbolic"
        if any(h in k_str.lower() for h in ("headphone", "headset", "earphone", "hp")):
            return "audio-headphones-symbolic"
        if any(s in k_str.lower() for s in ("speaker", "playback", "output", "sink")):
            return "audio-speakers-symbolic"
        return "audio-input-microphone-symbolic"

    def set_device_custom_icon(self, device_key: str, icon_name: str):
        """Sets a persistent custom icon for an audio device."""
        icons = dict(config_manager.get("device_icons", {}))
        icon_name = icon_name.strip()
        if icon_name:
            icons[device_key] = icon_name
        else:
            icons.pop(device_key, None)
        config_manager.set("device_icons", icons, immediate=True)
        if self.on_device_renamed_callback:
            self.on_device_renamed_callback(device_key, "")

    def get_tracked_devices(self) -> list:
        """Returns the list of user-tracked devices hydrated with live state."""
        self.detect_connected_hardware()
        tracked_keys = config_manager.get("tracked_devices", []) or []
        tracked_meta = config_manager.get("tracked_device_metadata", {}) or {}

        aliases = config_manager.get("device_aliases", {})
        assigned_mixes = config_manager.get("device_assigned_mixes", {})

        result = []
        for key in tracked_keys:
            if key in self.discovered_devices:
                dev = dict(self.discovered_devices[key])
            else:
                # Disconnected device: retrieve cached persistent metadata
                saved = tracked_meta.get(key, {})
                saved_name = saved.get("name", key)
                k_low = key.lower()
                name_low = saved_name.lower()
                if saved.get("type"):
                    inferred_type = saved["type"]
                    badge_text = saved.get("badge", "In / Out" if inferred_type == "duplex" else ("Input" if inferred_type == "input" else "Output"))
                elif any(m in k_low or m in name_low for m in ("mic", "fefine", "fifine", "capture", "input")):
                    inferred_type = "input"
                    badge_text = "Input"
                elif any(o in k_low or o in name_low for o in ("headphone", "speaker", "output", "playback", "sink", "iem")):
                    inferred_type = "output"
                    badge_text = "Output"
                else:
                    inferred_type = "duplex"
                    badge_text = "In / Out"

                dev = {
                    "device_key": key,
                    "name": aliases.get(key, saved_name),
                    "description": "Hardware Disconnected",
                    "type": inferred_type,
                    "badge": badge_text,
                    "icon": self.get_device_icon(key) or saved.get("icon", "audio-headset-symbolic"),
                    "sources": [],
                    "sinks": [],
                    "primary_source_id": None,
                    "primary_sink_id": None,
                    "connected": False,
                    "is_elgato": saved.get("is_elgato", False)
                }
            
            dev["icon"] = self.get_device_icon(key) or dev.get("icon")
            dev["display_name"] = aliases.get(key, dev["name"])
            dev["custom_name"] = aliases.get(key, "")
            dev["assigned_mix"] = assigned_mixes.get(key, "personal_mix")
            result.append(dev)
        return result

    def get_tracked_output_devices(self) -> list:
        tracked = self.get_tracked_devices()
        outputs = []
        for dev in tracked:
            if dev.get("type") in ["duplex", "output"] or dev.get("sinks") or dev.get("primary_sink_id"):
                outputs.append(dev)
        return outputs

    def get_tracked_input_devices(self) -> list:
        tracked = self.get_tracked_devices()
        inputs = []
        for dev in tracked:
            if dev.get("type") in ["duplex", "input"] or dev.get("sources") or dev.get("primary_source_id"):
                inputs.append(dev)
        return inputs

    def get_all_available_input_devices(self) -> list:
        """Returns all connected/discovered and tracked input devices."""
        self.detect_connected_hardware()
        tracked = self.get_tracked_input_devices()
        seen_keys = {d.get("device_key") for d in tracked if d.get("device_key")}
        results = list(tracked)
        for k, dev in self.discovered_devices.items():
            if k not in seen_keys:
                if dev.get("type") in ["duplex", "input"] or dev.get("sources") or dev.get("primary_source_id"):
                    d = dict(dev)
                    d["display_name"] = self.get_device_display_name(k)
                    results.append(d)
                    seen_keys.add(k)
        return results

    def get_all_available_output_devices(self) -> list:
        """Returns all connected/discovered and tracked output devices."""
        self.detect_connected_hardware()
        tracked = self.get_tracked_output_devices()
        seen_keys = {d.get("device_key") for d in tracked if d.get("device_key")}
        results = list(tracked)
        for k, dev in self.discovered_devices.items():
            if k not in seen_keys:
                if dev.get("type") in ["duplex", "output"] or dev.get("sinks") or dev.get("primary_sink_id"):
                    d = dict(dev)
                    d["display_name"] = self.get_device_display_name(k)
                    results.append(d)
                    seen_keys.add(k)
        return results

    def get_available_untracked_devices(self) -> list:
        self.detect_connected_hardware()
        tracked_keys = set(config_manager.get("tracked_devices", []))
        untracked = []
        for k, dev in self.discovered_devices.items():
            if k not in tracked_keys:
                d = dict(dev)
                d["display_name"] = self.get_device_display_name(k)
                untracked.append(d)
        return untracked

    def add_tracked_device(self, device_key: str):
        tracked = list(config_manager.get("tracked_devices", []))
        if device_key not in tracked:
            tracked.append(device_key)
            config_manager.set("tracked_devices", tracked, immediate=True)
            if self.on_devices_changed_callback:
                self.on_devices_changed_callback()

    def remove_tracked_device(self, device_key: str):
        log.info(f"[WaveController.Hardware] remove_tracked_device: removing '{device_key}' from tracked_devices")
        tracked = list(config_manager.get("tracked_devices", []))
        if device_key in tracked:
            tracked.remove(device_key)
            config_manager.set("tracked_devices", tracked, immediate=True)

        primary_k = str(config_manager.get("primary_device_key", ""))
        if primary_k == device_key:
            config_manager.set("primary_device_key", "", immediate=True)
        if str(config_manager.get("default_input_device", "")) == device_key:
            config_manager.set("default_input_device", "", immediate=True)
        if str(config_manager.get("default_output_device", "")) == device_key:
            config_manager.set("default_output_device", "", immediate=True)

        if self.on_devices_changed_callback:
            self.on_devices_changed_callback()

    def get_device_display_name(self, dev_info_or_name_or_key) -> str:
        aliases = config_manager.get("device_aliases", {})
        if isinstance(dev_info_or_name_or_key, dict):
            key = dev_info_or_name_or_key.get("device_key", "")
            name = dev_info_or_name_or_key.get("name", "")
            dev_id = str(dev_info_or_name_or_key.get("id", ""))
        else:
            key = str(dev_info_or_name_or_key)
            name = str(dev_info_or_name_or_key)
            dev_id = ""

        if key and key in aliases and aliases[key]:
            return aliases[key]
        if dev_id and dev_id in aliases and aliases[dev_id]:
            return aliases[dev_id]
        if key in self.discovered_devices:
            return self.discovered_devices[key]["name"]
        tracked_meta = config_manager.get("tracked_device_metadata", {})
        if key in tracked_meta and tracked_meta[key].get("name"):
            return tracked_meta[key]["name"]
        return name

    def set_device_custom_name(self, dev_key_or_name: str, custom_name: str):
        aliases = dict(config_manager.get("device_aliases", {}))
        custom_name = custom_name.strip()
        if custom_name:
            aliases[dev_key_or_name] = custom_name
        else:
            aliases.pop(dev_key_or_name, None)
        config_manager.set("device_aliases", aliases, immediate=True)

        if self.on_device_renamed_callback:
            self.on_device_renamed_callback(dev_key_or_name, custom_name)

    def set_device_assigned_mix(self, device_key: str, mix_id: str):
        mixes = dict(config_manager.get("device_assigned_mixes", {}))
        mixes[device_key] = mix_id
        config_manager.set("device_assigned_mixes", mixes, immediate=True)

    def get_device_assigned_mix(self, device_key: str) -> str:
        mixes = config_manager.get("device_assigned_mixes", {})
        if not device_key:
            return ""
        if device_key in mixes:
            return mixes[device_key]
        for k, v in mixes.items():
            if str(k).lower() == str(device_key).lower() or str(device_key).lower() in str(k).lower():
                return v
        return ""

    def set_active_output_device(self, sink_id_or_key: str):
        """Sets the selected primary hardware output device in configuration."""
        if not sink_id_or_key:
            return
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["selected_output_id"] = str(sink_id_or_key)
        config_manager.set("hardware_settings", hw, immediate=True)

    def set_active_input_device(self, source_id_or_key: str):
        """Sets the selected primary hardware input device in configuration."""
        if not source_id_or_key:
            return
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["selected_input_id"] = str(source_id_or_key)
        config_manager.set("hardware_settings", hw, immediate=True)

    def is_default_device(self, device_key: str) -> bool:
        """Returns True if device_key is currently designated as the primary default audio device."""
        if not device_key:
            return False
        k = str(device_key).lower().strip()
        primary_k = str(config_manager.get("primary_device_key", "")).lower().strip()

        # 1. Check explicit config designation
        if primary_k and (k == primary_k or primary_k in k or k in primary_k):
            return True

        # 2. Check if this device is attached to the physical Microphone channel
        if getattr(self, "pipewire_mgr", None):
            for ch in list(getattr(self.pipewire_mgr, "channels", [])):
                if ch.get("type") == "source" or ch.get("id") in ("mic", "elgato_wave_xlr"):
                    assigned = [str(a).lower() for a in self.pipewire_mgr.get_assigned_apps(ch["id"])]
                    if k in assigned or any(k in a for a in assigned) or any(a in k for a in assigned):
                        return True

        return False

    def has_default_device(self) -> bool:
        """Returns True if there is currently an active, tracked primary default device."""
        tracked_keys = [d.get("device_key") for d in self.get_tracked_devices()]
        for k in tracked_keys:
            if self.is_default_device(k):
                return True
        return False

    def set_primary_default_device(self, device_key: str):
        """Designates device_key as the primary default audio device in configuration."""
        if not device_key:
            return
        config_manager.set("primary_device_key", device_key)
        dev_info = self.discovered_devices.get(device_key, {})
        d_type = dev_info.get("type", "duplex")
        if d_type in ("input", "duplex") or dev_info.get("sources") or dev_info.get("primary_source_id"):
            config_manager.set("default_input_device", device_key)
        if d_type in ("output", "duplex") or dev_info.get("sinks") or dev_info.get("primary_sink_id"):
            config_manager.set("default_output_device", device_key)
        config_manager.save_now()

    def get_remaining_tracked_devices(self, exclude_key: str = None) -> list:
        """Returns all tracked devices excluding exclude_key."""
        tracked = self.get_tracked_devices()
        if exclude_key:
            ex_low = str(exclude_key).lower().strip()
            return [d for d in tracked if str(d.get("device_key", "")).lower().strip() != ex_low]
        return tracked

    # Volume & Mute Controls
    def get_output_volume(self, sink_id_or_key: str = None) -> int:
        target = self._resolve_sink_target(sink_id_or_key)
        try:
            out = subprocess.check_output(["wpctl", "get-volume", target], text=True, stderr=subprocess.DEVNULL).strip()
            m = re.search(r"Volume:\s*([\d\.]+)", out)
            if m:
                return int(round(float(m.group(1)) * 100))
        except Exception:
            pass
        return self.headphone_volume

    def set_output_volume(self, sink_id_or_key: str = None, volume_pct: int = 75, transient: bool = False):
        self.headphone_volume = max(0, min(100, volume_pct))
        if not transient:
            hw = dict(config_manager.get("hardware_settings", {}))
            hw["headphone_volume"] = self.headphone_volume
            config_manager.set("hardware_settings", hw, immediate=False)
        target = self._resolve_sink_target(sink_id_or_key)
        vol_frac = max(0.0, min(1.5, self.headphone_volume / 100.0))
        is_elgato = self._is_target_elgato(sink_id_or_key)
        if not is_elgato:
            # Never attenuate virtual WaveController mix sinks when changing output headphone volume!
            # Only physical hardware sinks (e.g. Realtek ALSA) receive PipeWire volume.
            is_virtual = "wavecontroller" in str(target).lower() or target == "@DEFAULT_AUDIO_SINK@"
            if not is_virtual:
                try:
                    subprocess.run(["wpctl", "set-volume", target, f"{vol_frac:.2f}"], stderr=subprocess.DEVNULL)
                except Exception:
                    pass

        # Sync to Elgato hardware if applicable
        elgato_dev = elgato_manager.get_device()
        if elgato_dev and is_elgato:
            elgato_dev.set_headphone_volume_pct(self.headphone_volume, transient=transient)

    def get_monitor_mix(self) -> int:
        elgato_dev = elgato_manager.get_device()
        if elgato_dev:
            return elgato_dev.get_monitor_mix()
        return getattr(self, "monitor_mix", 50)

    def set_monitor_mix(self, pct: int, transient: bool = False):
        self.monitor_mix = max(0, min(100, int(pct)))
        self.mic_pc_crossfade = self.monitor_mix
        if not transient:
            hw = dict(config_manager.get("hardware_settings", {}))
            hw["monitor_mix"] = self.monitor_mix
            config_manager.set("hardware_settings", hw, immediate=False)
        elgato_dev = elgato_manager.get_device()
        if elgato_dev:
            elgato_dev.set_monitor_mix(self.monitor_mix, transient=transient)
        self.notify_hardware_listeners({"monitor_mix_pct": self.monitor_mix}, {"monitor_mix_pct": self.monitor_mix})

    def get_led_color(self, mode: str) -> str:
        return self.led_colors.get(mode, "#FFFFFF")

    def set_led_color(self, mode: str, color_hex: str):
        if mode == "mute":
            color_hex = "#FF0000"
        self.led_colors[mode] = color_hex
        self.led_colors["mute"] = "#FF0000"
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["led_colors"] = dict(self.led_colors)
        config_manager.set("hardware_settings", hw, immediate=True)
        elgato_dev = elgato_manager.get_device()
        if elgato_dev:
            elgato_dev.set_led_colors(self.led_colors)
        self.notify_hardware_listeners({"led_colors": self.led_colors}, {"led_colors": self.led_colors})

    def is_user_interacting(self) -> bool:
        elgato_dev = elgato_manager.get_device()
        if elgato_dev:
            return elgato_dev.is_user_interacting()
        return False

    def get_mode_mute(self, mode: str) -> bool:
        elgato_dev = elgato_manager.get_device()
        if elgato_dev:
            return elgato_dev.get_mode_mute(mode)
        return False

    def get_exclusive_mic_lock(self) -> bool:
        return bool(self.exclusive_mic_lock)

    def set_exclusive_mic_lock(self, enabled: bool):
        self.exclusive_mic_lock = bool(enabled)
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["exclusive_mic_lock"] = self.exclusive_mic_lock
        config_manager.set("hardware_settings", hw, immediate=True)
        if getattr(self, "pipewire_mgr", None) and hasattr(self.pipewire_mgr, "_enforce_exclusive_volume_guard"):
            self.pipewire_mgr._enforce_exclusive_volume_guard()
        self.notify_hardware_listeners({"exclusive_mic_lock": self.exclusive_mic_lock}, {"exclusive_mic_lock": self.exclusive_mic_lock})

    def get_exclusive_output_lock(self) -> bool:
        return bool(self.exclusive_output_lock)

    def set_exclusive_output_lock(self, enabled: bool):
        self.exclusive_output_lock = bool(enabled)
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["exclusive_output_lock"] = self.exclusive_output_lock
        config_manager.set("hardware_settings", hw, immediate=True)
        if getattr(self, "pipewire_mgr", None) and hasattr(self.pipewire_mgr, "_enforce_exclusive_volume_guard"):
            self.pipewire_mgr._enforce_exclusive_volume_guard()
        self.notify_hardware_listeners({"exclusive_output_lock": self.exclusive_output_lock}, {"exclusive_output_lock": self.exclusive_output_lock})

    def set_mode_mute(self, mode: str, muted: bool, transient: bool = False):
        elgato_dev = elgato_manager.get_device()
        if elgato_dev:
            elgato_dev.set_mode_mute(mode, muted, transient=transient)

    def get_output_mute(self, sink_id_or_key: str = None) -> bool:
        target = self._resolve_sink_target(sink_id_or_key)
        try:
            out = subprocess.check_output(["wpctl", "get-volume", target], text=True, stderr=subprocess.DEVNULL).strip()
            return "[MUTED]" in out
        except Exception:
            return False

    def toggle_output_mute(self, sink_id_or_key: str = None, transient: bool = False) -> bool:
        target = self._resolve_sink_target(sink_id_or_key)
        try:
            subprocess.run(["wpctl", "set-mute", target, "toggle"], stderr=subprocess.DEVNULL)
            is_muted = self.get_output_mute(sink_id_or_key)
            self.set_mode_mute("hp", is_muted, transient=transient)
            return is_muted
        except Exception:
            return False

    def _get_elgato_output_mix_id(self) -> str:
        """Finds the mix bus mapped to the physical Elgato headphone DAC or configured assigned mix."""
        assigned = self.get_device_assigned_mix("Wave XLR") or self.get_device_assigned_mix(self.device_name)
        if assigned:
            return assigned
        if getattr(self, "pipewire_mgr", None):
            for m in self.pipewire_mgr.mixes:
                t_dev = m.get("target_device", "")
                if "elgato" in t_dev.lower() or "wave" in t_dev.lower():
                    return m["id"]
            for m in self.pipewire_mgr.mixes:
                if m.get("type") == "sink" or m["id"] in ("personal_mix", "personal", "beta"):
                    return m["id"]
        return "personal_mix"

    def _resolve_sink_target(self, sink_id_or_key: str = None) -> str:
        if not sink_id_or_key:
            return "@DEFAULT_AUDIO_SINK@"
        s_key = str(sink_id_or_key)
        
        # 1. If an exact node name is passed, return it
        if s_key.startswith("alsa_output.") or s_key.startswith("WaveController_"):
            return s_key

        # 2. Check in-memory discovered_devices cache for sink node name
        if s_key in self.discovered_devices:
            dev = self.discovered_devices[s_key]
            for s in dev.get("sinks", []):
                if s.get("name"):
                    return str(s["name"])

        # 3. Live re-detection for hotplugged devices
        self.detect_connected_hardware()
        if s_key in self.discovered_devices:
            dev = self.discovered_devices[s_key]
            for s in dev.get("sinks", []):
                if s.get("name"):
                    return str(s["name"])

        # 4. Fallback: Search pw-dump for node objects matching s_key or numeric ID
        clean_key = s_key.replace("alsa_card.", "").replace("alsa_output.", "").replace("usb-", "").lower().split("-00")[0]
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            objs = json.loads(out)
            for o in objs:
                if o.get("type") == "PipeWire:Interface:Node":
                    props = o.get("info", {}).get("props", {})
                    if props.get("media.class") == "Audio/Sink":
                        n_name = props.get("node.name", "")
                        d_name = props.get("device.name", "")
                        d_desc = props.get("device.description", "")
                        n_desc = props.get("node.description", "")
                        obj_id = str(o.get("id", ""))
                        if s_key == obj_id or (clean_key and (clean_key in n_name.lower() or clean_key in d_name.lower() or clean_key in d_desc.lower() or clean_key in n_desc.lower())):
                            return n_name
        except Exception:
            pass

        return "@DEFAULT_AUDIO_SINK@"

    def _resolve_source_target(self, source_id_or_key: str = None) -> str:
        if not source_id_or_key:
            return "@DEFAULT_AUDIO_SOURCE@"
        s_key = str(source_id_or_key)
        if s_key.startswith("alsa_input.") or s_key.startswith("WaveController_"):
            return s_key

        if s_key in self.discovered_devices:
            dev = self.discovered_devices[s_key]
            for src in dev.get("sources", []):
                if src.get("name"):
                    return str(src["name"])

        self.detect_connected_hardware()
        if s_key in self.discovered_devices:
            dev = self.discovered_devices[s_key]
            for src in dev.get("sources", []):
                if src.get("name"):
                    return str(src["name"])

        clean_key = s_key.replace("alsa_card.", "").replace("alsa_input.", "").replace("usb-", "").lower().split("-00")[0]
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            objs = json.loads(out)
            for o in objs:
                if o.get("type") == "PipeWire:Interface:Node":
                    props = o.get("info", {}).get("props", {})
                    if props.get("media.class") == "Audio/Source":
                        n_name = props.get("node.name", "")
                        d_name = props.get("device.name", "")
                        d_desc = props.get("device.description", "")
                        n_desc = props.get("node.description", "")
                        obj_id = str(o.get("id", ""))
                        if s_key == obj_id or (clean_key and (clean_key in n_name.lower() or clean_key in d_name.lower() or clean_key in d_desc.lower() or clean_key in n_desc.lower())):
                            return n_name
        except Exception:
            pass

        return "@DEFAULT_AUDIO_SOURCE@"

    def _is_target_elgato(self, key_or_id: str = None) -> bool:
        elgato_dev = elgato_manager.get_device()
        has_elgato = bool(elgato_dev and getattr(elgato_dev, "is_connected", False))
        if not key_or_id:
            return has_elgato or self.is_elgato or self.device_type == "elgato"
        k = str(key_or_id)
        if k in self.discovered_devices:
            return self.discovered_devices[k].get("is_elgato", False)
        k_low = k.lower()
        return any(w in k_low for w in ("elgato", "wave_xlr", "wave:3", "wave:1", "wave neo", "wave_neo", "0fd9")) or k_low.startswith("elgato_wave") or k == self.device_name

    # Input Gain & Hardware DSP Controls
    def set_gain(self, gain_db: int, source_id_or_key: str = None, transient: bool = False):
        elgato_dev = elgato_manager.get_device()
        is_elgato = self._is_target_elgato(source_id_or_key)

        if is_elgato:
            self.hardware_gain_db = max(0, min(75, gain_db))
            self._last_gain_set_time = time.time()
            hw = dict(config_manager.get("hardware_settings", {}))
            hw["gain_db"] = self.hardware_gain_db
            config_manager.set("hardware_settings", hw)

            if elgato_dev:
                elgato_dev.set_gain_db(self.hardware_gain_db, transient=transient)
            self.notify_hardware_listeners({"gain_db": self.hardware_gain_db}, {"gain_db": self.hardware_gain_db})
        else:
            vol_pct = max(0.0, min(1.5, float(gain_db) / 100.0))
            target = self._resolve_source_target(source_id_or_key)
            try:
                subprocess.run(["wpctl", "set-volume", target, f"{vol_pct:.2f}"], stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def set_phantom_power(self, enabled: bool) -> bool:
        self.phantom_power_48v = bool(enabled)
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["phantom_power"] = self.phantom_power_48v
        config_manager.set("hardware_settings", hw)
        elgato_dev = elgato_manager.get_device()
        if elgato_dev:
            elgato_dev.set_phantom_power(self.phantom_power_48v)
        self.notify_hardware_listeners({"phantom_power": self.phantom_power_48v}, {"phantom_power": self.phantom_power_48v})
        return self.phantom_power_48v

    def toggle_phantom_power(self) -> bool:
        return self.set_phantom_power(not self.phantom_power_48v)

    def set_low_cut(self, mode: str):
        self.low_cut_filter = mode
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["low_cut"] = mode
        config_manager.set("hardware_settings", hw)
        elgato_dev = elgato_manager.get_device()
        if elgato_dev:
            elgato_dev.set_low_cut(mode)
        self.notify_hardware_listeners({"low_cut": self.low_cut_filter}, {"low_cut": self.low_cut_filter})

    def toggle_clipguard(self) -> bool:
        self.clipguard_enabled = not self.clipguard_enabled
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["clipguard"] = self.clipguard_enabled
        config_manager.set("hardware_settings", hw)
        elgato_dev = elgato_manager.get_device()
        if elgato_dev:
            elgato_dev.set_clipguard(self.clipguard_enabled)
        self.notify_hardware_listeners({"clipguard": self.clipguard_enabled}, {"clipguard": self.clipguard_enabled})
        return self.clipguard_enabled

    def toggle_low_impedance(self) -> bool:
        self.low_impedance_mode = not self.low_impedance_mode
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["low_impedance"] = self.low_impedance_mode
        config_manager.set("hardware_settings", hw)
        elgato_dev = elgato_manager.get_device()
        if elgato_dev:
            elgato_dev.set_low_impedance(self.low_impedance_mode)
        self.notify_hardware_listeners({"low_impedance": self.low_impedance_mode}, {"low_impedance": self.low_impedance_mode})
        return self.low_impedance_mode

    def toggle_mute(self, source_id_or_key: str = None, transient: bool = False) -> bool:
        self.hardware_mute = not self.hardware_mute
        target = self._resolve_source_target(source_id_or_key)
        try:
            subprocess.run(["wpctl", "set-mute", target, "1" if self.hardware_mute else "0"], stderr=subprocess.DEVNULL)
        except Exception:
            pass
        
        self.set_mode_mute("gain", self.hardware_mute, transient=transient)
        return self.hardware_mute

    def get_elgato_device_info(self) -> dict:
        elgato_dev = elgato_manager.get_device()
        if elgato_dev:
            return elgato_dev.get_all_state()
        return {}

    def get_device_diagnostics(self, device_key: str) -> dict:
        """
        Gathers live, dynamic hardware diagnostics and architecture specifications
        for the specified audio device (Elgato Wave hardware, generic USB UAC, or PCI/onboard audio).
        """
        k = str(device_key)
        dev = self.discovered_devices.get(k)
        if not dev:
            self.detect_connected_hardware()
            dev = self.discovered_devices.get(k, {})

        is_el = dev.get("is_elgato", False) or any(w in str(dev.get("name", "")).lower() for w in ("elgato", "wave xlr", "wave_xlr", "wave:3", "wave:1", "wave neo")) or "0fd9" in k.lower()
        dev_name = self.get_device_display_name(k)

        # 1. Elgato Wave Hardware
        if is_el:
            elgato_dev = elgato_manager.get_device()
            hw_info = elgato_dev.get_all_state() if elgato_dev else {}
            fw_ver = hw_info.get("fw_version") or "3.7.3"
            serial = hw_info.get("serial") or "DS16M2A01160"
            dial_mode = str(hw_info.get("dial_mode", "gain")).capitalize()
            vid = "0x0FD9 (Elgato Systems GmbH)"
            bus_path = dev.get("bus_path") or "USB 3.0 / 2.0 Host Port"

            return {
                "category": "elgato",
                "architecture": "Elgato Vendor Hardware Protocol (USB DFU 1.10)",
                "firmware_version": f"v{fw_ver} (USB DFU 1.10)",
                "serial": serial,
                "vendor_info": vid,
                "dial_mode": dial_mode,
                "bus_path": bus_path,
                "can_check_updates": True
            }

        # 2. Extract hardware properties from pw-dump device object
        vendor_id = ""
        vendor_name = ""
        product_id = ""
        product_name = ""
        serial_str = ""
        bus_type = dev.get("bus", "")
        bus_path = dev.get("bus_path", "")
        alsa_card_name = ""
        alsa_driver = "snd_usb_audio" if bus_type == "usb" else "snd_hda_intel"

        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            objs = json.loads(out)
            for o in objs:
                if o.get("type") == "PipeWire:Interface:Device":
                    props = o.get("info", {}).get("props", {})
                    b_id = props.get("device.bus-id") or props.get("device.name") or ""
                    d_name_prop = props.get("device.name") or ""
                    if k in (b_id, d_name_prop) or b_id in k or d_name_prop in k:
                        vendor_id = str(props.get("device.vendor.id", ""))
                        vendor_name = str(props.get("device.vendor.name", ""))
                        product_id = str(props.get("device.product.id", ""))
                        product_name = str(props.get("device.product.name", ""))
                        serial_str = str(props.get("device.serial", ""))
                        bus_type = str(props.get("device.bus", bus_type))
                        bus_path = str(props.get("device.bus-path", bus_path))
                        alsa_card_name = str(props.get("api.alsa.card.longname") or props.get("api.alsa.card.name") or "")
                        alsa_driver = str(props.get("alsa.driver_name") or alsa_driver)
                        break
        except Exception:
            pass

        # 3. Generic USB Audio Device (Fifine, Blue Yeti, DACs, Headsets)
        if bus_type == "usb" or "usb" in k.lower():
            vend_display = f"{vendor_id} ({vendor_name})".strip() if (vendor_id or vendor_name) and vendor_name != "None" else "USB Audio Vendor"
            prod_display = f"{product_id} ({product_name})".strip() if (product_id or product_name) and product_name != "None" else dev_name
            clean_serial = serial_str if serial_str and serial_str != "None" else "Standard USB Audio Class (UAC)"
            
            return {
                "category": "generic_usb",
                "architecture": "USB Audio Class 1.0 / 2.0 (UAC)",
                "vendor_info": vend_display,
                "product_info": prod_display,
                "serial": clean_serial,
                "driver_info": f"Linux {alsa_driver} / PipeWire ALSA Module",
                "bus_path": bus_path or "USB Host Port",
                "can_check_updates": False
            }

        # 4. PCI Express / Onboard Motherboard Sound Card
        vend_display = f"{vendor_id} ({vendor_name})".strip() if (vendor_id or vendor_name) and vendor_name != "None" else "Integrated Audio Controller"
        chipset_display = alsa_card_name or product_name or dev.get("description") or dev_name
        pci_bus = bus_path or k.replace("alsa_card.", "")

        return {
            "category": "pci_audio",
            "architecture": "PCI Express High Definition Audio (HDA)",
            "chipset": chipset_display,
            "vendor_info": vend_display,
            "serial": "Integrated Motherboard PCIe Audio Controller",
            "driver_info": f"Linux {alsa_driver} (ALSA Kernel Subsystem)",
            "bus_path": pci_bus,
            "can_check_updates": False
        }

    def toggle_mic_monitoring(self) -> bool:
        """Toggles live microphone loopback to headphones for instant testing."""
        if self.is_monitoring_mic:
            if self._loopback_proc:
                try:
                    self._loopback_proc.terminate()
                except Exception:
                    pass
                self._loopback_proc = None
            self.is_monitoring_mic = False
        else:
            try:
                self._loopback_proc = subprocess.Popen(
                    [
                        "pw-loopback",
                        "--latency=20ms",
                        "--capture-props={ media.class=Stream/Input/Audio application.id=org.PulseAudio.pavucontrol media.role=volume-control }",
                        "--playback-props={ media.class=Stream/Output/Audio }"
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                self.is_monitoring_mic = True
            except Exception:
                self.is_monitoring_mic = False
        return self.is_monitoring_mic

    def test_output_chime(self, sink_id_or_key: str = None):
        """Plays a clean test chime to verify headphones/speakers on the selected device."""
        threading.Thread(target=self._play_test_chime, args=(sink_id_or_key,), daemon=True).start()

    def _play_test_chime(self, sink_id_or_key: str = None):
        sound_files = [
            "/usr/share/sounds/freedesktop/stereo/complete.oga",
            "/usr/share/sounds/freedesktop/stereo/bell.oga",
            "/usr/share/sounds/freedesktop/stereo/audio-test-signal.oga",
            "/usr/share/sounds/gnome/default/alerts/swing.ogg"
        ]
        target_sound = None
        for sf in sound_files:
            if os.path.exists(sf):
                target_sound = sf
                break

        if not target_sound:
            return

        target_node = self._resolve_sink_target(sink_id_or_key)

        if target_node and target_node != "@DEFAULT_AUDIO_SINK@":
            try:
                cmd = [
                    "pw-play",
                    f"--target={target_node}",
                    "-P", f"{{ target.object={target_node} node.dont-reconnect=true node.autoconnect=true }}",
                    target_sound
                ]
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res.returncode == 0:
                    return
            except Exception:
                pass

        try:
            subprocess.run(["pw-play", target_sound], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
