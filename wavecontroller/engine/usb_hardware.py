import subprocess
import threading
import time

class USBHardwareManager:
    """
    Hardware integration layer strictly managing Audio Input & Output devices.
    Filters out webcams, stream decks, video capture, and non-audio USB peripherals.
    """

    def __init__(self):
        self.device_name = "fifine Microphone"
        self.device_type = "generic" # 'elgato' or 'generic'
        self.input_devices = [] # [{"id": "...", "name": "...", "is_default": bool}]
        self.output_devices = [] # [{"id": "...", "name": "...", "is_default": bool}]
        self.connected_audio_devices = [] # Sidebar list
        
        self.hardware_gain_db = 45 # 0 to 75 dB
        self.phantom_power_48v = False
        self.clipguard_enabled = True
        self.low_cut_filter = "80Hz" # 'Off', '80Hz', '120Hz'
        self.hardware_mute = False
        self.headphone_volume = 70
        self.mic_pc_crossfade = 50
        
        self.is_monitoring_mic = False
        self._loopback_proc = None

        self.detect_connected_hardware()

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
        except Exception:
            pass

    def set_active_output_device(self, device_id: str):
        """Sets the selected audio output device as default in PipeWire."""
        try:
            subprocess.run(["wpctl", "set-default", str(device_id)], stderr=subprocess.DEVNULL)
            for d in self.output_devices:
                d["is_default"] = (d["id"] == device_id)
        except Exception:
            pass

    def test_output_chime(self):
        """Plays a clean test sound to verify headphones/speakers."""
        threading.Thread(target=self._play_test_chime, daemon=True).start()

    def _play_test_chime(self):
        for sound_file in [
            "/usr/share/sounds/freedesktop/stereo/bell.oga",
            "/usr/share/sounds/freedesktop/stereo/complete.oga",
            "/usr/share/sounds/gnome/default/alerts/glass.ogg"
        ]:
            if subprocess.run(["which", "paplay"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                try:
                    subprocess.run(["paplay", sound_file], stderr=subprocess.DEVNULL)
                    return
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
            self._send_elgato_control(cmd=0x02, val=1 if self.phantom_power_48v else 0)
        return self.phantom_power_48v

    def set_low_cut(self, mode: str):
        self.low_cut_filter = mode
        if self.device_type == "elgato":
            val = 0 if mode == "Off" else (1 if mode == "80Hz" else 2)
            self._send_elgato_control(cmd=0x03, val=val)

    def toggle_clipguard(self) -> bool:
        self.clipguard_enabled = not self.clipguard_enabled
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
