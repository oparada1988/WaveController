"""
Unit tests for ChannelFXChain and FXChainManager in WaveController.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from wavecontroller.engine.plugins.fx_chain import ChannelFXChain, FXChainManager
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
