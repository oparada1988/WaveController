import os
import re
import math
import subprocess
import threading
import time
import array
import fcntl
from wavecontroller.engine.metering.capture_driver import calc_perceptual_peak, open_pw_record, drain_and_calc_peaks

class MultiChannelPeakMonitor:
    """
    Captures real-time stereo (Left and Right) audio peaks using pw-record
    with isolated node port names for physical microphones and playback audio.
    Per-channel ingestion sinks get dedicated monitor taps for isolated VU metering.
    """
    def __init__(self, pipewire_mgr=None, hardware_mgr=None):
        self.peaks = {} # {channel_id: {"left": float, "right": float}}
        self.running = False
        self.mic_proc = None
        self.sink_proc = None
        self.thread = None
        self._discovery_thread = None
        self._refresh_event = threading.Event()
        self._lock = threading.Lock()
        self.pipewire_mgr = pipewire_mgr
        self.hardware_mgr = hardware_mgr
        # Per-channel isolated monitor processes and smoothed peak state
        self._channel_procs = {}  # {channel_id: subprocess.Popen}
        self._channel_proc_channels = {}  # {channel_id: int}
        self._channel_peaks = {}  # {channel_id: {"left": float, "right": float}}

    def set_pipewire_manager(self, pw_mgr):
        self.pipewire_mgr = pw_mgr

    def set_hardware_manager(self, hw_mgr):
        self.hardware_mgr = hw_mgr

    def start(self):
        self.running = True
        self._refresh_event.clear()
        # Initial discovery before starting the loops
        self._do_refresh_discovery()

        # 1. Continuous 40 FPS real-time audio capture loop
        self.thread = threading.Thread(target=self._run_capture_loop, daemon=True)
        self.thread.start()

        # 2. Background asynchronous graph discovery & link auditing worker
        self._discovery_thread = threading.Thread(target=self._run_discovery_loop, daemon=True)
        self._discovery_thread.start()

    def trigger_refresh(self):
        """Signals the background discovery worker to immediately re-evaluate active audio channels and stream targets without blocking the capture loop."""
        self._refresh_event.set()

    def stop(self):
        self.running = False
        self._refresh_event.set()
        with self._lock:
            procs = [self.mic_proc, self.sink_proc] + list(self._channel_procs.values())
            self.mic_proc = None
            self.sink_proc = None
            self._channel_procs.clear()
            self._channel_proc_channels.clear()

        for p in procs:
            if p:
                try:
                    p.terminate()
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass

    def _discover_mic_target(self) -> tuple:
        """Finds active physical microphone target node name and channels."""
        try:
            out = subprocess.check_output(['pw-link', '-o'], text=True, stderr=subprocess.DEVNULL)
            all_ports = [l.strip() for l in out.splitlines() if l.strip().startswith("alsa_input.") and ":capture_" in l.strip()]
            
            elgato_ports = [p for p in all_ports if 'wave' in p.lower() or 'elgato' in p.lower()]
            usb_ports = [p for p in all_ports if 'usb' in p.lower() and p not in elgato_ports]
            other_ports = [p for p in all_ports if p not in elgato_ports and p not in usb_ports]
            
            selected = elgato_ports or usb_ports or other_ports
            if not selected:
                return None, 1
            
            port = selected[0]
            node_name = port.split(':')[0]
            is_mono = any(p.lower().endswith("mono") or "mono" in p.lower() for p in selected)
            channels = 1 if is_mono else 2
            return node_name, channels
        except Exception:
            return None, 1

    def _discover_sink_target(self) -> str:
        """Finds active virtual mix sink or output playback device monitor target."""
        try:
            out = subprocess.check_output(['pw-link', '-o'], text=True, stderr=subprocess.DEVNULL)
            all_ports = [l.strip() for l in out.splitlines() if ':monitor_' in l.strip()]
            
            personal_ports = [p for p in all_ports if 'personal_mix_sink' in p.lower()]
            if personal_ports:
                return personal_ports[0].split(':')[0]

            wc_ports = [p for p in all_ports if 'wavecontroller' in p.lower() and 'sink' in p.lower()]
            elgato_ports = [p for p in all_ports if 'wave' in p.lower() or 'elgato' in p.lower() or '0fd9' in p.lower()]
            other_ports = [p for p in all_ports if p not in wc_ports and p not in elgato_ports and 'source' not in p.lower()]
            
            selected = personal_ports or wc_ports or elgato_ports or other_ports
            if selected:
                return selected[0].split(':')[0]
        except Exception:
            pass
        return None

    def _open_pw_record(self, node_name: str, target: str = None, channels: int = 2, is_sink: bool = False):
        return open_pw_record(node_name, target=target, channels=channels, is_sink=is_sink)

    def _link_mic_monitor(self):
        """Discovers physical hardware microphone ports and links wave_mic_monitor to them directly."""
        try:
            current_links = set()
            try:
                links_out = subprocess.check_output(['pw-link', '-l'], text=True, stderr=subprocess.DEVNULL)
                current_node = None
                for line in links_out.splitlines():
                    line_str = line.strip()
                    if not line.startswith(' ') and ':' in line_str:
                        current_node = line_str
                    elif '|<-' in line_str and current_node and 'wave_mic_monitor' in current_node:
                        src_port = line_str.replace('|<-', '').strip()
                        # Unlink if not an alsa_input physical hardware port or if it is a monitor port
                        if not src_port.startswith("alsa_input.") or "monitor" in src_port.lower():
                            subprocess.run(['pw-link', '-d', src_port, current_node], stderr=subprocess.DEVNULL)
                        else:
                            current_links.add((src_port, current_node))
            except Exception:
                pass

            # 2. Discover physical alsa_input capture ports
            out = subprocess.check_output(['pw-link', '-o'], text=True, stderr=subprocess.DEVNULL)
            all_ports = [line.strip() for line in out.splitlines() if line.strip().startswith("alsa_input.") and ":capture_" in line.strip()]

            # Prioritize Elgato Wave XLR, then other USB microphones, then PCI
            elgato_ports = [p for p in all_ports if 'wave' in p.lower() or 'elgato' in p.lower()]
            usb_ports = [p for p in all_ports if 'usb' in p.lower() and p not in elgato_ports]
            other_ports = [p for p in all_ports if p not in elgato_ports and p not in usb_ports]

            selected_ports = elgato_ports or usb_ports or other_ports
            if not selected_ports:
                return

            # Check for MONO capture (e.g. Wave XLR capture_MONO)
            mono_port = next((p for p in selected_ports if p.lower().endswith("mono") or "mono" in p.lower()), None)
            if mono_port:
                for dst_port in ('wave_mic_monitor:input_FL', 'wave_mic_monitor:input_FR', 'wave_mic_monitor:input_MONO'):
                    if (mono_port, dst_port) not in current_links:
                        subprocess.run(['pw-link', mono_port, dst_port], stderr=subprocess.DEVNULL)
                return

            # Stereo capture
            fl_port = next((p for p in selected_ports if p.lower().endswith("fl") or p.endswith("1")), selected_ports[0])
            fr_port = next((p for p in selected_ports if p.lower().endswith("fr") or p.endswith("2")), fl_port)

            if fl_port and (fl_port, 'wave_mic_monitor:input_FL') not in current_links:
                subprocess.run(['pw-link', fl_port, 'wave_mic_monitor:input_FL'], stderr=subprocess.DEVNULL)
            if fr_port and (fr_port, 'wave_mic_monitor:input_FR') not in current_links:
                subprocess.run(['pw-link', fr_port, 'wave_mic_monitor:input_FR'], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _link_sink_monitor(self):
        """Discovers active monitor output ports (virtual mix sinks + hardware outputs) and links wave_sink_monitor to them."""
        try:
            # 1. Discover active monitor output ports from virtual mixes and sound cards
            out = subprocess.check_output(['pw-link', '-o'], text=True, stderr=subprocess.DEVNULL)
            ports = [l.strip() for l in out.splitlines() if l.strip()]

            mon_fls = [p for p in ports if p.endswith(':monitor_FL')]
            mon_frs = [p for p in ports if p.endswith(':monitor_FR')]

            # Strict 1:1 invariant: prioritize WaveController_personal_mix_Sink
            primary_fl = None
            primary_fr = None

            for fl in mon_fls:
                if 'personal_mix_sink:monitor_fl' in fl.lower():
                    primary_fl = fl
                    break
            if not primary_fl:
                for fl in mon_fls:
                    if 'wavecontroller' in fl.lower() and 'sink:monitor_fl' in fl.lower():
                        primary_fl = fl
                        break

            for fr in mon_frs:
                if 'personal_mix_sink:monitor_fr' in fr.lower():
                    primary_fr = fr
                    break
            if not primary_fr:
                for fr in mon_frs:
                    if 'wavecontroller' in fr.lower() and 'sink:monitor_fr' in fr.lower():
                        primary_fr = fr
                        break

            # 2. Unlink any extraneous source ports from wave_sink_monitor (strict 1:1 isolation)
            try:
                links_out = subprocess.check_output(['pw-link', '-l'], text=True, stderr=subprocess.DEVNULL)
                current_node = None
                for line in links_out.splitlines():
                    line_str = line.strip()
                    if not line.startswith(' ') and ':' in line_str:
                        current_node = line_str
                    elif '|<-' in line_str and current_node and 'wave_sink_monitor' in current_node:
                        src_port = line_str.replace('|<-', '').strip()
                        if current_node == 'wave_sink_monitor:input_FL' and src_port != primary_fl:
                            subprocess.run(['pw-link', '-d', src_port, current_node], stderr=subprocess.DEVNULL)
                        elif current_node == 'wave_sink_monitor:input_FR' and src_port != primary_fr:
                            subprocess.run(['pw-link', '-d', src_port, current_node], stderr=subprocess.DEVNULL)
                        elif 'input_mono' in current_node.lower():
                            subprocess.run(['pw-link', '-d', src_port, current_node], stderr=subprocess.DEVNULL)
            except Exception:
                pass

            # 3. Check what input ports wave_sink_monitor has and establish strict 1:1 links
            io_out = subprocess.check_output(['pw-link', '-io'], text=True, stderr=subprocess.DEVNULL)
            in_ports = [l.strip() for l in io_out.splitlines() if l.strip().startswith('wave_sink_monitor:')]
            has_fl = any(':input_FL' in p for p in in_ports)
            has_fr = any(':input_FR' in p for p in in_ports)

            if has_fl and primary_fl:
                subprocess.run(['pw-link', primary_fl, 'wave_sink_monitor:input_FL'], stderr=subprocess.DEVNULL)
            if has_fr and primary_fr:
                subprocess.run(['pw-link', primary_fr, 'wave_sink_monitor:input_FR'], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    @staticmethod
    def _calc_perceptual_peak(peak_raw: float, rms: float) -> float:
        return calc_perceptual_peak(peak_raw, rms)

    def _drain_and_calc_peaks(self, proc, channels: int = 2):
        return drain_and_calc_peaks(proc, channels=channels)

    def _refresh_channel_monitors(self):
        """Discovers active WaveController_Channel_* sinks and physical input sources, spawning/pruning per-channel pw-record processes."""
        if not hasattr(self, "_lock") or self._lock is None:
            self._lock = threading.Lock()
        if not hasattr(self, "_refresh_event") or self._refresh_event is None:
            self._refresh_event = threading.Event()
        if not hasattr(self, "_channel_procs"):
            self._channel_procs = {}
        if not hasattr(self, "_channel_proc_channels"):
            self._channel_proc_channels = {}
        if not hasattr(self, "_channel_peaks"):
            self._channel_peaks = {}
        if not hasattr(self, "_target_keys"):
            self._target_keys = {}
        if not hasattr(self, "_target_peaks"):
            self._target_peaks = {}
        if not hasattr(self, "peaks") or self.peaks is None:
            self.peaks = {}

        try:
            out = subprocess.check_output(['pw-link', '-o'], text=True, stderr=subprocess.DEVNULL)
            all_ports = [l.strip() for l in out.splitlines() if l.strip()]
        except Exception:
            return

        # Map channel_id -> (target_node_name, channel_count, is_sink)
        active_channels = {}

        # Collect unique targets: target_node -> {"channels": int, "is_sink": bool, "keys": set()}
        target_map = {}

        # 1. Playback Channels (Permanent Direct Pre-Fader App Stream Monitoring or Exposed Virtual Sinks)
        if self.pipewire_mgr:
            port_meta = self.pipewire_mgr._get_active_port_metadata_map() if hasattr(self.pipewire_mgr, "_get_active_port_metadata_map") else {}
            for ch in getattr(self.pipewire_mgr, "channels", []):
                ch_id = ch.get("id", "")
                if ch.get("type") == "source" or not ch_id:
                    continue

                assigned = self.pipewire_mgr.get_assigned_apps(ch_id) if hasattr(self.pipewire_mgr, "get_assigned_apps") else []

                # 1A. Dedicated Pre-Fader Channel Virtual Ingestion Sinks (if expose_sink is enabled)
                sink_node = f"WaveController_Channel_{ch_id}"
                if any(p.startswith(f"{sink_node}:monitor_") for p in all_ports):
                    if sink_node not in target_map:
                        target_map[sink_node] = {"channels": 2, "is_sink": True, "keys": set()}
                    target_map[sink_node]["keys"].add(ch_id)

                # 1B. Permanent Direct Pre-Fader Application Stream Monitoring (Raw App Output Ports)
                if assigned:
                    for app in assigned:
                        tokens = self.pipewire_mgr._get_match_tokens(app) if hasattr(self.pipewire_mgr, "_get_match_tokens") else set()
                        for p in all_ports:
                            if p.startswith("output.WaveController_") or p.startswith("WaveController_") or ":monitor_" in p:
                                continue
                            if ":output_" in p:
                                matches = False
                                if hasattr(self.pipewire_mgr, "_port_matches_tokens"):
                                    matches = self.pipewire_mgr._port_matches_tokens(p, tokens, port_meta)
                                else:
                                    p_low = p.lower()
                                    matches = any(tok in p_low for tok in tokens if len(tok) >= 3)
                                if matches:
                                    app_node = p.split(":")[0]
                                    if app_node not in target_map:
                                        target_map[app_node] = {"channels": 2, "is_sink": False, "keys": set()}
                                    target_map[app_node]["keys"].add(ch_id)

        # 2. Source Channels
        if self.pipewire_mgr:
            for ch in getattr(self.pipewire_mgr, "channels", []):
                if ch.get("type") == "source":
                    ch_id = ch.get("id", "")
                    ch_id_low = ch_id.lower()
                    ch_name_low = str(ch.get("name", "")).lower()
                    assigned_devs = [str(a).lower() for a in (self.pipewire_mgr.get_assigned_apps(ch_id) if hasattr(self.pipewire_mgr, "get_assigned_apps") else [])]

                    matched_node = None
                    is_mono = True
                    for p in all_ports:
                        if ":capture_" in p and p.startswith("alsa_input."):
                            p_low = p.lower()
                            if "wave" in ch_id_low or "elgato" in ch_id_low or "wave" in ch_name_low:
                                if "wave" in p_low or "elgato" in p_low or "0fd9" in p_low:
                                    matched_node = p.split(":")[0]
                                    is_mono = "mono" in p_low
                                    break
                            elif "fefine" in ch_id_low or "fifine" in ch_id_low or "fefine" in ch_name_low or "fifine" in ch_name_low:
                                if "fifine" in p_low or "fefine" in p_low or "3142" in p_low:
                                    matched_node = p.split(":")[0]
                                    is_mono = "mono" in p_low
                                    break
                            elif any(dev in p_low for dev in assigned_devs if len(dev) >= 3 and dev != "system capture"):
                                matched_node = p.split(":")[0]
                                is_mono = "mono" in p_low
                                break
                            elif ch_id_low in p_low or (len(ch_name_low) >= 3 and ch_name_low in p_low):
                                matched_node = p.split(":")[0]
                                is_mono = "mono" in p_low
                                break

                    if matched_node:
                        if matched_node not in target_map:
                            target_map[matched_node] = {"channels": 1 if is_mono else 2, "is_sink": False, "keys": set()}
                        target_map[matched_node]["keys"].add(ch_id)

        # 3. Direct Physical Hardware Input Device Capture
        if hasattr(self, "hardware_mgr") and self.hardware_mgr:
            for dev_k, dev in getattr(self.hardware_mgr, "discovered_devices", {}).items():
                if dev.get("connected", True):
                    for src in dev.get("sources", []):
                        src_name = src.get("name")
                        if src_name:
                            is_mono = "mono" in src_name.lower()
                            ch_cnt = 1 if is_mono else 2
                            if src_name not in target_map:
                                target_map[src_name] = {"channels": ch_cnt, "is_sink": False, "keys": set()}
                            target_map[src_name]["keys"].add(dev_k)
                            clean_k = dev_k.replace("alsa_card.", "").replace("usb-", "")
                            target_map[src_name]["keys"].add(clean_k)

        # Prune processes for targets no longer active
        with self._lock:
            for target_node in list(self._channel_procs.keys()):
                if target_node not in target_map:
                    proc = self._channel_procs.pop(target_node, None)
                    if proc:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    self._channel_proc_channels.pop(target_node, None)

            # Prune stale target peaks for targets no longer active
            for target_node in list(self._target_peaks.keys()):
                if target_node not in target_map:
                    self._target_peaks.pop(target_node, None)

            self._target_keys = {t: info["keys"] for t, info in target_map.items()}

            # For any channel in pipewire_mgr not mapped to any active target, reset its peaks immediately
            mapped_keys = set()
            for t, info in target_map.items():
                mapped_keys.update(info.get("keys", set()))

            if self.pipewire_mgr:
                for ch in getattr(self.pipewire_mgr, "channels", []):
                    cid = ch.get("id")
                    if cid and cid not in mapped_keys:
                        self._channel_peaks[cid] = {"left": 0.0, "right": 0.0, "peak": 0.0}
                        self.peaks[cid] = {"left": 0.0, "right": 0.0, "peak": 0.0}
                        self.peaks[f"wavecontroller_channel_{cid}"] = {"left": 0.0, "right": 0.0, "peak": 0.0}

        # Spawn processes for new unique target nodes (outside lock)
        for target_node, info in target_map.items():
            with self._lock:
                proc = self._channel_procs.get(target_node)
                is_running = proc and proc.poll() is None
            if is_running:
                continue

            clean_tag = re.sub(r'[^a-zA-Z0-9_-]', '_', target_node.replace("alsa_input.", "").replace("input.WaveController_submix_", "")).strip('_')
            node_name = f"wave_meter_{clean_tag[:28]}"
            new_proc = self._open_pw_record(node_name, target=target_node, channels=info["channels"], is_sink=info["is_sink"])
            if new_proc:
                with self._lock:
                    self._channel_procs[target_node] = new_proc
                    self._channel_proc_channels[target_node] = info["channels"]
                    if target_node not in self._target_peaks:
                        self._target_peaks[target_node] = {"left": 0.0, "right": 0.0}

        # Explicit 1:1 Patch Linking & Ingestion Audit (Sever WirePlumber fallbacks & rogue links)
        self._link_and_audit_channel_monitors(target_map, all_ports)

    def _link_and_audit_channel_monitors(self, target_map: dict, all_ports: list):
        """
        Explicitly links per-channel meters directly to their designated submix or source monitor ports,
        and severs any rogue or WirePlumber fallback links (e.g. from WaveController_personal_mix_Sink).
        """
        try:
            target_to_meter = {}
            authorized_sources = {}

            for target_node, info in target_map.items():
                clean_tag = re.sub(r'[^a-zA-Z0-9_-]', '_', target_node.replace("alsa_input.", "").replace("input.WaveController_submix_", "")).strip('_')
                node_name = f"wave_meter_{clean_tag[:28]}"
                target_to_meter[target_node] = node_name

                src_ports = set()
                if info.get("is_sink", False):
                    for p in all_ports:
                        if p.startswith(f"{target_node}:") and ":monitor_" in p:
                            src_ports.add(p)
                else:
                    for p in all_ports:
                        if p.startswith(f"{target_node}:") and (":capture_" in p or ":output_" in p):
                            src_ports.add(p)

                authorized_sources[node_name] = src_ports

            # 1. Ingestion Audit: Check existing links and sever rogue ones
            existing_meter_links = {}
            try:
                links_out = subprocess.check_output(['pw-link', '-l'], text=True, stderr=subprocess.DEVNULL)
                current_meter = None
                for line in links_out.splitlines():
                    line_str = line.strip()
                    if not line.startswith(' ') and ':' in line_str:
                        current_meter = line_str
                        if current_meter.startswith("wave_meter_"):
                            existing_meter_links[current_meter] = set()
                    elif '|<-' in line_str and current_meter and current_meter.startswith("wave_meter_"):
                        dest_node = current_meter.split(':')[0]
                        src_port = line_str.replace('|<-', '').strip()
                        valid_srcs = authorized_sources.get(dest_node, set())
                        if src_port not in valid_srcs:
                            subprocess.run(['pw-link', '-d', src_port, current_meter], stderr=subprocess.DEVNULL)
                        else:
                            existing_meter_links.setdefault(current_meter, set()).add(src_port)
            except Exception:
                pass

            # 2. Establish explicit 1:1 patch links ONLY if not already established
            for target_node, info in target_map.items():
                node_name = target_to_meter.get(target_node)
                src_ports = authorized_sources.get(node_name, set())
                if not src_ports:
                    continue

                if info.get("channels", 2) == 1:
                    mono_src = next((p for p in src_ports if "mono" in p.lower()), list(src_ports)[0])
                    dest_mono = f"{node_name}:input_MONO"
                    if mono_src not in existing_meter_links.get(dest_mono, set()):
                        subprocess.run(['pw-link', mono_src, dest_mono], stderr=subprocess.DEVNULL)
                        subprocess.run(['pw-link', mono_src, f"{node_name}:input_FL"], stderr=subprocess.DEVNULL)
                        subprocess.run(['pw-link', mono_src, f"{node_name}:input_FR"], stderr=subprocess.DEVNULL)
                else:
                    fl_src = next((p for p in src_ports if p.lower().endswith("fl") or p.endswith("1") or "_l" in p.lower()), None)
                    fr_src = next((p for p in src_ports if p.lower().endswith("fr") or p.endswith("2") or "_r" in p.lower()), None)

                    if fl_src:
                        dest_fl = f"{node_name}:input_FL"
                        if fl_src not in existing_meter_links.get(dest_fl, set()):
                            subprocess.run(['pw-link', fl_src, dest_fl], stderr=subprocess.DEVNULL)
                    if fr_src:
                        dest_fr = f"{node_name}:input_FR"
                        if fr_src not in existing_meter_links.get(dest_fr, set()):
                            subprocess.run(['pw-link', fr_src, dest_fr], stderr=subprocess.DEVNULL)
                    if not fl_src and not fr_src and src_ports:
                        for sp in src_ports:
                            dest_fl = f"{node_name}:input_FL"
                            dest_fr = f"{node_name}:input_FR"
                            if sp not in existing_meter_links.get(dest_fl, set()):
                                subprocess.run(['pw-link', sp, dest_fl], stderr=subprocess.DEVNULL)
                            if sp not in existing_meter_links.get(dest_fr, set()):
                                subprocess.run(['pw-link', sp, dest_fr], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _run_discovery_loop(self):
        """Background asynchronous graph discovery worker that audits links and manages pw-record processes."""
        while self.running:
            self._refresh_event.wait(timeout=2.0)
            if not self.running:
                break
            self._refresh_event.clear()
            try:
                self._do_refresh_discovery()
            except Exception:
                pass

    def _do_refresh_discovery(self):
        """Executes full PipeWire target discovery, process management, and link auditing in the background."""
        self._refresh_channel_monitors()

        curr_target, curr_ch = self._discover_mic_target()
        with self._lock:
            need_mic_restart = (curr_target != getattr(self, "mic_target", None) or not self.mic_proc or self.mic_proc.poll() is not None)
            old_mic_proc = self.mic_proc if need_mic_restart else None

        if need_mic_restart:
            if old_mic_proc:
                try:
                    old_mic_proc.terminate()
                except Exception:
                    pass
            new_mic_proc = self._open_pw_record('wave_mic_monitor', target=curr_target, channels=curr_ch)
            with self._lock:
                self.mic_target = curr_target
                self.mic_channels = curr_ch
                self.mic_proc = new_mic_proc

        curr_sink_target = self._discover_sink_target()
        with self._lock:
            need_sink_restart = (curr_sink_target != getattr(self, "sink_target", None) or not self.sink_proc or self.sink_proc.poll() is not None)
            old_sink_proc = self.sink_proc if need_sink_restart else None

        if need_sink_restart:
            if old_sink_proc:
                try:
                    old_sink_proc.terminate()
                except Exception:
                    pass
            new_sink_proc = self._open_pw_record('wave_sink_monitor', target=curr_sink_target, channels=2, is_sink=True)
            with self._lock:
                self.sink_target = curr_sink_target
                self.sink_proc = new_sink_proc

        self._link_mic_monitor()
        self._link_sink_monitor()

    def _run_capture_loop(self):
        """Pure real-time 40 FPS audio capture and peak calculation loop without blocking subprocess calls."""
        mic_l, mic_r = 0.0, 0.0
        sink_l, sink_r = 0.0, 0.0

        while self.running:
            try:
                with self._lock:
                    m_proc = self.mic_proc
                    m_ch = getattr(self, "mic_channels", 2)
                    s_proc = self.sink_proc
                    ch_items = list(self._channel_procs.items())

                raw_ml, raw_mr = self._drain_and_calc_peaks(m_proc, channels=m_ch)
                raw_sl, raw_sr = self._drain_and_calc_peaks(s_proc, channels=2)

                # Read per-channel monitor peaks for all active channel sinks
                for target_node, proc in ch_items:
                    if proc and proc.poll() is None:
                        proc_ch = self._channel_proc_channels.get(target_node, 2)
                        raw_cl, raw_cr = self._drain_and_calc_peaks(proc, channels=proc_ch)
                        cur = getattr(self, "_target_peaks", {}).get(target_node, {"left": 0.0, "right": 0.0})
                        cl = cur.get("left", 0.0)
                        cr = cur.get("right", 0.0)

                        if raw_cl > cl:
                            cl = cl + (raw_cl - cl) * 0.85
                        else:
                            cl = max(0.0, cl * 0.965 - 0.0008)

                        if raw_cr > cr:
                            cr = cr + (raw_cr - cr) * 0.85
                        else:
                            cr = max(0.0, cr * 0.965 - 0.0008)

                        val_l = 0.0 if cl < 0.002 else cl
                        val_r = 0.0 if cr < 0.002 else cr
                        peak_data = {"left": val_l, "right": val_r, "peak": max(val_l, val_r)}
                        if not hasattr(self, "_target_peaks"):
                            self._target_peaks = {}
                        self._target_peaks[target_node] = peak_data

                # Aggregate per-channel peaks across all monitored targets
                new_channel_peaks = {}
                for target_node, peak_data in getattr(self, "_target_peaks", {}).items():
                    assoc_keys = getattr(self, "_target_keys", {}).get(target_node, set())
                    for k in assoc_keys:
                        if k not in new_channel_peaks:
                            new_channel_peaks[k] = {"left": peak_data["left"], "right": peak_data["right"], "peak": peak_data["peak"]}
                        else:
                            new_channel_peaks[k]["left"] = max(new_channel_peaks[k]["left"], peak_data["left"])
                            new_channel_peaks[k]["right"] = max(new_channel_peaks[k]["right"], peak_data["right"])
                            new_channel_peaks[k]["peak"] = max(new_channel_peaks[k]["peak"], peak_data["peak"])
                    new_channel_peaks[target_node] = peak_data

                with self._lock:
                    self._channel_peaks = new_channel_peaks

                # Detect if any process exited unexpectedly and trigger background discovery
                proc_dead = False
                for target_node, proc in ch_items:
                    if proc and proc.poll() is not None:
                        proc_dead = True
                        break
                if proc_dead or (not m_proc or m_proc.poll() is not None) or (not s_proc or s_proc.poll() is not None):
                    self._refresh_event.set()

                # Fast attack (instant punch on rise) + smooth exponential release & graceful fade-down to 0
                if raw_ml > mic_l:
                    mic_l = mic_l + (raw_ml - mic_l) * 0.85
                else:
                    mic_l = max(0.0, mic_l * 0.965 - 0.0008)

                if raw_mr > mic_r:
                    mic_r = mic_r + (raw_mr - mic_r) * 0.85
                else:
                    mic_r = max(0.0, mic_r * 0.965 - 0.0008)

                if raw_sl > sink_l:
                    sink_l = sink_l + (raw_sl - sink_l) * 0.85
                else:
                    sink_l = max(0.0, sink_l * 0.965 - 0.0008)

                if raw_sr > sink_r:
                    sink_r = sink_r + (raw_sr - sink_r) * 0.85
                else:
                    sink_r = max(0.0, sink_r * 0.965 - 0.0008)

                # Gentle zero clamp only at true bottom
                m_l = 0.0 if mic_l < 0.002 else mic_l
                m_r = 0.0 if mic_r < 0.002 else mic_r
                s_l = 0.0 if sink_l < 0.002 else sink_l
                s_r = 0.0 if sink_r < 0.002 else sink_r

                with self._lock:
                    self._last_sink_peaks = {"left": s_l, "right": s_r, "peak": max(s_l, s_r)}
                    self._last_mic_peaks = {"left": m_l, "right": m_r, "peak": max(m_l, m_r)}

                    # Physical microphone channels ONLY get physical microphone level
                    for ch in ["mic", "microphone", "elgato_wave_xlr", "wave_xlr", "input", "system_capture"]:
                        self.peaks[ch] = {"left": m_l, "right": m_r, "peak": max(m_l, m_r)}

                    # 1. Personal Mix bus receives direct hardware sink monitor levels
                    personal_peak = {"left": s_l, "right": s_r, "peak": max(s_l, s_r)}
                    self.peaks["personal_mix"] = personal_peak
                    self.peaks["personal"] = personal_peak

                    # 2. Dynamic mix bus peaks: accurately aggregate only routed, unmuted channels for each mix (Strict Zero-Bleed)
                    if self.pipewire_mgr:
                        try:
                            mixes_list = getattr(self.pipewire_mgr, "mixes", [])
                            channels_list = getattr(self.pipewire_mgr, "channels", [])
                            ch_states = getattr(self.pipewire_mgr, "channel_states", {})
                            master_states = getattr(self.pipewire_mgr, "channel_master_states", {})
                            mx_states = getattr(self.pipewire_mgr, "mix_states", {})

                            for mx in mixes_list:
                                mx_id = mx.get("id", "")
                                if not mx_id or mx_id in ("personal", "personal_mix"):
                                    continue

                                mx_st = mx_states.get(mx_id, {})
                                if mx_st.get("muted", False):
                                    mix_peak = {"left": 0.0, "right": 0.0, "peak": 0.0}
                                    self.peaks[mx_id] = mix_peak
                                    self.peaks[mx_id.replace("_mix", "")] = mix_peak
                                    continue

                                mx_vol_frac = max(0.0, min(1.5, mx_st.get("volume", 100) / 100.0))
                                if mx_vol_frac <= 0.001:
                                    mix_peak = {"left": 0.0, "right": 0.0, "peak": 0.0}
                                    self.peaks[mx_id] = mix_peak
                                    self.peaks[mx_id.replace("_mix", "")] = mix_peak
                                    continue

                                mix_accum_l = 0.0
                                mix_accum_r = 0.0

                                for ch in channels_list:
                                    ch_id = ch.get("id", "")
                                    if not ch_id:
                                        continue

                                    # Check if channel is enabled for this mix
                                    st = ch_states.get(ch_id, {}).get(mx_id, {})
                                    if not st.get("enabled", True) or st.get("muted", False):
                                        continue

                                    # Check master channel mute
                                    m_st = master_states.get(ch_id, {})
                                    if m_st.get("muted", False):
                                        continue

                                    # Calculate volume attenuation
                                    ch_sub_vol = max(0.0, min(1.5, st.get("volume", 80) / 100.0))
                                    ch_master_vol = max(0.0, min(1.5, m_st.get("volume", 80) / 100.0))
                                    ch_scale = ch_sub_vol * ch_master_vol * mx_vol_frac

                                    # Obtain channel level
                                    is_source = (ch.get("type") == "source") or any(k in ch_id.lower() for k in ("mic", "microphone", "elgato_wave", "wave_xlr", "capture_mono"))
                                    if is_source:
                                        c_l, c_r = m_l, m_r
                                    else:
                                        cp = self._channel_peaks.get(ch_id, {"left": 0.0, "right": 0.0})
                                        c_l = cp.get("left", 0.0)
                                        c_r = cp.get("right", 0.0)

                                    mix_accum_l = max(mix_accum_l, c_l * ch_scale)
                                    mix_accum_r = max(mix_accum_r, c_r * ch_scale)

                                mix_accum_l = min(1.0, mix_accum_l)
                                mix_accum_r = min(1.0, mix_accum_r)
                                mix_peak_val = max(mix_accum_l, mix_accum_r)
                                mix_data = {"left": mix_accum_l, "right": mix_accum_r, "peak": mix_peak_val}

                                self.peaks[mx_id] = mix_data
                                short_name = mx_id.replace("_mix", "")
                                if short_name != mx_id:
                                    self.peaks[short_name] = mix_data
                        except Exception:
                            pass
                    else:
                        for non_p in ["chat_mix", "chat", "mobo_mix", "mobo", "stream_mix", "stream"]:
                            self.peaks[non_p] = {"left": 0.0, "right": 0.0, "peak": 0.0}

                    # Explicitly update per-channel ingestion peaks and zero-out inactive channels
                    if self.pipewire_mgr:
                        for ch in getattr(self.pipewire_mgr, "channels", []):
                            cid = ch.get("id")
                            if not cid or ch.get("type") == "source":
                                continue
                            ch_p = self._channel_peaks.get(cid, {"left": 0.0, "right": 0.0, "peak": 0.0})
                            self.peaks[cid] = ch_p
                            self.peaks[f"wavecontroller_channel_{cid}"] = ch_p
                    else:
                        for ch_id, ch_p in self._channel_peaks.items():
                            self.peaks[ch_id] = ch_p
                            self.peaks[f"wavecontroller_channel_{ch_id}"] = ch_p
            except Exception:
                pass

            time.sleep(0.025) # 40 FPS

    def get_channel_stereo_peaks(self, channel_id: str) -> tuple:
        with self._lock:
            ch_low = str(channel_id).lower().strip()

            # 1. Primary Physical Microphone Channels & Source Channels
            if ch_low in ("mic", "elgato_wave_xlr", "wave_xlr", "microphone", "input", "system_capture"):
                mic_p = getattr(self, "_last_mic_peaks", {})
                if mic_p:
                    return mic_p.get("left", 0.0), mic_p.get("right", 0.0)

            if self.pipewire_mgr:
                for ch in getattr(self.pipewire_mgr, "channels", []):
                    if ch.get("id", "").lower() == ch_low and ch.get("type") == "source":
                        mic_p = getattr(self, "_last_mic_peaks", {})
                        if mic_p:
                            return mic_p.get("left", 0.0), mic_p.get("right", 0.0)

            # 2. Per-Channel Dedicated Isolated VU Meter Process Peaks
            if ch_low in self._channel_peaks:
                p = self._channel_peaks[ch_low]
                return p.get("left", 0.0), p.get("right", 0.0)

            # Return 0.0 for quiet, unassigned, or secondary mics — Strict Zero Cross-Bleed!
            return 0.0, 0.0

    def get_channel_peak(self, channel_id: str) -> float:
        l, r = self.get_channel_stereo_peaks(channel_id)
        return max(l, r)

    def get_all_peaks(self) -> dict:
        with self._lock:
            return dict(self.peaks)

    def on_system_resume(self):
        """Refreshes peak monitor processes and re-links VU monitor streams after system wake."""
        self._refresh_event.set()

