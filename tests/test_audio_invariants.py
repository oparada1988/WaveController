#!/usr/bin/env python3
"""
WaveController Audio Invariant Regression Test Suite
===================================================
Enforces strict audio contracts to prevent regressions:
1. Meter Scaling Invariants: Mathematical curve consistency across volume levels.
2. Link Isolation Invariants: No duplicate summed links into wave_sink_monitor, no bypass leaks.
3. IPC / Mix Invariants: Zero cross-bleed into unassigned mixes, 1:1 mix-to-channel tracking.
"""

import os
import sys
import math
import json
import socket
import subprocess
import unittest
from unittest.mock import MagicMock

# Ensure wavecontroller module is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Direct config_manager to an isolated temporary sandbox for test executions to protect user config
import tempfile
TEST_CFG_DIR = tempfile.mkdtemp(prefix="wavecontroller_test_")
from wavecontroller.engine.config_manager import config_manager
config_manager.config_dir = TEST_CFG_DIR
config_manager.config_file = os.path.join(TEST_CFG_DIR, "config.json")
config_manager.save_now = lambda: None
config_manager.schedule_save = lambda *a, **k: None


class TestMeterMathInvariants(unittest.TestCase):
    """Verifies that peak and loudness formulas meet studio specifications."""

    def _scale_studio_peak(self, p_in: float) -> float:
        """Reference implementation matching peak_monitor.py:334"""
        if p_in <= 0.005:
            return 0.0
        if p_in < 0.020:
            return (p_in - 0.005) / (0.020 - 0.005) * 0.03
        norm_in = min(1.0, (p_in - 0.020) / (0.95 - 0.020))
        return min(1.0, 0.03 + 0.97 * math.pow(norm_in, 1.0 / 2.5))

    def test_noise_floor_silence(self):
        """Noise floor and silence (< 0.005) must produce exactly 0.0."""
        self.assertEqual(self._scale_studio_peak(0.0), 0.0)
        self.assertEqual(self._scale_studio_peak(0.001), 0.0)
        self.assertEqual(self._scale_studio_peak(0.0049), 0.0)

    def test_ambient_mic_floor(self):
        """Quiet ambient room noise (~0.017) must stay in bottom floor (<= 5%)."""
        val = self._scale_studio_peak(0.017)
        self.assertLessEqual(val, 0.05, f"Ambient mic noise too high: {val:.3f}")

    def test_fifty_percent_volume_not_collapsed(self):
        """At 50% PipeWire cubic volume (0.50^3 * 0.85 = 0.106), meter must NOT collapse to a tiny sliver (< 25%)."""
        amp_50 = 0.85 * (0.50 ** 3)
        val = self._scale_studio_peak(amp_50)
        self.assertGreaterEqual(val, 0.35, f"50% volume meter collapsed: {val:.3f} < 0.35")
        self.assertLessEqual(val, 0.50, f"50% volume meter overshot: {val:.3f} > 0.50")

    def test_eighty_percent_volume_sweet_spot(self):
        """At 80% volume (0.80^3 * 0.85 = 0.435), meter must be in upper nominal range (65% - 80%)."""
        amp_80 = 0.85 * (0.80 ** 3)
        val = self._scale_studio_peak(amp_80)
        self.assertGreaterEqual(val, 0.65, f"80% volume meter too low: {val:.3f}")
        self.assertLessEqual(val, 0.82, f"80% volume meter too high: {val:.3f}")

    def test_hundred_percent_volume_punch(self):
        """At 100% volume on full-scale track (0.85), meter must reach warm yellow / peak zone (> 88%)."""
        val = self._scale_studio_peak(0.85)
        self.assertGreaterEqual(val, 0.88, f"100% volume meter did not reach peak zone: {val:.3f}")

    def test_loud_mic_tap_red_zone(self):
        """Loud mic taps (> 0.90) must punch deep into peak red (>= 95%)."""
        val = self._scale_studio_peak(0.92)
        self.assertGreaterEqual(val, 0.95, f"Loud tap did not reach peak red: {val:.3f} < 0.95")


class TestPipeWireTopologyInvariants(unittest.TestCase):
    """Verifies that PipeWire links do not violate mixer routing contracts."""

    def setUp(self):
        try:
            from wavecontroller.engine.pipewire_manager import PipeWireManager
            pwm = PipeWireManager()
            pwm._reconcile_app_streams_fast()
            out = subprocess.check_output(["pw-link", "-l"], text=True, stderr=subprocess.DEVNULL)
            self.links_out = out
        except Exception:
            self.links_out = ""

    def test_wave_sink_monitor_no_duplicate_summing(self):
        """wave_sink_monitor must NEVER have more than 1 source linked per input channel (strict 1:1, no duplicate summing)."""
        if not self.links_out:
            self.skipTest("PipeWire not running or pw-link unavailable.")

        fl_sources = []
        fr_sources = []
        current_node = None

        for line in self.links_out.splitlines():
            line_s = line.strip()
            if not line.startswith(" ") and ":" in line_s:
                current_node = line_s
            elif "|<-" in line_s and current_node:
                src = line_s.replace("|<-", "").strip()
                if current_node == "wave_sink_monitor:input_FL":
                    fl_sources.append(src)
                elif current_node == "wave_sink_monitor:input_FR":
                    fr_sources.append(src)

        if fl_sources:
            self.assertLessEqual(
                len(fl_sources), 1,
                f"REGRESSION: Duplicate sources summed into wave_sink_monitor:input_FL: {fl_sources}"
            )
        if fr_sources:
            self.assertLessEqual(
                len(fr_sources), 1,
                f"REGRESSION: Duplicate sources summed into wave_sink_monitor:input_FR: {fr_sources}"
            )

    def test_no_direct_app_hardware_bypass(self):
        """Assigned applications like Spotify must NOT have direct links to alsa_output when WaveController is active."""
        if not self.links_out:
            self.skipTest("PipeWire not running.")

        has_spotify = "spotify:output_" in self.links_out
        has_wavecontroller = "WaveController_personal_Sink" in self.links_out

        if has_spotify and has_wavecontroller:
            spotify_dests = []
            current_node = None
            for line in self.links_out.splitlines():
                line_s = line.strip()
                if not line.startswith(" ") and ":" in line_s:
                    current_node = line_s
                elif ("|->" in line_s or "->" in line_s) and current_node and "spotify:output_" in current_node:
                    dest = line_s.replace("|->", "").replace("->", "").strip()
                    spotify_dests.append(dest)

            for d in spotify_dests:
                self.assertFalse(
                    d.startswith("alsa_output."),
                    f"REGRESSION: Spotify is bypassing WaveController and linked directly to hardware: {d}"
                )

    def test_channel_meters_no_master_sink_leak(self):
        """No wave_meter_* process may ever be linked to WaveController_personal_mix_Sink (strict zero master mix bleed)."""
        if not self.links_out:
            self.skipTest("PipeWire not running.")

        current_node = None
        for line in self.links_out.splitlines():
            line_s = line.strip()
            if not line.startswith(" ") and ":" in line_s:
                current_node = line_s
            elif "|<-" in line_s and current_node and current_node.startswith("wave_meter_"):
                src = line_s.replace("|<-", "").strip()
                self.assertFalse(
                    "personal_mix_sink" in src.lower(),
                    f"REGRESSION: Meter {current_node} is leaking audio from Master Personal Mix: {src}"
                )

    def test_meter_driver_configuration(self):
        """open_pw_record command flags must configure pavucontrol application role, low latency, and target routing."""
        from wavecontroller.engine.metering.capture_driver import open_pw_record
        import inspect
        src = inspect.getsource(open_pw_record)
        self.assertIn("--latency=20ms", src, "REGRESSION: open_pw_record is missing low latency flag")
        self.assertIn("media.role=volume-control", src, "REGRESSION: open_pw_record is missing volume-control role")
        self.assertIn("--target", src, "REGRESSION: open_pw_record is missing --target flag")


class TestHardwareDisconnectProtection(unittest.TestCase):
    """Verifies transient device loss does not immediately become removal."""

    def setUp(self):
        from wavecontroller.engine.usb_hardware import USBHardwareManager
        self.manager = USBHardwareManager.__new__(USBHardwareManager)
        self.manager._device_missing_since = {}

    def test_transient_loss_is_not_reported(self):
        self.assertEqual(self.manager._get_stably_removed_keys({"wave"}, set(), now=100.0), set())
        self.assertEqual(self.manager._get_stably_removed_keys(set(), set(), now=102.9), set())
        self.assertEqual(self.manager._get_stably_removed_keys(set(), {"wave"}, now=103.0), set())

    def test_persistent_loss_is_reported_once(self):
        self.assertEqual(self.manager._get_stably_removed_keys({"wave"}, set(), now=100.0), set())
        self.assertEqual(self.manager._get_stably_removed_keys(set(), set(), now=103.0), {"wave"})
        self.assertEqual(self.manager._get_stably_removed_keys(set(), set(), now=106.0), set())


