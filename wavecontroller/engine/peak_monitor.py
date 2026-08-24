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
    """
    def __init__(self):
        self.peaks = {} # {channel_id: {"left": float, "right": float}}
        self.running = False
        self.mic_proc = None
        self.sink_proc = None
        self.thread = None
        self._lock = threading.Lock()

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

    def _open_pw_record(self, node_name: str):
        cmd = [
            'pw-record',
            '-P', f'node.name={node_name}',
            '--raw',
            '--format=s16',
            '--rate=48000',
            '--channels=2',
            '--latency=20ms',
            '-'
        ]
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
        """Discovers active monitor output ports and links wave_sink_monitor to them."""
        try:
            # 1. Unlink any default microphone capture ports that WirePlumber auto-linked to wave_sink_monitor
            try:
                links_out = subprocess.check_output(['pw-link', '-l'], text=True, stderr=subprocess.DEVNULL)
                current_node = None
                for line in links_out.splitlines():
                    line_str = line.strip()
                    if not line.startswith(' ') and ':' in line_str:
                        current_node = line_str
                    elif '|<-' in line_str and current_node and 'wave_sink_monitor' in current_node:
                        src_port = line_str.replace('|<-', '').strip()
                        if 'capture' in src_port.lower():
                            subprocess.run(['pw-link', '-d', src_port, current_node], stderr=subprocess.DEVNULL)
            except Exception:
                pass

            # 2. Discover active monitor output ports from sound cards and virtual mixes
            out = subprocess.check_output(['pw-link', '-o'], text=True, stderr=subprocess.DEVNULL)
            mon_fl = None
            mon_fr = None
            
            # Find matching monitor ports (USB IEC958, PCI analog, or virtual mix sinks)
            candidate_fls = []
            candidate_frs = []
            for line in out.splitlines():
                l = line.strip()
                if l.endswith(':monitor_FL'):
                    candidate_fls.append(l)
                elif l.endswith(':monitor_FR'):
                    candidate_frs.append(l)

            # Prioritize: 1. Active hardware sink (iec958 / usb / pci) 2. WaveController personal mix
            for fl in candidate_fls:
                if 'iec958' in fl.lower() or 'usb' in fl.lower() or 'analog' in fl.lower() or 'pci' in fl.lower():
                    mon_fl = fl
                    break
            if not mon_fl and candidate_fls:
                mon_fl = candidate_fls[0]

            for fr in candidate_frs:
                if 'iec958' in fr.lower() or 'usb' in fr.lower() or 'analog' in fr.lower() or 'pci' in fr.lower():
                    mon_fr = fr
                    break
            if not mon_fr and candidate_frs:
                mon_fr = candidate_frs[0]

            if mon_fl and mon_fr:
                subprocess.run(['pw-link', mon_fl, 'wave_sink_monitor:input_FL'], stderr=subprocess.DEVNULL)
                subprocess.run(['pw-link', mon_fr, 'wave_sink_monitor:input_FR'], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _drain_and_calc_peaks(self, proc):
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
        if len(combined) < 4:
            return 0.0, 0.0

        if len(combined) % 2 != 0:
            combined = combined[:-1]

        # Analyze the most recent audio window (up to last 8192 bytes ≈ 42ms)
        window = combined[-8192:] if len(combined) > 8192 else combined
        samples = array.array('h', window)
        n_samples = len(samples)
        if n_samples < 2:
            return 0.0, 0.0

        lefts = samples[0::2]
        rights = samples[1::2]

        # 1. RMS Energy (perceived loudness power without waveform phase jitter)
        sum_sq_l = sum(s * s for s in lefts)
        sum_sq_r = sum(s * s for s in rights)
        rms_l = math.sqrt(sum_sq_l / len(lefts)) / 32768.0 if lefts else 0.0
        rms_r = math.sqrt(sum_sq_r / len(rights)) / 32768.0 if rights else 0.0

        # 2. True Peak (dynamic transients and punch)
        peak_raw_l = max(max(lefts), -min(lefts)) / 32768.0 if lefts else 0.0
        peak_raw_r = max(max(rights), -min(rights)) / 32768.0 if rights else 0.0

        # Calibrated 70% RMS + 30% Peak hybrid weighting
        val_l = (rms_l * 2.2 * 0.70) + (peak_raw_l * 1.4 * 0.30)
        val_r = (rms_r * 2.2 * 0.70) + (peak_raw_r * 1.4 * 0.30)

        return max(0.0, min(1.0, val_l)), max(0.0, min(1.0, val_r))

    def _run_capture_loop(self):
        # 1. Open mic capture with unique node name and link to physical hardware mic
        self.mic_proc = self._open_pw_record('wave_mic_monitor')
        time.sleep(0.1)
        self._link_mic_monitor()
        
        # 2. Open playback capture with unique node name and link to active sink monitor
        time.sleep(0.1)
        self.sink_proc = self._open_pw_record('wave_sink_monitor')
        time.sleep(0.15)
        self._link_sink_monitor()

        mic_l, mic_r = 0.0, 0.0
        sink_l, sink_r = 0.0, 0.0

        while self.running:
            try:
                raw_ml, raw_mr = self._drain_and_calc_peaks(self.mic_proc)
                raw_sl, raw_sr = self._drain_and_calc_peaks(self.sink_proc)

                # Re-spawn if exited
                if (not self.mic_proc or self.mic_proc.poll() is not None) and self.running:
                    self.mic_proc = self._open_pw_record('wave_mic_monitor')
                    self._link_mic_monitor()
                if (not self.sink_proc or self.sink_proc.poll() is not None) and self.running:
                    self.sink_proc = self._open_pw_record('wave_sink_monitor')
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
                    # Physical microphone channels (mic, fefine, etc.) ONLY get physical microphone level
                    self.peaks["mic"] = {"left": m_l, "right": m_r, "peak": max(m_l, m_r)}
                    self.peaks["fefine"] = {"left": m_l, "right": m_r, "peak": max(m_l, m_r)}
                    self.peaks["microphone"] = {"left": m_l, "right": m_r, "peak": max(m_l, m_r)}
                    
                    # Application & System Playback channels (Spotify, Discord, Games, etc.) get sink monitor level
                    for ch in ["spotify", "music", "game", "chat", "browser", "system", "sfx", "master"]:
                        self.peaks[ch] = {"left": s_l, "right": s_r, "peak": max(s_l, s_r)}

                    # Mix buses also receive monitor levels
                    for mix in ["personal_mix", "personal", "chat_mix", "mobo_mix", "mobo", "stream_mix"]:
                        self.peaks[mix] = {"left": s_l, "right": s_r, "peak": max(s_l, s_r)}
            except Exception:
                pass

            time.sleep(0.025) # 40 FPS

    def get_channel_stereo_peaks(self, channel_id: str) -> tuple:
        with self._lock:
            ch_low = str(channel_id).lower().strip()
            if ch_low in self.peaks:
                p = self.peaks[ch_low]
                return p.get("left", 0.0), p.get("right", 0.0)
            for k, p in self.peaks.items():
                if k in ch_low or ch_low in k:
                    return p.get("left", 0.0), p.get("right", 0.0)
            # Default fallback for playback channels
            if ch_low not in ("mic", "microphone", "fefine", "input"):
                p = self.peaks.get("system", self.peaks.get("spotify", {}))
                return p.get("left", 0.0), p.get("right", 0.0)
            return (0.0, 0.0)

    def get_channel_peak(self, channel_id: str) -> float:
        l, r = self.get_channel_stereo_peaks(channel_id)
        return max(l, r)

    def get_all_peaks(self) -> dict:
        with self._lock:
            return dict(self.peaks)
