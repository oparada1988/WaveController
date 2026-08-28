"""
High-Speed Audio Capture Driver & Studio Peak Math
==================================================
Reads unbuffered raw PCM streams from PipeWire (pw-record) and converts
linear sample amplitudes into OBS/Wave Link studio-grade decibel perceptual curves.
"""

import os
import math
import array
import fcntl
import subprocess

def calc_perceptual_peak(peak_raw: float, rms: float) -> float:
    """Translates raw linear PCM samples into studio-grade decibel/perceptual meter levels.
    
    Uses an OBS/Wave Link broadcast curve from -54 dBFS (0%) to 0 dBFS (100%):
    - Whisper / Background noise (-46 dB): ~10%
    - Speech / Vocals (-18 dB): ~60% - 65%
    - Mastered music (-12 dB to -6 dB): ~80% - 92%
    - True peak transients / 0 dBFS limit: 95% - 100%
    """
    mag = max(peak_raw, rms * 1.8)
    if mag <= 0.002:
        return 0.0
    if mag <= 0.005:
        return (mag - 0.002) / (0.005 - 0.002) * 0.02
    if mag <= 0.025:
        return 0.02 + ((mag - 0.005) / (0.025 - 0.005)) * 0.18
    if mag <= 0.150:
        return 0.20 + ((mag - 0.025) / (0.150 - 0.025)) * 0.40
    if mag <= 0.450:
        return 0.60 + ((mag - 0.150) / (0.450 - 0.150)) * 0.25
    if mag <= 0.850:
        return 0.85 + ((mag - 0.450) / (0.850 - 0.450)) * 0.10
    
    val = 0.95 + ((min(1.0, mag) - 0.850) / 0.150) * 0.05
    return min(1.0, val)

def open_pw_record(node_name: str, target: str = None, channels: int = 2, is_sink: bool = False):
    """Spawns an unbuffered background pw-record process for real-time VU meter telemetry."""
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
    if is_sink or (target and ('sink' in target.lower() or 'wavecontroller_submix_' in target.lower())):
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

def drain_and_calc_peaks(proc, channels: int = 2) -> tuple:
    """Non-blocking high-speed drain of PCM audio buffer from a running pw-record process."""
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

    # Mono 1-channel capture
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
        val = calc_perceptual_peak(peak_raw, rms)
        return val, val

    # Stereo 2-channel interleaved capture
    sum_sq_l = 0
    sum_sq_r = 0
    peak_val_l = 0
    peak_val_r = 0
    n_pairs = n_samples // 2
    if n_pairs < 1:
        return 0.0, 0.0

    for i in range(0, n_samples - 1, 2):
        sl = samples[i]
        sr = samples[i+1]
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
    peak_l_raw = peak_val_l / 32768.0
    peak_r_raw = peak_val_r / 32768.0

    val_l = calc_perceptual_peak(peak_l_raw, rms_l)
    val_r = calc_perceptual_peak(peak_r_raw, rms_r)
    return val_l, val_r
