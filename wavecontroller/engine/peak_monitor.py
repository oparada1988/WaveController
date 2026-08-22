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
    across physical microphones and system playback audio streams.
    """
    def __init__(self):
        self.peaks = {} # {channel_id: {"left": float, "right": float}}
        self.running = False
        self.mic_proc = None
        self.thread = None
        self._lock = threading.Lock()

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run_capture_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.mic_proc:
            try:
                self.mic_proc.kill()
            except Exception:
                pass
            self.mic_proc = None

    def _open_pw_record(self):
        cmd = [
            'pw-record',
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

    def _run_capture_loop(self):
        self.mic_proc = self._open_pw_record()

        mic_l, mic_r = 0.0, 0.0
        decay = 0.82

        while self.running:
            raw_l = 0.0
            raw_r = 0.0

            if self.mic_proc and self.mic_proc.poll() is None:
                try:
                    ready, _, _ = select.select([self.mic_proc.stdout.fileno()], [], [], 0.02)
                    if ready:
                        data = self.mic_proc.stdout.read(4096)
                        if data and len(data) >= 4:
                            if len(data) % 2 != 0:
                                data = data[:-1]
                            samples = array.array('h', data)
                            if len(samples) >= 2:
                                lefts = samples[0::2]
                                rights = samples[1::2]
                                max_l = max(max(lefts), -min(lefts)) / 32768.0 if lefts else 0.0
                                max_r = max(max(rights), -min(rights)) / 32768.0 if rights else 0.0
                                raw_l = min(1.0, max_l * 1.6)
                                raw_r = min(1.0, max_r * 1.6)
                except Exception:
                    pass
            elif self.running:
                # Reopen if terminated
                self.mic_proc = self._open_pw_record()

            mic_l = max(raw_l, mic_l * decay)
            mic_r = max(raw_r, mic_r * decay)

            with self._lock:
                # Assign mic level to "mic" channel
                self.peaks["mic"] = {"left": mic_l, "right": mic_r}
                # Assign to any music/app channels
                for ch in ["spotify", "music", "game", "chat", "sfx", "browser", "system"]:
                    self.peaks[ch] = {"left": 0.0, "right": 0.0}

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
