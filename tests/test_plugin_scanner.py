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
        self.assertEqual(p.plugin_uri, "http://calf.sourceforge.net/plugins/Equalizer8Band")

    def test_lv2_port_metadata_parsing(self):
        scanner = PluginScanner()
        ttl_data = """
        lv2:port [
            a lv2:InputPort, lv2:AudioPort ;
            lv2:index 0 ;
            lv2:symbol "in_l" ;
            lv2:name "In L" ;
        ] , [
            a lv2:InputPort, lv2:AudioPort ;
            lv2:index 1 ;
            lv2:symbol "in_r" ;
            lv2:name "In R" ;
        ] , [
            a lv2:OutputPort, lv2:AudioPort ;
            lv2:index 2 ;
            lv2:symbol "out_l" ;
            lv2:name "Out L" ;
        ] , [
            a lv2:OutputPort, lv2:AudioPort ;
            lv2:index 3 ;
            lv2:symbol "out_r" ;
            lv2:name "Out R" ;
        ] , [
            a lv2:InputPort, lv2:ControlPort ;
            lv2:index 4 ;
            lv2:symbol "mix" ;
            lv2:name "Mix" ;
            lv2:default 50 ;
            lv2:minimum 0 ;
            lv2:maximum 100 ;
        ] .
        """

        inputs, outputs, params = scanner._parse_lv2_ports(ttl_data)

        self.assertEqual(inputs, ["in_l", "in_r"])
        self.assertEqual(outputs, ["out_l", "out_r"])
        self.assertEqual(len(params), 1)
        self.assertEqual(params[0].id, "mix")
        self.assertEqual(params[0].default_value, 50)

    def test_install_package_folder_installs_child_bundles(self):
        package_dir = os.path.join(self.test_dir, "dragonfly-reverb-3.2.10")
        hall_bundle = os.path.join(package_dir, "DragonflyHallReverb.vst3")
        room_bundle = os.path.join(package_dir, "DragonflyRoomReverb.vst3")
        hall_bin = os.path.join(hall_bundle, "Contents", "x86_64-linux")
        room_bin = os.path.join(room_bundle, "Contents", "x86_64-linux")
        os.makedirs(hall_bin, exist_ok=True)
        os.makedirs(room_bin, exist_ok=True)
        with open(os.path.join(hall_bin, "DragonflyHallReverb.so"), "w") as f:
            f.write("mock binary")
        with open(os.path.join(room_bin, "DragonflyRoomReverb.so"), "w") as f:
            f.write("mock binary")

        install_dir = os.path.join(self.test_dir, "home", ".vst3")
        manifest_file = os.path.join(self.test_dir, "installed_plugins.json")

        scanner = PluginScanner()
        scanner.MANIFEST_FILE = manifest_file
        scanner._manifest = {}

        with patch("wavecontroller.engine.plugins.scanner.os.path.expanduser") as expanduser:
            def _expand(path):
                if path == "~/.vst3":
                    return install_dir
                if path == "~/.lv2":
                    return os.path.join(self.test_dir, "home", ".lv2")
                return os.path.expandvars(path)

            expanduser.side_effect = _expand
            success, message = scanner.install_plugin_from_path(package_dir)

        self.assertTrue(success, message)
        self.assertTrue(os.path.isdir(os.path.join(install_dir, "DragonflyHallReverb.vst3")))
        self.assertTrue(os.path.isdir(os.path.join(install_dir, "DragonflyRoomReverb.vst3")))
        self.assertFalse(os.path.islink(os.path.join(install_dir, "DragonflyHallReverb.vst3")))
        self.assertFalse(os.path.islink(os.path.join(install_dir, "DragonflyRoomReverb.vst3")))
        self.assertFalse(os.path.lexists(os.path.join(install_dir, "dragonfly-reverb-3.2.10")))
        self.assertIn(os.path.join(install_dir, "DragonflyHallReverb.vst3"), scanner._manifest)
        self.assertIn(os.path.join(install_dir, "DragonflyRoomReverb.vst3"), scanner._manifest)
        self.assertEqual(scanner._manifest[os.path.join(install_dir, "DragonflyHallReverb.vst3")]["install_method"], "copy")

    def test_reinstall_converts_owned_symlink_to_copy(self):
        bundle_path = os.path.join(self.vst3_dir, "TestReverb.vst3")
        bin_dir = os.path.join(bundle_path, "Contents", "x86_64-linux")
        os.makedirs(bin_dir, exist_ok=True)
        with open(os.path.join(bin_dir, "TestReverb.so"), "w") as f:
            f.write("mock binary")

        install_dir = os.path.join(self.test_dir, "home", ".vst3")
        os.makedirs(install_dir, exist_ok=True)
        dest_path = os.path.join(install_dir, "TestReverb.vst3")
        os.symlink(bundle_path, dest_path, target_is_directory=True)

        scanner = PluginScanner()
        scanner._manifest = {
            dest_path: {
                "format": "VST3",
                "source_path": bundle_path,
                "symlink_path": dest_path,
            }
        }

        with patch("wavecontroller.engine.plugins.scanner.os.path.expanduser") as expanduser:
            def _expand(path):
                if path == "~/.vst3":
                    return install_dir
                return os.path.expandvars(path)

            expanduser.side_effect = _expand
            success, message = scanner.install_plugin_from_path(bundle_path)

        self.assertTrue(success, message)
        self.assertIn("Converted", message)
        self.assertTrue(os.path.isdir(dest_path))
        self.assertFalse(os.path.islink(dest_path))
        self.assertEqual(scanner._manifest[dest_path]["install_method"], "copy")
        self.assertNotIn("symlink_path", scanner._manifest[dest_path])

    def test_reinstall_converts_same_source_orphan_symlink_to_copy(self):
        bundle_path = os.path.join(self.lv2_dir, "TestReverb.lv2")
        os.makedirs(bundle_path, exist_ok=True)
        with open(os.path.join(bundle_path, "manifest.ttl"), "w") as f:
            f.write("mock manifest")

        install_dir = os.path.join(self.test_dir, "home", ".lv2")
        os.makedirs(install_dir, exist_ok=True)
        dest_path = os.path.join(install_dir, "TestReverb.lv2")
        os.symlink(bundle_path, dest_path, target_is_directory=True)

        scanner = PluginScanner()
        scanner._manifest = {}

        with patch("wavecontroller.engine.plugins.scanner.os.path.expanduser") as expanduser:
            def _expand(path):
                if path == "~/.lv2":
                    return install_dir
                return os.path.expandvars(path)

            expanduser.side_effect = _expand
            success, message = scanner.install_plugin_from_path(bundle_path)

        self.assertTrue(success, message)
        self.assertIn("Converted", message)
        self.assertTrue(os.path.isdir(dest_path))
        self.assertFalse(os.path.islink(dest_path))
        self.assertEqual(scanner._manifest[dest_path]["install_method"], "copy")
        self.assertEqual(scanner._manifest[dest_path]["source_path"], bundle_path)

    def test_reinstall_adopts_existing_copy_when_manifest_missing(self):
        bundle_path = os.path.join(self.vst3_dir, "TestReverb.vst3")
        bin_dir = os.path.join(bundle_path, "Contents", "x86_64-linux")
        os.makedirs(bin_dir, exist_ok=True)
        with open(os.path.join(bin_dir, "TestReverb.so"), "w") as f:
            f.write("mock binary")

        install_dir = os.path.join(self.test_dir, "home", ".vst3")
        os.makedirs(install_dir, exist_ok=True)
        dest_path = os.path.join(install_dir, "TestReverb.vst3")
        shutil.copytree(bundle_path, dest_path)

        scanner = PluginScanner()
        scanner._manifest = {}

        with patch("wavecontroller.engine.plugins.scanner.os.path.expanduser") as expanduser:
            def _expand(path):
                if path == "~/.vst3":
                    return install_dir
                return os.path.expandvars(path)

            expanduser.side_effect = _expand
            success, message = scanner.install_plugin_from_path(bundle_path)

        self.assertTrue(success, message)
        self.assertTrue(os.path.isdir(dest_path))
        self.assertFalse(os.path.islink(dest_path))
        self.assertEqual(scanner._manifest[dest_path]["install_method"], "copy")
        self.assertIn("adopted_at", scanner._manifest[dest_path])

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
