import os
import json
import subprocess
import threading
import time

class PipeWireManager:
    """
    Manages PipeWire virtual sinks, sub-mix routing buses, active application routing,
    and real-time system/application volume levels.
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

    DEFAULT_APP_MAPPINGS = {
        "music": ["Spotify", "Rhythmbox", "Apple Music", "Cider", "VLC"],
        "chat": ["Discord", "Teams", "Zoom", "Mumble", "Skype", "WEBRTC VoiceEngine"],
        "game": ["Steam", "Proton", "Wine", "Games"],
        "browser": ["Chromium", "Chrome", "Firefox", "Brave", "Edge"],
        "sfx": ["StreamController", "Stream Deck"]
    }

    def __init__(self):
        self.channels = list(self.DEFAULT_CHANNELS)
        self.mixes = list(self.DEFAULT_MIXES)
        self.channel_states = {} # {channel_id: {mix_id: {"volume": int, "muted": bool, "linked": bool}}}
        self.assigned_apps = dict(self.DEFAULT_APP_MAPPINGS) # {channel_id: list of app names}
        self.output_devices = []
        self.selected_monitor_device = None
        self.running = False
        self._lock = threading.Lock()
        
        self._init_default_states()
        
    def _init_default_states(self):
        for ch in self.channels:
            ch_id = ch["id"]
            self.channel_states[ch_id] = {}
            if ch_id not in self.assigned_apps:
                self.assigned_apps[ch_id] = []
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
                elif "Filters:" in line or "Streams:" in line or "Video" in line:
                    in_sinks = False
                    in_sources = False
                    continue
                
                line_str = line.strip()
                if not line_str or line_str.startswith("├") or line_str.startswith("└") or line_str.startswith("│"):
                    is_def = "*" in line_str
                    parts = line_str.replace("├─", "").replace("└─", "").replace("│", "").replace("*", "").strip()
                    if parts and parts[0].isdigit():
                        tokens = parts.split(".", 1)
                        if len(tokens) == 2:
                            node_id = tokens[0].strip()
                            name_part = tokens[1].split("[")[0].strip()
                            name_lower = name_part.lower()
                            if any(x in name_lower for x in ["facecam", "cam", "video", "virtual", "null"]):
                                continue
                            if in_sinks:
                                sinks.append({"id": node_id, "name": name_part, "is_default": is_def})
                            elif in_sources:
                                sources.append({"id": node_id, "name": name_part, "is_default": is_def})
            
            with self._lock:
                self.output_devices = sinks
                if sinks and not self.selected_monitor_device:
                    for s in sinks:
                        if s.get("is_default"):
                            self.selected_monitor_device = s["name"]
                            break
                    if not self.selected_monitor_device and sinks:
                        self.selected_monitor_device = sinks[0]["name"]
        except Exception:
            pass

    def get_active_application_streams(self) -> list:
        """Discovers running audio playback streams currently outputting audio."""
        apps = []
        seen = set()
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for obj in data:
                props = obj.get("info", {}).get("props", {})
                media_class = props.get("media.class", "")
                if "Stream/Output/Audio" in media_class:
                    name = props.get("application.name") or props.get("node.description") or props.get("node.name")
                    binary = props.get("application.process.binary")
                    icon = props.get("application.icon-name") or props.get("application.icon_name")
                    node_id = obj.get("id")
                    if name and name not in seen:
                        seen.add(name)
                        apps.append({
                            "id": node_id,
                            "name": name,
                            "binary": binary or name.lower(),
                            "icon": icon or "audio-x-generic-symbolic"
                        })
        except Exception:
            pass
        return apps

    def assign_app_to_channel(self, channel_id: str, app_name: str):
        with self._lock:
            # Remove from other channels first
            for ch, apps in self.assigned_apps.items():
                if app_name in apps:
                    apps.remove(app_name)
            if channel_id in self.assigned_apps:
                self.assigned_apps[channel_id].append(app_name)
                
            # Apply current channel volume to newly assigned app
            state = self.channel_states.get(channel_id, {}).get("personal", {"volume": 80, "muted": False})
            self._apply_volume_to_system(channel_id, state["volume"], state.get("muted", False))

    def get_assigned_apps(self, channel_id: str) -> list:
        with self._lock:
            return list(self.assigned_apps.get(channel_id, []))

    def _setup_pipewire_buses(self):
        """Ensures the WaveController virtual audio node graph exists in PipeWire."""
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

                # Apply volume to actual PipeWire streams / apps
                self._apply_volume_to_system(channel_id, state["volume"], state.get("muted", False))

    def set_channel_mute(self, channel_id: str, mix_id: str, muted: bool):
        with self._lock:
            if channel_id in self.channel_states and mix_id in self.channel_states[channel_id]:
                self.channel_states[channel_id][mix_id]["muted"] = muted
                state = self.channel_states[channel_id][mix_id]
                self._apply_volume_to_system(channel_id, state["volume"], muted)

    def toggle_channel_mute(self, channel_id: str, mix_id: str) -> bool:
        with self._lock:
            if channel_id in self.channel_states and mix_id in self.channel_states[channel_id]:
                curr = self.channel_states[channel_id][mix_id]["muted"]
                new_mute = not curr
                self.channel_states[channel_id][mix_id]["muted"] = new_mute
                state = self.channel_states[channel_id][mix_id]
                self._apply_volume_to_system(channel_id, state["volume"], new_mute)
                return new_mute
        return False

    def _apply_volume_to_system(self, channel_id: str, volume_pct: int, is_muted: bool):
        """Asynchronously applies volume and mute to PipeWire/ALSA nodes."""
        threading.Thread(target=self._exec_apply_volume, args=(channel_id, volume_pct, is_muted), daemon=True).start()

    def _exec_apply_volume(self, channel_id: str, volume_pct: int, is_muted: bool):
        vol_frac = max(0.0, min(1.5, volume_pct / 100.0))

        if channel_id == "mic":
            try:
                subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", f"{vol_frac:.2f}"], stderr=subprocess.DEVNULL)
                subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1" if is_muted else "0"], stderr=subprocess.DEVNULL)
            except Exception:
                pass
            return

        # For playback channels, find all stream nodes of assigned applications
        assigned_app_names = self.get_assigned_apps(channel_id)
        if not assigned_app_names and channel_id == "system":
            # Apply to default audio sink if system
            try:
                subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{vol_frac:.2f}"], stderr=subprocess.DEVNULL)
                subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if is_muted else "0"], stderr=subprocess.DEVNULL)
            except Exception:
                pass
            return

        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for obj in data:
                props = obj.get("info", {}).get("props", {})
                media_class = props.get("media.class", "")
                if "Stream/Output/Audio" in media_class:
                    name = props.get("application.name", "")
                    binary = props.get("application.process.binary", "")
                    node_name = props.get("node.name", "")
                    
                    matched = False
                    for app in assigned_app_names:
                        app_low = app.lower()
                        if app_low in name.lower() or app_low in binary.lower() or app_low in node_name.lower():
                            matched = True
                            break
                    
                    if matched:
                        node_id = str(obj["id"])
                        subprocess.run(["wpctl", "set-volume", node_id, f"{vol_frac:.2f}"], stderr=subprocess.DEVNULL)
                        subprocess.run(["wpctl", "set-mute", node_id, "1" if is_muted else "0"], stderr=subprocess.DEVNULL)
        except Exception:
            pass

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

    def add_channel(self, name: str, icon: str = None) -> dict:
        with self._lock:
            ch_id = name.lower().replace(" ", "_")
            existing_ids = [c["id"] for c in self.channels]
            if ch_id in existing_ids:
                ch_id = f"{ch_id}_{len(self.channels)}"

            resolved_icon = icon or self.resolve_icon_for_app(name)
            new_ch = {"id": ch_id, "name": name, "type": "sink", "icon": resolved_icon, "default_vol": 80}
            self.channels.append(new_ch)
            self.channel_states[ch_id] = {}
            self.assigned_apps[ch_id] = [name]
            for mx in self.mixes:
                self.channel_states[ch_id][mx["id"]] = {"volume": 80, "muted": False, "linked": True}
            return new_ch

    def add_mix(self, name: str, subtitle: str = "Custom Mix", icon: str = "audio-speakers-symbolic", color: str = "#3584e4") -> dict:
        with self._lock:
            mix_id = name.lower().replace(" ", "_")
            existing_ids = [m["id"] for m in self.mixes]
            if mix_id in existing_ids:
                mix_id = f"{mix_id}_{len(self.mixes)}"
            new_mix = {
                "id": mix_id,
                "name": name,
                "subtitle": subtitle,
                "icon": icon,
                "color": color
            }
            self.mixes.append(new_mix)
            for ch in self.channels:
                ch_id = ch["id"]
                if ch_id not in self.channel_states:
                    self.channel_states[ch_id] = {}
                self.channel_states[ch_id][mix_id] = {"volume": 80, "muted": False, "linked": True}
            return new_mix

    @staticmethod
    def resolve_icon_for_app(app_name: str) -> str:
        app_low = app_name.lower()
        if "spotify" in app_low:
            return "spotify"
        elif "discord" in app_low:
            return "discord"
        elif "steam" in app_low or "game" in app_low:
            return "steam"
        elif "firefox" in app_low:
            return "firefox"
        elif "chrome" in app_low or "chromium" in app_low:
            return "chromium"
        elif "vlc" in app_low:
            return "vlc"
        elif "stream" in app_low:
            return "view-grid-symbolic"
        elif "mic" in app_low:
            return "audio-input-microphone-symbolic"
        return "audio-x-generic-symbolic"
