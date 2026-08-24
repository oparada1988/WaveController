"""
Elgato Wave Hardware Protocol Driver (Wave XLR, Wave:3, Wave:1).

Communicates via raw libusb USB Class Control Transfers on Endpoint 0.
Uses wIndex=0x3303 to bypass Linux kernel snd-usb-audio interface claiming,
enabling seamless hardware DSP control, gain (0-75dB), 48V phantom power,
Clipguard limiter, low-cut filter, and rotary dial synchronization without
interrupting audio capture or playback.
"""

import ctypes
import ctypes.util
import struct
import threading
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple

log = logging.getLogger("wavecontroller.elgato")

# USB Protocol Constants
BREQUEST_READ = 0x85
BREQUEST_WRITE = 0x05
RT_CLASS_IN = 0xA1
RT_CLASS_OUT = 0x21

# libusb C API initialization
_lib = None
_lib_ctx = None
_lib_lock = threading.RLock()

def _init_libusb():
    global _lib, _lib_ctx
    with _lib_lock:
        if _lib is not None:
            return _lib
        lib_path = ctypes.util.find_library("usb-1.0") or "libusb-1.0.so.0" or "libusb-1.0.so"
        try:
            _lib = ctypes.CDLL(lib_path)
            _lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
            _lib.libusb_init.restype = ctypes.c_int
            _lib.libusb_open_device_with_vid_pid.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
            _lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
            _lib.libusb_close.argtypes = [ctypes.c_void_p]
            _lib.libusb_close.restype = None
            _lib.libusb_control_transfer.argtypes = [
                ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8,
                ctypes.c_uint16, ctypes.c_uint16,
                ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint16, ctypes.c_uint,
            ]
            _lib.libusb_control_transfer.restype = ctypes.c_int

            _lib_ctx = ctypes.c_void_p()
            ret = _lib.libusb_init(ctypes.byref(_lib_ctx))
            if ret != 0:
                log.error(f"libusb_init failed with code {ret}")
                _lib = None
        except Exception as e:
            log.error(f"Failed to load libusb library: {e}")
            _lib = None
    return _lib


@dataclass(frozen=True)
class ElgatoProfile:
    key: str
    display_name: str
    vid: int
    pid: int
    wvalue_config: int
    wvalue_meter: int
    wvalue_devinfo: int
    windex: int
    config_len: int
    meter_len: int
    devinfo_len: int
    devinfo_api: Tuple[int, int]
    devinfo_fw: Tuple[int, int, int]
    devinfo_serial: Tuple[int, int]
    off_gain: int
    gain_max_db: float
    gain_raw_max: int
    off_mute: int
    off_phantom: Optional[int]
    off_clipguard: Optional[int]
    off_low_cut: Optional[int]
    off_hp_vol: int
    hp_fmt: str
    hp_scale: float
    off_vol_select: Optional[int]
    vol_select_map: Dict[int, str]
    off_low_z: Optional[int]
    off_monitor_mix: Optional[int]
    mix_max: int


PROFILE_WAVE_XLR = ElgatoProfile(
    key="wave_xlr",
    display_name="Wave XLR",
    vid=0x0FD9,
    pid=0x007D,
    wvalue_config=0x0000,
    wvalue_meter=0x0001,
    wvalue_devinfo=0x000A,
    windex=0x3303,
    config_len=34,
    meter_len=10,
    devinfo_len=51,
    devinfo_api=(0, 1),
    devinfo_fw=(6, 7, 8),
    devinfo_serial=(27, 47),
    off_gain=0,
    gain_max_db=75.0,
    gain_raw_max=0x5000,
    off_mute=4,
    off_phantom=5,
    off_clipguard=7,
    off_low_cut=8,
    off_hp_vol=9,
    hp_fmt="<h",
    hp_scale=256.0,
    off_vol_select=14,
    vol_select_map={0x01: "gain", 0x02: "hp", 0x03: "mix"},
    off_low_z=33,
    off_monitor_mix=None,
    mix_max=0,
)

