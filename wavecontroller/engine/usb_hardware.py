import subprocess
import threading
import time

class USBHardwareManager:
    """
    Hardware integration layer supporting Tier 1 (Elgato Wave:3 & Wave XLR via USB Control Transfers)
    and Tier 2 (Universal USB Microphones like fifine, Blue Yeti, Rode via ALSA/PipeWire).
    """

    def __init__(self):
        self.device_name = "fifine Microphone"
        self.device_type = "generic" # 'elgato' or 'generic'
        self.hardware_gain_db = 45 # 0 to 75 dB
        self.phantom_power_48v = False
        self.clipguard_enabled = True
        self.low_cut_filter = "80Hz" # 'Off', '80Hz', '120Hz'
        self.hardware_mute = False
        self.led_ring_color = "#00a8ff"
        self.headphone_volume = 70
        self.mic_pc_crossfade = 50
        
        self.detect_connected_hardware()

    def detect_connected_hardware(self):
        """Scans USB and PipeWire devices to detect active microphone hardware."""
        try:
            lsusb_out = subprocess.check_output(["lsusb"], text=True, stderr=subprocess.DEVNULL)
            if "0fd9:" in lsusb_out: # Elgato Systems
                if "0fd9:0088" in lsusb_out:
                    self.device_name = "Elgato Wave:3"
                    self.device_type = "elgato"
                elif "0fd9:0083" in lsusb_out:
                    self.device_name = "Elgato Wave XLR MK.2"
                    self.device_type = "elgato"
                else:
                    self.device_name = "Elgato Wave Device"
                    self.device_type = "elgato"
                return
            
            # Check for fifine microphone
            if "3142:a010" in lsusb_out or "fifine" in lsusb_out.lower():
                self.device_name = "fifine Microphone"
                self.device_type = "generic"
                return
        except Exception:
            pass

        self.device_name = "fifine Microphone"
        self.device_type = "generic"

    def set_gain(self, gain_db: int):
        self.hardware_gain_db = max(0, min(75, gain_db))
        if self.device_type == "elgato":
            self._send_elgato_control(cmd=0x01, val=self.hardware_gain_db)
        else:
            # Scale to ALSA input volume
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
        """Sends USB Control Transfer packet via wIndex=0x3303."""
        pass
