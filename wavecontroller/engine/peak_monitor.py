import os
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
            # 1. Unlink any virtual sources (e.g. WaveController_chat_mix_Source) that WirePlumber auto-linked
            try:
                links_out = subprocess.check_output(['pw-link', '-l'], text=True, stderr=subprocess.DEVNULL)
                current_node = None
                for line in links_out.splitlines():
                    line_str = line.strip()
                    if not line.startswith(' ') and ':' in line_str:
                        current_node = line_str
                    elif '|<-' in line_str and current_node and 'wave_mic_monitor' in current_node:
                        src_port = line_str.replace('|<-', '').strip()
                        # Unlink if not an alsa_input physical hardware port
                        if not src_port.startswith("alsa_input."):
                            subprocess.run(['pw-link', '-d', src_port, current_node], stderr=subprocess.DEVNULL)
            except Exception:
                pass

            # 2. Discover physical alsa_input capture ports
            out = subprocess.check_output(['pw-link', '-o'], text=True, stderr=subprocess.DEVNULL)
            mic_fl = None
            mic_fr = None

            candidate_mic_fls = []
            candidate_mic_frs = []
            for line in out.splitlines():
                l = line.strip()
                if l.startswith("alsa_input.") and ":capture_" in l:
                    if l.endswith("FL") or l.endswith("1") or l.endswith("mono") or l.endswith("stereo"):
                        candidate_mic_fls.append(l)
                    if l.endswith("FR") or l.endswith("2") or l.endswith("stereo"):
                        candidate_mic_frs.append(l)

            # Prioritize usb mic (e.g. fifine / usb) then pci
            for fl in candidate_mic_fls:
                if 'usb' in fl.lower() or 'fifine' in fl.lower():
                    mic_fl = fl
                    break
            if not mic_fl and candidate_mic_fls:
                mic_fl = candidate_mic_fls[0]

            for fr in candidate_mic_frs:
                if 'usb' in fr.lower() or 'fifine' in fr.lower():
                    mic_fr = fr
                    break
            if not mic_fr and candidate_mic_frs:
                mic_fr = candidate_mic_frs[0]

            if mic_fl:
                subprocess.run(['pw-link', mic_fl, 'wave_mic_monitor:input_FL'], stderr=subprocess.DEVNULL)
            if mic_fr:
                subprocess.run(['pw-link', mic_fr, 'wave_mic_monitor:input_FR'], stderr=subprocess.DEVNULL)
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
        if len(samples) < 2:
            return 0.0, 0.0

        lefts = samples[0::2]
        rights = samples[1::2]
        max_l = max(max(lefts), -min(lefts)) / 32768.0 if lefts else 0.0
        max_r = max(max(rights), -min(rights)) / 32768.0 if rights else 0.0

        # Calibrated 1.5x gain boost matching Volume Controller Plus
        peak_l = max(0.0, min(1.0, max_l * 1.5))
        peak_r = max(0.0, min(1.0, max_r * 1.5))
        return peak_l, peak_r

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
            raw_ml, raw_mr = self._drain_and_calc_peaks(self.mic_proc)
            raw_sl, raw_sr = self._drain_and_calc_peaks(self.sink_proc)

            # Re-spawn if exited
            if (not self.mic_proc or self.mic_proc.poll() is not None) and self.running:
                self.mic_proc = self._open_pw_record('wave_mic_monitor')
                self._link_mic_monitor()
            if (not self.sink_proc or self.sink_proc.poll() is not None) and self.running:
                self.sink_proc = self._open_pw_record('wave_sink_monitor')
                self._link_sink_monitor()

            # Fast attack, slow release exponential smoothing for studio console VU ballistics
            mic_l = raw_ml if raw_ml >= mic_l else max(raw_ml, mic_l * 0.85 - 0.002)
            mic_r = raw_mr if raw_mr >= mic_r else max(raw_mr, mic_r * 0.85 - 0.002)
            sink_l = raw_sl if raw_sl >= sink_l else max(raw_sl, sink_l * 0.85 - 0.002)
            sink_r = raw_sr if raw_sr >= sink_r else max(raw_sr, sink_r * 0.85 - 0.002)

            # Noise floor clamp
            m_l = 0.0 if mic_l < 0.005 else mic_l
            m_r = 0.0 if mic_r < 0.005 else mic_r
            s_l = 0.0 if sink_l < 0.005 else sink_l
            s_r = 0.0 if sink_r < 0.005 else sink_r

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
