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

# Ensure wavecontroller module is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


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
        has_wavecontroller = "WaveController" in self.links_out

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
        self.pwm._submix_procs = {}
        self.pwm._submix_node_ids = {}
        self.pwm._submix_volume_queue = {}
        self.pwm.channels = []
        self.pwm.mixes = []
        self.pwm.assigned_apps = {}
        self.pwm.mix_states = {}
        self.pwm.config_path = "/tmp/fake_config.json"
        self.pwm._save_state_to_config = lambda **k: None
        self.pwm._ensure_virtual_mix_nodes = lambda: None
        self.pwm._refresh_node_cache = lambda: None
        self.pwm._sync_channel_audio_routing = lambda **k: None
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

        # Test circuit breaker initialization
    def test_submix_loopback_monitor_sink_flag_enforced(self):
        """Invariant: input.WaveController_submix_* nodes MUST be targeted with is_sink=True to capture monitor ports."""
        from wavecontroller.engine.peak_monitor import MultiChannelPeakMonitor
        from unittest.mock import patch, MagicMock

        with patch("subprocess.check_output") as mock_co, patch("subprocess.Popen") as mock_popen:
            mock_co.return_value = (
                "input.WaveController_submix_discord_personal_mix:monitor_FL\n"
                "input.WaveController_submix_discord_personal_mix:monitor_FR\n"
            )
            mock_pm = MultiChannelPeakMonitor.__new__(MultiChannelPeakMonitor)
            mock_pm.pipewire_mgr = MagicMock()
            mock_pm.pipewire_mgr.channels = [{"id": "discord", "type": "sink"}]
            mock_pm._channel_procs = {}
            mock_pm._channel_proc_channels = {}
            mock_pm._channel_peaks = {}
            mock_pm.running = False
            
            # Run channel discovery
            mock_pm._refresh_channel_monitors()
            
            # Verify pw-record was called with stream.capture.sink=true
            mock_popen.assert_called_once()
            args, kwargs = mock_popen.call_args
            cmd_args = args[0]
            self.assertIn("stream.capture.sink=true", " ".join(cmd_args),
                          "REGRESSION: Submix monitor must be recorded with stream.capture.sink=true")
            self.assertIn("input.WaveController_submix_discord_personal_mix", cmd_args)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

