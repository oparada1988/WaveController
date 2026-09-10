"""
Microphone & Physical Source Ingestion Manager
===============================================
Dedicated manager for physical microphone inputs (Elgato Wave XLR, USB microphones, ALSA capture).
Completely isolated from desktop application playback streams and loopback matrices.
"""

import re
import subprocess
from wavecontroller.engine.graph.process_classifier import get_match_tokens, port_matches_tokens
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

    def set_system_source_volume(self, volume: int):
        """Dispatches hardware/system input volume via wpctl."""
        try:
            frac = max(0.0, min(1.0, volume / 100.0))
            subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", f"{frac:.2f}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def set_system_source_mute(self, muted: bool):
        """Dispatches hardware/system input mute via wpctl."""
        try:
            val = "1" if muted else "0"
            subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", val], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def discover_microphone_capture_ports(self, channel_id: str, channel_name: str, assigned_devs: list, out_ports: list, port_meta: dict = None) -> list:
        """
        Finds all active ALSA hardware capture ports matching the microphone channel.
        """
        tokens = get_match_tokens(str(channel_id))
        if channel_name:
            tokens.update(get_match_tokens(str(channel_name)))
        for dev in assigned_devs:
            tokens.update(get_match_tokens(str(dev)))

        matched = []
        for p in out_ports:
            clean_p = re.sub(r"^\d+\s+", "", p).strip()
            if clean_p.startswith("output.WaveController_") or clean_p.startswith("WaveController_") or ":monitor_" in clean_p:
                continue
            if ":capture_" in clean_p:
                if port_matches_tokens(clean_p, tokens, port_meta):
                    matched.append(p)
        return matched
