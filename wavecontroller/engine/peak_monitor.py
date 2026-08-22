import os
import subprocess
import threading
import time
import array
import fcntl
import select

class MultiChannelPeakMonitor:
    """
    Captures real-time stereo (Left and Right) audio peaks across physical microphones
    and system playback audio streams.
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

    def _open_capture(self, device: str):
        cmd = [
            'parecord',
            '--raw',
            '--format=s16le',
            '--channels=2',
            '--rate=44100',
            '--latency-msec=30',
            '--process-time-msec=10',
            '--property=application.id=org.WaveController.PeakMonitor',
            f'--device={device}'
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            fd = proc.stdout.fileno()
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            return proc
        except Exception:
            return None

    def _read_stereo_peaks(self, proc):
        peak_l = 0.0
        peak_r = 0.0
        if proc and proc.poll() is None:
            try:
                ready, _, _ = select.select([proc.stdout.fileno()], [], [], 0.01)
                if ready:
                    data = proc.stdout.read(4096)
                    if data and len(data) >= 4:
                        if len(data) % 2 != 0:
                            data = data[:-1]
                        samples = array.array('h', data)
                        if len(samples) >= 2:
                            lefts = samples[0::2]
                            rights = samples[1::2]
                            
                            max_l = max(max(lefts), -min(lefts)) / 32768.0 * 1.5
                            max_r = max(max(rights), -min(rights)) / 32768.0 * 1.5
                            
                            peak_l = min(1.0, max_l)
                            peak_r = min(1.0, max_r)
            except Exception:
                pass
        return peak_l, peak_r

    def _run_capture_loop(self):
        self.mic_proc = self._open_capture('@DEFAULT_SOURCE@')
        self.sink_proc = self._open_capture('@DEFAULT_SINK@.monitor')

        mic_l, mic_r = 0.0, 0.0
        sink_l, sink_r = 0.0, 0.0
        decay = 0.85

        while self.running:
            raw_ml, raw_mr = self._read_stereo_peaks(self.mic_proc)
            raw_sl, raw_sr = self._read_stereo_peaks(self.sink_proc)

            mic_l = max(raw_ml, mic_l * decay)
            mic_r = max(raw_mr, mic_r * decay)
            sink_l = max(raw_sl, sink_l * decay)
            sink_r = max(raw_sr, sink_r * decay)

            with self._lock:
                self.peaks["mic"] = {"left": mic_l, "right": mic_r}
                self.peaks["music"] = {"left": sink_l, "right": sink_r}
                self.peaks["game"] = {"left": sink_l * 0.9, "right": sink_r * 0.9}
                self.peaks["chat"] = {"left": 0.0, "right": 0.0}
                self.peaks["sfx"] = {"left": 0.0, "right": 0.0}
                self.peaks["browser"] = {"left": sink_l * 0.8, "right": sink_r * 0.8}
                self.peaks["system"] = {"left": sink_l, "right": sink_r}

            time.sleep(0.025) # 40 FPS refresh rate

    def get_channel_stereo_peaks(self, channel_id: str) -> tuple:
        with self._lock:
            p = self.peaks.get(channel_id, {"left": 0.0, "right": 0.0})
            return p.get("left", 0.0), p.get("right", 0.0)

    def get_channel_peak(self, channel_id: str) -> float:
        with self._lock:
            p = self.peaks.get(channel_id, {"left": 0.0, "right": 0.0})
            return max(p.get("left", 0.0), p.get("right", 0.0))

    def get_all_peaks(self) -> dict:
        with self._lock:
            return dict(self.peaks)
