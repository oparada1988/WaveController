import os
import json
import threading
from gi.repository import GLib

class ConfigManager:
    """
    Thread-safe, persistent configuration manager for WaveController.
    Stores all application state, channels, sub-mixes, app mappings,
    custom device names, and hardware settings in ~/.config/WaveController/config.json.
    """
    
    DEFAULT_CONFIG = {
        "version": 1,
        "channels": [
            {"id": "mic", "name": "Microphone", "type": "source", "icon": "audio-input-microphone-symbolic", "default_vol": 80, "sync_meter": False}
        ],
        "mixes": [
            {"id": "personal", "name": "Personal Mix", "subtitle": "1 output", "icon": "audio-headphones-symbolic", "color": "#3db356"}
        ],
        "assigned_apps": {
            "mic": ["System capture"]
        },
        "channel_states": {
            "mic": {
                "personal": {"volume": 80, "muted": False, "linked": True}
            }
        },
        "device_aliases": {},
        "hardware_settings": {
            "selected_input_id": None,
            "selected_output_id": None,
            "gain_db": 45,
            "phantom_power": False,
            "clipguard": True,
            "low_cut": "80Hz",
            "led_colors": {
                "gain": "#FFFFFF",
                "hp": "#2ECC71",
                "mix": "#FF9500",
                "vu": "#00E5FF",
                "mute": "#FF0000"
            },
            "vu_meter_enabled": {
                "gain": True,
                "hp": True
            }
        }
    }

    def __init__(self):
        self.config_dir = os.path.expanduser("~/.config/WaveController")
        self.config_file = os.path.join(self.config_dir, "config.json")
        self._lock = threading.RLock()
        self._debounce_source_id = None
        self._data = {}
        
        self._load()

    def _load(self):
        os.makedirs(self.config_dir, exist_ok=True)
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        # Merge with default schema
                        self._data = dict(self.DEFAULT_CONFIG)
                        self._data.update(loaded)
                        return
            except Exception as e:
                print(f"[ConfigManager] Error reading config file, falling back to defaults: {e}")
        
        # Fresh default setup
        self._data = dict(self.DEFAULT_CONFIG)
        self.save_now()

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value, immediate: bool = False):
        with self._lock:
            self._data[key] = value
        if immediate:
            self.save_now()
        else:
            self.schedule_save()

    def update(self, key_values: dict, immediate: bool = False):
        with self._lock:
            self._data.update(key_values)
        if immediate:
            self.save_now()
        else:
            self.schedule_save()

    def schedule_save(self, delay_ms: int = 400):
        """Debounced save to avoid disk thrashing during rapid slider movements."""
        def _trigger_save():
            self._debounce_source_id = None
            self.save_now()
            return False

        try:
            if self._debounce_source_id is not None:
                GLib.source_remove(self._debounce_source_id)
            self._debounce_source_id = GLib.timeout_add(delay_ms, _trigger_save)
        except Exception:
            self.save_now()

    def save_now(self):
        """Atomically writes configuration to disk using a temporary file."""
        with self._lock:
            try:
                os.makedirs(self.config_dir, exist_ok=True)
                tmp_file = f"{self.config_file}.tmp"
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_file, self.config_file)
            except Exception as e:
                print(f"[ConfigManager] Failed to write config to {self.config_file}: {e}")

config_manager = ConfigManager()
