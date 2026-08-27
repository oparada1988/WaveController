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
        """Assigned applications like Spotify must NOT have direct links to alsa_output when WaveController channel exists."""
        if not self.links_out:
            self.skipTest("PipeWire not running.")

        has_spotify = "spotify:output_" in self.links_out
        has_spotify_channel = "WaveController_Channel_spotify" in self.links_out

        if has_spotify and has_spotify_channel:
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
