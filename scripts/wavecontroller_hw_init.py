#!/usr/bin/env python3
"""
WaveController Hardware Pre-Initialization Helper
Executed by Udev / Systemd at kernel boot or USB hotplug to restore
the user's last saved gain (dB), headphone volume, phantom power,
Clipguard, and LED colors to Elgato Wave hardware before GDM login.
"""

import os
import sys
import glob
import json
import struct
import ctypes
import ctypes.util

# USB Protocol Constants
BREQUEST_READ = 0x85
BREQUEST_WRITE = 0x05
RT_CLASS_IN = 0xA1
RT_CLASS_OUT = 0x21

PROFILES = [
    {
        "name": "Wave XLR",
        "vid": 0x0FD9,
        "pid": 0x007D,
        "wvalue_config": 0x0000,
        "windex": 0x3303,
        "config_len": 34,
        "off_gain": 0,
        "gain_max_db": 75.0,
        "gain_raw_max": 0x4B00,
        "off_mute": 4,
        "off_phantom": 6,
        "off_clipguard": 7,
        "off_low_cut": 8,
        "off_hp_vol": 9,
        "hp_fmt": "<h",
        "hp_scale": 256.0,
        "off_vol_select": 14,
        "off_low_z": 33,
        "off_monitor_mix": 12,
        "off_rgb_mute": 15,
        "off_rgb_ring": 18,
    },
    {
        "name": "Wave XLR MK2",
        "vid": 0x0FD9,
        "pid": 0x00B6,
        "wvalue_config": 0x0000,
        "windex": 0x3303,
        "config_len": 34,
        "off_gain": 0,
        "gain_max_db": 75.0,
        "gain_raw_max": 0x4B00,
        "off_mute": 4,
        "off_phantom": 6,
        "off_clipguard": 7,
        "off_low_cut": 8,
        "off_hp_vol": 9,
        "hp_fmt": "<h",
        "hp_scale": 256.0,
        "off_vol_select": 14,
        "off_low_z": 33,
        "off_monitor_mix": 12,
        "off_rgb_mute": 15,
        "off_rgb_ring": 18,
    },
    {
        "name": "Wave:3",
        "vid": 0x0FD9,
        "pid": 0x0070,
        "wvalue_config": 0x0000,
        "windex": 0x3303,
        "config_len": 16,
        "off_gain": 0,
        "gain_max_db": 40.0,
        "gain_raw_max": 0x2800,
        "off_mute": 4,
        "off_phantom": None,
        "off_clipguard": 5,
        "off_low_cut": 6,
        "off_hp_vol": 7,
        "hp_fmt": "<h",
        "hp_scale": 256.0,
        "off_vol_select": 12,
        "off_low_z": None,
        "off_monitor_mix": 10,
        "off_rgb_mute": None,
        "off_rgb_ring": None,
    }
]