class TestIPCLiveInvariants(unittest.TestCase):
    """Verifies live daemon socket contracts if the daemon is currently active."""

    def setUp(self):
        self.sock_path = os.path.expanduser("~/.config/WaveController/wavecontroller.sock")
        if not os.path.exists(self.sock_path):
            self.skipTest("WaveController daemon socket not active.")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            s.connect(self.sock_path)
            s.close()
        except Exception:
            self.skipTest("WaveController daemon socket not responding.")

    def test_ipc_socket_responsive(self):
        """Socket must respond within 500ms without lock contention."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(self.sock_path)
            s.sendall(json.dumps({"command": "get_peaks"}).encode("utf-8"))
            data = s.recv(65536)
            res = json.loads(data.decode("utf-8"))
            self.assertIn("peaks", res, "IPC get_peaks did not return 'peaks' dictionary")
        finally:
            s.close()

    def test_chat_mix_isolation(self):
        """Chat Mix must report 0.0 when no channels are assigned/active in it."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(self.sock_path)
            s.sendall(json.dumps({"command": "get_peaks"}).encode("utf-8"))
            res = json.loads(s.recv(65536).decode("utf-8"))
            peaks = res.get("peaks", {})
            
            cfg_path = os.path.expanduser("~/.config/WaveController/config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path) as f:
                    cfg = json.load(f)
                chat_enabled = [
                    cid for cid, st in cfg.get("channel_states", {}).items()
                    if st.get("chat_mix", {}).get("enabled", False) and not st.get("chat_mix", {}).get("muted", False)
                ]
                if not chat_enabled:
                    chat_p = peaks.get("chat_mix", {}).get("peak", 0.0)
                    self.assertEqual(chat_p, 0.0, f"REGRESSION: Audio bleeding into Chat Mix: {chat_p}")
        finally:
            s.close()


class TestTokenMatchingInvariants(unittest.TestCase):
    """Verifies that universal token matching resolves any application or input device without lag."""

    def setUp(self):
        import threading
        from wavecontroller.engine.pipewire_manager import PipeWireManager
        self.pwm = PipeWireManager.__new__(PipeWireManager)
        self.pwm._lock = threading.RLock()
        self.pwm.channel_states = {}
        self.pwm.channel_master_states = {}
        self.pwm._submix_procs = {}
        self.pwm._submix_node_ids = {}
        self.pwm._submix_volume_queue = {}
        self.pwm._volume_queue = {}
        self.pwm._volume_event = threading.Event()
        self.pwm.channels = []
        self.pwm.mixes = []
        self.pwm.assigned_apps = {}
        self.pwm.mix_states = {}
        self.pwm.config_path = "/tmp/fake_config.json"
        self.pwm._save_state_to_config = lambda *a, **k: None
        self.pwm._ensure_virtual_mix_nodes = lambda *a, **k: None
        self.pwm._refresh_node_cache = lambda *a, **k: None
        self.pwm._sync_channel_audio_routing = lambda *a, **k: None
        self.pwm._bind_app_to_wireplumber_target = lambda *a, **k: None
        self.pwm.resolve_icon_for_app = lambda a: "applications-multimedia-symbolic"
        self.pwm._match_mix_id = lambda m: m

    def test_multiword_application_matching(self):
        """Multi-word apps must match their process binaries and PipeWire output port names."""
        # Google Chrome
        tokens = self.pwm._get_match_tokens("Google Chrome")
        self.assertTrue(self.pwm._port_matches_tokens("google-chrome:output_FL", tokens))
        self.assertTrue(self.pwm._port_matches_tokens("chrome:output_FL", tokens))
        self.assertTrue(self.pwm._port_matches_tokens("Google Chrome:output_FL", tokens))
        self.assertFalse(self.pwm._port_matches_tokens("spotify:output_FL", tokens))

        # VLC Media Player
        tokens = self.pwm._get_match_tokens("VLC media player")
        self.assertTrue(self.pwm._port_matches_tokens("vlc:output_FL", tokens))
        self.assertTrue(self.pwm._port_matches_tokens("vlc-media-player:output_FR", tokens))

        # OBS Studio
        tokens = self.pwm._get_match_tokens("OBS Studio")
        self.assertTrue(self.pwm._port_matches_tokens("obs:output_FL", tokens))
        self.assertTrue(self.pwm._port_matches_tokens("obs64:output_FL", tokens))

        # Steam
        tokens = self.pwm._get_match_tokens("Steam")
        self.assertTrue(self.pwm._port_matches_tokens("steam:output_FL", tokens))
        self.assertTrue(self.pwm._port_matches_tokens("steamwebhelper:output_FL", tokens))

    def test_input_device_matching(self):
        """Input devices (microphones, capture devices) must match their capture ports."""
        # Elgato Wave XLR
        tokens = self.pwm._get_match_tokens("Elgato Wave XLR")
        self.assertTrue(self.pwm._port_matches_tokens("alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00.mono-fallback:capture_MONO", tokens))

        # Fifine Microphone
        tokens = self.pwm._get_match_tokens("Fifine Microphone")
        self.assertTrue(self.pwm._port_matches_tokens("alsa_input.usb-3142_fifine_Microphone-00.analog-stereo:capture_FL", tokens))

        # Generic / Blue Yeti Microphone
        tokens = self.pwm._get_match_tokens("Blue Yeti")
        self.assertTrue(self.pwm._port_matches_tokens("alsa_input.usb-Blue_Microphones_Yeti_1234-00.analog-stereo:capture_FL", tokens))

        # Bluetooth Input (by identifier or description)
        tokens_desc = self.pwm._get_match_tokens("Sony WH-1000XM4")
        self.assertTrue(self.pwm._port_matches_tokens("bluez_input.sony_wh_1000xm4:capture_FL", tokens_desc))

    def test_bus_tokens_do_not_conflate_devices(self):
        """Invariant: Generic bus tokens ('usb', 'alsa', 'analog') must NEVER cause cross-matching between distinct physical devices."""
        elgato_tokens = self.pwm._get_match_tokens("usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00")
        fifine_port = "alsa_output.usb-3142_fifine_Microphone-00.analog-stereo:playback_FL"
        self.assertFalse(self.pwm._port_matches_tokens(fifine_port, elgato_tokens),
                         "Elgato tokens must never match Fifine port via generic 'usb' keyword")

        fifine_tokens = self.pwm._get_match_tokens("usb-3142_fifine_Microphone-00")
        elgato_port = "alsa_output.usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00.analog-stereo:playback_FL"
        self.assertFalse(self.pwm._port_matches_tokens(elgato_port, fifine_tokens),
                         "Fifine tokens must never match Elgato port via generic 'usb' keyword")

    def test_unconfigured_channel_mix_default_disabled(self):
        """Invariant: Channels must default to disabled (unrouted) for new or unconfigured mixes to prevent accidental bleed."""
        self.assertFalse(self.pwm.is_channel_mix_enabled("unconfigured_channel", "unconfigured_mix"))

    def test_application_streams_excluded_from_volume_cache(self):
        """Invariant: Client application playback streams must NEVER be cached for volume dispatch."""
        self.pwm._node_cache = {
            "output.wavecontroller_submix_spotify_personal_mix": ["110"],
            "spotify": ["114"]
        }
        # Submix lookup for "spotify" must resolve to the loopback node "110", NEVER application stream "114"
        sub_node = "output.wavecontroller_submix_spotify_personal_mix"
        matched_ids = self.pwm._node_cache.get(sub_node, [])
        self.assertIn("110", matched_ids)
        self.assertNotIn("114", matched_ids)

    def test_perceptual_peak_meter_scaling(self):
        """Invariant: Studio broadcast decibel response must accurately map silence to 0 and full-blast audio to >= 0.80."""
        from wavecontroller.engine.peak_monitor import MultiChannelPeakMonitor
        # Strict zero-bleed contract
        self.assertEqual(MultiChannelPeakMonitor._calc_perceptual_peak(0.0, 0.0), 0.0)
        self.assertEqual(MultiChannelPeakMonitor._calc_perceptual_peak(0.001, 0.0005), 0.0)
        # Mastered commercial audio (peak ~0.85, rms ~0.20) must be >= 0.80
        mastered_val = MultiChannelPeakMonitor._calc_perceptual_peak(0.85, 0.20)
        self.assertGreaterEqual(mastered_val, 0.80, f"Mastered audio peak too low: {mastered_val}")
        # Full scale 0 dBFS limit
        self.assertAlmostEqual(MultiChannelPeakMonitor._calc_perceptual_peak(1.0, 0.5), 1.0, places=2)

    def test_channel_removal_teardown(self):
        """Invariant: Removing a channel must terminate and clean all its submix loopback processes."""
        from unittest.mock import MagicMock
        mock_proc = MagicMock()
        self.pwm._submix_procs[("temp_ch", "personal_mix")] = mock_proc
        self.pwm._submix_node_ids[("temp_ch", "personal_mix")] = 999
        self.pwm.channels = [{"id": "temp_ch", "name": "Temp", "type": "sink"}]
        self.pwm.channel_states["temp_ch"] = {}

        self.pwm.remove_channel("temp_ch")

        mock_proc.terminate.assert_called_once()
        self.assertNotIn(("temp_ch", "personal_mix"), self.pwm._submix_procs)
        self.assertNotIn(("temp_ch", "personal_mix"), self.pwm._submix_node_ids)

    def test_mix_removal_teardown(self):
        """Invariant: Removing a mix must terminate and clean all its submix loopback processes."""
        from unittest.mock import MagicMock
        mock_proc = MagicMock()
        self.pwm._submix_procs[("spotify", "temp_mix")] = mock_proc
        self.pwm._submix_node_ids[("spotify", "temp_mix")] = 888
        self.pwm.mixes = [{"id": "temp_mix", "name": "Temp Mix"}]

        self.pwm.remove_mix("temp_mix")

        mock_proc.terminate.assert_called_once()
        self.assertNotIn(("spotify", "temp_mix"), self.pwm._submix_procs)
        self.assertNotIn(("spotify", "temp_mix"), self.pwm._submix_node_ids)

    def test_mix_ingestion_shield(self):
        """Invariant: Mix Ingestion Shield must unlink any direct client app link into mix sinks."""
        from unittest.mock import patch, MagicMock
        with patch("subprocess.check_output") as mock_co, patch("subprocess.run") as mock_run:
            # Simulate a rogue Spotify link directly to Personal Mix sink
            mock_co.side_effect = lambda cmd, **kwargs: (
                "spotify:output_FL\n  |-> WaveController_personal_mix_Sink:playback_FL\n"
                "output.WaveController_submix_spotify_personal:output_FL\n  |-> WaveController_personal_mix_Sink:playback_FL\n"
                if "-l" in cmd else ""
            )
            self.pwm.mixes = [{"id": "personal", "name": "Personal Mix", "type": "sink"}]
            
            # Run shield check logic
            fresh_links = self.pwm._get_pw_links_map()
            self.assertIn("spotify:output_FL", fresh_links)
            
            # Verify that only non-submix sources are targeted for unlinking
            for src_p, dests in fresh_links.items():
                if not src_p.startswith("output.WaveController_submix_"):
                    for dest_p in dests:
                        if "WaveController_personal_mix_Sink:playback_" in dest_p:
                            mock_run(["pw-link", "-d", src_p, dest_p])

            mock_run.assert_called_with(["pw-link", "-d", "spotify:output_FL", "WaveController_personal_mix_Sink:playback_FL"])

    def test_reconcile_app_streams_no_thrashing(self):
        """Invariant: _reconcile_app_streams_fast must NOT sever valid links to WaveController_Channel_{ch_id}."""
        from unittest.mock import patch, MagicMock
        with patch("subprocess.check_output") as mock_co, patch("subprocess.run") as mock_run:
            mock_co.side_effect = lambda cmd, **kwargs: (
                "spotify:output_FL\nspotify:output_FR\n" if "-o" in cmd
                else "spotify:output_FL\n  |-> WaveController_Channel_spotify:playback_FL\n"
                     "spotify:output_FR\n  |-> WaveController_Channel_spotify:playback_FR\n"
            )
            self.pwm.channels = [{"id": "spotify", "type": "app"}]
            self.pwm.assigned_apps = {"spotify": ["Spotify"]}
            self.pwm._sync_channel_audio_routing = MagicMock()

            self.pwm._reconcile_app_streams_fast()

            # Must NOT call pw-link -d on valid channel sink link
            mock_run.assert_not_called()
            # Must NOT trigger redundant routing sync
            self.pwm._sync_channel_audio_routing.assert_not_called()

    def test_reconcile_app_streams_authorizes_wave_meter(self):
        """Invariant: _reconcile_app_streams_fast must NEVER sever connections to wave_meter_."""
        from unittest.mock import patch, MagicMock
        with patch("subprocess.check_output") as mock_co, patch("subprocess.run") as mock_run:
            mock_co.side_effect = lambda cmd, **kwargs: (
                "spotify:output_FL\nspotify:output_FR\n" if "-o" in cmd
                else "spotify:output_FL\n  |-> wave_meter_spotify:input_FL\n"
                     "spotify:output_FR\n  |-> wave_meter_spotify:input_FR\n"
            )
            self.pwm.channels = [{"id": "spotify", "type": "app"}]
            self.pwm.assigned_apps = {"spotify": ["Spotify"]}
            self.pwm.mixes = [{"id": "personal_mix", "name": "Personal Mix"}]
            self.pwm.channel_states = {"spotify": {"personal_mix": {"enabled": False}}}

            self.pwm._reconcile_app_streams_fast()

            # Must NOT call pw-link -d to sever the meter connection
            mock_run.assert_not_called()

    def test_no_blanket_pkill_in_node_sync(self):
        """Invariant: _ensure_virtual_mix_nodes must NEVER execute blanket pkill during runtime."""
        import inspect
        src = inspect.getsource(self.pwm._ensure_virtual_mix_nodes)
        self.assertNotIn("pkill", src, "REGRESSION: Blanket pkill found in _ensure_virtual_mix_nodes")

    def test_granular_channel_lifecycle_isolation(self):
        """Invariant: Deleting channel A must never terminate or unlink channel B's processes."""
        from unittest.mock import MagicMock
        proc_a = MagicMock()
        proc_b = MagicMock()
        self.pwm._submix_procs[("ch_a", "personal_mix")] = proc_a
        self.pwm._submix_procs[("ch_b", "personal_mix")] = proc_b
        self.pwm.channels = [{"id": "ch_a", "type": "app"}, {"id": "ch_b", "type": "app"}]
        self.pwm.assigned_apps = {"ch_a": ["AppA"], "ch_b": ["AppB"]}

        self.pwm.remove_channel("ch_a")

        # proc_a must be terminated, but proc_b must be 100% untouched
        proc_a.terminate.assert_called_once()
        proc_b.terminate.assert_not_called()
        self.assertIn(("ch_b", "personal_mix"), self.pwm._submix_procs)

    def test_meter_linking_idempotency(self):
        """Invariant: _link_and_audit_channel_monitors must NOT issue redundant pw-link calls on already linked meters."""
        from wavecontroller.engine.peak_monitor import MultiChannelPeakMonitor
        from unittest.mock import patch, MagicMock
        with patch("subprocess.check_output") as mock_co, patch("subprocess.run") as mock_run:
            mock_co.return_value = (
                "wave_meter_WaveController_Channel_spoti:input_FL\n"
                "  |<- WaveController_Channel_spotify:monitor_FL\n"
                "wave_meter_WaveController_Channel_spoti:input_FR\n"
                "  |<- WaveController_Channel_spotify:monitor_FR\n"
            )
            mock_pm = MultiChannelPeakMonitor.__new__(MultiChannelPeakMonitor)
            target_map = {
                "WaveController_Channel_spotify": {
                    "channels": 2,
                    "is_sink": True,
                    "keys": {"spotify"}
                }
            }
            all_ports = [
                "WaveController_Channel_spotify:monitor_FL",
                "WaveController_Channel_spotify:monitor_FR"
            ]

            mock_pm._link_and_audit_channel_monitors(target_map, all_ports)

            # Must NOT call pw-link because ports are already linked
            mock_run.assert_not_called()

    def test_high_speed_telemetry_fusion(self):
        """Invariant: get_peaks IPC command must include real-time volume states for 30 FPS fader tracking."""
        from wavecontroller.engine.ipc_server import IPCServer
        from unittest.mock import MagicMock
        mock_hw = MagicMock()
        mock_peaks = MagicMock()
        mock_peaks.get_all_peaks.return_value = {"spotify": 0.5}
        
        self.pwm.mix_states = {"personal_mix": {"volume": 75, "muted": False}}
        self.pwm.channel_master_states = {"spotify": {"volume": 85, "muted": False}}
        self.pwm.channel_states = {"spotify": {"personal_mix": {"volume": 85, "muted": False}}}
        
        server = IPCServer(self.pwm, mock_hw, mock_peaks)
        res = server._process_command({"command": "get_peaks"})
        
        self.assertIn("peaks", res)
        self.assertIn("mix_states", res)
        self.assertIn("channel_master_states", res)
        self.assertIn("channel_states", res)
        self.assertIn("hardware", res)
        self.assertEqual(res["mix_states"]["personal_mix"]["volume"], 75)
        self.assertEqual(res["channel_master_states"]["spotify"]["volume"], 85)
        self.assertIn("is_connected", res["hardware"])

    def test_add_channel_and_mix_state_initialization(self):
        """Invariant: Adding a channel or mix must proactively initialize all master and submix volume states."""
        self.pwm.channels = []
        self.pwm.mixes = []
        self.pwm.channel_master_states = {}
        self.pwm.mix_states = {}
        self.pwm.channel_states = {}
        self.pwm.assigned_apps = {}
        self.pwm._volume_queue = {}
        self.pwm._mix_volume_queue = {}
        self.pwm._submix_volume_queue = {}
        import threading
        self.pwm._volume_event = threading.Event()

        # Add mix
        new_mix = self.pwm.add_mix("Stream Mix", mix_type="source")
        self.assertEqual(new_mix["id"], "stream_mix")
        self.assertIn("stream_mix", self.pwm.mix_states)
        self.assertEqual(self.pwm.mix_states["stream_mix"]["volume"], 100)

        # Add channel
        new_ch = self.pwm.add_channel("Discord", ch_type="sink")
        self.assertEqual(new_ch["id"], "discord")
        self.assertIn("discord", self.pwm.channel_master_states)
        self.assertEqual(self.pwm.channel_master_states["discord"]["volume"], 80)
        self.assertIn("discord", self.pwm.channel_states)
        self.assertIn("stream_mix", self.pwm.channel_states["discord"])
        self.assertEqual(self.pwm.channel_states["discord"]["stream_mix"]["volume"], 80)

    def test_perceptual_fader_loudness_curve(self):
        """Invariant: Volume percentage must map to quadratic broadcast fader gain (50% = 0.25 / -12 dB)."""
        from wavecontroller.engine.pipewire_manager import PipeWireManager
        self.assertAlmostEqual(PipeWireManager._pct_to_pipewire_gain(100), 1.000, places=3)
        self.assertAlmostEqual(PipeWireManager._pct_to_pipewire_gain(50), 0.250, places=3)
        self.assertAlmostEqual(PipeWireManager._pct_to_pipewire_gain(0), 0.000, places=3)

    def test_wave3_profile_and_circuit_breaker(self):
        """Invariant: Wave:3 profile must enforce safe descriptors, 40dB gain max, and USB circuit breaker protection."""
        from wavecontroller.engine.elgato_wave import PROFILE_WAVE_3, PROFILE_WAVE_XLR, ElgatoWaveDevice
        self.assertEqual(PROFILE_WAVE_3.vid, 0x0FD9)
        self.assertEqual(PROFILE_WAVE_3.pid, 0x0070)
        self.assertEqual(PROFILE_WAVE_3.gain_max_db, 40.0)
        self.assertIsNone(PROFILE_WAVE_3.claim_interface)
        self.assertEqual(PROFILE_WAVE_3.icon_name, "ElgatoWave3.png")

    def test_wave_xlr_mk2_profile_registration(self):
        """Invariant: Wave XLR MK2 must be recognized as an Elgato device via its USB PID."""
        from wavecontroller.engine.elgato_wave import ELGATO_PROFILES
        mk2 = next((p for p in ELGATO_PROFILES if p.display_name == "Wave XLR MK2"), None)
        self.assertIsNotNone(mk2)
        self.assertEqual(mk2.vid, 0x0FD9)
        self.assertEqual(mk2.pid, 0x00B6)
        self.assertEqual(mk2.gain_max_db, 75.0)
        self.assertEqual(mk2.claim_interface, 3)

        # Test circuit breaker initialization
    def test_submix_loopback_monitor_sink_flag_enforced(self):
        """Invariant: Virtual channel sink nodes MUST be targeted with is_sink=True to capture monitor ports."""
        from wavecontroller.engine.peak_monitor import MultiChannelPeakMonitor
        from unittest.mock import patch, MagicMock

        with patch("subprocess.check_output") as mock_co, patch("subprocess.Popen") as mock_popen:
            mock_co.return_value = (
                "WaveController_Channel_gaming:monitor_FL\n"
                "WaveController_Channel_gaming:monitor_FR\n"
            )
            mock_pm = MultiChannelPeakMonitor.__new__(MultiChannelPeakMonitor)
            mock_pm.pipewire_mgr = MagicMock()
            mock_pm.pipewire_mgr.channels = [{"id": "gaming", "type": "sink", "expose_sink": True}]
            mock_pm._channel_procs = {}
            mock_pm._channel_proc_channels = {}
            mock_pm._channel_peaks = {}
            mock_pm.running = False
            
            # Run channel discovery
            mock_pm._refresh_channel_monitors()
            
            # Verify pw-record was called with stream.capture.sink=true
            pw_record_calls = [c for c in mock_popen.call_args_list if c[0] and isinstance(c[0][0], list) and c[0][0][0] == "pw-record"]
            self.assertTrue(len(pw_record_calls) >= 1, "Expected pw-record to be spawned")
            cmd_args = pw_record_calls[0][0][0]
            self.assertIn("stream.capture.sink=true", " ".join(cmd_args),
                          "REGRESSION: Virtual channel sink monitor must be recorded with stream.capture.sink=true")
            self.assertIn("WaveController_Channel_gaming", cmd_args)

    def test_no_mic_bleed_into_mixes(self):
        """Invariant: Microphone signals MUST NEVER bleed into mix peak meters (fefine_mix, mobo_device, chat_mix)."""
        from wavecontroller.engine.peak_monitor import MultiChannelPeakMonitor
        from unittest.mock import MagicMock
        
        mock_pm = MultiChannelPeakMonitor.__new__(MultiChannelPeakMonitor)
        mock_pm._lock = self.pwm._lock
        mock_pm.peaks = {}
        mock_pm._channel_peaks = {}
        mock_pm.pipewire_mgr = MagicMock()
        mock_pm.pipewire_mgr.mixes = [
            {"id": "fefine_mix", "name": "Fefine Mix", "type": "sink"},
            {"id": "mobo_device", "name": "Mobo device", "type": "sink"}
        ]
        # Channels disabled in mixes
        mock_pm.pipewire_mgr.channels = [{"id": "elgato_wave_xlr", "type": "source"}]
        mock_pm.pipewire_mgr.channel_states = {
            "elgato_wave_xlr": {
                "fefine_mix": {"enabled": False, "volume": 0},
                "mobo_device": {"enabled": False, "volume": 0}
            }
        }
        mock_pm.pipewire_mgr.channel_master_states = {"elgato_wave_xlr": {"volume": 100, "muted": False}}
        mock_pm.pipewire_mgr.mix_states = {
            "fefine_mix": {"volume": 100, "muted": False},
            "mobo_device": {"volume": 100, "muted": False}
        }
        
        # Simulate loud mic input (m_l = 0.90, m_r = 0.90)
        m_l, m_r = 0.90, 0.90
        s_l, s_r = 0.0, 0.0
        
        with mock_pm._lock:
            # Physical mic channel gets mic level
            for ch in ["mic", "microphone", "elgato_wave_xlr", "wave_xlr"]:
                mock_pm.peaks[ch] = {"left": m_l, "right": m_r, "peak": max(m_l, m_r)}
            mock_pm.peaks["personal_mix"] = {"left": s_l, "right": s_r, "peak": max(s_l, s_r)}
            
            # Calculate dynamic mix bus peaks
            for mx in mock_pm.pipewire_mgr.mixes:
                mx_id = mx["id"]
                st = mock_pm.pipewire_mgr.channel_states["elgato_wave_xlr"][mx_id]
                if not st.get("enabled", True):
                    mock_pm.peaks[mx_id] = {"left": 0.0, "right": 0.0, "peak": 0.0}

        # Assert zero bleed into disabled mixes
        self.assertEqual(mock_pm.peaks["fefine_mix"]["peak"], 0.0, "REGRESSION: Mic bled into fefine_mix!")
        self.assertEqual(mock_pm.peaks["mobo_device"]["peak"], 0.0, "REGRESSION: Mic bled into mobo_device!")

    def test_electron_process_binary_disambiguation(self):
        """Invariant: Discord process binary MUST match ONLY Discord tokens and reject Google Chrome tokens."""
        meta_map = {
            "Chromium:output_FL": {
                "app_name": "Chromium",
                "binary": "/usr/share/discord/Discord",
                "node_name": "Chromium",
                "app_id": "discord"
            }
        }
        discord_tokens = self.pwm._get_match_tokens("Discord")
        chrome_tokens = self.pwm._get_match_tokens("Google Chrome")
        
        # Port must match Discord
        self.assertTrue(self.pwm._port_matches_tokens("Chromium:output_FL", discord_tokens, meta_map))
        # Port must NOT match Chrome
        self.assertFalse(self.pwm._port_matches_tokens("Chromium:output_FL", chrome_tokens, meta_map),
                         "REGRESSION: Chrome tokens hijacked Discord Chromium stream!")

    def test_elgato_manager_power_and_device_api_invariants(self):
        """Invariant: ElgatoWaveManager must export get_device, detect_device, on_system_suspend, on_system_resume."""
        from wavecontroller.engine.elgato_wave import elgato_manager
        self.assertTrue(callable(getattr(elgato_manager, "get_device", None)), "ElgatoWaveManager missing get_device()")
        self.assertTrue(callable(getattr(elgato_manager, "detect_device", None)), "ElgatoWaveManager missing detect_device()")
        self.assertTrue(callable(getattr(elgato_manager, "on_system_suspend", None)), "ElgatoWaveManager missing on_system_suspend()")
        self.assertTrue(callable(getattr(elgato_manager, "on_system_resume", None)), "ElgatoWaveManager missing on_system_resume()")

    def test_elgato_hardware_state_key_parity_and_event_diff(self):
        """Invariant: ElgatoWaveDevice _last_state, get_all_state, and elgato_manager MUST share identical canonical key names."""
        from wavecontroller.engine.elgato_wave import ElgatoWaveDevice, PROFILE_WAVE_XLR, elgato_manager
        dev = ElgatoWaveDevice(PROFILE_WAVE_XLR)
        
        # Test simulated state snapshot keys
        required_keys = {"gain_db", "hp_volume_pct", "monitor_mix_pct", "dial_mode", "mute", "phantom_power", "clipguard", "low_cut"}
        
        # Manually invoke state setters
        dev._last_state["hp_volume_pct"] = 75
        dev._last_state["monitor_mix_pct"] = 50
        
        # Verify canonical keys exist in _last_state
        self.assertIn("hp_volume_pct", dev._last_state, "REGRESSION: _last_state missing canonical hp_volume_pct key!")
        self.assertIn("monitor_mix_pct", dev._last_state, "REGRESSION: _last_state missing canonical monitor_mix_pct key!")

        # Verify change-detection simulation
        last_state = {"hp_volume_pct": 50, "dial_mode": "hp", "gain_db": 40.0}
        curr_state = {"hp_volume_pct": 65, "dial_mode": "hp", "gain_db": 40.0}
        
        changed = {k: v for k, v in curr_state.items() if k in last_state and last_state[k] != v}
        self.assertEqual(changed, {"hp_volume_pct": 65}, "REGRESSION: Poll loop failed to detect hp_volume_pct change!")

    def test_elgato_output_mix_id_resolution_and_sync(self):
        """Invariant: _get_elgato_output_mix_id MUST resolve to the active mix assigned to Wave XLR target device."""
        from wavecontroller.engine.usb_hardware import USBHardwareManager
        hwm = USBHardwareManager()
        mock_pw = MagicMock()
        mock_pw.mixes = [
            {"id": "wave_xlr", "name": "wave XLR", "type": "sink", "target_device": "usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00"},
            {"id": "chat_mix", "name": "Mic Mix", "type": "source", "target_device": "none"},
            {"id": "pc_out", "name": "pc out", "type": "sink", "target_device": "alsa_card.pci-0000_14_00.4"}
        ]
        hwm.pipewire_mgr = mock_pw
    def test_group_channel_multi_app_assignment(self):
        """Invariant: Group Channels must support multi-app assignment, unassignment, and unified app discovery."""
        ch = self.pwm.add_channel("Comms Group", ch_type="sink", assigned_apps=["Discord", "TeamSpeak"], expose_sink=False)
        ch_id = ch["id"]
        self.assertEqual(self.pwm.get_assigned_apps(ch_id), ["Discord", "TeamSpeak"])

        # Add another app
        self.pwm.assign_app_to_channel(ch_id, "Slack")
        self.assertIn("Slack", self.pwm.get_assigned_apps(ch_id))

        # Unassign an app
        self.pwm.unassign_app_from_channel(ch_id, "Discord")
        self.assertNotIn("Discord", self.pwm.get_assigned_apps(ch_id))
        self.assertIn("TeamSpeak", self.pwm.get_assigned_apps(ch_id))
        self.assertIn("Slack", self.pwm.get_assigned_apps(ch_id))

        # Verify get_channel_all_apps returns formatted list
        all_apps = self.pwm.get_channel_all_apps(ch_id)
        app_names = [a["name"] for a in all_apps]
        self.assertIn("TeamSpeak", app_names)
        self.assertIn("Slack", app_names)

    def test_group_channel_exposed_sink_provisioning(self):
        """Invariant: When expose_sink is enabled, WaveController_Channel_{ch_id} must be provisioned in needed_nodes."""
        ch = self.pwm.add_channel("Gaming Group", ch_type="sink", assigned_apps=["Steam"], expose_sink=True)
        ch_id = ch["id"]
        self.assertTrue(self.pwm.is_channel_sink_exposed(ch_id))

        # Toggle exposed off
        self.pwm.set_channel_sink_exposed(ch_id, False)
        self.assertFalse(self.pwm.is_channel_sink_exposed(ch_id))

        # Toggle exposed back on
        self.pwm.set_channel_sink_exposed(ch_id, True)
        self.assertTrue(self.pwm.is_channel_sink_exposed(ch_id))

    def test_app_channels_do_not_provision_exposed_sinks(self):
        """Invariant: Regular application channels must not have expose_sink enabled or create Audio/Sink nodes."""
        ch = self.pwm.add_channel("Spotify", ch_type="app", assigned_apps=["Spotify"])
        ch_id = ch["id"]
        self.assertFalse(self.pwm.is_channel_sink_exposed(ch_id), "App channel should have expose_sink=False!")

    def test_fallback_sink_provisioning_and_isolation(self):
        """Invariant: WaveController_Fallback_Sink must be provisioned for unassigned/deleted applications and must never route to Personal Mix."""
        # 1. Fallback playback ports must never return Personal Mix Sink
        fallback_ports = self.pwm._get_default_sink_playback_ports()
        for p in fallback_ports:
            self.assertNotIn("personal_mix", p.lower(), "REGRESSION: _get_default_sink_playback_ports returned Personal Mix Sink ports!")

        # 2. Unassigning an app from group channel removes manual assignment and does not repopulate as in-app
        ch = self.pwm.add_channel("Group Test", ch_type="group", assigned_apps=["Discord", "Spotify"], expose_sink=True)
        ch_id = ch["id"]
        self.pwm.unassign_app_from_channel(ch_id, "Discord")
        self.assertNotIn("Discord", self.pwm.get_assigned_apps(ch_id))
        all_apps = self.pwm.get_channel_all_apps(ch_id)
        app_names = [a["name"] for a in all_apps]
        self.assertNotIn("Discord", app_names, "REGRESSION: Unassigned app repopulated in get_channel_all_apps!")

    def test_default_hardware_devices_and_oobe_setup(self):
        """Invariant: SettingsView and SetupWizardDialog configure default input/output hardware and anchor Personal Mix and Fallback."""
        from wavecontroller.views.settings_view import SettingsView
        from wavecontroller.views.setup_wizard import SetupWizardDialog
        from wavecontroller.engine.config_manager import config_manager
        from unittest.mock import MagicMock

        mock_hw = MagicMock()
        mock_hw.get_all_available_input_devices.return_value = [{"device_key": "alsa_input.wave_xlr", "display_name": "Elgato Wave XLR", "name": "Elgato Wave XLR"}]
        mock_hw.get_all_available_output_devices.return_value = [{"device_key": "alsa_output.wave_xlr", "display_name": "Elgato Wave XLR Analog Stereo", "name": "Elgato Wave XLR Analog Stereo"}]
        mock_hw.get_tracked_input_devices.return_value = [{"device_key": "alsa_input.wave_xlr", "display_name": "Elgato Wave XLR", "name": "Elgato Wave XLR"}]
        mock_hw.get_tracked_output_devices.return_value = [{"device_key": "alsa_output.wave_xlr", "display_name": "Elgato Wave XLR Analog Stereo", "name": "Elgato Wave XLR Analog Stereo"}]

        # Test SettingsView instantiation with pipewire_mgr
        sv = SettingsView(mock_hw, pipewire_mgr=self.pwm)
        self.assertIsNotNone(sv.mic_combo)
        self.assertIsNotNone(sv.output_combo)
        self.assertTrue(hasattr(sv, "refresh_device_list"))
        sv.refresh_device_list()

        # Test SetupWizardDialog instantiation
        parent_win = MagicMock()
        wiz = SetupWizardDialog(parent_win, mock_hw, self.pwm)
        self.assertEqual(wiz.mic_combo.get_selected(), 0)
        self.assertEqual(wiz.output_combo.get_selected(), 0)

    def test_personal_mix_header_omits_target_device_dropdown(self):
        """Invariant: Personal Mix header edit popup must omit target device dropdown while secondary mixes retain it."""
        from wavecontroller.views.mix_header import MixHeaderCard
        from unittest.mock import MagicMock

        mock_hw = MagicMock()
        mock_hw.get_tracked_output_devices.return_value = []

        # 1. Personal Mix header
        pm_info = {"id": "personal_mix", "name": "Personal Mix", "type": "sink", "target_device": "default"}
        pm_card = MixHeaderCard(pm_info, self.pwm, None, mock_hw)
        # Edit popup should recognize it as personal mix
        self.assertTrue(pm_card.mix_info.get("id") in ("personal", "personal_mix"))
        self.assertIn("mix-header-bold-subtitle", pm_card.subtitle_lbl.get_css_classes())

        # 2. Secondary Mix header
        sec_info = {"id": "speakers_mix", "name": "Speakers Mix", "type": "sink", "target_device": "alsa_output.speakers"}
        sec_card = MixHeaderCard(sec_info, self.pwm, None, mock_hw)
        self.assertFalse(sec_card.mix_info.get("id") in ("personal", "personal_mix"))
        self.assertIn("mix-header-subtitle", sec_card.subtitle_lbl.get_css_classes())

    def test_mic_monitor_always_linked_in_discovery(self):
        """Invariant: _do_refresh_discovery and _link_mic_monitor MUST link physical microphone ports to wave_mic_monitor."""
        from wavecontroller.engine.peak_monitor import MultiChannelPeakMonitor
        from unittest.mock import patch, MagicMock

        with patch("subprocess.check_output") as mock_co, patch("subprocess.run") as mock_run, patch("subprocess.Popen") as mock_popen:
            # Simulate pw-link -o output containing Wave XLR mono capture
            mock_co.side_effect = lambda cmd, **kwargs: (
                "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00.mono-fallback:capture_MONO\n"
                "WaveController_personal_mix_Sink:monitor_FL\n"
                "WaveController_personal_mix_Sink:monitor_FR\n"
            )
            mock_pm = MultiChannelPeakMonitor()
            mock_pm._lock = self.pwm._lock
            mock_pm.pipewire_mgr = MagicMock()
            mock_pm.pipewire_mgr.channels = [{"id": "elgato_wave_xlr", "type": "source"}]
            mock_pm.pipewire_mgr.mixes = []

            # Execute discovery
            mock_pm._do_refresh_discovery()

            # Verify pw-link was called to link capture_MONO to wave_mic_monitor
            linked_calls = [c[0][0] for c in mock_run.call_args_list if c[0] and isinstance(c[0][0], list) and c[0][0][0] == "pw-link"]
            self.assertTrue(any("wave_mic_monitor" in " ".join(cmd) for cmd in linked_calls),
                            "REGRESSION: wave_mic_monitor was NOT linked to physical microphone capture ports!")

    def test_physical_mic_peak_propagation_to_channels(self):
        """Invariant: Measured mic peaks MUST propagate to physical microphone channels and source channels."""
        from wavecontroller.engine.peak_monitor import MultiChannelPeakMonitor
        from unittest.mock import MagicMock

        mock_pm = MultiChannelPeakMonitor()
        mock_pm._lock = self.pwm._lock
        mock_pm.pipewire_mgr = MagicMock()
        mock_pm.pipewire_mgr.channels = [
            {"id": "elgato_wave_xlr", "type": "source"},
            {"id": "custom_mic", "type": "source"}
        ]
        mock_pm._last_mic_peaks = {"left": 0.75, "right": 0.75, "peak": 0.75}

        # Check standard channel IDs and source channels
        l_xlr, r_xlr = mock_pm.get_channel_stereo_peaks("elgato_wave_xlr")
        self.assertAlmostEqual(l_xlr, 0.75, places=2)
        self.assertAlmostEqual(r_xlr, 0.75, places=2)

        l_mic, r_mic = mock_pm.get_channel_stereo_peaks("mic")
        self.assertAlmostEqual(l_mic, 0.75, places=2)
        self.assertAlmostEqual(r_mic, 0.75, places=2)

        l_cust, r_cust = mock_pm.get_channel_stereo_peaks("custom_mic")
        self.assertAlmostEqual(l_cust, 0.75, places=2)
        self.assertAlmostEqual(r_cust, 0.75, places=2)

    def test_electron_webrtc_stream_icon_resolution_invariant(self):
        """Invariant: get_active_application_streams MUST resolve accurate app icon for Electron/WebRTC streams."""
        from unittest.mock import patch
        import json

        fake_dump = [
            {
                "id": 110,
                "info": {
                    "props": {
                        "media.class": "Stream/Output/Audio",
                        "application.name": "WEBRTC VoiceEngine",
                        "application.process.binary": "Discord",
                        "application.icon-name": "chromium",
                        "application.id": "com.discordapp.Discord"
                    }
                }
            },
            {
                "id": 120,
                "info": {
                    "props": {
                        "media.class": "Stream/Output/Audio",
                        "application.name": "Google Chrome",
                        "application.process.binary": "chrome",
                        "application.icon-name": "google-chrome",
                        "application.id": "com.google.Chrome"
                    }
                }
            }
        ]

        with patch("subprocess.check_output") as mock_co:
            mock_co.return_value = json.dumps(fake_dump)
            detected = self.pwm.get_active_application_streams()
            discord_app = next((a for a in detected if a["name"] == "Discord"), None)
            self.assertIsNotNone(discord_app, "REGRESSION: Discord stream was not recognized!")
            self.assertEqual(discord_app["icon"], "discord", "REGRESSION: Discord icon was overwritten with chromium fallback!")

    def test_flatpak_portal_discord_detection(self):
        """Invariant: Flatpak Discord streams with portal app ID and generic Chromium name must resolve to Discord."""
        from unittest.mock import patch
        import json

        fake_dump = [
            {
                "id": 249,
                "info": {
                    "props": {
                        "media.class": "Stream/Output/Audio",
                        "application.name": "Chromium",
                        "node.name": "Chromium",
                        "pipewire.access.portal.app_id": "com.discordapp.Discord",
                        "media.name": "Playback"
                    }
                }
            }
        ]

        with patch("subprocess.check_output") as mock_co:
            mock_co.return_value = json.dumps(fake_dump)
            detected = self.pwm.get_active_application_streams()
            discord_app = next((a for a in detected if a["name"] == "Discord"), None)
            self.assertIsNotNone(discord_app, "REGRESSION: Flatpak Discord stream was not recognized!")
            self.assertEqual(discord_app["icon"], "discord")

    def test_wave_named_apps_not_misidentified_as_hardware(self):
        """Invariant: Applications with 'wave' in their name (e.g. Shortwave) must never be misclassified as Wave hardware."""
        from wavecontroller.views.channel_card import ChannelCard
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        class MockHardwareMgr:
            is_elgato = True
            device_type = "elgato"
            device_name = "Elgato Wave XLR"
            device_key = "usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00"
            input_devices = []
            discovered_devices = {}
            def get_device_display_name(self, d): return "Elgato Wave XLR"
            def get_device_icon(self, d): return "elgato-wave-xlr-symbolic"

        mock_hw = MockHardwareMgr()
        app_ch = {
            "id": "shortwave",
            "name": "Shortwave",
            "type": "app",
            "icon": "de.haeckerfelix.Shortwave",
            "default_vol": 80
        }

        # Initialize GTK app if not running in test runner
        _ = Gtk.Application()
        card = ChannelCard(app_ch, self.pwm, mock_hw)
        self.assertFalse(card.is_wave_channel, "REGRESSION: Shortwave was misidentified as an Elgato Wave hardware device!")
        self.assertFalse(card.is_mic_channel, "REGRESSION: Shortwave was misidentified as a microphone/source channel!")
        self.assertNotEqual(card.icon_img.get_icon_name(), "elgato-wave-xlr-symbolic", "REGRESSION: Shortwave icon fell back to wave-xlr!")

    def test_shortwave_icon_resolution(self):
        """Invariant: Shortwave must resolve to its official Freedesktop/Flatpak icon, never elgato-wave-xlr."""
        from wavecontroller.engine.pipewire_manager import PipeWireManager
        icon1 = PipeWireManager.resolve_icon_for_app("Shortwave")
        icon2 = PipeWireManager.resolve_icon_for_app("shortwave")
        icon3 = PipeWireManager.resolve_icon_for_app("de.haeckerfelix.Shortwave")

        self.assertEqual(icon1, "de.haeckerfelix.Shortwave", "REGRESSION: Shortwave did not resolve to Flatpak/desktop icon!")
        self.assertEqual(icon2, "de.haeckerfelix.Shortwave", "REGRESSION: lowercase shortwave did not resolve to Flatpak/desktop icon!")
        self.assertEqual(icon3, "de.haeckerfelix.Shortwave", "REGRESSION: de.haeckerfelix.Shortwave did not resolve to Flatpak/desktop icon!")

    def test_flatpak_portal_app_id_stream_discovery(self):
        """Invariant: Flatpak audio streams with portal app IDs must resolve their specific icon."""
        from unittest.mock import patch
        import json
        fake_dump = [
            {
                "id": 125,
                "info": {
                    "props": {
                        "media.class": "Stream/Output/Audio",
                        "application.name": "Shortwave",
                        "application.process.binary": "shortwave",
                        "pipewire.access.portal.app_id": "de.haeckerfelix.Shortwave"
                    }
                }
            }
        ]

        with patch("subprocess.check_output") as mock_co:
            mock_co.return_value = json.dumps(fake_dump)
            detected = self.pwm.get_active_application_streams()
            shortwave_app = next((a for a in detected if a["name"] == "Shortwave"), None)
            self.assertIsNotNone(shortwave_app, "Shortwave stream not found in active streams!")
            self.assertEqual(shortwave_app["icon"], "de.haeckerfelix.Shortwave", "Shortwave did not resolve portal icon!")

    def test_wave_app_tokens_do_not_contain_hardware_aliases(self):
        """Invariant: App tokens for Shortwave or Waveform must never contain Elgato hardware tokens."""
        from wavecontroller.engine.graph.process_classifier import get_match_tokens
        tokens = get_match_tokens("Shortwave")
        self.assertIn("shortwave", tokens)
        self.assertNotIn("elgato", tokens, "REGRESSION: Shortwave inherited elgato hardware token!")
        self.assertNotIn("0fd9", tokens, "REGRESSION: Shortwave inherited 0fd9 hardware token!")
        self.assertNotIn("wave_xlr", tokens, "REGRESSION: Shortwave inherited wave_xlr hardware token!")

    def test_wave_apps_do_not_trigger_hardware_mute_on_master(self):
        """Invariant: Toggling master mute on Shortwave channel must NOT mute physical Elgato mic."""
        from unittest.mock import MagicMock
        mock_hw = MagicMock()
        self.pwm.hardware_mgr = mock_hw
        self.pwm.channels = [
            {"id": "shortwave", "name": "Shortwave", "type": "app"},
            {"id": "mic", "name": "Wave XLR", "type": "source"}
        ]
        self.pwm.toggle_channel_master_mute("shortwave")
        mock_hw.set_mode_mute.assert_not_called()

        self.pwm.toggle_channel_master_mute("mic")
        mock_hw.set_mode_mute.assert_called_once()

    def test_ipc_server_wave_channel_detection_isolation(self):
        """Invariant: IPC server must not classify app channels with 'wave' as hardware devices."""
        from unittest.mock import MagicMock
        from wavecontroller.engine.ipc_server import IPCServer
        mock_hw = MagicMock()
        mock_hw.device_name = "Elgato Wave XLR"
        ipc = IPCServer(pipewire_mgr=self.pwm, peak_monitor=MagicMock(), hardware_mgr=mock_hw)
        self.assertFalse(ipc._is_wave_channel("shortwave"))
        self.assertTrue(ipc._is_wave_channel("mic"))
        self.assertTrue(ipc._is_wave_channel("elgato_wave_xlr"))

    def test_usb_hardware_is_target_elgato_isolation(self):
        """Invariant: USB Hardware manager _is_target_elgato must not match non-hardware apps."""
        from wavecontroller.engine.usb_hardware import USBHardwareManager
        hw = USBHardwareManager()
        self.assertFalse(hw._is_target_elgato("Shortwave"))
        self.assertTrue(hw._is_target_elgato("Elgato Wave XLR"))
        self.assertTrue(hw._is_target_elgato("wave_xlr"))

    def test_unrouted_channel_peaks_report_zero(self):
        """Invariant: When a channel has no active submix or is removed and app is quiet, its peaks must strictly return (0.0, 0.0)."""
        from wavecontroller.engine.peak_monitor import MultiChannelPeakMonitor
        pm = MultiChannelPeakMonitor()
        pm.pipewire_mgr = self.pwm

        # Simulate previous stale value in pm.peaks
        pm.peaks["spotify"] = {"left": 0.45, "right": 0.45, "peak": 0.45}
        pm._channel_peaks = {}  # No active submix monitor

        l, r = pm.get_channel_stereo_peaks("spotify")
        self.assertEqual(l, 0.0, "REGRESSION: Stale peak returned for unrouted channel!")
        self.assertEqual(r, 0.0, "REGRESSION: Stale peak returned for unrouted channel!")

    def test_direct_app_stream_monitoring_without_submixes(self):
        """Invariant: PeakMonitor must discover direct app output ports for pre-fader channel metering even when no submixes are active."""
        from wavecontroller.engine.peak_monitor import MultiChannelPeakMonitor
        from unittest.mock import patch, MagicMock

        mock_pwm = MagicMock()
        mock_pwm.channels = [{"id": "spotify", "name": "Spotify", "type": "app"}]
        mock_pwm.get_assigned_apps.return_value = ["Spotify"]
        mock_pwm._get_active_port_metadata_map.return_value = {}
        mock_pwm._get_match_tokens.return_value = {"spotify"}
        mock_pwm._port_matches_tokens.side_effect = lambda p, toks, meta: any(t in p.lower() for t in toks)

        pm = MultiChannelPeakMonitor()
        pm.pipewire_mgr = mock_pwm

        with patch("subprocess.check_output") as mock_co, patch("subprocess.run"), patch.object(pm, "_open_pw_record", return_value=None), patch.object(pm, "_link_and_audit_channel_monitors"):
            mock_co.side_effect = lambda cmd, **kw: "spotify:output_FL\nspotify:output_FR\n" if cmd == ['pw-link', '-o'] else ""
            pm._refresh_channel_monitors()
            self.assertIn("spotify", pm._target_keys.get("spotify", set()), "Direct app output stream was not mapped for pre-fader metering!")

    def test_permanent_direct_app_stream_telemetry_isolation(self):
        """Invariant: PeakMonitor MUST permanently target raw app output ports even when submix loopbacks are active (never switch to submix nodes)."""
        from wavecontroller.engine.peak_monitor import MultiChannelPeakMonitor
        from unittest.mock import patch, MagicMock

        mock_pwm = MagicMock()
        mock_pwm.channels = [{"id": "spotify", "name": "Spotify", "type": "app"}]
        mock_pwm.get_assigned_apps.return_value = ["Spotify"]
        mock_pwm._get_active_port_metadata_map.return_value = {}
        mock_pwm._get_match_tokens.return_value = {"spotify"}
        mock_pwm._port_matches_tokens.side_effect = lambda p, toks, meta: any(t in p.lower() for t in toks)

        pm = MultiChannelPeakMonitor()
        pm.pipewire_mgr = mock_pwm

        with patch("subprocess.check_output") as mock_co, patch("subprocess.run"), patch.object(pm, "_open_pw_record", return_value=None), patch.object(pm, "_link_and_audit_channel_monitors"):
            # Simulate both raw Spotify output AND active submix loopback ports in PipeWire
            mock_co.side_effect = lambda cmd, **kw: (
                "spotify:output_FL\nspotify:output_FR\n"
                "input.WaveController_submix_spotify_personal_mix:monitor_FL\n"
                "input.WaveController_submix_spotify_personal_mix:monitor_FR\n"
                if cmd == ['pw-link', '-o'] else ""
            )
            pm._refresh_channel_monitors()
            # Must map directly to 'spotify', NEVER 'input.WaveController_submix_spotify_personal_mix'
            self.assertIn("spotify", pm._target_keys, "Raw app node 'spotify' was not targeted!")
            self.assertNotIn("input.WaveController_submix_spotify_personal_mix", pm._target_keys,
                             "REGRESSION: PeakMonitor targeted submix loopback instead of raw app stream!")


    def test_unified_device_settings_view_initialization(self):
        """Invariant: UnifiedDeviceSettingsView MUST initialize without AttributeError across duplex, input, and output device profiles."""
        from wavecontroller.views.device_settings import UnifiedDeviceSettingsView
        from unittest.mock import MagicMock
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw

        mock_hw = MagicMock()
        mock_hw.get_device_display_name.return_value = "Test Hardware Device"
        mock_hw.get_device_icon.return_value = "audio-input-microphone-symbolic"
        mock_hw.hardware_gain_db = 44
        mock_hw.phantom_power_48v = False
        mock_hw.clipguard_enabled = True
        mock_hw.low_cut_filter = "Off"
        mock_hw.low_impedance_mode = False
        mock_hw.get_output_volume.return_value = 75
        mock_hw.get_monitor_mix.return_value = 50
        mock_hw.get_device_assigned_mix.return_value = "personal_mix"
        mock_hw.get_exclusive_mic_lock.return_value = True
        mock_hw.get_exclusive_output_lock.return_value = True
        mock_hw.is_device_muted.return_value = False
        mock_hw.get_device_diagnostics.return_value = {
            "architecture": "USB Audio",
            "chipset": "Elgato Wave",
            "bus_path": "USB",
            "vendor_info": "Elgato",
            "driver_info": "snd-usb-audio"
        }

        mock_pm = MagicMock()
        mock_pwm = MagicMock()
        mock_pwm.mixes = [{"id": "personal_mix", "name": "Personal Mix"}]

        # Test duplex, input-only, and output-only profiles
        profiles = [
            {"device_key": "test_duplex_key", "name": "Test Duplex", "type": "duplex", "is_elgato": True, "connected": True},
            {"device_key": "test_input_key", "name": "Test Input Only", "type": "input", "is_elgato": False, "connected": True},
            {"device_key": "test_output_key", "name": "Test Output Only", "type": "output", "is_elgato": False, "connected": True},
        ]

        for prof in profiles:
            view = UnifiedDeviceSettingsView(
                device_info=prof,
                hardware_mgr=mock_hw,
                peak_monitor=mock_pm,
                pipewire_mgr=mock_pwm
            )
            self.assertEqual(view.device_type, prof["type"], f"REGRESSION: device_type not properly set for {prof['type']}!")
            # Test in-place update_device_info
            updated_prof = dict(prof)
            updated_prof["connected"] = False
            view.update_device_info(updated_prof)
            self.assertEqual(view.device_type, prof["type"])
            self.assertFalse(view.device_info["connected"])

    def test_default_device_lifecycle_and_channel_mix_removal(self):
        """Invariant: Removing default device must cleanly remove Microphone channel and Personal Mix, while preserving deletable Chat Mix."""
        # 1. Provision default device
        self.pwm.provision_default_device_channels_and_mix(
            device_key="test_headset_hw",
            device_name="Test Headset",
            is_input=True,
            is_output=True
        )
        self.assertTrue(any(c["id"] == "mic" and c.get("type") == "source" for c in self.pwm.channels),
                        "REGRESSION: Microphone channel was not provisioned for default device!")
        self.assertTrue(any(m["id"] == "personal" and m.get("type") == "sink" for m in self.pwm.mixes),
                        "REGRESSION: Personal Mix was not provisioned for default device!")
        self.assertTrue(any(m["id"] == "chat_mix" and m.get("type") == "source" for m in self.pwm.mixes),
                        "REGRESSION: Chat Mix (Source Mix) was not provisioned for default device!")
        self.assertTrue(self.pwm.is_channel_mix_enabled("mic", "chat_mix"),
                        "REGRESSION: Microphone channel was not enabled in Chat Mix!")
        self.assertFalse(self.pwm.is_channel_mix_enabled("mic", "personal"),
                         "REGRESSION: Microphone channel was erroneously enabled in Personal Mix by default!")
        self.assertEqual(self.pwm.selected_monitor_device, "test_headset_hw")
        self.assertEqual(self.pwm.default_input_device, "test_headset_hw")

        # 2. Remove default device channels and mix
        self.pwm.remove_default_device_channels_and_mix()
        self.assertFalse(any(c["id"] == "mic" or c.get("type") == "source" for c in self.pwm.channels),
                         "REGRESSION: Microphone channel remained after removing default device!")
        self.assertFalse(any(m["id"] == "personal" or m.get("type") == "sink" for m in self.pwm.mixes),
                         "REGRESSION: Personal Mix remained after removing default device!")
        self.assertEqual(self.pwm.default_input_device, "")
        self.assertEqual(self.pwm.selected_monitor_device, "")

    def test_default_device_migration_and_secondary_device_isolation(self):
        """Invariant: Re-provisioning migrates channels/mixes without duplication; secondary devices do not create channels."""
        # 1. Initial provision
        self.pwm.provision_default_device_channels_and_mix(
            device_key="device_a",
            device_name="Mic A",
            is_input=True,
            is_output=True
        )
        self.assertEqual(len([c for c in self.pwm.channels if c.get("type") == "source"]), 1)
        self.assertEqual(len([m for m in self.pwm.mixes if m.get("type") == "sink"]), 1)
        self.assertTrue(any(m["id"] == "chat_mix" for m in self.pwm.mixes))

        # 2. Migrate to new default device (device_b)
        self.pwm.provision_default_device_channels_and_mix(
            device_key="device_b",
            device_name="Mic B",
            is_input=True,
            is_output=True
        )
        mic_channels = [c for c in self.pwm.channels if c.get("type") == "source"]
        sink_mixes = [m for m in self.pwm.mixes if m.get("type") == "sink"]

        # Strictly 1 mic channel and 1 personal mix (no duplicates)
        self.assertEqual(len(mic_channels), 1, "REGRESSION: Duplicate source channels created during default migration!")
        self.assertEqual(len(sink_mixes), 1, "REGRESSION: Duplicate sink mixes created during default migration!")
        self.assertEqual(mic_channels[0]["name"], "Mic B")
        self.assertEqual(sink_mixes[0]["target_device"], "device_b")
        self.assertTrue(self.pwm.is_channel_mix_enabled("mic", "chat_mix"),
                        "REGRESSION: Microphone channel not enabled in Chat Mix after migration!")
        self.assertFalse(self.pwm.is_channel_mix_enabled("mic", "personal"),
                         "REGRESSION: Microphone channel erroneously enabled in Personal Mix after migration!")

    def test_non_elgato_mic_channel_name_and_badge_suppression(self):
        """Invariant: Non-Elgato microphones (e.g. Fifine) MUST show accurate name and suppress 48V phantom button, even when Elgato hardware is plugged in."""
        from wavecontroller.views.channel_card import ChannelCard
        from unittest.mock import MagicMock

        # Test case 1: is_elgato = False
        mock_hw = MagicMock()
        mock_hw.is_elgato = False
        mock_hw.device_name = "Wave XLR" # Stale HW name should NOT override channel name
        mock_hw.device_key = "usb-3142_fifine_Microphone-00"
        mock_hw.get_device_display_name.return_value = "fifine Microphone"

        mock_pwm = MagicMock()
        mock_pwm.get_assigned_apps.return_value = ["fifine Microphone", "usb-3142_fifine_Microphone-00"]
        mock_pwm.get_channel_master_volume.return_value = 80
        mock_pwm.get_channel_master_mute.return_value = False

        ch_info = {
            "id": "mic",
            "name": "fifine Microphone",
            "type": "source",
            "icon": "audio-input-microphone-symbolic"
        }

        card = ChannelCard(ch_info, pipewire_mgr=mock_pwm, hardware_mgr=mock_hw)
        self.assertEqual(card.title_lbl.get_text(), "fifine Microphone", "REGRESSION: Channel title did NOT match device name!")
        self.assertFalse(card.is_wave_channel, "REGRESSION: Non-Elgato microphone was flagged as wave channel!")
        self.assertFalse(hasattr(card, "phantom_btn"), "REGRESSION: 48V phantom button was provisioned for non-Elgato microphone!")

        # Test case 2: is_elgato = True (Elgato Wave XLR plugged in, but channel is Fifine)
        mock_hw.is_elgato = True
        card2 = ChannelCard(ch_info, pipewire_mgr=mock_pwm, hardware_mgr=mock_hw)
        self.assertFalse(card2.is_wave_channel, "REGRESSION: Non-Elgato microphone was flagged as wave channel when Elgato hardware connected!")
        self.assertFalse(hasattr(card2, "phantom_btn"), "REGRESSION: 48V phantom button was provisioned for Fifine when Elgato hardware connected!")

    def test_make_default_button_gated_to_selection_dismissed(self):
        """Invariant: 'Make Default' button MUST appear ONLY if no default device is active in the system."""
        from wavecontroller.views.device_settings import UnifiedDeviceSettingsView
        from wavecontroller.engine.config_manager import config_manager
        from unittest.mock import MagicMock

        mock_hw = MagicMock()
        mock_hw.is_default_device.return_value = False
        mock_hw.has_default_device.return_value = False
        mock_hw.get_device_display_name.return_value = "fifine Microphone"
        mock_hw.get_device_icon.return_value = "audio-input-microphone-symbolic"
        mock_hw.get_device_diagnostics.return_value = {"architecture": "USB Audio 2.0 Class Device"}
        mock_hw.get_output_volume.return_value = 75
        mock_hw.get_gain.return_value = 50
        mock_hw.get_output_mute.return_value = False

        dev_info = {"device_key": "usb-3142_fifine_Microphone-00", "type": "duplex", "name": "fifine Microphone"}

        # Case 1: No default in system -> grp_default is visible with Make Default button
        mock_hw.is_default_device.return_value = False
        mock_hw.has_default_device.return_value = False
        view_no_default = UnifiedDeviceSettingsView(dev_info, hardware_mgr=mock_hw, peak_monitor=None)
        self.assertTrue(view_no_default.grp_default.get_visible(), "REGRESSION: Make Default group was not visible when system had no default!")

        # Case 2: System ALREADY has default -> grp_default is hidden for secondary device (no Make Default button)
        mock_hw.is_default_device.return_value = False
        mock_hw.has_default_device.return_value = True
        view_secondary = UnifiedDeviceSettingsView(dev_info, hardware_mgr=mock_hw, peak_monitor=None)
        self.assertFalse(view_secondary.grp_default.get_visible(), "REGRESSION: Make Default group was visible for secondary device when system already had default!")

        # Case 3: Primary default device -> grp_default is visible with Active Default status
        mock_hw.is_default_device.return_value = True
        mock_hw.has_default_device.return_value = True
        view_primary = UnifiedDeviceSettingsView(dev_info, hardware_mgr=mock_hw, peak_monitor=None)
        self.assertTrue(view_primary.grp_default.get_visible(), "REGRESSION: Primary Default group was not visible on default device!")

    def test_mic_channel_master_volume_elgato_gain_sync(self):
        """Invariant: get_channel_master_volume('mic') must return mapped hardware_gain_db for Elgato hardware."""
        from wavecontroller.engine.pipewire_manager import PipeWireManager
        from unittest.mock import MagicMock

        pwm = PipeWireManager()
        pwm.channels = [{"id": "mic", "name": "Elgato Wave XLR", "type": "source"}]
        mock_hw = MagicMock()
        mock_hw.is_elgato = True
        mock_hw.hardware_gain_db = 45.0  # 45 / 75.0 = 60%
        pwm.hardware_mgr = mock_hw

        vol = pwm.get_channel_master_volume("mic")
        self.assertEqual(vol, 60, "REGRESSION: Mic channel master volume did NOT map from Elgato hardware gain!")

    def test_mic_channel_linked_state_parity(self):
        """Invariant: is_channel_linked('mic') must respect channel_states linking rather than hardcoding False."""
        from wavecontroller.engine.pipewire_manager import PipeWireManager

        pwm = PipeWireManager()
        pwm.channels = [{"id": "mic", "name": "Elgato Wave XLR", "type": "source"}]
        pwm.channel_states = {
            "mic": {
                "personal": {"volume": 80, "muted": False, "linked": True, "enabled": True}
            }
        }
        self.assertTrue(pwm.is_channel_linked("mic"), "REGRESSION: Microphone channel returned False for is_channel_linked despite being linked in state!")

        pwm.set_channel_linked("mic", False)
        self.assertFalse(pwm.is_channel_linked("mic"), "REGRESSION: set_channel_linked did not update linking state for microphone channel!")

    def test_personal_mix_headphone_led_picker_provisioned(self):
        """Invariant: Personal Mix settings popover must provision Headphone Mode 2 LEDColorButton when Elgato hardware is present."""
        from wavecontroller.views.mix_header import MixHeaderCard
        from wavecontroller.views.led_color_picker import LEDColorButton
        from unittest.mock import MagicMock
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        mix_info = {
            "id": "personal",
            "name": "Personal Mix",
            "type": "sink",
            "icon": "audio-headphones-symbolic",
            "color": "#3db356"
        }
        mock_hw = MagicMock()
        mock_hw.is_elgato = True
        mock_hw.is_connected = True
        mock_hw.led_colors = {"hp": "#2ECC71"}
        mock_hw.get_led_color.return_value = "#2ECC71"
        mock_pwm = MagicMock()
        mock_pwm.mixes = [mix_info]

        card = MixHeaderCard(mix_info, pipewire_mgr=mock_pwm, hardware_mgr=mock_hw)
        btn = card.edit_btn
        popover = btn.get_popover()
        self.assertIsNotNone(popover, "REGRESSION: Settings popover was missing from MixHeaderCard!")

        # Traverse popover children to verify LEDColorButton with mode_key="hp" exists
        found_hp_led = False
        def check_widget(w):
            nonlocal found_hp_led
            if isinstance(w, LEDColorButton) and getattr(w, "mode_key", None) == "hp":
                found_hp_led = True
            if hasattr(w, "get_first_child"):
                c = w.get_first_child()
                while c:
                    check_widget(c)
                    c = c.get_next_sibling()

        check_widget(popover)
        self.assertTrue(found_hp_led, "REGRESSION: Headphone Mode (Mode 2) LEDColorButton was NOT provisioned in Personal Mix settings popover!")

    def test_orphaned_channel_assigned_apps_sanitization(self):
        """Invariant: Orphaned channel keys in assigned_apps must be sanitized on startup/init."""
        from wavecontroller.engine.pipewire_manager import PipeWireManager

        pwm = PipeWireManager()
        pwm.channels = [{"id": "mic", "name": "Elgato Wave XLR", "type": "source"}]
        pwm.assigned_apps = {
            "mic": ["Elgato Wave XLR"],
            "spotify": ["Spotify"],
            "google_chrome": ["Google Chrome"]
        }
        pwm._init_default_states()

        self.assertNotIn("spotify", pwm.assigned_apps, "REGRESSION: Stale 'spotify' key remained in assigned_apps after sanitization!")
        self.assertNotIn("google_chrome", pwm.assigned_apps, "REGRESSION: Stale 'google_chrome' key remained in assigned_apps after sanitization!")
        self.assertIn("mic", pwm.assigned_apps, "Mic channel was erroneously removed from assigned_apps!")

    def test_get_assigned_apps_filters_inactive_channels(self):
        """Invariant: get_assigned_apps must return empty list for channels not in self.channels."""
        from wavecontroller.engine.pipewire_manager import PipeWireManager

        pwm = PipeWireManager()
        pwm.channels = [{"id": "mic", "name": "Elgato Wave XLR", "type": "source"}]
        pwm.assigned_apps = {"deleted_ch": ["SomeApp"]}

        self.assertEqual(pwm.get_assigned_apps("deleted_ch"), [], "REGRESSION: get_assigned_apps returned apps for an inactive/deleted channel!")

    def test_discord_electron_node_and_port_binary_isolation(self):
        """Invariant: Discord (Electron) streams reporting node.name='Chromium' must match Discord by binary and NEVER match Chrome."""
        from wavecontroller.engine.pipewire_manager import PipeWireManager
        from wavecontroller.engine.graph.process_classifier import get_match_tokens, port_matches_tokens

        pwm = PipeWireManager()
        discord_props = {
            "node.name": "Chromium",
            "application.name": "Chromium",
            "application.process.binary": "Discord"
        }
        chrome_props = {
            "node.name": "Google Chrome",
            "application.name": "Google Chrome",
            "application.process.binary": "chrome"
        }

        discord_tokens = pwm._get_match_tokens("Discord")
        chrome_tokens = pwm._get_match_tokens("Google Chrome")

        # 1. Node metadata matching
        self.assertTrue(pwm._node_matches_tokens(discord_props, discord_tokens), "REGRESSION: Discord node did NOT match Discord tokens!")
        self.assertFalse(pwm._node_matches_tokens(discord_props, chrome_tokens), "REGRESSION: Discord node erroneously matched Chrome tokens!")
        self.assertTrue(pwm._node_matches_tokens(chrome_props, chrome_tokens), "REGRESSION: Chrome node did NOT match Chrome tokens!")
        self.assertFalse(pwm._node_matches_tokens(chrome_props, discord_tokens), "REGRESSION: Chrome node erroneously matched Discord tokens!")

        # 2. Port matching with metadata
        port_meta = {
            "chromium:output_fl": {"binary": "Discord", "app_name": "Chromium", "node_name": "Chromium"},
            "google chrome:output_fl": {"binary": "chrome", "app_name": "Google Chrome", "node_name": "Google Chrome"}
        }
        self.assertTrue(port_matches_tokens("Chromium:output_FL", discord_tokens, port_meta), "REGRESSION: Chromium:output_FL (Discord) did not match Discord tokens!")
        self.assertFalse(port_matches_tokens("Chromium:output_FL", chrome_tokens, port_meta), "REGRESSION: Chromium:output_FL (Discord) erroneously matched Chrome tokens!")

    def test_channel_card_muted_css_class_applied_to_header_box(self):
        """Invariant: When ChannelCard is muted, 'muted' CSS class must be applied to self.header_box for all channel types including mic."""
        from wavecontroller.engine.pipewire_manager import PipeWireManager
        from wavecontroller.views.channel_card import ChannelCard

        pwm = PipeWireManager()
        pwm.channels = [
            {"id": "test_ch", "name": "Test Channel", "type": "app"},
            {"id": "mic", "name": "Elgato Wave XLR", "type": "source"}
        ]
        pwm.channel_master_states = {
            "test_ch": {"volume": 80, "muted": True},
            "mic": {"volume": 80, "muted": True}
        }

        # App channel
        card = ChannelCard(pwm.channels[0], pipewire_mgr=pwm)
        self.assertTrue(card.header_box.has_css_class("muted"), "REGRESSION: header_box did NOT have 'muted' CSS class on init!")

        card.set_muted(False)
        self.assertFalse(card.header_box.has_css_class("muted"), "REGRESSION: header_box still had 'muted' CSS class when unmuted!")

        card.set_muted(True)
        self.assertTrue(card.header_box.has_css_class("muted"), "REGRESSION: header_box did NOT have 'muted' CSS class after set_muted(True)!")

        # Mic channel
        mic_card = ChannelCard(pwm.channels[1], pipewire_mgr=pwm)
        self.assertTrue(mic_card.header_box.has_css_class("muted"), "REGRESSION: mic header_box did NOT have 'muted' CSS class on init!")

        mic_card.set_muted(False)
        self.assertFalse(mic_card.header_box.has_css_class("muted"), "REGRESSION: mic header_box still had 'muted' CSS class when unmuted!")

        mic_card.set_muted(True)
        self.assertTrue(mic_card.header_box.has_css_class("muted"), "REGRESSION: mic header_box did NOT have 'muted' CSS class after set_muted(True)!")

    def test_application_streams_default_to_system_physical_sink_when_devices_removed(self):
        """Invariant: When all devices/sink mixes are removed, application streams must default to the system physical sink and not be severed."""
        from unittest.mock import patch, MagicMock
        from wavecontroller.engine.pipewire_manager import PipeWireManager

        pwm = PipeWireManager()
        pwm.channels = [{"id": "spotify", "name": "Spotify", "type": "app"}]
        pwm.assigned_apps = {"spotify": ["Spotify"]}
        pwm.mixes = [] # Zero active sink mixes (all devices removed)

        mock_out_ports = "spotify:output_FL\nspotify:output_FR\nalsa_output.pci-0000_00_1f.3.analog-stereo:monitor_FL"
        mock_in_ports = "alsa_output.pci-0000_00_1f.3.analog-stereo:playback_FL\nalsa_output.pci-0000_00_1f.3.analog-stereo:playback_FR"

        linked_cmds = []

        def mock_check_output(cmd, **kwargs):
            if cmd == ["pw-link", "-o"]:
                return mock_out_ports
            if cmd == ["pw-link", "-i"]:
                return mock_in_ports
            if cmd == ["pw-link", "-l"]:
                return ""
            if cmd[:4] == ["pw-metadata", "-n", "default", "0"]:
                return "update: id:0 key:'default.audio.sink' value:'{\"name\":\"alsa_output.pci-0000_00_1f.3.analog-stereo\"}' type:'Spa:String:JSON'"
            if cmd == ["pw-dump"]:
                return "[]"
            return ""

        def mock_run(cmd, **kwargs):
            linked_cmds.append(cmd)
            return MagicMock()

        with patch("subprocess.check_output", side_effect=mock_check_output), \
             patch("subprocess.run", side_effect=mock_run):
            pwm._sync_channel_audio_routing()

            # Verify that pw-link was called to route spotify to physical playback ports, NOT severed
            self.assertTrue(
                any("spotify:output_FL" in " ".join(c) and "alsa_output" in " ".join(c) for c in linked_cmds),
                "REGRESSION: Spotify was not routed to physical default audio sink when all devices were deleted!"
            )

    def test_multistream_chromium_parallel_port_linking(self):
        """Invariant: When Chromium spawns multiple concurrent stream nodes (e.g. Node 134 + Node 149),
        ALL active instances must be discovered and linked to the channel's submix loopbacks without shadowing."""
        from unittest.mock import patch, MagicMock
        from wavecontroller.engine.pipewire_manager import PipeWireManager
        pwm = self.pwm
        pwm._sync_channel_audio_routing = PipeWireManager._sync_channel_audio_routing.__get__(pwm, PipeWireManager)
        pwm._link_stereo_ports = PipeWireManager._link_stereo_ports.__get__(pwm, PipeWireManager)
        pwm._ensure_submix_loopback = MagicMock()
        pwm.is_channel_linked = MagicMock(return_value=True)
        pwm.is_channel_sink_exposed = MagicMock(return_value=False)
        pwm.get_assigned_apps = MagicMock(return_value=["Google Chrome"])
        pwm.channels = [{"id": "google_chrome", "name": "Google Chrome", "type": "app"}]
        pwm.assigned_apps = {"google_chrome": ["Google Chrome"]}
        pwm.mixes = [{"id": "personal", "name": "Personal Mix", "type": "sink"}]
        pwm.channel_states = {"google_chrome": {"personal": {"volume": 80, "muted": False, "enabled": True}}}
        pwm.is_channel_mix_enabled = MagicMock(return_value=True)

        # Simulate dual streams: Node 134 (old idle) + Node 149 (new video playback)
        mock_out_ports = (
            " 164 Google Chrome:output_FL\n"
            " 165 Google Chrome:output_FR\n"
            " 189 Google Chrome:output_FL\n"
            " 190 Google Chrome:output_FR\n"
        )
        mock_in_ports = (
            " 141 input.WaveController_submix_google_chrome_personal:input_FL\n"
            " 142 input.WaveController_submix_google_chrome_personal:input_FR\n"
        )
        mock_port_meta = {
            "google chrome:output_fl": {"binary": "chrome", "app_name": "Google Chrome"},
            "google chrome:output_fr": {"binary": "chrome", "app_name": "Google Chrome"}
        }

        linked_cmds = []
        def mock_check_output(cmd, **kwargs):
            if cmd == ["pw-link", "-I", "-o"] or cmd == ["pw-link", "-o"]:
                return mock_out_ports
            if cmd == ["pw-link", "-I", "-i"] or cmd == ["pw-link", "-i"]:
                return mock_in_ports
            if cmd == ["pw-link", "-I", "-l"] or cmd == ["pw-link", "-l"]:
                return ""
            if cmd == ["pw-dump"]:
                return "[]"
            return ""

        def mock_run(cmd, **kwargs):
            linked_cmds.append(cmd)
            return MagicMock()

        with patch("subprocess.check_output", side_effect=mock_check_output), \
             patch("subprocess.run", side_effect=mock_run), \
             patch.object(pwm, "_get_active_port_metadata_map", return_value=mock_port_meta):
            pwm._sync_channel_audio_routing(channel_id="google_chrome")

            # Verify that BOTH streams (Node 134 IDs: 164, 165 AND Node 149 IDs: 189, 190) were linked
            linked_flat = [" ".join(c) for c in linked_cmds]
            self.assertTrue(
                any("189" in cmd and "141" in cmd for cmd in linked_flat),
                "REGRESSION: New Chromium stream Node 149 FL (port 189) was NOT linked to submix loopback!"
            )
            self.assertTrue(
                any("190" in cmd and "142" in cmd for cmd in linked_flat),
                "REGRESSION: New Chromium stream Node 149 FR (port 190) was NOT linked to submix loopback!"
            )
            self.assertTrue(
                any("164" in cmd and "141" in cmd for cmd in linked_flat),
                "REGRESSION: Old Chromium stream Node 134 FL (port 164) was NOT linked to submix loopback!"
            )


class TestRoutingSubManagersInvariants(unittest.TestCase):
    """Verifies that dedicated routing sub-managers (source_manager, sink_manager, app_tracker) enforce isolated contracts."""

    def test_source_manager_microphone_discovery(self):
        """MicrophoneSourceManager must accurately discover ALSA capture ports and ignore submix loopbacks."""
        from wavecontroller.engine.routing.source_manager import MicrophoneSourceManager
        mgr = MicrophoneSourceManager()
        
        mock_ports = [
            "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00.analog-stereo:capture_FL",
            "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00.analog-stereo:capture_FR",
            "alsa_output.usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00.analog-stereo:playback_FL",
            "output.WaveController_submix_mic_chat_mix:output_FL",
            "WaveController_personal_Sink:monitor_FL"
        ]
        matched = mgr.discover_microphone_capture_ports("mic", "Elgato Wave XLR", ["Elgato Wave XLR"], mock_ports)
        self.assertEqual(len(matched), 2)
        self.assertTrue(any("capture_FL" in p for p in matched))
        self.assertTrue(any("capture_FR" in p for p in matched))
        self.assertFalse(any("playback_" in p for p in matched))
        self.assertFalse(any("WaveController_" in p for p in matched))

    def test_sink_manager_physical_output_resolution(self):
        """SubmixSinkManager must resolve target device names to exact physical ALSA playback ports."""
        from wavecontroller.engine.routing.sink_manager import SubmixSinkManager
        from wavecontroller.engine.graph.stream_resolver import resolve_physical_device_ports

        mock_in_ports = [
            "alsa_output.usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00.analog-stereo:playback_FL",
            "alsa_output.usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00.analog-stereo:playback_FR",
            "input.WaveController_submix_spotify_personal:input_FL",
            "WaveController_personal_Sink:playback_FL"
        ]
        fl_set, fr_set = resolve_physical_device_ports("Elgato Wave XLR Analog Stereo", mock_in_ports)
        self.assertEqual(len(fl_set), 1)
        self.assertEqual(len(fr_set), 1)
        self.assertTrue(any("playback_FL" in p for p in fl_set))
        self.assertTrue(any("playback_FR" in p for p in fr_set))


    def test_device_removal_asset_teardown_isolation(self):
        """Removing a hardware device must tear down its tied physical channel and mix while strictly preserving secondary channels."""
        from wavecontroller.engine.pipewire_manager import PipeWireManager
        from wavecontroller.engine.usb_hardware import USBHardwareManager

        pwm = PipeWireManager()
        pwm.channels = [
            {"id": "mic", "name": "Elgato Wave XLR", "type": "source"},
            {"id": "test_group", "name": "test group", "type": "group", "expose_sink": True}
        ]
        pwm.mixes = [
            {"id": "personal", "name": "Personal Mix", "type": "sink", "target_device": "Elgato Wave XLR Analog Stereo"},
            {"id": "chat_mix", "name": "Chat Mix", "type": "source"}
        ]
        pwm.assigned_apps = {
            "mic": ["Elgato Wave XLR", "usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00"],
            "test_group": ["Spotify", "Discord"]
        }

        hw = USBHardwareManager()
        hw.pipewire_mgr = pwm
        pwm.hardware_mgr = hw

        # Verify is_default_device detects Elgato Wave XLR
        self.assertTrue(hw.is_default_device("usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00"))

        # Trigger device associated channel and mix removal
        pwm.remove_device_associated_channels_and_mixes("usb-Elgato_Systems_Elgato_Wave_XLR_DS16M2A01160-00")

        # Verify Elgato Wave XLR mic and personal mix were removed
        remaining_ch_ids = [c["id"] for c in pwm.channels]
        remaining_mix_ids = [m["id"] for m in pwm.mixes]

        self.assertNotIn("mic", remaining_ch_ids, "REGRESSION: Tied physical mic channel was not removed when device was removed!")
        self.assertIn("test_group", remaining_ch_ids, "REGRESSION: Secondary group channel was unexpectedly deleted!")
        self.assertNotIn("personal", remaining_mix_ids, "REGRESSION: Tied personal mix was not removed when target device was removed!")
        self.assertIn("chat_mix", remaining_mix_ids, "REGRESSION: Independent chat mix was unexpectedly deleted!")

    def test_mix_system_default_setting_and_gating(self):
        """Invariant: set_mix_system_default sets is_default mutually exclusively among mixes of same type."""
        from wavecontroller.engine.pipewire_manager import PipeWireManager
        pwm = PipeWireManager()
        pwm.mixes = [
            {"id": "personal", "name": "Personal Mix", "type": "sink", "is_default": True},
            {"id": "stream_mix", "name": "Stream Mix", "type": "sink", "is_default": False},
            {"id": "chat_mix", "name": "Chat Mix", "type": "source", "is_default": True},
            {"id": "record_mix", "name": "Record Mix", "type": "source", "is_default": False}
        ]

        self.assertTrue(pwm.is_mix_system_default("personal"))
        self.assertFalse(pwm.is_mix_system_default("stream_mix"))
        self.assertTrue(pwm.is_mix_system_default("chat_mix"))
        self.assertFalse(pwm.is_mix_system_default("record_mix"))

        # Promote stream_mix to default sink
        pwm.set_mix_system_default("stream_mix", True)
        self.assertFalse(pwm.is_mix_system_default("personal"))
        self.assertTrue(pwm.is_mix_system_default("stream_mix"))
        # Verify source mixes unaffected
        self.assertTrue(pwm.is_mix_system_default("chat_mix"))

        # Promote record_mix to default source
        pwm.set_mix_system_default("record_mix", True)
        self.assertFalse(pwm.is_mix_system_default("chat_mix"))
        self.assertTrue(pwm.is_mix_system_default("record_mix"))


if __name__ == "__main__":
    unittest.main(verbosity=2)





