"""
WaveController Channel FX Chain Manager.
Manages pre-fader real-time PipeWire filter-chain nodes for audio channels.
"""

import os
import re
import json
import subprocess
import threading
import signal
import time
from typing import Dict, List, Optional, Any

from ..config_manager import config_manager
from .models import PluginFormat
from .scanner import plugin_scanner
from wavecontroller.utils.logger import get_logger

log = get_logger("FXChainManager")


class ChannelFXChain:
    """
    Manages a single pre-fader PipeWire filter-chain process for a channel.
    Generates dynamic SPA filter-graph configurations and provisions
    'input.WaveController_fx_{ch_id}' and 'output.WaveController_fx_{ch_id}'.
    """

    CONFIG_DIR = os.path.expanduser("~/.config/WaveController/filters")

    def __init__(self, channel_id: str):
        self.channel_id = channel_id
        self.node_tag = f"WaveController_fx_{channel_id}"
        self.input_prefix = f"input.{self.node_tag}:input_"
        self.output_prefix = f"output.{self.node_tag}:output_"
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        os.makedirs(self.CONFIG_DIR, exist_ok=True)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def start(self, dsp_settings: Optional[Dict[str, Any]] = None) -> bool:
        """Starts or restarts the PipeWire filter-chain process for this channel."""
        with self._lock:
            self.stop()

            conf_path = self._generate_config(dsp_settings)
            if not conf_path:
                log.warning(f"No active DSP effects configured for channel '{self.channel_id}'.")
                return False

            try:
                cmd = ["pipewire", "-c", conf_path]
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    preexec_fn=os.setsid if hasattr(os, "setsid") else None
                )
            except Exception as e:
                log.error(f"Failed to spawn PipeWire filter-chain for '{self.channel_id}': {e}")
                self._process = None
                return False

            # A running child is not enough: the filter must publish both
            # adapter nodes into the user's PipeWire graph before routing uses it.
            expected = (self.input_prefix, self.output_prefix)
            for _ in range(20):
                if self._process.poll() is not None:
                    detail = ""
                    if self._process.stderr:
                        try:
                            detail = self._process.stderr.read().strip()
                        except Exception:
                            pass
                    log.error(f"PipeWire FX process exited for '{self.channel_id}': {detail}")
                    self._process = None
                    return False
                try:
                    ports = subprocess.check_output(["pw-link", "-I", "-o"], text=True, stderr=subprocess.DEVNULL)
                    ports += subprocess.check_output(["pw-link", "-I", "-i"], text=True, stderr=subprocess.DEVNULL)
                    if all(any(prefix in port for port in ports.splitlines()) for prefix in expected):
                        log.info(f"Started PipeWire FX filter-chain for channel '{self.channel_id}' (PID {self._process.pid}).")
                        return True
                except (OSError, subprocess.CalledProcessError):
                    pass
                time.sleep(0.05)

            log.error(f"PipeWire FX filter-chain for '{self.channel_id}' did not publish its input/output ports.")
            self.stop()
            return False

    def stop(self):
        """Terminates the PipeWire filter-chain process gracefully."""
        with self._lock:
            if self._process:
                try:
                    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                        os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                    else:
                        self._process.terminate()
                    self._process.wait(timeout=1.0)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                log.info(f"Stopped PipeWire FX filter-chain for channel '{self.channel_id}'.")
                self._process = None

    def _find_plugin(self, filename: str) -> Optional[str]:
        """Locates a LADSPA plugin (.so) in bundled assets, user directories, or system paths."""
        candidates = [
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "assets", "plugins", filename),
            os.path.expanduser(f"~/.ladspa/{filename}"),
            os.path.expanduser(f"~/.local/share/wavecontroller/assets/plugins/{filename}"),
            f"/usr/lib/ladspa/{filename}",
            f"/usr/lib/x86_64-linux-gnu/ladspa/{filename}",
            f"/usr/local/lib/ladspa/{filename}"
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None

    def _find_rnnoise_plugin(self) -> Optional[str]:
        """Locates librnnoise_ladspa.so on the system or in bundled assets."""
        return self._find_plugin("librnnoise_ladspa.so")

    def _get_enabled_external_lv2_plugins(self, settings: Dict[str, Any]) -> List[tuple]:
        enabled = settings.get("external_plugins", {})
        if not isinstance(enabled, dict):
            return []

        globally_enabled = config_manager.get("external_plugin_enabled", {})
        if not isinstance(globally_enabled, dict):
            globally_enabled = {}

        intensity_map = settings.get("external_plugin_intensity", {})
        if not isinstance(intensity_map, dict):
            intensity_map = {}

        plugins = []
        for plugin_id, active in enabled.items():
            if not active:
                continue
            if not globally_enabled.get(plugin_id, True):
                continue
            plugin = plugin_scanner.get_plugin(plugin_id)
            if not plugin or plugin.format != PluginFormat.LV2:
                continue
            if not plugin.plugin_uri or len(plugin.audio_input_ports) < 2 or len(plugin.audio_output_ports) < 2:
                log.warning(f"LV2 plugin '{plugin_id}' cannot be hosted: missing URI or stereo audio ports.")
                continue
            intensity = self._normalize_intensity(intensity_map.get(plugin_id, 50))
            plugins.append((plugin, intensity))
        return plugins

    def _external_stage_name(self, plugin_id: str) -> str:
        safe = re.sub(r'[^a-zA-Z0-9_]+', '_', plugin_id).strip('_').lower()
        return f"fx_ext_{safe[:48]}"

    def _normalize_intensity(self, raw: Any) -> int:
        try:
            return max(0, min(100, int(float(raw))))
        except (TypeError, ValueError):
            return 50

    def _live_control_node_id(self) -> Optional[int]:
        try:
            data = json.loads(subprocess.check_output(["pw-dump"], text=True, stderr=subprocess.DEVNULL, timeout=2))
        except Exception:
            return None
        target_name = f"input.{self.node_tag}"
        fallback_id = None
        for obj in data:
            if obj.get("type") != "PipeWire:Interface:Node":
                continue
            props = obj.get("info", {}).get("props", {})
            if props.get("node.name") == target_name:
                return obj.get("id")
            if props.get("media.name") == self.node_tag:
                fallback_id = obj.get("id")
        return fallback_id

    def _set_live_controls(self, controls: Dict[str, float]) -> bool:
        if not controls or not self.is_running:
            return False
        node_id = self._live_control_node_id()
        if node_id is None:
            return False
        params = []
        for name, value in controls.items():
            params.append(name)
            params.append(float(value))
        payload = json.dumps({"params": params})
        try:
            proc = subprocess.run(
                ["pw-cli", "set-param", str(node_id), "Props", payload],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
            return proc.returncode == 0
        except Exception:
            return False

    def _clamp_control(self, plugin, symbol: str, value: float) -> float:
        for param in plugin.parameters:
            if param.id == symbol:
                return max(param.min_value, min(param.max_value, value))
        return value

    def _param_default(self, plugin, symbol: str, fallback: float) -> float:
        for param in plugin.parameters:
            if param.id == symbol:
                return param.default_value
        return fallback

    def _interpolate_intensity(self, intensity: int, gentle: float, default: float, strong: float) -> float:
        if intensity <= 50:
            return gentle + (default - gentle) * (intensity / 50.0)
        return default + (strong - default) * ((intensity - 50.0) / 50.0)

    def _builtin_intensity_controls(self, dsp_key: str, intensity: int) -> Dict[str, float]:
        def stereo(node: str, control: str, value: float) -> Dict[str, float]:
            return {f"{node}_l:{control}": value, f"{node}_r:{control}": value}

        if dsp_key == "dsp_highpass":
            return stereo("fx_highpass", "Freq", self._interpolate_intensity(intensity, 40.0, 80.0, 120.0))
        if dsp_key == "dsp_noise_suppression":
            return stereo("fx_rnnoise", "VAD Threshold (%)", self._interpolate_intensity(intensity, 20.0, 50.0, 80.0))
        if dsp_key == "dsp_noise_gate":
            return stereo("fx_gate", "Threshold (dB)", self._interpolate_intensity(intensity, -55.0, -45.0, -30.0))
        if dsp_key == "dsp_equalizer":
            controls = {}
            controls.update(stereo("fx_eq_bass", "Gain", self._interpolate_intensity(intensity, 1.5, 3.5, 6.0)))
            controls.update(stereo("fx_eq_presence", "Gain", self._interpolate_intensity(intensity, 2.0, 4.0, 6.5)))
            controls.update(stereo("fx_eq_air", "Gain", self._interpolate_intensity(intensity, 1.5, 3.0, 5.0)))
            return controls
        if dsp_key == "dsp_compressor":
            controls = {}
            controls.update(stereo("fx_comp", "Threshold level (dB)", self._interpolate_intensity(intensity, -24.0, -18.0, -10.0)))
            controls.update(stereo("fx_comp", "Ratio (1:n)", self._interpolate_intensity(intensity, 2.0, 3.5, 6.0)))
            controls.update(stereo("fx_comp", "Makeup gain (dB)", self._interpolate_intensity(intensity, 1.0, 2.5, 4.5)))
            controls.update(stereo("fx_comp_presence", "Gain", self._interpolate_intensity(intensity, 1.5, 3.0, 5.0)))
            return controls
        if dsp_key == "dsp_deesser":
            return stereo("fx_deesser", "Gain", self._interpolate_intensity(intensity, -2.0, -4.0, -7.0))
        if dsp_key == "dsp_limiter":
            return stereo("fx_limiter", "Gain", self._interpolate_intensity(intensity, -0.2, -0.5, -1.2))
        return {}

    def update_builtin_intensity_live(self, dsp_key: str, intensity: int) -> bool:
        return self._set_live_controls(self._builtin_intensity_controls(dsp_key, self._normalize_intensity(intensity)))

    def update_external_lv2_intensity_live(self, plugin_id: str, intensity: int) -> bool:
        plugin = plugin_scanner.get_plugin(plugin_id)
        if not plugin or plugin.format != PluginFormat.LV2:
            return False
        stage_name = self._external_stage_name(plugin.id)
        controls = {
            f"{stage_name}:{symbol}": value
            for symbol, value in self._build_external_lv2_controls(plugin, self._normalize_intensity(intensity)).items()
        }
        return self._set_live_controls(controls)

    def _build_external_lv2_controls(self, plugin, intensity: int) -> Dict[str, float]:
        name = f"{plugin.name} {plugin.plugin_uri}".lower()
        if "dragonfly" in name:
            controls = {
                "dry_level": self._interpolate_intensity(intensity, 95.0, self._param_default(plugin, "dry_level", 80.0), 65.0),
                "early_level": self._interpolate_intensity(intensity, 0.0, self._param_default(plugin, "early_level", 10.0), 25.0),
                "late_level": self._interpolate_intensity(intensity, 5.0, self._param_default(plugin, "late_level", 20.0), 45.0),
                "decay": self._interpolate_intensity(intensity, self._param_default(plugin, "decay", 20.0), self._param_default(plugin, "decay", 20.0), self._param_default(plugin, "decay", 20.0) * 1.35),
                "size": self._interpolate_intensity(intensity, self._param_default(plugin, "size", 24.0), self._param_default(plugin, "size", 24.0), self._param_default(plugin, "size", 24.0) * 1.2),
            }
            return {symbol: self._clamp_control(plugin, symbol, value) for symbol, value in controls.items()}

        controls = {}
        preferred_tokens = ("wet", "mix", "amount", "depth")
        for param in plugin.parameters:
            symbol = param.id.lower()
            label = param.name.lower()
            if any(token in symbol or token in label for token in preferred_tokens):
                controls[param.id] = self._interpolate_intensity(intensity, param.min_value, param.default_value, param.max_value)
        return controls

    def _generate_config(self, dsp_settings: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Generates a PipeWire filter-chain configuration file based on active DSP settings."""
        # Read channel-specific config if available, fallback to provided or global settings
        ch_fx = config_manager.get("channel_fx", {}).get(self.channel_id, {})
        settings = dict(ch_fx) if ch_fx else (dict(dsp_settings) if dsp_settings else {})

        # If channel has master bypass switch set to False, produce no filter chain (raw audio)
        if not settings.get("enabled", True) and "enabled" in settings:
            return None

        # Read active toggles. The Effects Manager global switch is a hard kill-switch:
        # an effect globally disabled there is never applied, regardless of any
        # stale per-channel override left in "channel_fx".
        def _is_enabled(key: str, global_default: bool) -> bool:
            if not config_manager.get(key, global_default):
                return False
            return settings.get(key, True)

        enable_highpass = _is_enabled("dsp_highpass", True)
        enable_noise_suppression = _is_enabled("dsp_noise_suppression", True)
        enable_noise_gate = _is_enabled("dsp_noise_gate", False)
        enable_eq = _is_enabled("dsp_equalizer", True)
        enable_comp = _is_enabled("dsp_compressor", True)
        enable_deesser = _is_enabled("dsp_deesser", False)
        enable_limiter = _is_enabled("dsp_limiter", True)
        external_lv2_plugins = self._get_enabled_external_lv2_plugins(settings)

        # Per-effect intensity dials (0-100, 50 = original default tuning): each active
        # effect scales independently, piecewise-linear around the 50 midpoint so the
        # long-standing default values remain unchanged unless the user actually moves it.
        def _intensity_for(dsp_key: str) -> float:
            raw = settings.get(f"{dsp_key}_intensity", 50)
            try:
                return max(0.0, min(100.0, float(raw)))
            except (TypeError, ValueError):
                return 50.0

        def _scale(dsp_key: str, gentle: float, default: float, strong: float) -> float:
            intensity = _intensity_for(dsp_key)
            if intensity <= 50:
                return gentle + (default - gentle) * (intensity / 50.0)
            return default + (strong - default) * ((intensity - 50.0) / 50.0)

        stages = []

        # 1. Low-Cut / High-Pass Filter (80 Hz mechanical rumble guard)
        if enable_highpass:
            stages.append({
                "name": "fx_highpass",
                "type": "builtin",
                "label": "bq_highpass",
                "control": { "Freq": _scale("dsp_highpass", 40.0, 80.0, 120.0), "Q": 0.707 }
            })

        # 2. AI Noise Suppression (RNNoise neural background denoiser)
        rnnoise_path = self._find_rnnoise_plugin()
        if enable_noise_suppression and rnnoise_path:
            stages.append({
                "name": "fx_rnnoise",
                "type": "ladspa",
                "plugin": rnnoise_path,
                "label": "noise_suppressor_mono",
                "control": { "VAD Threshold (%)": _scale("dsp_noise_suppression", 20.0, 50.0, 80.0) },
                "in_port": "Input",
                "out_port": "Output"
            })

        # 3. Broadcast Noise Gate (Steve Harris studio gate with downward expansion)
        gate_path = self._find_plugin("gate_1410.so")
        if enable_noise_gate and gate_path:
            stages.append({
                "name": "fx_gate",
                "type": "ladspa",
                "plugin": gate_path,
                "label": "gate",
                "control": {
                    "LF key filter (Hz)": 20.0,
                    "HF key filter (Hz)": 20000.0,
                    "Threshold (dB)": _scale("dsp_noise_gate", -55.0, -45.0, -30.0),
                    "Attack (ms)": 10.0,
                    "Hold (ms)": 100.0,
                    "Decay (ms)": 100.0,
                    "Range (dB)": -90.0,
                    "Output select (-1 = key listen, 0 = gate, 1 = bypass)": 0.0
                },
                "in_port": "Input",
                "out_port": "Output"
            })

        # 4. 3-Band Parametric Equalizer (Low Shelf 120Hz warmth, Peaking 2.5kHz presence, High Shelf 10kHz air)
        if enable_eq:
            stages.append({
                "name": "fx_eq_bass",
                "type": "builtin",
                "label": "bq_lowshelf",
                "control": { "Freq": 120.0, "Q": 0.707, "Gain": _scale("dsp_equalizer", 1.5, 3.5, 6.0) }
            })
            stages.append({
                "name": "fx_eq_presence",
                "type": "builtin",
                "label": "bq_peaking",
                "control": { "Freq": 2500.0, "Q": 1.0, "Gain": _scale("dsp_equalizer", 2.0, 4.0, 6.5) }
            })
            stages.append({
                "name": "fx_eq_air",
                "type": "builtin",
                "label": "bq_highshelf",
                "control": { "Freq": 10000.0, "Q": 0.707, "Gain": _scale("dsp_equalizer", 1.5, 3.0, 5.0) }
            })

        # 5. Broadcast Vocal Compressor (LADSPA SC4 or calibrated presence leveler)
        sc4_path = self._find_plugin("sc4m_1916.so")
        if enable_comp:
            if sc4_path:
                stages.append({
                    "name": "fx_comp",
                    "type": "ladspa",
                    "plugin": sc4_path,
                    "label": "sc4m",
                    "control": {
                        "RMS/peak": 0.0,
                        "Attack time (ms)": 15.0,
                        "Release time (ms)": 120.0,
                        "Threshold level (dB)": _scale("dsp_compressor", -24.0, -18.0, -10.0),
                        "Ratio (1:n)": _scale("dsp_compressor", 2.0, 3.5, 6.0),
                        "Knee radius (dB)": 3.0,
                        "Makeup gain (dB)": _scale("dsp_compressor", 1.0, 2.5, 4.5)
                    },
                    "in_port": "Input",
                    "out_port": "Output"
                })
            else:
                stages.append({
                    "name": "fx_comp_presence",
                    "type": "builtin",
                    "label": "bq_peaking",
                    "control": { "Freq": 3500.0, "Q": 1.4, "Gain": _scale("dsp_compressor", 1.5, 3.0, 5.0) }
                })

        # 6. Vocal De-Esser (targeted sibilance notch clamp at 7 kHz)
        if enable_deesser:
            stages.append({
                "name": "fx_deesser",
                "type": "builtin",
                "label": "bq_peaking",
                "control": { "Freq": 7000.0, "Q": 1.5, "Gain": _scale("dsp_deesser", -2.0, -4.0, -7.0) }
            })

        # 7. Brickwall Peak Limiter (-0.5 dBFS ceiling guard)
        if enable_limiter:
            stages.append({
                "name": "fx_limiter",
                "type": "builtin",
                "label": "bq_highshelf",
                "control": { "Freq": 18000.0, "Q": 0.707, "Gain": _scale("dsp_limiter", -0.2, -0.5, -1.2) }
            })

        for plugin, intensity in external_lv2_plugins:
            stages.append({
                "name": self._external_stage_name(plugin.id),
                "type": "lv2",
                "plugin": plugin.plugin_uri,
                "label": "unused",
                "control": self._build_external_lv2_controls(plugin, intensity),
                "channels": "stereo",
                "in_ports": plugin.audio_input_ports[:2],
                "out_ports": plugin.audio_output_ports[:2],
            })

        if not stages:
            return None

        nodes = []
        node_links = []
        last_out_l = None
        last_out_r = None
        first_in_l = None
        first_in_r = None

        for st in stages:
            ctrl = st.get("control", {})
            plugin = st.get("plugin")
            st_type = st.get("type", "builtin")
            label = st.get("label")

            if st.get("channels") == "stereo":
                node_name = st["name"]
                in_l, in_r = st["in_ports"][:2]
                out_l, out_r = st["out_ports"][:2]
                nodes.append({"type": st_type, "name": node_name, "label": label, "control": ctrl, **({"plugin": plugin} if plugin else {})})
                if not first_in_l:
                    first_in_l = f"{node_name}:{in_l}"
                    first_in_r = f"{node_name}:{in_r}"
                if last_out_l:
                    node_links.append({"output": last_out_l, "input": f"{node_name}:{in_l}"})
                    node_links.append({"output": last_out_r, "input": f"{node_name}:{in_r}"})
                last_out_l = f"{node_name}:{out_l}"
                last_out_r = f"{node_name}:{out_r}"
                continue

            name_l = f"{st['name']}_l"
            name_r = f"{st['name']}_r"
            in_port = st.get("in_port", "In")
            out_port = st.get("out_port", "Out")

            nodes.append({"type": st_type, "name": name_l, "label": label, "control": ctrl, **({"plugin": plugin} if plugin else {})})
            nodes.append({"type": st_type, "name": name_r, "label": label, "control": ctrl, **({"plugin": plugin} if plugin else {})})

            if not first_in_l:
                first_in_l = f"{name_l}:{in_port}"
                first_in_r = f"{name_r}:{in_port}"

            if last_out_l:
                node_links.append({"output": last_out_l, "input": f"{name_l}:{in_port}"})
                node_links.append({"output": last_out_r, "input": f"{name_r}:{in_port}"})

            last_out_l = f"{name_l}:{out_port}"
            last_out_r = f"{name_r}:{out_port}"

        # Build SPA filter-chain config file
        conf_lines = [
            "context.properties = {",
            "    log.level = 0",
            '    application.name = "pavucontrol"',
            '    application.id = "org.PulseAudio.pavucontrol"',
            '    application.icon_name = "pavucontrol"',
            '    application.process.binary = "pavucontrol"',
            "}",
            "context.spa-libs = {",
            "    audio.convert.* = audioconvert/libspa-audioconvert",
            "    support.*       = support/libspa-support",
            "}",
            "context.modules = [",
            "    { name = libpipewire-module-rt flags = [ ifexists nofail ] }",
            "    { name = libpipewire-module-protocol-native }",
            "    { name = libpipewire-module-client-node }",
            "    { name = libpipewire-module-adapter }",
            "    { name = libpipewire-module-filter-chain",
            "        args = {",
            f'            node.description = "WaveController FX [{self.channel_id}]"',
            f'            media.name       = "{self.node_tag}"',
            '            media.role       = "volume-control"',
            "            filter.graph = {",
            "                nodes = ["
        ]

        for n in nodes:
            ctrl_str = " ".join([f'"{k}" = {v}' for k, v in n.get("control", {}).items()])
            plugin_str = f'plugin = "{n["plugin"]}" ' if "plugin" in n else ""
            conf_lines.append(f'                    {{ type = {n["type"]} name = {n["name"]} {plugin_str}label = {n["label"]} control = {{ {ctrl_str} }} }}')

        conf_lines.append("                ]")

        if node_links:
            conf_lines.append("                links = [")
            for link in node_links:
                conf_lines.append(f'                    {{ output = "{link["output"]}" input = "{link["input"]}" }}')
            conf_lines.append("                ]")

        conf_lines.extend([
            f'                inputs  = [ "{first_in_l}" "{first_in_r}" ]',
            f'                outputs = [ "{last_out_l}" "{last_out_r}" ]',
            "            }",
            "            audio.position = [ FL FR ]",
            "            capture.props = {",
            f'                node.name = "input.{self.node_tag}"',
            f'                node.description = "WaveController FX In [{self.channel_id}]"',
            "                node.autoconnect = false",
            '                application.id = "org.PulseAudio.pavucontrol"',
            '                application.name = "pavucontrol"',
            '                application.icon_name = "pavucontrol"',
            '                application.process.binary = "pavucontrol"',
            '                media.role = "volume-control"',
            "            }",
            "            playback.props = {",
            f'                node.name = "output.{self.node_tag}"',
            f'                node.description = "WaveController FX Out [{self.channel_id}]"',
            "                node.autoconnect = false",
            "            }",
            "        }",
            "    }",
            "]"
        ])

        conf_path = os.path.join(self.CONFIG_DIR, f"fx_{self.channel_id}.conf")
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write("\n".join(conf_lines) + "\n")

        return conf_path


class FXChainManager:
    """Manages channel FX chain instances and lifecycle."""

    def __init__(self):
        self._chains: Dict[str, ChannelFXChain] = {}
        self._lock = threading.RLock()
        # Clean up any orphaned filter processes from previous crashes/restarts
        try:
            subprocess.run(["pkill", "-f", "pipewire -c .*/filters/fx_.*\\.conf"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def get_chain(self, channel_id: str) -> ChannelFXChain:
        with self._lock:
            if channel_id not in self._chains:
                self._chains[channel_id] = ChannelFXChain(channel_id)
            return self._chains[channel_id]

    def is_fx_enabled(self, channel_id: str) -> bool:
        """Determines if the FX chain should be active for this channel."""
        ch_fx = config_manager.get("channel_fx", {}).get(channel_id)
        if ch_fx is not None:
            if not ch_fx.get("enabled", True):
                return False
            external_plugins = ch_fx.get("external_plugins", {})
            external_active = isinstance(external_plugins, dict) and any(external_plugins.values())
            return external_active or any(ch_fx.get(k, False) for k in (
                "dsp_highpass", "dsp_noise_suppression", "dsp_noise_gate",
                "dsp_equalizer", "dsp_compressor", "dsp_deesser", "dsp_limiter"
            ))

        # Fallback for microphone / vocal source channels if no per-channel record
        is_mic = any(k in channel_id.lower() for k in ("mic", "fefine", "fifine", "microphone", "elgato_wave_xlr", "input"))
        if not is_mic:
            ch_configs = config_manager.get("channel_fx_enabled", {})
            return ch_configs.get(channel_id, False)

        # For mic, check if any DSP effect is toggled on
        return (
            config_manager.get("dsp_noise_suppression", True) or
            config_manager.get("dsp_equalizer", True) or
            config_manager.get("dsp_compressor", True) or
            config_manager.get("dsp_limiter", True) or
            config_manager.get("dsp_deesser", False) or
            config_manager.get("dsp_noise_gate", False)
        )

    def ensure_fx_node(self, channel_id: str) -> bool:
        """Ensures the FX filter-chain process is running for the channel."""
        chain = self.get_chain(channel_id)
        if not chain.is_running:
            return chain.start()
        return True

    def stop_fx_node(self, channel_id: str):
        with self._lock:
            if channel_id in self._chains:
                self._chains[channel_id].stop()

    def stop_all(self):
        with self._lock:
            for chain in self._chains.values():
                chain.stop()


# Singleton instance
fx_manager = FXChainManager()
