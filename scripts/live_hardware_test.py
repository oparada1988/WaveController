#!/usr/bin/env python3
import time
import sys
import os
import json
import socket
import subprocess
from datetime import datetime

def query_ipc(command, **kwargs):
    sock_path = os.path.expanduser("~/.config/WaveController/wavecontroller.sock")
    if not os.path.exists(sock_path):
        return None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(sock_path)
        payload = {"command": command}
        payload.update(kwargs)
        s.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        data = s.recv(8192).decode("utf-8")
        s.close()
        return json.loads(data)
    except Exception as e:
        return {"error": str(e)}

def get_pipewire_status():
    status = {"mic_muted": None, "mic_vol": None, "sink_muted": None, "sink_vol": None, "links": []}
    try:
        out = subprocess.check_output(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"], text=True, stderr=subprocess.DEVNULL).strip()
        status["mic_muted"] = "[MUTED]" in out
        import re
        m = re.search(r'Volume:\s*([\d\.]+)', out)
        if m:
            status["mic_vol"] = float(m.group(1))
    except Exception:
        pass

    try:
        out = subprocess.check_output(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], text=True, stderr=subprocess.DEVNULL).strip()
        status["sink_muted"] = "[MUTED]" in out
        import re
        m = re.search(r'Volume:\s*([\d\.]+)', out)
        if m:
            status["sink_vol"] = float(m.group(1))
    except Exception:
        pass

    try:
        out = subprocess.check_output(["pw-link", "-l"], text=True, stderr=subprocess.DEVNULL)
        lines = out.splitlines()
        current_src = ""
        for line in lines:
            if not line.startswith(" ") and not line.startswith("|"):
                current_src = line.strip()
            elif "|->" in line:
                dest = line.replace("|->", "").strip()
                if "wave" in current_src.lower() or "elgato" in current_src.lower() or "wave" in dest.lower() or "elgato" in dest.lower():
                    status["links"].append(f"{current_src} -> {dest}")
    except Exception:
        pass

    return status

def print_snapshot(label):
    ts = datetime.now().strftime("%H:%M:%S")
    ipc_data = query_ipc("get_channels")
    pw = get_pipewire_status()
    
    print("\n" + "=" * 80)
    print(f"[{ts}] SNAPSHOT: {label}")
    print("=" * 80)
    
    if ipc_data and ipc_data.get("status") == "ok":
        masters = ipc_data.get("master_states", {})
        elgato_master = masters.get("elgato_wave_xlr", masters.get("mic", {}))
        print(f"  [WaveController App State]")
        print(f"    Mic Channel Master State: Muted = {elgato_master.get('muted')}, Volume = {elgato_master.get('volume')}%")
        mixes = ipc_data.get("mix_states", {})
        for m_id, m_state in mixes.items():
            print(f"    Mix '{m_id}' State: Muted = {m_state.get('muted')}, Volume = {m_state.get('volume')}%")
    
    print(f"  [PipeWire / WirePlumber]")
    print(f"    @DEFAULT_AUDIO_SOURCE@: Muted = {pw['mic_muted']} (Volume = {pw['mic_vol']})")
    print(f"    @DEFAULT_AUDIO_SINK@:   Muted = {pw['sink_muted']} (Volume = {pw['sink_vol']})")
    print(f"    Active PipeWire Links ({len(pw['links'])}):")
    for l in pw["links"]:
        print(f"      • {l}")
    print("-" * 80)

if __name__ == "__main__":
    lbl = sys.argv[1] if len(sys.argv) > 1 else "Current State"
    print_snapshot(lbl)
