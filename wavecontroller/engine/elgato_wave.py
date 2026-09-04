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
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
from wavecontroller.utils.logger import get_logger

log = get_logger("ElgatoWave")

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
        candidates = [ctypes.util.find_library("usb-1.0"), "libusb-1.0.so.0", "libusb-1.0.so"]
        _lib = None
        for candidate in candidates:
            if not candidate:
                continue
            try:
                _lib = ctypes.CDLL(candidate)
                break
            except Exception:
                continue
        if _lib is None:
            log.error("Failed to load libusb library (none of candidates succeeded)")
            return None
        try:
            _lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
            _lib.libusb_init.restype = ctypes.c_int
            _lib.libusb_open_device_with_vid_pid.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
            _lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
            _lib.libusb_close.argtypes = [ctypes.c_void_p]
            _lib.libusb_close.restype = None
            _lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
            _lib.libusb_claim_interface.restype = ctypes.c_int
            _lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
            _lib.libusb_release_interface.restype = ctypes.c_int
            if hasattr(_lib, "libusb_detach_kernel_driver"):
                _lib.libusb_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
                _lib.libusb_detach_kernel_driver.restype = ctypes.c_int
            if hasattr(_lib, "libusb_set_auto_detach_kernel_driver"):
                _lib.libusb_set_auto_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
                _lib.libusb_set_auto_detach_kernel_driver.restype = ctypes.c_int
            _lib.libusb_control_transfer.argtypes = [
                ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8,
                ctypes.c_uint16, ctypes.c_uint16,
                ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint16, ctypes.c_uint,
            ]
            _lib.libusb_control_transfer.restype = ctypes.c_int
            if hasattr(_lib, "libusb_exit"):
                _lib.libusb_exit.argtypes = [ctypes.c_void_p]
                _lib.libusb_exit.restype = None
            _lib_ctx = ctypes.c_void_p()
            ret = _lib.libusb_init(ctypes.byref(_lib_ctx))
            if ret != 0:
                log.error(f"libusb_init failed with code {ret}")
                _lib = None
        except Exception as e:
            log.error(f"Failed to load libusb library: {e}")
            _lib = None
    return _lib


def _reinit_libusb_after_resume():
    """Recreates the libusb context after system resume.

    The kernel resets/re-powers USB root hubs across suspend (observed as
    "root hub lost power or was reset"), which invalidates the libusb context
    created before sleep. Reusing it crashes the whole process with a native
    segfault deep inside libusb_open_device_with_vid_pid, bypassing Python's
    exception handling entirely. Tearing down and recreating the context
    avoids handing libusb any state that predates the reset.
    """
    global _lib, _lib_ctx
    with _lib_lock:
        if _lib is not None and _lib_ctx:
            try:
                if hasattr(_lib, "libusb_exit"):
                    _lib.libusb_exit(_lib_ctx)
            except Exception as e:
                log.warning(f"libusb_exit during resume reinit failed (continuing): {e}")
        _lib = None
        _lib_ctx = None
    _init_libusb()


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
    gain_scale: float = 256.0
    off_rgb_mute: Optional[int] = None
    off_rgb_ring: Optional[int] = None
    claim_interface: Optional[int] = None
    icon_name: str = "ElgatoWaveXLR.png"


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
    gain_raw_max=0x4B00,
    gain_scale=256.0,
    off_mute=4,
    off_phantom=6,
    off_clipguard=7,
    off_low_cut=8,
    off_hp_vol=9,
    hp_fmt="<h",
    hp_scale=256.0,
    off_vol_select=14,
    vol_select_map={0x01: "gain", 0x02: "hp", 0x03: "mix"},
    off_low_z=33,
    off_monitor_mix=12,
    mix_max=0x6400,
    off_rgb_mute=15,
    off_rgb_ring=18,
    claim_interface=3,
    icon_name="ElgatoWaveXLR.png",
)

PROFILE_WAVE_XLR_MK2 = ElgatoProfile(
    key="wave_xlr_mk2",
    display_name="Wave XLR MK2",
    vid=0x0FD9,
    pid=0x00B6,
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
    gain_raw_max=0x4B00,
    gain_scale=256.0,
    off_mute=4,
    off_phantom=6,
    off_clipguard=7,
    off_low_cut=8,
    off_hp_vol=9,
    hp_fmt="<h",
    hp_scale=256.0,
    off_vol_select=14,
    vol_select_map={0x01: "gain", 0x02: "hp", 0x03: "mix"},
    off_low_z=33,
    off_monitor_mix=12,
    mix_max=0x6400,
    off_rgb_mute=15,
    off_rgb_ring=18,
    claim_interface=3,
    icon_name="ElgatoWaveXLRMK2.png",
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
    gain_scale=256.0,
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
    off_rgb_mute=None,
    off_rgb_ring=None,
    claim_interface=None,
    icon_name="ElgatoWave3.png",
)

