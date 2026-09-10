"""
WaveController Audio Plugins & Real-Time DSP Subsystem.
"""

from .models import AudioPlugin, PluginFormat, PluginCategory, PluginParameter
from .scanner import PluginScanner, plugin_scanner
from .fx_chain import ChannelFXChain, FXChainManager, fx_manager

__all__ = [
    "AudioPlugin",
    "PluginFormat",
    "PluginCategory",
    "PluginParameter",
    "PluginScanner",
    "plugin_scanner",
    "ChannelFXChain",
    "FXChainManager",
    "fx_manager",
]
