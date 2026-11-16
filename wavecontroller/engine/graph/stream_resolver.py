"""
Stream Resolver & Multi-Instance Router
========================================
Dedicated routing resolver for handling multi-stream processes (Chromium, Electron, multi-tab browsers)
and robust token-based physical device target matching.
Maintains 100% architectural isolation from core volume, mute, and state loops.
"""

import re
import subprocess
from wavecontroller.engine.graph.process_classifier import get_match_tokens, port_matches_tokens

def resolve_physical_device_ports(target_device: str, in_ports: list = None) -> tuple:
    """
    Resolves human-readable device names (e.g. 'Elgato Wave XLR Analog Stereo')
    or ALSA device strings to their corresponding physical playback ports.
    Returns (desired_fl_set, desired_fr_set).
    """
    if in_ports is None:
        try:
            try:
                in_ports_raw = subprocess.check_output(["pw-link", "-I", "-i"], text=True, stderr=subprocess.DEVNULL)
            except Exception:
                in_ports_raw = ""
            if not in_ports_raw:
                in_ports_raw = subprocess.check_output(["pw-link", "-i"], text=True, stderr=subprocess.DEVNULL)
            in_ports = [l.strip() for l in in_ports_raw.splitlines() if l.strip()]
        except Exception:
            in_ports = []

    clean_in_ports = [re.sub(r"^\d+\s+", "", p).strip() for p in in_ports]
    desired_fl = set()
    desired_fr = set()

    if not target_device or target_device == "none":
        return desired_fl, desired_fr

    clean_target = str(target_device).replace("alsa_card.", "").replace("alsa_output.", "").replace("alsa_input.", "").strip().lower()
    if clean_target in ("default", "none") or "wavecontroller" in clean_target:
        wave_ports = [p for p in clean_in_ports if ("wave" in p.lower() or "elgato" in p.lower()) and ":playback_" in p and p.startswith("alsa_output.")]
        if wave_ports:
            clean_target = wave_ports[0].split(":")[0].replace("alsa_output.", "").strip().lower()
        else:
            first_alsa = [p.split(":")[0] for p in clean_in_ports if p.startswith("alsa_output.") and ":playback_" in p]
            if first_alsa:
                clean_target = first_alsa[0].replace("alsa_output.", "").strip().lower()

    dev_tokens = get_match_tokens(clean_target) if clean_target else set()

    for p in clean_in_ports:
        if p.startswith("WaveController_") or p.startswith("output.WaveController_") or p.startswith("input.WaveController_"):
            continue
        if ":playback_" not in p or not p.startswith("alsa_output."):
            continue

        p_low = p.lower()
        matched = False
        if clean_target and clean_target != "default":
            if clean_target in p_low:
                matched = True
            elif dev_tokens and port_matches_tokens(p, dev_tokens):
                matched = True
        elif clean_target == "default":
            first_alsa = [p_clean.split(":")[0] for p_clean in clean_in_ports if p_clean.startswith("alsa_output.") and ":playback_" in p_clean]
            if first_alsa and p.startswith(f"{first_alsa[0]}:"):
                matched = True

        if matched:
            suffix = p.split(":")[-1].lower()
            if "_fl" in suffix or suffix.endswith("_1") or suffix.endswith("_l") or suffix == "playback_0":
                desired_fl.add(p)
            elif "_fr" in suffix or suffix.endswith("_2") or suffix.endswith("_r") or suffix == "playback_1":
                desired_fr.add(p)

    return desired_fl, desired_fr