ELGATO_PROFILES = [PROFILE_WAVE_XLR, PROFILE_WAVE_XLR_MK2, PROFILE_WAVE_3]


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
        self._steady_dial_mode: str = "gain"
        self._steady_gain_db: float = 45.0
        self._steady_hp_pct: int = 70
        self._steady_mix_pct: int = 50
        self._is_streaming_vu: bool = False
        self._is_peeking: bool = False
        self._peek_mode: Optional[str] = None
        self._revert_timer: Optional[threading.Timer] = None
        self._mode_mutes: Dict[str, bool] = {"gain": False, "hp": False, "mix": False}
        self._led_colors: Dict[str, str] = {
            "gain": "#FFFFFF",
            "hp": "#2ECC71",
            "mix": "#FF9500",
            "mute": "#FF0000"
        }
        self._user_interacting: bool = False
        self._interaction_timer: Optional[threading.Timer] = None
        self._last_vu_time: float = 0.0
        self._vu_ballistics_level: float = 0.0
        self._last_raw_hw_mute: Optional[bool] = None
        
        # Anti-Flooding Circuit Breaker
        self._consecutive_errors: int = 0
        self._backoff_until: float = 0.0
        self._active_windex: int = self.profile.windex

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
                    # Claim vendor control interface only if profile explicitly requires it
                    if self.profile.claim_interface is not None:
                        if hasattr(lib, "libusb_set_auto_detach_kernel_driver"):
                            try:
                                lib.libusb_set_auto_detach_kernel_driver(handle, 1)
                            except Exception:
                                pass
                        if hasattr(lib, "libusb_detach_kernel_driver"):
                            try:
                                lib.libusb_detach_kernel_driver(handle, self.profile.claim_interface)
                            except Exception:
                                pass
                        try:
                            lib.libusb_claim_interface(handle, self.profile.claim_interface)
                        except Exception as e:
                            log.debug(f"Could not claim interface {self.profile.claim_interface}: {e}")
                    if not self._read_initial_info():
                        self.disconnect()
                        return False
                    log.info(f"Successfully connected to Elgato {self.profile.display_name} (0x{self.profile.vid:04X}:0x{self.profile.pid:04X})")
                    return True
            except Exception as e:
                log.warning(f"Failed to open Elgato device: {e}")
                self.disconnect()
        return False

    def disconnect(self):
        with self._lock:
            if self._handle and _lib:
                if self.profile.claim_interface is not None:
                    try:
                        _lib.libusb_release_interface(self._handle, self.profile.claim_interface)
                    except Exception:
                        pass
                try:
                    _lib.libusb_close(self._handle)
                except Exception:
                    pass
                self._handle = None
                self._consecutive_errors = 0
                self._backoff_until = 0.0

    def _ctrl_read(self, wValue: int, length: int, is_probing: bool = False) -> bytearray:
        if not self._handle or not _lib:
            raise RuntimeError("Device not connected")
        
        now = time.time()
        if now < self._backoff_until and not is_probing:
            raise RuntimeError(f"USB circuit breaker backoff active ({self._backoff_until - now:.1f}s remaining)")

        buf = (ctypes.c_ubyte * length)()
        with self._lock:
            if not self._handle or not _lib:
                raise RuntimeError("Device disconnected during transfer")
            ret = _lib.libusb_control_transfer(
                self._handle, RT_CLASS_IN, BREQUEST_READ, wValue, self._active_windex,
                buf, length, 500
            )
        if ret < 0:
            if not is_probing:
                self._consecutive_errors += 1
                if self._consecutive_errors >= 2 or ret in (-4, -99):
                    log.warning(f"Elgato {self.profile.display_name} USB connection lost (error {ret}). Releasing handle for auto-recovery.")
                    self.disconnect()
                    raise RuntimeError(f"USB connection lost (error {ret})")
            raise RuntimeError(f"USB control read failed (error {ret})")
        
        self._consecutive_errors = 0
        self._backoff_until = 0.0
        return bytearray(buf[:ret])

    def _ctrl_write(self, wValue: int, data: bytes):
        if not self._handle or not _lib:
            raise RuntimeError("Device not connected")

        now = time.time()
        if now < self._backoff_until:
            raise RuntimeError(f"USB circuit breaker backoff active ({self._backoff_until - now:.1f}s remaining)")

        buf = (ctypes.c_ubyte * len(data))(*data)
        with self._lock:
            if not self._handle or not _lib:
                raise RuntimeError("Device disconnected during transfer")
            ret = _lib.libusb_control_transfer(
                self._handle, RT_CLASS_OUT, BREQUEST_WRITE, wValue, self._active_windex,
                buf, len(data), 500
            )
        if ret < 0:
            self._consecutive_errors += 1
            if self._consecutive_errors >= 2 or ret in (-4, -99):
                log.warning(f"Elgato {self.profile.display_name} USB connection lost (error {ret}). Releasing handle for auto-recovery.")
                self.disconnect()
                raise RuntimeError(f"USB connection lost (error {ret})")
            raise RuntimeError(f"USB control write failed (error {ret})")

        self._consecutive_errors = 0
        self._backoff_until = 0.0

    def _read_initial_info(self):
        # Probe working wIndex candidates for this device model
        windex_candidates = [self.profile.windex]
        if self.profile.key == "wave3":
            windex_candidates = [0x3303, 0x0003, 0x0002, 0x0000, 0x0200]
        elif self.profile.key == "wave_xlr":
            windex_candidates = [0x3303, 0x0000]

        data = None
        for candidate_windex in windex_candidates:
            if not self._handle:
                break
            self._active_windex = candidate_windex
            self._consecutive_errors = 0
            self._backoff_until = 0.0
            try:
                data = self._ctrl_read(self.profile.wvalue_devinfo, self.profile.devinfo_len, is_probing=True)
                if data and len(data) >= self.profile.devinfo_len:
                    break
            except Exception:
                data = None

        if data and len(data) >= self.profile.devinfo_len:
            p = self.profile
            try:
                api_ver = f"{data[p.devinfo_api[0]]}.{data[p.devinfo_api[1]]}"
                fw_ver = f"{data[p.devinfo_fw[0]]}.{data[p.devinfo_fw[1]]}.{data[p.devinfo_fw[2]]}"
                serial = bytes(data[p.devinfo_serial[0]:p.devinfo_serial[1]]).decode("ascii", errors="replace").rstrip("\x00")
            except Exception:
                api_ver = "1.0"
                fw_ver = "Unknown"
                serial = "Unknown"

            self.dev_info = {
                "name": p.display_name,
                "api_version": api_ver,
                "fw_version": fw_ver,
                "serial": serial
            }
            log.info(f"Elgato {p.display_name} Info -> FW: {fw_ver}, Serial: {serial} (wIndex=0x{self._active_windex:04X})")
            self._consecutive_errors = 0
            self._backoff_until = 0.0
            return True
        else:
            self._active_windex = self.profile.windex
            log.warning(f"Could not read device info for {self.profile.display_name}")
            self.dev_info = {"name": self.profile.display_name, "fw_version": "Unknown", "serial": "Unknown"}
            self._consecutive_errors = 0
            self._backoff_until = 0.0
            return False

    def read_config(self) -> bytearray:
        return self._ctrl_read(self.profile.wvalue_config, self.profile.config_len)

    def write_config(self, config: bytearray):
        self._ctrl_write(self.profile.wvalue_config, bytes(config))
        if self.profile.off_mute is not None and len(config) > self.profile.off_mute:
            self._last_raw_hw_mute = bool(config[self.profile.off_mute])

    # --- Gain (0 to 75 dB) ---
    def get_gain_db(self) -> float:
        try:
            cfg = self.read_config()
            raw = struct.unpack_from("<H", cfg, self.profile.off_gain)[0]
            scale = getattr(self.profile, "gain_scale", 256.0)
            return round(raw / scale, 1)
        except Exception:
            return 45.0

    def set_gain_db(self, gain_db: float, transient: bool = False):
        try:
            self._steady_gain_db = float(gain_db)
            self._is_streaming_vu = False
            self.notify_user_interaction("gain")
            gain_db = max(0.0, min(self.profile.gain_max_db, float(gain_db)))
            scale = getattr(self.profile, "gain_scale", 256.0)
            raw = int(round(gain_db * scale))
            cfg = self.read_config()
            struct.pack_into("<H", cfg, self.profile.off_gain, raw)
            if transient:
                self._trigger_transient_peek("gain", cfg)
            else:
                curr_mode = self.get_dial_mode()
                cfg[self.profile.off_mute] = self._calc_hw_mute_byte(curr_mode)
                self._apply_led_colors_to_config(cfg, active_mode=curr_mode)
                self.write_config(cfg)
                self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])
            self._last_state["gain_db"] = gain_db
        except Exception as e:
            log.warning(f"Failed to set hardware gain: {e}")

    # --- Capacitive Mute & Hardware Per-Mode Mute ---
    def _calc_hw_mute_byte(self, active_mode: str) -> int:
        """
        Hardware Byte 4 (off_mute) controls the physical XLR analog mic preamp relay
        and hardware LED ring mute display mode.
        It should only be 0x01 (Muted) if the active mode currently displayed on the dial is muted.
        This allows Mode 2 (Headphone) and Mode 3 (Balance) to display their true level arcs
        even when Mic (Mode 1) is muted in software.
        """
        return 0x01 if bool(self.get_mode_mute(active_mode)) else 0x00

    def get_mute(self) -> bool:
        curr_mode = self.get_dial_mode()
        return self._mode_mutes.get(curr_mode, False)

    def get_mode_mute(self, mode: str) -> bool:
        if mode == "mix":
            return bool(self._mode_mutes.get("gain", False) and self._mode_mutes.get("hp", False))
        return bool(self._mode_mutes.get(mode, False))

    def set_mode_mute(self, mode: str, muted: bool, transient: bool = False):
        self._mode_mutes[mode] = bool(muted)
        if mode == "mix":
            self._mode_mutes["gain"] = bool(muted)
            self._mode_mutes["hp"] = bool(muted)

        curr_mode = self.get_dial_mode()
        try:
            cfg = self.read_config()
            curr_mode_muted = self.get_mode_mute(curr_mode)
            if transient:
                self.notify_user_interaction(mode)
                self._trigger_transient_peek(mode, cfg)
            else:
                cfg[self.profile.off_mute] = self._calc_hw_mute_byte(curr_mode)
                self._apply_led_colors_to_config(cfg, active_mode=curr_mode)
                self.write_config(cfg)
                self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])
            self._last_state["mute"] = curr_mode_muted
        except Exception as e:
            log.warning(f"Failed to set hardware mute for mode {mode}: {e}")

    def set_mute(self, muted: bool, transient: bool = False):
        curr_mode = self.get_dial_mode()
        self.set_mode_mute(curr_mode, muted, transient=transient)

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

    # --- Hardware DSP Meter Telemetry ---
    def get_meter(self) -> Tuple[float, float]:
        """Reads instantaneous hardware DSP meter levels from Endpoint 0.
        
        Returns (peak_l, peak_r) normalized to 0.0 - 1.0.
        """
        try:
            m = self._ctrl_read(self.profile.wvalue_meter, self.profile.meter_len)
            if not m or len(m) < 5:
                return 0.0, 0.0
            raw_l = m[0]
            raw_r = m[4] if len(m) > 4 else m[0]
            
            p_l = min(1.0, ((raw_l - 144) / 80.0) ** 0.45) if raw_l > 144 else 0.0
            p_r = min(1.0, ((raw_r - 144) / 80.0) ** 0.45) if raw_r > 144 else 0.0
                
            return p_l, p_r
        except Exception:
            return 0.0, 0.0

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

    def set_headphone_volume_pct(self, pct: int, transient: bool = False):
        try:
            self._steady_hp_pct = int(pct)
            self._is_streaming_vu = False
            self.notify_user_interaction("hp")
            pct = max(0, min(100, pct))
            db = (pct / 100.0 * 60.0) - 60.0
            raw = int(db * self.profile.hp_scale)
            cfg = self.read_config()
            struct.pack_into(self.profile.hp_fmt, cfg, self.profile.off_hp_vol, raw)
            if transient:
                self._trigger_transient_peek("hp", cfg)
            else:
                curr_mode = self.get_dial_mode()
                cfg[self.profile.off_mute] = self._calc_hw_mute_byte(curr_mode)
                self._apply_led_colors_to_config(cfg, active_mode=curr_mode)
                self.write_config(cfg)
                self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])
            self._last_state["hp_volume_pct"] = pct
            self._last_state["hp_pct"] = pct
        except Exception as e:
            log.warning(f"Failed to set headphone volume: {e}")

    # --- Monitor Mix (Crossfade: 0% Mic to 100% PC) ---
    def get_monitor_mix(self) -> int:
        if self.profile.off_monitor_mix is None:
            return 50
        try:
            cfg = self.read_config()
            raw = struct.unpack_from("<H", cfg, self.profile.off_monitor_mix)[0]
            hw_pct = raw >> 8
            # Invert hardware register (0x0000 = 100% PC Right, 0x6400 = 100% Mic Left)
            return max(0, min(100, 100 - hw_pct))
        except Exception:
            return 50

    def set_monitor_mix(self, pct: int, transient: bool = False):
        if self.profile.off_monitor_mix is None:
            return
        try:
            self._steady_mix_pct = int(pct)
            self._is_streaming_vu = False
            self.notify_user_interaction("mix")
            pct = max(0, min(100, int(pct)))
            hw_pct = 100 - pct
            raw = hw_pct << 8
            cfg = self.read_config()
            struct.pack_into("<H", cfg, self.profile.off_monitor_mix, raw)
            if transient:
                self._trigger_transient_peek("mix", cfg)
            else:
                curr_mode = self.get_dial_mode()
                cfg[self.profile.off_mute] = self._calc_hw_mute_byte(curr_mode)
                self._apply_led_colors_to_config(cfg, active_mode=curr_mode)
                self.write_config(cfg)
                self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])
            self._last_state["monitor_mix_pct"] = pct
            self._last_state["monitor_mix"] = pct
        except Exception as e:
            log.warning(f"Failed to set monitor mix: {e}")

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

    # --- Dial Mode & Transient LED Peek ---
    def get_dial_mode(self) -> str:
        if self.profile.off_vol_select is None:
            return "gain"
        try:
            cfg = self.read_config()
            val = cfg[self.profile.off_vol_select]
            return self.profile.vol_select_map.get(val, "gain")
        except Exception:
            return "gain"

    def set_dial_mode(self, mode: str, permanent: bool = True):
        if self.profile.off_vol_select is None:
            return
        rev_map = {v: k for k, v in self.profile.vol_select_map.items()}
        val = rev_map.get(mode)
        if val is None:
            return
        try:
            cfg = self.read_config()
            cfg[self.profile.off_vol_select] = val
            is_mode_muted = self.get_mode_mute(mode)
            cfg[self.profile.off_mute] = self._calc_hw_mute_byte(mode)
            if permanent:
                self._steady_dial_mode = mode
                self._cancel_revert_timer()
            self._apply_led_colors_to_config(cfg, active_mode=mode)
            self.write_config(cfg)
            self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])
            self._last_state["dial_mode"] = mode
            self._last_state["mute"] = is_mode_muted
        except Exception as e:
            log.warning(f"Failed to set dial mode: {e}")

    def _trigger_transient_peek(self, target_mode: str, cfg: bytearray):
        """Temporarily illuminates target mode and reverts after 2.0s."""
        if self.profile.off_vol_select is None:
            self.write_config(cfg)
            return

        current_steady = getattr(self, "_steady_dial_mode", "gain")
        rev_map = {v: k for k, v in self.profile.vol_select_map.items()}
        target_val = rev_map.get(target_mode)

        if target_val is not None:
            cfg[self.profile.off_vol_select] = target_val
            is_target_muted = self.get_mode_mute(target_mode)
            cfg[self.profile.off_mute] = self._calc_hw_mute_byte(target_mode)
            self._apply_led_colors_to_config(cfg, active_mode=target_mode)

        if target_mode != current_steady:
            self._is_peeking = True
            self._peek_mode = target_mode
            if self._revert_timer and self._revert_timer.is_alive():
                self._revert_timer.cancel()
            self._revert_timer = threading.Timer(2.0, self._revert_to_steady_mode)
            self._revert_timer.daemon = True
            self._revert_timer.start()
        else:
            self._is_peeking = False
            self._peek_mode = None
            if self._revert_timer and self._revert_timer.is_alive():
                self._revert_timer.cancel()
            self._revert_timer = None

        self.write_config(cfg)
        self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])
        self._last_state["dial_mode"] = target_mode
        self._last_state["mute"] = bool(self.get_mode_mute(target_mode))

    def _revert_to_steady_mode(self):
        try:
            self._is_peeking = False
            self._peek_mode = None
            steady = getattr(self, "_steady_dial_mode", "gain")
            rev_map = {v: k for k, v in self.profile.vol_select_map.items()}
            val = rev_map.get(steady)
            if val is not None and self.profile.off_vol_select is not None:
                cfg = self.read_config()
                cfg[self.profile.off_vol_select] = val
                is_steady_muted = self.get_mode_mute(steady)
                cfg[self.profile.off_mute] = self._calc_hw_mute_byte(steady)
                self._apply_led_colors_to_config(cfg, active_mode=steady)
                self.write_config(cfg)
                self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])
                self._last_state["dial_mode"] = steady
                self._last_state["mute"] = is_steady_muted
                
                # Atomically sync elgato_manager's last_state to prevent false click triggers
                if hasattr(elgato_manager, "last_state") and isinstance(elgato_manager.last_state, dict):
                    elgato_manager.last_state["dial_mode"] = steady
                    elgato_manager.last_state["mute"] = is_steady_muted
        except Exception as e:
            log.debug(f"Transient revert ignored: {e}")

    def _cancel_revert_timer(self):
        timer = getattr(self, "_revert_timer", None)
        if timer and timer.is_alive():
            timer.cancel()
        self._revert_timer = None
        self._is_peeking = False
        self._peek_mode = None

    # --- Hardware RGB LED Ring Customization ---
    def set_led_colors(self, colors: Dict[str, str]):
        """Sets RGB hex colors for 'gain', 'hp', 'mix', 'mute'."""
        self._led_colors.update(colors)
        try:
            cfg = self.read_config()
            current_mode = self.get_dial_mode()
            self._apply_led_colors_to_config(cfg, active_mode=current_mode)
            self.write_config(cfg)
        except Exception as e:
            log.warning(f"Failed to set LED colors: {e}")

    def apply_mode_color(self, mode: str):
        try:
            cfg = self.read_config()
            self._apply_led_colors_to_config(cfg, active_mode=mode)
            self.write_config(cfg)
        except Exception as e:
            log.warning(f"Failed to apply mode color: {e}")

    def _apply_led_colors_to_config(self, cfg: bytearray, active_mode: str = "gain"):
        if self.profile.off_rgb_mute is not None:
            mute_hex = self._led_colors.get("mute", "#FF0000").lstrip("#")
            if len(mute_hex) == 6:
                try:
                    mr, mg, mb = int(mute_hex[0:2], 16), int(mute_hex[2:4], 16), int(mute_hex[4:6], 16)
                    cfg[self.profile.off_rgb_mute] = mr
                    cfg[self.profile.off_rgb_mute + 1] = mg
                    cfg[self.profile.off_rgb_mute + 2] = mb
                except Exception:
                    pass

        if self.profile.off_rgb_ring is not None:
            color_hex = self._led_colors.get(active_mode, "#FFFFFF").lstrip("#")
            if len(color_hex) == 6:
                try:
                    r, g, b = int(color_hex[0:2], 16), int(color_hex[2:4], 16), int(color_hex[4:6], 16)
                    for off in [self.profile.off_rgb_ring, self.profile.off_rgb_ring + 3, self.profile.off_rgb_ring + 6]:
                        if off + 2 < len(cfg):
                            cfg[off] = r
                            cfg[off + 1] = g
                            cfg[off + 2] = b
                except Exception:
                    pass

    def apply_full_config(self, hw_settings: Dict[str, Any], led_colors: Dict[str, str] = None, hardware_mute: bool = False) -> bool:
        """
        Atomically applies all hardware settings (gain, monitor mix, headphone volume, 48V phantom power,
        clipguard, low-cut filter, low impedance, dial mode, and LED colors) in a SINGLE USB control transfer.
        Provides sub-millisecond restoration and completely eliminates microphone feedback on resume.
        """
        try:
            cfg = self.read_config()
            if not cfg or len(cfg) < self.profile.config_len:
                return False

            p = self.profile

            # 1. 48V Phantom Power
            if p.off_phantom is not None:
                phantom = bool(hw_settings.get("phantom_power", False))
                cfg[p.off_phantom] = 0x01 if phantom else 0x00
                self._last_state["phantom_power"] = phantom

            # 2. Hardware Preamp Gain
            saved_gain = hw_settings.get("gain_db", self._steady_gain_db)
            gain_db = max(0.0, min(p.gain_max_db, float(saved_gain)))
            self._steady_gain_db = gain_db
            scale = getattr(p, "gain_scale", 256.0)
            raw_gain = int(round(gain_db * scale))
            struct.pack_into("<H", cfg, p.off_gain, raw_gain)
            self._last_state["gain_db"] = gain_db

            # 3. Clipguard Dual-Stage Limiter
            if p.off_clipguard is not None:
                cg = bool(hw_settings.get("clipguard", True))
                cfg[p.off_clipguard] = 0x01 if cg else 0x00
                self._last_state["clipguard"] = cg

            # 4. Low-Cut Filter
            if p.off_low_cut is not None:
                lc = str(hw_settings.get("low_cut", "80Hz"))
                val = 0 if lc == "Off" else (1 if lc == "80Hz" else 2)
                cfg[p.off_low_cut] = val
                self._last_state["low_cut"] = lc

            # 5. Low Impedance Mode
            if p.off_low_z is not None:
                lz = bool(hw_settings.get("low_impedance", False))
                cfg[p.off_low_z] = 0x01 if lz else 0x00
                self._last_state["low_z"] = lz

            # 6. Headphone Volume
            saved_hp = hw_settings.get("headphone_volume", self._steady_hp_pct)
            hp_pct = max(0, min(100, int(round(float(saved_hp)))))
            self._steady_hp_pct = hp_pct
            hp_db = (hp_pct / 100.0 * 60.0) - 60.0
            raw_hp = int(hp_db * p.hp_scale)
            struct.pack_into(p.hp_fmt, cfg, p.off_hp_vol, raw_hp)
            self._last_state["hp_volume_pct"] = hp_pct
            self._last_state["hp_pct"] = hp_pct

            # 7. Monitor Mix Crossfade (0% Mic to 100% PC)
            if p.off_monitor_mix is not None:
                saved_mix = hw_settings.get("monitor_mix", self._steady_mix_pct)
                mix_pct = max(0, min(100, int(round(float(saved_mix)))))
                self._steady_mix_pct = mix_pct
                hw_pct = 100 - mix_pct
                raw_mix = hw_pct << 8
                struct.pack_into("<H", cfg, p.off_monitor_mix, raw_mix)
                self._last_state["monitor_mix_pct"] = mix_pct
                self._last_state["monitor_mix"] = mix_pct

            # 8. Dial Mode
            steady_mode = getattr(self, "_steady_dial_mode", "gain")
            if p.off_vol_select is not None:
                rev_map = {v: k for k, v in p.vol_select_map.items()}
                val = rev_map.get(steady_mode, 0x01)
                cfg[p.off_vol_select] = val
                self._last_state["dial_mode"] = steady_mode

            # 9. Mode Mutes & Hardware Mute Byte
            self._mode_mutes["gain"] = bool(hardware_mute)
            self._mode_mutes["hp"] = False
            self._mode_mutes["mix"] = False
            if p.off_mute is not None:
                cfg[p.off_mute] = self._calc_hw_mute_byte(steady_mode)
                self._last_raw_hw_mute = bool(cfg[p.off_mute])
            self._last_state["mute"] = self.get_mode_mute(steady_mode)

            # 10. LED Colors
            if led_colors:
                self._led_colors.update(led_colors)
            self._apply_led_colors_to_config(cfg, active_mode=steady_mode)

            # Single atomic USB write
            self.write_config(cfg)

            # Synchronize elgato_manager's snapshot to avoid false diff triggers on resume
            if hasattr(elgato_manager, "last_state") and isinstance(elgato_manager.last_state, dict):
                elgato_manager.last_state.update(self._last_state)

            log.info(f"Elgato {p.display_name} full configuration applied atomically (Gain={gain_db}dB, HP={hp_pct}%, Mix={mix_pct if p.off_monitor_mix else 'N/A'}%)")
            return True
        except Exception as e:
            log.warning(f"Failed to apply full config atomically: {e}")
            return False

    # --- Live Hardware VU Meter & Interaction State ---
    def notify_user_interaction(self, mode: str = None):
        """Notifies driver that user is actively adjusting a knob/slider, pausing VU meter for 1.8s."""
        self._user_interacting = True
        if self._interaction_timer and self._interaction_timer.is_alive():
            self._interaction_timer.cancel()
        self._interaction_timer = threading.Timer(1.8, self._on_interaction_timeout)
        self._interaction_timer.daemon = True
        self._interaction_timer.start()

    def _on_interaction_timeout(self):
        self._user_interacting = False
        self._is_streaming_vu = False
        try:
            if not getattr(self, "_is_peeking", False):
                curr_mode = self.get_dial_mode()
                self.apply_mode_color(curr_mode)
        except Exception:
            pass

    def is_user_interacting(self) -> bool:
        return self._user_interacting

    def update_live_vu(self, mode: str, peak_level: float):
        """
        VU metering is driven through software matrix meters. Hardware analog registers
        (preamp gain, headphone volume, monitor crossfade) are preserved safely.
        """
        # Preserved safely: avoid corrupting analog preamp gain and headphone DAC volume registers
        return

    def get_all_state(self) -> Dict[str, Any]:
        try:
            cfg = self.read_config()
            raw_gain = struct.unpack_from("<H", cfg, self.profile.off_gain)[0]
            scale = getattr(self.profile, "gain_scale", 256.0)
            read_gain_db = round(raw_gain / scale, 1)

            dial_mode = "gain"
            if self.profile.off_vol_select is not None:
                dial_mode = self.profile.vol_select_map.get(cfg[self.profile.off_vol_select], "gain")

            raw_hw_mute = bool(cfg[self.profile.off_mute])
            if self._last_raw_hw_mute is None:
                self._last_raw_hw_mute = raw_hw_mute
            elif raw_hw_mute != self._last_raw_hw_mute:
                # User physically touched capacitive mute button on hardware
                active_mode = dial_mode
                if active_mode == "mix":
                    self._mode_mutes["gain"] = raw_hw_mute
                    self._mode_mutes["hp"] = raw_hw_mute
                    self._mode_mutes["mix"] = raw_hw_mute
                else:
                    self._mode_mutes[active_mode] = raw_hw_mute
                self._last_raw_hw_mute = raw_hw_mute

            mute = self.get_mode_mute(dial_mode)

            raw_hp = struct.unpack_from(self.profile.hp_fmt, cfg, self.profile.off_hp_vol)[0]
            hp_db = raw_hp / self.profile.hp_scale
            read_hp_pct = max(0, min(100, int(round((hp_db + 60.0) / 60.0 * 100.0))))

            monitor_mix = 50
            if self.profile.off_monitor_mix is not None:
                raw_mix = struct.unpack_from("<H", cfg, self.profile.off_monitor_mix)[0]
                hw_pct = raw_mix >> 8
                read_mix_pct = max(0, min(100, 100 - hw_pct))
            else:
                read_mix_pct = 50

            gain_db = read_gain_db
            hp_pct = read_hp_pct
            monitor_mix = read_mix_pct

            low_z = False
            if self.profile.off_low_z is not None:
                low_z = bool(cfg[self.profile.off_low_z])

            phantom = False
            if self.profile.off_phantom is not None and len(cfg) > self.profile.off_phantom:
                phantom = bool(cfg[self.profile.off_phantom])

            clipguard = True
            if self.profile.off_clipguard is not None and len(cfg) > self.profile.off_clipguard:
                clipguard = bool(cfg[self.profile.off_clipguard])

            low_cut = "80Hz"
            if self.profile.off_low_cut is not None and len(cfg) > self.profile.off_low_cut:
                val = cfg[self.profile.off_low_cut]
                low_cut = "Off" if val == 0 else ("80Hz" if val == 1 else "120Hz")

            return {
                "connected": True,
                "name": self.profile.display_name,
                "gain_db": gain_db,
                "mute": mute,
                "phantom_power": phantom,
                "clipguard": clipguard,
                "low_cut": low_cut,
                "hp_volume_pct": hp_pct,
                "monitor_mix_pct": monitor_mix,
                "dial_mode": dial_mode,
                "low_impedance": low_z,
                "led_colors": dict(self._led_colors),
                "mode_mutes": dict(self._mode_mutes),
                "serial": self.dev_info.get("serial", ""),
                "fw_version": self.dev_info.get("fw_version", ""),
            }
        except Exception as e:
            log.debug(f"[ElgatoWave] get_all_state failed: {e}")
            return {"connected": False}


