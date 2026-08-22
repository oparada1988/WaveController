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
    across both physical microphones and system playback audio streams.
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

    def _open_pw_record(self, target: str = None):
        cmd = [
            'pw-record',
            '--raw',
            '--format=s16',
            '--rate=48000',
            '--channels=2',
            '--latency=20ms'
        ]
        if target:
            cmd.append(f'--target={target}')
        cmd.append('-')

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            fd = proc.stdout.fileno()
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            return proc
        except Exception:
            return None

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
        # Open mic capture and sink playback capture
        self.mic_proc = self._open_pw_record('alsa_input.usb-3142_fifine_Microphone-00.analog-stereo')
        self.sink_proc = self._open_pw_record('alsa_output.pci-0000_14_00.4.analog-stereo')

        mic_l, mic_r = 0.0, 0.0
        sink_l, sink_r = 0.0, 0.0
        decay = 0.80

        while self.running:
            raw_ml, raw_mr = self._read_peaks_from_proc(self.mic_proc)
            raw_sl, raw_sr = self._read_peaks_from_proc(self.sink_proc)

            # Re-spawn if exited
            if (not self.mic_proc or self.mic_proc.poll() is not None) and self.running:
                self.mic_proc = self._open_pw_record('alsa_input.usb-3142_fifine_Microphone-00.analog-stereo')
            if (not self.sink_proc or self.sink_proc.poll() is not None) and self.running:
                self.sink_proc = self._open_pw_record('alsa_output.pci-0000_14_00.4.analog-stereo')

            mic_l = max(raw_ml, mic_l * decay)
            mic_r = max(raw_mr, mic_r * decay)
            sink_l = max(raw_sl, sink_l * decay)
            sink_r = max(raw_sr, sink_r * decay)

            with self._lock:
                # 1. Microphone levels
                self.peaks["mic"] = {"left": mic_l, "right": mic_r}
                
                # 2. Application & System Playback levels (Spotify, Discord, Games, etc.)
                for ch in ["spotify", "music", "game", "chat", "browser", "system", "sfx"]:
                    self.peaks[ch] = {"left": sink_l, "right": sink_r}

            time.sleep(0.025) # 40 FPS

    def get_channel_stereo_peaks(self, channel_id: str) -> tuple:
        with self._lock:
            # Match directly or by prefix/substring
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
