"""
Microphone & Physical Source Ingestion Manager
===============================================
Dedicated manager for physical microphone inputs (Elgato Wave XLR, USB microphones, ALSA capture).
Completely isolated from desktop application playback streams and loopback matrices.
"""

import subprocess
from wavecontroller.utils.logger import get_logger

log = get_logger("MicrophoneSourceManager")

class MicrophoneSourceManager:
    """
    Manages physical microphone capture ports, hardware input discovery,
    and voice stream feeding into Chat Mix (Virtual Input) and Personal Mix (Sidetone).
    """
    def __init__(self, pipewire_mgr=None, hardware_mgr=None):
        self.pipewire_mgr = pipewire_mgr
        self.hardware_mgr = hardware_mgr

    def get_system_source_status(self) -> tuple:
        """Queries system default audio source volume and mute status via wpctl."""
        try:
            out = subprocess.check_output(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"], text=True, stderr=subprocess.DEVNULL, timeout=3).strip()
            parts = out.split()
            if len(parts) >= 2:
                vol = int(round(float(parts[1]) * 100))
                muted = "[MUTED]" in out
                return vol, muted
        except Exception:
            pass
        return None, None
