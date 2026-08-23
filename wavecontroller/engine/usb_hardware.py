import subprocess
import threading
import time

from .config_manager import config_manager

class USBHardwareManager:
    """
    Hardware integration layer strictly managing Audio Input & Output devices.
    Filters out webcams, stream decks, video capture, and non-audio USB peripherals.
    Supports persistent custom nicknames and output device mute controls.
    """

    def __init__(self):
        self.device_name = "fifine Microphone"
        self.device_type = "generic" # 'elgato' or 'generic'
        self.input_devices = [] # [{"id": "...", "name": "...", "is_default": bool}]
        self.output_devices = [] # [{"id": "...", "name": "...", "is_default": bool}]
        self.connected_audio_devices = [] # Sidebar list
        
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

        self.detect_connected_hardware()

    def get_device_display_name(self, dev_info_or_name) -> str:
        """Returns the custom nickname for a device if configured, else the original name."""
        if isinstance(dev_info_or_name, dict):
            name = dev_info_or_name.get("name", "")
            dev_id = str(dev_info_or_name.get("id", ""))
        else:
            name = str(dev_info_or_name)
            dev_id = ""

        aliases = config_manager.get("device_aliases", {})
        if dev_id and dev_id in aliases:
            return aliases[dev_id]
        if name in aliases:
            return aliases[name]
        return name

    def set_device_custom_name(self, dev_name_or_id: str, custom_name: str):
        """Sets a persistent custom nickname for an audio device."""
        aliases = dict(config_manager.get("device_aliases", {}))
        custom_name = custom_name.strip()
        if custom_name:
            aliases[dev_name_or_id] = custom_name
        else:
            aliases.pop(dev_name_or_id, None)
        config_manager.set("device_aliases", aliases, immediate=True)

        if self.on_device_renamed_callback:
            self.on_device_renamed_callback(dev_name_or_id, custom_name)

    def detect_connected_hardware(self):
        """Discovers valid audio input and output devices using PipeWire wpctl."""
        inputs = []
        outputs = []
        sidebar_devs = []

        try:
            out = subprocess.check_output(["wpctl", "status"], text=True, stderr=subprocess.DEVNULL)
            in_sinks = False
            in_sources = False

            for line in out.splitlines():
                line_raw = line.strip()
                if "Sinks:" in line_raw:
                    in_sinks = True
                    in_sources = False
                    continue
                elif "Sources:" in line_raw:
                    in_sinks = False
                    in_sources = True
                    continue
                elif "Filters:" in line_raw or "Streams:" in line_raw or "Video" in line_raw or "Settings" in line_raw:
                    in_sinks = False
                    in_sources = False
                    continue

                if not line_raw or line_raw.startswith("├") or line_raw.startswith("└") or line_raw.startswith("│"):
                    is_def = "*" in line_raw
                    clean = line_raw.replace("├─", "").replace("└─", "").replace("│", "").replace("*", "").strip()
                    if clean and clean[0].isdigit():
                        tokens = clean.split(".", 1)
                        if len(tokens) == 2:
                            node_id = tokens[0].strip()
                            name = tokens[1].split("[")[0].strip()
                            
                            # Filter out non-audio and internal monitors
                            name_lower = name.lower()
                            if any(x in name_lower for x in ["facecam", "cam", "video", "virtual", "null"]):
                                continue

                            dev_info = {
                                "id": node_id,
                                "name": name,
                                "is_default": is_def
                            }

                            if in_sources:
                                dev_info["type"] = "source"
                                dev_info["icon"] = "audio-input-microphone-symbolic"
                                inputs.append(dev_info)
                                sidebar_devs.append(dev_info)
                            elif in_sinks:
                                dev_info["type"] = "sink"
                                dev_info["icon"] = "audio-headphones-symbolic"
                                outputs.append(dev_info)

        except Exception:
            pass

        # Fallback if wpctl empty
        if not inputs:
            inputs.append({"id": "69", "name": "fifine Microphone", "type": "source", "icon": "audio-input-microphone-symbolic", "is_default": True})
            sidebar_devs.append(inputs[0])
        if not outputs:
            outputs.append({"id": "59", "name": "Analog Stereo Output", "type": "sink", "icon": "audio-headphones-symbolic", "is_default": True})

        self.input_devices = inputs
        self.output_devices = outputs
        self.connected_audio_devices = sidebar_devs

        # Determine primary mic
        for d in inputs:
            if d.get("is_default") or "fifine" in d["name"].lower() or "wave" in d["name"].lower():
                self.device_name = d["name"]
                self.device_type = "elgato" if "wave" in d["name"].lower() else "generic"
                break

    def get_output_volume(self, device_id: str = None) -> int:
        """Returns current volume percentage for the output device."""
        target = str(device_id) if device_id else "@DEFAULT_AUDIO_SINK@"
        try:
            out = subprocess.check_output(["wpctl", "get-volume", target], text=True, stderr=subprocess.DEVNULL).strip()
            import re
            m = re.search(r'Volume:\s*([\d\.]+)', out)
            if m:
                return int(round(float(m.group(1)) * 100))
        except Exception:
            pass
        return 75

    def set_output_volume(self, device_id: str = None, volume_pct: int = 75):
        """Sets volume percentage for the output device."""
        target = str(device_id) if device_id else "@DEFAULT_AUDIO_SINK@"
        vol_frac = max(0.0, min(1.5, volume_pct / 100.0))
        try:
            subprocess.run(["wpctl", "set-volume", target, f"{vol_frac:.2f}"], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def get_output_mute(self, device_id: str = None) -> bool:
        """Returns True if the specified or default output device is muted."""
        target = str(device_id) if device_id else "@DEFAULT_AUDIO_SINK@"
        try:
            out = subprocess.check_output(["wpctl", "get-volume", target], text=True, stderr=subprocess.DEVNULL).strip()
            return "[MUTED]" in out
        except Exception:
            return False

    def toggle_output_mute(self, device_id: str = None) -> bool:
        """Toggles mute on the specified or default output device."""
        target = str(device_id) if device_id else "@DEFAULT_AUDIO_SINK@"
        try:
            subprocess.run(["wpctl", "set-mute", target, "toggle"], stderr=subprocess.DEVNULL)
            return self.get_output_mute(device_id)
        except Exception:
            return False

    def set_output_mute(self, device_id: str = None, muted: bool = True) -> bool:
        """Sets mute on the specified or default output device."""
        target = str(device_id) if device_id else "@DEFAULT_AUDIO_SINK@"
        try:
            subprocess.run(["wpctl", "set-mute", target, "1" if muted else "0"], stderr=subprocess.DEVNULL)
            return muted
        except Exception:
            return False

    def set_active_input_device(self, device_id: str):
        """Sets the selected audio input device as default in PipeWire."""
        try:
            subprocess.run(["wpctl", "set-default", str(device_id)], stderr=subprocess.DEVNULL)
            for d in self.input_devices:
                if d["id"] == device_id:
                    self.device_name = d["name"]
                    self.device_type = "elgato" if "wave" in d["name"].lower() else "generic"
                    d["is_default"] = True
                else:
                    d["is_default"] = False
            
            hw = dict(config_manager.get("hardware_settings", {}))
            hw["selected_input_id"] = str(device_id)
            config_manager.set("hardware_settings", hw)
        except Exception:
            pass

    def set_active_output_device(self, device_id: str):
        """Sets the selected audio output device as default in PipeWire."""
        try:
            subprocess.run(["wpctl", "set-default", str(device_id)], stderr=subprocess.DEVNULL)
            for d in self.output_devices:
                d["is_default"] = (d["id"] == device_id)

            hw = dict(config_manager.get("hardware_settings", {}))
            hw["selected_output_id"] = str(device_id)
            config_manager.set("hardware_settings", hw)
        except Exception:
            pass

    def test_output_chime(self, device_id: str = None):
        """Plays a clean test chime to verify headphones/speakers on the selected device."""
        threading.Thread(target=self._play_test_chime, args=(device_id,), daemon=True).start()

    def _play_test_chime(self, device_id: str = None):
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

        target_dev = str(device_id) if device_id else None
        if not target_dev:
            for d in self.output_devices:
                if d.get("is_default"):
                    target_dev = str(d["id"])
                    break

        # 1. Direct pw-play targeting the specific sink node
        if target_dev:
            try:
                res = subprocess.run(["pw-play", f"--target={target_dev}", target_sound], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res.returncode == 0:
                    return
            except Exception:
                pass

        # 2. paplay with device parameter
        try:
            cmd = ["paplay"]
            if target_dev:
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

    def set_gain(self, gain_db: int):
        self.hardware_gain_db = max(0, min(75, gain_db))
        hw = dict(config_manager.get("hardware_settings", {}))
        hw["gain_db"] = self.hardware_gain_db
        config_manager.set("hardware_settings", hw)

        if self.device_type == "elgato":
            self._send_elgato_control(cmd=0x01, val=self.hardware_gain_db)
        else:
            vol_pct = self.hardware_gain_db / 75.0
            try:
                subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", f"{vol_pct:.2f}"], stderr=subprocess.DEVNULL)
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

    def toggle_mute(self) -> bool:
        self.hardware_mute = not self.hardware_mute
        try:
            subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1" if self.hardware_mute else "0"], stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return self.hardware_mute

    def _send_elgato_control(self, cmd: int, val: int):
        pass
