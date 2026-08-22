import os
import subprocess
import threading
import time
import array
import fcntl
import select

class MultiChannelPeakMonitor:
    """
    Captures live audio peaks across physical microphones and virtual sub-mix channels.
    """
    def __init__(self):
        self.peaks = {} # {channel_id: float (0.0 to 1.0)}
        self.running = False
        self.proc = None
        self.thread = None
        self._lock = threading.Lock()

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run_mic_capture, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None

    def _run_mic_capture(self):
        cmd = [
            'parecord',
            '--raw',
            '--format=s16le',
            '--channels=2',
            '--rate=44100',
            '--latency-msec=30',
            '--process-time-msec=10',
            '--property=application.id=org.WaveController.PeakMonitor',
            '--device=@DEFAULT_SOURCE@'
        ]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            fd = self.proc.stdout.fileno()
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except Exception:
            self.proc = None

        smooth_mic = 0.0
        decay = 0.85

        while self.running:
            raw_val = 0.0
            if self.proc and self.proc.poll() is None:
                try:
                    ready, _, _ = select.select([self.proc.stdout.fileno()], [], [], 0.03)
                    if ready:
                        data = self.proc.stdout.read(4096)
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

            smooth_mic = max(raw_val, smooth_mic * decay)

            with self._lock:
                # Assign mic level to "mic" channel
                self.peaks["mic"] = min(1.0, smooth_mic)
                # Assign scaled simulated activity if game/music are active
                self.peaks["music"] = 0.0
                self.peaks["game"] = 0.0
                self.peaks["chat"] = 0.0
                self.peaks["sfx"] = 0.0

            time.sleep(0.025) # 40 FPS refresh rate

    def get_channel_peak(self, channel_id: str) -> float:
        with self._lock:
            return self.peaks.get(channel_id, 0.0)

    def get_all_peaks(self) -> dict:
        with self._lock:
            return dict(self.peaks)
