import os
import math
import subprocess
import threading
import time
import array
import fcntl
import select

class MultiChannelPeakMonitor:
    """
    Captures real-time stereo (Left and Right) audio peaks using pw-record
    with isolated node port names for physical microphones and playback audio.
    Per-channel ingestion sinks get dedicated monitor taps for isolated VU metering.
    """
    def __init__(self):
        self.peaks = {} # {channel_id: {"left": float, "right": float}}
        self.running = False
        self.mic_proc = None
        self.sink_proc = None
        self.thread = None
        self._lock = threading.Lock()
        # Per-channel isolated monitor processes and smoothed peak state
        self._channel_procs = {}  # {channel_id: subprocess.Popen}
        self._channel_peaks = {}  # {channel_id: {"left": float, "right": float}}

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
            
            wc_ports = [p for p in all_ports if 'wavecontroller' in p.lower() and 'sink' in p.lower()]
            elgato_ports = [p for p in all_ports if 'wave' in p.lower() or 'elgato' in p.lower() or '0fd9' in p.lower()]
            other_ports = [p for p in all_ports if p not in wc_ports and p not in elgato_ports and 'source' not in p.lower()]
            
            selected = wc_ports or elgato_ports or other_ports
            if selected:
                return selected[0].split(':')[0]
        except Exception:
            pass
        return None

    def _open_pw_record(self, node_name: str, target: str = None, channels: int = 2):
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
            # 1. Unlink any default microphone capture ports or virtual source mixes from wave_sink_monitor
            try:
                links_out = subprocess.check_output(['pw-link', '-l'], text=True, stderr=subprocess.DEVNULL)
                current_node = None
                for line in links_out.splitlines():
                    line_str = line.strip()
                    if not line.startswith(' ') and ':' in line_str:
                        current_node = line_str
                    elif '|<-' in line_str and current_node and 'wave_sink_monitor' in current_node:
                        src_port = line_str.replace('|<-', '').strip()
                        # Unlink if capture port or if it is a virtual Source mix (e.g. Chat Mix Source)
                        if 'capture' in src_port.lower() or 'source' in src_port.lower() or 'input' in src_port.lower():
                            subprocess.run(['pw-link', '-d', src_port, current_node], stderr=subprocess.DEVNULL)
            except Exception:
                pass

            # 2. Check what input ports wave_sink_monitor has
            io_out = subprocess.check_output(['pw-link', '-io'], text=True, stderr=subprocess.DEVNULL)
            in_ports = [l.strip() for l in io_out.splitlines() if l.strip().startswith('wave_sink_monitor:')]
            has_fl = any(':input_FL' in p for p in in_ports)
            has_fr = any(':input_FR' in p for p in in_ports)
            has_mono = any(':input_MONO' in p for p in in_ports)

            # 3. Discover active monitor output ports from sound cards and virtual mixes
            out = subprocess.check_output(['pw-link', '-o'], text=True, stderr=subprocess.DEVNULL)
            ports = [l.strip() for l in out.splitlines() if l.strip()]

            # Find matching monitor ports
            mon_fls = [p for p in ports if p.endswith(':monitor_FL')]
            mon_frs = [p for p in ports if p.endswith(':monitor_FR')]
            mon_monos = [p for p in ports if p.endswith(':monitor_MONO') or ':monitor' in p]

            # Prioritize: 1. WaveController Virtual Mix Sinks 2. Elgato Wave XLR / USB 3. PCI / Default Output
            target_fls = []
            target_frs = []

            for fl in mon_fls:
                if 'wavecontroller' in fl.lower() and 'sink' in fl.lower():
                    target_fls.append(fl)
                elif 'wave' in fl.lower() or '0fd9' in fl.lower() or 'elgato' in fl.lower():
                    target_fls.append(fl)
                elif 'usb' in fl.lower() or 'analog' in fl.lower() or 'pci' in fl.lower():
                    target_fls.append(fl)

            for fr in mon_frs:
                if 'wavecontroller' in fr.lower() and 'sink' in fr.lower():
                    target_frs.append(fr)
                elif 'wave' in fr.lower() or '0fd9' in fr.lower() or 'elgato' in fr.lower():
                    target_frs.append(fr)
                elif 'usb' in fr.lower() or 'analog' in fr.lower() or 'pci' in fr.lower():
                    target_frs.append(fr)

            # Link primary virtual mix sinks or primary hardware outputs
            if has_fl:
                for fl in target_fls:
                    if 'wave_sink_monitor' not in fl and 'wave_mic_monitor' not in fl:
                        subprocess.run(['pw-link', fl, 'wave_sink_monitor:input_FL'], stderr=subprocess.DEVNULL)
            if has_fr:
                for fr in target_frs:
                    if 'wave_sink_monitor' not in fr and 'wave_mic_monitor' not in fr:
                        subprocess.run(['pw-link', fr, 'wave_sink_monitor:input_FR'], stderr=subprocess.DEVNULL)

            if has_mono:
                mono_targets = target_fls or mon_monos
                for m in mono_targets:
                    if 'wave_sink_monitor' not in m and 'wave_mic_monitor' not in m:
                        subprocess.run(['pw-link', m, 'wave_sink_monitor:input_MONO'], stderr=subprocess.DEVNULL)
        except Exception:
            pass

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
            sum_sq = sum(s * s for s in samples)
            rms = math.sqrt(sum_sq / len(samples)) / 32768.0 if samples else 0.0
            peak_raw = max(max(samples), -min(samples)) / 32768.0 if samples else 0.0
            val = (rms * 2.2 * 0.70) + (peak_raw * 1.4 * 0.30)
            val = max(0.0, min(1.0, val))
            return val, val

        # Stereo 2-channel interleaved capture
        lefts = samples[0::2]
        rights = samples[1::2]

        sum_sq_l = sum(s * s for s in lefts)
        sum_sq_r = sum(s * s for s in rights)
        rms_l = math.sqrt(sum_sq_l / len(lefts)) / 32768.0 if lefts else 0.0
        rms_r = math.sqrt(sum_sq_r / len(rights)) / 32768.0 if rights else 0.0

        peak_raw_l = max(max(lefts), -min(lefts)) / 32768.0 if lefts else 0.0
        peak_raw_r = max(max(rights), -min(rights)) / 32768.0 if rights else 0.0

        val_l = (rms_l * 2.2 * 0.70) + (peak_raw_l * 1.4 * 0.30)
        val_r = (rms_r * 2.2 * 0.70) + (peak_raw_r * 1.4 * 0.30)

        return max(0.0, min(1.0, val_l)), max(0.0, min(1.0, val_r))

    def _refresh_channel_monitors(self):
        """Discovers active WaveController_Channel_* sinks and spawns/prunes per-channel pw-record processes."""
        try:
            out = subprocess.check_output(['pw-link', '-o'], text=True, stderr=subprocess.DEVNULL)
            all_ports = [l.strip() for l in out.splitlines() if l.strip()]
        except Exception:
            return

        # Find all active channel sink monitor ports (WaveController_Channel_<id>:monitor_FL)
        active_channels = set()
        for p in all_ports:
            if p.startswith("WaveController_Channel_") and ":monitor_" in p:
                ch_node = p.split(":")[0]  # e.g. "WaveController_Channel_spotify"
                ch_id = ch_node.replace("WaveController_Channel_", "")
                active_channels.add(ch_id)

        # Prune processes for channels that no longer exist
        for ch_id in list(self._channel_procs.keys()):
            if ch_id not in active_channels:
                proc = self._channel_procs.pop(ch_id, None)
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self._channel_peaks.pop(ch_id, None)

        # Spawn processes for new channels
        for ch_id in active_channels:
            proc = self._channel_procs.get(ch_id)
            if proc and proc.poll() is None:
                continue  # Already running
            # Spawn a pw-record targeting this channel's sink node
            node_name = f"wave_ch_meter_{ch_id}"
            target = f"WaveController_Channel_{ch_id}"
            new_proc = self._open_pw_record(node_name, target=target, channels=2)
            if new_proc:
                self._channel_procs[ch_id] = new_proc
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
        self.sink_proc = self._open_pw_record('wave_sink_monitor', target=sink_target, channels=2)
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
                        raw_cl, raw_cr = self._drain_and_calc_peaks(proc, channels=2)
                        cur = self._channel_peaks.get(ch_id, {"left": 0.0, "right": 0.0})
                        cl = cur.get("left", 0.0)
                        cr = cur.get("right", 0.0)

                        if raw_cl > cl:
                            cl = cl + (raw_cl - cl) * 0.80
                        else:
                            cl = max(0.0, cl * 0.93 - 0.002)

                        if raw_cr > cr:
                            cr = cr + (raw_cr - cr) * 0.80
                        else:
                            cr = max(0.0, cr * 0.93 - 0.002)

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
                        self.sink_proc = self._open_pw_record('wave_sink_monitor', target=curr_sink_target, channels=2)
                        time.sleep(0.1)

                    self._link_sink_monitor()

                # Re-spawn if exited
                if (not self.mic_proc or self.mic_proc.poll() is not None) and self.running:
                    self.mic_proc = self._open_pw_record('wave_mic_monitor', target=self.mic_target, channels=self.mic_channels)
                    if not self.mic_target:
                        self._link_mic_monitor()
                if (not self.sink_proc or self.sink_proc.poll() is not None) and self.running:
                    self.sink_proc = self._open_pw_record('wave_sink_monitor', target=self.sink_target, channels=2)
                    self._link_sink_monitor()

                # Fast attack (instant punch on rise) + smooth exponential release & graceful fade-down to 0
                if raw_ml > mic_l:
                    mic_l = mic_l + (raw_ml - mic_l) * 0.80
                else:
                    mic_l = max(0.0, mic_l * 0.93 - 0.002)

                if raw_mr > mic_r:
                    mic_r = mic_r + (raw_mr - mic_r) * 0.80
                else:
                    mic_r = max(0.0, mic_r * 0.93 - 0.002)

                if raw_sl > sink_l:
                    sink_l = sink_l + (raw_sl - sink_l) * 0.80
                else:
                    sink_l = max(0.0, sink_l * 0.93 - 0.002)

                if raw_sr > sink_r:
                    sink_r = sink_r + (raw_sr - sink_r) * 0.80
                else:
                    sink_r = max(0.0, sink_r * 0.93 - 0.002)

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

                    # Mix buses receive monitor levels
                    for mix in ["personal_mix", "personal", "chat_mix", "mobo_mix", "mobo", "stream_mix"]:
                        self.peaks[mix] = {"left": s_l, "right": s_r, "peak": max(s_l, s_r)}

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
            
            # 1. Physical Microphone / Input Channels
            is_mic = any(k in ch_low for k in ("mic", "microphone", "wave", "elgato", "fefine", "fifine", "input", "capture"))
            if is_mic:
                if ch_low in self.peaks:
                    p = self.peaks[ch_low]
                    return p.get("left", 0.0), p.get("right", 0.0)
                mic_p = getattr(self, "_last_mic_peaks", self.peaks.get("mic", self.peaks.get("elgato_wave_xlr", {})))
                return mic_p.get("left", 0.0), mic_p.get("right", 0.0)

            # 2. Per-Channel Isolated Ingestion Sinks
            if ch_low in self._channel_peaks:
                p = self._channel_peaks[ch_low]
                return p.get("left", 0.0), p.get("right", 0.0)
            if ch_low in self.peaks:
                p = self.peaks[ch_low]
                return p.get("left", 0.0), p.get("right", 0.0)
            for k, p in self._channel_peaks.items():
                if k in ch_low or ch_low in k:
                    return p.get("left", 0.0), p.get("right", 0.0)
            for k, p in self.peaks.items():
                if k in ch_low or ch_low in k:
                    return p.get("left", 0.0), p.get("right", 0.0)
            
            # Return 0.0 for quiet or unassigned playback channels instead of leaking global sink audio
            return 0.0, 0.0

    def get_channel_peak(self, channel_id: str) -> float:
        l, r = self.get_channel_stereo_peaks(channel_id)
        return max(l, r)

    def get_all_peaks(self) -> dict:
        with self._lock:
            return dict(self.peaks)
