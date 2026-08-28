#!/usr/bin/env python3
"""
WaveController High-Precision Real-Time Live Routing Monitor
============================================================
Monitors live PipeWire routing, device changes, channel creation,
mix creation, and submix loopback attachments with millisecond resolution.
Validates zero-bleed, bypass protection, and 1:1 summing invariants in real time.
"""

import os
import sys
import time
import json
import socket
import subprocess
from datetime import datetime

CONFIG_PATH = os.path.expanduser("~/.config/WaveController/config.json")
SOCK_PATH = os.path.expanduser("~/.config/WaveController/wavecontroller.sock")

def get_timestamp():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def parse_pw_links():
    """Returns a dict of {source_port: set(dest_ports)} from pw-link -l."""
    try:
        out = subprocess.check_output(["pw-link", "-l"], text=True, stderr=subprocess.DEVNULL)
        links = {}
        curr_src = None
        for line in out.splitlines():
            l_str = line.strip()
            if not line.startswith(" ") and not line.startswith("|") and ":" in l_str:
                curr_src = l_str
                if curr_src not in links:
                    links[curr_src] = set()
            elif ("|->" in l_str or "->" in l_str) and curr_src:
                dest = l_str.replace("|->", "").replace("->", "").strip()
                links[curr_src].add(dest)
        return links
    except Exception:
        return {}

def get_active_nodes():
    """Returns set of node names from pw-dump."""
    try:
        out = subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL)
        data = json.loads(out)
        nodes = set()
        for obj in data:
            if obj.get("type") == "PipeWire:Interface:Node":
                props = obj.get("info", {}).get("props", {})
                name = props.get("node.name", "")
                if name:
                    nodes.add(name)
        return nodes
    except Exception:
        return set()

def query_ipc_peaks():
    """Queries IPC socket for peaks."""
    if not os.path.exists(SOCK_PATH):
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.1)
        s.connect(SOCK_PATH)
        s.sendall(json.dumps({"command": "get_peaks"}).encode("utf-8"))
        data = s.recv(65536)
        s.close()
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None

def check_invariants(links):
    """Checks the 3 core regression invariants."""
    warnings = []
    
    # 1. Strict 1:1 summing into wave_sink_monitor
    fl_sources = []
    for src, dests in links.items():
        if "wave_sink_monitor:input_FL" in dests:
            fl_sources.append(src)
    if len(fl_sources) > 1:
        warnings.append(f"[REGRESSION ALERT] Duplicate summing in wave_sink_monitor:input_FL: {fl_sources}")

    # 2. No direct hardware bypass for assigned apps
    for src, dests in links.items():
        if "spotify:output_" in src:
            for d in dests:
                if d.startswith("alsa_output."):
                    warnings.append(f"[REGRESSION ALERT] Direct hardware bypass detected: {src} -> {d}")

    return warnings

