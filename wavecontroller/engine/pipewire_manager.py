import os
import json
import subprocess
import threading
import time
from gi.repository import GLib

from .config_manager import config_manager

class PipeWireManager:
    """
    High-performance PipeWire manager with stream node caching, debounced
    asynchronous volume dispatch, bidirectional sync with Volume Controller Plus,
    and automatic configuration persistence to ~/.config/WaveController/config.json.
    """
    
    DEFAULT_CHANNELS = [
        {"id": "mic", "name": "Microphone", "type": "source", "icon": "audio-input-microphone-symbolic", "default_vol": 80, "sync_meter": False}
    ]

    DEFAULT_MIXES = [
        {"id": "personal", "name": "Personal Mix", "subtitle": "1 output", "icon": "audio-headphones-symbolic", "color": "#3db356"}
    ]

    DEFAULT_APP_MAPPINGS = {
        "mic": ["System capture"]
    }

    def __init__(self):
        saved_channels = config_manager.get("channels")
        saved_mixes = config_manager.get("mixes")
        saved_apps = config_manager.get("assigned_apps")
        saved_states = config_manager.get("channel_states")

        self.channels = list(saved_channels) if saved_channels else list(self.DEFAULT_CHANNELS)
        self.mixes = list(saved_mixes) if saved_mixes else list(self.DEFAULT_MIXES)
        self.assigned_apps = dict(saved_apps) if saved_apps else dict(self.DEFAULT_APP_MAPPINGS)
        self.channel_states = dict(saved_states) if saved_states else {}
        self.output_devices = []
        self.selected_monitor_device = None
        self.running = False
        self._lock = threading.RLock()
        
        # High-Performance Node Cache & Volume Dispatch Queue
        self._node_cache = {} # {app_name_lower: [node_id, ...]}
        self._last_cache_time = 0.0
        self._volume_queue = {} # {channel_id: (volume_pct, is_muted)}
        self._volume_event = threading.Event()
        self._worker_thread = None
        self._sync_thread = None
        self.on_external_change_callback = None

        self._init_default_states()

    def _save_state_to_config(self, immediate: bool = False):
        """Persists current channels, mixes, assigned apps, and channel states."""
        with self._lock:
            data = {
                "channels": self.channels,
                "mixes": self.mixes,
                "assigned_apps": self.assigned_apps,
                "channel_states": self.channel_states
            }
            config_manager.update(data, immediate=immediate)
        
    def _init_default_states(self):
        # Query real initial mic volume
        init_mic_vol, init_mic_muted = self._query_system_source_status()
        mic_vol = init_mic_vol if init_mic_vol is not None else 80
        mic_muted = init_mic_muted if init_mic_muted is not None else False

        for ch in self.channels:
            ch_id = ch["id"]
            if ch_id not in self.channel_states:
                self.channel_states[ch_id] = {}
            if ch_id not in self.assigned_apps:
                self.assigned_apps[ch_id] = []
            for mx in self.mixes:
                mx_id = mx["id"]
                if mx_id not in self.channel_states[ch_id]:
                    self.channel_states[ch_id][mx_id] = {
                        "volume": mic_vol if ch_id == "mic" else ch.get("default_vol", 80),
                        "muted": mic_muted if ch_id == "mic" else False,
                        "linked": True
                    }
        self._save_state_to_config(immediate=False)

    def _query_system_source_status(self):
        try:
            out = subprocess.check_output(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"], text=True, stderr=subprocess.DEVNULL).strip()
            parts = out.split()
            if len(parts) >= 2:
                vol = int(round(float(parts[1]) * 100))
                muted = "[MUTED]" in out
                return vol, muted
        except Exception:
            pass
        return None, None

    def start(self):
        self.running = True
        self.refresh_devices()
        self._refresh_node_cache()
        
        # 1. Volume dispatch worker
        self._worker_thread = threading.Thread(target=self._volume_worker_loop, daemon=True)
        self._worker_thread.start()

        # 2. External volume sync poller (Syncs Volume Controller Plus on Stream Deck +)
        self._sync_thread = threading.Thread(target=self._external_sync_loop, daemon=True)
        self._sync_thread.start()

    def stop(self):
        self.running = False
        self._volume_event.set()

    def get_application_volume_status(self, app_name: str) -> tuple:
        """Queries volume and mute status of an application from its PipeWire node."""
        if not app_name:
            return None, None
        app_low = app_name.lower().strip()
        nodes = []
        with self._lock:
            for k, ids in self._node_cache.items():
                if app_low in k or k in app_low:
                    nodes.extend(ids)
        if not nodes:
            self._refresh_node_cache()
            with self._lock:
                for k, ids in self._node_cache.items():
                    if app_low in k or k in app_low:
                        nodes.extend(ids)
                        
        for node_id in nodes:
            try:
                out = subprocess.check_output(["wpctl", "get-volume", str(node_id)], text=True, stderr=subprocess.DEVNULL).strip()
                import re
                m = re.search(r'Volume:\s*([\d\.]+)', out)
                if m:
                    vol = int(round(float(m.group(1)) * 100))
                    muted = "[MUTED]" in out
                    return vol, muted
            except Exception:
                pass
        return None, None

    def _external_sync_loop(self):
        """Monitors system and application volume changes in real-time (1:1 sync with Volume Controller Plus)."""
        while self.running:
            try:
                changed = False
                
                # 1. Sync Microphone (Source) Channel
                curr_mic_vol, curr_mic_muted = self._query_system_source_status()
                if curr_mic_vol is not None:
                    with self._lock:
                        if "mic" in self.channel_states and "personal" in self.channel_states["mic"]:
                            st = self.channel_states["mic"]["personal"]
                            if abs(st["volume"] - curr_mic_vol) >= 1 or st["muted"] != curr_mic_muted:
                                diff = curr_mic_vol - st["volume"]
                                st["volume"] = curr_mic_vol
                                st["muted"] = curr_mic_muted
                                if st.get("linked", True):
                                    for m_id, o_st in self.channel_states["mic"].items():
                                        if m_id != "personal":
                                            o_st["volume"] = max(0, min(100, o_st["volume"] + diff))
                                            o_st["muted"] = curr_mic_muted
                                changed = True

                # 2. Sync Application Channels (e.g. Spotify, Games, Discord)
                channels_to_check = []
                with self._lock:
                    for ch in self.channels:
                        if ch["id"] != "mic":
                            assigned = self.assigned_apps.get(ch["id"], [ch["name"]])
                            channels_to_check.append((ch["id"], assigned))

                for ch_id, assigned_list in channels_to_check:
                    for app_name in assigned_list:
                        app_vol, app_muted = self.get_application_volume_status(app_name)
                        if app_vol is not None:
                            with self._lock:
                                if ch_id in self.channel_states and "personal" in self.channel_states[ch_id]:
                                    st = self.channel_states[ch_id]["personal"]
                                    if abs(st["volume"] - app_vol) >= 1 or st["muted"] != app_muted:
                                        diff = app_vol - st["volume"]
                                        st["volume"] = app_vol
                                        st["muted"] = app_muted
                                        if st.get("linked", True):
                                            for m_id, o_st in self.channel_states[ch_id].items():
                                                if m_id != "personal":
                                                    o_st["volume"] = max(0, min(100, o_st["volume"] + diff))
                                                    o_st["muted"] = app_muted
                                        changed = True
                            break

                if changed and self.on_external_change_callback:
                    GLib.idle_add(self.on_external_change_callback)
            except Exception:
                pass
            time.sleep(0.04) # 25 Hz fast poller (40ms)

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

    def _refresh_node_cache(self):
        """Scans PipeWire graph and caches active playback stream Node IDs."""
        cache = {}
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for obj in data:
                if obj.get("type") != "PipeWire:Interface:Node":
                    continue
                props = obj.get("info", {}).get("props", {})
                media_class = props.get("media.class", "")
                if "Stream/Output/Audio" in media_class:
                    node_id = str(obj["id"])
                    names = [
                        props.get("application.name", "").lower(),
                        props.get("application.process.binary", "").lower(),
                        props.get("node.name", "").lower(),
                        props.get("node.description", "").lower()
                    ]
                    for n in names:
                        if n:
                            if n not in cache:
                                cache[n] = []
                            if node_id not in cache[n]:
                                cache[n].append(node_id)
        except Exception:
            pass

        with self._lock:
            self._node_cache = cache
            self._last_cache_time = time.time()

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
                    
                    if not name:
                        continue
                    name_low = name.lower()
                    if any(x in name_low for x in ["wave_sink", "wave_mic", "vcp_monitor", "pw-record", "parecord", "pipewire", "wireplumber", "easyeffects"]):
                        continue
                        
                    if name not in seen:
                        seen.add(name)
                        apps.append({
                            "id": node_id,
                            "name": name,
                            "binary": binary or name.lower(),
                            "icon": icon or self.resolve_icon_for_app(name)
                        })
        except Exception:
            pass
        return apps

    def assign_app_to_channel(self, channel_id: str, app_name: str):
        with self._lock:
            for ch, apps in self.assigned_apps.items():
                if app_name in apps:
                    apps.remove(app_name)
            if channel_id in self.assigned_apps:
                self.assigned_apps[channel_id].append(app_name)
                
            state = self.channel_states.get(channel_id, {}).get("personal", {"volume": 80, "muted": False})
            self.set_channel_volume(channel_id, "personal", state["volume"])
            self._save_state_to_config(immediate=True)

    def get_assigned_apps(self, channel_id: str) -> list:
        with self._lock:
            return list(self.assigned_apps.get(channel_id, []))

    def set_channel_volume(self, channel_id: str, mix_id: str, volume: int):
        with self._lock:
            if channel_id in self.channel_states and mix_id in self.channel_states[channel_id]:
                state = self.channel_states[channel_id][mix_id]
                diff = volume - state["volume"]
                state["volume"] = max(0, min(100, volume))
                
                if state.get("linked", True):
                    for other_mix_id, other_state in self.channel_states[channel_id].items():
                        if other_mix_id != mix_id:
                            other_state["volume"] = max(0, min(100, other_state["volume"] + diff))

                # Enqueue debounced volume update
                self._volume_queue[channel_id] = (state["volume"], state.get("muted", False))
                self._volume_event.set()
                self._save_state_to_config(immediate=False)

    def set_channel_mute(self, channel_id: str, mix_id: str, muted: bool):
        with self._lock:
            if channel_id in self.channel_states and mix_id in self.channel_states[channel_id]:
                self.channel_states[channel_id][mix_id]["muted"] = muted
                state = self.channel_states[channel_id][mix_id]
                self._volume_queue[channel_id] = (state["volume"], muted)
                self._volume_event.set()
                self._save_state_to_config(immediate=False)

    def toggle_channel_mute(self, channel_id: str, mix_id: str) -> bool:
        with self._lock:
            if channel_id in self.channel_states and mix_id in self.channel_states[channel_id]:
                curr = self.channel_states[channel_id][mix_id]["muted"]
                new_mute = not curr
                self.channel_states[channel_id][mix_id]["muted"] = new_mute
                state = self.channel_states[channel_id][mix_id]
                self._volume_queue[channel_id] = (state["volume"], new_mute)
                self._volume_event.set()
                self._save_state_to_config(immediate=False)
                return new_mute
        return False

    def _volume_worker_loop(self):
        """Persistent worker thread dispatching coalesced volume updates with zero drag latency."""
        while self.running:
            self._volume_event.wait(timeout=0.5)
            self._volume_event.clear()

            if time.time() - self._last_cache_time > 5.0:
                self._refresh_node_cache()

            with self._lock:
                pending = dict(self._volume_queue)
                self._volume_queue.clear()

            for channel_id, (volume_pct, is_muted) in pending.items():
                vol_frac = max(0.0, min(1.5, volume_pct / 100.0))

                if channel_id == "mic":
                    try:
                        subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", f"{vol_frac:.2f}"], stderr=subprocess.DEVNULL)
                        subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1" if is_muted else "0"], stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                    continue

                assigned_app_names = self.get_assigned_apps(channel_id)
                target_node_ids = set()

                with self._lock:
                    for app in assigned_app_names:
                        app_low = app.lower()
                        for cached_name, node_ids in self._node_cache.items():
                            if app_low in cached_name or cached_name in app_low:
                                target_node_ids.update(node_ids)

                if not target_node_ids and assigned_app_names:
                    self._refresh_node_cache()
                    with self._lock:
                        for app in assigned_app_names:
                            app_low = app.lower()
                            for cached_name, node_ids in self._node_cache.items():
                                if app_low in cached_name or cached_name in app_low:
                                    target_node_ids.update(node_ids)

                for node_id in target_node_ids:
                    try:
                        subprocess.run(["wpctl", "set-volume", str(node_id), f"{vol_frac:.2f}"], stderr=subprocess.DEVNULL)
                        subprocess.run(["wpctl", "set-mute", str(node_id), "1" if is_muted else "0"], stderr=subprocess.DEVNULL)
                    except Exception:
                        pass

    def toggle_channel_link(self, channel_id: str, mix_id: str) -> bool:
        with self._lock:
            if channel_id in self.channel_states and mix_id in self.channel_states[channel_id]:
                curr = self.channel_states[channel_id][mix_id].get("linked", True)
                new_val = not curr
                for m_id in self.channel_states[channel_id]:
                    self.channel_states[channel_id][m_id]["linked"] = new_val
                self._save_state_to_config(immediate=True)
                return new_val
        return True

    def get_channel_state(self, channel_id: str, mix_id: str) -> dict:
        with self._lock:
            return dict(self.channel_states.get(channel_id, {}).get(mix_id, {"volume": 80, "muted": False, "linked": True}))

    def add_channel(self, name: str, icon: str = None, ch_type: str = "sink", assigned_apps: list = None, sync_meter: bool = False) -> dict:
        with self._lock:
            ch_id = name.lower().replace(" ", "_").replace("/", "_").replace(".", "_")
            existing_ids = [c["id"] for c in self.channels]
            if ch_id in existing_ids:
                ch_id = f"{ch_id}_{len(self.channels)}"

            resolved_icon = icon or self.resolve_icon_for_app(name)
            new_ch = {
                "id": ch_id,
                "name": name,
                "type": ch_type,
                "icon": resolved_icon,
                "default_vol": 80,
                "sync_meter": sync_meter
            }
            self.channels.append(new_ch)
            self.channel_states[ch_id] = {}
            self.assigned_apps[ch_id] = assigned_apps if assigned_apps is not None else ([name] if ch_type == "sink" else [])
            for mx in self.mixes:
                self.channel_states[ch_id][mx["id"]] = {"volume": 80, "muted": False, "linked": True}

            self._refresh_node_cache()
            self._save_state_to_config(immediate=True)
            return new_ch

    def remove_channel(self, channel_id: str) -> bool:
        with self._lock:
            if channel_id == "mic":
                return False
            self.channels = [c for c in self.channels if c["id"] != channel_id]
            if channel_id in self.channel_states:
                del self.channel_states[channel_id]
            if channel_id in self.assigned_apps:
                del self.assigned_apps[channel_id]
            self._refresh_node_cache()
            self._save_state_to_config(immediate=True)
            return True

    def rename_channel(self, channel_id: str, new_name: str) -> bool:
        with self._lock:
            for ch in self.channels:
                if ch["id"] == channel_id:
                    ch["name"] = new_name
                    self._save_state_to_config(immediate=True)
                    return True
            return False

    def set_channel_sync_meter(self, channel_id: str, sync: bool):
        with self._lock:
            for ch in self.channels:
                if ch["id"] == channel_id:
                    ch["sync_meter"] = sync
                    self._save_state_to_config(immediate=True)
                    break

    def get_channel_sync_meter(self, channel_id: str) -> bool:
        with self._lock:
            for ch in self.channels:
                if ch["id"] == channel_id:
                    return ch.get("sync_meter", False)
            return False

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
            self._save_state_to_config(immediate=True)
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
