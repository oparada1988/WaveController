"""
Audio Plugin Models for WaveController.
Defines data structures for VST3, LV2, LADSPA, CLAP, and Built-in DSP effects.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Any


class PluginFormat(str, Enum):
    BUILTIN = "BUILTIN"
    VST3 = "VST3"
    LV2 = "LV2"
    LADSPA = "LADSPA"
    CLAP = "CLAP"


class PluginCategory(str, Enum):
    NOISE_REDUCTION = "Noise Reduction"
    EQUALIZER = "Equalizer"
    DYNAMICS = "Dynamics"
    SPATIAL = "Reverb & Delay"
    PITCH = "Pitch & Modulation"
    UTILITY = "Utility"
    OTHER = "Other"


@dataclass
class PluginParameter:
    """Represents a controllable parameter on an audio plugin."""
    id: str
    name: str
    min_value: float = 0.0
    max_value: float = 1.0
    default_value: float = 0.0
    current_value: float = 0.0
    unit: str = ""
    is_boolean: bool = False
    step: float = 0.01

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PluginParameter":
        return cls(**data)


@dataclass
class AudioPlugin:
    """Metadata describing an audio effect plugin installed on the system."""
    id: str                          # Unique plugin identifier (URI or bundle ID)
    name: str                        # Display name
    format: PluginFormat             # VST3, LV2, LADSPA, BUILTIN, etc.
    category: PluginCategory = PluginCategory.OTHER
    vendor: str = "Unknown"
    version: str = "1.0.0"
    description: str = ""
    path: str = ""                   # Path to .vst3, .lv2 bundle, or file
    binary_path: str = ""            # Path to executable binary (.so)
    is_builtin: bool = False
    is_user_installed: bool = False  # True only if installed via WaveController's Install Effect flow (removable)
    inputs: int = 2                  # Number of audio input channels
    outputs: int = 2                 # Number of audio output channels
    parameters: List[PluginParameter] = field(default_factory=list)
    has_custom_gui: bool = False     # Whether the plugin has a native editor GUI

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["format"] = self.format.value
        d["category"] = self.category.value
        d["parameters"] = [p.to_dict() for p in self.parameters]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AudioPlugin":
        d = dict(data)
        fmt_str = d.get("format", PluginFormat.BUILTIN.value)
        try:
            d["format"] = PluginFormat(fmt_str)
        except ValueError:
            d["format"] = PluginFormat.BUILTIN

        cat_str = d.get("category", PluginCategory.OTHER.value)
        try:
            d["category"] = PluginCategory(cat_str)
        except ValueError:
            d["category"] = PluginCategory.OTHER

        params_raw = d.get("parameters", [])
        d["parameters"] = [PluginParameter.from_dict(p) if isinstance(p, dict) else p for p in params_raw]
        return cls(**d)