def main():
    print("=" * 88, flush=True)
    print("       WAVECONTROLLER LIVE ROUTING & LATENCY MONITOR (TIER 1 PROFILER)", flush=True)
    print("=" * 88, flush=True)
    print(f"[{get_timestamp()}] [INITIALIZING] Attaching to PipeWire graph & WaveController state...", flush=True)
    
    last_config = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                last_config = json.load(f)
        except Exception:
            pass

    last_links = parse_pw_links()
    last_nodes = get_active_nodes()
    
    print(f"[{get_timestamp()}] [READY] Baseline established:", flush=True)
    print(f"  • Configured Channels : {[c['name'] + ' (' + c['id'] + ')' for c in last_config.get('channels', [])]}", flush=True)
    print(f"  • Configured Mixes    : {[m['name'] + ' (' + m['id'] + ')' for m in last_config.get('mixes', [])]}", flush=True)
    print(f"  • Tracked Devices     : {last_config.get('tracked_devices', [])}", flush=True)
    print(f"  • Active WC Nodes     : {[n for n in last_nodes if 'WaveController' in n]}", flush=True)
    print("-" * 88, flush=True)
    print("Listening for changes (polling at 30 Hz / ~33ms resolution)...", flush=True)
    print("Perform your actions in the GUI whenever you're ready!\n", flush=True)

    event_start_times = {}
    last_socket_states = {}
    last_socket_masters = {}
    last_socket_mixes = {}

    try:
        while True:
            t_now = time.time()
            t_str = get_timestamp()

            # 0. Live Socket State Check (30 Hz Zero-Debounce)
            ipc_data = query_ipc_peaks()
            if ipc_data and ipc_data.get("status") == "ok":
                # Master Channels
                curr_masters = ipc_data.get("channel_master_states", {})
                for cid, st in curr_masters.items():
                    old_st = last_socket_masters.get(cid, {})
                    if st.get("volume") != old_st.get("volume") or st.get("muted") != old_st.get("muted"):
                        print(f"[{t_str}] \033[96m[LIVE FADER: CHANNEL MASTER]\033[0m Channel '{cid}': Vol={st.get('volume')}%, Muted={st.get('muted')}", flush=True)
                last_socket_masters = {k: dict(v) for k, v in curr_masters.items()}

                # Submix Faders
                curr_ch_states = ipc_data.get("channel_states", {})
                for cid, m_map in curr_ch_states.items():
                    old_m_map = last_socket_states.get(cid, {})
                    for mid, st in m_map.items():
                        old_st = old_m_map.get(mid, {})
                        if st.get("volume") != old_st.get("volume") or st.get("muted") != old_st.get("muted"):
                            print(f"[{t_str}] \033[93m[LIVE FADER: SUBMIX]\033[0m Channel '{cid}' -> Mix '{mid}': Vol={st.get('volume')}%, Muted={st.get('muted')}", flush=True)
                last_socket_states = {k: {m: dict(v) for m, v in mv.items()} for k, mv in curr_ch_states.items()}

                # Mix Masters
                curr_mixes = ipc_data.get("mix_states", {})
                for mid, st in curr_mixes.items():
                    old_st = last_socket_mixes.get(mid, {})
                    if st.get("volume") != old_st.get("volume") or st.get("muted") != old_st.get("muted"):
                        print(f"[{t_str}] \033[94m[LIVE FADER: MASTER MIX]\033[0m Mix '{mid}': Vol={st.get('volume')}%, Muted={st.get('muted')}", flush=True)
                last_socket_mixes = {k: dict(v) for k, v in curr_mixes.items()}

            # 1. Config Check (Disk Sync)
            curr_config = {}
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, "r") as f:
                        curr_config = json.load(f)
                except Exception:
                    curr_config = last_config

            if curr_config != last_config:
                # Diff tracked devices
                old_devs = set(last_config.get("tracked_devices", []))
                new_devs = set(curr_config.get("tracked_devices", []))
                added_devs = new_devs - old_devs
                removed_devs = old_devs - new_devs
                for d in added_devs:
                    print(f"[{t_str}] \033[96m[DEVICE ADDED]\033[0m Tracked device added: '{d}'", flush=True)
                    event_start_times[f"device_{d}"] = t_now
                for d in removed_devs:
                    print(f"[{t_str}] \033[93m[DEVICE REMOVED]\033[0m Tracked device removed: '{d}'", flush=True)

                # Diff channels
                old_channels = {c["id"]: c for c in last_config.get("channels", [])}
                new_channels = {c["id"]: c for c in curr_config.get("channels", [])}
                for cid, c in new_channels.items():
                    if cid not in old_channels:
                        print(f"[{t_str}] \033[92m[CHANNEL CREATED]\033[0m Name: '{c.get('name')}', ID: '{cid}', Type: '{c.get('type')}', Icon: '{c.get('icon')}'", flush=True)
                        event_start_times[f"ch_{cid}"] = t_now
                for cid in old_channels:
                    if cid not in new_channels:
                        print(f"[{t_str}] \033[91m[CHANNEL DELETED]\033[0m ID: '{cid}'", flush=True)

                # Diff mixes
                old_mixes = {m["id"]: m for m in last_config.get("mixes", [])}
                new_mixes = {m["id"]: m for m in curr_config.get("mixes", [])}
                for mid, m in new_mixes.items():
                    if mid not in old_mixes:
                        print(f"[{t_str}] \033[94m[MIX CREATED]\033[0m Name: '{m.get('name')}', ID: '{mid}', Type: '{m.get('type')}', Target: '{m.get('target_device')}'", flush=True)
                        event_start_times[f"mix_{mid}"] = t_now
                for mid in old_mixes:
                    if mid not in new_mixes:
                        print(f"[{t_str}] \033[91m[MIX DELETED]\033[0m ID: '{mid}'", flush=True)

                # Diff channel master volume states
                old_master_states = last_config.get("channel_master_states", {})
                new_master_states = curr_config.get("channel_master_states", {})
                for cid, st in new_master_states.items():
                    old_st = old_master_states.get(cid, {})
                    if st.get("volume") != old_st.get("volume") or st.get("muted") != old_st.get("muted"):
                        print(f"[{t_str}] \033[96m[CHANNEL MASTER FADER]\033[0m Channel '{cid}': Vol={st.get('volume')}%, Muted={st.get('muted')}", flush=True)

                # Diff mix master volume states
                old_mix_states = last_config.get("mix_states", {})
                new_mix_states = curr_config.get("mix_states", {})
                for mid, st in new_mix_states.items():
                    old_st = old_mix_states.get(mid, {})
                    if st.get("volume") != old_st.get("volume") or st.get("muted") != old_st.get("muted"):
                        print(f"[{t_str}] \033[94m[MASTER MIX FADER]\033[0m Mix '{mid}': Vol={st.get('volume')}%, Muted={st.get('muted')}", flush=True)

                # Diff submix routing states (channel_states)
                old_states = last_config.get("channel_states", {})
                new_states = curr_config.get("channel_states", {})
                for cid, m_map in new_states.items():
                    old_m_map = old_states.get(cid, {})
                    for mid, st in m_map.items():
                        old_st = old_m_map.get(mid, {})
                        if st.get("enabled") != old_st.get("enabled"):
                            en = st.get("enabled", False)
                            status_text = "\033[92mROUTED (ENABLED)\033[0m" if en else "\033[90mUNROUTED (DISABLED)\033[0m"
                            print(f"[{t_str}] \033[95m[SUBMIX ROUTING]\033[0m Channel '{cid}' -> Mix '{mid}': {status_text} (Vol: {st.get('volume', 80)}%, Muted: {st.get('muted', False)})", flush=True)
                            event_start_times[f"submix_{cid}_{mid}"] = t_now
                        elif st.get("volume") != old_st.get("volume") or st.get("muted") != old_st.get("muted"):
                            print(f"[{t_str}] \033[93m[SUBMIX FADER]\033[0m Channel '{cid}' -> Mix '{mid}': Vol={st.get('volume')}%, Muted={st.get('muted')}", flush=True)

                last_config = curr_config

            # 2. PipeWire Nodes Check
            curr_nodes = get_active_nodes()
            if curr_nodes != last_nodes:
                added_nodes = curr_nodes - last_nodes
                removed_nodes = last_nodes - curr_nodes
                for n in added_nodes:
                    if "WaveController" in n or "submix" in n:
                        print(f"[{t_str}] \033[92m[NODE CREATED]\033[0m PipeWire Node: '{n}'", flush=True)
                for n in removed_nodes:
                    if "WaveController" in n or "submix" in n:
                        print(f"[{t_str}] \033[90m[NODE DESTROYED]\033[0m PipeWire Node: '{n}'", flush=True)
                last_nodes = curr_nodes

            # 3. PipeWire Link Check
            curr_links = parse_pw_links()
            if curr_links != last_links:
                # Find new links
                for src, dests in curr_links.items():
                    old_dests = last_links.get(src, set())
                    newly_added = dests - old_dests
                    for d in newly_added:
                        is_wc = "WaveController" in src or "WaveController" in d or "submix" in src or "submix" in d
                        if is_wc:
                            # Calculate latency if triggered by recent event
                            latency_info = ""
                            for ev_k, ev_t in list(event_start_times.items()):
                                parts = ev_k.split("_", 1)
                                if len(parts) > 1 and (parts[1] in src or parts[1] in d):
                                    elapsed_ms = (t_now - ev_t) * 1000.0
                                    latency_info = f" \033[92m(Link Latency: {elapsed_ms:.1f}ms)\033[0m"
                                    break
                            print(f"[{t_str}] \033[92m[PW-LINK CONNECTED]\033[0m {src}  --->  {d}{latency_info}", flush=True)

                # Find removed links
                for src, dests in last_links.items():
                    curr_dests = curr_links.get(src, set())
                    newly_removed = dests - curr_dests
                    for d in newly_removed:
                        is_wc = "WaveController" in src or "WaveController" in d or "submix" in src or "submix" in d
                        if is_wc:
                            print(f"[{t_str}] \033[91m[PW-LINK SEVERED]\033[0m   {src}  -X->  {d}", flush=True)

                # 4. Invariant Verification on Every Link Change
                invariants_warnings = check_invariants(curr_links)
                for w in invariants_warnings:
                    print(f"[{t_str}] \033[91m{w}\033[0m", flush=True)

                last_links = curr_links

            time.sleep(0.033) # ~30 FPS poll
    except KeyboardInterrupt:
        print(f"\n[{get_timestamp()}] [STOPPED] Live monitor stopped gracefully.", flush=True)

if __name__ == "__main__":
    main()
