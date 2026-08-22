import os
import subprocess
import threading
import time
import array
import fcntl
import select

class MultiChannelPeakMonitor:
    """
    Captures live audio peaks across physical microphones and system playback audio streams.
    """
    def __init__(self):
        self.peaks = {} # {channel_id: float (0.0 to 1.0)}
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

    def _read_proc_peak(self, proc):
        raw_val = 0.0
        if proc and proc.poll() is None:
            try:
                ready, _, _ = select.select([proc.stdout.fileno()], [], [], 0.01)
                if ready:
                    data = proc.stdout.read(4096)
                    if data and len(data) >= 2:
                        if len(data) % 2 != 0:
                            data = data[:-1]
                        samples = array.array('h', data)
                        if samples:
                            max_v = max(samples)
                            min_v = min(samples)
                            raw_val = max(max_v, -min_v) / 32768.0 * 1.5
            except Exception:
                pass
        return raw_val

    def _run_capture_loop(self):
        self.mic_proc = self._open_capture('@DEFAULT_SOURCE@')
        self.sink_proc = self._open_capture('@DEFAULT_SINK@.monitor')

        smooth_mic = 0.0
        smooth_sink = 0.0
        decay = 0.85

        while self.running:
            raw_mic = self._read_proc_peak(self.mic_proc)
            raw_sink = self._read_proc_peak(self.sink_proc)

            smooth_mic = max(raw_mic, smooth_mic * decay)
            smooth_sink = max(raw_sink, smooth_sink * decay)

            with self._lock:
                self.peaks["mic"] = min(1.0, smooth_mic)
                self.peaks["music"] = min(1.0, smooth_sink)
                self.peaks["game"] = min(1.0, smooth_sink)
                self.peaks["chat"] = 0.0
                self.peaks["sfx"] = 0.0
                self.peaks["system"] = min(1.0, smooth_sink)

            time.sleep(0.025) # 40 FPS refresh rate

    def get_channel_peak(self, channel_id: str) -> float:
        with self._lock:
            return self.peaks.get(channel_id, 0.0)

    def get_all_peaks(self) -> dict:
        with self._lock:
            return dict(self.peaks)
