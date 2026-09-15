"""
Plugin Scanner for WaveController.
Indexes installed VST3, LV2, LADSPA, CLAP, and built-in audio effects.
"""

import os
import re
import json
import glob
import shutil
import time
import threading
from typing import Dict, List, Optional, Callable, Any, Tuple
from gi.repository import GLib

from .models import AudioPlugin, PluginFormat, PluginCategory, PluginParameter
from wavecontroller.utils.logger import get_logger

log = get_logger("PluginScanner")


class PluginScanner:
    """
    Asynchronously scans standard system and user directories for audio plugins.
    Supports VST3 bundles, LV2 bundles (Turtle manifest parser), LADSPA plugins,
    and WaveController's built-in vocal DSP suite.
    """

    CACHE_FILE = os.path.expanduser("~/.config/WaveController/plugins_cache.json")
    MANIFEST_FILE = os.path.expanduser("~/.config/WaveController/installed_plugins.json")

    DEFAULT_VST3_PATHS = [
        os.path.expanduser("~/.vst3"),
        "/usr/lib/vst3",
        "/usr/local/lib/vst3"
    ]

    DEFAULT_LV2_PATHS = [
        os.path.expanduser("~/.lv2"),
        "/usr/lib/lv2",
        "/usr/local/lib/lv2"
    ]

    DEFAULT_LADSPA_PATHS = [
        os.path.expanduser("~/.ladspa"),
        "/usr/lib/ladspa",
        "/usr/lib/x86_64-linux-gnu/ladspa",
        "/usr/local/lib/ladspa"
    ]

    DEFAULT_CLAP_PATHS = [
        os.path.expanduser("~/.clap"),
        "/usr/lib/clap",
        "/usr/local/lib/clap"
    ]

    def __init__(self):
        self._plugins: Dict[str, AudioPlugin] = {}
        self._is_scanning = False
        self._lock = threading.RLock()
        self._manifest: Dict[str, Dict[str, Any]] = {}
        self._ensure_user_dirs()
        self._load_manifest()
        self._load_cache()

    def _ensure_user_dirs(self):
        """Ensure standard user plugin directories exist for user convenience."""
        user_dirs = [
            os.path.expanduser("~/.vst3"),
            os.path.expanduser("~/.lv2"),
            os.path.expanduser("~/.ladspa"),
            os.path.expanduser("~/.clap"),
            os.path.expanduser("~/.config/WaveController")
        ]
        for d in user_dirs:
            try:
                os.makedirs(d, exist_ok=True)
            except Exception as e:
                log.debug(f"Could not create user directory {d}: {e}")

    @property
    def is_scanning(self) -> bool:
        with self._lock:
            return self._is_scanning

    def get_all_plugins(self) -> List[AudioPlugin]:
        """Returns all indexed audio plugins."""
        with self._lock:
            return list(self._plugins.values())

    def get_plugins_by_category(self, category: PluginCategory) -> List[AudioPlugin]:
        """Returns plugins filtered by functional category."""
        with self._lock:
            return [p for p in self._plugins.values() if p.category == category]

    def get_plugins_by_format(self, fmt: PluginFormat) -> List[AudioPlugin]:
        """Returns plugins filtered by format (VST3, LV2, BUILTIN, etc.)."""
        with self._lock:
            return [p for p in self._plugins.values() if p.format == fmt]

    def get_plugin(self, plugin_id: str) -> Optional[AudioPlugin]:
        """Get a specific plugin by its unique ID."""
        with self._lock:
            return self._plugins.get(plugin_id)

    # --------------------------------------------------------------------------
    # Manifest of app-installed plugins (only these are removable via the UI)
    # --------------------------------------------------------------------------
    def _load_manifest(self):
        try:
            if os.path.exists(self.MANIFEST_FILE):
                with open(self.MANIFEST_FILE, "r", encoding="utf-8") as f:
                    self._manifest = json.load(f)
        except Exception as e:
            log.warning(f"Failed to load plugin manifest: {e}")
            self._manifest = {}

    def _save_manifest(self):
        try:
            os.makedirs(os.path.dirname(self.MANIFEST_FILE), exist_ok=True)
            existing = {}
            if os.path.exists(self.MANIFEST_FILE):
                try:
                    with open(self.MANIFEST_FILE, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    existing = {}
            existing.update(self._manifest)
            self._manifest = existing
            tmp = f"{self.MANIFEST_FILE}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, indent=2)
            os.replace(tmp, self.MANIFEST_FILE)
        except Exception as e:
            log.warning(f"Failed to save plugin manifest: {e}")

    def _get_custom_scan_paths(self) -> List[str]:
        """Additional user-configured directories to scan, on top of the standard defaults."""
        try:
            from wavecontroller.engine.config_manager import config_manager
            return list(config_manager.get("custom_plugin_paths", []))
        except Exception:
            return []

    def add_custom_scan_path(self, path: str) -> bool:
        """Adds an extra directory to be scanned for VST3/LV2 bundles."""
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            return False
        from wavecontroller.engine.config_manager import config_manager
        paths = list(config_manager.get("custom_plugin_paths", []))
        if path not in paths:
            paths.append(path)
            config_manager.set("custom_plugin_paths", paths, immediate=True)
        return True

    def remove_custom_scan_path(self, path: str):
        """Removes a directory from the custom scan path list (does not delete anything on disk)."""
        from wavecontroller.engine.config_manager import config_manager
        paths = list(config_manager.get("custom_plugin_paths", []))
        if path in paths:
            paths.remove(path)
            config_manager.set("custom_plugin_paths", paths, immediate=True)

    def _validate_bundle(self, path: str) -> Optional[PluginFormat]:
        """Checks whether a folder is a valid VST3 or LV2 bundle. Returns the detected format, or None."""
        if not os.path.isdir(path):
            return None
        name = os.path.basename(path.rstrip("/"))
        if name.endswith(".vst3"):
            for _, _, files in os.walk(path):
                if any(f.endswith(".so") for f in files):
                    return PluginFormat.VST3
            return None
        if name.endswith(".lv2"):
            if os.path.exists(os.path.join(path, "manifest.ttl")):
                return PluginFormat.LV2
            return None
        # LV2 bundles are sometimes unpacked without a .lv2 suffix, but VST3
        # bundles must be selected by the actual .vst3 directory so package
        # folders are not mistaken for a single installable plugin.
        if os.path.exists(os.path.join(path, "manifest.ttl")):
            return PluginFormat.LV2
        return None

    def _find_installable_bundles(self, source_path: str) -> List[Tuple[str, PluginFormat]]:
        """Returns concrete plugin bundle folders to install from a selected path."""
        fmt = self._validate_bundle(source_path)
        if fmt is not None:
            return [(source_path, fmt)]

        bundles: List[Tuple[str, PluginFormat]] = []
        try:
            entries = sorted(os.listdir(source_path))
        except Exception:
            return bundles

        for entry in entries:
            child_path = os.path.join(source_path, entry)
            child_fmt = self._validate_bundle(child_path)
            if child_fmt is not None:
                bundles.append((child_path, child_fmt))
        return bundles

    def install_plugin_from_path(self, source_path: str) -> tuple:
        """
        Installs VST3/LV2 bundles selected by the user by copying them into the
        standard ~/.vst3 or ~/.lv2 directory and recording ownership in the
        manifest, so they can later be safely removed via the UI.
        Returns (success: bool, message: str).
        """
        source_path = os.path.abspath(os.path.expanduser(source_path))
        if not os.path.isdir(source_path):
            return False, "Please select the plugin's bundle folder (a .vst3 or .lv2 directory)."

        bundles = self._find_installable_bundles(source_path)
        if not bundles:
            return False, "Please select a .vst3/.lv2 plugin bundle or a folder containing plugin bundles."

        installed = []
        converted = []
        skipped = []
        errors = []

        for bundle_path, fmt in bundles:
            dest_dir = os.path.expanduser("~/.vst3" if fmt == PluginFormat.VST3 else "~/.lv2")
            os.makedirs(dest_dir, exist_ok=True)
            bundle_name = os.path.basename(bundle_path.rstrip("/"))
            dest_path = os.path.join(dest_dir, bundle_name)

            if os.path.lexists(dest_path):
                manifest_entry = self._manifest.get(dest_path, {})
                is_same_source_symlink = os.path.islink(dest_path) and os.path.realpath(dest_path) == os.path.realpath(bundle_path)
                if manifest_entry.get("source_path") == bundle_path or is_same_source_symlink:
                    if os.path.islink(dest_path):
                        try:
                            os.remove(dest_path)
                            shutil.copytree(bundle_path, dest_path, symlinks=True)
                            manifest_entry = dict(manifest_entry)
                            manifest_entry["format"] = fmt.value
                            manifest_entry["source_path"] = bundle_path
                            manifest_entry["install_path"] = dest_path
                            manifest_entry["install_method"] = "copy"
                            manifest_entry["converted_at"] = time.time()
                            manifest_entry.pop("symlink_path", None)
                            with self._lock:
                                self._manifest[dest_path] = manifest_entry
                            converted.append(bundle_name)
                        except Exception as e:
                            errors.append(f"{bundle_name}: failed to convert symlink install to copy: {e}")
                        continue
                    skipped.append(bundle_name)
                    continue
                if not manifest_entry and os.path.isdir(dest_path) and not os.path.islink(dest_path):
                    with self._lock:
                        self._manifest[dest_path] = {
                            "format": fmt.value,
                            "source_path": bundle_path,
                            "install_path": dest_path,
                            "install_method": "copy",
                            "adopted_at": time.time()
                        }
                    converted.append(bundle_name)
                    continue
                errors.append(f"'{bundle_name}' already exists at {dest_path}")
                continue

            try:
                shutil.copytree(bundle_path, dest_path, symlinks=True)
            except Exception as e:
                errors.append(f"{bundle_name}: {e}")
                continue

            with self._lock:
                self._manifest[dest_path] = {
                    "format": fmt.value,
                    "source_path": bundle_path,
                    "install_path": dest_path,
                    "install_method": "copy",
                    "installed_at": time.time()
                }
            installed.append(bundle_name)

        if installed or converted:
            with self._lock:
                self._save_manifest()
            if converted and not installed:
                return True, f"Converted {len(converted)} plugin bundle(s) to copied installs. Rescanning..."
            if converted:
                return True, f"Installed {len(installed)} and converted {len(converted)} plugin bundle(s). Rescanning..."
            return True, f"Installed {len(installed)} plugin bundle(s). Rescanning..."

        if errors:
            return False, "; ".join(errors)
        return False, f"{len(skipped)} plugin bundle(s) already installed."

    def remove_installed_plugin(self, plugin_id: str) -> tuple:
        """
        Removes a plugin previously installed via install_plugin_from_path(). Only removes
        the copied bundle WaveController created (never the original source, and never
        anything found in a system/user directory the app didn't install itself).
        Returns (success: bool, message: str).
        """
        with self._lock:
            plugin = self._plugins.get(plugin_id)
            if not plugin or not plugin.is_user_installed:
                return False, "This plugin was not installed via WaveController and cannot be removed here."
            install_path = plugin.path
            manifest_entry = self._manifest.get(install_path)

        if not manifest_entry:
            return False, "No installation record found for this plugin."

        try:
            if os.path.islink(install_path):
                os.remove(install_path)
            elif os.path.isdir(install_path):
                shutil.rmtree(install_path)
            elif os.path.exists(install_path):
                os.remove(install_path)
        except Exception as e:
            return False, f"Failed to remove plugin: {e}"

        with self._lock:
            self._manifest.pop(install_path, None)
            self._save_manifest()

        return True, f"Removed '{plugin.name}'."

    def scan_async(self, callback: Optional[Callable[[List[AudioPlugin]], None]] = None):
        """Starts plugin scanning in a background worker thread."""
        with self._lock:
            if self._is_scanning:
                log.warning("Plugin scan already in progress.")
                return
            self._is_scanning = True

        def _worker():
            try:
                log.info("Starting audio plugin discovery scan...")
                results = self._perform_scan()
                with self._lock:
                    self._plugins = results
                    self._is_scanning = False
                self._save_cache()
                log.info(f"Plugin scan complete. Found {len(results)} plugins.")

                if callback:
                    plugins_list = list(results.values())
                    GLib.idle_add(lambda: callback(plugins_list))
            except Exception as e:
                log.error(f"Error during plugin scan: {e}", exc_info=True)
                with self._lock:
                    self._is_scanning = False
                if callback:
                    with self._lock:
                        plugins_list = list(self._plugins.values())
                    GLib.idle_add(lambda: callback(plugins_list))

        t = threading.Thread(target=_worker, name="WaveController-PluginScanner", daemon=True)
        t.start()

    def scan_sync(self) -> List[AudioPlugin]:
        """Synchronously scan for plugins (useful for tests or initialization)."""
        with self._lock:
            self._is_scanning = True
        try:
            results = self._perform_scan()
            with self._lock:
                self._plugins = results
                self._is_scanning = False
            self._save_cache()
            return list(results.values())
        except Exception as e:
            log.error(f"Sync scan error: {e}")
            with self._lock:
                self._is_scanning = False
            return list(self._plugins.values())

    def _perform_scan(self) -> Dict[str, AudioPlugin]:
        """Executes discovery across all formats."""
        discovered: Dict[str, AudioPlugin] = {}

        custom_paths = self._get_custom_scan_paths()

        # 1. Register Built-in Vocal DSP Suite
        for p in self._get_builtin_plugins():
            discovered[p.id] = p

        # 2. Scan VST3
        for path in self.DEFAULT_VST3_PATHS + custom_paths:
            if os.path.isdir(path):
                vst_plugins = self._scan_vst3_directory(path)
                for p in vst_plugins:
                    discovered[p.id] = p

        # 3. Scan LV2
        for path in self.DEFAULT_LV2_PATHS + custom_paths:
            if os.path.isdir(path):
                lv2_plugins = self._scan_lv2_directory(path)
                for p in lv2_plugins:
                    discovered[p.id] = p

        # 4. Scan LADSPA
        for path in self.DEFAULT_LADSPA_PATHS:
            if os.path.isdir(path):
                ladspa_plugins = self._scan_ladspa_directory(path)
                for p in ladspa_plugins:
                    discovered[p.id] = p

        # 5. Scan CLAP
        for path in self.DEFAULT_CLAP_PATHS:
            if os.path.isdir(path):
                clap_plugins = self._scan_clap_directory(path)
                for p in clap_plugins:
                    discovered[p.id] = p

        # Mark plugins installed through WaveController's Install Effect flow as removable
        with self._lock:
            manifest_paths = set(self._manifest.keys())
        for p in discovered.values():
            if p.path in manifest_paths:
                p.is_user_installed = True

        return discovered

    # --------------------------------------------------------------------------
    # Built-in Plugins
    # --------------------------------------------------------------------------
    def _get_builtin_plugins(self) -> List[AudioPlugin]:
        """Defines WaveController's native PipeWire filter-chain DSP suite."""
        return [
            AudioPlugin(
                id="builtin.rnnoise",
                name="AI Noise Suppression (RNNoise)",
                format=PluginFormat.BUILTIN,
                category=PluginCategory.NOISE_REDUCTION,
                vendor="WaveController",
                version="1.0.0",
                description="Neural network based noise suppression that eliminates background fan noise, keyboard clicks, and room hum.",
                is_builtin=True,
                parameters=[
                    PluginParameter(id="vad_threshold", name="VAD Sensitivity", min_value=0.0, max_value=100.0, default_value=50.0, current_value=50.0, unit="%")
                ]
            ),
            AudioPlugin(
                id="builtin.noisegate",
                name="Broadcast Noise Gate",
                format=PluginFormat.BUILTIN,
                category=PluginCategory.NOISE_REDUCTION,
                vendor="WaveController",
                version="1.0.0",
                description="Mutes audio below a specified threshold to eliminate room bleed when silent.",
                is_builtin=True,
                parameters=[
                    PluginParameter(id="threshold_db", name="Threshold", min_value=-80.0, max_value=0.0, default_value=-45.0, current_value=-45.0, unit="dB"),
                    PluginParameter(id="attack_ms", name="Attack", min_value=1.0, max_value=100.0, default_value=10.0, current_value=10.0, unit="ms"),
                    PluginParameter(id="release_ms", name="Release", min_value=10.0, max_value=1000.0, default_value=150.0, current_value=150.0, unit="ms")
                ]
            ),
            AudioPlugin(
                id="builtin.highpass",
                name="Vocal Low-Cut / High-Pass",
                format=PluginFormat.BUILTIN,
                category=PluginCategory.EQUALIZER,
                vendor="WaveController",
                version="1.0.0",
                description="Attenuates low-frequency mechanical rumble, desk thumps, and microphone plosives.",
                is_builtin=True,
                parameters=[
                    PluginParameter(id="freq_hz", name="Cutoff Frequency", min_value=20.0, max_value=300.0, default_value=80.0, current_value=80.0, unit="Hz")
                ]
            ),
            AudioPlugin(
                id="builtin.peq3",
                name="Parametric Vocal Equalizer (3-Band)",
                format=PluginFormat.BUILTIN,
                category=PluginCategory.EQUALIZER,
                vendor="WaveController",
                version="1.0.0",
                description="Broadcast 3-band parametric EQ for bass warmth, speech clarity, and vocal air.",
                is_builtin=True,
                parameters=[
                    PluginParameter(id="low_gain_db", name="Bass (120 Hz)", min_value=-15.0, max_value=15.0, default_value=0.0, current_value=0.0, unit="dB"),
                    PluginParameter(id="mid_gain_db", name="Presence (2.5 kHz)", min_value=-15.0, max_value=15.0, default_value=2.0, current_value=2.0, unit="dB"),
                    PluginParameter(id="high_gain_db", name="Air (10 kHz)", min_value=-15.0, max_value=15.0, default_value=1.5, current_value=1.5, unit="dB")
                ]
            ),
            AudioPlugin(
                id="builtin.compressor",
                name="Broadcast Vocal Compressor",
                format=PluginFormat.BUILTIN,
                category=PluginCategory.DYNAMICS,
                vendor="WaveController",
                version="1.0.0",
                description="Controls vocal dynamic range, elevating quiet whispers while taming loud shouting.",
                is_builtin=True,
                parameters=[
                    PluginParameter(id="threshold_db", name="Threshold", min_value=-60.0, max_value=0.0, default_value=-18.0, current_value=-18.0, unit="dB"),
                    PluginParameter(id="ratio", name="Ratio", min_value=1.0, max_value=20.0, default_value=4.0, current_value=4.0, unit=":1"),
                    PluginParameter(id="attack_ms", name="Attack", min_value=1.0, max_value=100.0, default_value=15.0, current_value=15.0, unit="ms"),
                    PluginParameter(id="release_ms", name="Release", min_value=10.0, max_value=1000.0, default_value=100.0, current_value=100.0, unit="ms"),
                    PluginParameter(id="makeup_gain_db", name="Makeup Gain", min_value=0.0, max_value=24.0, default_value=3.0, current_value=3.0, unit="dB")
                ]
            ),
            AudioPlugin(
                id="builtin.deesser",
                name="Vocal De-Esser",
                format=PluginFormat.BUILTIN,
                category=PluginCategory.DYNAMICS,
                vendor="WaveController",
                version="1.0.0",
                description="Reduces harsh high-frequency sibilance ('s', 'sh', and 't' sounds).",
                is_builtin=True,
                parameters=[
                    PluginParameter(id="frequency_hz", name="Frequency", min_value=4000.0, max_value=10000.0, default_value=6000.0, current_value=6000.0, unit="Hz"),
                    PluginParameter(id="threshold_db", name="Threshold", min_value=-50.0, max_value=0.0, default_value=-24.0, current_value=-24.0, unit="dB"),
                    PluginParameter(id="amount_db", name="Reduction", min_value=0.0, max_value=20.0, default_value=6.0, current_value=6.0, unit="dB")
                ]
            ),
            AudioPlugin(
                id="builtin.limiter",
                name="Broadcast Peak Limiter",
                format=PluginFormat.BUILTIN,
                category=PluginCategory.DYNAMICS,
                vendor="WaveController",
                version="1.0.0",
                description="Brickwall peak limiter preventing digital clipping (0 dBFS ceiling guard).",
                is_builtin=True,
                parameters=[
                    PluginParameter(id="ceiling_db", name="Ceiling", min_value=-12.0, max_value=0.0, default_value=-0.5, current_value=-0.5, unit="dBFS"),
                    PluginParameter(id="release_ms", name="Release", min_value=10.0, max_value=500.0, default_value=50.0, current_value=50.0, unit="ms")
                ]
            )
        ]

    # --------------------------------------------------------------------------
    # VST3 Scanner
    # --------------------------------------------------------------------------
    def _scan_vst3_directory(self, base_path: str) -> List[AudioPlugin]:
        plugins = []
        try:
            entries = os.listdir(base_path)
        except Exception:
            return plugins

        for entry in entries:
            full_path = os.path.join(base_path, entry)
            if entry.endswith(".vst3"):
                plugin = self._parse_vst3_bundle(full_path)
                if plugin:
                    plugins.append(plugin)
            elif os.path.isdir(full_path):
                # Check subdirectories 1 level deep (vendor folders)
                try:
                    sub_entries = os.listdir(full_path)
                    for sub in sub_entries:
                        if sub.endswith(".vst3"):
                            sub_path = os.path.join(full_path, sub)
                            plugin = self._parse_vst3_bundle(sub_path, vendor_hint=entry)
                            if plugin:
                                plugins.append(plugin)
                except Exception:
                    pass

        return plugins

    def _parse_vst3_bundle(self, bundle_path: str, vendor_hint: str = "Unknown") -> Optional[AudioPlugin]:
        """Parses a standard .vst3 bundle directory."""
        if not os.path.isdir(bundle_path):
            return None

        bundle_name = os.path.basename(bundle_path)
        raw_name = os.path.splitext(bundle_name)[0]

        # Locate binary inside bundle
        binary_path = ""
        possible_bin_paths = [
            os.path.join(bundle_path, "Contents", "x86_64-linux", f"{raw_name}.so"),
            os.path.join(bundle_path, "Contents", "x86_64-linux"),
            os.path.join(bundle_path, f"{raw_name}.so")
        ]
        for p in possible_bin_paths:
            if os.path.isfile(p):
                binary_path = p
                break
            elif os.path.isdir(p):
                # Search for any .so inside x86_64-linux
                sos = glob.glob(os.path.join(p, "*.so"))
                if sos:
                    binary_path = sos[0]
                    break

        # Check for moduleinfo.json (Steinberg VST3 SDK metadata)
        info_paths = [
            os.path.join(bundle_path, "Contents", "Resources", "moduleinfo.json"),
            os.path.join(bundle_path, "moduleinfo.json")
        ]
        plugin_name = raw_name
        vendor = vendor_hint
        version = "1.0.0"
        description = f"VST3 Audio Plugin: {raw_name}"
        category = self._infer_category_from_name(raw_name)

        for ip in info_paths:
            if os.path.exists(ip):
                try:
                    with open(ip, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if "Name" in data:
                            plugin_name = data["Name"]
                        if "Vendor" in data:
                            vendor = data["Vendor"]
                        if "Version" in data:
                            version = data["Version"]
                        if "Description" in data:
                            description = data["Description"]
                        if "SubCategories" in data and isinstance(data["SubCategories"], list):
                            category = self._map_vst3_subcategories(data["SubCategories"])
                except Exception as e:
                    log.debug(f"Could not parse moduleinfo.json in {bundle_path}: {e}")

        plugin_id = f"vst3.{raw_name.lower().replace(' ', '_')}"

        return AudioPlugin(
            id=plugin_id,
            name=plugin_name,
            format=PluginFormat.VST3,
            category=category,
            vendor=vendor,
            version=version,
            description=description,
            path=bundle_path,
            binary_path=binary_path,
            is_builtin=False,
            has_custom_gui=True
        )

    # --------------------------------------------------------------------------
    # LV2 Scanner
    # --------------------------------------------------------------------------
    def _scan_lv2_directory(self, base_path: str) -> List[AudioPlugin]:
        plugins = []
        try:
            entries = os.listdir(base_path)
        except Exception:
            return plugins

        for entry in entries:
            full_path = os.path.join(base_path, entry)
            if entry.endswith(".lv2") and os.path.isdir(full_path):
                parsed = self._parse_lv2_bundle(full_path)
                plugins.extend(parsed)

        return plugins

    def _parse_lv2_bundle(self, bundle_path: str) -> List[AudioPlugin]:
        """Parses an LV2 bundle directory and extracts plugins from manifest.ttl."""
        manifest_path = os.path.join(bundle_path, "manifest.ttl")
        if not os.path.exists(manifest_path):
            return []

        plugins = []
        bundle_name = os.path.basename(bundle_path)
        default_name = os.path.splitext(bundle_name)[0]

        try:
            with open(manifest_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            # Find plugin declarations: <URI> a lv2:Plugin
            plugin_uris = re.findall(r'<([^>]+)>\s+a\s+lv2:Plugin', content)
            if not plugin_uris:
                # Also check without prefix or full URI
                plugin_uris = re.findall(r'<([^>]+)>\s+a\s+<http://lv2plug.in/ns/lv2core#Plugin>', content)

            # Look for binary
            binary_match = re.search(r'lv2:binary\s+<([^>]+)>', content)
            bin_name = binary_match.group(1) if binary_match else ""
            binary_path = os.path.join(bundle_path, bin_name) if bin_name else ""

            # Look for secondary ttl files referenced via rdfs:seeAlso
            see_also_files = re.findall(r'rdfs:seeAlso\s+<([^>]+)>', content)

            # Parse metadata from secondary ttl files
            detailed_meta = {}
            audio_input_ports: List[str] = []
            audio_output_ports: List[str] = []
            control_params: List[PluginParameter] = []
            for rel_file in see_also_files:
                ttl_path = os.path.join(bundle_path, rel_file)
                if os.path.exists(ttl_path):
                    try:
                        with open(ttl_path, "r", encoding="utf-8", errors="ignore") as tf:
                            ttl_data = tf.read()
                            # Extract doap:name
                            name_match = re.search(r'doap:name\s+"([^"]+)"', ttl_data)
                            if name_match:
                                detailed_meta["name"] = name_match.group(1)
                            # Extract category tags
                            for cat_key in ["Compressor", "Expander", "Limiter", "Gate", "EQ", "ParaEQ", "Reverb", "Delay", "Filter", "Utility"]:
                                if cat_key.lower() in ttl_data.lower():
                                    detailed_meta["cat"] = cat_key
                                    break
                            in_ports, out_ports, params = self._parse_lv2_ports(ttl_data)
                            audio_input_ports.extend(in_ports)
                            audio_output_ports.extend(out_ports)
                            control_params.extend(params)
                    except Exception:
                        pass

            name = detailed_meta.get("name", default_name)
            category = self._infer_category_from_name(detailed_meta.get("cat", name))

            if plugin_uris:
                for uri in plugin_uris:
                    clean_id = f"lv2.{uri.replace('://', '_').replace('/', '_').replace('#', '_').replace('.', '_')}"
                    plugins.append(AudioPlugin(
                        id=clean_id,
                        name=name,
                        format=PluginFormat.LV2,
                        category=category,
                        vendor="LV2",
                        version="1.0.0",
                        description=f"LV2 Audio Plugin: {name}",
                        path=bundle_path,
                        binary_path=binary_path,
                        plugin_uri=uri,
                        audio_input_ports=audio_input_ports,
                        audio_output_ports=audio_output_ports,
                        parameters=control_params,
                        is_builtin=False,
                        has_custom_gui=True
                    ))
            else:
                # Fallback bundle entry
                clean_id = f"lv2.{default_name.lower().replace(' ', '_')}"
                plugins.append(AudioPlugin(
                    id=clean_id,
                    name=name,
                    format=PluginFormat.LV2,
                    category=category,
                    vendor="LV2",
                    version="1.0.0",
                    description=f"LV2 Audio Plugin: {name}",
                    path=bundle_path,
                    binary_path=binary_path,
                    audio_input_ports=audio_input_ports,
                    audio_output_ports=audio_output_ports,
                    parameters=control_params,
                    is_builtin=False,
                    has_custom_gui=True
                ))
        except Exception as e:
            log.debug(f"Error reading LV2 bundle {bundle_path}: {e}")

        return plugins

    def _parse_lv2_ports(self, ttl_data: str) -> Tuple[List[str], List[str], List[PluginParameter]]:
        """Extracts LV2 audio port symbols and basic input controls from Turtle metadata."""
        audio_inputs = []
        audio_outputs = []
        controls = []

        parsed_ports = []
        for match in re.finditer(r'\[\s*(.*?)\]\s*[,\.;]', ttl_data, re.DOTALL):
            block = match.group(1)
            if "lv2:AudioPort" not in block and "lv2:ControlPort" not in block:
                continue
            symbol_match = re.search(r'lv2:symbol\s+"([^"]+)"', block)
            if not symbol_match:
                continue
            index_match = re.search(r'lv2:index\s+([0-9]+)', block)
            name_match = re.search(r'lv2:name\s+"([^"]+)"', block)
            default_match = re.search(r'lv2:default\s+([-+0-9.eE]+)', block)
            min_match = re.search(r'lv2:minimum\s+([-+0-9.eE]+)', block)
            max_match = re.search(r'lv2:maximum\s+([-+0-9.eE]+)', block)

            symbol = symbol_match.group(1)
            index = int(index_match.group(1)) if index_match else len(parsed_ports)
            parsed_ports.append((index, block, symbol, name_match, default_match, min_match, max_match))

        for _, block, symbol, name_match, default_match, min_match, max_match in sorted(parsed_ports, key=lambda p: p[0]):
            if "lv2:AudioPort" in block:
                if "lv2:InputPort" in block:
                    audio_inputs.append(symbol)
                elif "lv2:OutputPort" in block:
                    audio_outputs.append(symbol)
            elif "lv2:ControlPort" in block and "lv2:InputPort" in block:
                try:
                    default_value = float(default_match.group(1)) if default_match else 0.0
                    min_value = float(min_match.group(1)) if min_match else 0.0
                    max_value = float(max_match.group(1)) if max_match else 1.0
                except ValueError:
                    default_value = 0.0
                    min_value = 0.0
                    max_value = 1.0
                controls.append(PluginParameter(
                    id=symbol,
                    name=name_match.group(1) if name_match else symbol.replace("_", " ").title(),
                    min_value=min_value,
                    max_value=max_value,
                    default_value=default_value,
                    current_value=default_value,
                    step=0.01
                ))

        return audio_inputs, audio_outputs, controls

    # --------------------------------------------------------------------------
    # LADSPA Scanner
    # --------------------------------------------------------------------------
    def _scan_ladspa_directory(self, base_path: str) -> List[AudioPlugin]:
        plugins = []
        try:
            entries = os.listdir(base_path)
        except Exception:
            return plugins

        for entry in entries:
            if entry.endswith(".so"):
                full_path = os.path.join(base_path, entry)
                raw_name = entry[:-3]
                cat = self._infer_category_from_name(raw_name)

                # Special-case RNNoise ladspa
                if "rnnoise" in raw_name.lower():
                    cat = PluginCategory.NOISE_REDUCTION

                plugin_id = f"ladspa.{raw_name.lower()}"
                plugins.append(AudioPlugin(
                    id=plugin_id,
                    name=raw_name.replace("_", " ").title(),
                    format=PluginFormat.LADSPA,
                    category=cat,
                    vendor="LADSPA",
                    version="1.0.0",
                    description=f"LADSPA Audio Plugin: {raw_name}",
                    path=full_path,
                    binary_path=full_path,
                    is_builtin=False,
                    has_custom_gui=False
                ))

        return plugins

    # --------------------------------------------------------------------------
    # CLAP Scanner
    # --------------------------------------------------------------------------
    def _scan_clap_directory(self, base_path: str) -> List[AudioPlugin]:
        plugins = []
        try:
            entries = os.listdir(base_path)
        except Exception:
            return plugins

        for entry in entries:
            if entry.endswith(".clap"):
                full_path = os.path.join(base_path, entry)
                raw_name = entry[:-5]
                plugin_id = f"clap.{raw_name.lower().replace(' ', '_')}"
                plugins.append(AudioPlugin(
                    id=plugin_id,
                    name=raw_name,
                    format=PluginFormat.CLAP,
                    category=self._infer_category_from_name(raw_name),
                    vendor="CLAP",
                    version="1.0.0",
                    description=f"CLAP Audio Plugin: {raw_name}",
                    path=full_path,
                    binary_path=full_path,
                    is_builtin=False,
                    has_custom_gui=True
                ))

        return plugins

    # --------------------------------------------------------------------------
    # Classification & Mapping Helpers
    # --------------------------------------------------------------------------
    def _infer_category_from_name(self, name: str) -> PluginCategory:
        n = name.lower()
        if any(k in n for k in ["noise", "denoise", "gate", "suppress", "clean"]):
            return PluginCategory.NOISE_REDUCTION
        if any(k in n for k in ["eq", "equalizer", "filter", "highpass", "lowpass", "bandpass"]):
            return PluginCategory.EQUALIZER
        if any(k in n for k in ["comp", "compressor", "limiter", "deess", "de-ess", "maximizer", "expander", "transient"]):
            return PluginCategory.DYNAMICS
        if any(k in n for k in ["reverb", "delay", "echo", "space", "room", "hall"]):
            return PluginCategory.SPATIAL
        if any(k in n for k in ["pitch", "tune", "chorus", "flanger", "phaser", "vibrato", "tremolo"]):
            return PluginCategory.PITCH
        if any(k in n for k in ["gain", "volume", "meter", "pan", "mono", "stereo", "analyzer", "utility"]):
            return PluginCategory.UTILITY
        return PluginCategory.OTHER

    def _map_vst3_subcategories(self, sub_categories: List[str]) -> PluginCategory:
        text = " ".join(sub_categories).lower()
        if "restoration" in text or "denoise" in text or "gate" in text:
            return PluginCategory.NOISE_REDUCTION
        if "eq" in text or "filter" in text:
            return PluginCategory.EQUALIZER
        if "dynamics" in text or "compressor" in text or "limiter" in text:
            return PluginCategory.DYNAMICS
        if "reverb" in text or "delay" in text:
            return PluginCategory.SPATIAL
        if "pitch" in text or "modulation" in text:
            return PluginCategory.PITCH
        if "tools" in text or "utility" in text:
            return PluginCategory.UTILITY
        return PluginCategory.OTHER

    # --------------------------------------------------------------------------
    # Cache Persistence
    # --------------------------------------------------------------------------
    def _load_cache(self):
        if not os.path.exists(self.CACHE_FILE):
            # First initialization: populate with built-ins
            for p in self._get_builtin_plugins():
                self._plugins[p.id] = p
            return

        try:
            with open(self.CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                plugins_raw = data.get("plugins", {})
                for pid, pdata in plugins_raw.items():
                    self._plugins[pid] = AudioPlugin.from_dict(pdata)
            # Ensure built-ins are always present
            for p in self._get_builtin_plugins():
                self._plugins[p.id] = p
            log.info(f"Loaded {len(self._plugins)} plugins from cache.")
        except Exception as e:
            log.warning(f"Failed to load plugins cache: {e}")
            for p in self._get_builtin_plugins():
                self._plugins[p.id] = p

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(self.CACHE_FILE), exist_ok=True)
            data = {
                "version": 1,
                "count": len(self._plugins),
                "plugins": {pid: p.to_dict() for pid, p in self._plugins.items()}
            }
            with open(self.CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log.debug(f"Saved {len(self._plugins)} plugins to cache.")
        except Exception as e:
            log.warning(f"Failed to write plugins cache: {e}")


# Singleton instance
plugin_scanner = PluginScanner()
