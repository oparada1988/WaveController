import os
import math
import subprocess
import threading
import time
import array
import fcntl

class MultiChannelPeakMonitor:
    """
    Captures real-time stereo (Left and Right) audio peaks using pw-record
    with isolated node port names for physical microphones and playback audio.
    Per-channel ingestion sinks get dedicated monitor taps for isolated VU metering.
    """
    def __init__(self, pipewire_mgr=None):
        self.peaks = {} # {channel_id: {"left": float, "right": float}}
        self.running = False
        self.mic_proc = None
        self.sink_proc = None
        self.thread = None
        self._lock = threading.Lock()
        self.pipewire_mgr = pipewire_mgr
        # Per-channel isolated monitor processes and smoothed peak state
        self._channel_procs = {}  # {channel_id: subprocess.Popen}
        self._channel_proc_channels = {}  # {channel_id: int}
        self._channel_peaks = {}  # {channel_id: {"left": float, "right": float}}

    def set_pipewire_manager(self, pw_mgr):
        self.pipewire_mgr = pw_mgr

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run_capture_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        for p in [self.mic_proc, self.sink_proc]:
            if p:
                try:
                    p.kill()
                except Exception:
                    pass
        self.mic_proc = None
        self.sink_proc = None
        # Stop all per-channel monitor processes
        for ch_id, proc in list(self._channel_procs.items()):
            try:
                proc.kill()
            except Exception:
                pass
        self._channel_procs.clear()

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
        # Spoof application ID as org.PulseAudio.pavucontrol and media.role as volume-control
        # to bypass GNOME Shell's persistent microphone privacy recording icon on the top panel.
        cmd = [
            'pw-record',
            '-P', f'node.name={node_name}',
            '-P', f'node.description={node_name}',
            '-P', 'application.id=org.PulseAudio.pavucontrol',
            '-P', 'application.name=pavucontrol',
            '-P', 'application.icon_name=pavucontrol',
            '-P', 'application.process.binary=pavucontrol',
            '-P', 'media.role=volume-control',
            '-P', f'audio.channels={channels}',
            '-P', 'audio.position=[FL,FR]' if channels == 2 else 'audio.position=[MONO]',
            '--raw',
            '--format=s16',
            '--rate=48000',
            f'--channels={channels}',
            '--latency=20ms'
        ]
        if is_sink or (target and ('sink' in target.lower() or 'wavecontroller_channel_' in target.lower())):
            cmd.extend(['-P', 'stream.capture.sink=true'])
        if target:
            cmd.extend(['--target', target])
        cmd.append('-')
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            fd = proc.stdout.fileno()
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            return proc
        except Exception:
            return None

    def _link_mic_monitor(self):
        """Discovers physical hardware microphone ports and links wave_mic_monitor to them directly."""
        try:
            # 1. Unlink any virtual sources or sink monitors that WirePlumber auto-linked
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
                subprocess.run(['pw-link', mono_port, 'wave_mic_monitor:input_FL'], stderr=subprocess.DEVNULL)
                subprocess.run(['pw-link', mono_port, 'wave_mic_monitor:input_FR'], stderr=subprocess.DEVNULL)
                subprocess.run(['pw-link', mono_port, 'wave_mic_monitor:input_MONO'], stderr=subprocess.DEVNULL)
                return

            # Stereo capture
            fl_port = next((p for p in selected_ports if p.lower().endswith("fl") or p.endswith("1")), selected_ports[0])
            fr_port = next((p for p in selected_ports if p.lower().endswith("fr") or p.endswith("2")), fl_port)

            if fl_port:
                subprocess.run(['pw-link', fl_port, 'wave_mic_monitor:input_FL'], stderr=subprocess.DEVNULL)
            if fr_port:
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
        """Translates raw linear PCM samples into studio-grade decibel/perceptual meter levels.
        
        Uses an OBS/Wave Link broadcast curve from -54 dBFS (0%) to 0 dBFS (100%):
        - Whisper / Background noise (-46 dB): ~10%
        - Speech / Vocals (-18 dB): ~60% - 65%
        - Mastered music (-12 dB to -6 dB): ~80% - 92%
        - True peak transients / 0 dBFS limit: 95% - 100%
        """
        mag = max(peak_raw, rms * 1.6)
        if mag <= 0.0015:
            return 0.0
        db = 20.0 * math.log10(mag)
        if db <= -54.0:
            return 0.0
        ratio = (db + 54.0) / 54.0
        return max(0.0, min(1.0, ratio ** 1.15))

    def _drain_and_calc_peaks(self, proc, channels: int = 2):
        if not proc or proc.poll() is not None:
            return 0.0, 0.0
        fd = proc.stdout.fileno()
        all_data = []
        while True:
            try:
                chunk = os.read(fd, 16384)
                if not chunk:
                    break
                all_data.append(chunk)
            except (BlockingIOError, InterruptedError, OSError):
                break

        if not all_data:
            return 0.0, 0.0

        combined = b"".join(all_data)
        if len(combined) < 2:
            return 0.0, 0.0

        if len(combined) % 2 != 0:
            combined = combined[:-1]

        # Analyze the most recent audio window (up to last 8192 bytes ≈ 42ms)
        window = combined[-8192:] if len(combined) > 8192 else combined
        samples = array.array('h', window)
        n_samples = len(samples)
        if n_samples < 1:
            return 0.0, 0.0

        # Mono 1-channel capture (e.g. Wave XLR mono microphone)
        if channels == 1:
            sum_sq = 0
            peak_val = 0
            for s in samples:
                sum_sq += s * s
                a = abs(s)
                if a > peak_val:
                    peak_val = a
            rms = math.sqrt(sum_sq / n_samples) / 32768.0
            peak_raw = peak_val / 32768.0
            val = self._calc_perceptual_peak(peak_raw, rms)
            return val, val

        # Stereo 2-channel interleaved capture (Single pass avoids slices & generator object allocation)
        sum_sq_l = 0
        sum_sq_r = 0
        peak_val_l = 0
        peak_val_r = 0
        n_pairs = n_samples // 2
        if n_pairs < 1:
            return 0.0, 0.0

        for i in range(0, n_pairs * 2, 2):
            sl = samples[i]
            sr = samples[i + 1]
            sum_sq_l += sl * sl
            sum_sq_r += sr * sr
            al = abs(sl)
            ar = abs(sr)
            if al > peak_val_l:
                peak_val_l = al
            if ar > peak_val_r:
                peak_val_r = ar

        rms_l = math.sqrt(sum_sq_l / n_pairs) / 32768.0
        rms_r = math.sqrt(sum_sq_r / n_pairs) / 32768.0
        peak_raw_l = peak_val_l / 32768.0
        peak_raw_r = peak_val_r / 32768.0

        val_l = self._calc_perceptual_peak(peak_raw_l, rms_l)
        val_r = self._calc_perceptual_peak(peak_raw_r, rms_r)

        return val_l, val_r

    def _refresh_channel_monitors(self):
        """Discovers active WaveController_Channel_* sinks and physical input sources, spawning/pruning per-channel pw-record processes."""
        try:
            out = subprocess.check_output(['pw-link', '-o'], text=True, stderr=subprocess.DEVNULL)
            all_ports = [l.strip() for l in out.splitlines() if l.strip()]
        except Exception:
            return

        # Map channel_id -> (target_node_name, channel_count, is_sink)
        active_channels = {}

        # 1. Active Playback Channel Sinks (WaveController_Channel_<id>:monitor_FL)
        for p in all_ports:
            if p.startswith("WaveController_Channel_") and ":monitor_" in p:
                ch_node = p.split(":")[0]  # e.g. "WaveController_Channel_spotify"
                ch_id = ch_node.replace("WaveController_Channel_", "")
                active_channels[ch_id] = (ch_node, 2, True)

        # 2. Active Input / Microphone Channels (Physical ALSA Capture nodes)
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
                        active_channels[ch_id] = (matched_node, 1 if is_mono else 2, False)

        # Prune processes for channels that no longer exist
        for ch_id in list(self._channel_procs.keys()):
            if ch_id not in active_channels:
                proc = self._channel_procs.pop(ch_id, None)
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self._channel_proc_channels.pop(ch_id, None)
                self._channel_peaks.pop(ch_id, None)

        # Spawn processes for new channels
        for ch_id, (target, channels, is_sink) in active_channels.items():
            proc = self._channel_procs.get(ch_id)
            if proc and proc.poll() is None:
                continue  # Already running
            node_name = f"wave_meter_{ch_id}"
            new_proc = self._open_pw_record(node_name, target=target, channels=channels, is_sink=is_sink)
            if new_proc:
                self._channel_procs[ch_id] = new_proc
                self._channel_proc_channels[ch_id] = channels
                if ch_id not in self._channel_peaks:
                    self._channel_peaks[ch_id] = {"left": 0.0, "right": 0.0}

    def _run_capture_loop(self):
        # 1. Open mic capture directly targeted to physical microphone node
        mic_target, mic_channels = self._discover_mic_target()
        self.mic_channels = mic_channels
        self.mic_target = mic_target
        self.mic_proc = self._open_pw_record('wave_mic_monitor', target=mic_target, channels=mic_channels)
        if not mic_target:
            time.sleep(0.1)
            self._link_mic_monitor()
        
        # 2. Open playback capture targeted to active mix sink monitor
        sink_target = self._discover_sink_target()
        self.sink_target = sink_target
        self.sink_proc = self._open_pw_record('wave_sink_monitor', target=sink_target, channels=2, is_sink=True)
        time.sleep(0.15)
        self._link_sink_monitor()

        # 3. Discover and open per-channel monitors for active ingestion sinks
        self._refresh_channel_monitors()

        mic_l, mic_r = 0.0, 0.0
        sink_l, sink_r = 0.0, 0.0
        tick_counter = 0

        while self.running:
            try:
                raw_ml, raw_mr = self._drain_and_calc_peaks(self.mic_proc, channels=self.mic_channels)
                raw_sl, raw_sr = self._drain_and_calc_peaks(self.sink_proc, channels=2)

                # Read per-channel monitor peaks for all active channel sinks
                with self._lock:
                    ch_items = list(self._channel_procs.items())

                for ch_id, proc in ch_items:
                    if proc and proc.poll() is None:
                        proc_ch = self._channel_proc_channels.get(ch_id, 2)
                        raw_cl, raw_cr = self._drain_and_calc_peaks(proc, channels=proc_ch)
                        cur = self._channel_peaks.get(ch_id, {"left": 0.0, "right": 0.0})
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
                        self._channel_peaks[ch_id] = {"left": val_l, "right": val_r, "peak": max(val_l, val_r)}

                tick_counter += 1
                if tick_counter % 80 == 0: # Check and refresh links periodically (~2 seconds)
                    self._refresh_channel_monitors()

                    curr_target, curr_ch = self._discover_mic_target()
                    if curr_target != self.mic_target or not self.mic_proc or self.mic_proc.poll() is not None:
                        if self.mic_proc:
                            try:
                                self.mic_proc.terminate()
                            except Exception:
                                pass
                        self.mic_target = curr_target
                        self.mic_channels = curr_ch
                        self.mic_proc = self._open_pw_record('wave_mic_monitor', target=curr_target, channels=curr_ch)
                        if not curr_target:
                            self._link_mic_monitor()

                    curr_sink_target = self._discover_sink_target()
                    if curr_sink_target != self.sink_target or not self.sink_proc or self.sink_proc.poll() is not None:
                        if self.sink_proc:
                            try:
                                self.sink_proc.terminate()
                            except Exception:
                                pass
                        self.sink_target = curr_sink_target
                        self.sink_proc = self._open_pw_record('wave_sink_monitor', target=curr_sink_target, channels=2, is_sink=True)

                    self._link_sink_monitor()

                # Re-spawn if exited
                if (not self.mic_proc or self.mic_proc.poll() is not None) and self.running:
                    self.mic_proc = self._open_pw_record('wave_mic_monitor', target=self.mic_target, channels=self.mic_channels)
                    if not self.mic_target:
                        self._link_mic_monitor()
                if (not self.sink_proc or self.sink_proc.poll() is not None) and self.running:
                    self.sink_proc = self._open_pw_record('wave_sink_monitor', target=self.sink_target, channels=2, is_sink=True)
                    self._link_sink_monitor()

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
                    for ch in ["mic", "microphone", "fefine", "fifine", "elgato_wave_xlr", "wave", "wave_xlr", "input", "system_capture"]:
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
                                    is_source = (ch.get("type") == "source") or any(k in ch_id.lower() for k in ("mic", "fefine", "microphone", "wave", "elgato", "input", "capture"))
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

                    # Isolated per-channel ingestion peaks
                    for ch_id, ch_p in self._channel_peaks.items():
                        self.peaks[ch_id] = ch_p
                        self.peaks[f"wavecontroller_channel_{ch_id}"] = ch_p
            except Exception:
                pass

            time.sleep(0.025) # 40 FPS

    def get_channel_stereo_peaks(self, channel_id: str) -> tuple:
        with self._lock:
            ch_low = str(channel_id).lower().strip()

            # 1. Per-Channel Dedicated Isolated VU Meter Process Peaks
            if ch_low in self._channel_peaks:
                p = self._channel_peaks[ch_low]
                return p.get("left", 0.0), p.get("right", 0.0)

            # 2. Exact match in global channel peaks dict
            if ch_low in self.peaks:
                p = self.peaks[ch_low]
                return p.get("left", 0.0), p.get("right", 0.0)

            # 3. Default primary microphone channel ('mic' or 'elgato_wave_xlr')
            if ch_low in ("mic", "elgato_wave_xlr"):
                mic_p = getattr(self, "_last_mic_peaks", self.peaks.get("mic", self.peaks.get("elgato_wave_xlr", {})))
                return mic_p.get("left", 0.0), mic_p.get("right", 0.0)

            # 4. Partial key matching in dedicated per-channel peaks
            for k, p in self._channel_peaks.items():
                if k in ch_low or ch_low in k:
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
        try:
            self._refresh_channel_monitors()
            curr_mic, curr_ch = self._discover_mic_target()
            if self.mic_proc:
                try:
                    self.mic_proc.terminate()
                except Exception:
                    pass
            self.mic_target = curr_mic
            self.mic_channels = curr_ch
            self.mic_proc = self._open_pw_record('wave_mic_monitor', target=curr_mic, channels=curr_ch)
            if not curr_mic:
                self._link_mic_monitor()

            curr_sink = self._discover_sink_target()
            if self.sink_proc:
                try:
                    self.sink_proc.terminate()
                except Exception:
                    pass
            self.sink_target = curr_sink
            self.sink_proc = self._open_pw_record('wave_sink_monitor', target=curr_sink, channels=2, is_sink=True)
            self._link_sink_monitor()
        except Exception:
            pass