PROFILE_WAVE_3 = ElgatoProfile(
    key="wave3",
    display_name="Wave:3",
    vid=0x0FD9,
    pid=0x0070,
    wvalue_config=0x0000,
    wvalue_meter=0x0001,
    wvalue_devinfo=0x000A,
    windex=0x3303,
    config_len=16,
    meter_len=8,
    devinfo_len=64,
    devinfo_api=(0, 1),
    devinfo_fw=(21, 22, 23),
    devinfo_serial=(36, 48),
    off_gain=0,
    gain_max_db=40.0,
    gain_raw_max=0x2800,
    off_mute=4,
    off_phantom=None,
    off_clipguard=5,
    off_low_cut=6,
    off_hp_vol=7,
    hp_fmt="<h",
    hp_scale=256.0,
    off_vol_select=12,
    vol_select_map={0x01: "gain", 0x02: "hp", 0x03: "mix"},
    off_low_z=None,
    off_monitor_mix=10,
    mix_max=0x6400,
)

ELGATO_PROFILES = [PROFILE_WAVE_XLR, PROFILE_WAVE_3]


class ElgatoWaveDevice:
    """
    Direct user-space hardware controller for an Elgato Wave device.
    Manages USB control transfers, hardware preamp gain (0-75dB), 48V phantom power,
    capacitive mute, headphone volume, Clipguard, low-cut filter, and rotary dial sync.
    """

    def __init__(self, profile: ElgatoProfile):
        self.profile = profile
        self._handle = None
        self._lock = threading.RLock()
        self._last_state: Dict[str, Any] = {}
        self.dev_info: Dict[str, str] = {}

    def is_connected(self) -> bool:
        return self._handle is not None

    def connect(self) -> bool:
        lib = _init_libusb()
        if not lib or not _lib_ctx:
            return False

        with self._lock:
            if self._handle:
                return True
            try:
                handle = lib.libusb_open_device_with_vid_pid(_lib_ctx, self.profile.vid, self.profile.pid)
                if handle:
                    self._handle = handle
                    log.info(f"Successfully connected to Elgato {self.profile.display_name} (0x{self.profile.vid:04X}:0x{self.profile.pid:04X})")
                    self._read_initial_info()
                    return True
            except Exception as e:
                log.warning(f"Failed to open Elgato device: {e}")
        return False

    def disconnect(self):
        with self._lock:
            if self._handle and _lib:
                try:
                    _lib.libusb_close(self._handle)
                except Exception:
                    pass
                self._handle = None

    def _ctrl_read(self, wValue: int, length: int) -> bytearray:
        if not self._handle or not _lib:
            raise RuntimeError("Device not connected")
        buf = (ctypes.c_ubyte * length)()
        with self._lock:
            ret = _lib.libusb_control_transfer(
                self._handle, RT_CLASS_IN, BREQUEST_READ, wValue, self.profile.windex,
                buf, length, 1000
            )
        if ret < 0:
            raise RuntimeError(f"USB control read failed (error {ret})")
        return bytearray(buf[:ret])

    def _ctrl_write(self, wValue: int, data: bytes):
        if not self._handle or not _lib:
            raise RuntimeError("Device not connected")
        buf = (ctypes.c_ubyte * len(data))(*data)
        with self._lock:
            ret = _lib.libusb_control_transfer(
                self._handle, RT_CLASS_OUT, BREQUEST_WRITE, wValue, self.profile.windex,
                buf, len(data), 1000
            )
        if ret < 0:
            raise RuntimeError(f"USB control write failed (error {ret})")

    def _read_initial_info(self):
        try:
            data = self._ctrl_read(self.profile.wvalue_devinfo, self.profile.devinfo_len)
            p = self.profile
            api_ver = f"{data[p.devinfo_api[0]]}.{data[p.devinfo_api[1]]}"
            fw_ver = f"{data[p.devinfo_fw[0]]}.{data[p.devinfo_fw[1]]}.{data[p.devinfo_fw[2]]}"
            serial = bytes(data[p.devinfo_serial[0]:p.devinfo_serial[1]]).decode("ascii", errors="replace").rstrip("\x00")
            self.dev_info = {
                "name": p.display_name,
                "api_version": api_ver,
                "fw_version": fw_ver,
                "serial": serial
            }
            log.info(f"Elgato {p.display_name} Info -> FW: {fw_ver}, Serial: {serial}")
        except Exception as e:
            log.warning(f"Could not read device info: {e}")
            self.dev_info = {"name": self.profile.display_name, "fw_version": "Unknown", "serial": "Unknown"}

    def read_config(self) -> bytearray:
        return self._ctrl_read(self.profile.wvalue_config, self.profile.config_len)

    def write_config(self, config: bytearray):
        self._ctrl_write(self.profile.wvalue_config, bytes(config))

    # --- Gain (0 to 75 dB) ---
    def get_gain_db(self) -> float:
        try:
            cfg = self.read_config()
            raw = struct.unpack_from("<H", cfg, self.profile.off_gain)[0]
            frac = raw / float(self.profile.gain_raw_max)
            return round(frac * self.profile.gain_max_db, 1)
        except Exception:
            return 45.0

    def set_gain_db(self, gain_db: float):
        try:
            gain_db = max(0.0, min(self.profile.gain_max_db, float(gain_db)))
            raw = int((gain_db / self.profile.gain_max_db) * self.profile.gain_raw_max)
            cfg = self.read_config()
            struct.pack_into("<H", cfg, self.profile.off_gain, raw)
            self.write_config(cfg)
            self._last_state["gain_db"] = gain_db
        except Exception as e:
            log.warning(f"Failed to set hardware gain: {e}")

    # --- Capacitive Mute ---
    def get_mute(self) -> bool:
        try:
            cfg = self.read_config()
            return bool(cfg[self.profile.off_mute])
        except Exception:
            return False

    def set_mute(self, muted: bool):
        try:
            cfg = self.read_config()
            cfg[self.profile.off_mute] = 0x01 if muted else 0x00
            self.write_config(cfg)
            self._last_state["mute"] = muted
        except Exception as e:
            log.warning(f"Failed to set hardware mute: {e}")

    # --- 48V Phantom Power ---
    def get_phantom_power(self) -> bool:
        if self.profile.off_phantom is None:
            return False
        try:
            cfg = self.read_config()
            return bool(cfg[self.profile.off_phantom])
        except Exception:
            return False

    def set_phantom_power(self, enabled: bool):
        if self.profile.off_phantom is None:
            return
        try:
            cfg = self.read_config()
            cfg[self.profile.off_phantom] = 0x01 if enabled else 0x00
            self.write_config(cfg)
            self._last_state["phantom_power"] = enabled
            log.info(f"Elgato 48V Phantom Power set to {'ON' if enabled else 'OFF'}")
        except Exception as e:
            log.warning(f"Failed to set 48V phantom power: {e}")

    # --- Clipguard Dual-Stage Limiter ---
    def get_clipguard(self) -> bool:
        if self.profile.off_clipguard is None:
            return True
        try:
            cfg = self.read_config()
            return bool(cfg[self.profile.off_clipguard])
        except Exception:
            return True

    def set_clipguard(self, enabled: bool):
        if self.profile.off_clipguard is None:
            return
        try:
            cfg = self.read_config()
            cfg[self.profile.off_clipguard] = 0x01 if enabled else 0x00
            self.write_config(cfg)
            self._last_state["clipguard"] = enabled
        except Exception as e:
            log.warning(f"Failed to set Clipguard: {e}")

    # --- Enhanced Low-Cut Filter ---
    def get_low_cut(self) -> str:
        if self.profile.off_low_cut is None:
            return "Off"
        try:
            cfg = self.read_config()
            val = cfg[self.profile.off_low_cut]
            return "Off" if val == 0 else ("80Hz" if val == 1 else "120Hz")
        except Exception:
            return "80Hz"

    def set_low_cut(self, mode: str):
        if self.profile.off_low_cut is None:
            return
        try:
            val = 0 if mode == "Off" else (1 if mode == "80Hz" else 2)
            cfg = self.read_config()
            cfg[self.profile.off_low_cut] = val
            self.write_config(cfg)
            self._last_state["low_cut"] = mode
        except Exception as e:
            log.warning(f"Failed to set low cut filter: {e}")

    # --- Headphone Volume ---
    def get_headphone_volume_pct(self) -> int:
        try:
            cfg = self.read_config()
            raw = struct.unpack_from(self.profile.hp_fmt, cfg, self.profile.off_hp_vol)[0]
            db = raw / self.profile.hp_scale # -60.0 to 0.0 dB
            pct = int(round((db + 60.0) / 60.0 * 100.0))
            return max(0, min(100, pct))
        except Exception:
            return 70

    def set_headphone_volume_pct(self, pct: int):
        try:
            pct = max(0, min(100, pct))
            db = (pct / 100.0 * 60.0) - 60.0
            raw = int(db * self.profile.hp_scale)
            cfg = self.read_config()
            struct.pack_into(self.profile.hp_fmt, cfg, self.profile.off_hp_vol, raw)
            self.write_config(cfg)
            self._last_state["hp_pct"] = pct
        except Exception as e:
            log.warning(f"Failed to set headphone volume: {e}")

    # --- Low-Impedance Mode ---
    def get_low_impedance(self) -> bool:
        if self.profile.off_low_z is None:
            return False
        try:
            cfg = self.read_config()
            return bool(cfg[self.profile.off_low_z])
        except Exception:
            return False

    def set_low_impedance(self, enabled: bool):
        if self.profile.off_low_z is None:
            return
        try:
            cfg = self.read_config()
            cfg[self.profile.off_low_z] = 0x01 if enabled else 0x00
            self.write_config(cfg)
            self._last_state["low_z"] = enabled
        except Exception as e:
            log.warning(f"Failed to set low impedance mode: {e}")

    # --- Dial Mode & Monitor Mix ---
    def get_dial_mode(self) -> str:
        if self.profile.off_vol_select is None:
            return "gain"
        try:
            cfg = self.read_config()
            val = cfg[self.profile.off_vol_select]
            return self.profile.vol_select_map.get(val, "gain")
        except Exception:
            return "gain"

    def get_all_state(self) -> Dict[str, Any]:
        try:
            cfg = self.read_config()
            raw_gain = struct.unpack_from("<H", cfg, self.profile.off_gain)[0]
            gain_db = round((raw_gain / float(self.profile.gain_raw_max)) * self.profile.gain_max_db, 1)
            mute = bool(cfg[self.profile.off_mute])
            
            raw_hp = struct.unpack_from(self.profile.hp_fmt, cfg, self.profile.off_hp_vol)[0]
            hp_db = raw_hp / self.profile.hp_scale
            hp_pct = max(0, min(100, int(round((hp_db + 60.0) / 60.0 * 100.0))))

            dial_mode = "gain"
            if self.profile.off_vol_select is not None:
                dial_mode = self.profile.vol_select_map.get(cfg[self.profile.off_vol_select], "gain")

            low_z = False
            if self.profile.off_low_z is not None:
                low_z = bool(cfg[self.profile.off_low_z])

            phantom = self.get_phantom_power()
            clipguard = self.get_clipguard()
            low_cut = self.get_low_cut()

            return {
                "connected": True,
                "name": self.profile.display_name,
                "gain_db": gain_db,
                "mute": mute,
                "phantom_power": phantom,
                "clipguard": clipguard,
                "low_cut": low_cut,
                "hp_volume_pct": hp_pct,
                "dial_mode": dial_mode,
                "low_impedance": low_z,
                "serial": self.dev_info.get("serial", ""),
                "fw_version": self.dev_info.get("fw_version", ""),
            }
        except Exception:
            return {"connected": False}


