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
        lib_path = ctypes.util.find_library("usb-1.0") or "libusb-1.0.so.0" or "libusb-1.0.so"
        try:
            _lib = ctypes.CDLL(lib_path)
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
    off_rgb_mute: Optional[int] = None
    off_rgb_ring: Optional[int] = None


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
    off_rgb_mute=None,
    off_rgb_ring=None,
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
        self._steady_dial_mode: str = "gain"
        self._steady_gain_db: float = 45.0
        self._steady_hp_pct: int = 70
        self._steady_mix_pct: int = 50
        self._is_streaming_vu: bool = False
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
                    # Claim vendor control interface 3 for direct USB control transfers without kernel interference
                    try:
                        lib.libusb_claim_interface(handle, 3)
                    except Exception:
                        pass
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
                    _lib.libusb_release_interface(self._handle, 3)
                except Exception:
                    pass
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

    def set_gain_db(self, gain_db: float, transient: bool = False):
        try:
            self._steady_gain_db = float(gain_db)
            self._is_streaming_vu = False
            self.notify_user_interaction("gain")
            gain_db = max(0.0, min(self.profile.gain_max_db, float(gain_db)))
            raw = int((gain_db / self.profile.gain_max_db) * self.profile.gain_raw_max)
            cfg = self.read_config()
            struct.pack_into("<H", cfg, self.profile.off_gain, raw)
            if transient:
                self._trigger_transient_peek("gain", cfg)
            else:
                is_muted = self.get_mode_mute("gain")
                cfg[self.profile.off_mute] = 0x01 if is_muted else 0x00
                self._apply_led_colors_to_config(cfg, active_mode="gain")
                self.write_config(cfg)
                self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])
            self._last_state["gain_db"] = gain_db
        except Exception as e:
            log.warning(f"Failed to set hardware gain: {e}")

    # --- Capacitive Mute & Hardware Per-Mode Mute ---
    def get_mute(self) -> bool:
        curr_mode = self.get_dial_mode()
        return self._mode_mutes.get(curr_mode, False)

    def get_mode_mute(self, mode: str) -> bool:
        return self._mode_mutes.get(mode, False)

    def set_mode_mute(self, mode: str, muted: bool):
        self._mode_mutes[mode] = bool(muted)
        if mode == "mix":
            self._mode_mutes["gain"] = bool(muted)
            self._mode_mutes["hp"] = bool(muted)

        curr_mode = self.get_dial_mode()
        try:
            cfg = self.read_config()
            curr_mode_muted = self._mode_mutes.get(curr_mode, False)
            # Physical hardware mute register controls both LED ring state and mode mute
            cfg[self.profile.off_mute] = 0x01 if curr_mode_muted else 0x00
            self._apply_led_colors_to_config(cfg, active_mode=curr_mode)
            self.write_config(cfg)
            self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])
            self._last_state["mute"] = curr_mode_muted
        except Exception as e:
            log.warning(f"Failed to set hardware mute for mode {mode}: {e}")

    def set_mute(self, muted: bool):
        curr_mode = self.get_dial_mode()
        self.set_mode_mute(curr_mode, muted)

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
                is_hp_muted = self.get_mode_mute("hp")
                cfg[self.profile.off_mute] = 0x01 if is_hp_muted else 0x00
                self._apply_led_colors_to_config(cfg, active_mode="hp")
                self.write_config(cfg)
                self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])
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
                is_mix_muted = self.get_mode_mute("mix")
                cfg[self.profile.off_mute] = 0x01 if is_mix_muted else 0x00
                self._apply_led_colors_to_config(cfg, active_mode="mix")
                self.write_config(cfg)
                self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])
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
            cfg[self.profile.off_mute] = 0x01 if is_mode_muted else 0x00
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
        """Temporarily illuminates target mode and reverts after 1.8s."""
        if self.profile.off_vol_select is None:
            self.write_config(cfg)
            return

        current_steady = getattr(self, "_steady_dial_mode", "gain")
        rev_map = {v: k for k, v in self.profile.vol_select_map.items()}
        target_val = rev_map.get(target_mode)

        if target_val is not None:
            cfg[self.profile.off_vol_select] = target_val
            is_target_muted = self.get_mode_mute(target_mode)
            cfg[self.profile.off_mute] = 0x01 if is_target_muted else 0x00
            self._apply_led_colors_to_config(cfg, active_mode=target_mode)

        self.write_config(cfg)

        if target_mode != current_steady:
            self._cancel_revert_timer()
            self._revert_timer = threading.Timer(1.8, self._revert_to_steady_mode)
            self._revert_timer.daemon = True
            self._revert_timer.start()

    def _revert_to_steady_mode(self):
        try:
            steady = getattr(self, "_steady_dial_mode", "gain")
            rev_map = {v: k for k, v in self.profile.vol_select_map.items()}
            val = rev_map.get(steady)
            if val is not None and self.profile.off_vol_select is not None:
                cfg = self.read_config()
                cfg[self.profile.off_vol_select] = val
                is_steady_muted = self.get_mode_mute(steady)
                cfg[self.profile.off_mute] = 0x01 if is_steady_muted else 0x00
                self._apply_led_colors_to_config(cfg, active_mode=steady)
                self.write_config(cfg)
                self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])
                self._last_state["dial_mode"] = steady
                self._last_state["mute"] = is_steady_muted
        except Exception as e:
            log.debug(f"Transient revert ignored: {e}")

    def _cancel_revert_timer(self):
        timer = getattr(self, "_revert_timer", None)
        if timer and timer.is_alive():
            timer.cancel()
        self._revert_timer = None

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
            mode_is_muted = self.get_mode_mute(active_mode)
            # If the current mode is muted, illuminate Red.
            # If the current mode is unmuted (even if mic is muted in hardware), illuminate in active mode's color!
            mute_hex = self._led_colors.get("mute", "#FF0000").lstrip("#") if mode_is_muted else self._led_colors.get(active_mode, "#FFFFFF").lstrip("#")
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
            read_gain_db = round((raw_gain / float(self.profile.gain_raw_max)) * self.profile.gain_max_db, 1)

            dial_mode = "gain"
            if self.profile.off_vol_select is not None:
                dial_mode = self.profile.vol_select_map.get(cfg[self.profile.off_vol_select], "gain")

            raw_hw_mute = bool(cfg[self.profile.off_mute])
            if self._last_raw_hw_mute is None:
                self._last_raw_hw_mute = raw_hw_mute
            elif raw_hw_mute != self._last_raw_hw_mute:
                # User physically touched capacitive mute button on hardware
                active_mode = dial_mode
                toggled_mute = not self._mode_mutes.get(active_mode, False)
                self._mode_mutes[active_mode] = toggled_mute
                if active_mode == "mix":
                    self._mode_mutes["gain"] = toggled_mute
                    self._mode_mutes["hp"] = toggled_mute

                is_curr_muted = self._mode_mutes.get(active_mode, False)

                cfg[self.profile.off_mute] = 0x01 if is_curr_muted else 0x00
                self._apply_led_colors_to_config(cfg, active_mode=active_mode)
                try:
                    self.write_config(cfg)
                except Exception:
                    pass
                self._last_raw_hw_mute = bool(cfg[self.profile.off_mute])

            mute = self._mode_mutes.get(dial_mode, False)

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
                "monitor_mix_pct": monitor_mix,
                "dial_mode": dial_mode,
                "low_impedance": low_z,
                "led_colors": dict(self._led_colors),
                "mode_mutes": dict(self._mode_mutes),
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
        self.last_state = {}
        while not self._stop_poll:
            time.sleep(0.025) # 40 Hz polling for real-time, zero-latency dial and 48V sync
            dev = self.active_device
            if not dev or not dev.is_connected():
                time.sleep(1.0)
                continue
            try:
                curr = dev.get_all_state()
                if not curr.get("connected"):
                    continue
                if not self.last_state:
                    # Broadcast initial full state snapshot immediately on connection (filter mute from initial trigger)
                    self.last_state = dict(curr)
                    if self.on_state_changed:
                        init_diff = dict(curr)
                        init_diff.pop("mute", None)
                        self.on_state_changed(curr, init_diff)
                else:
                    changed = {}
                    for k, v in curr.items():
                        if k in self.last_state and self.last_state[k] != v:
                            changed[k] = v
                    if changed:
                        self.last_state.update(changed)
                        if any(k in changed for k in ("gain_db", "hp_volume_pct", "monitor_mix_pct", "dial_mode")):
                            dev.notify_user_interaction()

                        if "dial_mode" in changed:
                            # User switched mode via knob click
                            timer = getattr(dev, "_revert_timer", None)
                            if not timer or not timer.is_alive():
                                new_mode = curr["dial_mode"]
                                dev._steady_dial_mode = new_mode
                                is_mode_muted = dev.get_mode_mute(new_mode)
                                try:
                                    c = dev.read_config()
                                    c[dev.profile.off_mute] = 0x01 if is_mode_muted else 0x00
                                    dev._apply_led_colors_to_config(c, active_mode=new_mode)
                                    dev.write_config(c)
                                    dev._last_raw_hw_mute = bool(c[dev.profile.off_mute])
                                    dev._last_state["mute"] = is_mode_muted
                                    self.last_state["mute"] = is_mode_muted
                                except Exception:
                                    pass

                        if self.on_state_changed:
                            self.on_state_changed(curr, changed)
            except Exception:
                pass


elgato_manager = ElgatoManager()