def find_saved_config() -> dict:
    """Finds the most recently updated WaveController config.json."""
    candidates = []
    
    # Check SUDO_USER if run via sudo/udev
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        p = f"/home/{sudo_user}/.config/WaveController/config.json"
        if os.path.isfile(p):
            candidates.append(p)

    # Check all user home directories
    for user_cfg in glob.glob("/home/*/.config/WaveController/config.json"):
        if os.path.isfile(user_cfg):
            candidates.append(user_cfg)
            
    # Check system-wide default
    if os.path.isfile("/etc/wavecontroller/config.json"):
        candidates.append("/etc/wavecontroller/config.json")

    if not candidates:
        return {}

    # Sort by last modification time (newest first)
    candidates.sort(key=lambda x: os.path.getmtime(x), reverse=True)
    try:
        with open(candidates[0], "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("hardware_settings", {})
    except Exception:
        return {}

def init_libusb():
    lib_path = ctypes.util.find_library("usb-1.0") or "libusb-1.0.so.0" or "libusb-1.0.so"
    try:
        lib = ctypes.CDLL(lib_path)
        lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.libusb_init.restype = ctypes.c_int
        lib.libusb_open_device_with_vid_pid.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
        lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
        lib.libusb_close.argtypes = [ctypes.c_void_p]
        lib.libusb_close.restype = None
        lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_claim_interface.restype = ctypes.c_int
        lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_release_interface.restype = ctypes.c_int
        lib.libusb_control_transfer.argtypes = [
            ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8,
            ctypes.c_uint16, ctypes.c_uint16,
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint16, ctypes.c_uint,
        ]
        lib.libusb_control_transfer.restype = ctypes.c_int
        
        ctx = ctypes.c_void_p()
        ret = lib.libusb_init(ctypes.byref(ctx))
        if ret != 0:
            return None, None
        return lib, ctx
    except Exception:
        return None, None

def apply_hardware_settings():
    hw_settings = find_saved_config()
    gain_db = float(hw_settings.get("gain_db", 30))
    hp_pct = int(hw_settings.get("headphone_volume", 50))
    phantom = bool(hw_settings.get("phantom_power", True))
    clipguard = bool(hw_settings.get("clipguard", True))
    low_cut_str = str(hw_settings.get("low_cut", "80Hz"))
    low_z = bool(hw_settings.get("low_impedance", False))
    monitor_mix_pct = int(hw_settings.get("monitor_mix", 50))
    led_colors = hw_settings.get("led_colors", {
        "gain": "#2ECC71",
        "hp": "#613583",
        "mix": "#FF9500",
        "mute": "#FF0000"
    })

    lib, ctx = init_libusb()
    if not lib or not ctx:
        sys.exit(1)

    devices_configured = 0
    for prof in PROFILES:
        handle = lib.libusb_open_device_with_vid_pid(ctx, prof["vid"], prof["pid"])
        if not handle:
            continue

        try:
            lib.libusb_claim_interface(handle, 3)
        except Exception:
            pass

        try:
            # 1. Read current config buffer
            length = prof["config_len"]
            buf = (ctypes.c_ubyte * length)()
            ret = lib.libusb_control_transfer(
                handle, RT_CLASS_IN, BREQUEST_READ, prof["wvalue_config"], prof["windex"],
                buf, length, 1000
            )
            if ret < 0:
                continue

            cfg = bytearray(buf[:ret])

            # 2. Apply Preamp Gain
            clamped_gain = max(0.0, min(prof["gain_max_db"], gain_db))
            raw_gain = int((clamped_gain / prof["gain_max_db"]) * prof["gain_raw_max"])
            struct.pack_into("<H", cfg, prof["off_gain"], raw_gain)

            # 3. Apply Hardware Mute Byte (0x00 = Unmuted)
            if prof.get("off_mute") is not None:
                cfg[prof["off_mute"]] = 0x00

            # 4. Apply 48V Phantom Power
            if prof.get("off_phantom") is not None:
                cfg[prof["off_phantom"]] = 0x01 if phantom else 0x00

            # 5. Apply Clipguard
            if prof.get("off_clipguard") is not None:
                cfg[prof["off_clipguard"]] = 0x01 if clipguard else 0x00

            # 6. Apply Low-Cut Filter
            if prof.get("off_low_cut") is not None:
                val = 0 if low_cut_str == "Off" else (1 if low_cut_str == "80Hz" else 2)
                cfg[prof["off_low_cut"]] = val

            # 7. Apply Headphone Volume
            if prof.get("off_hp_vol") is not None:
                clamped_hp = max(0, min(100, hp_pct))
                db = (clamped_hp / 100.0 * 60.0) - 60.0
                raw_hp = int(db * prof["hp_scale"])
                struct.pack_into(prof["hp_fmt"], cfg, prof["off_hp_vol"], raw_hp)

            # 8. Apply Monitor Mix (0% Mic to 100% PC)
            if prof.get("off_monitor_mix") is not None:
                clamped_mix = max(0, min(100, monitor_mix_pct))
                hw_mix = 100 - clamped_mix
                raw_mix = hw_mix << 8
                struct.pack_into("<H", cfg, prof["off_monitor_mix"], raw_mix)

            # 9. Apply Low-Impedance Mode (Wave XLR only)
            if prof.get("off_low_z") is not None:
                cfg[prof["off_low_z"]] = 0x01 if low_z else 0x00

            # 10. Apply Dial Mode (Mode 1: Gain)
            if prof.get("off_vol_select") is not None:
                cfg[prof["off_vol_select"]] = 0x01

            # 11. Apply LED Colors
            if prof.get("off_rgb_mute") is not None:
                gain_hex = led_colors.get("gain", "#2ECC71").lstrip("#")
                if len(gain_hex) == 6:
                    try:
                        mr, mg, mb = int(gain_hex[0:2], 16), int(gain_hex[2:4], 16), int(gain_hex[4:6], 16)
                        cfg[prof["off_rgb_mute"]] = mr
                        cfg[prof["off_rgb_mute"] + 1] = mg
                        cfg[prof["off_rgb_mute"] + 2] = mb
                    except Exception:
                        pass

            if prof.get("off_rgb_ring") is not None:
                gain_hex = led_colors.get("gain", "#2ECC71").lstrip("#")
                if len(gain_hex) == 6:
                    try:
                        r, g, b = int(gain_hex[0:2], 16), int(gain_hex[2:4], 16), int(gain_hex[4:6], 16)
                        for off in [prof["off_rgb_ring"], prof["off_rgb_ring"] + 3, prof["off_rgb_ring"] + 6]:
                            if off + 2 < len(cfg):
                                cfg[off] = r
                                cfg[off + 1] = g
                                cfg[off + 2] = b
                    except Exception:
                        pass

            # 12. Send Write Control Transfer
            write_buf = (ctypes.c_ubyte * len(cfg))(*cfg)
            lib.libusb_control_transfer(
                handle, RT_CLASS_OUT, BREQUEST_WRITE, prof["wvalue_config"], prof["windex"],
                write_buf, len(cfg), 1000
            )
            devices_configured += 1
            print(f"[WaveController Boot Init] Successfully initialized {prof['name']} -> Gain: {gain_db}dB, HP Vol: {hp_pct}%, 48V: {phantom}")
        except Exception as e:
            print(f"[WaveController Boot Init] Warning: Failed to configure {prof['name']}: {e}", file=sys.stderr)
        finally:
            try:
                lib.libusb_release_interface(handle, 3)
            except Exception:
                pass
            try:
                lib.libusb_close(handle)
            except Exception:
                pass

    return devices_configured

if __name__ == "__main__":
    apply_hardware_settings()
