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

def get_multi_stream_numeric_ports(tokens: set, port_meta: dict = None, out_ports: list = None) -> list:
    """
    Returns list of dicts with exact numeric port IDs, channel types ('FL', 'FR', 'MONO'),
    and node names for all active streams matching the application tokens.
    """
    if not tokens:
        return []
        
    if out_ports is None:
        try:
            try:
                out_ports_raw = subprocess.check_output(["pw-link", "-I", "-o"], text=True, stderr=subprocess.DEVNULL)
            except Exception:
                out_ports_raw = ""
            if not out_ports_raw:
                out_ports_raw = subprocess.check_output(["pw-link", "-o"], text=True, stderr=subprocess.DEVNULL)
            out_ports = [l.strip() for l in out_ports_raw.splitlines() if l.strip()]
        except Exception:
            out_ports = []

    results = []
    for p in out_ports:
        # Ignore internal submix loops, meters, and virtual adapters
        clean_p = re.sub(r"^\d+\s+", "", p).strip()
        if clean_p.startswith("output.WaveController_") or clean_p.startswith("WaveController_") or clean_p.startswith("wave_"):
            continue
        if ":output_" not in clean_p and ":capture_" not in clean_p:
            continue

        if port_matches_tokens(clean_p, tokens, port_meta):
            port_id = p.split()[0] if p and p.split()[0].isdigit() else clean_p
            p_low = clean_p.lower()
            if "_fl" in p_low or p_low.endswith("_1") or "_l" in p_low:
                chan = "FL"
            elif "_fr" in p_low or p_low.endswith("_2") or "_r" in p_low:
                chan = "FR"
            elif "mono" in p_low:
                chan = "MONO"
            else:
                chan = "FL"
            
            node_name = clean_p.split(":")[0]
            results.append({
                "port_id": port_id,
                "port_name": clean_p,
                "node_name": node_name,
                "channel": chan
            })

    return results

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

def reconcile_meter_ports(meter_node_name: str, src_stream_ports: list, existing_meter_links: dict = None):
    """
    Idempotently patches all active stream numeric ports to the target meter node (e.g. wave_meter_<ch_id>).
    Allows multi-stream summing for Chromium/Electron so audio peaks are immediately visible on frame 1.
    """
    if not meter_node_name or not src_stream_ports:
        return

    dest_fl = f"{meter_node_name}:input_FL"
    dest_fr = f"{meter_node_name}:input_FR"
    dest_mono = f"{meter_node_name}:input_MONO"

    fl_links = existing_meter_links.get(dest_fl, set()) if existing_meter_links else set()
    fr_links = existing_meter_links.get(dest_fr, set()) if existing_meter_links else set()
    mono_links = existing_meter_links.get(dest_mono, set()) if existing_meter_links else set()

    for item in src_stream_ports:
        p_id = item["port_id"]
        p_name = item["port_name"]
        chan = item["channel"]

        if chan == "MONO":
            if p_name not in mono_links and p_id not in mono_links:
                subprocess.run(["pw-link", str(p_id), dest_mono], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["pw-link", str(p_id), dest_fl], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["pw-link", str(p_id), dest_fr], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif chan == "FL":
            if p_name not in fl_links and p_id not in fl_links:
                subprocess.run(["pw-link", str(p_id), dest_fl], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif chan == "FR":
            if p_name not in fr_links and p_id not in fr_links:
                subprocess.run(["pw-link", str(p_id), dest_fr], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