class ElgatoManager:
    """Singleton coordinator discovering and managing attached Elgato Wave devices."""

    def __init__(self):
        self.active_device: Optional[ElgatoWaveDevice] = None
        self._stop_poll = False
        self._poll_thread: Optional[threading.Thread] = None
        self.on_state_changed = None # Callback when hardware dial/mute/48V changes physically

    def detect_device(self) -> Optional[ElgatoWaveDevice]:
        for p in ELGATO_PROFILES:
            dev = ElgatoWaveDevice(p)
            if dev.connect():
                self.active_device = dev
                self._start_sync_loop()
                return dev
        return None

    def get_device(self) -> Optional[ElgatoWaveDevice]:
        if self.active_device and self.active_device.is_connected():
            return self.active_device
        return self.detect_device()

    def _start_sync_loop(self):
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._stop_poll = False
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _poll_loop(self):
        import time
        last_state = {}
        while not self._stop_poll:
            time.sleep(0.08) # ~12.5 Hz polling for rapid dial and 48V sync
            dev = self.active_device
            if not dev or not dev.is_connected():
                time.sleep(1.0)
                continue
            try:
                curr = dev.get_all_state()
                if not curr.get("connected"):
                    continue
                if not last_state:
                    # Broadcast initial full state snapshot immediately on connection
                    if self.on_state_changed:
                        self.on_state_changed(curr, dict(curr))
                else:
                    changed = {}
                    for k, v in curr.items():
                        if k in last_state and last_state[k] != v:
                            changed[k] = v
                    if changed and self.on_state_changed:
                        self.on_state_changed(curr, changed)
                last_state = curr
            except Exception:
                pass


elgato_manager = ElgatoManager()
