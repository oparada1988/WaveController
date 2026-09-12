"""
Unit tests for ChannelFXChain and FXChainManager in WaveController.
"""

import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from wavecontroller.engine.plugins.fx_chain import ChannelFXChain, FXChainManager
from wavecontroller.engine.plugins.models import AudioPlugin, PluginCategory, PluginFormat
from wavecontroller.engine.config_manager import config_manager


class TestFXChain(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="wave_test_fx_")
        self.chain = ChannelFXChain("test_mic")
        self.chain.CONFIG_DIR = self.test_dir

    def tearDown(self):
        self.chain.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_config_generation(self):
        # Effects Manager global toggles now hard-gate _generate_config; isolate this
        # test from whatever the real on-disk config.json happens to contain.
        global_flags = {
            "channel_fx": {},
            "dsp_highpass": True,
            "dsp_equalizer": True,
            "dsp_compressor": True,
            "dsp_limiter": True,
            "dsp_noise_suppression": False,
            "dsp_noise_gate": False,
            "dsp_deesser": False,
        }
        original_get = config_manager.get
        with patch.object(config_manager, "get", side_effect=lambda k, d=None: global_flags.get(k, original_get(k, d))):
            conf_path = self.chain._generate_config({
                "dsp_highpass": True,
                "dsp_equalizer": True,
                "dsp_compressor": True,
                "dsp_limiter": True,
                "dsp_noise_suppression": False
            })
        self.assertIsNotNone(conf_path)
        self.assertTrue(os.path.exists(conf_path))

        with open(conf_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check critical SPA filter-chain contract
        self.assertIn("libpipewire-module-filter-chain", content)
        self.assertIn("input.WaveController_fx_test_mic", content)
        self.assertIn("output.WaveController_fx_test_mic", content)
        self.assertIn("bq_highpass", content)
        self.assertIn("bq_peaking", content)
        self.assertIn('inputs  = [ "fx_highpass_l:In" "fx_highpass_r:In" ]', content)
        self.assertIn('outputs = [ "fx_limiter_l:Out" "fx_limiter_r:Out" ]', content)
        self.assertIn("node.autoconnect = false", content)

    def test_config_generation_with_external_lv2_plugin(self):
        lv2_plugin = AudioPlugin(
            id="lv2.example_reverb",
            name="Example Reverb",
            format=PluginFormat.LV2,
            category=PluginCategory.SPATIAL,
            plugin_uri="urn:example:reverb",
            audio_input_ports=["in_l", "in_r"],
            audio_output_ports=["out_l", "out_r"],
        )
        global_flags = {
            "channel_fx": {
                "test_mic": {
                    "enabled": True,
                    "dsp_highpass": False,
                    "dsp_noise_suppression": False,
                    "dsp_noise_gate": False,
                    "dsp_equalizer": False,
                    "dsp_compressor": False,
                    "dsp_deesser": False,
                    "dsp_limiter": False,
                    "external_plugins": {"lv2.example_reverb": True},
                }
            },
            "dsp_highpass": True,
            "dsp_equalizer": True,
            "dsp_compressor": True,
            "dsp_limiter": True,
            "dsp_noise_suppression": False,
            "dsp_noise_gate": False,
            "dsp_deesser": False,
        }
        original_get = config_manager.get
        with patch.object(config_manager, "get", side_effect=lambda k, d=None: global_flags.get(k, original_get(k, d))), \
                patch("wavecontroller.engine.plugins.fx_chain.plugin_scanner.get_plugin", return_value=lv2_plugin):
            conf_path = self.chain._generate_config()

        self.assertIsNotNone(conf_path)
        with open(conf_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("type = lv2", content)
        self.assertIn('plugin = "urn:example:reverb"', content)
        self.assertIn('inputs  = [ "fx_ext_lv2_example_reverb:in_l" "fx_ext_lv2_example_reverb:in_r" ]', content)
        self.assertIn('outputs = [ "fx_ext_lv2_example_reverb:out_l" "fx_ext_lv2_example_reverb:out_r" ]', content)

    def test_external_lv2_intensity_controls_dragonfly_macro(self):
        lv2_plugin = AudioPlugin(
            id="lv2.dragonfly_room",
            name="Dragonfly Room Reverb",
            format=PluginFormat.LV2,
            category=PluginCategory.SPATIAL,
            plugin_uri="urn:dragonfly:room",
            audio_input_ports=["in_l", "in_r"],
            audio_output_ports=["out_l", "out_r"],
        )
        lv2_plugin.parameters = [
            MagicMock(id="dry_level", min_value=0, max_value=100, default_value=80),
            MagicMock(id="early_level", min_value=0, max_value=100, default_value=10),
            MagicMock(id="late_level", min_value=0, max_value=100, default_value=20),
            MagicMock(id="decay", min_value=0, max_value=100, default_value=30),
            MagicMock(id="size", min_value=0, max_value=100, default_value=24),
        ]
        global_flags = {
            "channel_fx": {
                "test_mic": {
                    "enabled": True,
                    "dsp_highpass": False,
                    "dsp_noise_suppression": False,
                    "dsp_noise_gate": False,
                    "dsp_equalizer": False,
                    "dsp_compressor": False,
                    "dsp_deesser": False,
                    "dsp_limiter": False,
                    "external_plugins": {"lv2.dragonfly_room": True},
                    "external_plugin_intensity": {"lv2.dragonfly_room": 100},
                }
            },
            "external_plugin_enabled": {"lv2.dragonfly_room": True},
            "dsp_highpass": True,
            "dsp_equalizer": True,
            "dsp_compressor": True,
            "dsp_limiter": True,
            "dsp_noise_suppression": False,
            "dsp_noise_gate": False,
            "dsp_deesser": False,
        }
        original_get = config_manager.get
        with patch.object(config_manager, "get", side_effect=lambda k, d=None: global_flags.get(k, original_get(k, d))), \
                patch("wavecontroller.engine.plugins.fx_chain.plugin_scanner.get_plugin", return_value=lv2_plugin):
            conf_path = self.chain._generate_config()

        with open(conf_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn('"dry_level" = 65.0', content)
        self.assertIn('"late_level" = 45.0', content)

    @patch("subprocess.run")
    def test_builtin_intensity_live_update_uses_pw_cli_params(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.chain._process = MagicMock()
        self.chain._process.poll.return_value = None

        with patch.object(self.chain, "_live_control_node_id", return_value=77):
            updated = self.chain.update_builtin_intensity_live("dsp_highpass", 100)

        self.assertTrue(updated)
        args = mock_run.call_args.args[0]
        self.assertEqual(args[:4], ["pw-cli", "set-param", "77", "Props"])
        payload = json.loads(args[4])
        self.assertEqual(payload["params"], ["fx_highpass_l:Freq", 120.0, "fx_highpass_r:Freq", 120.0])

    @patch("subprocess.run")
    def test_external_lv2_intensity_live_update_uses_plugin_controls(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.chain._process = MagicMock()
        self.chain._process.poll.return_value = None
        lv2_plugin = AudioPlugin(
            id="lv2.dragonfly_room",
            name="Dragonfly Room Reverb",
            format=PluginFormat.LV2,
            category=PluginCategory.SPATIAL,
            plugin_uri="urn:dragonfly:room",
            audio_input_ports=["in_l", "in_r"],
            audio_output_ports=["out_l", "out_r"],
        )
        lv2_plugin.parameters = [
            MagicMock(id="dry_level", min_value=0, max_value=100, default_value=80),
            MagicMock(id="early_level", min_value=0, max_value=100, default_value=10),
            MagicMock(id="late_level", min_value=0, max_value=100, default_value=20),
            MagicMock(id="decay", min_value=0, max_value=100, default_value=30),
            MagicMock(id="size", min_value=0, max_value=100, default_value=24),
        ]

        with patch.object(self.chain, "_live_control_node_id", return_value=77), \
                patch("wavecontroller.engine.plugins.fx_chain.plugin_scanner.get_plugin", return_value=lv2_plugin):
            updated = self.chain.update_external_lv2_intensity_live("lv2.dragonfly_room", 100)

        self.assertTrue(updated)
        args = mock_run.call_args.args[0]
        self.assertEqual(args[:4], ["pw-cli", "set-param", "77", "Props"])
        params = json.loads(args[4])["params"]
        self.assertIn("fx_ext_lv2_dragonfly_room:dry_level", params)
        self.assertIn(65.0, params)
        self.assertIn("fx_ext_lv2_dragonfly_room:late_level", params)
        self.assertIn(45.0, params)

    def test_globally_disabled_external_lv2_plugin_is_not_hosted(self):
        lv2_plugin = AudioPlugin(
            id="lv2.example_reverb",
            name="Example Reverb",
            format=PluginFormat.LV2,
            category=PluginCategory.SPATIAL,
            plugin_uri="urn:example:reverb",
            audio_input_ports=["in_l", "in_r"],
            audio_output_ports=["out_l", "out_r"],
        )
        global_flags = {
            "channel_fx": {
                "test_mic": {
                    "enabled": True,
                    "dsp_highpass": False,
                    "dsp_noise_suppression": False,
                    "dsp_noise_gate": False,
                    "dsp_equalizer": False,
                    "dsp_compressor": False,
                    "dsp_deesser": False,
                    "dsp_limiter": False,
                    "external_plugins": {"lv2.example_reverb": True},
                }
            },
            "external_plugin_enabled": {"lv2.example_reverb": False},
            "dsp_highpass": True,
            "dsp_equalizer": True,
            "dsp_compressor": True,
            "dsp_limiter": True,
            "dsp_noise_suppression": False,
            "dsp_noise_gate": False,
            "dsp_deesser": False,
        }
        original_get = config_manager.get
        with patch.object(config_manager, "get", side_effect=lambda k, d=None: global_flags.get(k, original_get(k, d))), \
                patch("wavecontroller.engine.plugins.fx_chain.plugin_scanner.get_plugin", return_value=lv2_plugin):
            conf_path = self.chain._generate_config()

        self.assertIsNone(conf_path)

    def test_fx_manager_enablement(self):
        mgr = FXChainManager()
        # Isolate from whatever the real on-disk config.json currently contains
        # (this repo's config gets toggled interactively during manual testing).
        global_flags = {
            "channel_fx": {},
            "dsp_highpass": True,
            "dsp_noise_suppression": True,
            "dsp_noise_gate": False,
            "dsp_equalizer": True,
            "dsp_compressor": True,
            "dsp_deesser": False,
            "dsp_limiter": True,
            "channel_fx_enabled": {},
        }
        original_get = config_manager.get
        with patch.object(config_manager, "get", side_effect=lambda k, d=None: global_flags.get(k, original_get(k, d))):
            # Mic channel should be enabled if default DSP settings are active
            self.assertTrue(mgr.is_fx_enabled("mic"))
            self.assertTrue(mgr.is_fx_enabled("elgato_wave_xlr_mic"))

            # Arbitrary channel should be disabled by default unless configured
            self.assertFalse(mgr.is_fx_enabled("game_audio_sink"))

    @patch("os.killpg")
    @patch("os.getpgid")
    @patch("subprocess.Popen")
    def test_process_lifecycle(self, mock_popen, mock_getpgid, mock_killpg):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        mock_popen.return_value = mock_proc
        mock_getpgid.return_value = 99999

        fx_ports = (
            "input.WaveController_fx_test_mic:input_FL\n"
            "output.WaveController_fx_test_mic:output_FL\n"
        )
        with patch("subprocess.check_output", return_value=fx_ports):
            started = self.chain.start({
                "dsp_highpass": True,
                "dsp_equalizer": True,
                "dsp_compressor": True,
                "dsp_limiter": True,
            })
        self.assertTrue(started)
        self.assertTrue(self.chain.is_running)

        self.chain.stop()
        mock_killpg.assert_called()
        self.assertFalse(self.chain.is_running)


if __name__ == "__main__":
    unittest.main()
