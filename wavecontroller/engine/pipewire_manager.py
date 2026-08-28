import os
import re
import json
import subprocess
import threading
import time
from gi.repository import GLib

from .config_manager import config_manager
from wavecontroller.utils.logger import get_logger

log = get_logger("PipeWireManager")

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
        {"id": "personal", "name": "Personal Mix", "subtitle": "1 output", "icon": "personal-symbolic", "color": "#3db356", "type": "sink"}
    ]

    DEFAULT_APP_MAPPINGS = {
        "mic": ["System capture"]
    }

    def __init__(self, hardware_mgr=None):
        self.hardware_mgr = hardware_mgr
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
        self._submix_volume_queue = {} # {(channel_id, mix_id): (volume_pct, is_muted)}
        self._mix_volume_queue = {} # {mix_id: (volume_pct, is_muted)}
        self._volume_event = threading.Event()
        self._worker_thread = None
        self._sync_thread = None
        self._submix_procs = {} # {(channel_id, mix_id): subprocess.Popen}
        self._submix_node_ids = {} # {(channel_id, mix_id): [node_id, ...]}
        self._mix_node_ids_cache = {} # {mix_id: [node_id, ...]}
        self._in_flight_nodes = set()
        self._pending_node_dispatches = {}
        self._bound_stream_nodes = set()
        self.on_external_change_callback = None
        self._is_sleeping = False

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
                    "muted": False
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
                        "muted": False,
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
            channels_copy = list(self.channels)

        # Clean up any orphaned background pw-loopbacks from past crashed/killed sessions
        try:
            subprocess.run(["pkill", "-f", "pw-loopback.*WaveController_submix_"], stderr=subprocess.DEVNULL)
            time.sleep(0.05)
        except Exception:
            pass

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

        # Per-channel ingestion sinks for playback application channels
        for ch in channels_copy:
            ch_id = ch["id"]
            ch_name = ch.get("name", ch_id)
            is_source = (ch.get("type") == "source") or any(k in ch_id.lower() for k in ("mic", "fefine", "microphone", "wave", "elgato", "input", "capture"))
            if not is_source:
                node_name = f"WaveController_Channel_{ch_id}"
                needed_nodes[node_name] = (f"WaveController {ch_name} Channel", "Audio/Sink")

        existing_active_names = set()
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            valid_submix_names = set()
            for ch in channels_copy:
                for mx in mixes_copy:
                    valid_submix_names.add(f"WaveController_submix_{ch['id']}_{mx['id']}")

            for obj in data:
                props = obj.get("info", {}).get("props", {})
                n_name = props.get("node.name", "")
                n_desc = props.get("node.description", "")
                if "WaveController_submix_" in n_name:
                    sub_clean = n_name.replace("input.", "").replace("output.", "")
                    if sub_clean not in valid_submix_names:
                        obj_id = obj.get("id")
                        if obj_id:
                            subprocess.run(["pw-cli", "destroy", str(obj_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif n_name.startswith("WaveController_") or n_desc.startswith("WaveController "):
                    if n_name not in needed_nodes:
                        obj_id = obj.get("id")
                        if obj_id:
                            subprocess.run(["pw-cli", "destroy", str(obj_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        existing_active_names.add(n_name)
        except Exception:
            pass

        # 1. Provision any missing needed nodes
        nodes_created = False
        for node_name, (desc, media_class) in needed_nodes.items():
            if node_name not in existing_active_names:
                try:
                    cmd = f'{{ factory.name=support.null-audio-sink node.name="{node_name}" node.description="{desc}" media.class={media_class} object.linger=true }}'
                    subprocess.run(["pw-cli", "create-node", "adapter", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    nodes_created = True
                except Exception:
                    pass

        if nodes_created:
            time.sleep(0.08)

        with self._lock:
            self._mix_node_ids_cache.clear()

        # Real-time synchronization of PipeWire port connections (pw-link)
        self._sync_channel_audio_routing()

        # Enforce all configured volumes to override stale WirePlumber state on startup
        with self._lock:
            for ch in self.channels:
                ch_id = ch["id"]
                st_dict = self.channel_states.get(ch_id, {})
                for mx_id, st in st_dict.items():
                    if self.is_channel_mix_enabled(ch_id, mx_id):
                        self._submix_volume_queue[(ch_id, mx_id)] = (st.get("volume", 100), st.get("muted", False))
            for mx in self.mixes:
                mx_id = mx["id"]
                st = self.mix_states.get(mx_id, {})
                self._mix_volume_queue[mx_id] = (st.get("volume", 100), st.get("muted", False))
            self._volume_event.set()

    def stop(self):
        self.running = False
        self._volume_event.set()

        # 1. Gracefully reconnect all active app streams back to physical default audio sink
        try:
            out_ports_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
            in_ports_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
            out_ports = [l.strip() for l in out_ports_raw.splitlines() if l.strip()]
            in_ports = [l.strip() for l in in_ports_raw.splitlines() if l.strip()]

            # Determine default physical playback ports
            default_phys_in = []
            target_device = self.selected_monitor_device or ""
            clean_target = target_device.replace("alsa_card.", "").replace("alsa_output.", "").strip().lower()

            if clean_target and clean_target != "none":
                for p in in_ports:
                    if p.startswith("alsa_output.") and ":playback_" in p and clean_target in p.lower():
                        default_phys_in.append(p)

            if not default_phys_in:
                # Fallback to any active alsa_output sink ports
                default_phys_in = [p for p in in_ports if p.startswith("alsa_output.") and ":playback_" in p][:2]

            if default_phys_in:
                for ch in self.channels:
                    if ch.get("type") != "source" and not any(k in ch["id"].lower() for k in ("mic", "fefine", "microphone", "wave", "elgato", "input", "capture")):
                        ch_out = []
                        assigned = self.get_assigned_apps(ch["id"])
                        for app in assigned:
                            app_low = app.lower()
                            for p in out_ports:
                                if p.startswith("output.WaveController_") or p.startswith("WaveController_"):
                                    continue
                                p_low = p.lower()
                                if (app_low in p_low or p_low.startswith(app_low)) and ":output_" in p:
                                    ch_out.append(p)
                        if ch_out:
                            self._link_stereo_ports(ch_out, default_phys_in, unlink=False)
        except Exception:
            pass

        # 2. Cleanly terminate all submix loopback processes
        with self._lock:
            for p in list(self._submix_procs.values()):
                try:
                    p.terminate()
                except Exception:
                    pass
            self._submix_procs.clear()
            self._submix_node_ids.clear()
            self._submix_volume_queue.clear()
            self._mix_node_ids_cache.clear()
            self._mix_volume_queue.clear()

        # 3. Destroy per-channel ingestion sink nodes
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for obj in data:
                props = obj.get("info", {}).get("props", {})
                n_name = props.get("node.name", "")
                if n_name.startswith("WaveController_Channel_"):
                    obj_id = obj.get("id")
                    if obj_id:
                        subprocess.run(["pw-cli", "destroy", str(obj_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def on_system_suspend(self):
        """Prepares PipeWire manager for system sleep/suspend."""
        log.info("[WaveController.PipeWire] System going to sleep: pausing volume guards...")
        self._is_sleeping = True

    def on_system_resume(self):
        """Restores all channel master volumes, submix faders, and audio routing after system resume."""
        log.info("[WaveController.PipeWire] System resumed: restoring channel volumes and routing...")
        self._is_sleeping = False
        time.sleep(0.3)
        self._refresh_node_cache()

        # 1. Re-assert all Channel Master Volumes
        with self._lock:
            master_states = {k: dict(v) for k, v in self.channel_master_states.items()}
            submix_states = {k: {m: dict(v) for m, v in mv.items()} for k, mv in self.channel_states.items()}
            mix_states = {k: dict(v) for k, v in self.mix_states.items()}

        for ch_id, st in master_states.items():
            vol = st.get("volume", 80)
            muted = st.get("muted", False)
            self.set_channel_master_volume(ch_id, vol)
            if muted:
                self.set_channel_master_mute(ch_id, True)

        # 2. Re-assert all Submix Faders
        for ch_id, m_map in submix_states.items():
            for m_id, s_st in m_map.items():
                vol = s_st.get("volume", 80)
                muted = s_st.get("muted", False)
                self.set_channel_volume(ch_id, m_id, vol)
                if muted:
                    self.set_channel_mute(ch_id, m_id, True)

        # 3. Re-assert Mix Master Volumes
        for m_id, m_st in mix_states.items():
            vol = m_st.get("volume", 100)
            muted = m_st.get("muted", False)
            self.set_mix_master_volume(m_id, vol)
            if muted:
                self.set_mix_master_mute(m_id, True)

        # 4. Trigger volume event to dispatch to PipeWire nodes immediately
        with self._lock:
            self._volume_event.set()

        # 5. Re-synchronize channel audio routing
        self._sync_channel_audio_routing()

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
        """Monitors system source changes in real-time (e.g. system default microphone)."""
        while self.running:
            try:
                changed = False
                
                # 1. Sync Microphone (Source) Channel Master (Generic ALSA only; skip if Elgato hardware is managing preamp)
                is_elgato_mic = self.hardware_mgr and (getattr(self.hardware_mgr, "is_elgato", False) or getattr(self.hardware_mgr, "device_type", "") == "elgato")
                if not is_elgato_mic:
                    curr_mic_vol, curr_mic_muted = self._query_system_source_status()
                    if curr_mic_vol is not None:
                        with self._lock:
                            if "mic" not in self.channel_master_states:
                                self.channel_master_states["mic"] = {"volume": 80, "muted": False}
                            st = self.channel_master_states["mic"]
                            if abs(st["volume"] - curr_mic_vol) >= 2 or st["muted"] != curr_mic_muted:
                                st["volume"] = curr_mic_vol
                                st["muted"] = curr_mic_muted
                                changed = True

                if changed and self.on_external_change_callback:
                    GLib.idle_add(self.on_external_change_callback)

                # 2. Periodic real-time stream, guard & mix reconciliation
                sync_tick = getattr(self, "_sync_loop_tick", 0) + 1
                self._sync_loop_tick = sync_tick
                if sync_tick % 10 == 0:
                    self._reconcile_app_streams_fast()
                if sync_tick % 50 == 0:
                    self._enforce_exclusive_volume_guard()
                    self._ensure_mix_sinks_unmuted()
                    self._sync_channel_audio_routing()
            except Exception:
                pass
            time.sleep(0.04) # 25 Hz fast poller (40ms)

    def _enforce_exclusive_volume_guard(self):
        """
        Enforces Exclusive Volume Guard & Protection for hardware devices.
        When exclusive_output_lock is active:
          - Locks physical ALSA output sink volume to 100% (1.0) and unmuted in PipeWire
            so external tools (pavucontrol, GNOME volume) cannot attenuate physical DAC output.
        When exclusive_mic_lock is active:
          - Locks physical ALSA input source capture volume to 100% (1.0) in PipeWire
            so external apps (Discord AGC, WebRTC, pavucontrol) cannot override analog preamp gain.
        """
        if not self.hardware_mgr:
            return

        if getattr(self, "_is_sleeping", False):
            return

        excl_out = getattr(self.hardware_mgr, "exclusive_output_lock", True)
        excl_mic = getattr(self.hardware_mgr, "exclusive_mic_lock", True)

        if not excl_out and not excl_mic:
            return

        out_node_ids = set()
        in_node_ids = set()

        if hasattr(self.hardware_mgr, "discovered_devices"):
            for dev_key, dev in self.hardware_mgr.discovered_devices.items():
                is_elgato = dev.get("is_elgato", False) or "wave" in str(dev.get("name", "")).lower()
                if is_elgato:
                    if excl_out and dev.get("primary_sink_id"):
                        out_node_ids.add(str(dev["primary_sink_id"]))
                    for s in dev.get("sinks", []):
                        if excl_out and s.get("id"):
                            out_node_ids.add(str(s["id"]))

                    if excl_mic and dev.get("primary_source_id"):
                        in_node_ids.add(str(dev["primary_source_id"]))
                    for src in dev.get("sources", []):
                        if excl_mic and src.get("id"):
                            in_node_ids.add(str(src["id"]))

        # Fallback to scanning pw-dump if discovered_devices is not fully populated yet
        if (excl_out and not out_node_ids) or (excl_mic and not in_node_ids):
            try:
                out_raw = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
                objs = json.loads(out_raw)
                for obj in objs:
                    if obj.get("type") != "PipeWire:Interface:Node":
                        continue
                    props = obj.get("info", {}).get("props", {})
                    n_name = props.get("node.name", "").lower()
                    media_class = props.get("media.class", "")
                    if "wave" in n_name or "elgato" in n_name:
                        if excl_out and media_class == "Audio/Sink":
                            out_node_ids.add(str(obj["id"]))
                        elif excl_mic and media_class == "Audio/Source":
                            in_node_ids.add(str(obj["id"]))
            except Exception:
                pass

        # Enforce Output Sink Lock (100% volume in ALSA/PipeWire if exclusive_output_lock is True)
        if excl_out:
            for sink_id in out_node_ids:
                try:
                    out = subprocess.check_output(["wpctl", "get-volume", str(sink_id)], text=True, stderr=subprocess.DEVNULL).strip()
                    m = re.search(r'Volume:\s*([\d\.]+)', out)
                    if m:
                        vol_val = float(m.group(1))
                        if abs(vol_val - 1.0) > 0.005:
                            subprocess.run(["wpctl", "set-volume", str(sink_id), "1.0"], stderr=subprocess.DEVNULL)
                except Exception:
                    pass

        # Enforce Mic Capture Lock (100% capture volume in ALSA/PipeWire if exclusive_mic_lock is True)
        if excl_mic:
            for src_id in in_node_ids:
                try:
                    out = subprocess.check_output(["wpctl", "get-volume", str(src_id)], text=True, stderr=subprocess.DEVNULL).strip()
                    m = re.search(r'Volume:\s*([\d\.]+)', out)
                    if m:
                        vol_val = float(m.group(1))
                        if abs(vol_val - 1.0) > 0.005:
                            subprocess.run(["wpctl", "set-volume", str(src_id), "1.0"], stderr=subprocess.DEVNULL)
                except Exception:
                    pass

    def _ensure_mix_sinks_unmuted(self):
        """Enforces unmuted state on PipeWire virtual null-sinks so audio always passes to physical outputs."""
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for obj in data:
                props = obj.get("info", {}).get("props", {})
                n_name = props.get("node.name", "")
                if n_name.startswith("WaveController_") and n_name.endswith("_Sink"):
                    obj_id = obj.get("id")
                    if obj_id:
                        m_id = n_name.replace("WaveController_", "").replace("_Sink", "")
                        mix_muted = self.mix_states.get(m_id, {}).get("muted", False)
                        if not mix_muted:
                            try:
                                subprocess.run(["wpctl", "set-mute", str(obj_id), "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            except Exception:
                                pass
        except Exception:
            pass

    def _get_match_tokens(self, name_or_id: str) -> set:
        """Generates normalized matching tokens for any application, process binary, or audio device."""
        if not name_or_id:
            return set()
        raw = str(name_or_id).lower().strip()
        tokens = {raw}

        # 1. Spacing and punctuation permutations
        tokens.add(raw.replace(" ", "-"))
        tokens.add(raw.replace(" ", "_"))
        tokens.add(raw.replace(" ", ""))
        tokens.add(raw.replace("-", " "))
        tokens.add(raw.replace("_", " "))
        tokens.add(raw.replace("-", ""))
        tokens.add(raw.replace("_", ""))

        # 2. Known audio binary mappings (Chrome, VLC, Discord, Steam, OBS, etc.)
        for bin_name, (disp, alt) in self.KNOWN_AUDIO_BINARIES.items():
            if bin_name in raw or disp.lower() in raw or alt.lower() in raw:
                tokens.add(bin_name)
                tokens.add(alt.lower())
                tokens.add(disp.lower())
                tokens.add(disp.lower().replace(" ", "-"))
                tokens.add(disp.lower().replace(" ", "_"))

        # 3. Known hardware device aliases
        if any(w in raw for w in ("wave", "elgato", "0fd9")):
            tokens.update({"wave", "elgato", "0fd9", "wave_xlr", "wave-xlr"})
        if any(w in raw for w in ("fefine", "fifine", "3142")):
            tokens.update({"fifine", "fefine", "3142"})

        # 4. Extract individual distinct alphanumeric words (len >= 3)
        stop_words = {
            "the", "and", "for", "with", "player", "media", "audio", "sound",
            "stream", "desktop", "client", "app", "application", "input", "output",
            "stereo", "mono", "analog", "default", "system", "capture", "playback",
            "usb", "alsa", "pci", "card", "sink", "source", "device", "devices",
            "node", "nodes", "port", "ports"
        }
        words = [w for w in re.split(r"[\s\-_.:/]+", raw) if len(w) >= 3 and w not in stop_words]
        tokens.update(words)
        return tokens

    def _port_matches_tokens(self, port_name: str, tokens: set) -> bool:
        """Checks if a PipeWire port belongs to an application or device matching any token."""
        if not port_name or not tokens:
            return False
        p_low = port_name.lower()
        node_part = p_low.split(":")[0]
        node_clean = node_part.replace("-", " ").replace("_", " ")

        if node_part in tokens or node_clean in tokens:
            return True

        for t in tokens:
            if len(t) < 3:
                continue
            if t == node_part or t == node_clean:
                return True
            if t in node_part or t in node_clean or node_part.startswith(t):
                return True
        return False

    def _bind_app_to_wireplumber_target(self, app_name: str, channel_id: str):
        """Notifies WirePlumber via PipeWire metadata that an application belongs to its dedicated channel sink."""
        try:
            target_sink = f"WaveController_Channel_{channel_id}"
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            tokens = self._get_match_tokens(app_name)
            for obj in data:
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    if props.get("media.class") == "Stream/Output/Audio":
                        n_app = props.get("application.name", "").lower()
                        n_bin = props.get("application.process.binary", "").lower()
                        n_name = props.get("node.name", "").lower()
                        n_id = props.get("application.id", "").lower()
                        
                        match = False
                        for t in tokens:
                            if len(t) < 3:
                                continue
                            if t in n_app or t in n_bin or t in n_name or t in n_id or n_app.startswith(t):
                                match = True
                                break

                        if match:
                            nid = obj["id"]
                            if nid not in self._bound_stream_nodes:
                                subprocess.run(
                                    ["pw-metadata", "-n", "default", str(nid), "target.object", target_sink],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                                )
                                self._bound_stream_nodes.add(nid)
        except Exception:
            pass

    def _reconcile_app_streams_fast(self):
        """Ultra-fast reactive stream interceptor ensuring assigned apps
        are immediately attached to their dedicated channel sink and severed from default mix leaks."""
        with self._lock:
            has_assigned = any(bool(apps) for apps in self.assigned_apps.values())
        if not has_assigned:
            return

        try:
            o_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
            out_ports = [l.strip() for l in o_raw.splitlines() if l.strip()]

            # Quick filter for candidate non-WaveController application output ports
            app_ports = []
            for p in out_ports:
                if p.startswith("WaveController_") or p.startswith("output.WaveController_") or p.startswith("alsa_") or p.startswith("wave_"):
                    continue
                if ":output_" in p or ":playback_" in p or ":monitor_" in p:
                    app_ports.append(p)

            if not app_ports:
                return

            with self._lock:
                channels_copy = list(self.channels)

            links_map = None
            in_ports = None

            for ch in channels_copy:
                if ch.get("type") == "source":
                    continue
                ch_id = ch["id"]
                assigned = self.get_assigned_apps(ch_id)
                if not assigned:
                    continue

                for app in assigned:
                    tokens = self._get_match_tokens(app)
                    matched_ports = [p for p in app_ports if self._port_matches_tokens(p, tokens) and ":output_" in p]
                    if not matched_ports:
                        continue

                    if in_ports is None:
                        i_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
                        in_ports = [l.strip() for l in i_raw.splitlines() if l.strip()]

                    ch_prefix = f"WaveController_Channel_{ch_id}:"
                    ch_in_ports = [p for p in in_ports if p.startswith(ch_prefix) and ":playback_" in p]
                    if not ch_in_ports:
                        continue

                    if links_map is None:
                        links_map = self._get_pw_links_map()

                    # 1. Connect to channel ingestion sink if not already linked
                    need_link = False
                    for sp in matched_ports:
                        connected_dests = links_map.get(sp, set())
                        if not any(dp.startswith(ch_prefix) for dp in connected_dests):
                            need_link = True
                            break

                    if need_link:
                        self._link_stereo_ports(matched_ports, ch_in_ports, unlink=False)
                        self._bind_app_to_wireplumber_target(app, ch_id)
                        # Immediately sync downstream channel routing so audio flows through assigned mix without delay
                        self._sync_channel_audio_routing(channel_id=ch_id)

                    # 2. Immediately sever any leaking links to physical outputs or other mix sinks
                    for sp in matched_ports:
                        connected_dests = links_map.get(sp, set())
                        for dp in connected_dests:
                            if dp.startswith(ch_prefix):
                                continue
                            if dp.startswith("alsa_output.") or (dp.startswith("WaveController_") and ":playback_" in dp):
                                try:
                                    subprocess.run(["pw-link", "-d", sp, dp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                except Exception:
                                    pass
        except Exception:
            pass

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
                
                # Client Application Stream Isolation: NEVER index client playback streams for volume adjustments
                if media_class == "Stream/Output/Audio" and not props.get("node.name", "").startswith("output.WaveController_submix_"):
                    continue

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
                node_name = props.get("node.name", "")
                app_id = props.get("application.id", "")
                
                # Only include genuine client audio playback streams (not sinks, sources, or internal loopbacks)
                if media_class == "Stream/Output/Audio" or (media_type == "Audio" and not media_class.startswith("Audio/")):
                    name = props.get("application.name") or props.get("node.description") or props.get("media.name") or node_name
                    binary = props.get("application.process.binary", "")
                    icon = props.get("application.icon-name") or props.get("application.icon_name")
                    node_id = obj.get("id")
                    
                    if not name:
                        continue
                    name_low = str(name).lower()
                    bin_low = str(binary).lower()
                    node_low = str(node_name).lower()
                    app_id_low = str(app_id).lower()
                    
                    # Exclude internal virtual submixes, loopbacks, meters, and system utilities
                    internal_keywords = [
                        "wavecontroller", "submix", "loopback", "wave_sink", "wave_mic",
                        "vcp_monitor", "pw-record", "parecord", "pipewire", "wireplumber",
                        "easyeffects", "wpctl", "system_capture", "system capture",
                        "speech-dispatcher", "null-sink", "pw-loopback", "monitor",
                        "pavucontrol", "org.pulseaudio.pavucontrol"
                    ]
                    if any(kw in name_low or kw in bin_low or kw in node_low or kw in app_id_low for kw in internal_keywords):
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
            self._bind_app_to_wireplumber_target(app_name, channel_id)

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
            if not states:
                return True
            return any(s.get("linked", True) for s in states.values())

    def set_channel_linked(self, channel_id: str, linked: bool):
        """Sets the linking state for a channel across all mixes."""
        with self._lock:
            if channel_id in self.channel_states:
                for m_id in self.channel_states[channel_id]:
                    self.channel_states[channel_id][m_id]["linked"] = linked
                self._save_state_to_config(immediate=True)
                self._sync_channel_audio_routing(channel_id)

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
                            self._submix_volume_queue[(channel_id, mx_id)] = (vol, is_muted)

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

            # Master Channel Mute cascades to ALL compatible submixes unconditionally
            for mx in self.mixes:
                mx_id = mx["id"]
                if self.is_channel_mix_compatible(channel_id, mx_id):
                    if channel_id in self.channel_states and mx_id in self.channel_states[channel_id]:
                        self.channel_states[channel_id][mx_id]["muted"] = muted
                        sub_v = self.channel_states[channel_id][mx_id].get("volume", vol)
                        self._submix_volume_queue[(channel_id, mx_id)] = (sub_v, muted)

            # Enqueue physical stream volume dispatch
            self._volume_queue[channel_id] = (vol, muted)
            self._volume_event.set()
            self._save_state_to_config(immediate=False)
            self._sync_channel_audio_routing(channel_id)

            # Sync physical Elgato hardware mute if this is a mic channel
            if self.hardware_mgr and any(k in channel_id.lower() for k in ("elgato", "wave", "mic", "microphone")):
                self.hardware_mgr.set_mode_mute("gain", muted, transient=True)

    def toggle_channel_master_mute(self, channel_id: str) -> bool:
        with self._lock:
            if channel_id not in self.channel_master_states:
                self.channel_master_states[channel_id] = {"volume": 80, "muted": False}
            curr = self.channel_master_states[channel_id].get("muted", False)
            new_mute = not curr
            self.channel_master_states[channel_id]["muted"] = new_mute
            vol = self.channel_master_states[channel_id].get("volume", 80)

            # Master Channel Mute cascades to ALL compatible submixes unconditionally
            for mx in self.mixes:
                mx_id = mx["id"]
                if self.is_channel_mix_compatible(channel_id, mx_id):
                    if channel_id in self.channel_states and mx_id in self.channel_states[channel_id]:
                        self.channel_states[channel_id][mx_id]["muted"] = new_mute

            self._volume_queue[channel_id] = (vol, new_mute)
            self._volume_event.set()
            self._save_state_to_config(immediate=False)
            self._sync_channel_audio_routing(channel_id)

            # Sync physical Elgato hardware mute if this is a mic channel
            if self.hardware_mgr and any(k in channel_id.lower() for k in ("elgato", "wave", "mic", "microphone")):
                self.hardware_mgr.set_mode_mute("gain", new_mute, transient=True)

            return new_mute

    # -------------------------------------------------------------
    # Mix Master Bus Control (for Discord, OBS, Headphones, etc.)
    # -------------------------------------------------------------
    def _match_mix_id(self, mix_id: str) -> str:
        if not mix_id:
            with self._lock:
                return self.mixes[0]["id"] if self.mixes else "personal_mix"
        target_low = str(mix_id).lower().strip()
        with self._lock:
            # 1. Exact match
            for m in self.mixes:
                if m["id"].lower() == target_low or m["name"].lower() == target_low:
                    return m["id"]
            # 2. Suffix/prefix match (e.g. "personal" -> "personal_mix")
            for m in self.mixes:
                m_id_low = m["id"].lower()
                m_name_low = m["name"].lower()
                if target_low in m_id_low or m_id_low in target_low:
                    return m["id"]
                if target_low in m_name_low or m_name_low in target_low:
                    return m["id"]
        return target_low

    @staticmethod
    def _pct_to_pipewire_gain(vol_pct: int) -> float:
        """
        Converts 0-100% volume slider percentage to quadratic broadcast fader gain.
        Formula: (vol_pct / 100.0) ** 2
        Provides silky-smooth, 1:1 acoustic linearity matching human hearing (50% = -12 dB / half perceived loudness).
        """
        frac = max(0.0, min(1.5, float(vol_pct) / 100.0))
        return frac ** 2

    def _get_mix_node_ids(self, mix_id: str) -> list:
        canon_mix = self._match_mix_id(mix_id)
        with self._lock:
            cached = self._mix_node_ids_cache.get(canon_mix)
            if cached:
                return list(cached)

        ids = []
        target_sink = f"wavecontroller_{canon_mix.lower()}_sink"
        target_src = f"wavecontroller_{canon_mix.lower()}_source"
        with self._lock:
            mix_obj = next((m for m in self.mixes if m["id"] == canon_mix), None)
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
                    
                    # 1. Physical Hardware Sink Mix (e.g. Fifine, Mobo, USB DAC, etc.)
                    if (m_type == "sink" or "personal" in canon_mix) and target_dev and target_dev not in ("none", ""):
                        if "elgato" not in target_dev.lower() and "wave" not in target_dev.lower():
                            clean_target = target_dev.replace("alsa_card.", "").replace("alsa_output.", "").replace("alsa_input.", "").strip().lower()
                            if media_class == "Audio/Sink" and (clean_target in n_name or clean_target in props.get("node.description", "").lower()):
                                ids.append(obj_id)

                    # 2. Virtual Broadcast Source Mixes (e.g. Stream Mix, Chat Mix) or fallback when no physical dev assigned
                    if not ids:
                        if target_src in n_name or target_sink in n_name:
                            ids.append(obj_id)
        except Exception:
            pass

        if not ids:
            self._refresh_node_cache()
            with self._lock:
                for name, node_ids in self._node_cache.items():
                    if target_sink in name or target_src in name:
                        ids.extend(node_ids)

        unique_ids = list(set(ids))
        if unique_ids:
            with self._lock:
                self._mix_node_ids_cache[canon_mix] = unique_ids
        return unique_ids

    def get_mix_master_volume(self, mix_id: str) -> int:
        canon_mix = self._match_mix_id(mix_id)
        with self._lock:
            return self.mix_states.get(canon_mix, {}).get("volume", 100)

    def get_mix_master_mute(self, mix_id: str) -> bool:
        canon_mix = self._match_mix_id(mix_id)
        with self._lock:
            return self.mix_states.get(canon_mix, {}).get("muted", False)

    def set_mix_master_volume(self, mix_id: str, volume: int):
        canon_mix = self._match_mix_id(mix_id)
        with self._lock:
            if canon_mix not in self.mix_states:
                self.mix_states[canon_mix] = {"volume": 100, "muted": False}
            vol = max(0, min(100, volume))
            self.mix_states[canon_mix]["volume"] = vol
            self._mix_volume_queue[canon_mix] = (vol, self.mix_states[canon_mix].get("muted", False))
            self._volume_event.set()
            self._save_state_to_config(immediate=False)

    def set_mix_volume(self, mix_id: str, volume: int):
        self.set_mix_master_volume(mix_id, volume)

    def get_mix_volume(self, mix_id: str) -> int:
        return self.get_mix_master_volume(mix_id)

    def set_mix_master_mute(self, mix_id: str, muted: bool):
        canon_mix = self._match_mix_id(mix_id)
        with self._lock:
            if canon_mix not in self.mix_states:
                self.mix_states[canon_mix] = {"volume": 100, "muted": False}
            self.mix_states[canon_mix]["muted"] = muted
            self._mix_volume_queue[canon_mix] = (self.mix_states[canon_mix].get("volume", 100), muted)
            self._volume_event.set()
            self._save_state_to_config(immediate=False)
        self._sync_mix_physical_output_routing(canon_mix)

    def toggle_mix_master_mute(self, mix_id: str) -> bool:
        canon_mix = self._match_mix_id(mix_id)
        with self._lock:
            if canon_mix not in self.mix_states:
                self.mix_states[canon_mix] = {"volume": 100, "muted": False}
            curr = self.mix_states[canon_mix].get("muted", False)
            new_mute = not curr
            self.mix_states[canon_mix]["muted"] = new_mute
            self._mix_volume_queue[canon_mix] = (self.mix_states[canon_mix].get("volume", 100), new_mute)
            self._volume_event.set()
            self._save_state_to_config(immediate=False)
        self._sync_mix_physical_output_routing(canon_mix)
        return new_mute

    def _dispatch_node_volume(self, node_id: str, gain: float, is_muted: bool):
        """
        Dispatches volume to a PipeWire node with strict single in-flight queuing.
        Prevents WirePlumber process backlog during high-speed mouse dragging (60-120 FPS).
        """
        n_id_str = str(node_id)
        with self._lock:
            self._pending_node_dispatches[n_id_str] = (gain, is_muted)
            if n_id_str in self._in_flight_nodes:
                return
            self._in_flight_nodes.add(n_id_str)

        def _worker(nid: str):
            while True:
                with self._lock:
                    target = self._pending_node_dispatches.pop(nid, None)
                    if target is None:
                        self._in_flight_nodes.discard(nid)
                        break
                g_val, m_val = target
                try:
                    subprocess.run(["wpctl", "set-volume", nid, f"{g_val:.4f}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    subprocess.run(["wpctl", "set-mute", nid, "1" if m_val else "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass

        threading.Thread(target=_worker, args=(n_id_str,), daemon=True).start()

    def _apply_submix_gain(self, ch_id: str, m_id: str, vol_pct: int, is_muted: bool):
        """Applies independent sub-mix attenuation to dedicated PipeWire loopback stream node using broadcast fader curve."""
        canon_mix = self._match_mix_id(m_id)
        key = (ch_id, canon_mix)
        node_name = f"WaveController_submix_{ch_id}_{canon_mix}"
        gain = self._pct_to_pipewire_gain(vol_pct)
        
        node_ids = self._submix_node_ids.get(key, [])
        if not node_ids:
            try:
                out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
                data = json.loads(out)
                found = []
                for obj in data:
                    if obj.get("type") == "PipeWire:Interface:Node":
                        props = obj.get("info", {}).get("props", {})
                        n_name = props.get("node.name", "").lower()
                        if node_name.lower() in n_name:
                            # Target strictly the playback output stream to avoid double attenuation with capture input
                            if n_name.startswith("output.") or props.get("media.class") == "Stream/Output/Audio":
                                found.append(str(obj["id"]))
                            elif n_name.startswith("input.") or props.get("media.class") == "Stream/Input/Audio":
                                try:
                                    self._dispatch_node_volume(str(obj["id"]), 1.00, False)
                                except Exception:
                                    pass
                if not found:
                    for obj in data:
                        if obj.get("type") == "PipeWire:Interface:Node":
                            props = obj.get("info", {}).get("props", {})
                            n_name = props.get("node.name", "").lower()
                            if node_name.lower() in n_name:
                                found.append(str(obj["id"]))
                                break
                if found:
                    self._submix_node_ids[key] = found
                    node_ids = found
            except Exception:
                pass

        for n_id in node_ids:
            self._dispatch_node_volume(n_id, gain, is_muted)

    def _ensure_submix_loopback(self, ch_id: str, m_id: str, vol_pct: int, is_muted: bool):
        """Provisions an isolated, ultra-low latency sub-mix loopback stream with independent hardware DSP gain."""
        canon_mix = self._match_mix_id(m_id)
        key = (ch_id, canon_mix)
        node_name = f"WaveController_submix_{ch_id}_{canon_mix}"
        with self._lock:
            proc = self._submix_procs.get(key)
            if proc is None or proc.poll() is not None:
                self._submix_node_ids.pop(key, None)
                cmd = [
                    "pw-loopback",
                    "--capture-props={ node.autoconnect=false application.id=org.PulseAudio.pavucontrol media.role=volume-control }",
                    "--playback-props={ node.autoconnect=false }",
                    "-n", node_name,
                    "--latency=5"
                ]
                try:
                    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    self._submix_procs[key] = p
                    time.sleep(0.05)
                except Exception:
                    pass
        self._apply_submix_gain(ch_id, canon_mix, vol_pct, is_muted)

    def _stop_submix_loopback(self, ch_id: str, m_id: str):
        """Tears down the sub-mix loopback stream process cleanly."""
        canon_mix = self._match_mix_id(m_id)
        key = (ch_id, canon_mix)
        with self._lock:
            self._submix_node_ids.pop(key, None)
            proc = self._submix_procs.pop(key, None)
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=0.3)
                except Exception:
                    pass

    def set_channel_volume(self, channel_id: str, mix_id: str, volume: int):
        """Sets the sub-mix send level into a specific virtual mix bus."""
        canon_mix = self._match_mix_id(mix_id)
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
                    if canon_mix in self.channel_states[channel_id]:
                        self.channel_states[channel_id][canon_mix]["volume"] = vol
                    else:
                        # Fallback for dynamically created mix state
                        self.channel_states[channel_id][canon_mix] = {
                            "volume": vol,
                            "muted": False,
                            "linked": False,
                            "enabled": True
                        }
                self._save_state_to_config(immediate=False)

        if is_linked:
            self.set_channel_master_volume(channel_id, vol)
        else:
            is_muted = self.channel_states.get(channel_id, {}).get(canon_mix, {}).get("muted", False)
            with self._lock:
                self._submix_volume_queue[(channel_id, canon_mix)] = (vol, is_muted)
                self._volume_event.set()

    def set_channel_mute(self, channel_id: str, mix_id: str, muted: bool):
        """Mutes or unmutes a channel within a specific virtual mix bus (independent per-mix mute)."""
        canon_mix = self._match_mix_id(mix_id)
        with self._lock:
            if channel_id in self.channel_states and canon_mix in self.channel_states[channel_id]:
                self.channel_states[channel_id][canon_mix]["muted"] = muted
            vol = self.get_channel_volume(channel_id, canon_mix)
            self._submix_volume_queue[(channel_id, canon_mix)] = (vol, muted)
            self._volume_event.set()
            self._save_state_to_config(immediate=False)

    def toggle_channel_mute(self, channel_id: str, mix_id: str) -> bool:
        """Toggles mute state within a specific virtual mix bus."""
        canon_mix = self._match_mix_id(mix_id)
        curr = self.channel_states.get(channel_id, {}).get(canon_mix, {}).get("muted", False)
        new_mute = not curr
        self.set_channel_mute(channel_id, canon_mix, new_mute)
        return new_mute

    def _volume_worker_loop(self):
        """Persistent worker thread dispatching coalesced volume updates with single in-flight zero drag latency."""
        while self.running:
            self._volume_event.wait(timeout=0.5)
            self._volume_event.clear()

            if time.time() - self._last_cache_time > 5.0:
                self._refresh_node_cache()

            with self._lock:
                pending_master = dict(self._volume_queue)
                self._volume_queue.clear()
                pending_submix = dict(self._submix_volume_queue)
                self._submix_volume_queue.clear()
                pending_mix = dict(self._mix_volume_queue)
                self._mix_volume_queue.clear()

            # 1. Process Master Channel Volume Dispatches
            for channel_id, (volume_pct, is_muted) in pending_master.items():
                gain = self._pct_to_pipewire_gain(volume_pct)

                if channel_id == "mic":
                    last_v, last_m = getattr(self, "_last_mic_dispatch", (-1.0, None))
                    if abs(last_v - gain) > 0.001 or last_m != is_muted:
                        self._last_mic_dispatch = (gain, is_muted)
                        self._dispatch_node_volume("@DEFAULT_AUDIO_SOURCE@", gain, is_muted)
                    continue

                assigned_app_names = self.get_assigned_apps(channel_id)
                ch_name = ""
                with self._lock:
                    ch_obj = next((c for c in self.channels if c["id"] == channel_id), None)
                    if ch_obj:
                        ch_name = ch_obj.get("name", "")

                # If channel is an Elgato hardware device, bypass wpctl ALSA dispatch to avoid UAC2 hardware volume fights
                if "elgato" in channel_id.lower() or "wave_xlr" in channel_id.lower() or (ch_name and "elgato" in ch_name.lower()):
                    continue

                # Virtual playback sinks (WaveController_Channel_<ch>):
                # - When linked: virtual ingestion sink remains at unity (1.00) while submix loopback faders scale in lockstep.
                # - When unlinked: apply master channel volume directly to the virtual ingestion sink as pre-fader channel attenuation.
                is_virtual_sink = any(c.get("id") == channel_id and c.get("type") == "sink" for c in self.channels)
                target_node_ids = set()
                ch_sink_name = f"wavecontroller_channel_{channel_id}".lower()

                if is_virtual_sink:
                    sink_node_ids = set()
                    with self._lock:
                        if ch_sink_name in self._node_cache:
                            sink_node_ids.update(self._node_cache[ch_sink_name])

                    if not sink_node_ids:
                        self._refresh_node_cache()
                        with self._lock:
                            if ch_sink_name in self._node_cache:
                                sink_node_ids.update(self._node_cache[ch_sink_name])

                    sink_gain = 1.00 if self.is_channel_linked(channel_id) else gain
                    for s_id in sink_node_ids:
                        self._dispatch_node_volume(str(s_id), sink_gain, is_muted)
                    continue

                # Priority 1: Direct volume dispatch to the channel's dedicated virtual sink adapter
                with self._lock:
                    if ch_sink_name in self._node_cache:
                        target_node_ids.update(self._node_cache[ch_sink_name])

                if not target_node_ids:
                    self._refresh_node_cache()
                    with self._lock:
                        if ch_sink_name in self._node_cache:
                            target_node_ids.update(self._node_cache[ch_sink_name])

                # Priority 2: For hardware input capture sources (e.g. Fifine Mic, Mobo Mic) that don't use virtual playback sinks
                if not target_node_ids:
                    ch_type = ch_obj.get("type", "source") if ch_obj else "source"
                    is_source = (ch_type == "source") or any(k in channel_id.lower() for k in ("mic", "fefine", "fifine", "microphone", "input", "capture", "mobo"))
                    if is_source:
                        hw_search = set([channel_id.lower()])
                        if "fefine" in hw_search:
                            hw_search.add("fifine")
                        if "mobo" in hw_search or "motherboard" in hw_search:
                            hw_search.update(["starship", "matisse", "pci-0000_14_00.4"])
                        with self._lock:
                            for sk in hw_search:
                                for cached_name, node_ids in self._node_cache.items():
                                    if "elgato" in cached_name or "wave_xlr" in cached_name:
                                        continue
                                    if sk in cached_name:
                                        target_node_ids.update(node_ids)

                for node_id in target_node_ids:
                    self._dispatch_node_volume(str(node_id), gain, is_muted)

            # 2. Process Independent Sub-Mix Gain Dispatches
            for (ch_id, m_id), (volume_pct, is_muted) in pending_submix.items():
                self._apply_submix_gain(ch_id, m_id, volume_pct, is_muted)

            # 3. Process Mix Master Bus Output Volume Dispatches
            for mix_id, (volume_pct, is_muted) in pending_mix.items():
                mix_gain = self._pct_to_pipewire_gain(volume_pct)
                node_ids = self._get_mix_node_ids(mix_id)
                for n_id in node_ids:
                    self._dispatch_node_volume(str(n_id), mix_gain, is_muted)

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
            return st.get("enabled", False)

    def set_channel_mix_enabled(self, channel_id: str, mix_id: str, enabled: bool):
        """Enables or disables routing of a channel into a specific mix bus."""
        if enabled and not self.is_channel_mix_compatible(channel_id, mix_id):
            return
        with self._lock:
            if channel_id not in self.channel_states:
                self.channel_states[channel_id] = {}
            if mix_id not in self.channel_states[channel_id]:
                master_vol = self.get_channel_master_volume(channel_id)
                self.channel_states[channel_id][mix_id] = {
                    "volume": master_vol,
                    "muted": False,
                    "linked": True,
                    "enabled": enabled
                }
            else:
                self.channel_states[channel_id][mix_id]["enabled"] = enabled
            
            if enabled:
                st = self.channel_states[channel_id][mix_id]
                self._submix_volume_queue[(channel_id, mix_id)] = (st.get("volume", 80), st.get("muted", False))
                self._volume_event.set()

            self._save_state_to_config(immediate=True)
        self._sync_channel_audio_routing(channel_id=channel_id)

    def _link_stereo_ports(self, src_ports: list, dst_ports: list, unlink: bool = False):
        """Helper to establish or destroy stereo/mono PipeWire link connections accurately."""
        if not src_ports or not dst_ports:
            return
        for src_p in src_ports:
            is_fl = "_fl" in src_p.lower() or "_1" in src_p or "_mono" in src_p.lower() or "_l" in src_p.lower()
            is_fr = "_fr" in src_p.lower() or "_2" in src_p or "_r" in src_p.lower()
            is_pure_mono = (len(src_ports) == 1) or ("_mono" in src_p.lower())
            for dst_p in dst_ports:
                dst_fl = "_fl" in dst_p.lower() or "_1" in dst_p or "_l" in dst_p.lower()
                dst_fr = "_fr" in dst_p.lower() or "_2" in dst_p or "_r" in dst_p.lower()
                
                match = False
                if is_pure_mono:
                    match = True
                elif is_fl and dst_fl:
                    match = True
                elif is_fr and dst_fr:
                    match = True

                if match:
                    cmd = ["pw-link"]
                    if unlink:
                        cmd.append("-d")
                    cmd.extend([src_p, dst_p])
                    try:
                        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass

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
            is_source_channel = (ch.get("type") == "source") or any(k in ch_id.lower() for k in ("mic", "fefine", "microphone", "wave", "elgato", "input", "capture"))
            
            # Find output ports for this channel
            ch_out_ports = []
            app_out_ports = []
            if is_source_channel:
                ch_name = str(ch.get("name", ""))
                ch_id_str = str(ch_id)
                assigned_devs = self.get_assigned_apps(ch_id)
                
                # Build comprehensive matching tokens for this input channel
                input_tokens = self._get_match_tokens(ch_id_str)
                input_tokens.update(self._get_match_tokens(ch_name))
                for dev in assigned_devs:
                    input_tokens.update(self._get_match_tokens(dev))

                matched_ports = []
                for p in out_ports:
                    if p.startswith("output.WaveController_") or p.startswith("WaveController_") or ":monitor_" in p:
                        continue
                    if ":capture_" in p:
                        if self._port_matches_tokens(p, input_tokens):
                            matched_ports.append(p)
                ch_out_ports = matched_ports
            else:
                # Route through per-channel ingestion sink
                ch_sink_prefix = f"WaveController_Channel_{ch_id}:"
                ch_sink_out_ports = [p for p in out_ports if p.startswith(ch_sink_prefix) and ":monitor_" in p]
                ch_sink_in_ports = [p for p in in_ports if p.startswith(ch_sink_prefix) and ":playback_" in p]

                # Find actual application output ports
                app_out_ports = []
                assigned = self.get_assigned_apps(ch_id)
                for app in assigned:
                    tokens = self._get_match_tokens(app)
                    for p in out_ports:
                        if p.startswith("output.WaveController_") or p.startswith("WaveController_"):
                            continue
                        if ":output_" in p and self._port_matches_tokens(p, tokens):
                            app_out_ports.append(p)

                # Link apps -> channel sink (ingestion point)
                if ch_sink_in_ports and app_out_ports:
                    self._link_stereo_ports(app_out_ports, ch_sink_in_ports, unlink=False)

                # Use channel sink monitor ports as the output for downstream mix routing
                ch_out_ports = ch_sink_out_ports

            if not is_source_channel and app_out_ports:
                # Ensure assigned apps don't directly play out to physical hardware sinks or mix sinks (bypass isolation)
                own_prefix = f"WaveController_Channel_{ch_id}:"
                for src_p in app_out_ports:
                    src_links = links_map.get(src_p, set())
                    for linked_dest in list(src_links):
                        if linked_dest.startswith("alsa_output.") and ":playback_" in linked_dest:
                            try:
                                subprocess.run(["pw-link", "-d", src_p, linked_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            except Exception:
                                pass
                        # Also sever links to mix sinks or OTHER channel sinks (prevent bypass & bleed)
                        elif linked_dest.startswith("WaveController_") and ":playback_" in linked_dest:
                            if not linked_dest.startswith(own_prefix):
                                try:
                                    subprocess.run(["pw-link", "-d", src_p, linked_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                except Exception:
                                    pass

            # Proactively sever ANY existing links from this channel to mixes where it is disabled
            for src_p in ch_out_ports:
                for linked_dest in list(links_map.get(src_p, set())):
                    if linked_dest.startswith("WaveController_") and (":playback_" in linked_dest or ":input_" in linked_dest):
                        for m in self.mixes:
                            m_pref_sink = f"WaveController_{m['id']}_Sink:playback_"
                            m_pref_source = f"WaveController_{m['id']}_Source:input_"
                            if linked_dest.startswith(m_pref_sink) or linked_dest.startswith(m_pref_source):
                                if not self.is_channel_mix_enabled(ch_id, m["id"]):
                                    try:
                                        subprocess.run(["pw-link", "-d", src_p, linked_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                    except Exception:
                                        pass

            for m in mixes_to_sync:
                m_id = m["id"]
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

                # Use dedicated submix loopback faders with real-time attenuation for all active mixes
                # 1. Sever any direct unattenuated link between channel output and mix target
                self._link_stereo_ports(ch_out_ports, target_in_ports, unlink=True)

                if is_enabled and not is_muted:
                    self._ensure_submix_loopback(ch_id, m_id, vol_pct, is_muted=False)
                    
                    loopback_in_prefix = f"input.WaveController_submix_{ch_id}_{m_id}:input_"
                    loopback_out_prefix = f"output.WaveController_submix_{ch_id}_{m_id}:output_"
                    
                    lb_in_ports = [p for p in in_ports if p.startswith(loopback_in_prefix)]
                    lb_out_ports = [p for p in out_ports if p.startswith(loopback_out_prefix)]

                    if not lb_in_ports or not lb_out_ports:
                        for _ in range(8):
                            time.sleep(0.03)
                            try:
                                o_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
                                i_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
                                out_ports = [l.strip() for l in o_raw.splitlines() if l.strip()]
                                in_ports = [l.strip() for l in i_raw.splitlines() if l.strip()]
                                lb_in_ports = [p for p in in_ports if p.startswith(loopback_in_prefix)]
                                lb_out_ports = [p for p in out_ports if p.startswith(loopback_out_prefix)]
                                if lb_in_ports and lb_out_ports:
                                    target_in_ports = [p for p in in_ports if any(p.startswith(pref) for pref in target_prefixes)]
                                    break
                            except Exception:
                                pass

                    # Link Stage 1: Channel Output -> Loopback Input
                    self._link_stereo_ports(ch_out_ports, lb_in_ports, unlink=False)
                    # Link Stage 2: Loopback Output -> Mix Target Input
                    self._link_stereo_ports(lb_out_ports, target_in_ports, unlink=False)
                else:
                    self._stop_submix_loopback(ch_id, m_id)
                    self._link_stereo_ports(ch_out_ports, target_in_ports, unlink=True)

        # Synchronize physical output target devices for all Sink mixes
        self._sync_mix_physical_output_routing(mix_id, out_ports, in_ports)

        # Strict Mix Ingestion Shield: Ensure no client application can ever connect directly to a mix sink or source.
        # Only authorized submix loopbacks (output.WaveController_submix_*) are permitted to feed into mix devices.
        try:
            fresh_links = self._get_pw_links_map()
            for m in mixes_copy:
                m_id = m["id"]
                target_prefixes = (f"WaveController_{m_id}_Sink:playback_", f"WaveController_{m_id}_Source:input_")
                for src_p, dests in fresh_links.items():
                    if not src_p.startswith("output.WaveController_submix_"):
                        for dest_p in dests:
                            if any(dest_p.startswith(pref) for pref in target_prefixes):
                                try:
                                    subprocess.run(["pw-link", "-d", src_p, dest_p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                except Exception:
                                    pass
        except Exception:
            pass

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

            is_mix_muted = self.get_mix_master_mute(m_id)
            if target_dev and target_dev != "none" and not is_mix_muted:
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
                        else:
                            dev_tokens = self._get_match_tokens(clean_target)
                            if self._port_matches_tokens(p, dev_tokens):
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
        canon_mix = self._match_mix_id(mix_id)
        with self._lock:
            st = self.channel_states.get(channel_id, {}).get(canon_mix, {})
            return {
                "volume": st.get("volume", 80),
                "muted": st.get("muted", False),
                "linked": st.get("linked", True),
                "enabled": st.get("enabled", True)
            }

    def get_channel_mute(self, channel_id: str, mix_id: str) -> bool:
        return self.get_channel_state(channel_id, mix_id).get("muted", False)

    def get_channel_volume(self, channel_id: str, mix_id: str) -> int:
        return self.get_channel_state(channel_id, mix_id).get("volume", 80)

    def add_channel(self, name: str, icon: str = None, ch_type: str = "sink", assigned_apps: list = None, sync_meter: bool = False) -> dict:
        with self._lock:
            ch_id = name.lower().replace(" ", "_").replace("/", "_").replace(".", "_")
            existing_ids = [c["id"] for c in self.channels]
            if ch_id in existing_ids:
                ch_id = f"{ch_id}_{len(self.channels)}"

            resolved_icon = icon or self.resolve_icon_for_app(name)
            default_vol = 80
            new_ch = {
                "id": ch_id,
                "name": name,
                "type": ch_type,
                "icon": resolved_icon,
                "default_vol": default_vol,
                "sync_meter": sync_meter
            }
            self.channels.append(new_ch)
            self.channel_master_states[ch_id] = {
                "volume": default_vol,
                "muted": False
            }
            self.channel_states[ch_id] = {}
            self.assigned_apps[ch_id] = assigned_apps if assigned_apps is not None else ([name] if ch_type == "sink" else [])
            for mx in self.mixes:
                self.channel_states[ch_id][mx["id"]] = {
                    "volume": default_vol,
                    "muted": False,
                    "linked": True,
                    "enabled": False
                }

            self._refresh_node_cache()
            self._save_state_to_config(immediate=True)
            self._ensure_virtual_mix_nodes()
            self._volume_queue[ch_id] = (default_vol, False)
            self._volume_event.set()
            self._sync_channel_audio_routing(channel_id=ch_id)
            return new_ch

    def remove_channel(self, channel_id: str) -> bool:
        with self._lock:
            # 1. Cleanly terminate and teardown all submix loopback processes for this channel
            keys_to_remove = [k for k in list(self._submix_procs.keys()) if k[0] == channel_id]
            for k in keys_to_remove:
                proc = self._submix_procs.pop(k, None)
                if proc:
                    try:
                        proc.terminate()
                        proc.wait(timeout=0.2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                self._submix_node_ids.pop(k, None)
                self._submix_volume_queue.pop(k, None)

            # 2. Terminate any orphan loopbacks matching this channel
            try:
                subprocess.run(["pkill", "-f", f"WaveController_submix_{channel_id}_"], stderr=subprocess.DEVNULL)
            except Exception:
                pass

            # 3. Sever all PipeWire links associated with this channel
            try:
                out = subprocess.check_output(["pw-link", "-l"], text=True, stderr=subprocess.DEVNULL)
                curr_src = None
                for line in out.splitlines():
                    l_str = line.strip()
                    if not line.startswith(" ") and ":" in l_str:
                        curr_src = l_str
                    elif "|->" in l_str and curr_src:
                        dest_p = l_str.replace("|->", "").strip()
                        if channel_id in curr_src or channel_id in dest_p:
                            subprocess.run(["pw-link", "-d", curr_src, dest_p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

            # 4. Fallback assigned applications to physical hardware output so they never attach to virtual mixes
            assigned = list(self.assigned_apps.get(channel_id, []))
            if assigned:
                try:
                    out_ports_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
                    in_ports_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
                    out_ports = [l.strip() for l in out_ports_raw.splitlines() if l.strip()]
                    in_ports = [l.strip() for l in in_ports_raw.splitlines() if l.strip()]

                    # Find physical hardware playback ports
                    default_phys_in = []
                    target_device = self.selected_monitor_device or ""
                    clean_target = target_device.replace("alsa_card.", "").replace("alsa_output.", "").strip().lower()
                    if clean_target and clean_target != "none":
                        for p in in_ports:
                            if p.startswith("alsa_output.") and ":playback_" in p and clean_target in p.lower():
                                default_phys_in.append(p)
                    if not default_phys_in:
                        default_phys_in = [p for p in in_ports if p.startswith("alsa_output.") and ":playback_" in p][:2]

                    # Find the app's output ports and reconnect to physical audio
                    app_out_ports = []
                    for app in assigned:
                        tokens = self._get_match_tokens(app)
                        for p in out_ports:
                            if p.startswith("output.WaveController_") or p.startswith("WaveController_"):
                                continue
                            if ":output_" in p and self._port_matches_tokens(p, tokens):
                                app_out_ports.append(p)

                    # Explicitly unroute the deleted channel's applications from any WaveController mix sink/source
                    if app_out_ports:
                        for p in in_ports:
                            if p.startswith("WaveController_") and (":playback_" in p or ":input_" in p):
                                self._link_stereo_ports(app_out_ports, [p], unlink=True)

                    if default_phys_in and app_out_ports:
                        self._link_stereo_ports(app_out_ports, default_phys_in, unlink=False)
                except Exception:
                    pass

            self.channels = [c for c in self.channels if c["id"] != channel_id]
            if channel_id in self.channel_states:
                del self.channel_states[channel_id]
            if channel_id in self.assigned_apps:
                del self.assigned_apps[channel_id]
            if hasattr(self, "channel_master_states") and channel_id in self.channel_master_states:
                del self.channel_master_states[channel_id]

            self._refresh_node_cache()
            self._save_state_to_config(immediate=True)
            self._ensure_virtual_mix_nodes()
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

    def move_mix_left(self, mix_id: str) -> bool:
        """Moves a mix column left by one position in the mixer matrix."""
        with self._lock:
            idx = next((i for i, m in enumerate(self.mixes) if m["id"] == mix_id), -1)
            if idx > 0:
                self.mixes[idx - 1], self.mixes[idx] = self.mixes[idx], self.mixes[idx - 1]
                self._save_state_to_config(immediate=True)
                return True
        return False

    def move_mix_right(self, mix_id: str) -> bool:
        """Moves a mix column right by one position in the mixer matrix."""
        with self._lock:
            idx = next((i for i, m in enumerate(self.mixes) if m["id"] == mix_id), -1)
            if 0 <= idx < len(self.mixes) - 1:
                self.mixes[idx + 1], self.mixes[idx] = self.mixes[idx], self.mixes[idx + 1]
                self._save_state_to_config(immediate=True)
                return True
        return False

    def reorder_mixes_by_id(self, src_mix_id: str, dest_mix_id: str) -> bool:
        """Reorders mixes by moving src_mix_id to the position of dest_mix_id."""
        with self._lock:
            src_idx = next((i for i, m in enumerate(self.mixes) if m["id"] == src_mix_id), -1)
            dest_idx = next((i for i, m in enumerate(self.mixes) if m["id"] == dest_mix_id), -1)
            if src_idx >= 0 and dest_idx >= 0 and src_idx != dest_idx:
                item = self.mixes.pop(src_idx)
                self.mixes.insert(dest_idx, item)
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
            self.mix_states[mix_id] = {"volume": 100, "muted": False}
            for ch in self.channels:
                ch_id = ch["id"]
                master_vol = self.get_channel_master_volume(ch_id)
                if ch_id not in self.channel_states:
                    self.channel_states[ch_id] = {}
                self.channel_states[ch_id][mix_id] = {
                    "volume": master_vol,
                    "muted": False,
                    "linked": True,
                    "enabled": False
                }
            self._save_state_to_config(immediate=True)
            self._ensure_virtual_mix_nodes()
            self._refresh_node_cache()
            self._mix_volume_queue[mix_id] = (100, False)
            self._volume_event.set()
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
        """Removes a mix and tears down its PipeWire virtual audio device and all associated submix loopbacks."""
        canon_mix = self._match_mix_id(mix_id)
        with self._lock:
            # 1. Cleanly terminate and teardown all submix loopback processes for this mix
            keys_to_remove = [k for k in list(self._submix_procs.keys()) if k[1] == mix_id or k[1] == canon_mix]
            for k in keys_to_remove:
                proc = self._submix_procs.pop(k, None)
                if proc:
                    try:
                        proc.terminate()
                        proc.wait(timeout=0.2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                self._submix_node_ids.pop(k, None)
                self._submix_volume_queue.pop(k, None)

            # 2. Terminate any orphan loopbacks matching this mix
            try:
                subprocess.run(["pkill", "-f", f"WaveController_submix_.*_{mix_id}"], stderr=subprocess.DEVNULL)
                if canon_mix != mix_id:
                    subprocess.run(["pkill", "-f", f"WaveController_submix_.*_{canon_mix}"], stderr=subprocess.DEVNULL)
            except Exception:
                pass

            # 3. Sever all PipeWire links associated with this mix
            try:
                out = subprocess.check_output(["pw-link", "-l"], text=True, stderr=subprocess.DEVNULL)
                curr_src = None
                mix_prefixes = [f"WaveController_{mix_id}_Sink", f"WaveController_{mix_id}_Source",
                                f"WaveController_{canon_mix}_Sink", f"WaveController_{canon_mix}_Source"]
                for line in out.splitlines():
                    l_str = line.strip()
                    if not line.startswith(" ") and ":" in l_str:
                        curr_src = l_str
                    elif "|->" in l_str and curr_src:
                        dest_p = l_str.replace("|->", "").strip()
                        if any(mp in curr_src or mp in dest_p for mp in mix_prefixes):
                            subprocess.run(["pw-link", "-d", curr_src, dest_p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

            self.mixes = [m for m in self.mixes if m["id"] != mix_id and m["id"] != canon_mix]
            for ch_id in self.channel_states:
                self.channel_states[ch_id].pop(mix_id, None)
                self.channel_states[ch_id].pop(canon_mix, None)
            if hasattr(self, "mix_states"):
                self.mix_states.pop(mix_id, None)
                self.mix_states.pop(canon_mix, None)

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
