import os
import json
import threading
from gi.repository import GLib
from wavecontroller.utils.logger import get_logger

log = get_logger("ConfigManager")

class ConfigManager:
    """
    Thread-safe, persistent configuration manager for WaveController.
    Stores all application state, channels, sub-mixes, app mappings,
    custom device names, and hardware settings in ~/.config/WaveController/config.json.
    """
    
    DEFAULT_CONFIG = {
        "version": 1,
        "first_run_completed": False,
        "channels": [],
        "mixes": [],
        "assigned_apps": {},
        "system_defaults_enabled": False,
        "pipewire_quantum": 512,
        "sidebar_collapsed": False,
        "channel_states": {},
        "channel_master_states": {},
        "mix_states": {},
        "device_aliases": {},
        "tracked_devices": [],
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
                "mute": "#FF0000"
            },
            "exclusive_mic_lock": True,
            "exclusive_output_lock": True
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
                log.error(f"Error reading config file, falling back to defaults: {e}")
        
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

    def save(self, immediate: bool = True):
        """Alias for save_now() to ensure backward compatibility."""
        self.save_now()

    def save_now(self):
        """Atomically writes configuration to disk using a temporary file."""
        with self._lock:
            tmp_file = f"{self.config_file}.tmp"
            try:
                os.makedirs(self.config_dir, exist_ok=True)
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_file, self.config_file)
            except Exception as e:
                log.error(f"Failed to write config to {self.config_file}: {e}")
                if os.path.exists(tmp_file):
                    try:
                        os.remove(tmp_file)
                    except OSError:
                        pass

    def export_backup(self, target_path: str) -> bool:
        """Exports current full configuration to an external JSON backup file."""
        with self._lock:
            try:
                export_data = dict(self._data)
                export_data["_backup_metadata"] = {
                    "app": "WaveController",
                    "version": export_data.get("version", 1)
                }
                os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
                with open(target_path, "w", encoding="utf-8") as f:
                    json.dump(export_data, f, indent=2, ensure_ascii=False)
                return True
            except Exception as e:
                log.error(f"Export backup failed: {e}")
                return False

    def import_backup(self, source_path: str) -> bool:
        """Validates and imports an external configuration backup JSON file."""
        with self._lock:
            try:
                if not os.path.exists(source_path):
                    return False
                with open(source_path, "r", encoding="utf-8") as f:
                    imported = json.load(f)
                if not isinstance(imported, dict):
                    return False
                
                # Verify required structure
                if "channels" not in imported and "mixes" not in imported:
                    return False
                
                clean_data = dict(self.DEFAULT_CONFIG)
                clean_data.update(imported)
                clean_data.pop("_backup_metadata", None)
                self._data = clean_data
                self.save_now()
                return True
            except Exception as e:
                log.error(f"Import backup failed: {e}")
                return False

    def reset_to_defaults(self) -> bool:
        """Resets current configuration completely to default schema."""
        with self._lock:
            try:
                self._data = dict(self.DEFAULT_CONFIG)
                self.save_now()
                return True
            except Exception as e:
                log.error(f"Reset to defaults failed: {e}")
                return False

config_manager = ConfigManager()
