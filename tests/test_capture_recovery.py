import array
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wavecontroller.engine.metering.capture_driver import drain_and_calc_peaks_with_activity
from wavecontroller.engine.usb_hardware import USBHardwareManager


class CaptureActivityTests(unittest.TestCase):
    def setUp(self):
        self.proc = SimpleNamespace(
            poll=Mock(return_value=None),
            stdout=SimpleNamespace(fileno=Mock(return_value=123)),
        )

    @patch("wavecontroller.engine.metering.capture_driver.os.read", side_effect=BlockingIOError)
    def test_missing_pcm_is_distinct_from_silence(self, _read):
        self.assertEqual(drain_and_calc_peaks_with_activity(self.proc, channels=1), (0.0, 0.0, False))

    @patch(
        "wavecontroller.engine.metering.capture_driver.os.read",
        side_effect=[bytes(960), BlockingIOError],
    )
    def test_silent_pcm_is_reported_as_flowing(self, _read):
        self.assertEqual(drain_and_calc_peaks_with_activity(self.proc, channels=1), (0.0, 0.0, True))

    @patch(
        "wavecontroller.engine.metering.capture_driver.os.read",
        side_effect=[b"\x00", BlockingIOError],
    )
    def test_partial_sample_is_not_reported_as_pcm_flow(self, _read):
        self.assertEqual(drain_and_calc_peaks_with_activity(self.proc, channels=1), (0.0, 0.0, False))

    @patch(
        "wavecontroller.engine.metering.capture_driver.os.read",
        side_effect=[array.array("h", [12000] * 480).tobytes(), BlockingIOError],
    )
    def test_non_silent_pcm_produces_peaks_and_flow_status(self, _read):
        left, right, flowing = drain_and_calc_peaks_with_activity(self.proc, channels=1)
        self.assertTrue(flowing)
        self.assertGreater(left, 0.0)
        self.assertEqual(left, right)


class StartupProfileRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.manager = USBHardwareManager.__new__(USBHardwareManager)
        self.manager.discovered_devices = {
            "elgato": {"is_elgato": True, "device_id": 45},
        }
        self.card = {
            "id": 45,
            "info": {
                "params": {
                    "Profile": [{
                        "name": "output:iec958-stereo+input:mono-fallback",
                        "index": 3,
                        "classes": ["Audio/Source", "Audio/Sink"],
                    }],
                    "EnumProfile": [{
                        "name": "output:iec958-stereo+input:mono-fallback",
                        "index": 3,
                    }],
                },
            },
        }

    @patch("wavecontroller.engine.usb_hardware.time.sleep")
    @patch("wavecontroller.engine.usb_hardware.subprocess.run")
    @patch("wavecontroller.engine.usb_hardware.safe_pw_dump")
    def test_reopens_current_iec958_duplex_profile(self, dump, run, _sleep):
        dump.return_value = [self.card]
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")

        self.assertTrue(self.manager._reset_elgato_capture_profile_once())
        self.assertEqual(
            [call.args[0][-1] for call in run.call_args_list],
            ["0", "3"],
        )

    @patch("wavecontroller.engine.usb_hardware.subprocess.run")
    @patch("wavecontroller.engine.usb_hardware.safe_pw_dump")
    def test_profile_restore_failure_is_reported(self, dump, run):
        dump.return_value = [self.card]
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="profile unavailable"),
        ]

        self.assertFalse(self.manager._reset_elgato_capture_profile_once())
        self.assertEqual(run.call_count, 2)

    @patch("wavecontroller.engine.usb_hardware.safe_pw_dump")
    def test_missing_card_does_not_issue_profile_commands(self, dump):
        dump.return_value = []
        with patch("wavecontroller.engine.usb_hardware.subprocess.run") as run:
            self.assertFalse(self.manager._reset_elgato_capture_profile_once())
        run.assert_not_called()

    @patch("wavecontroller.engine.usb_hardware.time.sleep")
    def test_waits_for_capture_flow_before_reopening_profile(self, _sleep):
        self.manager._startup_capture_recovery_done = False
        self.manager.detect_connected_hardware = Mock()
        self.manager.pipewire_mgr = SimpleNamespace(peak_monitor=SimpleNamespace())
        self.manager._refresh_audio_graph_after_startup = Mock()
        self.manager._reset_elgato_capture_profile_once = Mock(return_value=True)
        with patch.object(
            self.manager,
            "_mic_capture_is_flowing",
            side_effect=[False] * 6 + [True],
        ):
            self.manager._reset_elgato_capture_profile_after_startup()

        self.manager._reset_elgato_capture_profile_once.assert_called_once_with()
        self.assertTrue(self.manager._startup_capture_recovery_done)
        self.manager._refresh_audio_graph_after_startup.assert_called_once_with()

    @patch("wavecontroller.engine.usb_hardware.time.sleep")
    def test_healthy_capture_does_not_reopen_profile(self, _sleep):
        self.manager._startup_capture_recovery_done = False
        self.manager.detect_connected_hardware = Mock()
        self.manager.pipewire_mgr = SimpleNamespace(peak_monitor=SimpleNamespace())
        self.manager._refresh_audio_graph_after_startup = Mock()
        self.manager._reset_elgato_capture_profile_once = Mock()
        with patch.object(self.manager, "_mic_capture_is_flowing", return_value=True):
            self.manager._reset_elgato_capture_profile_after_startup()

        self.manager._reset_elgato_capture_profile_once.assert_not_called()
        self.assertTrue(self.manager._startup_capture_recovery_done)


if __name__ == "__main__":
    unittest.main()
