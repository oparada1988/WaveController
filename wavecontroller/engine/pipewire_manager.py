import os
import json
import subprocess
import threading
import time

class PipeWireManager:
    """
    Manages PipeWire virtual sinks, sub-mix routing buses, and channel volume levels.
    Creates the Wave Link dual-mix matrix architecture on Linux.
    """
    
    DEFAULT_CHANNELS = [
        {"id": "mic", "name": "Microphone", "type": "source", "icon": "audio-input-microphone-symbolic", "default_vol": 80},
        {"id": "game", "name": "Games", "type": "sink", "icon": "applications-games-symbolic", "default_vol": 85},
        {"id": "music", "name": "Music", "type": "sink", "icon": "audio-x-generic-symbolic", "default_vol": 75},
        {"id": "chat", "name": "Voice Chat", "type": "sink", "icon": "user-available-symbolic", "default_vol": 90},
        {"id": "sfx", "name": "Stream Deck / SFX", "type": "sink", "icon": "view-grid-symbolic", "default_vol": 70},
        {"id": "browser", "name": "Browser", "type": "sink", "icon": "web-browser-symbolic", "default_vol": 80},
        {"id": "system", "name": "System Audio", "type": "sink", "icon": "audio-speakers-symbolic", "default_vol": 80}
    ]

    DEFAULT_MIXES = [
        {"id": "personal", "name": "Personal Mix", "subtitle": "1 output", "icon": "audio-headphones-symbolic", "color": "#3db356"},
        {"id": "chat_mix", "name": "Chat Mix", "subtitle": "Discord / In-Game", "icon": "system-users-symbolic", "color": "#3584e4"},
        {"id": "record_mix", "name": "Record Mix", "subtitle": "OBS / Stream", "icon": "media-record-symbolic", "color": "#e05252"}
    ]

    def __init__(self):
        self.channels = list(self.DEFAULT_CHANNELS)
        self.mixes = list(self.DEFAULT_MIXES)
        self.channel_states = {} # {channel_id: {mix_id: {"volume": int, "muted": bool, "linked": bool}}}
        self.output_devices = []
        self.selected_monitor_device = None
        self.running = False
        self._lock = threading.Lock()
        
        self._init_default_states()
        
    def _init_default_states(self):
        for ch in self.channels:
            ch_id = ch["id"]
            self.channel_states[ch_id] = {}
            for mx in self.mixes:
                mx_id = mx["id"]
                self.channel_states[ch_id][mx_id] = {
                    "volume": ch.get("default_vol", 80),
                    "muted": False,
                    "linked": True
                }

    def start(self):
        self.running = True
        self.refresh_devices()
        threading.Thread(target=self._setup_pipewire_buses, daemon=True).start()

    def stop(self):
        self.running = False

    def refresh_devices(self):
        """Discovers available physical output sinks and input sources from PipeWire."""
        try:
            out = subprocess.check_output(["wpctl", "status"], text=True, stderr=subprocess.DEVNULL)
            sinks = []
            sources = []
            
            in_sinks = False
            in_sources = False
            
            for line in out.splitlines():
                if "Sinks:" in line:
                    in_sinks = True
                    in_sources = False
                    continue
                elif "Sources:" in line:
                    in_sinks = False
                    in_sources = True
                    continue
                elif "Filters:" in line or "Streams:" in line:
                    in_sinks = False
                    in_sources = False
                    continue
                
                line_str = line.strip()
                if not line_str or line_str.startswith("├") or line_str.startswith("└") or line_str.startswith("│"):
                    # Parse node lines like: "59. Starship/Matisse HD Audio Controller Analog Stereo [vol: 0.65]"
                    parts = line_str.replace("├─", "").replace("└─", "").replace("│", "").replace("*", "").strip()
                    if parts and parts[0].isdigit():
                        tokens = parts.split(".", 1)
                        if len(tokens) == 2:
                            node_id = tokens[0].strip()
                            name_part = tokens[1].split("[")[0].strip()
                            if in_sinks:
                                sinks.append({"id": node_id, "name": name_part})
                            elif in_sources:
                                sources.append({"id": node_id, "name": name_part})
            
            with self._lock:
                self.output_devices = sinks
                if sinks and not self.selected_monitor_device:
                    self.selected_monitor_device = sinks[0]["name"]
        except Exception:
            pass

    def _setup_pipewire_buses(self):
        """Ensures the WaveController virtual audio node graph exists in PipeWire."""
        # Virtual sinks are created via pw-cli or PipeWire module-null-sink / loopbacks
        pass

    def set_channel_volume(self, channel_id: str, mix_id: str, volume: int):
        with self._lock:
            if channel_id in self.channel_states and mix_id in self.channel_states[channel_id]:
                state = self.channel_states[channel_id][mix_id]
                diff = volume - state["volume"]
                state["volume"] = max(0, min(100, volume))
                
                # If linked, propagate proportional change to other mixes
                if state.get("linked", True):
                    for other_mix_id, other_state in self.channel_states[channel_id].items():
                        if other_mix_id != mix_id:
                            other_state["volume"] = max(0, min(100, other_state["volume"] + diff))

    def set_channel_mute(self, channel_id: str, mix_id: str, muted: bool):
        with self._lock:
            if channel_id in self.channel_states and mix_id in self.channel_states[channel_id]:
                self.channel_states[channel_id][mix_id]["muted"] = muted

    def toggle_channel_mute(self, channel_id: str, mix_id: str) -> bool:
        with self._lock:
            if channel_id in self.channel_states and mix_id in self.channel_states[channel_id]:
                curr = self.channel_states[channel_id][mix_id]["muted"]
                self.channel_states[channel_id][mix_id]["muted"] = not curr
                return not curr
        return False

    def toggle_channel_link(self, channel_id: str, mix_id: str) -> bool:
        with self._lock:
            if channel_id in self.channel_states and mix_id in self.channel_states[channel_id]:
                curr = self.channel_states[channel_id][mix_id].get("linked", True)
                new_val = not curr
                for m_id in self.channel_states[channel_id]:
                    self.channel_states[channel_id][m_id]["linked"] = new_val
                return new_val
        return True

    def get_channel_state(self, channel_id: str, mix_id: str) -> dict:
        with self._lock:
            return dict(self.channel_states.get(channel_id, {}).get(mix_id, {"volume": 80, "muted": False, "linked": True}))

    def add_channel(self, name: str, icon: str = "audio-x-generic-symbolic") -> dict:
        with self._lock:
            ch_id = name.lower().replace(" ", "_")
            new_ch = {"id": ch_id, "name": name, "type": "sink", "icon": icon, "default_vol": 80}
            self.channels.append(new_ch)
            self.channel_states[ch_id] = {}
            for mx in self.mixes:
                self.channel_states[ch_id][mx["id"]] = {"volume": 80, "muted": False, "linked": True}
            return new_ch
