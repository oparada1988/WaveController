#!/usr/bin/env python3
import time
import sys
import os
import json
import subprocess
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wavecontroller.engine.elgato_wave import elgato_manager

def get_pipewire_state():
    pw_state = {
        "mic_wpctl_muted": None,
        "mic_wpctl_vol": None,
        "sink_wpctl_muted": None,
        "sink_wpctl_vol": None,
        "mic_links": []
    }
    
    # 1. wpctl status for default source / sink
    try:
        out = subprocess.check_output(["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"], text=True, stderr=subprocess.DEVNULL).strip()
        pw_state["mic_wpctl_muted"] = "[MUTED]" in out
        import re
        m = re.search(r'Volume:\s*([\d\.]+)', out)
        if m:
            pw_state["mic_wpctl_vol"] = float(m.group(1))
    except Exception:
        pass

    try:
        out = subprocess.check_output(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], text=True, stderr=subprocess.DEVNULL).strip()
        pw_state["sink_wpctl_muted"] = "[MUTED]" in out
        import re
        m = re.search(r'Volume:\s*([\d\.]+)', out)
        if m:
            pw_state["sink_wpctl_vol"] = float(m.group(1))
    except Exception:
        pass

    # 2. pw-link active connections for Elgato Wave XLR
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
                    pw_state["mic_links"].append(f"{current_src} -> {dest}")
    except Exception:
        pass

    return pw_state

def main():
    print("=" * 80)
    print("      WAVECONTROLLER LIVE HARDWARE & PIPEWIRE CAPTURE MONITOR")
    print("=" * 80)
    
    dev = elgato_manager.get_device()
    if not dev or not dev.is_connected():
        if not elgato_manager.detect_device():
            print("[ERROR] Could not connect to Elgato Wave device!")
            return
        dev = elgato_manager.get_device()

    print("[INFO] Connected to Elgato Wave device successfully.")
    print("[INFO] Polling hardware registers & PipeWire links at 10 Hz (100ms)...")
    print("-" * 80)

    last_hw_state = {}
    last_pw_state = {}

    try:
        while True:
            try:
                curr_hw = dev.get_all_state()
                raw_cfg = dev.read_config()
                raw_byte_4 = raw_cfg[dev.profile.off_mute] if dev.profile.off_mute is not None else None
                raw_byte_14 = raw_cfg[dev.profile.off_vol_select] if dev.profile.off_vol_select is not None else None
            except Exception as e:
                curr_hw = {"connected": False, "error": str(e)}
                raw_byte_4 = None
                raw_byte_14 = None

            curr_pw = get_pipewire_state()

            # Detect differences
            hw_diff = {}
            for k, v in curr_hw.items():
                if k not in last_hw_state or last_hw_state[k] != v:
                    hw_diff[k] = v

            pw_diff = {}
            for k, v in curr_pw.items():
                if k not in last_pw_state or last_pw_state[k] != v:
                    pw_diff[k] = v

            if hw_diff or pw_diff or not last_hw_state:
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                b14_str = f"0x{raw_byte_14:02X}" if raw_byte_14 is not None else "None"
                b4_str = f"0x{raw_byte_4:02X}" if raw_byte_4 is not None else "None"
                mute_lbl = "MUTED" if raw_byte_4 == 1 else ("UNMUTED" if raw_byte_4 == 0 else "UNKNOWN")

                print(f"\n[{ts}] === STATE CHANGE DETECTED ===")
                print(f"  [HARDWARE REGISTERS]")
                print(f"    Mode (Byte 14): {b14_str} ({curr_hw.get('dial_mode', 'N/A')}) | Preamp Mute (Byte 4): {b4_str} ({mute_lbl})")
                print(f"    Gain: {curr_hw.get('gain_db', 'N/A')} dB | HP Vol: {curr_hw.get('hp_volume_pct', 'N/A')}% | Mix: {curr_hw.get('monitor_mix_pct', 'N/A')}% | 48V: {curr_hw.get('phantom_power', 'N/A')}")
                print(f"    Driver Mode Mutes: {curr_hw.get('mode_mutes', {})}")
                print(f"  [PIPEWIRE & WPCTL]")
                print(f"    Mic Node Muted: {curr_pw.get('mic_wpctl_muted')} (Vol: {curr_pw.get('mic_wpctl_vol')})")
                print(f"    Sink Node Muted: {curr_pw.get('sink_wpctl_muted')} (Vol: {curr_pw.get('sink_wpctl_vol')})")
                print(f"    Active PipeWire Links ({len(curr_pw.get('mic_links', []))}):")
                for link in curr_pw.get("mic_links", []):
                    print(f"      • {link}")
                print("-" * 80)
                sys.stdout.flush()

                last_hw_state = dict(curr_hw)
                last_pw_state = dict(curr_pw)

            if curr_hw.get("connected") is False or "error" in curr_hw:
                time.sleep(1.0)
            else:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[INFO] Live capture stopped by user.")

if __name__ == "__main__":
    main()
