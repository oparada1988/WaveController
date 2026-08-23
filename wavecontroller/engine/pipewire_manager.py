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
        {"id": "personal", "name": "Personal Mix", "subtitle": "1 output", "icon": "audio-headphones-symbolic", "color": "#3db356", "type": "sink"}
    ]

    DEFAULT_APP_MAPPINGS = {
        "mic": ["System capture"]
    }

    def __init__(self):
        saved_channels = config_manager.get("channels")
        saved_mixes = config_manager.get("mixes")
        saved_apps = config_manager.get("assigned_apps")
        saved_states = config_manager.get("channel_states")
        saved_masters = config_manager.get("channel_master_states")
        saved_mix_states = config_manager.get("mix_states")

        self.channels = list(saved_channels) if saved_channels else list(self.DEFAULT_CHANNELS)
        self.mixes = list(saved_mixes) if saved_mixes else list(self.DEFAULT_MIXES)
        self.assigned_apps = dict(saved_apps) if saved_apps else dict(self.DEFAULT_APP_MAPPINGS)
        self.channel_states = dict(saved_states) if saved_states else {}
        self.channel_master_states = dict(saved_masters) if saved_masters else {}
        self.mix_states = dict(saved_mix_states) if saved_mix_states else {}
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
        self._submix_procs = {} # {(channel_id, mix_id): subprocess.Popen}
        self.on_external_change_callback = None

        self._init_default_states()

    def _save_state_to_config(self, immediate: bool = False):
        """Persists current channels, mixes, assigned apps, and channel states."""
        with self._lock:
            data = {
                "channels": self.channels,
                "mixes": self.mixes,
                "assigned_apps": self.assigned_apps,
                "channel_states": self.channel_states,
                "channel_master_states": self.channel_master_states,
                "mix_states": self.mix_states
            }
            config_manager.update(data, immediate=immediate)
        
    def _init_default_states(self):
        # Query real initial mic volume
        init_mic_vol, init_mic_muted = self._query_system_source_status()
        mic_vol = init_mic_vol if init_mic_vol is not None else 80
        mic_muted = init_mic_muted if init_mic_muted is not None else False

        for mx in self.mixes:
            mx_id = mx["id"]
            if mx_id not in self.mix_states:
                self.mix_states[mx_id] = {"volume": 100, "muted": False}

        for ch in self.channels:
            ch_id = ch["id"]
            if ch_id not in self.channel_master_states:
                self.channel_master_states[ch_id] = {
                    "volume": mic_vol if ch_id == "mic" else ch.get("default_vol", 80),
                    "muted": mic_muted if ch_id == "mic" else False
                }
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
        self._ensure_virtual_mix_nodes()
        self._refresh_node_cache()
        
        # 1. Volume dispatch worker
        self._worker_thread = threading.Thread(target=self._volume_worker_loop, daemon=True)
        self._worker_thread.start()

        # 2. External volume sync poller (Syncs Volume Controller Plus on Stream Deck +)
        self._sync_thread = threading.Thread(target=self._external_sync_loop, daemon=True)
        self._sync_thread.start()

    def _ensure_virtual_mix_nodes(self):
        """
        Synchronizes PipeWire virtual audio nodes strictly with currently configured mixes.
        Prunes any stale/orphan WaveController virtual devices and provisions only active Source/Sink nodes.
        """
        with self._lock:
            mixes_copy = list(self.mixes)

        needed_nodes = {}
        for m in mixes_copy:
            m_id = m["id"]
            m_name = m["name"]
            m_type = m.get("type", "source")

            if m_id == "personal" or m_type == "sink":
                node_name = f"WaveController_{m_id}_Sink"
                needed_nodes[node_name] = (f"WaveController {m_name} (Sink)", "Audio/Sink")
            else:
                node_name = f"WaveController_{m_id}_Source"
                needed_nodes[node_name] = (f"WaveController {m_name}", "Audio/Source/Virtual")

        existing_active_names = set()
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for obj in data:
                props = obj.get("info", {}).get("props", {})
                n_name = props.get("node.name", "")
                n_desc = props.get("node.description", "")
                if n_name.startswith("WaveController_") or n_desc.startswith("WaveController "):
                    if n_name not in needed_nodes:
                        obj_id = obj.get("id")
                        if obj_id:
                            subprocess.run(["pw-cli", "destroy", str(obj_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        existing_active_names.add(n_name)
        except Exception:
            pass

        # Provision any missing needed nodes
        for node_name, (desc, media_class) in needed_nodes.items():
            if node_name not in existing_active_names:
                try:
                    cmd = f'{{ factory.name=support.null-audio-sink node.name="{node_name}" node.description="{desc}" media.class={media_class} object.linger=true }}'
                    subprocess.run(["pw-cli", "create-node", "adapter", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass

        # Real-time synchronization of PipeWire port connections (pw-link)
        self._sync_channel_audio_routing()

    def stop(self):
        self.running = False
        self._volume_event.set()
        with self._lock:
            for p in list(self._submix_procs.values()):
                try:
                    p.terminate()
                except Exception:
                    pass
            self._submix_procs.clear()

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
                
                # 1. Sync Microphone (Source) Channel Master
                curr_mic_vol, curr_mic_muted = self._query_system_source_status()
                if curr_mic_vol is not None:
                    with self._lock:
                        if "mic" not in self.channel_master_states:
                            self.channel_master_states["mic"] = {"volume": 80, "muted": False}
                        st = self.channel_master_states["mic"]
                        if abs(st["volume"] - curr_mic_vol) >= 1 or st["muted"] != curr_mic_muted:
                            st["volume"] = curr_mic_vol
                            st["muted"] = curr_mic_muted
                            changed = True

                # 2. Sync Application Channels (e.g. Spotify, Games, Discord) Master
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
                                if ch_id not in self.channel_master_states:
                                    self.channel_master_states[ch_id] = {"volume": 80, "muted": False}
                                st = self.channel_master_states[ch_id]
                                if abs(st["volume"] - app_vol) >= 1 or st["muted"] != app_muted:
                                    st["volume"] = app_vol
                                    st["muted"] = app_muted
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
                if "Stream/Output/Audio" in media_class or media_class.startswith("Audio/"):
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

    KNOWN_AUDIO_BINARIES = {
        "spotify": ("Spotify", "spotify"),
        "discord": ("Discord", "discord"),
        "steam": ("Steam", "steam"),
        "steamwebhelper": ("Steam", "steam"),
        "firefox": ("Firefox", "firefox"),
        "chrome": ("Google Chrome", "google-chrome"),
        "chromium": ("Chromium", "chromium"),
        "brave": ("Brave", "brave-browser"),
        "vlc": ("VLC Media Player", "vlc"),
        "mpv": ("MPV", "mpv"),
        "rhythmbox": ("Rhythmbox", "rhythmbox"),
        "audacity": ("Audacity", "audacity"),
        "obs": ("OBS Studio", "obs"),
        "obs64": ("OBS Studio", "obs"),
        "cider": ("Cider", "cider"),
        "strawberry": ("Strawberry", "strawberry"),
        "telegram-desktop": ("Telegram", "telegram")
    }

    def get_active_application_streams(self) -> list:
        """Discovers running audio applications from active PipeWire streams and desktop processes."""
        apps = []
        seen = set()

        # 1. Active PipeWire Audio Streams
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for obj in data:
                props = obj.get("info", {}).get("props", {})
                media_class = props.get("media.class", "")
                media_type = props.get("media.type", "")
                
                if "Stream/Output/Audio" in media_class or media_type == "Audio":
                    name = props.get("application.name") or props.get("node.description") or props.get("node.name")
                    binary = props.get("application.process.binary")
                    icon = props.get("application.icon-name") or props.get("application.icon_name")
                    node_id = obj.get("id")
                    
                    if not name:
                        continue
                    name_low = name.lower()
                    if any(x in name_low for x in ["wave_sink", "wave_mic", "vcp_monitor", "pw-record", "parecord", "pipewire", "wireplumber", "easyeffects", "wpctl", "system_capture", "system capture"]):
                        continue
                        
                    if name not in seen and name.lower() not in seen:
                        seen.add(name)
                        seen.add(name.lower())
                        apps.append({
                            "id": node_id,
                            "name": name,
                            "binary": binary or name.lower(),
                            "icon": icon or self.resolve_icon_for_app(name)
                        })
        except Exception:
            pass

        # 2. Running User Desktop Audio Processes (e.g. newly opened apps before playback)
        try:
            for proc_entry in os.listdir("/proc"):
                if proc_entry.isdigit():
                    try:
                        comm_file = os.path.join("/proc", proc_entry, "comm")
                        if os.path.exists(comm_file):
                            with open(comm_file, "r") as f:
                                comm = f.read().strip().lower()
                                if comm in self.KNOWN_AUDIO_BINARIES:
                                    app_title, app_icon = self.KNOWN_AUDIO_BINARIES[comm]
                                    if app_title not in seen and app_title.lower() not in seen:
                                        seen.add(app_title)
                                        seen.add(app_title.lower())
                                        apps.append({
                                            "id": None,
                                            "name": app_title,
                                            "binary": comm,
                                            "icon": app_icon or self.resolve_icon_for_app(app_title)
                                        })
                    except Exception:
                        pass
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
            self._refresh_node_cache()
            self._sync_channel_audio_routing(channel_id=channel_id)

    def get_assigned_apps(self, channel_id: str) -> list:
        with self._lock:
            return list(self.assigned_apps.get(channel_id, []))

    # -------------------------------------------------------------
    # Channel Master Gain / Stream Volume (1:1 with Volume Controller Plus)
    # -------------------------------------------------------------
    def is_channel_linked(self, channel_id: str) -> bool:
        """Returns True if the channel has multi-mix linking enabled."""
        with self._lock:
            states = self.channel_states.get(channel_id, {})
            return any(s.get("linked", True) for s in states.values())

    def get_channel_master_volume(self, channel_id: str) -> int:
        with self._lock:
            st = self.channel_master_states.get(channel_id, {})
            return st.get("volume", 80)

    def get_channel_master_mute(self, channel_id: str) -> bool:
        with self._lock:
            st = self.channel_master_states.get(channel_id, {})
            return st.get("muted", False)

    def set_channel_master_volume(self, channel_id: str, volume: int):
        with self._lock:
            if channel_id not in self.channel_master_states:
                self.channel_master_states[channel_id] = {"volume": 80, "muted": False}
            old_vol = self.channel_master_states[channel_id].get("volume", 80)
            vol = max(0, min(100, volume))
            diff = vol - old_vol
            self.channel_master_states[channel_id]["volume"] = vol
            is_muted = self.channel_master_states[channel_id].get("muted", False)
            
            # If Channel Link is enabled: sync master volume directly to all compatible mix send faders
            if self.is_channel_linked(channel_id):
                for mx in self.mixes:
                    mx_id = mx["id"]
                    if self.is_channel_mix_compatible(channel_id, mx_id):
                        if channel_id in self.channel_states and mx_id in self.channel_states[channel_id]:
                            self.channel_states[channel_id][mx_id]["volume"] = vol

            # Enqueue physical stream volume dispatch
            self._volume_queue[channel_id] = (vol, is_muted)
            self._volume_event.set()
            self._save_state_to_config(immediate=False)

    def set_channel_master_mute(self, channel_id: str, muted: bool):
        with self._lock:
            if channel_id not in self.channel_master_states:
                self.channel_master_states[channel_id] = {"volume": 80, "muted": False}
            self.channel_master_states[channel_id]["muted"] = muted
            vol = self.channel_master_states[channel_id].get("volume", 80)

            # If Channel Link is enabled: sync master mute to all compatible mixes
            if self.is_channel_linked(channel_id):
                for mx in self.mixes:
                    mx_id = mx["id"]
                    if self.is_channel_mix_compatible(channel_id, mx_id):
                        if channel_id in self.channel_states and mx_id in self.channel_states[channel_id]:
                            self.channel_states[channel_id][mx_id]["muted"] = muted

            # Enqueue physical stream volume dispatch
            self._volume_queue[channel_id] = (vol, muted)
            self._volume_event.set()
            self._save_state_to_config(immediate=False)

    def toggle_channel_master_mute(self, channel_id: str) -> bool:
        with self._lock:
            if channel_id not in self.channel_master_states:
                self.channel_master_states[channel_id] = {"volume": 80, "muted": False}
            curr = self.channel_master_states[channel_id].get("muted", False)
            new_mute = not curr
            self.channel_master_states[channel_id]["muted"] = new_mute
            vol = self.channel_master_states[channel_id].get("volume", 80)

            if self.is_channel_linked(channel_id):
                for mx in self.mixes:
                    mx_id = mx["id"]
                    if self.is_channel_mix_compatible(channel_id, mx_id):
                        if channel_id in self.channel_states and mx_id in self.channel_states[channel_id]:
                            self.channel_states[channel_id][mx_id]["muted"] = new_mute

            self._volume_queue[channel_id] = (vol, new_mute)
            self._volume_event.set()
            self._save_state_to_config(immediate=False)
            return new_mute

    # -------------------------------------------------------------
    # Mix Master Bus Control (for Discord, OBS, Headphones, etc.)
    # -------------------------------------------------------------
    def _get_mix_node_ids(self, mix_id: str) -> list:
        ids = []
        target_sink = f"wavecontroller_{mix_id.lower()}_sink"
        target_src = f"wavecontroller_{mix_id.lower()}_source"
        with self._lock:
            mix_obj = next((m for m in self.mixes if m["id"] == mix_id), None)
            target_dev = mix_obj.get("target_device") if mix_obj else None
            m_type = mix_obj.get("type", "source") if mix_obj else "source"

        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for obj in data:
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    n_name = props.get("node.name", "").lower()
                    media_class = props.get("media.class", "")
                    obj_id = str(obj["id"])
                    
                    if target_sink in n_name or target_src in n_name:
                        ids.append(obj_id)
                    elif (m_type == "sink" or "personal" in mix_id) and target_dev and target_dev != "none":
                        clean_target = target_dev.replace("alsa_card.", "").replace("alsa_output.", "").replace("alsa_input.", "").strip().lower()
                        if media_class == "Audio/Sink" and (clean_target in n_name or clean_target in props.get("node.description", "").lower()):
                            ids.append(obj_id)
        except Exception:
            pass

        if not ids:
            self._refresh_node_cache()
            with self._lock:
                for name, node_ids in self._node_cache.items():
                    if target_sink in name or target_src in name:
                        ids.extend(node_ids)
        return list(set(ids))

    def get_mix_master_volume(self, mix_id: str) -> int:
        with self._lock:
            return self.mix_states.get(mix_id, {}).get("volume", 100)

    def get_mix_master_mute(self, mix_id: str) -> bool:
        with self._lock:
            return self.mix_states.get(mix_id, {}).get("muted", False)

    def set_mix_master_volume(self, mix_id: str, volume: int):
        with self._lock:
            if mix_id not in self.mix_states:
                self.mix_states[mix_id] = {"volume": 100, "muted": False}
            vol = max(0, min(100, volume))
            self.mix_states[mix_id]["volume"] = vol
            self._save_state_to_config(immediate=False)
            
        vol_frac = vol / 100.0
        node_ids = self._get_mix_node_ids(mix_id)
        for n_id in node_ids:
            try:
                subprocess.run(["wpctl", "set-volume", str(n_id), f"{vol_frac:.2f}"], stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def set_mix_master_mute(self, mix_id: str, muted: bool):
        with self._lock:
            if mix_id not in self.mix_states:
                self.mix_states[mix_id] = {"volume": 100, "muted": False}
            self.mix_states[mix_id]["muted"] = muted
            self._save_state_to_config(immediate=False)
            
        node_ids = self._get_mix_node_ids(mix_id)
        for n_id in node_ids:
            try:
                subprocess.run(["wpctl", "set-mute", str(n_id), "1" if muted else "0"], stderr=subprocess.DEVNULL)
            except Exception:
                pass

    def toggle_mix_master_mute(self, mix_id: str) -> bool:
        with self._lock:
            if mix_id not in self.mix_states:
                self.mix_states[mix_id] = {"volume": 100, "muted": False}
            curr = self.mix_states[mix_id].get("muted", False)
            new_mute = not curr
            self.mix_states[mix_id]["muted"] = new_mute
            self._save_state_to_config(immediate=False)
            
        node_ids = self._get_mix_node_ids(mix_id)
        for n_id in node_ids:
            try:
                subprocess.run(["wpctl", "set-mute", str(n_id), "1" if new_mute else "0"], stderr=subprocess.DEVNULL)
            except Exception:
                pass
        return new_mute

    def _apply_submix_gain(self, ch_id: str, m_id: str, vol_pct: int, is_muted: bool):
        """Applies independent sub-mix attenuation to dedicated PipeWire loopback stream node."""
        node_name = f"WaveController_submix_{ch_id}_{m_id}"
        vol_frac = max(0.0, min(1.5, vol_pct / 100.0))
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for obj in data:
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    if node_name in props.get("node.name", ""):
                        subprocess.run(["wpctl", "set-volume", str(obj["id"]), f"{vol_frac:.2f}"], stderr=subprocess.DEVNULL)
                        subprocess.run(["wpctl", "set-mute", str(obj["id"]), "1" if is_muted else "0"], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _ensure_submix_loopback(self, ch_id: str, m_id: str, capture_target: str, playback_target: str, vol_pct: int, is_muted: bool):
        """Provisions an isolated, ultra-low latency sub-mix loopback stream with independent hardware DSP gain."""
        key = (ch_id, m_id)
        with self._lock:
            proc = self._submix_procs.get(key)
            if proc is None or proc.poll() is not None:
                node_name = f"WaveController_submix_{ch_id}_{m_id}"
                cmd = ["pw-loopback", "-C", capture_target, "-P", playback_target, "-n", node_name, "--latency=5"]
                try:
                    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._submix_procs[key] = p
                except Exception:
                    pass
        self._apply_submix_gain(ch_id, m_id, vol_pct, is_muted)

    def _stop_submix_loopback(self, ch_id: str, m_id: str):
        """Tears down the sub-mix loopback stream process cleanly."""
        key = (ch_id, m_id)
        with self._lock:
            proc = self._submix_procs.pop(key, None)
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=0.5)
                except Exception:
                    pass

    def set_channel_volume(self, channel_id: str, mix_id: str, volume: int):
        """Sets the sub-mix send level into a specific virtual mix bus."""
        vol = max(0, min(100, volume))
        is_linked = self.is_channel_linked(channel_id)
        with self._lock:
            if channel_id in self.channel_states:
                if is_linked:
                    for m_id in self.channel_states[channel_id]:
                        self.channel_states[channel_id][m_id]["volume"] = vol
                    if channel_id in self.channel_master_states:
                        self.channel_master_states[channel_id]["volume"] = vol
                else:
                    if mix_id in self.channel_states[channel_id]:
                        self.channel_states[channel_id][mix_id]["volume"] = vol
                self._save_state_to_config(immediate=False)

        if is_linked:
            self.set_channel_master_volume(channel_id, vol)
        else:
            is_muted = self.channel_states.get(channel_id, {}).get(mix_id, {}).get("muted", False)
            self._apply_submix_gain(channel_id, mix_id, vol, is_muted)
        self._sync_channel_audio_routing(channel_id, mix_id)

    def set_channel_mute(self, channel_id: str, mix_id: str, muted: bool):
        """Mutes or unmutes a channel within a specific virtual mix bus."""
        is_linked = self.is_channel_linked(channel_id)
        with self._lock:
            if channel_id in self.channel_states:
                if is_linked:
                    for m_id in self.channel_states[channel_id]:
                        self.channel_states[channel_id][m_id]["muted"] = muted
                    if channel_id in self.channel_master_states:
                        self.channel_master_states[channel_id]["muted"] = muted
                else:
                    if mix_id in self.channel_states[channel_id]:
                        self.channel_states[channel_id][mix_id]["muted"] = muted
                self._save_state_to_config(immediate=False)

        if is_linked:
            self.set_channel_master_mute(channel_id, muted)
        else:
            vol = self.channel_states.get(channel_id, {}).get(mix_id, {}).get("volume", 80)
            self._apply_submix_gain(channel_id, mix_id, vol, muted)
        self._sync_channel_audio_routing(channel_id, mix_id)

    def toggle_channel_mute(self, channel_id: str, mix_id: str) -> bool:
        """Toggles mute state within a specific virtual mix bus."""
        curr = self.channel_states.get(channel_id, {}).get(mix_id, {}).get("muted", False)
        new_mute = not curr
        self.set_channel_mute(channel_id, mix_id, new_mute)
        return new_mute

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
                ch_name = ""
                with self._lock:
                    ch_obj = next((c for c in self.channels if c["id"] == channel_id), None)
                    if ch_obj:
                        ch_name = ch_obj.get("name", "")

                search_keys = set([channel_id.lower()])
                if ch_name:
                    search_keys.add(ch_name.lower())
                for a in assigned_app_names:
                    search_keys.add(a.lower())

                # Common aliases for hardware channels
                if "fefine" in search_keys:
                    search_keys.add("fifine")
                if "mobo" in search_keys or "motherboard" in search_keys:
                    search_keys.add("starship")
                    search_keys.add("matisse")
                    search_keys.add("pci-0000_14_00.4")

                target_node_ids = set()

                def collect_matches():
                    matches = set()
                    with self._lock:
                        for sk in search_keys:
                            for cached_name, node_ids in self._node_cache.items():
                                if sk in cached_name or cached_name in sk:
                                    matches.update(node_ids)
                    return matches

                target_node_ids = collect_matches()

                if not target_node_ids:
                    self._refresh_node_cache()
                    target_node_ids = collect_matches()

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
                self._sync_channel_audio_routing(channel_id)
                return new_val
        return True

    def is_channel_mix_compatible(self, channel_id: str, mix_id: str) -> bool:
        """Returns True since any audio channel can be routed to any mix bus."""
        return True

    def is_channel_mix_enabled(self, channel_id: str, mix_id: str) -> bool:
        """Returns True if the channel is actively routed into this mix."""
        if not self.is_channel_mix_compatible(channel_id, mix_id):
            return False
        with self._lock:
            st = self.channel_states.get(channel_id, {}).get(mix_id, {})
            return st.get("enabled", True)

    def set_channel_mix_enabled(self, channel_id: str, mix_id: str, enabled: bool):
        """Enables or disables routing of a channel into a specific mix bus."""
        if enabled and not self.is_channel_mix_compatible(channel_id, mix_id):
            return
        with self._lock:
            if channel_id not in self.channel_states:
                self.channel_states[channel_id] = {}
            if mix_id not in self.channel_states[channel_id]:
                self.channel_states[channel_id][mix_id] = {
                    "volume": 80,
                    "muted": False,
                    "linked": True,
                    "enabled": enabled
                }
            else:
                self.channel_states[channel_id][mix_id]["enabled"] = enabled
            self._save_state_to_config(immediate=True)
        self._sync_channel_audio_routing(channel_id, mix_id)

    def _sync_channel_audio_routing(self, channel_id: str = None, mix_id: str = None):
        """
        Synchronizes real PipeWire port attachments (pw-link) for all channels and mixes.
        When a channel is enabled for a mix, creates real-time patch links visible in qpwgraph.
        When unrouted/disabled, destroys the links in real-time.
        """
        try:
            out_ports_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
            out_ports = [l.strip() for l in out_ports_raw.splitlines() if l.strip()]
        except Exception:
            out_ports = []

        try:
            in_ports_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
            in_ports = [l.strip() for l in in_ports_raw.splitlines() if l.strip()]
        except Exception:
            in_ports = []

        with self._lock:
            channels_copy = list(self.channels)
            mixes_copy = list(self.mixes)

        channels_to_sync = [c for c in channels_copy if channel_id is None or c["id"] == channel_id]
        mixes_to_sync = [m for m in mixes_copy if mix_id is None or m["id"] == mix_id]
        links_map = self._get_pw_links_map()

        for ch in channels_to_sync:
            ch_id = ch["id"]
            is_linked = self.is_channel_linked(ch_id)
            
            # Find output ports for this channel
            ch_out_ports = []
            capture_source_name = None
            if ch_id == "mic":
                capture_source_name = "@DEFAULT_AUDIO_SOURCE@"
                for p in out_ports:
                    if ":capture_" in p:
                        ch_out_ports.append(p)
            else:
                assigned = self.get_assigned_apps(ch_id)
                if assigned:
                    capture_source_name = assigned[0]
                else:
                    capture_source_name = ch.get("name", ch_id)

                for app in assigned:
                    app_low = app.lower()
                    for p in out_ports:
                        p_low = p.lower()
                        if (app_low in p_low or p_low.startswith(app_low)) and ":output_" in p:
                            ch_out_ports.append(p)

            if ch_id != "mic" and ch_out_ports:
                # Ensure assigned apps don't directly play out to physical hardware sinks (bypass isolation)
                for src_p in ch_out_ports:
                    src_links = links_map.get(src_p, set())
                    for linked_dest in list(src_links):
                        if linked_dest.startswith("alsa_output.") and ":playback_" in linked_dest:
                            try:
                                subprocess.run(["pw-link", "-d", src_p, linked_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            except Exception:
                                pass

            for m in mixes_to_sync:
                m_id = m["id"]
                m_type = m.get("type", "source")
                playback_node = f"WaveController_{m_id}_Sink" if (m_type == "sink" or m_id == "personal") else f"WaveController_{m_id}_Source"

                target_prefixes = [f"WaveController_{m_id}_Sink:playback_", f"WaveController_{m_id}_Source:input_"]
                target_in_ports = []
                for p in in_ports:
                    for pref in target_prefixes:
                        if p.startswith(pref):
                            target_in_ports.append(p)

                is_enabled = self.is_channel_mix_enabled(ch_id, m_id)
                st = self.channel_states.get(ch_id, {}).get(m_id, {})
                vol_pct = st.get("volume", 80)
                is_muted = st.get("muted", False)

                if not is_linked:
                    # Unlinked Mode: Use independent submix loopback with per-mix attenuation
                    # Unlink direct pw-link between app and mix sink
                    if ch_out_ports:
                        for src_p in ch_out_ports:
                            for tgt_p in target_in_ports:
                                try:
                                    subprocess.run(["pw-link", "-d", src_p, tgt_p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                except Exception:
                                    pass

                    if is_enabled and capture_source_name:
                        self._ensure_submix_loopback(ch_id, m_id, capture_source_name, playback_node, vol_pct, is_muted)
                    else:
                        self._stop_submix_loopback(ch_id, m_id)
                else:
                    # Linked Mode: Stop dedicated loopback and route via direct low-latency pw-link
                    self._stop_submix_loopback(ch_id, m_id)

                    if not ch_out_ports or not target_in_ports:
                        continue

                    for src_p in ch_out_ports:
                        is_fl = "_FL" in src_p or "_1" in src_p or "_mono" in src_p.lower() or "_l" in src_p.lower()
                        is_fr = "_FR" in src_p or "_2" in src_p or "_r" in src_p.lower()
                        is_pure_mono = (len(ch_out_ports) == 1) or ("_mono" in src_p.lower())
                        for tgt_p in target_in_ports:
                            tgt_fl = "_FL" in tgt_p or "_1" in tgt_p or "_l" in tgt_p.lower()
                            tgt_fr = "_FR" in tgt_p or "_2" in tgt_p or "_r" in tgt_p.lower()
                            
                            match = False
                            if is_pure_mono:
                                match = True
                            elif is_fl and tgt_fl:
                                match = True
                            elif is_fr and tgt_fr:
                                match = True

                            if match:
                                cmd = ["pw-link"]
                                if not is_enabled:
                                    cmd.append("-d")
                                cmd.extend([src_p, tgt_p])
                                try:
                                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                except Exception:
                                    pass

        # Synchronize physical output target devices for all Sink mixes
        self._sync_mix_physical_output_routing(mix_id, out_ports, in_ports)

    def _get_pw_links_map(self) -> dict:
        """Returns a dict mapping source_port -> set(destination_ports) from PipeWire."""
        try:
            out = subprocess.check_output(["pw-link", "-l"], text=True, stderr=subprocess.DEVNULL)
            links = {}
            curr_src = None
            for line in out.splitlines():
                if not line:
                    continue
                if not line.startswith(" "):
                    curr_src = line.strip()
                    if curr_src not in links:
                        links[curr_src] = set()
                else:
                    line_str = line.strip()
                    if line_str.startswith("|->") or line_str.startswith("->"):
                        target = line_str.replace("|->", "").replace("->", "").strip()
                        if curr_src:
                            links[curr_src].add(target)
                    elif line_str.startswith("|<-") or line_str.startswith("<-"):
                        src = line_str.replace("|<-", "").replace("<-", "").strip()
                        if src not in links:
                            links[src] = set()
                        if curr_src:
                            links[src].add(curr_src)
            return links
        except Exception:
            return {}

    def _sync_mix_physical_output_routing(self, mix_id: str = None, out_ports: list = None, in_ports: list = None):
        """
        Routes WaveController Sink mixes (e.g. Personal Mix, Guest Mix)
        to their designated physical output target devices via pw-link.
        Also unlinks any obsolete or unassigned physical connections.
        """
        if out_ports is None:
            try:
                out_ports_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
                out_ports = [l.strip() for l in out_ports_raw.splitlines() if l.strip()]
            except Exception:
                out_ports = []

        if in_ports is None:
            try:
                in_ports_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
                in_ports = [l.strip() for l in in_ports_raw.splitlines() if l.strip()]
            except Exception:
                in_ports = []

        links_map = self._get_pw_links_map()

        # Find default physical sink if needed
        default_sink_name = ""
        try:
            default_sink_name = subprocess.check_output(["pactl", "get-default-sink"], text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            pass

        with self._lock:
            mixes_copy = list(self.mixes)

        mixes_to_sync = [m for m in mixes_copy if mix_id is None or m["id"] == mix_id]

        for m in mixes_to_sync:
            m_id = m["id"]
            m_type = m.get("type", "source")
            target_dev = m.get("target_device", "none" if m_id != "personal" else "default")

            if m_type != "sink" and m_id != "personal":
                continue

            mon_fl = f"WaveController_{m_id}_Sink:monitor_FL"
            mon_fr = f"WaveController_{m_id}_Sink:monitor_FR"

            desired_fl = set()
            desired_fr = set()

            if target_dev and target_dev != "none":
                clean_target = target_dev.replace("alsa_card.", "").replace("alsa_output.", "").replace("alsa_input.", "").strip().lower()
                for p in in_ports:
                    if p.startswith("WaveController_"):
                        continue
                    if ":playback_" not in p:
                        continue
                    
                    p_low = p.lower()
                    matched = False
                    if target_dev == "default":
                        if default_sink_name and default_sink_name.lower() in p_low:
                            matched = True
                        elif not default_sink_name:
                            matched = True
                    else:
                        if clean_target in p_low:
                            matched = True

                    if matched:
                        suffix = p.split(":")[-1].lower()
                        if "_fl" in suffix or suffix.endswith("_1") or suffix.endswith("_l") or suffix == "playback_0":
                            desired_fl.add(p)
                        elif "_fr" in suffix or suffix.endswith("_2") or suffix.endswith("_r") or suffix == "playback_1":
                            desired_fr.add(p)

            # Reconcile FL links
            current_fl_links = links_map.get(mon_fl, set())
            for linked_dest in list(current_fl_links):
                if linked_dest.startswith("alsa_output.") and linked_dest not in desired_fl:
                    try:
                        subprocess.run(["pw-link", "-d", mon_fl, linked_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
            if mon_fl in out_ports:
                for dest in desired_fl:
                    if dest not in current_fl_links:
                        try:
                            subprocess.run(["pw-link", mon_fl, dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass

            # Reconcile FR links
            current_fr_links = links_map.get(mon_fr, set())
            for linked_dest in list(current_fr_links):
                if linked_dest.startswith("alsa_output.") and linked_dest not in desired_fr:
                    try:
                        subprocess.run(["pw-link", "-d", mon_fr, linked_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
            if mon_fr in out_ports:
                for dest in desired_fr:
                    if dest not in current_fr_links:
                        try:
                            subprocess.run(["pw-link", mon_fr, dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass

    def get_channel_state(self, channel_id: str, mix_id: str) -> dict:
        with self._lock:
            st = self.channel_states.get(channel_id, {}).get(mix_id, {})
            return {
                "volume": st.get("volume", 80),
                "muted": st.get("muted", False),
                "linked": st.get("linked", True),
                "enabled": st.get("enabled", True)
            }

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
                is_compat = (ch_type == mx.get("type", "source"))
                self.channel_states[ch_id][mx["id"]] = {
                    "volume": 80,
                    "muted": False,
                    "linked": True,
                    "enabled": is_compat
                }

            self._refresh_node_cache()
            self._save_state_to_config(immediate=True)
            self._sync_channel_audio_routing(channel_id=ch_id)
            return new_ch

    def remove_channel(self, channel_id: str) -> bool:
        with self._lock:
            self.channels = [c for c in self.channels if c["id"] != channel_id]
            if channel_id in self.channel_states:
                del self.channel_states[channel_id]
            if channel_id in self.assigned_apps:
                del self.assigned_apps[channel_id]
            self._refresh_node_cache()
            self._save_state_to_config(immediate=True)
            self._sync_channel_audio_routing()
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

    def move_channel_up(self, channel_id: str) -> bool:
        """Moves a channel up by one position in the mixer matrix."""
        with self._lock:
            idx = next((i for i, c in enumerate(self.channels) if c["id"] == channel_id), -1)
            if idx > 0:
                self.channels[idx - 1], self.channels[idx] = self.channels[idx], self.channels[idx - 1]
                self._save_state_to_config(immediate=True)
                return True
        return False

    def move_channel_down(self, channel_id: str) -> bool:
        """Moves a channel down by one position in the mixer matrix."""
        with self._lock:
            idx = next((i for i, c in enumerate(self.channels) if c["id"] == channel_id), -1)
            if 0 <= idx < len(self.channels) - 1:
                self.channels[idx + 1], self.channels[idx] = self.channels[idx], self.channels[idx + 1]
                self._save_state_to_config(immediate=True)
                return True
        return False

    def reorder_channels_by_id(self, src_channel_id: str, dest_channel_id: str) -> bool:
        """Reorders channels by moving src_channel_id to the position of dest_channel_id."""
        with self._lock:
            src_idx = next((i for i, c in enumerate(self.channels) if c["id"] == src_channel_id), -1)
            dest_idx = next((i for i, c in enumerate(self.channels) if c["id"] == dest_channel_id), -1)
            if src_idx >= 0 and dest_idx >= 0 and src_idx != dest_idx:
                item = self.channels.pop(src_idx)
                self.channels.insert(dest_idx, item)
                self._save_state_to_config(immediate=True)
                return True
        return False

    @staticmethod
    def resolve_smart_mix_icon(name: str, mix_type: str = "source") -> str:
        """Auto-resolves a minimal symbolic icon based on the mix name and intended use case."""
        n = name.lower()
        if any(k in n for k in ["record", "podcast", "track", "capture"]):
            return "media-record-symbolic"
        elif any(k in n for k in ["chat", "discord", "voice", "talk", "comms", "zoom", "teams", "skype"]):
            return "user-available-symbolic"
        elif any(k in n for k in ["stream", "obs", "broadcast", "twitch", "youtube", "live"]):
            return "camera-web-symbolic"
        elif any(k in n for k in ["game", "gaming", "play"]):
            return "input-gaming-symbolic"
        elif any(k in n for k in ["music", "spotify", "media", "song", "soundtrack"]):
            return "applications-multimedia-symbolic"
        elif any(k in n for k in ["headphone", "personal", "monitor", "ear", "iem"]):
            return "audio-headphones-symbolic"
        elif any(k in n for k in ["speaker", "studio", "main", "desk", "soundbar"]):
            return "audio-speakers-symbolic"
        elif any(k in n for k in ["browser", "web", "chrome", "firefox", "video"]):
            return "applications-internet-symbolic"
        elif any(k in n for k in ["system", "alert", "sfx", "notification", "soundboard"]):
            return "preferences-system-symbolic"
        elif mix_type == "sink":
            return "audio-headphones-symbolic"
        else:
            return "audio-input-microphone-symbolic"

    def add_mix(self, name: str, subtitle: str = "Custom Mix", mix_type: str = "source", icon: str = None, color: str = "#3584e4", target_device: str = "none") -> dict:
        with self._lock:
            mix_id = name.lower().replace(" ", "_")
            existing_ids = [m["id"] for m in self.mixes]
            if mix_id in existing_ids:
                mix_id = f"{mix_id}_{len(self.mixes)}"
            
            if not icon:
                icon = self.resolve_smart_mix_icon(name, mix_type)

            new_mix = {
                "id": mix_id,
                "name": name,
                "subtitle": subtitle,
                "type": mix_type,
                "icon": icon,
                "color": color,
                "target_device": target_device if mix_type == "sink" else "none"
            }
            self.mixes.append(new_mix)
            for ch in self.channels:
                ch_id = ch["id"]
                if ch_id not in self.channel_states:
                    self.channel_states[ch_id] = {}
                self.channel_states[ch_id][mix_id] = {
                    "volume": 80,
                    "muted": False,
                    "linked": True,
                    "enabled": False
                }
            self._save_state_to_config(immediate=True)
            self._ensure_virtual_mix_nodes()
            self._refresh_node_cache()
            self._sync_channel_audio_routing(mix_id=mix_id)
            return new_mix

    def update_mix(self, mix_id: str, name: str = None, subtitle: str = None, color: str = None, icon: str = None, target_device: str = None) -> bool:
        """Updates metadata (name, subtitle, color, icon, target_device) of a configured mix and syncs PipeWire node descriptions."""
        with self._lock:
            for m in self.mixes:
                if m["id"] == mix_id:
                    if name:
                        m["name"] = name.strip()
                    if subtitle is not None:
                        m["subtitle"] = subtitle.strip()
                    if color:
                        m["color"] = color
                    if icon:
                        m["icon"] = icon
                    if target_device is not None:
                        m["target_device"] = target_device
                    self._save_state_to_config(immediate=True)
                    self._ensure_virtual_mix_nodes()
                    self._refresh_node_cache()
                    self._sync_channel_audio_routing(mix_id=mix_id)
                    return True
        return False

    def remove_mix(self, mix_id: str):
        """Removes a mix and tears down its PipeWire virtual audio device."""
        with self._lock:
            self.mixes = [m for m in self.mixes if m["id"] != mix_id]
            for ch_id in self.channel_states:
                self.channel_states[ch_id].pop(mix_id, None)
            self._save_state_to_config(immediate=True)
            self._ensure_virtual_mix_nodes()
            self._refresh_node_cache()
            self._sync_channel_audio_routing()

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
