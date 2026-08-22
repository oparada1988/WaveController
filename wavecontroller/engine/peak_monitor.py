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

    def _link_sink_monitor(self):
        """Discovers active monitor output ports and links wave_sink_monitor to them."""
        try:
            out = subprocess.check_output(['pw-link', '-o'], text=True, stderr=subprocess.DEVNULL)
            mon_fl = None
            mon_fr = None
            for line in out.splitlines():
                l = line.strip()
                if 'monitor_FL' in l and ('analog' in l or 'pci' in l or 'usb' in l):
                    mon_fl = l
                elif 'monitor_FR' in l and ('analog' in l or 'pci' in l or 'usb' in l):
                    mon_fr = l

            if mon_fl and mon_fr:
                # Unlink default mic capture from sink monitor
                subprocess.run(['pw-link', '-d', 'alsa_input.usb-3142_fifine_Microphone-00.analog-stereo:capture_FL', 'wave_sink_monitor:input_FL'], stderr=subprocess.DEVNULL)
                subprocess.run(['pw-link', '-d', 'alsa_input.usb-3142_fifine_Microphone-00.analog-stereo:capture_FR', 'wave_sink_monitor:input_FR'], stderr=subprocess.DEVNULL)
                # Link to sink monitor
                subprocess.run(['pw-link', mon_fl, 'wave_sink_monitor:input_FL'], stderr=subprocess.DEVNULL)
                subprocess.run(['pw-link', mon_fr, 'wave_sink_monitor:input_FR'], stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _read_peaks_from_proc(self, proc):
        peak_l = 0.0
        peak_r = 0.0
        if proc and proc.poll() is None:
            try:
                ready, _, _ = select.select([proc.stdout.fileno()], [], [], 0.015)
                if ready:
                    data = proc.stdout.read(4096)
                    if data and len(data) >= 4:
                        if len(data) % 2 != 0:
                            data = data[:-1]
                        samples = array.array('h', data)
                        if len(samples) >= 2:
                            lefts = samples[0::2]
                            rights = samples[1::2]
                            max_l = max(max(lefts), -min(lefts)) / 32768.0 if lefts else 0.0
                            max_r = max(max(rights), -min(rights)) / 32768.0 if rights else 0.0
                            peak_l = min(1.0, max_l * 2.2)
                            peak_r = min(1.0, max_r * 2.2)
            except Exception:
                pass
        return peak_l, peak_r

    def _run_capture_loop(self):
        # 1. Open mic capture with unique node name
        self.mic_proc = self._open_pw_record('wave_mic_monitor')
        
        # 2. Open playback capture with unique node name and link to sink monitor
        time.sleep(0.1)
        self.sink_proc = self._open_pw_record('wave_sink_monitor')
        time.sleep(0.15)
        self._link_sink_monitor()

        mic_l, mic_r = 0.0, 0.0
        sink_l, sink_r = 0.0, 0.0
        decay = 0.80

        while self.running:
            raw_ml, raw_mr = self._read_peaks_from_proc(self.mic_proc)
            raw_sl, raw_sr = self._read_peaks_from_proc(self.sink_proc)

            # Re-spawn if exited
            if (not self.mic_proc or self.mic_proc.poll() is not None) and self.running:
                self.mic_proc = self._open_pw_record('wave_mic_monitor')
            if (not self.sink_proc or self.sink_proc.poll() is not None) and self.running:
                self.sink_proc = self._open_pw_record('wave_sink_monitor')
                self._link_sink_monitor()

            mic_l = max(raw_ml, mic_l * decay)
            mic_r = max(raw_mr, mic_r * decay)
            sink_l = max(raw_sl, sink_l * decay)
            sink_r = max(raw_sr, sink_r * decay)

            with self._lock:
                # Microphone channel ONLY gets microphone level
                self.peaks["mic"] = {"left": mic_l, "right": mic_r}
                
                # Application & System Playback channels (Spotify, Discord, Games, etc.) get sink monitor level
                for ch in ["spotify", "music", "game", "chat", "browser", "system", "sfx"]:
                    self.peaks[ch] = {"left": sink_l, "right": sink_r}

            time.sleep(0.025) # 40 FPS

    def get_channel_stereo_peaks(self, channel_id: str) -> tuple:
        with self._lock:
            ch_low = channel_id.lower()
            if ch_low in self.peaks:
                p = self.peaks[ch_low]
                return p.get("left", 0.0), p.get("right", 0.0)
            for k, p in self.peaks.items():
                if k in ch_low or ch_low in k:
                    return p.get("left", 0.0), p.get("right", 0.0)
            return (0.0, 0.0)

    def get_channel_peak(self, channel_id: str) -> float:
        l, r = self.get_channel_stereo_peaks(channel_id)
        return max(l, r)

    def get_all_peaks(self) -> dict:
        with self._lock:
            return dict(self.peaks)
