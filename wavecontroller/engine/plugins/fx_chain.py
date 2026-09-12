"""
WaveController Channel FX Chain Manager.
Manages pre-fader real-time PipeWire filter-chain nodes for audio channels.
"""

import os
import subprocess
import threading
import signal
import time
from typing import Dict, List, Optional, Any

from ..config_manager import config_manager
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

        if not stages:
            return None

        nodes = []
        node_links = []
        last_out_l = None
        last_out_r = None
        first_in_l = None
        first_in_r = None

        for st in stages:
            name_l = f"{st['name']}_l"
            name_r = f"{st['name']}_r"
            ctrl = st.get("control", {})
            plugin = st.get("plugin")
            st_type = st.get("type", "builtin")
            label = st.get("label")
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
        # Check channel-specific config first
        ch_fx = config_manager.get("channel_fx", {}).get(channel_id)
        if ch_fx is not None:
            if not ch_fx.get("enabled", True):
                return False
            return any(ch_fx.get(k, False) for k in (
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
