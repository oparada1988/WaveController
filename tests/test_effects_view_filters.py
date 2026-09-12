"""
Unit tests for EffectsView plugin library filtering helpers.
"""

from wavecontroller.engine.plugins.models import AudioPlugin, PluginCategory, PluginFormat
from wavecontroller.views.effects_view import _is_hostable_lv2_plugin, _plugin_display_key


def test_hostable_lv2_plugin_requires_stereo_ports_and_uri():
    plugin = AudioPlugin(
        id="lv2.test",
        name="Test Plugin",
        format=PluginFormat.LV2,
        category=PluginCategory.OTHER,
        plugin_uri="urn:test",
        audio_input_ports=["in_l", "in_r"],
        audio_output_ports=["out_l", "out_r"],
    )

    assert _is_hostable_lv2_plugin(plugin)

    plugin.audio_output_ports = ["out_mono"]
    assert not _is_hostable_lv2_plugin(plugin)


def test_display_key_collapses_spacing_and_camelcase_equivalents():
    lv2_name = AudioPlugin(id="lv2.dragonfly", name="Dragonfly Hall Reverb", format=PluginFormat.LV2)
    vst3_name = AudioPlugin(id="vst3.dragonfly", name="DragonflyHallReverb", format=PluginFormat.VST3)

    assert _plugin_display_key(lv2_name) == _plugin_display_key(vst3_name)
