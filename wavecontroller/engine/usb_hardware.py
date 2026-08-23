import os
import subprocess
import threading
import time
import json
import re

from .config_manager import config_manager

class USBHardwareManager:
    """
    Hardware integration layer managing physical Audio Input, Output, and Duplex devices.
    Groups composite USB microphones with monitoring headphone outputs (e.g. Fifine, Wave:3).
    Provides user-curated device management (Add/Remove devices, persistent nicknames, volume, gain, DSP).
    """

    def __init__(self):
        self.device_name = "fifine Microphone"
        self.device_type = "generic" # 'elgato' or 'generic'
        self.discovered_devices = {} # {device_key: dev_info_dict}
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
        self.headphone_volume = 70
        self.mic_pc_crossfade = 50
        
        self.is_monitoring_mic = False
        self._loopback_proc = None
        self.on_device_renamed_callback = None
        self.on_devices_changed_callback = None

        self.detect_connected_hardware()
        self._ensure_default_tracked_devices()

    def _ensure_default_tracked_devices(self):
        """Initializes tracked devices list on first run if empty."""
        tracked = config_manager.get("tracked_devices", None)
        if tracked is None:
            new_tracked = []
            # Auto-add primary duplex / input device
            for k, dev in self.discovered_devices.items():
                if dev.get("type") in ["duplex", "input"]:
                    new_tracked.append(k)
                    break
            # Auto-add primary output device if different
            for k, dev in self.discovered_devices.items():
                if k not in new_tracked and dev.get("type") in ["duplex", "output"]:
                    new_tracked.append(k)
                    break
            
            # If still empty, add any first discovered device
            if not new_tracked and self.discovered_devices:
                new_tracked.append(list(self.discovered_devices.keys())[0])

            config_manager.set("tracked_devices", new_tracked, immediate=True)

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
                d["icon"] = "audio-headset-symbolic" if any(x in d["name"].lower() for x in ["head", "fifine", "wave"]) else "audio-input-microphone-symbolic"
            elif num_in > 0:
                d["type"] = "input"
                d["badge"] = "In"
                d["icon"] = "audio-input-microphone-symbolic"
            else:
                d["type"] = "output"
                d["badge"] = "Out"
                d["icon"] = "audio-headphones-symbolic" if "head" in d["name"].lower() else "audio-speakers-symbolic"
            
            d["primary_source_id"] = d["sources"][0]["id"] if d["sources"] else None
            d["primary_sink_id"] = d["sinks"][0]["id"] if d["sinks"] else None
            d["connected"] = True
            hw_map[d["device_key"]] = d

        # Check any orphaned nodes without a parent device
        for n in nodes:
            dev_id = n.get("device_id")
            if not dev_id or dev_id not in devices:
                key = n["name"]
                dtype = "input" if n["media_class"] == "Audio/Source" else "output"
                hw_map[key] = {
                    "device_id": n["id"],
                    "device_key": key,
                    "name": n["description"] or n["name"],
                    "description": n["description"] or n["name"],
                    "bus": "",
                    "form_factor": "",
                    "type": dtype,
                    "badge": "In" if dtype == "input" else "Out",
                    "icon": "audio-input-microphone-symbolic" if dtype == "input" else "audio-headphones-symbolic",
                    "sources": [n] if dtype == "input" else [],
                    "sinks": [n] if dtype == "output" else [],
                    "primary_source_id": n["id"] if dtype == "input" else None,
                    "primary_sink_id": n["id"] if dtype == "output" else None,
                    "connected": True
                }

        self.discovered_devices = hw_map

        # Legacy lists for backward compatibility
        inputs = []
        outputs = []
        for k, d in hw_map.items():
            for s in d.get("sources", []):
                inputs.append({"id": str(s["id"]), "name": d["name"], "is_default": False, "type": "source", "device_key": k})
            for s in d.get("sinks", []):
                outputs.append({"id": str(s["id"]), "name": d["name"], "is_default": False, "type": "sink", "device_key": k})
        
        self.input_devices = inputs
        self.output_devices = outputs
        self.connected_audio_devices = list(hw_map.values())

        # Determine primary mic
        for k, d in hw_map.items():
            if d.get("type") in ["duplex", "input"]:
                self.device_name = d["name"]
                self.device_type = "elgato" if "wave" in d["name"].lower() else "generic"
                break

    def get_tracked_devices(self) -> list:
        """Returns the list of user-tracked devices hydrated with live state."""
        self.detect_connected_hardware()
        tracked_keys = config_manager.get("tracked_devices", [])
        aliases = config_manager.get("device_aliases", {})
        assigned_mixes = config_manager.get("device_assigned_mixes", {})

        result = []
        for key in tracked_keys:
            if key in self.discovered_devices:
                dev = dict(self.discovered_devices[key])
            else:
                # Disconnected device
                dev = {
                    "device_key": key,
                    "name": aliases.get(key, key),
                    "description": "Hardware Disconnected",
                    "type": "duplex",
                    "badge": "Offline",
                    "icon": "network-offline-symbolic",
                    "sources": [],
                    "sinks": [],
                    "primary_source_id": None,
                    "primary_sink_id": None,
                    "connected": False
                }
            
            dev["display_name"] = aliases.get(key, dev["name"])
            dev["custom_name"] = aliases.get(key, "")
            dev["assigned_mix"] = assigned_mixes.get(key, "personal_mix")
            result.append(dev)
        return result

    def get_available_untracked_devices(self) -> list:
        """Returns discovered hardware devices that are not currently tracked."""
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
        """Adds a device to the user's tracked devices list."""
        tracked = list(config_manager.get("tracked_devices", []))
        if device_key not in tracked:
            tracked.append(device_key)
            config_manager.set("tracked_devices", tracked, immediate=True)
            if self.on_devices_changed_callback:
                self.on_devices_changed_callback()

    def remove_tracked_device(self, device_key: str):
        """Removes a device from the user's tracked devices list."""
        tracked = list(config_manager.get("tracked_devices", []))
        if device_key in tracked:
            tracked.remove(device_key)
            config_manager.set("tracked_devices", tracked, immediate=True)
            if self.on_devices_changed_callback:
                self.on_devices_changed_callback()

    def get_device_display_name(self, dev_info_or_name_or_key) -> str:
        """Returns the custom nickname for a device if configured, else the hardware name."""
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
        if name and name in aliases and aliases[name]:
            return aliases[name]

        # Lookup in discovered devices
        if key in self.discovered_devices:
            return self.discovered_devices[key]["name"]
        return name

    def set_device_custom_name(self, dev_key_or_name: str, custom_name: str):
        """Sets a persistent custom nickname for an audio device."""
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
        """Saves which WaveController sub-mix feeds into this device's output sink."""
        mixes = dict(config_manager.get("device_assigned_mixes", {}))
        mixes[device_key] = mix_id
        config_manager.set("device_assigned_mixes", mixes, immediate=True)

    def get_device_assigned_mix(self, device_key: str) -> str:
        mixes = config_manager.get("device_assigned_mixes", {})
        return mixes.get(device_key, "personal_mix")

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
        return 75

    def set_output_volume(self, sink_id_or_key: str = None, volume_pct: int = 75):
        target = self._resolve_sink_target(sink_id_or_key)
        vol_frac = max(0.0, min(1.5, volume_pct / 100.0))
        try:
            subprocess.run(["wpctl", "set-volume", target, f"{vol_frac:.2f}"], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def get_output_mute(self, sink_id_or_key: str = None) -> bool:
        target = self._resolve_sink_target(sink_id_or_key)
        try:
            out = subprocess.check_output(["wpctl", "get-volume", target], text=True, stderr=subprocess.DEVNULL).strip()
            return "[MUTED]" in out
        except Exception:
            return False

    def toggle_output_mute(self, sink_id_or_key: str = None) -> bool:
        target = self._resolve_sink_target(sink_id_or_key)
        try:
            subprocess.run(["wpctl", "set-mute", target, "toggle"], stderr=subprocess.DEVNULL)
            return self.get_output_mute(sink_id_or_key)
        except Exception:
            return False

    def _resolve_sink_target(self, sink_id_or_key: str = None) -> str:
        if not sink_id_or_key:
            return "@DEFAULT_AUDIO_SINK@"
        s_key = str(sink_id_or_key)
        if s_key.isdigit():
            return s_key
        if s_key in self.discovered_devices:
            dev = self.discovered_devices[s_key]
            if dev.get("primary_sink_id"):
                return str(dev["primary_sink_id"])
        return "@DEFAULT_AUDIO_SINK@"

    def _resolve_source_target(self, source_id_or_key: str = None) -> str:
        if not source_id_or_key:
            return "@DEFAULT_AUDIO_SOURCE@"
        s_key = str(source_id_or_key)
        if s_key.isdigit():
            return s_key
        if s_key in self.discovered_devices:
            dev = self.discovered_devices[s_key]
            if dev.get("primary_source_id"):
                return str(dev["primary_source_id"])
        return "@DEFAULT_AUDIO_SOURCE@"

    # Input Gain & Hardware DSP Controls
    def set_gain(self, gain_db: int, source_id_or_key: str = None):
        self.hardware_gain_db = max(0, min(75, gain_db))
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["gain_db"] = self.hardware_gain_db
        config_manager.set("hardware_settings", hw)

        if self.device_type == "elgato":
            self._send_elgato_control(cmd=0x01, val=self.hardware_gain_db)
        else:
            vol_pct = self.hardware_gain_db / 75.0
            target = self._resolve_source_target(source_id_or_key)
            try:
                subprocess.run(["wpctl", "set-volume", target, f"{vol_pct:.2f}"], stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def toggle_phantom_power(self) -> bool:
        if self.device_type == "elgato":
            self.phantom_power_48v = not self.phantom_power_48v
            hw = dict(config_manager.get("hardware_settings", {}))
            hw["phantom_power"] = self.phantom_power_48v
            config_manager.set("hardware_settings", hw)
            self._send_elgato_control(cmd=0x02, val=1 if self.phantom_power_48v else 0)
        return self.phantom_power_48v

    def set_low_cut(self, mode: str):
        self.low_cut_filter = mode
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["low_cut"] = mode
        config_manager.set("hardware_settings", hw)
        if self.device_type == "elgato":
            val = 0 if mode == "Off" else (1 if mode == "80Hz" else 2)
            self._send_elgato_control(cmd=0x03, val=val)

    def toggle_clipguard(self) -> bool:
        self.clipguard_enabled = not self.clipguard_enabled
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["clipguard"] = self.clipguard_enabled
        config_manager.set("hardware_settings", hw)
        if self.device_type == "elgato":
            self._send_elgato_control(cmd=0x04, val=1 if self.clipguard_enabled else 0)
        return self.clipguard_enabled

    def toggle_mute(self, source_id_or_key: str = None) -> bool:
        self.hardware_mute = not self.hardware_mute
        target = self._resolve_source_target(source_id_or_key)
        try:
            subprocess.run(["wpctl", "set-mute", target, "1" if self.hardware_mute else "0"], stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return self.hardware_mute

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
                    ["pw-loopback", "--latency=20ms", "--capture-props=media.class=Stream/Input/Audio", "--playback-props=media.class=Stream/Output/Audio"],
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

        target_dev = self._resolve_sink_target(sink_id_or_key)

        # 1. Direct pw-play targeting the specific sink node
        if target_dev and target_dev.isdigit():
            try:
                res = subprocess.run(["pw-play", f"--target={target_dev}", target_sound], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res.returncode == 0:
                    return
            except Exception:
                pass

        # 2. paplay with device parameter
        try:
            cmd = ["paplay"]
            if target_dev and target_dev != "@DEFAULT_AUDIO_SINK@":
                cmd.extend(["--device", target_dev])
            cmd.append(target_sound)
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0:
                return
        except Exception:
            pass

        # 3. Fallback to default pw-play
        try:
            subprocess.run(["pw-play", target_sound], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    # Legacy method compatibility
    def set_active_input_device(self, device_id: str):
        try:
            subprocess.run(["wpctl", "set-default", str(device_id)], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def set_active_output_device(self, device_id: str):
        try:
            subprocess.run(["wpctl", "set-default", str(device_id)], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _send_elgato_control(self, cmd: int, val: int):
        pass
