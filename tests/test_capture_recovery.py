import array
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wavecontroller.engine.metering.capture_driver import drain_and_calc_peaks_with_activity
from wavecontroller.engine.usb_hardware import USBHardwareManager

# Header pw-record 1.6 writes before s16/48 kHz/mono PCM on a pipe (little-endian Sun AU).
PW_RECORD_AU_HEADER = bytes.fromhex("646e732e18000000ffffffff0300000080bb000001000000")


class CaptureActivityTests(unittest.TestCase):
    def setUp(self):
        self.proc = SimpleNamespace(
            poll=Mock(return_value=None),
            stdout=SimpleNamespace(fileno=Mock(return_value=123)),
        )

    @patch("wavecontroller.engine.metering.capture_driver.os.read", side_effect=BlockingIOError)
    def test_missing_pcm_is_distinct_from_silence(self, _read):
        self.assertEqual(
            drain_and_calc_peaks_with_activity(self.proc, channels=1),
            (0.0, 0.0, False, False),
        )

    @patch(
        "wavecontroller.engine.metering.capture_driver.os.read",
        side_effect=[bytes(960), BlockingIOError],
    )
    def test_silent_pcm_is_reported_as_flowing(self, _read):
        self.assertEqual(
            drain_and_calc_peaks_with_activity(self.proc, channels=1),
            (0.0, 0.0, True, False),
        )

    @patch(
        "wavecontroller.engine.metering.capture_driver.os.read",
        side_effect=[b"\x00", BlockingIOError],
    )
    def test_partial_sample_is_not_reported_as_pcm_flow(self, _read):
        self.assertEqual(
            drain_and_calc_peaks_with_activity(self.proc, channels=1),
            (0.0, 0.0, False, False),
        )

    @patch(
        "wavecontroller.engine.metering.capture_driver.os.read",
        side_effect=[PW_RECORD_AU_HEADER + bytes(960), BlockingIOError],
    )
    def test_pw_record_header_is_not_mistaken_for_signal(self, _read):
        self.assertEqual(
            drain_and_calc_peaks_with_activity(self.proc, channels=1),
            (0.0, 0.0, True, False),
        )

    @patch(
        "wavecontroller.engine.metering.capture_driver.os.read",
        side_effect=[
            PW_RECORD_AU_HEADER[:10],
            BlockingIOError,
            PW_RECORD_AU_HEADER[10:],
            BlockingIOError,
            bytes(960),
            BlockingIOError,
        ],
    )
    def test_split_pw_record_header_is_not_reported_as_pcm(self, _read):
        self.assertEqual(drain_and_calc_peaks_with_activity(self.proc, channels=1), (0.0, 0.0, False, False))
        self.assertEqual(drain_and_calc_peaks_with_activity(self.proc, channels=1), (0.0, 0.0, False, False))
        self.assertEqual(drain_and_calc_peaks_with_activity(self.proc, channels=1), (0.0, 0.0, True, False))

    @patch(
        "wavecontroller.engine.metering.capture_driver.os.read",
        side_effect=[array.array("h", [12000] * 480).tobytes(), BlockingIOError],
    )
    def test_non_silent_pcm_produces_peaks_and_flow_status(self, _read):
        left, right, flowing, has_signal = drain_and_calc_peaks_with_activity(self.proc, channels=1)
        self.assertTrue(flowing)
        self.assertTrue(has_signal)
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
    def test_missing_pcm_triggers_profile_recovery(self, _sleep):
        self.manager._startup_capture_recovery_done = False
        self.manager.detect_connected_hardware = Mock()
        self.manager.pipewire_mgr = SimpleNamespace(peak_monitor=SimpleNamespace())
        self.manager._refresh_audio_graph_after_startup = Mock()
        self.manager._reset_elgato_capture_profile_once = Mock(return_value=True)
        with patch.object(self.manager, "_mic_capture_is_healthy", side_effect=[False] * 10 + [True]):
            with patch.object(self.manager, "_mic_capture_is_flowing", return_value=False):
                self.manager._reset_elgato_capture_profile_after_startup()

        self.manager._reset_elgato_capture_profile_once.assert_called_once_with()
        self.assertTrue(self.manager._startup_capture_recovery_done)
        self.manager._refresh_audio_graph_after_startup.assert_called_once_with()

    @patch("wavecontroller.engine.usb_hardware.time.sleep")
    def test_zero_pcm_triggers_profile_recovery(self, _sleep):
        self.manager._startup_capture_recovery_done = False
        self.manager.detect_connected_hardware = Mock()
        self.manager.pipewire_mgr = SimpleNamespace(peak_monitor=SimpleNamespace())
        self.manager._refresh_audio_graph_after_startup = Mock()
        self.manager._reset_elgato_capture_profile_once = Mock(return_value=True)
        self.manager._elgato_capture_is_muted = Mock(return_value=False)
        with patch.object(self.manager, "_mic_capture_is_healthy", side_effect=[False] * 10 + [True]):
            with patch.object(self.manager, "_mic_capture_is_flowing", return_value=True):
                self.manager._reset_elgato_capture_profile_after_startup()

        self.manager._reset_elgato_capture_profile_once.assert_called_once_with()
        self.assertTrue(self.manager._startup_capture_recovery_done)
        self.manager._refresh_audio_graph_after_startup.assert_called_once_with()

    @patch("wavecontroller.engine.usb_hardware.time.sleep")
    @patch("wavecontroller.engine.usb_hardware.safe_pw_dump")
    def test_muted_source_skips_zero_pcm_recovery(self, dump, _sleep):
        dump.return_value = [{
            "id": 268,
            "info": {
                "props": {"device.id": 45, "media.class": "Audio/Source"},
                "params": {"Props": [{"mute": True, "softMute": False}]},
            },
        }]
        self.manager._startup_capture_recovery_done = False
        self.manager.detect_connected_hardware = Mock()
        self.manager.pipewire_mgr = SimpleNamespace(peak_monitor=SimpleNamespace())
        self.manager._refresh_audio_graph_after_startup = Mock()
        self.manager._reset_elgato_capture_profile_once = Mock()
        with patch.object(self.manager, "_mic_capture_is_healthy", return_value=False):
            with patch.object(self.manager, "_mic_capture_is_flowing", return_value=True):
                self.manager._reset_elgato_capture_profile_after_startup()

        self.manager._reset_elgato_capture_profile_once.assert_not_called()
        self.assertTrue(self.manager._startup_capture_recovery_done)

    @patch("wavecontroller.engine.usb_hardware.time.sleep")
    def test_healthy_capture_does_not_reopen_profile(self, _sleep):
        self.manager._startup_capture_recovery_done = False
        self.manager.detect_connected_hardware = Mock()
        self.manager.pipewire_mgr = SimpleNamespace(peak_monitor=SimpleNamespace())
        self.manager._refresh_audio_graph_after_startup = Mock()
        self.manager._reset_elgato_capture_profile_once = Mock()
        with patch.object(self.manager, "_mic_capture_is_healthy", return_value=True):
            self.manager._reset_elgato_capture_profile_after_startup()

        self.manager._reset_elgato_capture_profile_once.assert_not_called()
        self.assertTrue(self.manager._startup_capture_recovery_done)


if __name__ == "__main__":
    unittest.main()
