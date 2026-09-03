import os
import re
import json
import subprocess
import threading
import time
from gi.repository import GLib

from .config_manager import config_manager
from wavecontroller.engine.graph.process_classifier import (
    KNOWN_AUDIO_BINARIES,
    get_match_tokens,
    get_active_port_metadata_map,
    port_matches_tokens
)
from wavecontroller.engine.routing.source_manager import MicrophoneSourceManager
from wavecontroller.engine.routing.sink_manager import SubmixSinkManager
from wavecontroller.engine.routing.app_tracker import AppStreamTracker
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
        saved_channels = config_manager.get("channels", None)
        saved_mixes = config_manager.get("mixes", None)
        saved_apps = config_manager.get("assigned_apps", None)
        saved_states = config_manager.get("channel_states", None)
        saved_masters = config_manager.get("channel_master_states", None)
        saved_mix_states = config_manager.get("mix_states", None)
        first_run_done = config_manager.get("first_run_completed", False)
        has_saved_channels = saved_channels is not None and len(saved_channels) > 0

        if not first_run_done and not has_saved_channels:
            self.channels = []
            self.mixes = []
            self.assigned_apps = {}
            self.channel_states = {}
            self.channel_master_states = {}
            self.mix_states = {}
        else:
            self.channels = list(saved_channels) if saved_channels is not None else list(self.DEFAULT_CHANNELS)
            self.mixes = list(saved_mixes) if saved_mixes is not None else list(self.DEFAULT_MIXES)
            self.assigned_apps = dict(saved_apps) if saved_apps is not None else dict(self.DEFAULT_APP_MAPPINGS)
            self.channel_states = dict(saved_states) if saved_states is not None else {}
            self.channel_master_states = dict(saved_masters) if saved_masters is not None else {}
            self.mix_states = dict(saved_mix_states) if saved_mix_states is not None else {}
        self.output_devices = []
        self.selected_monitor_device = config_manager.get("default_output_device", None)
        if self.selected_monitor_device and "wavecontroller" in str(self.selected_monitor_device).lower():
            self.selected_monitor_device = None
        self.default_input_device = config_manager.get("default_input_device", "")
        if not self.default_input_device:
            for ch in self.channels:
                if ch.get("type") == "source" or ch.get("id") in ("mic", "elgato_wave_xlr"):
                    apps = self.assigned_apps.get(ch["id"], [])
                    if apps:
                        self.default_input_device = apps[-1] if len(apps) > 1 else apps[0]
                    break
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
        self._bound_unassigned_nodes = set()   # tracks unassigned streams bound to physical fallback
        self.on_external_change_callback = None
        self._is_sleeping = False
        self.peak_monitor = None

        # Dedicated Isolated Routing Sub-Managers
        self.source_manager = MicrophoneSourceManager(self, hardware_mgr=self.hardware_mgr)
        self.sink_manager = SubmixSinkManager(self)
        self.app_tracker = AppStreamTracker(self)

        self._init_default_states()

    def set_peak_monitor(self, peak_mon):
        """Links the active MultiChannelPeakMonitor instance for real-time <25ms reactive metering."""
        self.peak_monitor = peak_mon

    def _notify_peak_monitor_refresh(self):
        """Immediately signals the peak monitor to attach to newly assigned or modified streams (<25ms)."""
        pm = getattr(self, "peak_monitor", None)
        if pm and hasattr(pm, "trigger_refresh"):
            pm.trigger_refresh()

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

        valid_ch_ids = {ch["id"] for ch in self.channels}
        self.assigned_apps = {k: v for k, v in self.assigned_apps.items() if k in valid_ch_ids}
        self.channel_states = {k: v for k, v in self.channel_states.items() if k in valid_ch_ids}
        self.channel_master_states = {k: v for k, v in self.channel_master_states.items() if k in valid_ch_ids}

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
        if hasattr(self, "source_manager") and self.source_manager:
            return self.source_manager.get_system_source_status()
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
        # Session startup: clean up any orphaned background pw-loopbacks from past crashed sessions
        try:
            subprocess.run(["pkill", "-f", "pw-loopback.*WaveController_submix_"], stderr=subprocess.DEVNULL)
            time.sleep(0.05)
        except Exception:
            pass

        self.running = True
        self.refresh_devices()
        self._ensure_virtual_mix_nodes()
        self._refresh_node_cache()
        self.apply_pipewire_quantum()
        self._ensure_client_streams_unity_volume()
        
        # 1. Volume dispatch worker
        self._worker_thread = threading.Thread(target=self._volume_worker_loop, daemon=True)
        self._worker_thread.start()

        # 2. External volume sync poller (Syncs Volume Controller Plus on Stream Deck +)
        self._sync_thread = threading.Thread(target=self._external_sync_loop, daemon=True)
        self._sync_thread.start()

    def apply_pipewire_quantum(self, quantum=None) -> bool:
        """Applies the configured system-wide PipeWire processing quantum."""
        if quantum is None:
            quantum = config_manager.get("pipewire_quantum", 512)
        try:
            quantum = int(quantum)
        except (TypeError, ValueError):
            return False
        if quantum not in (256, 512, 1024):
            return False

        result = subprocess.run(
            ["pw-metadata", "-n", "settings", "0", "clock.quantum", str(quantum)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        if result.returncode == 0:
            config_manager.set("pipewire_quantum", quantum, immediate=True)
            log.info(f"[WaveController.PipeWire] Set global quantum to {quantum} frames")
            return True
        log.warning(f"[WaveController.PipeWire] Failed to set global quantum to {quantum} frames")
        return False

    def _ensure_virtual_mix_nodes(self):
        """
        Synchronizes PipeWire virtual audio nodes strictly with currently configured mixes.
        Prunes any stale/orphan WaveController virtual devices and provisions only active Source/Sink nodes.
        """
        with self._lock:
            mixes_copy = list(self.mixes)
            channels_copy = list(self.channels)

        needed_nodes = {}
        for m in mixes_copy:
            m_id = m["id"]
            m_name = m["name"]
            m_type = m.get("type", "source")

            if m_id == "personal" or m_type == "sink":
                node_name = f"WaveController_{m_id}_Sink"
                needed_nodes[node_name] = (f"WaveController {m_name} (Sink)", "Audio/Sink", False)
            else:
                node_name = f"WaveController_{m_id}_Source"
                needed_nodes[node_name] = (f"WaveController {m_name}", "Audio/Duplex")

        # Provision dedicated pre-fader virtual ingestion nodes ONLY for exposed Group Channels
        for ch in channels_copy:
            ch_id = ch["id"]
            ch_type = ch.get("type", "sink")
            if ch_type == "source":
                continue
            if ch.get("expose_sink", False):
                ch_name = ch.get("name", ch_id)
                node_name = f"WaveController_Channel_{ch_id}"
                # Exposed virtual sound card for Group Channels (visible in GNOME Settings and app pickers)
                needed_nodes[node_name] = (f"WaveController {ch_name} (Sink)", "Audio/Sink", False)

        # Provision dedicated Fallback cleanup (ensure no orphan fallback nodes remain)
        needed_nodes.pop("WaveController_Fallback_Sink", None)

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
                    elif props.get("media.class") != needed_nodes[n_name][1]:
                        obj_id = obj.get("id")
                        if obj_id:
                            subprocess.run(["pw-cli", "destroy", str(obj_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    elif n_name in existing_active_names:
                        # Duplicate node with identical name already tracked! Destroy duplicate to ensure strict 1:1 node cardinality
                        obj_id = obj.get("id")
                        if obj_id:
                            subprocess.run(["pw-cli", "destroy", str(obj_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        existing_active_names.add(n_name)
        except Exception:
            pass

        # 1. Provision any missing needed nodes
        nodes_created = False
        for node_name, node_tuple in needed_nodes.items():
            desc = node_tuple[0]
            media_class = node_tuple[1]
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

        self._apply_configured_system_defaults()

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
                    ch_type = ch.get("type", "sink")
                    if ch_type in ("app", "sink", "group") or (ch_type != "source" and not any(k in ch["id"].lower() for k in ("mic", "fefine", "microphone", "input", "capture"))):
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

        # 2. Cleanly terminate all submix and fallback loopback processes
        with self._lock:
            if hasattr(self, "_fallback_proc") and self._fallback_proc:
                try:
                    self._fallback_proc.terminate()
                    self._fallback_proc.wait(timeout=0.2)
                except Exception:
                    pass
                self._fallback_proc = None

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

        # 3. Destroy virtual mixing and per-channel ingestion sink nodes
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            phys_sink_id = None
            phys_source_id = None

            for obj in data:
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    media_class = props.get("media.class", "")
                    n_name = props.get("node.name", "")
                    obj_id = obj.get("id")

                    # Destroy WaveController virtual nodes so PipeWire falls back to physical devices
                    if n_name.startswith("WaveController_Channel_") or n_name.startswith("WaveController_") or n_name.startswith("input.WaveController_") or n_name.startswith("output.WaveController_"):
                        if obj_id:
                            subprocess.run(["pw-cli", "destroy", str(obj_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    elif media_class == "Audio/Sink" and (n_name.startswith("alsa_output.") or props.get("device.api") == "alsa"):
                        if not phys_sink_id or "elgato" in n_name.lower() or "wave" in n_name.lower():
                            phys_sink_id = obj_id
                    elif media_class == "Audio/Source" and (n_name.startswith("alsa_input.") or props.get("device.api") == "alsa"):
                        if not phys_source_id or "elgato" in n_name.lower() or "wave" in n_name.lower():
                            phys_source_id = obj_id

            # 4. Restore system default audio sink and source back to physical hardware
            if phys_sink_id:
                subprocess.run(["wpctl", "set-default", str(phys_sink_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if phys_source_id:
                subprocess.run(["wpctl", "set-default", str(phys_source_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # 5. Clear all target.object and target.node metadata bindings on active streams
            for obj in data:
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    if props.get("media.class") == "Stream/Output/Audio":
                        nid = str(obj["id"])
                        subprocess.run(["pw-metadata", "-n", "default", "-d", nid, "target.object"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        subprocess.run(["pw-metadata", "-n", "default", "-d", nid, "target.node"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def on_system_suspend(self):
        """Prepares PipeWire manager for system sleep/suspend."""
        log.info("[WaveController.PipeWire] System going to sleep: pausing volume guards...")
        self._is_sleeping = True

    def on_system_resume(self):
        """Restores all virtual nodes, channel master volumes, submix faders, and audio routing after system resume."""
        log.info("[WaveController.PipeWire] System resumed: restoring virtual nodes, volumes, and routing...")
        self._is_sleeping = False

        # 0. Wait for PipeWire daemon to be responsive before re-provisioning
        for attempt in range(20):
            try:
                subprocess.check_output(["pw-cli", "info", "0"], text=True, stderr=subprocess.DEVNULL, timeout=1.0)
                log.info(f"[WaveController.PipeWire] PipeWire daemon ready on attempt {attempt + 1}")
                break
            except Exception:
                time.sleep(0.15)

        # 1. Clear stale caches — PipeWire node IDs are invalid after suspend
        with self._lock:
            self._node_cache.clear()
            self._mix_node_ids_cache.clear()
            self._submix_node_ids.clear()
            self._last_cache_time = 0

        # 2. Re-provision virtual mix/channel sink/source nodes (may have been destroyed by PipeWire restart)
        self._ensure_virtual_mix_nodes()
        self._refresh_node_cache()

        # 2b. Re-bind system default sink/source since node ids are regenerated above
        self._apply_configured_system_defaults()

        # 3. Re-assert all Channel Master Volumes
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

        # 4. Re-assert all Submix Faders
        for ch_id, m_map in submix_states.items():
            for m_id, s_st in m_map.items():
                vol = s_st.get("volume", 80)
                muted = s_st.get("muted", False)
                self.set_channel_volume(ch_id, m_id, vol)
                if muted:
                    self.set_channel_mute(ch_id, m_id, True)

        # 5. Re-assert Mix Master Volumes
        for m_id, m_st in mix_states.items():
            vol = m_st.get("volume", 100)
            muted = m_st.get("muted", False)
            self.set_mix_master_volume(m_id, vol)
            if muted:
                self.set_mix_master_mute(m_id, True)

        # 6. Trigger volume event to dispatch to PipeWire nodes immediately
        with self._lock:
            self._volume_event.set()

        # 7. Re-synchronize channel audio routing
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
                if sync_tick % 4 == 0:
                    self._reconcile_app_streams_fast()
                if sync_tick % 20 == 0:
                    self._enforce_exclusive_volume_guard()
                    self._ensure_mix_sinks_unmuted()
                    self._sync_channel_audio_routing()
            except Exception:
                pass
            time.sleep(0.25) # 4 Hz source-volume poller; graph work is rate-limited above

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
                if "wavecontroller" in str(dev_key).lower():
                    continue
                is_elgato = dev.get("is_elgato", False) or "wave" in str(dev.get("name", "")).lower()
                if is_elgato:
                    if excl_out and dev.get("primary_sink_id"):
                        out_node_ids.add(str(dev["primary_sink_id"]))
                    for s in dev.get("sinks", []):
                        if excl_out and s.get("id") and "wavecontroller" not in str(s.get("name", "")).lower():
                            out_node_ids.add(str(s["id"]))

                    if excl_mic and dev.get("primary_source_id"):
                        in_node_ids.add(str(dev["primary_source_id"]))
                    for src in dev.get("sources", []):
                        if excl_mic and src.get("id") and "wavecontroller" not in str(src.get("name", "")).lower():
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
                    is_alsa = props.get("device.api") == "alsa" or n_name.startswith("alsa_")
                    is_elgato_hw = is_alsa and ("elgato" in n_name or "0fd9" in n_name or any(k in n_name for k in ("wave_xlr", "wave:3", "wave:1", "wave_neo")))
                    if is_elgato_hw:
                        if excl_out and media_class == "Audio/Sink":
                            out_node_ids.add(str(obj["id"]))
                        elif excl_mic and (media_class == "Audio/Source" or "source" in media_class.lower()):
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

    def _ensure_client_streams_unity_volume(self):
        """Ensures that client playback streams (Discord, Chrome, Spotify) are at 1.00 unity volume and unmuted."""
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for obj in data:
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    media_class = props.get("media.class", "")
                    n_name = props.get("node.name", "")
                    if media_class == "Stream/Output/Audio" and not n_name.startswith("output.WaveController_"):
                        nid = str(obj["id"])
                        try:
                            v_out = subprocess.check_output(["wpctl", "get-volume", nid], text=True, stderr=subprocess.DEVNULL).strip()
                            if "[MUTED]" in v_out or "Volume: 0.00" in v_out or "Volume: 0.0" in v_out:
                                self._dispatch_node_volume(nid, 1.00, False)
                        except Exception:
                            pass
        except Exception:
            pass

    def _get_match_tokens(self, name_or_id: str) -> set:
        """Generates normalized matching tokens for any application, process binary, or audio device."""
        return get_match_tokens(name_or_id)

    def _get_active_port_metadata_map(self) -> dict:
        """Extracts live process binary, application name, and node information for all PipeWire ports."""
        return get_active_port_metadata_map()

    def _port_matches_tokens(self, port_name: str, tokens: set, port_meta: dict = None) -> bool:
        """Checks if a PipeWire port belongs to an application or device matching any token."""
        return port_matches_tokens(port_name, tokens, port_meta)

    def _node_matches_tokens(self, props: dict, tokens: set) -> bool:
        """Checks if a PipeWire node's metadata matches target application tokens, prioritizing process binary."""
        if not props or not tokens:
            return False
        n_app = str(props.get("application.name", "")).lower()
        n_bin = str(props.get("application.process.binary", "")).lower()
        n_name = str(props.get("node.name", "")).lower()
        n_id = str(props.get("application.id", "")).lower()

        bin_file = n_bin.split("/")[-1].split("\\")[-1] if n_bin else ""
        if bin_file:
            return any(t in bin_file or bin_file == t or t in n_bin for t in tokens if len(t) >= 3)
        elif n_app and n_app not in ("chromium", "playback", "webrtc voiceengine", "webrtc_audio_sink"):
            return any(t in n_app or n_app == t for t in tokens if len(t) >= 3)
        else:
            return any(t in n_name or t in n_id for t in tokens if len(t) >= 3)

    def _bind_app_to_wireplumber_target(self, app_name: str, channel_id: str):
        """Notifies WirePlumber via PipeWire metadata that an application belongs to its dedicated channel sink."""
        try:
            if "fallback" in channel_id.lower():
                target_sink = "WaveController_Fallback_Sink"
            elif channel_id.startswith("WaveController_"):
                target_sink = channel_id
            elif self.is_channel_sink_exposed(channel_id):
                target_sink = f"WaveController_Channel_{channel_id}"
            else:
                target_sink = "WaveController_personal_Sink"
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            tokens = self._get_match_tokens(app_name)
            for obj in data:
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    if props.get("media.class") == "Stream/Output/Audio":
                        if self._node_matches_tokens(props, tokens):
                            nid = obj["id"]
                            if nid not in self._bound_stream_nodes:
                                subprocess.run(
                                    ["pw-metadata", "-n", "default", str(nid), "target.object", target_sink],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                                )
                                self._bound_stream_nodes.add(nid)
        except Exception:
            pass

    def _unbind_app_from_wireplumber_target(self, app_name: str, channel_id: str = None):
        """Clears WirePlumber target.object and target.node metadata for an application stream, resetting to unity volume."""
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            tokens = self._get_match_tokens(app_name)
            for obj in data:
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    if props.get("media.class") == "Stream/Output/Audio":
                        if self._node_matches_tokens(props, tokens):
                            nid = obj["id"]
                            self._bound_stream_nodes.discard(nid)
                            subprocess.run(
                                ["pw-metadata", "-n", "default", "-d", str(nid), "target.object"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                            )
                            subprocess.run(
                                ["pw-metadata", "-n", "default", "-d", str(nid), "target.node"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                            )
                            # Ensure stream is audible at unity gain
                            subprocess.run(["wpctl", "set-volume", str(nid), "1.00"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            subprocess.run(["wpctl", "set-mute", str(nid), "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _release_all_apps_to_system_default(self):
        """Releases all application streams from WaveController and routes them directly to the system-wide physical default sink."""
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            for obj in data:
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    if props.get("media.class") == "Stream/Output/Audio":
                        nid = str(obj["id"])
                        self._bound_stream_nodes.discard(obj["id"])
                        self._bound_unassigned_nodes.discard(obj["id"])
                        subprocess.run(["pw-metadata", "-n", "default", "-d", nid, "target.object"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        subprocess.run(["pw-metadata", "-n", "default", "-d", nid, "target.node"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        subprocess.run(["wpctl", "set-volume", nid, "1.00"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        subprocess.run(["wpctl", "set-mute", nid, "0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            fallback_in = self._get_default_sink_playback_ports()
            if fallback_in:
                out_ports_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
                out_ports = [l.strip() for l in out_ports_raw.splitlines() if l.strip()]
                app_ports = []
                for p in out_ports:
                    if p.startswith("WaveController_") or p.startswith("output.WaveController_") or p.startswith("alsa_") or p.startswith("wave_"):
                        continue
                    if ":output_" in p:
                        app_ports.append(p)
                if app_ports:
                    self._link_stereo_ports(app_ports, fallback_in, unlink=False)
        except Exception:
            pass

    def _get_system_default_sink_name(self) -> str:
        """Returns the system default audio sink node name configured in PipeWire/WirePlumber."""
        try:
            out = subprocess.check_output(["pw-metadata", "-n", "default", "0", "default.audio.sink"], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if "key:'default.audio.sink'" in line and "value:'" in line:
                    val_str = line.split("value:'")[1].split("'")[0]
                    parsed = json.loads(val_str)
                    if isinstance(parsed, dict) and parsed.get("name"):
                        return parsed["name"]
        except Exception:
            pass
        return ""

    def _get_fallback_sink_playback_ports(self) -> list:
        """Finds input/playback ports of WaveController_Fallback_Sink."""
        try:
            in_ports_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
            in_ports = [l.strip() for l in in_ports_raw.splitlines() if l.strip()]
            matched = [p for p in in_ports if p.startswith("WaveController_Fallback_Sink:") and (":playback_" in p or ":input_" in p)]
            if matched:
                return matched[:2]
        except Exception:
            pass
        return []

    def _get_default_sink_playback_ports(self) -> list:
        """Finds input/playback ports for fallback/unassigned audio routing (System Default Physical Sink)."""
        sys_def = self._get_system_default_sink_name()
        try:
            in_ports_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
            in_ports = [l.strip() for l in in_ports_raw.splitlines() if l.strip()]
            if sys_def:
                sys_matched = [p for p in in_ports if p.startswith(f"{sys_def}:") and (":playback_" in p or ":input_" in p)]
                if sys_matched:
                    return sys_matched[:2]

            target_device = getattr(self, "selected_monitor_device", None) or ""
            clean_target = target_device.replace("alsa_card.", "").replace("alsa_output.", "").strip().lower()
            if clean_target and clean_target != "none" and "wavecontroller" not in clean_target:
                phys_matched = [p for p in in_ports if p.startswith("alsa_output.") and (":playback_" in p or ":input_" in p) and clean_target in p.lower()]
                if phys_matched:
                    return phys_matched[:2]

            fb_ports = self._get_fallback_sink_playback_ports()
            if fb_ports:
                return fb_ports

            return [p for p in in_ports if p.startswith("alsa_output.") and (":playback_" in p or ":input_" in p)][:2]
        except Exception:
            return []

    def _sync_unassigned_app_streams(self, out_ports=None, in_ports=None, links_map=None, port_meta=None):
        """Routes unassigned application streams to the configured physical output device (fallback).

        An 'unassigned' stream is any Stream/Output/Audio PipeWire node that does NOT belong
        to any channel's assigned_apps list. WirePlumber sets target.node=-1 for such streams
        when it cannot find a valid default sink (because WaveController_personal_Sink has
        replaced it). This method explicitly binds those streams to the physical hardware output
        via pw-metadata target.object and pw-link, overriding the -1 inhibitor.
        """
        try:
            # 1. Build the complete set of match tokens for ALL assigned apps across ALL channels
            with self._lock:
                all_assigned_apps = []
                for apps in self.assigned_apps.values():
                    all_assigned_apps.extend(apps)

            assigned_tokens_list = [self._get_match_tokens(a) for a in all_assigned_apps]

            # 2. Resolve the physical output: node name (for WirePlumber) and playback ports (for pw-link)
            phys_ports = self._get_default_sink_playback_ports()
            if not phys_ports:
                return

            # Determine the physical sink node name — MUST be an alsa_output.* hardware node,
            # never a WaveController virtual sink (which would create a silent routing loop).
            phys_node_name = None
            for p in phys_ports:
                clean_p = re.sub(r'^\d+\s+', '', p).strip()
                if clean_p.startswith("alsa_output.") and ":" in clean_p:
                    phys_node_name = clean_p.split(":")[0]
                    break

            # If _get_default_sink_playback_ports returned virtual/non-hardware ports,
            # fall back to scanning pw-dump for the configured monitor device directly.
            if not phys_node_name:
                with self._lock:
                    mon_dev = getattr(self, "selected_monitor_device", None) or ""
                try:
                    fallback_data = json.loads(subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL))
                    for _fb_obj in fallback_data:
                        if _fb_obj.get("type") != "PipeWire:Interface:Node":
                            continue
                        _fb_props = _fb_obj.get("info", {}).get("props", {})
                        _fb_name = _fb_props.get("node.name", "")
                        if _fb_name.startswith("alsa_output.") and _fb_props.get("media.class") == "Audio/Sink":
                            if not mon_dev or mon_dev.replace("alsa_output.", "").split(".")[0].lower() in _fb_name.lower():
                                phys_node_name = _fb_name
                                break
                except Exception:
                    pass

            if not phys_node_name:
                return

            # Refresh phys_ports to ensure they are from the resolved physical node
            try:
                all_in_ports_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
                all_in_ports = [l.strip() for l in all_in_ports_raw.splitlines() if l.strip()]
                phys_ports = [p for p in all_in_ports if p.startswith(f"{phys_node_name}:") and ":playback_" in p]
            except Exception:
                pass

            if not phys_ports:
                return

            # Separate FL/FR playback ports
            phys_fl = [p for p in phys_ports if any(s in p.lower().split(":")[-1] for s in ("_fl", "playback_0", "playback_fl"))]
            phys_fr = [p for p in phys_ports if any(s in p.lower().split(":")[-1] for s in ("_fr", "playback_1", "playback_fr"))]

            # 3. Enumerate all live Stream/Output/Audio nodes from pw-dump
            try:
                pw_data = json.loads(subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL))
            except Exception:
                return

            if not hasattr(self, "_bound_unassigned_nodes"):
                self._bound_unassigned_nodes = set()

            # Known system/compositor binaries that should NOT be forcibly routed
            _SYSTEM_STREAM_SKIP = frozenset({
                "mutter", "gnome-shell", "gnome-session", "gnome-settings-daemon",
                "pulseaudio", "pipewire-pulse", "pipewire-media-session", "wireplumber",
                "pavucontrol", "easyeffects", "carla", "qjackctl",
                "gst-launch", "gst-plugin-scanner",
            })

            for obj in pw_data:
                if obj.get("type") != "PipeWire:Interface:Node":
                    continue
                props = obj.get("info", {}).get("props", {})
                if props.get("media.class") != "Stream/Output/Audio":
                    continue

                node_name = props.get("node.name", "")
                app_name = props.get("application.name", "")
                binary = props.get("application.process.binary", "")

                # Skip all WaveController-internal nodes
                if (node_name.startswith("WaveController_") or
                        node_name.startswith("output.WaveController_") or
                        node_name.startswith("input.WaveController_") or
                        node_name.startswith("wave_")):
                    continue

                # Skip known system/compositor streams that should not be force-routed
                binary_low = binary.lower() if binary else ""
                node_low = node_name.lower()
                app_low = app_name.lower() if app_name else ""
                if any(s in binary_low or s in node_low or s in app_low for s in _SYSTEM_STREAM_SKIP):
                    continue

                # Skip streams with system-level media roles (notification, event, etc.)
                media_role = props.get("media.role", "").lower()
                if media_role in ("event", "notification", "phone", "animation", "a11y"):
                    continue

                nid = obj["id"]

                # 4. Check if this node matches any assigned app
                is_assigned = False
                for tok_set in assigned_tokens_list:
                    if self._node_matches_tokens(props, tok_set):
                        is_assigned = True
                        break

                if is_assigned:
                    # If the app is now assigned, remove it from unassigned tracking so it can be
                    # re-bound to the fallback in a future cycle if it becomes unassigned again.
                    self._bound_unassigned_nodes.discard(nid)
                    continue

                # 5. This is a genuinely unassigned stream — bind it to the physical output
                if nid not in self._bound_unassigned_nodes:
                    # Assert WirePlumber target.object → physical hardware sink
                    # This overrides the -1 inhibitor WirePlumber sets when it can't find the default sink
                    subprocess.run(
                        ["pw-metadata", "-n", "default", str(nid), "target.object", phys_node_name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    self._bound_unassigned_nodes.add(nid)
                    log.info(f"[WaveController.PipeWire] Unassigned stream '{app_name or node_name}' (id={nid}) "
                             f"bound to physical output '{phys_node_name}' via fallback routing.")

                # 6. Belt-and-suspenders: also pw-link directly to physical FL/FR ports
                # Resolve this node's output ports from the provided out_ports list
                app_fl_ports = []
                app_fr_ports = []
                if out_ports:
                    for p in out_ports:
                        clean_p = re.sub(r'^\d+\s+', '', p).strip()
                        if clean_p.startswith("WaveController_") or clean_p.startswith("output.WaveController_") or clean_p.startswith("wave_") or clean_p.startswith("alsa_"):
                            continue
                        # Match port to this specific node by node_name prefix
                        if not (clean_p.startswith(f"{node_name}:") or (app_name and clean_p.startswith(f"{app_name}:"))):
                            continue
                        p_low = clean_p.split(":")[-1].lower()
                        if any(s in p_low for s in ("_fl", "output_fl", "playback_fl", "output_0", "_l")):
                            app_fl_ports.append(clean_p)
                        elif any(s in p_low for s in ("_fr", "output_fr", "playback_fr", "output_1", "_r")):
                            app_fr_ports.append(clean_p)

                # Resolve current links for these ports to avoid redundant pw-link calls
                cur_links = links_map or {}
                for src_port in app_fl_ports:
                    existing_dests = {re.sub(r'^\d+\s+', '', d).strip() for d in cur_links.get(src_port, set())}
                    for dest in phys_fl:
                        clean_dest = re.sub(r'^\d+\s+', '', dest).strip()
                        if clean_dest not in existing_dests:
                            subprocess.run(["pw-link", src_port, clean_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                for src_port in app_fr_ports:
                    existing_dests = {re.sub(r'^\d+\s+', '', d).strip() for d in cur_links.get(src_port, set())}
                    for dest in phys_fr:
                        clean_dest = re.sub(r'^\d+\s+', '', dest).strip()
                        if clean_dest not in existing_dests:
                            subprocess.run(["pw-link", src_port, clean_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        except Exception:
            pass

    def _reconcile_app_streams_fast(self):
        """Ultra-fast reactive stream interceptor ensuring assigned apps
        are immediately attached to their active submix faders and severed from default mix leaks."""
        with self._lock:
            has_assigned = any(bool(apps) for apps in self.assigned_apps.values())
        if not has_assigned:
            return

        try:
            try:
                o_raw = subprocess.check_output(["pw-link", "-I", "-o"], text=True, stderr=subprocess.DEVNULL)
            except Exception:
                o_raw = ""
            if not o_raw:
                o_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
            out_ports = [l.strip() for l in o_raw.splitlines() if l.strip()]

            # Quick filter for candidate non-WaveController application output ports
            app_ports = []
            for p in out_ports:
                clean_p = re.sub(r'^\d+\s+', '', p).strip()
                if clean_p.startswith("WaveController_") or clean_p.startswith("output.WaveController_") or clean_p.startswith("alsa_") or clean_p.startswith("wave_"):
                    continue
                if ":output_" in clean_p or ":playback_" in clean_p or ":monitor_" in clean_p:
                    app_ports.append(p)

            if not app_ports:
                return

            with self._lock:
                channels_copy = list(self.channels)

            links_map = self._get_pw_links_map()
            port_meta = self._get_active_port_metadata_map()

            reconciled_any = False
            for ch in channels_copy:
                if ch.get("type") == "source":
                    continue
                ch_id = ch["id"]
                assigned = self.get_assigned_apps(ch_id)
                if not assigned:
                    continue

                for app in assigned:
                    tokens = self._get_match_tokens(app)
                    matched_ports = [p for p in app_ports if self._port_matches_tokens(p, tokens, port_meta) and ":output_" in p]
                    if not matched_ports:
                        continue

                    need_sync = False
                    channel_sink_prefix = f"WaveController_Channel_{ch_id}:"
                    submix_prefix = f"input.WaveController_submix_{ch_id}_"
                    has_active_sink_mix = any(m.get("type") == "sink" or m.get("id") in ("personal", "personal_mix") for m in self.mixes)
                    has_enabled_submixes = any(self.is_channel_mix_enabled(ch_id, m["id"]) for m in self.mixes) or self.is_channel_sink_exposed(ch_id)
                    for sp in matched_ports:
                        clean_sp = re.sub(r'^\d+\s+', '', sp).strip()
                        sp_id = sp.split()[0] if sp and sp.split()[0].isdigit() else None
                        connected_dests = set(links_map.get(sp, set()))
                        if clean_sp:
                            connected_dests.update(links_map.get(clean_sp, set()))
                        if sp_id:
                            connected_dests.update(links_map.get(sp_id, set()))

                        sp_target = sp_id if sp_id else clean_sp
                        # 1. Immediately sever direct leaks to physical hardware or unauthorized mix sinks
                        for dp in connected_dests:
                            dp_clean = re.sub(r'^\d+\s+', '', dp).strip()
                            if not dp_clean or dp_clean.isdigit():
                                continue
                            is_auth = dp_clean.startswith(channel_sink_prefix) or dp_clean.startswith(submix_prefix) or dp_clean.startswith("wave_meter_") or (not has_active_sink_mix and dp_clean.startswith("alsa_output."))
                            if not is_auth:
                                dp_target = dp.split()[0] if dp and dp.split()[0].isdigit() else dp
                                try:
                                    subprocess.run(["pw-link", "-d", sp_target, dp_target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                except Exception:
                                    pass
                                need_sync = True

                        # 2. Verify app is cleanly attached to its pre-fader channel ingestion sink or active submixes
                        if has_enabled_submixes:
                            is_attached = any(re.sub(r'^\d+\s+', '', dp).strip().startswith(channel_sink_prefix) or re.sub(r'^\d+\s+', '', dp).strip().startswith(submix_prefix) for dp in connected_dests)
                            if not is_attached:
                                need_sync = True

                    if need_sync:
                        self._sync_channel_audio_routing(channel_id=ch_id)
                        self._bind_app_to_wireplumber_target(app, ch_id)
                        reconciled_any = True

            if reconciled_any:
                self._notify_peak_monitor_refresh()

            # Reconcile unassigned application streams to WaveController_Fallback_Sink
            self._sync_unassigned_app_streams(out_ports=out_ports, links_map=links_map, port_meta=port_meta)
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
                            if any(x in name_lower for x in ["facecam", "cam", "video", "virtual", "null", "wavecontroller", "submix", "loopback", "wave_sink", "wave_mic"]):
                                continue
                            if in_sinks:
                                sinks.append({"id": node_id, "name": name_part, "is_default": is_def})
                            elif in_sources:
                                sources.append({"id": node_id, "name": name_part, "is_default": is_def})
            
            with self._lock:
                self.output_devices = sinks
                if sinks and (not self.selected_monitor_device or "wavecontroller" in str(self.selected_monitor_device).lower()):
                    for s in sinks:
                        if any(k in s["name"].lower() for k in ("wave", "elgato")):
                            self.selected_monitor_device = s["name"]
                            break
                    if not self.selected_monitor_device or "wavecontroller" in str(self.selected_monitor_device).lower():
                        for s in sinks:
                            if s.get("is_default"):
                                self.selected_monitor_device = s["name"]
                                break
                    if (not self.selected_monitor_device or "wavecontroller" in str(self.selected_monitor_device).lower()) and sinks:
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

    KNOWN_AUDIO_BINARIES = KNOWN_AUDIO_BINARIES

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
                portal_app_id = props.get("pipewire.access.portal.app_id") or props.get("application.id") or ""
                
                # Only include genuine client audio playback streams (not sinks, sources, DSP nodes, or internal loopbacks)
                media_role = props.get("media.role", "")
                if media_class == "Stream/Output/Audio" and media_role != "DSP":
                    name = props.get("application.name") or props.get("node.description") or props.get("media.name") or node_name
                    binary = props.get("application.process.binary", "")
                    icon = props.get("application.icon-name") or props.get("application.icon_name")
                    node_id = obj.get("id")
                    
                    if not name:
                        continue
                    name_low = str(name).lower().strip()
                    bin_low = str(binary).lower().strip()
                    node_low = str(node_name).lower().strip()
                    app_id_low = str(app_id).lower().strip()
                    portal_low = str(portal_app_id).lower().strip()
                    
                    # Exclude internal virtual submixes, loopbacks, meters, and system utilities
                    internal_keywords = [
                        "wavecontroller", "submix", "loopback", "wave_sink", "wave_mic",
                        "vcp_monitor", "pw-record", "parecord", "pipewire", "wireplumber",
                        "easyeffects", "wpctl", "system_capture", "system capture",
                        "speech-dispatcher", "null-sink", "pw-loopback", "monitor",
                        "pavucontrol", "org.pulseaudio.pavucontrol", "libremidi", "midi-bridge", "bluez_midi"
                    ]
                    if any(kw in name_low or kw in bin_low or kw in node_low or kw in app_id_low or kw in portal_low for kw in internal_keywords):
                        continue

                    # For Electron, Flatpak, and WebRTC streams, map process binary, app id, or portal app ID to human-readable application name
                    bin_file = bin_low.split("/")[-1].split("\\")[-1] if bin_low else ""
                    matched_entry = None

                    if bin_file in KNOWN_AUDIO_BINARIES:
                        matched_entry = KNOWN_AUDIO_BINARIES[bin_file]
                    elif portal_low in KNOWN_AUDIO_BINARIES:
                        matched_entry = KNOWN_AUDIO_BINARIES[portal_low]
                    elif app_id_low in KNOWN_AUDIO_BINARIES:
                        matched_entry = KNOWN_AUDIO_BINARIES[app_id_low]
                    elif any(d in portal_low or d in bin_low or d in app_id_low for d in ("discord", "vesktop", "webcord")):
                        matched_entry = ("Discord", "discord")
                    elif any(s in portal_low or s in bin_low or s in app_id_low for s in ("spotify",)):
                        matched_entry = ("Spotify", "spotify")

                    if matched_entry:
                        known_name, known_icon = matched_entry
                        if name_low in ("chromium", "webrtc voiceengine", "webrtc", "electron", "playback", "chromium input", "chromium output") or not name or name_low == bin_file or name_low == portal_low:
                            name = known_name
                        icon = known_icon

                    # If icon is missing or generic (e.g. chromium on Electron apps), resolve from app name, portal ID, and binary
                    if not icon or (icon in ("chromium", "google-chrome", "audio-x-generic", "audio-x-generic-symbolic", "application-default-icon") and "chrome" not in name_low and "chromium" not in name_low):
                        if portal_app_id:
                            resolved_portal = self.resolve_icon_for_app(portal_app_id)
                            if resolved_portal and resolved_portal not in ("audio-x-generic-symbolic", "audio-card-symbolic"):
                                icon = resolved_portal
                        if not icon or icon in ("audio-x-generic-symbolic", "audio-card-symbolic"):
                            resolved = self.resolve_icon_for_app(name)
                            if resolved and resolved not in ("audio-x-generic-symbolic", "audio-card-symbolic"):
                                icon = resolved
                            elif bin_file:
                                resolved_bin = self.resolve_icon_for_app(bin_file)
                                if resolved_bin and resolved_bin not in ("audio-x-generic-symbolic", "audio-card-symbolic"):
                                    icon = resolved_bin

                    if not icon:
                        if portal_app_id:
                            icon = self.resolve_icon_for_app(portal_app_id)
                        if not icon or icon in ("audio-x-generic-symbolic", "audio-card-symbolic"):
                            icon = self.resolve_icon_for_app(name)

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
                        comm = ""
                        comm_file = os.path.join("/proc", proc_entry, "comm")
                        if os.path.exists(comm_file):
                            with open(comm_file, "r") as f:
                                comm = f.read().strip().lower()

                        cmdline = ""
                        cmd_file = os.path.join("/proc", proc_entry, "cmdline")
                        if os.path.exists(cmd_file):
                            with open(cmd_file, "rb") as f:
                                cmdline = f.read().replace(b'\x00', b' ').decode('utf-8', errors='ignore').lower()

                        matched_key = None
                        if comm in KNOWN_AUDIO_BINARIES:
                            matched_key = comm
                        else:
                            for k in KNOWN_AUDIO_BINARIES:
                                if len(k) >= 4 and (f"/{k}" in cmdline or f"app/{k}" in cmdline or f"bin/{k}" in cmdline or k in comm):
                                    matched_key = k
                                    break

                        if matched_key:
                            app_title, app_icon = KNOWN_AUDIO_BINARIES[matched_key]
                            if app_title not in seen and app_title.lower() not in seen:
                                seen.add(app_title)
                                seen.add(app_title.lower())
                                apps.append({
                                    "id": None,
                                    "name": app_title,
                                    "binary": comm or matched_key,
                                    "icon": app_icon or self.resolve_icon_for_app(app_title)
                                })
                    except Exception:
                        pass
        except Exception:
            pass

        return apps

    def get_detected_apps(self) -> list:
        """Discovers running audio applications from active PipeWire streams and desktop processes."""
        return self.get_active_application_streams()

    def assign_app_to_channel(self, channel_id: str, app_name: str):
        with self._lock:
            for ch, apps in self.assigned_apps.items():
                if app_name in apps:
                    apps.remove(app_name)
            if channel_id in self.assigned_apps:
                if app_name not in self.assigned_apps[channel_id]:
                    self.assigned_apps[channel_id].append(app_name)
            else:
                self.assigned_apps[channel_id] = [app_name]
                
            self._save_state_to_config(immediate=True)
            self._refresh_node_cache()
            self._notify_peak_monitor_refresh()

        def _bg():
            # 0. Force-unmute the application and clear any drawer volume overrides
            canon_app = str(app_name).lower().strip()
            with self._lock:
                if hasattr(self, "_app_volume_overrides"):
                    self._app_volume_overrides.pop(canon_app, None)
            try:
                tokens = self._get_match_tokens(app_name)
                out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
                for obj in json.loads(out):
                    if obj.get("type") == "PipeWire:Interface:Node":
                        props = obj.get("info", {}).get("props", {})
                        if self._node_matches_tokens(props, tokens):
                            nid = str(obj["id"])
                            self._dispatch_node_volume(nid, 1.00, False)
            except Exception:
                pass

            # Sever any existing links from this app to Fallback or direct mix sinks
            try:
                tokens = self._get_match_tokens(app_name)
                port_meta = self._get_active_port_metadata_map()
                links_map = self._get_pw_links_map()
                for src_p, dests in links_map.items():
                    if ":output_" in src_p and self._port_matches_tokens(src_p, tokens, port_meta):
                        for dest_p in dests:
                            if dest_p.startswith("WaveController_Fallback_Sink:") or "WaveController_fallback" in dest_p or dest_p.startswith("WaveController_personal_mix_Sink:"):
                                try:
                                    subprocess.run(["pw-link", "-d", src_p, dest_p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                except Exception:
                                    pass
            except Exception:
                pass

            self._sync_channel_audio_routing(channel_id=channel_id)
            self._bind_app_to_wireplumber_target(app_name, channel_id)
            self._notify_peak_monitor_refresh()

        threading.Thread(target=_bg, daemon=True).start()

    def unassign_app_from_channel(self, channel_id: str, app_name: str):
        with self._lock:
            if channel_id in self.assigned_apps:
                if app_name in self.assigned_apps[channel_id]:
                    self.assigned_apps[channel_id].remove(app_name)
            self._save_state_to_config(immediate=True)
            self._refresh_node_cache()
            self._notify_peak_monitor_refresh()

        # Synchronous fast sever to prevent UI race conditions with get_channel_all_apps
        app_out_ports = []
        try:
            tokens = self._get_match_tokens(app_name)
            port_meta = self._get_active_port_metadata_map()
            links_map = self._get_pw_links_map()
            for src_p, dests in links_map.items():
                if ":output_" in src_p and self._port_matches_tokens(src_p, tokens, port_meta):
                    app_out_ports.append(src_p)
                    for dest_p in dests:
                        if (
                            f"input.WaveController_submix_{channel_id}_" in dest_p or
                            dest_p.startswith(f"WaveController_Channel_{channel_id}:") or
                            dest_p.startswith("WaveController_personal_mix_Sink:")
                        ):
                            try:
                                subprocess.run(["pw-link", "-d", src_p, dest_p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            except Exception:
                                pass
        except Exception:
            pass

        def _bg():
            # 0. Force-unmute the application and clear any drawer volume overrides
            canon_app = str(app_name).lower().strip()
            with self._lock:
                if hasattr(self, "_app_volume_overrides"):
                    self._app_volume_overrides.pop(canon_app, None)
            try:
                tokens = self._get_match_tokens(app_name)
                out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
                for obj in json.loads(out):
                    if obj.get("type") == "PipeWire:Interface:Node":
                        props = obj.get("info", {}).get("props", {})
                        if self._node_matches_tokens(props, tokens):
                            nid = str(obj["id"])
                            self._dispatch_node_volume(nid, 1.00, False)
            except Exception:
                pass

            # 1. Clear WirePlumber target binding for this channel
            self._unbind_app_from_wireplumber_target(app_name, channel_id)

            # 2. Resync channel routing
            self._sync_channel_audio_routing(channel_id=channel_id)

            # 3. Immediately route this app to the physical fallback output — do not wait for
            #    the next reconcile cycle.  Without this, WirePlumber re-asserts target.node=-1
            #    and the stream floats silently until the watchdog fires.
            self._sync_unassigned_app_streams()
            self._notify_peak_monitor_refresh()

        threading.Thread(target=_bg, daemon=True).start()

    def is_channel_sink_exposed(self, channel_id: str) -> bool:
        with self._lock:
            ch = next((c for c in self.channels if c["id"] == channel_id), None)
            return bool(ch.get("expose_sink", False)) if ch else False

    def set_channel_sink_exposed(self, channel_id: str, exposed: bool):
        with self._lock:
            ch = next((c for c in self.channels if c["id"] == channel_id), None)
            if ch:
                ch["expose_sink"] = bool(exposed)
                self._save_state_to_config(immediate=True)
                self._notify_peak_monitor_refresh()

        def _bg():
            self._ensure_virtual_mix_nodes()
            self._refresh_node_cache()
            self._sync_channel_audio_routing(channel_id=channel_id)
            self._notify_peak_monitor_refresh()

        threading.Thread(target=_bg, daemon=True).start()

    def get_channel_connected_apps(self, channel_id: str) -> list:
        """
        Returns all live audio client streams actively linked to this channel's virtual ingestion sink.
        Excludes WaveController internal nodes, submix loopbacks, and telemetry taps.
        """
        if not self.is_channel_sink_exposed(channel_id):
            return []

        connected_apps = []
        try:
            links_map = self._get_pw_links_map()
            port_meta = self._get_active_port_metadata_map()
            target_prefix = f"WaveController_Channel_{channel_id}:playback_"
            
            # Seed seen set with manually assigned apps to avoid duplicate entries
            manually_assigned = {str(a).strip().lower() for a in self.get_assigned_apps(channel_id)}
            seen_app_names = set(manually_assigned)

            for src_port, dests in links_map.items():
                # 1. Skip pure numeric port IDs (e.g. "121", "90")
                if not src_port or src_port.isdigit():
                    continue

                # 2. Strip numeric prefix from pw-link -I output (e.g. "121 Shortwave:output_FL" -> "Shortwave:output_FL")
                clean_src = re.sub(r'^\d+\s+', '', src_port.strip())
                if not clean_src or clean_src.isdigit():
                    continue

                if any(d.startswith(target_prefix) or re.sub(r'^\d+\s+', '', d.strip()).startswith(target_prefix) for d in dests):
                    if clean_src.startswith("WaveController_") or clean_src.startswith("output.WaveController_") or clean_src.startswith("wave_"):
                        continue

                    meta = port_meta.get(clean_src) or port_meta.get(clean_src.lower()) or port_meta.get(src_port) or {}
                    app_name = meta.get("app_name")
                    if not app_name:
                        bin_raw = meta.get("binary", "")
                        if bin_raw:
                            bin_file = bin_raw.split("/")[-1].split("\\")[-1]
                            if bin_file in KNOWN_AUDIO_BINARIES:
                                app_name = KNOWN_AUDIO_BINARIES[bin_file][0]
                            else:
                                app_name = bin_file
                    if not app_name:
                        node_raw = meta.get("node_name") or clean_src.split(":")[0]
                        node_clean = re.sub(r'^\d+\s+', '', node_raw.strip())
                        if node_clean.lower() in KNOWN_AUDIO_BINARIES:
                            app_name = KNOWN_AUDIO_BINARIES[node_clean.lower()][0]
                        else:
                            app_name = node_clean

                    # Do not include raw numeric strings or hardware ALSA ports
                    if not app_name or app_name.isdigit() or app_name.lower().startswith("alsa_"):
                        continue

                    canon = str(app_name).strip().lower()
                    if canon and canon not in seen_app_names:
                        seen_app_names.add(canon)
                        connected_apps.append({
                            "name": app_name,
                            "binary": canon,
                            "icon": self.resolve_icon_for_app(app_name),
                            "source": "virtual_sink"
                        })
        except Exception:
            pass
        return connected_apps

    def get_channel_all_apps(self, channel_id: str) -> list:
        """
        Returns all applications assigned to this channel (both manually assigned in UI and
        dynamically connected via the system virtual sink).
        """
        all_apps = []
        seen = set()

        # 1. Manually assigned apps in WaveController
        assigned = self.get_assigned_apps(channel_id)
        for app in assigned:
            app_clean = str(app).strip()
            if app_clean and not app_clean.isdigit() and app_clean.lower() not in seen and not app_clean.startswith("usb-") and not app_clean.startswith("alsa_card.") and not app_clean.startswith("alsa_"):
                seen.add(app_clean.lower())
                all_apps.append({
                    "name": app_clean,
                    "binary": app_clean.lower(),
                    "icon": self.resolve_icon_for_app(app_clean),
                    "source": "manual"
                })

        # 2. Dynamically connected apps through virtual sink (Way 2)
        if self.is_channel_sink_exposed(channel_id):
            connected = self.get_channel_connected_apps(channel_id)
            for c in connected:
                c_name = str(c.get("name", "")).strip()
                if not c_name or c_name.isdigit():
                    continue
                c_name_low = c_name.lower()
                if c_name_low not in seen:
                    seen.add(c_name_low)
                    all_apps.append(c)

        return all_apps

    def get_assigned_apps(self, channel_id: str) -> list:
        with self._lock:
            if not any(c.get("id") == channel_id for c in self.channels):
                return []
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
            if hasattr(self, "hardware_mgr") and self.hardware_mgr:
                ch_obj = next((c for c in self.channels if c["id"] == channel_id), None)
                is_mic = (
                    channel_id in ("mic", "elgato_wave_xlr") or
                    (ch_obj and ch_obj.get("type") in ("source", "hardware")) or
                    any(k in channel_id.lower() for k in ("mic", "fefine", "fifine", "wave", "elgato", "capture", "input"))
                )
                if is_mic and getattr(self.hardware_mgr, "is_elgato", False):
                    gain_db = getattr(self.hardware_mgr, "hardware_gain_db", None)
                    if gain_db is not None:
                        return max(0, min(100, int(round((gain_db / 75.0) * 100))))
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

            ch_obj = next((c for c in self.channels if c["id"] == channel_id), None)
            is_mic = bool(ch_obj and ch_obj.get("type") in ("source", "hardware") or channel_id in ("mic", "elgato_wave_xlr"))

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

            # Sync physical Elgato hardware mute if this is a genuine hardware mic channel
            ch_obj = next((c for c in self.channels if c["id"] == channel_id), None)
            is_hw_mic = bool(ch_obj and ch_obj.get("type") in ("source", "hardware") and (
                channel_id in ("mic", "elgato_wave_xlr") or
                channel_id.startswith("elgato_wave") or
                "wave_xlr" in channel_id.lower() or
                "wave_3" in channel_id.lower() or
                "wave_1" in channel_id.lower() or
                "wave_neo" in channel_id.lower()
            ))
            if self.hardware_mgr and is_hw_mic:
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

            # Determine hardware mic status while holding lock
            ch_obj = next((c for c in self.channels if c["id"] == channel_id), None)
            is_hw_mic = bool(ch_obj and ch_obj.get("type") in ("source", "hardware") and (
                channel_id in ("mic", "elgato_wave_xlr") or
                channel_id.startswith("elgato_wave") or
                "wave_xlr" in channel_id.lower() or
                "wave_3" in channel_id.lower() or
                "wave_1" in channel_id.lower() or
                "wave_neo" in channel_id.lower()
            ))

        # Heavy operations run outside the lock in a background thread to avoid blocking UI
        def _bg_sync():
            self._sync_channel_audio_routing(channel_id)
            # Sync physical Elgato hardware mute if this is a genuine hardware mic channel
            if self.hardware_mgr and is_hw_mic:
                self.hardware_mgr.set_mode_mute("gain", new_mute, transient=True)

        threading.Thread(target=_bg_sync, daemon=True).start()

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
        is_mic = False
        with self._lock:
            ch_obj = next((c for c in self.channels if c["id"] == channel_id), None)
            if ch_obj and ch_obj.get("type") in ("source", "hardware") or channel_id in ("mic", "elgato_wave_xlr"):
                is_mic = True

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

                ch_obj = None
                with self._lock:
                    ch_obj = next((c for c in self.channels if c["id"] == channel_id), None)
                ch_name = ch_obj.get("name", "") if ch_obj else ""

                is_mic_channel = (
                    channel_id in ("mic", "elgato_wave_xlr") or
                    channel_id.startswith("elgato_wave") or
                    "wave_xlr" in channel_id.lower() or
                    (ch_obj and ch_obj.get("type") in ("source", "hardware")) or
                    any(k in channel_id.lower() for k in ("mic", "fefine", "microphone"))
                )

                if is_mic_channel:
                    linear_frac = max(0.0, min(1.0, float(volume_pct) / 100.0))
                    last_v, last_m = getattr(self, "_last_mic_dispatch", (-1.0, None))
                    if abs(last_v - linear_frac) > 0.001 or last_m != is_muted:
                        self._last_mic_dispatch = (linear_frac, is_muted)
                        target_source_id = "@DEFAULT_AUDIO_SOURCE@"
                        def_in = getattr(self, "default_input_device", "")
                        clean_def = def_in.replace("alsa_card.", "").replace("alsa_input.", "").strip().lower()
                        ch_tokens = self._get_match_tokens(channel_id)
                        if ch_obj:
                            ch_tokens.update(self._get_match_tokens(ch_obj.get("name", "")))
                        for a in self.get_assigned_apps(channel_id):
                            ch_tokens.update(self._get_match_tokens(a))

                        try:
                            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
                            for obj in json.loads(out):
                                if obj.get("type") == "PipeWire:Interface:Node":
                                    props = obj.get("info", {}).get("props", {})
                                    m_class = props.get("media.class", "")
                                    n_name = props.get("node.name", "").lower()
                                    d_name = props.get("node.description", "").lower()
                                    if m_class == "Audio/Source" and (n_name.startswith("alsa_input.") or props.get("device.api") == "alsa"):
                                        if clean_def and clean_def in n_name:
                                            target_source_id = str(obj["id"])
                                            break
                                        if any(t in n_name or t in d_name for t in ch_tokens if t not in ("mic", "microphone", "input", "source")):
                                            target_source_id = str(obj["id"])
                                            break
                        except Exception:
                            pass
                        is_hw_elgato = hasattr(self, "hardware_mgr") and getattr(self.hardware_mgr, "is_elgato", False)
                        dispatch_vol = 1.00 if is_hw_elgato else linear_frac
                        self._dispatch_node_volume(target_source_id, dispatch_vol, is_muted)
                    continue

                assigned_app_names = self.get_assigned_apps(channel_id)

                # Virtual playback sinks (WaveController_Channel_<ch>):
                # - When linked: virtual ingestion sink remains at unity (1.00) while submix loopback faders scale in lockstep.
                # - When unlinked: apply master channel volume directly to the virtual ingestion sink as pre-fader channel attenuation.
                is_virtual_sink = any(c.get("id") == channel_id and c.get("type") in ("sink", "group", "app") for c in self.channels)
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
                    is_source = (ch_type == "source") or (ch_type not in ("app", "sink", "group") and any(k in channel_id.lower() for k in ("mic", "fefine", "fifine", "microphone", "input", "capture", "mobo")))
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
        """Helper to establish or destroy stereo/mono PipeWire link connections accurately using port names or integer IDs."""
        if not src_ports or not dst_ports:
            return
        for src_p in src_ports:
            is_fl = "_fl" in src_p.lower() or "_1" in src_p or "_mono" in src_p.lower() or "_l" in src_p.lower()
            is_fr = "_fr" in src_p.lower() or "_2" in src_p or "_r" in src_p.lower()
            is_pure_mono = (len(src_ports) == 1) or ("_mono" in src_p.lower())
            
            src_target = src_p.split()[0] if src_p and src_p.split()[0].isdigit() else src_p
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
                    dst_target = dst_p.split()[0] if dst_p and dst_p.split()[0].isdigit() else dst_p
                    cmd = ["pw-link"]
                    if unlink:
                        cmd.append("-d")
                    cmd.extend([src_target, dst_target])
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
            try:
                out_ports_raw = subprocess.check_output(["pw-link", "-I", "-o"], text=True, stderr=subprocess.DEVNULL)
            except Exception:
                out_ports_raw = ""
            if not out_ports_raw:
                out_ports_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
            out_ports = [l.strip() for l in out_ports_raw.splitlines() if l.strip()]
        except Exception:
            out_ports = []

        try:
            try:
                in_ports_raw = subprocess.check_output(["pw-link", "-I", "-i"], text=True, stderr=subprocess.DEVNULL)
            except Exception:
                in_ports_raw = ""
            if not in_ports_raw:
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
        port_meta = self._get_active_port_metadata_map()

        for ch in channels_to_sync:
            ch_id = ch["id"]
            is_linked = self.is_channel_linked(ch_id)
            ch_type = ch.get("type", "sink")
            is_source_channel = (ch_type == "source") or (ch_type not in ("app", "sink", "group") and any(k in ch_id.lower() for k in ("mic", "fefine", "fifine", "microphone", "elgato_wave_xlr", "input", "capture")))
            
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
                        if self._port_matches_tokens(p, input_tokens, port_meta):
                            matched_ports.append(p)
                ch_out_ports = matched_ports
            else:
                # Direct Submix Ingestion: Find actual application output ports
                app_out_ports = []
                assigned = self.get_assigned_apps(ch_id)
                for app in assigned:
                    tokens = self._get_match_tokens(app)
                    for p in out_ports:
                        if p.startswith("output.WaveController_") or p.startswith("WaveController_"):
                            continue
                        if ":output_" in p and self._port_matches_tokens(p, tokens, port_meta):
                            app_out_ports.append(p)

                sink_node = f"WaveController_Channel_{ch_id}"
                sink_play_ports = [p for p in in_ports if re.sub(r'^\d+\s+', '', p).strip().startswith(f"{sink_node}:")]
                sink_mon_ports = [p for p in out_ports if re.sub(r'^\d+\s+', '', p).strip().startswith(f"{sink_node}:")]

                # Route assigned application outputs into the group ingestion node (if exposed group channel)
                if ch.get("expose_sink", False) and app_out_ports and sink_play_ports:
                    self._link_stereo_ports(app_out_ports, sink_play_ports, unlink=False)
                    ch_out_ports = sink_mon_ports if sink_mon_ports else app_out_ports
                else:
                    ch_out_ports = app_out_ports

            if not is_source_channel and app_out_ports:
                has_active_sink_mix = any(m.get("type") == "sink" or m.get("id") in ("personal", "personal_mix") for m in mixes_copy)
                if has_active_sink_mix:
                    # When WaveController sink mixes are active, ensure assigned apps don't directly play out to physical hardware sinks (bypass isolation)
                    for src_p in app_out_ports:
                        src_links = links_map.get(src_p, set())
                        src_target = src_p.split()[0] if src_p and src_p.split()[0].isdigit() else src_p
                        for linked_dest in list(src_links):
                            dest_clean = re.sub(r'^\d+\s+', '', linked_dest).strip()
                            dest_target = linked_dest.split()[0] if linked_dest and linked_dest.split()[0].isdigit() else linked_dest
                            if dest_clean.startswith("alsa_output.") and ":playback_" in dest_clean:
                                try:
                                    subprocess.run(["pw-link", "-d", src_target, dest_target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                except Exception:
                                    pass
                            # Also sever direct links to mix sinks (prevent unattenuated bypass leaks)
                            elif dest_clean.startswith("WaveController_") and not dest_clean.startswith("input.WaveController_submix_") and not dest_clean.startswith(f"WaveController_Channel_{ch_id}:"):
                                try:
                                    subprocess.run(["pw-link", "-d", src_target, dest_target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                except Exception:
                                    pass
                else:
                    # When NO sink mixes exist (all devices removed / clean slate), ensure assigned apps route directly to physical system default output
                    fallback_in = self._get_default_sink_playback_ports()
                    if fallback_in:
                        self._link_stereo_ports(app_out_ports, fallback_in, unlink=False)

            # Proactively sever ANY existing links from this channel to mixes where it is disabled
            for src_p in ch_out_ports:
                src_target = src_p.split()[0] if src_p and src_p.split()[0].isdigit() else src_p
                for linked_dest in list(links_map.get(src_p, set())):
                    dest_clean = re.sub(r'^\d+\s+', '', linked_dest).strip()
                    dest_target = linked_dest.split()[0] if linked_dest and linked_dest.split()[0].isdigit() else linked_dest
                    if dest_clean.startswith("WaveController_") and (":playback_" in dest_clean or ":input_" in dest_clean):
                        for m in self.mixes:
                            m_pref_sink = f"WaveController_{m['id']}_Sink:playback_"
                            m_pref_source = f"WaveController_{m['id']}_Source:input_"
                            if dest_clean.startswith(m_pref_sink) or dest_clean.startswith(m_pref_source):
                                if not self.is_channel_mix_enabled(ch_id, m["id"]):
                                    try:
                                        subprocess.run(["pw-link", "-d", src_target, dest_target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                    except Exception:
                                        pass

            for m in mixes_to_sync:
                m_id = m["id"]
                target_prefixes = [
                    f"WaveController_{m_id}_Sink:playback_",
                    f"WaveController_{m_id}_Source:playback_",
                    f"WaveController_{m_id}_Source:input_",
                ]
                target_in_ports = []
                for p in in_ports:
                    p_clean = re.sub(r'^\d+\s+', '', p).strip()
                    for pref in target_prefixes:
                        if p_clean.startswith(pref):
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
                    
                    lb_in_ports = [p for p in in_ports if re.sub(r'^\d+\s+', '', p).strip().startswith(loopback_in_prefix)]
                    lb_out_ports = [p for p in out_ports if re.sub(r'^\d+\s+', '', p).strip().startswith(loopback_out_prefix)]

                    if not target_in_ports or not lb_in_ports or not lb_out_ports:
                        for _ in range(20):
                            time.sleep(0.01)
                            try:
                                try:
                                    o_raw = subprocess.check_output(["pw-link", "-I", "-o"], text=True, stderr=subprocess.DEVNULL)
                                except Exception:
                                    o_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
                                try:
                                    i_raw = subprocess.check_output(["pw-link", "-I", "-i"], text=True, stderr=subprocess.DEVNULL)
                                except Exception:
                                    i_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
                                out_ports = [l.strip() for l in o_raw.splitlines() if l.strip()]
                                in_ports = [l.strip() for l in i_raw.splitlines() if l.strip()]
                                lb_in_ports = [p for p in in_ports if re.sub(r'^\d+\s+', '', p).strip().startswith(loopback_in_prefix)]
                                lb_out_ports = [p for p in out_ports if re.sub(r'^\d+\s+', '', p).strip().startswith(loopback_out_prefix)]
                                if lb_in_ports and lb_out_ports:
                                    target_in_ports = [p for p in in_ports if any(re.sub(r'^\d+\s+', '', p).strip().startswith(pref) for pref in target_prefixes)]
                                    break
                            except Exception:
                                pass

                    # Ingestion Audit: Sever any incoming links to lb_in_ports that are NOT in ch_out_ports
                    clean_ch_ports = {re.sub(r'^\d+\s+', '', c).strip() for c in ch_out_ports} | {c.split()[0] for c in ch_out_ports if c} | set(ch_out_ports)
                    for dest_p in lb_in_ports:
                        dest_target = dest_p.split()[0] if dest_p and dest_p.split()[0].isdigit() else dest_p
                        dest_clean = re.sub(r'^\d+\s+', '', dest_p).strip()
                        for src_p, dests in links_map.items():
                            src_target = src_p.split()[0] if src_p and src_p.split()[0].isdigit() else src_p
                            src_clean = re.sub(r'^\d+\s+', '', src_p).strip()
                            dest_match = (dest_p in dests or dest_target in dests or dest_clean in dests)
                            src_in_ch = (src_p in clean_ch_ports or src_target in clean_ch_ports or src_clean in clean_ch_ports)
                            if dest_match and not src_in_ch:
                                try:
                                    subprocess.run(["pw-link", "-d", src_target, dest_target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                except Exception:
                                    pass

                    # Link Stage 1: Channel Output -> Loopback Input (only if ch_out_ports has items)
                    if ch_out_ports:
                        self._link_stereo_ports(ch_out_ports, lb_in_ports, unlink=False)
                    else:
                        self._link_stereo_ports(ch_out_ports, lb_in_ports, unlink=True)

                    # Link Stage 2: Loopback Output -> Mix Target Input
                    self._link_stereo_ports(lb_out_ports, target_in_ports, unlink=False)

                    # Link Stage 2 — Verification & Retry: pw-link sometimes silently drops FR links
                    # due to port ID ordering or graph state. Verify each lb_out port has a live
                    # link to the corresponding target_in port by checking pw-dump Link objects,
                    # then re-issue pw-link by numeric ID if the link is missing.
                    try:
                        # Build a set of (output-port-id, input-port-id) tuples from live links
                        live_links_raw = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
                        live_links_data = json.loads(live_links_raw)
                        active_port_pairs = set()
                        for _obj in live_links_data:
                            if _obj.get("type") == "PipeWire:Interface:Link":
                                _info = _obj.get("info", {})
                                if _info.get("state") in ("active", "paused", "allocating"):
                                    active_port_pairs.add((_info.get("output-port-id"), _info.get("input-port-id")))

                        for lb_p in lb_out_ports:
                            lb_id_str = lb_p.split()[0] if lb_p.split()[0].isdigit() else None
                            if not lb_id_str:
                                continue
                            lb_id = int(lb_id_str)
                            lb_clean = re.sub(r'^\d+\s+', '', lb_p).strip()
                            suffix_low = lb_clean.split(":")[-1].lower()
                            is_lb_fr = "_fr" in suffix_low
                            is_lb_fl = "_fl" in suffix_low

                            for tgt_p in target_in_ports:
                                tgt_id_str = tgt_p.split()[0] if tgt_p.split()[0].isdigit() else None
                                if not tgt_id_str:
                                    continue
                                tgt_id = int(tgt_id_str)
                                tgt_clean = re.sub(r'^\d+\s+', '', tgt_p).strip()
                                tgt_suf_low = tgt_clean.split(":")[-1].lower()
                                is_tgt_fr = "_fr" in tgt_suf_low
                                is_tgt_fl = "_fl" in tgt_suf_low

                                if not ((is_lb_fl and is_tgt_fl) or (is_lb_fr and is_tgt_fr)):
                                    continue

                                if (lb_id, tgt_id) not in active_port_pairs:
                                    # Link is missing — re-issue by numeric ID
                                    subprocess.run(
                                        ["pw-link", lb_id_str, tgt_id_str],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                                    )
                                    log.debug(f"[WaveController.PipeWire] Submix link re-issued: port {lb_id} -> {tgt_id} ({lb_clean} -> {tgt_clean})")
                    except Exception:
                        pass
                else:
                    self._stop_submix_loopback(ch_id, m_id)
                    self._link_stereo_ports(ch_out_ports, target_in_ports, unlink=True)

        # Synchronize physical output target devices for all Sink mixes
        self._sync_mix_physical_output_routing(mix_id, out_ports, in_ports)

        # Strict Channel-to-Mix Isolation: Ensure that applications actively assigned to a channel strip
        # do NOT bypass their channel's faders by linking directly to mix sinks.
        # Unassigned desktop applications (using the system default device) are allowed to feed Personal Mix.
        try:
            assigned_tokens = set()
            with self._lock:
                for apps in self.assigned_apps.values():
                    for a in apps:
                        assigned_tokens.update(self._get_match_tokens(a))

            fresh_links = self._get_pw_links_map()
            port_meta = self._get_active_port_metadata_map()
            for m in mixes_copy:
                m_id = m["id"]
                target_prefixes = (
                    f"WaveController_{m_id}_Sink:playback_",
                    f"WaveController_{m_id}_Source:playback_",
                    f"WaveController_{m_id}_Source:input_",
                )
                for src_p, dests in fresh_links.items():
                    if not src_p.startswith("output.WaveController_submix_"):
                        # If the source belongs to an actively assigned channel or channel sink, sever the direct bypass link
                        is_assigned_src = (
                            src_p.startswith("WaveController_Channel_") or
                            src_p.startswith("output.WaveController_Channel_") or
                            (assigned_tokens and self._port_matches_tokens(src_p, assigned_tokens, port_meta))
                        )
                        if is_assigned_src:
                            for dest_p in dests:
                                if any(dest_p.startswith(pref) for pref in target_prefixes):
                                    try:
                                        subprocess.run(["pw-link", "-d", src_p, dest_p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                    except Exception:
                                        pass

            self._sync_unassigned_app_streams(out_ports=out_ports, in_ports=in_ports, links_map=fresh_links, port_meta=port_meta)
            self._notify_peak_monitor_refresh()
        except Exception:
            pass

    def _get_pw_links_map(self) -> dict:
        """Returns a dict mapping source_port -> set(destination_ports) from PipeWire with both string names and numeric port IDs."""
        try:
            try:
                out = subprocess.check_output(["pw-link", "-I", "-l"], text=True, stderr=subprocess.DEVNULL)
            except Exception:
                out = ""
            if not out:
                out = subprocess.check_output(["pw-link", "-l"], text=True, stderr=subprocess.DEVNULL)
            links = {}
            curr_id = None
            curr_name = None
            for line in out.splitlines():
                if not line:
                    continue
                if not line.startswith(" ") or (not "|->" in line and not "|<-" in line and not "->" in line and not "<-" in line):
                    line_clean = line.strip()
                    m_hdr = re.match(r"^(\d+)\s+(.+)$", line_clean)
                    if m_hdr:
                        curr_id = m_hdr.group(1).strip()
                        curr_name = m_hdr.group(2).strip()
                        links.setdefault(curr_id, set())
                        links.setdefault(curr_name, set())
                        links.setdefault(f"{curr_id} {curr_name}", set())
                    else:
                        curr_id = None
                        curr_name = line_clean
                        links.setdefault(curr_name, set())
                elif "|->" in line or "->" in line:
                    m_tgt_id = re.search(r"\|\->\s*(\d+)\s+(.+)$", line)
                    if m_tgt_id:
                        d_id = m_tgt_id.group(1).strip()
                        d_name = m_tgt_id.group(2).strip()
                        keys = [k for k in (curr_id, curr_name, f"{curr_id} {curr_name}" if curr_id else None) if k]
                        for k in keys:
                            links.setdefault(k, set()).add(d_name)
                            links.setdefault(k, set()).add(f"{d_id} {d_name}")
                    else:
                        target = line.replace("|->", "").replace("->", "").strip()
                        if curr_name:
                            links.setdefault(curr_name, set()).add(target)
                elif "|<-" in line or "<-" in line:
                    m_src_id = re.search(r"\|<-\s*(\d+)\s+(.+)$", line)
                    if m_src_id:
                        s_id = m_src_id.group(1).strip()
                        s_name = m_src_id.group(2).strip()
                        keys = [k for k in (curr_id, curr_name, f"{curr_id} {curr_name}" if curr_id else None) if k]
                        for k in keys:
                            if k.isdigit():
                                continue
                            links.setdefault(s_id, set()).add(k)
                            links.setdefault(s_name, set()).add(k)
                            links.setdefault(f"{s_id} {s_name}", set()).add(k)
                    else:
                        src = line.replace("|<-", "").replace("<-", "").strip()
                        if curr_name:
                            links.setdefault(src, set()).add(curr_name)
            return links
        except Exception:
            return {}

    def _sync_mix_physical_output_routing(self, mix_id: str = None, out_ports: list = None, in_ports: list = None):
        """
        Routes WaveController Sink mixes (e.g. Personal Mix, Guest Mix)
        to their designated physical output target devices via pw-link.
        Also unlinks any obsolete or unassigned physical connections.
        """
        if hasattr(self, "sink_manager") and self.sink_manager:
            with self._lock:
                mixes_copy = list(self.mixes)
            links_map = self._get_pw_links_map()
            self.sink_manager.sync_physical_output_routing(
                mixes_copy,
                mix_id=mix_id,
                out_ports=out_ports,
                in_ports=in_ports,
                get_mix_mute_fn=self.get_mix_master_mute,
                links_map=links_map
            )
            return

        if out_ports is None:
            try:
                try:
                    out_ports_raw = subprocess.check_output(["pw-link", "-I", "-o"], text=True, stderr=subprocess.DEVNULL)
                except Exception:
                    out_ports_raw = ""
                if not out_ports_raw:
                    out_ports_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
                out_ports = [l.strip() for l in out_ports_raw.splitlines() if l.strip()]
            except Exception:
                out_ports = []

        if in_ports is None:
            try:
                try:
                    in_ports_raw = subprocess.check_output(["pw-link", "-I", "-i"], text=True, stderr=subprocess.DEVNULL)
                except Exception:
                    in_ports_raw = ""
                if not in_ports_raw:
                    in_ports_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
                in_ports = [l.strip() for l in in_ports_raw.splitlines() if l.strip()]
            except Exception:
                in_ports = []

        clean_out_ports = {re.sub(r'^\d+\s+', '', p).strip() for p in out_ports}
        clean_in_ports = [re.sub(r'^\d+\s+', '', p).strip() for p in in_ports]
        links_map = self._get_pw_links_map()

        with self._lock:
            mixes_copy = list(self.mixes)

        mixes_to_sync = [m for m in mixes_copy if mix_id is None or m["id"] == mix_id]

        for m in mixes_to_sync:
            m_id = m["id"]
            m_type = m.get("type", "source")

            is_personal = m_id in ("personal", "personal_mix") or (m_type == "sink" and "personal" in m_id)
            if is_personal:
                target_dev = getattr(self, "selected_monitor_device", None) or config_manager.get("default_output_device", "") or m.get("target_device", "") or "default"
                if not target_dev or "wavecontroller" in str(target_dev).lower():
                    target_dev = config_manager.get("default_output_device", "") or m.get("target_device", "") or "default"
                    if "wavecontroller" in str(target_dev).lower():
                        target_dev = "default"
                m["target_device"] = target_dev
            else:
                target_dev = m.get("target_device", "none" if not is_personal else "default")

            if m_type != "sink" and not is_personal:
                continue

            mon_fl = f"WaveController_{m_id}_Sink:monitor_FL"
            mon_fr = f"WaveController_{m_id}_Sink:monitor_FR"

            desired_fl = set()
            desired_fr = set()

            is_mix_muted = self.get_mix_master_mute(m_id)
            if target_dev and target_dev != "none" and not is_mix_muted:
                clean_target = str(target_dev).replace("alsa_card.", "").replace("alsa_output.", "").replace("alsa_input.", "").strip().lower()
                if not clean_target or clean_target in ("default", "none") or "wavecontroller" in clean_target:
                    wave_ports = [p for p in clean_in_ports if ("wave" in p.lower() or "elgato" in p.lower()) and ":playback_" in p and p.startswith("alsa_output.")]
                    if wave_ports:
                        clean_target = wave_ports[0].split(":")[0].replace("alsa_output.", "").strip().lower()
                    else:
                        first_alsa_nodes = [p.split(":")[0] for p in clean_in_ports if p.startswith("alsa_output.") and ":playback_" in p]
                        if first_alsa_nodes:
                            clean_target = first_alsa_nodes[0].replace("alsa_output.", "").strip().lower()

                for p in clean_in_ports:
                    if p.startswith("WaveController_") or p.startswith("output.WaveController_") or p.startswith("input.WaveController_"):
                        continue
                    if ":playback_" not in p or not p.startswith("alsa_output."):
                        continue
                    
                    p_low = p.lower()
                    matched = False
                    if clean_target and clean_target != "default":
                        if clean_target in p_low:
                            matched = True
                        else:
                            dev_tokens = self._get_match_tokens(clean_target)
                            if self._port_matches_tokens(p, dev_tokens):
                                matched = True
                    elif clean_target == "default":
                        first_alsa_nodes = [p_clean.split(":")[0] for p_clean in clean_in_ports if p_clean.startswith("alsa_output.") and ":playback_" in p_clean]
                        if first_alsa_nodes and p.startswith(f"{first_alsa_nodes[0]}:"):
                            matched = True

                    if matched:
                        suffix = p.split(":")[-1].lower()
                        if "_fl" in suffix or suffix.endswith("_1") or suffix.endswith("_l") or suffix == "playback_0":
                            desired_fl.add(p)
                        elif "_fr" in suffix or suffix.endswith("_2") or suffix.endswith("_r") or suffix == "playback_1":
                            desired_fr.add(p)

                # Resilient fallback for Personal Mix to ensure headphones are NEVER left unlinked
                if is_personal and (not desired_fl or not desired_fr):
                    alsa_playback_ports = [p for p in clean_in_ports if p.startswith("alsa_output.") and ":playback_" in p]
                    wave_ports = [p for p in alsa_playback_ports if "wave" in p.lower() or "elgato" in p.lower()]
                    candidate_ports = wave_ports or alsa_playback_ports
                    for p in candidate_ports:
                        suffix = p.split(":")[-1].lower()
                        if ("_fl" in suffix or suffix.endswith("_1") or suffix.endswith("_l") or suffix == "playback_0") and not desired_fl:
                            desired_fl.add(p)
                        elif ("_fr" in suffix or suffix.endswith("_2") or suffix.endswith("_r") or suffix == "playback_1") and not desired_fr:
                            desired_fr.add(p)

            # Reconcile FL links
            raw_fl_links = links_map.get(mon_fl, set())
            clean_fl_links = {re.sub(r'^\d+\s+', '', d).strip() for d in raw_fl_links if not d.isdigit()}
            for linked_dest in list(clean_fl_links):
                if linked_dest.startswith("alsa_output.") and linked_dest not in desired_fl:
                    try:
                        subprocess.run(["pw-link", "-d", mon_fl, linked_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
            if mon_fl in clean_out_ports:
                for dest in desired_fl:
                    if dest not in clean_fl_links:
                        try:
                            subprocess.run(["pw-link", mon_fl, dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass

            # Reconcile FR links
            raw_fr_links = links_map.get(mon_fr, set())
            clean_fr_links = {re.sub(r'^\d+\s+', '', d).strip() for d in raw_fr_links if not d.isdigit()}
            for linked_dest in list(clean_fr_links):
                if linked_dest.startswith("alsa_output.") and linked_dest not in desired_fr:
                    try:
                        subprocess.run(["pw-link", "-d", mon_fr, linked_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
            if mon_fr in clean_out_ports:
                for dest in desired_fr:
                    if dest not in clean_fr_links:
                        try:
                            subprocess.run(["pw-link", mon_fr, dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass

            if m_id in ("personal", "personal_mix") or (m_type == "sink" and "personal" in m_id):
                # Reconcile Fallback Sink monitor ports to the same physical monitor device target
                fb_mon_fl = next((p for p in clean_out_ports if p.startswith("WaveController_Fallback_Sink:") and ("_fl" in p.lower() or "_1" in p or ":output_1" in p or ":monitor_fl" in p.lower())), "WaveController_Fallback_Sink:monitor_FL")
                fb_mon_fr = next((p for p in clean_out_ports if p.startswith("WaveController_Fallback_Sink:") and ("_fr" in p.lower() or "_2" in p or ":output_2" in p or ":monitor_fr" in p.lower())), "WaveController_Fallback_Sink:monitor_FR")

                raw_fb_fl = links_map.get(fb_mon_fl, set())
                clean_fb_fl = {re.sub(r'^\d+\s+', '', d).strip() for d in raw_fb_fl if not d.isdigit()}
                for linked_dest in list(clean_fb_fl):
                    if linked_dest.startswith("alsa_output.") and linked_dest not in desired_fl:
                        try:
                            subprocess.run(["pw-link", "-d", fb_mon_fl, linked_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass
                if fb_mon_fl in clean_out_ports:
                    for dest in desired_fl:
                        if dest not in clean_fb_fl:
                            try:
                                subprocess.run(["pw-link", fb_mon_fl, dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            except Exception:
                                pass

                raw_fb_fr = links_map.get(fb_mon_fr, set())
                clean_fb_fr = {re.sub(r'^\d+\s+', '', d).strip() for d in raw_fb_fr if not d.isdigit()}
                for linked_dest in list(clean_fb_fr):
                    if linked_dest.startswith("alsa_output.") and linked_dest not in desired_fr:
                        try:
                            subprocess.run(["pw-link", "-d", fb_mon_fr, linked_dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        except Exception:
                            pass
                if fb_mon_fr in clean_out_ports:
                    for dest in desired_fr:
                        if dest not in clean_fb_fr:
                            try:
                                subprocess.run(["pw-link", fb_mon_fr, dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

    def add_channel(self, name: str, icon: str = None, ch_type: str = "sink", assigned_apps: list = None, sync_meter: bool = False, expose_sink: bool = False) -> dict:
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
                "sync_meter": sync_meter,
                "expose_sink": bool(expose_sink)
            }
            self.channels.append(new_ch)
            self.channel_master_states[ch_id] = {
                "volume": default_vol,
                "muted": False
            }
            self.channel_states[ch_id] = {}
            self.assigned_apps[ch_id] = assigned_apps if assigned_apps is not None else ([name] if ch_type in ("sink", "app") else [])
            for mx in self.mixes:
                self.channel_states[ch_id][mx["id"]] = {
                    "volume": default_vol,
                    "muted": False,
                    "linked": True,
                    "enabled": False
                }

            self._save_state_to_config(immediate=True)
            self._volume_queue[ch_id] = (default_vol, False)
            self._volume_event.set()

        def _bg_provision():
            self._ensure_virtual_mix_nodes()
            self._refresh_node_cache()
            self._sync_channel_audio_routing(channel_id=ch_id)
            self._notify_peak_monitor_refresh()

        threading.Thread(target=_bg_provision, daemon=True).start()
        return new_ch

    def remove_channel(self, channel_id: str) -> bool:
        with self._lock:
            ch_exists = any(c["id"] == channel_id for c in self.channels)
            if not ch_exists:
                return False

            assigned = list(self.assigned_apps.get(channel_id, []))
            self.channels = [c for c in self.channels if c["id"] != channel_id]
            self.channel_states.pop(channel_id, None)
            if hasattr(self, "channel_master_states") and isinstance(self.channel_master_states, dict):
                self.channel_master_states.pop(channel_id, None)
            self.assigned_apps.pop(channel_id, None)

            procs_to_terminate = []
            keys_to_remove = [k for k in list(self._submix_procs.keys()) if k[0] == channel_id]
            for k in keys_to_remove:
                proc = self._submix_procs.pop(k, None)
                if proc:
                    procs_to_terminate.append(proc)
                self._submix_node_ids.pop(k, None)
                self._submix_volume_queue.pop(k, None)

            self._save_state_to_config(immediate=True)
            self._notify_peak_monitor_refresh()

        def _bg_teardown():
            for proc in procs_to_terminate:
                try:
                    proc.terminate()
                    proc.wait(timeout=0.1)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            try:
                subprocess.run(["pkill", "-f", f"WaveController_submix_{channel_id}_"], stderr=subprocess.DEVNULL)
            except Exception:
                pass

            try:
                out = subprocess.check_output(["pw-link", "-l"], text=True, stderr=subprocess.DEVNULL)
                curr_src = None
                ch_prefixes = (
                    f"WaveController_Channel_{channel_id}:",
                    f"input.WaveController_submix_{channel_id}_",
                    f"output.WaveController_submix_{channel_id}_"
                )
                for line in out.splitlines():
                    l_str = line.strip()
                    if not line.startswith(" ") and ":" in l_str:
                        curr_src = l_str
                    elif "|->" in l_str and curr_src:
                        dest_p = l_str.replace("|->", "").strip()
                        if any(curr_src.startswith(pref) or dest_p.startswith(pref) for pref in ch_prefixes):
                            subprocess.run(["pw-link", "-d", curr_src, dest_p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

            if assigned:
                for app in assigned:
                    canon_app = str(app).lower().strip()
                    with self._lock:
                        if hasattr(self, "_app_volume_overrides"):
                            self._app_volume_overrides.pop(canon_app, None)
                    try:
                        tokens = self._get_match_tokens(app)
                        out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
                        for obj in json.loads(out):
                            if obj.get("type") == "PipeWire:Interface:Node":
                                props = obj.get("info", {}).get("props", {})
                                if self._node_matches_tokens(props, tokens):
                                    nid = str(obj["id"])
                                    self._dispatch_node_volume(nid, 1.00, False)
                    except Exception:
                        pass

                    self._unbind_app_from_wireplumber_target(app, channel_id)

                try:
                    out_ports_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
                    out_ports = [l.strip() for l in out_ports_raw.splitlines() if l.strip()]
                    fallback_in = self._get_default_sink_playback_ports()

                    app_out_ports = []
                    for app in assigned:
                        tokens = self._get_match_tokens(app)
                        for p in out_ports:
                            if p.startswith("output.WaveController_") or p.startswith("WaveController_"):
                                continue
                            if ":output_" in p and self._port_matches_tokens(p, tokens):
                                app_out_ports.append(p)

                    if app_out_ports:
                        in_ports_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
                        in_ports = [l.strip() for l in in_ports_raw.splitlines() if l.strip()]
                        for p in in_ports:
                            if p.startswith("WaveController_") and not p.startswith("WaveController_Fallback_Sink:") and (":playback_" in p or ":input_" in p):
                                self._link_stereo_ports(app_out_ports, [p], unlink=True)

                    if fallback_in and app_out_ports:
                        self._link_stereo_ports(app_out_ports, fallback_in, unlink=False)
                except Exception:
                    pass

            self._refresh_node_cache()
            self._ensure_virtual_mix_nodes()
            self._sync_channel_audio_routing()
            self._notify_peak_monitor_refresh()

        threading.Thread(target=_bg_teardown, daemon=True).start()
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
                ch_type = ch.get("type", "sink")
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
            self._mix_volume_queue[mix_id] = (100, False)
            self._volume_event.set()

        def _bg_mix_provision():
            self._ensure_virtual_mix_nodes()
            self._refresh_node_cache()
            self._sync_channel_audio_routing(mix_id=mix_id)

        threading.Thread(target=_bg_mix_provision, daemon=True).start()
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

                    def _bg_mix_update():
                        self._ensure_virtual_mix_nodes()
                        self._refresh_node_cache()
                        self._sync_channel_audio_routing(mix_id=mix_id)

                    threading.Thread(target=_bg_mix_update, daemon=True).start()
                    return True
        return False

    def is_mix_system_default(self, mix_id: str) -> bool:
        """Returns True if mix_id is currently configured as the system default audio sink or source."""
        canon_mix = self._match_mix_id(mix_id)
        with self._lock:
            for m in self.mixes:
                if m.get("id") in (mix_id, canon_mix):
                    if m.get("is_default", False):
                        return True
                    # Default fallbacks if no explicit is_default is set
                    m_type = m.get("type", "source" if m.get("id") != "personal" else "sink")
                    if m_type == "sink" and m.get("id") in ("personal", "personal_mix"):
                        has_explicit = any(other.get("is_default", False) for other in self.mixes if (other.get("type") == "sink" or other.get("id") == "personal"))
                        return not has_explicit
                    elif m_type == "source" and m.get("id") in ("chat_mix", "chat"):
                        has_explicit = any(
                            other.get("is_default", False)
                            for other in self.mixes
                            if other.get("type", "source" if other.get("id") != "personal" else "sink") == "source"
                        )
                        return not has_explicit
        return False

    def _apply_configured_system_defaults(self):
        """Applies saved mix defaults only after the user has opted in."""
        if not config_manager.get("system_defaults_enabled", False):
            return

        selected_types = set()
        for mix in list(self.mixes):
            mix_type = mix.get("type", "source" if mix.get("id") != "personal" else "sink")
            if mix_type in selected_types:
                continue
            if self.is_mix_system_default(mix["id"]):
                self.set_mix_system_default(mix["id"], True)
                selected_types.add(mix_type)

    def set_mix_system_default(self, mix_id: str, is_default: bool = True) -> bool:
        """Sets or unsets a mix as the system default audio sink (for Output mixes) or source (for Input mixes)."""
        canon_mix = self._match_mix_id(mix_id)
        defaults_enabled = config_manager.get("system_defaults_enabled", False)
        target_node_name = None
        m_type = "sink"
        with self._lock:
            target_mix = next((m for m in self.mixes if m.get("id") in (mix_id, canon_mix)), None)
            if not target_mix:
                return False
            m_type = target_mix.get("type", "source" if target_mix.get("id") != "personal" else "sink")
            for m in self.mixes:
                curr_type = m.get("type", "source" if m.get("id") != "personal" else "sink")
                if curr_type == m_type:
                    if m.get("id") in (mix_id, canon_mix):
                        m["is_default"] = is_default
                    else:
                        m["is_default"] = False

            if is_default:
                if m_type == "sink" or target_mix.get("id") == "personal":
                    target_node_name = f"WaveController_{target_mix['id']}_Sink"
                else:
                    target_node_name = f"WaveController_{target_mix['id']}_Source"

            self._save_state_to_config(immediate=True)

        if target_node_name and defaults_enabled:
            def _apply_default_node():
                for attempt in range(20):
                    try:
                        out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
                        node_id = None
                        for obj in json.loads(out):
                            if obj.get("type") == "PipeWire:Interface:Node":
                                props = obj.get("info", {}).get("props", {})
                                if props.get("node.name") == target_node_name:
                                    node_id = str(obj["id"])
                                    break
                        if node_id:
                            default_key = "default.audio.source" if m_type == "source" else "default.audio.sink"
                            configured_key = f"default.configured.{default_key}"
                            node_json = json.dumps({"name": target_node_name})
                            metadata_result = subprocess.run(
                                ["pw-metadata", "-n", "default", "0", default_key, node_json],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL
                            )
                            # Our virtual "_Source" mixes are Audio/Duplex nodes, which `wpctl set-default`
                            # refuses ("not a device node"), so write the persisted GNOME/PulseAudio-visible
                            # key directly instead of relying on wpctl for it.
                            configured_result = subprocess.run(
                                ["pw-metadata", "-n", "default", "0", configured_key, node_json],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL
                            )
                            if m_type != "source":
                                subprocess.run(
                                    ["wpctl", "set-default", node_id],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL
                                )
                            if metadata_result.returncode == 0 and configured_result.returncode == 0:
                                log.info(f"[WaveController.PipeWire] Set system default {m_type} to '{target_node_name}' (node_id={node_id})")
                                return
                    except Exception as e:
                        if attempt == 19:
                            log.warning(f"[WaveController.PipeWire] Failed to set system default {m_type}: {e}")
                    time.sleep(0.1)
                log.warning(f"[WaveController.PipeWire] Selected system default node '{target_node_name}' is not available")

            threading.Thread(target=_apply_default_node, daemon=True).start()
        elif not is_default and defaults_enabled:
            try:
                out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
                for obj in json.loads(out):
                    if obj.get("type") == "PipeWire:Interface:Node":
                        props = obj.get("info", {}).get("props", {})
                        media_class = props.get("media.class", "")
                        n_name = props.get("node.name", "")
                        if m_type == "sink" and media_class == "Audio/Sink" and (n_name.startswith("alsa_output.") or props.get("device.api") == "alsa"):
                            subprocess.run(["wpctl", "set-default", str(obj["id"])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            log.info(f"[WaveController.PipeWire] Restored system default sink to physical hardware '{n_name}' (id={obj['id']})")
                            break
                        elif m_type == "source" and media_class == "Audio/Source" and (n_name.startswith("alsa_input.") or props.get("device.api") == "alsa"):
                            subprocess.run(["wpctl", "set-default", str(obj["id"])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            log.info(f"[WaveController.PipeWire] Restored system default source to physical hardware '{n_name}' (id={obj['id']})")
                            break
            except Exception as e:
                log.warning(f"[WaveController.PipeWire] Failed to restore physical default: {e}")

        return True

    def remove_mix(self, mix_id: str):
        """Removes a mix and tears down its PipeWire virtual audio device and all associated submix loopbacks."""
        canon_mix = self._match_mix_id(mix_id)
        fallback_default_mix_id = None
        was_default = False
        m_type = "sink"
        with self._lock:
            target_mix = next((m for m in self.mixes if m.get("id") in (mix_id, canon_mix)), None)
            if target_mix:
                was_default = bool(target_mix.get("is_default", False))
                m_type = target_mix.get("type", "source" if target_mix.get("id") != "personal" else "sink")

            self.mixes = [m for m in self.mixes if m["id"] != mix_id and m["id"] != canon_mix]
            for ch_id in self.channel_states:
                self.channel_states[ch_id].pop(mix_id, None)
                self.channel_states[ch_id].pop(canon_mix, None)
            if hasattr(self, "mix_states"):
                self.mix_states.pop(mix_id, None)
                self.mix_states.pop(canon_mix, None)

            procs_to_terminate = []
            keys_to_remove = [k for k in list(self._submix_procs.keys()) if k[1] == mix_id or k[1] == canon_mix]
            for k in keys_to_remove:
                proc = self._submix_procs.pop(k, None)
                if proc:
                    procs_to_terminate.append(proc)
                self._submix_node_ids.pop(k, None)
                self._submix_volume_queue.pop(k, None)

            # If the deleted mix was the system default, fall back to standard default mix
            if was_default:
                if m_type == "sink":
                    fallback_default = next((m for m in self.mixes if m.get("id") == "personal" or m.get("type") == "sink"), None)
                else:
                    fallback_default = next((m for m in self.mixes if m.get("id") == "chat_mix" or m.get("type") == "source"), None)
                if fallback_default:
                    fallback_default["is_default"] = True
                    fallback_default_mix_id = fallback_default["id"]

            self._save_state_to_config(immediate=True)

        if fallback_default_mix_id:
            self.set_mix_system_default(fallback_default_mix_id, True)

        def _bg_mix_teardown():
            for proc in procs_to_terminate:
                try:
                    proc.terminate()
                    proc.wait(timeout=0.1)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            try:
                subprocess.run(["pkill", "-f", f"WaveController_submix_.*_{mix_id}"], stderr=subprocess.DEVNULL)
                if canon_mix != mix_id:
                    subprocess.run(["pkill", "-f", f"WaveController_submix_.*_{canon_mix}"], stderr=subprocess.DEVNULL)
            except Exception:
                pass

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

            try:
                out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
                data = json.loads(out)
                for obj in data:
                    props = obj.get("info", {}).get("props", {})
                    n_name = props.get("node.name", "")
                    if n_name in (f"WaveController_{mix_id}_Sink", f"WaveController_{mix_id}_Source",
                                f"WaveController_{canon_mix}_Sink", f"WaveController_{canon_mix}_Source"):
                        obj_id = obj.get("id")
                        if obj_id:
                            subprocess.run(["pw-cli", "destroy", str(obj_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

            self._ensure_virtual_mix_nodes()
            self._refresh_node_cache()

        threading.Thread(target=_bg_mix_teardown, daemon=True).start()

    @staticmethod
    def resolve_icon_for_app(app_name: str) -> str:
        """Determines the best GTK/Freedesktop icon name for a given application or device string."""
        if not app_name:
            return "audio-x-generic-symbolic"

        app_str = str(app_name).strip()
        app_low = app_str.lower()

        # 1. Exact / Known application mappings
        if "spotify" in app_low:
            return "spotify"
        elif "discord" in app_low:
            return "discord"
        elif "shortwave" in app_low:
            return "de.haeckerfelix.Shortwave"
        elif "steam" in app_low or "game" in app_low:
            return "steam"
        elif "firefox" in app_low:
            return "firefox"
        elif "google-chrome" in app_low or "google chrome" in app_low or app_low == "chrome":
            return "google-chrome"
        elif "chromium" in app_low:
            return "chromium"
        elif "brave" in app_low:
            return "brave-browser"
        elif "edge" in app_low or "msedge" in app_low:
            return "microsoft-edge"
        elif "vlc" in app_low:
            return "vlc"
        elif "obs" in app_low:
            return "com.obsproject.Studio"
        elif "slack" in app_low:
            return "slack"
        elif "teams" in app_low:
            return "teams"
        elif app_low in ("elgato wave xlr", "wave xlr", "wave:3", "wave:1", "wave neo", "elgato") or app_low.startswith("elgato wave"):
            return "elgato-wave-xlr-symbolic"
        elif app_low in ("mic", "microphone", "input"):
            return "audio-input-microphone-symbolic"

        # 2. Dynamic GTK Icon Theme Lookup
        try:
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk, Gdk
            display = Gdk.Display.get_default()
            if display:
                theme = Gtk.IconTheme.get_for_display(display)
                candidates = [
                    app_str,
                    app_low,
                    app_low.replace(" ", "-"),
                    app_low.replace(" ", "_"),
                    f"{app_str}-symbolic",
                    f"{app_low}-symbolic",
                    f"de.haeckerfelix.{app_str}",
                    f"org.gnome.{app_str}",
                    f"com.google.{app_str}",
                    f"org.{app_str}",
                    f"com.{app_str}"
                ]
                for cand in candidates:
                    if theme.has_icon(cand):
                        return cand
        except Exception:
            pass

        # 3. Search Desktop Entry files for Icon=
        for d in (
            "/var/lib/flatpak/exports/share/applications",
            os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
            "/usr/share/applications",
            os.path.expanduser("~/.local/share/applications")
        ):
            if os.path.isdir(d):
                try:
                    for fname in os.listdir(d):
                        if fname.endswith(".desktop") and (app_low in fname.lower() or app_str in fname):
                            full_p = os.path.join(d, fname)
                            try:
                                with open(full_p, "r", encoding="utf-8", errors="ignore") as f:
                                    for line in f:
                                        if line.startswith("Icon="):
                                            ic = line.strip().split("=", 1)[1]
                                            if ic:
                                                return ic
                            except Exception:
                                pass
                except Exception:
                    pass

        return "audio-x-generic-symbolic"

    def get_app_volume(self, app_name: str) -> int:
        """Returns the cached or assigned volume percentage of an individual application stream."""
        canon_app = str(app_name).lower().strip()
        with self._lock:
            if hasattr(self, "_app_volume_overrides") and canon_app in self._app_volume_overrides:
                return self._app_volume_overrides[canon_app].get("volume", 80)
        return 80

    def set_app_volume(self, app_name: str, volume_pct: int):
        """Sets the volume percentage of an individual application stream."""
        canon_app = str(app_name).lower().strip()
        vol = max(0, min(100, int(volume_pct)))
        with self._lock:
            if not hasattr(self, "_app_volume_overrides"):
                self._app_volume_overrides = {}
            if canon_app not in self._app_volume_overrides:
                self._app_volume_overrides[canon_app] = {}
            self._app_volume_overrides[canon_app]["volume"] = vol
        
        tokens = self._get_match_tokens(app_name)
        g_val = self._pct_to_pipewire_gain(vol)
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            for obj in json.loads(out):
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    if self._node_matches_tokens(props, tokens):
                        nid = str(obj["id"])
                        self._dispatch_node_volume(nid, g_val, self.get_app_mute(app_name))
        except Exception:
            pass

    def get_app_mute(self, app_name: str) -> bool:
        """Returns the mute state of an individual application stream."""
        canon_app = str(app_name).lower().strip()
        with self._lock:
            if hasattr(self, "_app_volume_overrides") and canon_app in self._app_volume_overrides:
                return self._app_volume_overrides[canon_app].get("muted", False)
        return False

    def set_app_mute(self, app_name: str, is_muted: bool):
        """Sets the mute state of an individual application stream."""
        canon_app = str(app_name).lower().strip()
        with self._lock:
            if not hasattr(self, "_app_volume_overrides"):
                self._app_volume_overrides = {}
            if canon_app not in self._app_volume_overrides:
                self._app_volume_overrides[canon_app] = {}
            self._app_volume_overrides[canon_app]["muted"] = bool(is_muted)
        
        tokens = self._get_match_tokens(app_name)
        vol = self.get_app_volume(app_name)
        g_val = self._pct_to_pipewire_gain(vol)
        try:
            out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
            for obj in json.loads(out):
                if obj.get("type") == "PipeWire:Interface:Node":
                    props = obj.get("info", {}).get("props", {})
                    if self._node_matches_tokens(props, tokens):
                        nid = str(obj["id"])
                        self._dispatch_node_volume(nid, g_val, is_muted)
        except Exception:
            pass

    def get_app_peaks(self, app_name: str) -> tuple:
        """Returns the live (peak_left, peak_right) stereo telemetry levels for an application stream."""
        if hasattr(self, "peak_monitor") and self.peak_monitor:
            tokens = self._get_match_tokens(app_name)
            for t in tokens:
                p = self.peak_monitor.get_app_stereo_peaks(t)
                if p != (0.0, 0.0):
                    return p
            p = self.peak_monitor.get_app_stereo_peaks(app_name)
            if p != (0.0, 0.0):
                return p
            try:
                with self.peak_monitor._lock:
                    app_low = str(app_name).lower().strip()
                    for k, peak_data in getattr(self.peak_monitor, "_channel_peaks", {}).items():
                        if k == app_low:
                            return peak_data.get("left", 0.0), peak_data.get("right", 0.0)
            except Exception:
                pass
        return 0.0, 0.0

    def provision_default_device_channels_and_mix(self, device_key: str, device_name: str = None, is_input: bool = True, is_output: bool = True):
        """Creates or rebinds the primary Personal Mix (Sink), Chat Mix (Source), and Microphone channel for default devices."""
        if not device_key:
            return

        name = device_name or "Microphone"
        log.info(f"[WaveController.PipeWire] provision_default_device_channels_and_mix for '{name}' (key='{device_key}', is_in={is_input}, is_out={is_output})")
        with self._lock:
            # 1. Output / Personal Mix (Sink Mix)
            if is_output:
                self.selected_monitor_device = device_key
                personal_mix = next((m for m in self.mixes if m.get("id") in ("personal", "personal_mix") or m.get("type") == "sink"), None)
                if personal_mix:
                    personal_mix["target_device"] = device_key
                else:
                    new_mix = {
                        "id": "personal",
                        "name": "Personal Mix",
                        "subtitle": "1 output",
                        "icon": "audio-headphones-symbolic",
                        "color": "#3db356",
                        "type": "sink",
                        "target_device": device_key
                    }
                    self.mixes.insert(0, new_mix)
                    if not hasattr(self, "mix_states") or not isinstance(self.mix_states, dict):
                        self.mix_states = {}
                    self.mix_states["personal"] = {"volume": 100, "muted": False}

            # 2. Chat Mix (Source Mix) - Virtual Input Mix for Discord, Voice Chat & OBS
            chat_mix = next((m for m in self.mixes if m.get("id") in ("chat_mix", "chat")), None)
            if not chat_mix:
                new_chat_mix = {
                    "id": "chat_mix",
                    "name": "Chat Mix",
                    "subtitle": "Virtual Input",
                    "icon": "audio-input-microphone-symbolic",
                    "color": "#9146ff",
                    "type": "source"
                }
                self.mixes.append(new_chat_mix)
                if not hasattr(self, "mix_states") or not isinstance(self.mix_states, dict):
                    self.mix_states = {}
                self.mix_states["chat_mix"] = {"volume": 100, "muted": False}

            # 3. Input / Microphone Channel
            if is_input:
                self.default_input_device = device_key
                mic_ch = next((c for c in self.channels if c.get("type") == "source" or c.get("id") in ("mic", "elgato_wave_xlr")), None)
                if mic_ch:
                    mic_ch["name"] = name
                    ch_id = mic_ch["id"]
                    self.assigned_apps[ch_id] = [name, device_key]
                    if ch_id not in self.channel_states:
                        self.channel_states[ch_id] = {}
                    for mx in self.mixes:
                        mx_id = mx["id"]
                        if mx_id not in self.channel_states[ch_id]:
                            self.channel_states[ch_id][mx_id] = {
                                "volume": 80,
                                "muted": False,
                                "linked": True,
                                "enabled": (mx_id in ("chat_mix", "chat"))
                            }
                        elif mx_id in ("chat_mix", "chat") and not self.channel_states[ch_id][mx_id].get("enabled", False):
                            # Ensure mic is enabled in Chat Mix
                            self.channel_states[ch_id][mx_id]["enabled"] = True
                else:
                    new_ch = {
                        "id": "mic",
                        "name": name,
                        "type": "source",
                        "icon": "audio-input-microphone-symbolic",
                        "default_vol": 80,
                        "sync_meter": False
                    }
                    self.channels.insert(0, new_ch)
                    self.assigned_apps["mic"] = [name, device_key]
                    if not hasattr(self, "channel_master_states") or not isinstance(self.channel_master_states, dict):
                        self.channel_master_states = {}
                    self.channel_master_states["mic"] = {"volume": 80, "muted": False}
                    if "mic" not in self.channel_states:
                        self.channel_states["mic"] = {}
                    for mx in self.mixes:
                        mx_id = mx["id"]
                        self.channel_states["mic"][mx_id] = {
                            "volume": 80,
                            "muted": False,
                            "linked": True,
                            "enabled": (mx_id in ("chat_mix", "chat"))
                        }

            # Ensure all other channels have state initialized for all active mixes
            for ch in self.channels:
                ch_id = ch["id"]
                if ch_id not in self.channel_states:
                    self.channel_states[ch_id] = {}
                for mx in self.mixes:
                    mx_id = mx["id"]
                    if mx_id not in self.channel_states[ch_id]:
                        master_v = self.get_channel_master_volume(ch_id)
                        self.channel_states[ch_id][mx_id] = {
                            "volume": master_v,
                            "muted": False,
                            "linked": True,
                            "enabled": False
                        }

            self._save_state_to_config(immediate=True)
            self._notify_peak_monitor_refresh()

        def _bg_provision():
            self._ensure_virtual_mix_nodes()
            self._refresh_node_cache()
            self._sync_channel_audio_routing()

        threading.Thread(target=_bg_provision, daemon=True).start()

    def remove_device_associated_channels_and_mixes(self, device_key: str):
        """Removes any source channels and sink mixes explicitly associated with the specified device."""
        if not device_key:
            return
        dev_k_low = str(device_key).lower().strip()
        dev_name = ""
        if self.hardware_mgr and hasattr(self.hardware_mgr, "get_device_display_name"):
            dev_name = str(self.hardware_mgr.get_device_display_name(device_key)).lower().strip()

        # 1. Identify associated source channels (e.g. mic channel)
        to_remove_ch = []
        for ch in list(self.channels):
            if ch.get("type") == "source":
                ch_id = ch["id"]
                ch_name_low = str(ch.get("name", "")).lower().strip()
                assigned = [str(a).lower() for a in self.assigned_apps.get(ch_id, [])]
                if dev_k_low in assigned or any(dev_k_low in a for a in assigned) or (dev_name and dev_name in ch_name_low) or (ch_id in ("mic", "elgato_wave_xlr") and ("elgato" in dev_k_low or "wave" in dev_k_low)):
                    to_remove_ch.append(ch_id)

        for ch_id in to_remove_ch:
            log.info(f"[WaveController.PipeWire] Removing source channel '{ch_id}' tied to removed device '{device_key}'")
            self.remove_channel(ch_id)

        # 2. Identify associated sink mixes (e.g. personal mix)
        to_remove_mix = []
        for mx in list(self.mixes):
            if mx.get("type") == "sink":
                m_id = mx["id"]
                tgt = str(mx.get("target_device", "")).lower().strip()
                if dev_k_low in tgt or (dev_name and dev_name in tgt) or (m_id in ("personal", "personal_mix") and ("elgato" in dev_k_low or "wave" in dev_k_low)):
                    to_remove_mix.append(m_id)

        for m_id in to_remove_mix:
            log.info(f"[WaveController.PipeWire] Removing sink mix '{m_id}' tied to removed device '{device_key}'")
            self.remove_mix(m_id)

        with self._lock:
            if dev_k_low in str(self.default_input_device).lower():
                self.default_input_device = ""
            if dev_k_low in str(self.selected_monitor_device).lower():
                self.selected_monitor_device = ""
            self._save_state_to_config(immediate=True)
            self._notify_peak_monitor_refresh()

        def _bg_cleanup():
            self._ensure_virtual_mix_nodes()
            self._refresh_node_cache()
            self._sync_channel_audio_routing()

        threading.Thread(target=_bg_cleanup, daemon=True).start()

    def remove_default_device_channels_and_mix(self):
        """Removes the primary Personal Mix and physical Microphone channel from the graph and config."""
        mic_ids = [c["id"] for c in list(self.channels) if c.get("type") == "source" or c.get("id") in ("mic", "elgato_wave_xlr")]
        for ch_id in mic_ids:
            self.remove_channel(ch_id)

        personal_ids = [m["id"] for m in list(self.mixes) if m.get("id") in ("personal", "personal_mix") or m.get("type") == "sink"]
        for m_id in personal_ids:
            self.remove_mix(m_id)

        with self._lock:
            self.default_input_device = ""
            self.selected_monitor_device = ""
            config_manager.set("primary_device_key", "", immediate=False)
            config_manager.set("default_input_device", "", immediate=False)
            config_manager.set("default_output_device", "", immediate=False)
            self._save_state_to_config(immediate=True)
            self._notify_peak_monitor_refresh()

        def _bg_cleanup():
            self._release_all_apps_to_system_default()
            self._ensure_virtual_mix_nodes()
            self._refresh_node_cache()
            self._sync_channel_audio_routing()

        threading.Thread(target=_bg_cleanup, daemon=True).start()