class ElgatoManager:
    """Singleton coordinator discovering and managing attached Elgato Wave devices."""

    def __init__(self):
        self.active_device: Optional[ElgatoWaveDevice] = None
        self._stop_poll = False
        self._is_sleeping = False
        self._poll_thread: Optional[threading.Thread] = None
        self.on_state_changed = None # Callback when hardware dial/mute/48V changes physically
        # Serializes all libusb open/connect attempts: multiple threads (poll loop,
        # resume fast-restore, peak-monitor capture loop) call detect_device()/get_device()
        # concurrently, and concurrent libusb_open_device_with_vid_pid calls during a
        # post-resume USB hotplug storm segfault natively inside libusb. RLock so
        # on_system_resume() can hold it across reinit + detect_device() as one unit.
        self._detect_lock = threading.RLock()

    def detect_device(self) -> Optional[ElgatoWaveDevice]:
        if self._is_sleeping:
            return None
        with self._detect_lock:
            if self._is_sleeping:
                return None
            if self.active_device and self.active_device.is_connected():
                return self.active_device
            for p in ELGATO_PROFILES:
                dev = ElgatoWaveDevice(p)
                if dev.connect():
                    self.active_device = dev
                    self._start_sync_loop()
                    return dev
        return None

    def get_device(self) -> Optional[ElgatoWaveDevice]:
        if self._is_sleeping:
            return None
        if self.active_device and self.active_device.is_connected():
            return self.active_device
        return self.detect_device()

    def on_system_suspend(self):
        """Cleanly stops background sync thread and releases USB interface before suspend."""
        self._is_sleeping = True
        self._stop_poll = True
        if self._poll_thread and self._poll_thread.is_alive():
            try:
                self._poll_thread.join(timeout=0.2)
            except Exception:
                pass
        self._poll_thread = None
        with self._detect_lock:
            if self.active_device:
                try:
                    self.active_device.disconnect()
                except Exception:
                    pass
            self.active_device = None

    def on_system_resume(self):
        """Restores background sync loop and reconnects Elgato hardware after wake."""
        self._stop_poll = False
        with self._detect_lock:
            # Keep other threads (peak-monitor capture loop, poll loop) locked out of
            # get_device()/detect_device() until libusb has a fresh post-resume context.
            _reinit_libusb_after_resume()
            self._is_sleeping = False
            dev = self.detect_device()
        if dev:
            self._start_sync_loop()

    def _start_sync_loop(self):
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._stop_poll = False
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _poll_loop(self):
        import time
        self.last_state = {}
        while not self._stop_poll:
            time.sleep(0.025) # 40 Hz polling for real-time, zero-latency dial and 48V sync
            if self._is_sleeping:
                time.sleep(0.5)
                continue
            dev = self.active_device
            if not dev or not dev.is_connected():
                time.sleep(0.5)
                dev = self.detect_device()
                if not dev or not dev.is_connected():
                    continue
                else:
                    self.last_state = {}
            try:
                curr = dev.get_all_state()
                if not curr.get("connected"):
                    continue
                if not self.last_state:
                    # Establish baseline snapshot on connection
                    self.last_state = dict(curr)
                else:
                    changed = {}
                    for k, v in curr.items():
                        if k in self.last_state:
                            if self.last_state[k] != v:
                                changed[k] = v
                        else:
                            self.last_state[k] = v
                    if changed:
                        self.last_state.update(changed)
                        timer = getattr(dev, "_revert_timer", None)
                        is_peeking = bool(timer and timer.is_alive() and getattr(dev, "_is_peeking", False))

                        if is_peeking:
                            # 1. Hardware dial knob clicked physically to a different mode
                            if "dial_mode" in changed and curr.get("dial_mode") != getattr(dev, "_peek_mode", None):
                                dev._cancel_revert_timer()
                                is_peeking = False
                                new_mode = curr["dial_mode"]
                                dev._steady_dial_mode = new_mode
                                is_mode_muted = dev.get_mode_mute(new_mode)
                                try:
                                    c = dev.read_config()
                                    c[dev.profile.off_mute] = dev._calc_hw_mute_byte(new_mode)
                                    dev._apply_led_colors_to_config(c, active_mode=new_mode)
                                    dev.write_config(c)
                                    dev._last_raw_hw_mute = bool(c[dev.profile.off_mute])
                                    dev._last_state["mute"] = is_mode_muted
                                    self.last_state["mute"] = is_mode_muted
                                except Exception:
                                    pass
                                if self.on_state_changed:
                                    self.on_state_changed(curr, changed)

                            # 2. Hardware knob rotated or mute sensor touched during peek
                            elif any(k in changed for k in ("gain_db", "hp_volume_pct", "monitor_mix_pct", "mute")):
                                dev.notify_user_interaction()
                                if self.on_state_changed:
                                    self.on_state_changed(curr, changed)

                        else:
                            # Steady state (not peeking)
                            if any(k in changed for k in ("gain_db", "hp_volume_pct", "monitor_mix_pct", "dial_mode")):
                                dev.notify_user_interaction()

                            if "dial_mode" in changed:
                                # User switched mode via knob click
                                new_mode = curr["dial_mode"]
                                dev._steady_dial_mode = new_mode
                                is_mode_muted = dev.get_mode_mute(new_mode)
                                try:
                                    c = dev.read_config()
                                    c[dev.profile.off_mute] = dev._calc_hw_mute_byte(new_mode)
                                    dev._apply_led_colors_to_config(c, active_mode=new_mode)
                                    dev.write_config(c)
                                    dev._last_raw_hw_mute = bool(c[dev.profile.off_mute])
                                    dev._last_state["mute"] = is_mode_muted
                                    self.last_state["mute"] = is_mode_muted
                                except Exception:
                                    pass

                            if self.on_state_changed:
                                self.on_state_changed(curr, changed)
            except Exception as e:
                log.warning(f"[ElgatoWave.Poll] Exception during poll loop: {e}")


elgato_manager = ElgatoManager()
