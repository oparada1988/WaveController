"""
Unit tests for WaveController Plugin Scanner & Metadata Models.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from wavecontroller.engine.plugins.models import (
    AudioPlugin, PluginFormat, PluginCategory, PluginParameter
)
from wavecontroller.engine.plugins.scanner import PluginScanner


class TestPluginScanner(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="wave_test_plugins_")
        self.vst3_dir = os.path.join(self.test_dir, "vst3")
        self.lv2_dir = os.path.join(self.test_dir, "lv2")
        self.ladspa_dir = os.path.join(self.test_dir, "ladspa")
        self.cache_file = os.path.join(self.test_dir, "test_cache.json")

        os.makedirs(self.vst3_dir, exist_ok=True)
        os.makedirs(self.lv2_dir, exist_ok=True)
        os.makedirs(self.ladspa_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_builtin_plugins(self):
        scanner = PluginScanner()
        scanner.CACHE_FILE = self.cache_file
        scanner.DEFAULT_VST3_PATHS = []
        scanner.DEFAULT_LV2_PATHS = []
        scanner.DEFAULT_LADSPA_PATHS = []
        scanner.DEFAULT_CLAP_PATHS = []

        plugins = scanner.scan_sync()
        ids = [p.id for p in plugins]

        self.assertIn("builtin.rnnoise", ids)
        self.assertIn("builtin.noisegate", ids)
        self.assertIn("builtin.peq3", ids)
        self.assertIn("builtin.compressor", ids)
        self.assertIn("builtin.deesser", ids)
        self.assertIn("builtin.limiter", ids)

        rnnoise = scanner.get_plugin("builtin.rnnoise")
        self.assertIsNotNone(rnnoise)
        self.assertEqual(rnnoise.format, PluginFormat.BUILTIN)
        self.assertEqual(rnnoise.category, PluginCategory.NOISE_REDUCTION)
        self.assertTrue(rnnoise.is_builtin)

    def test_vst3_bundle_parsing_with_moduleinfo(self):
        # Create a mock VST3 bundle with moduleinfo.json
        bundle_path = os.path.join(self.vst3_dir, "TestVocalComp.vst3")
        res_dir = os.path.join(bundle_path, "Contents", "Resources")
        bin_dir = os.path.join(bundle_path, "Contents", "x86_64-linux")
        os.makedirs(res_dir, exist_ok=True)
        os.makedirs(bin_dir, exist_ok=True)

        with open(os.path.join(bin_dir, "TestVocalComp.so"), "w") as f:
            f.write("mock binary")

        moduleinfo_content = {
            "Name": "Pro Vocal Compressor",
            "Vendor": "AudioLabs",
            "Version": "2.1.0",
            "Description": "Professional dynamic vocal compressor",
            "SubCategories": ["Fx", "Dynamics"]
        }
        import json
        with open(os.path.join(res_dir, "moduleinfo.json"), "w") as f:
            json.dump(moduleinfo_content, f)

        scanner = PluginScanner()
        scanner.CACHE_FILE = self.cache_file
        scanner.DEFAULT_VST3_PATHS = [self.vst3_dir]
        scanner.DEFAULT_LV2_PATHS = []
        scanner.DEFAULT_LADSPA_PATHS = []
        scanner.DEFAULT_CLAP_PATHS = []

        plugins = scanner.scan_sync()
        vst = scanner.get_plugin("vst3.testvocalcomp")

        self.assertIsNotNone(vst)
        self.assertEqual(vst.name, "Pro Vocal Compressor")
        self.assertEqual(vst.vendor, "AudioLabs")
        self.assertEqual(vst.version, "2.1.0")
        self.assertEqual(vst.format, PluginFormat.VST3)
        self.assertEqual(vst.category, PluginCategory.DYNAMICS)
        self.assertTrue(vst.has_custom_gui)

    def test_lv2_bundle_parsing(self):
        # Create a mock LV2 bundle with manifest.ttl
        bundle_path = os.path.join(self.lv2_dir, "calf_eq.lv2")
        os.makedirs(bundle_path, exist_ok=True)

        manifest_ttl = """
@prefix lv2:  <http://lv2plug.in/ns/lv2core#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<http://calf.sourceforge.net/plugins/Equalizer8Band>
    a lv2:Plugin ;
    lv2:binary <calfeq.so> ;
    rdfs:seeAlso <calfeq.ttl> .
"""
        calfeq_ttl = """
@prefix doap: <http://usefulinc.com/ns/doap#> .
@prefix lv2:  <http://lv2plug.in/ns/lv2core#> .

<http://calf.sourceforge.net/plugins/Equalizer8Band>
    a lv2:Plugin , lv2:EQPlugin ;
    doap:name "Calf 8-Band Parametric Equalizer" .
"""
        with open(os.path.join(bundle_path, "manifest.ttl"), "w") as f:
            f.write(manifest_ttl)
        with open(os.path.join(bundle_path, "calfeq.ttl"), "w") as f:
            f.write(calfeq_ttl)
        with open(os.path.join(bundle_path, "calfeq.so"), "w") as f:
            f.write("mock binary")

        scanner = PluginScanner()
        scanner.CACHE_FILE = self.cache_file
        scanner.DEFAULT_VST3_PATHS = []
        scanner.DEFAULT_LV2_PATHS = [self.lv2_dir]
        scanner.DEFAULT_LADSPA_PATHS = []
        scanner.DEFAULT_CLAP_PATHS = []

        plugins = scanner.scan_sync()
        lv2_plugins = scanner.get_plugins_by_format(PluginFormat.LV2)

        self.assertEqual(len(lv2_plugins), 1)
        p = lv2_plugins[0]
        self.assertEqual(p.name, "Calf 8-Band Parametric Equalizer")
        self.assertEqual(p.category, PluginCategory.EQUALIZER)
        self.assertEqual(p.format, PluginFormat.LV2)

    def test_cache_persistence_and_reload(self):
        scanner1 = PluginScanner()
        scanner1.CACHE_FILE = self.cache_file
        scanner1.DEFAULT_VST3_PATHS = []
        scanner1.DEFAULT_LV2_PATHS = []
        scanner1.DEFAULT_LADSPA_PATHS = []
        scanner1.DEFAULT_CLAP_PATHS = []
        scanner1.scan_sync()

        self.assertTrue(os.path.exists(self.cache_file))

        # Second scanner instance loading from cache
        scanner2 = PluginScanner()
        scanner2.CACHE_FILE = self.cache_file
        scanner2._load_cache()
        self.assertGreaterEqual(len(scanner2.get_all_plugins()), 7)


if __name__ == "__main__":
    unittest.main()
