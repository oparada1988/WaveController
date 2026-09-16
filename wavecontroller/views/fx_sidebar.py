import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

from ..engine.config_manager import config_manager
from ..engine.plugins import plugin_scanner, PluginFormat
from ..utils.logger import get_logger

log = get_logger("FXSidebar")


class FXSidebar(Gtk.Box):
    """Right-side inspector for the selected microphone channel's effects."""

    EFFECTS = [
        ("dsp_noise_suppression", "AI Noise Suppression", "Neural background noise removal", True),
        ("dsp_noise_gate", "Noise Gate", "Eliminates room hiss and breath bleed", False),
        ("dsp_equalizer", "Parametric Equalizer", "3-band vocal tone shaping", True),
        ("dsp_compressor", "Vocal Compressor", "Smooth broadcast leveling", True),
        ("dsp_deesser", "Vocal De-Esser", "Harsh sibilance smoothing", False),
        ("dsp_limiter", "Peak Limiter", "Brickwall output guard", True),
        ("dsp_highpass", "Low-Cut Filter", "High-pass rumble guard", True),
    ]

    def __init__(self, pipewire_mgr, on_closed=None, on_fx_changed=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.pipewire_mgr = pipewire_mgr
        self.on_closed = on_closed
        self.on_fx_changed = on_fx_changed
        self.channel_info = None
        self.add_css_class("fx-sidebar")
        self.set_size_request(260, -1)
        self.set_hexpand(False)
        self.set_halign(Gtk.Align.END)
        self.set_vexpand(True)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.add_css_class("fx-sidebar-header")
        self.title_label = Gtk.Label(label="Audio Effects")
        self.title_label.add_css_class("wave-sidebar-title")
        self.title_label.set_halign(Gtk.Align.START)
        self.title_label.set_hexpand(True)
        header.append(self.title_label)
        close_button = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_button.add_css_class("flat")
        close_button.add_css_class("wave-icon-btn")
        close_button.set_tooltip_text("Close effects sidebar")
        close_button.connect("clicked", self._on_close_clicked)
        header.append(close_button)
        self.append(header)

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.content.set_margin_start(14)
        self.content.set_margin_end(14)
        self.content.set_margin_bottom(16)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(self.content)
        self.append(scroll)

    def show_channel(self, channel_info: dict):
        self.channel_info = dict(channel_info)
        self._rebuild()

    def _on_close_clicked(self, _button):
        if self.on_closed:
            self.on_closed()

    def _channel_config(self):
        channel_id = self.channel_info["id"]
        channel_fx = config_manager.get("channel_fx", {})
        return dict(channel_fx.get(channel_id, {}))

    def _ensure_channel_config(self):
        channel_id = self.channel_info["id"]
        config = dict(config_manager.get("channel_fx", {}))
        if channel_id not in config:
            config[channel_id] = {
                "enabled": False,
                **{key: config_manager.get(key, default) for key, _, _, default in self.EFFECTS},
            }
        else:
            config[channel_id] = dict(config[channel_id])
        return config

    def _save_setting(self, key, value, reload_chain=True):
        config = self._ensure_channel_config()
        channel_id = self.channel_info["id"]
        config[channel_id][key] = value
        config_manager.set("channel_fx", config, immediate=True)
        if reload_chain and self.pipewire_mgr:
            self.pipewire_mgr.reload_channel_fx(channel_id)
        if self.on_fx_changed:
            self.on_fx_changed(channel_id)

    def _save_external_enabled(self, plugin_id, enabled):
        config = self._ensure_channel_config()
        channel_id = self.channel_info["id"]
        plugins = dict(config[channel_id].get("external_plugins", {}))
        plugins[plugin_id] = bool(enabled)
        config[channel_id]["external_plugins"] = plugins
        config_manager.set("channel_fx", config, immediate=True)
        if self.pipewire_mgr:
            self.pipewire_mgr.reload_channel_fx(channel_id)
        if self.on_fx_changed:
            self.on_fx_changed(channel_id)

    def _save_intensity(self, key, value, external=False):
        config = self._ensure_channel_config()
        channel_id = self.channel_info["id"]
        if external:
            intensity_map = dict(config[channel_id].get("external_plugin_intensity", {}))
            intensity_map[key] = int(value)
            config[channel_id]["external_plugin_intensity"] = intensity_map
        else:
            config[channel_id][f"{key}_intensity"] = int(value)
        config_manager.set("channel_fx", config, immediate=True)

    def _apply_intensity(self, key, value, external=False):
        channel_id = self.channel_info["id"]
        try:
            chain = self.pipewire_mgr.fx_manager.get_chain(channel_id)
            updated = (
                chain.update_external_lv2_intensity_live(key, value)
                if external else chain.update_builtin_intensity_live(key, value)
            )
            if not updated:
                self.pipewire_mgr.reload_channel_fx(channel_id)
        except Exception:
            log.exception("Failed to apply FX intensity for channel '%s'", channel_id)
            self.pipewire_mgr.reload_channel_fx(channel_id)

    def _hostable_external_plugins(self):
        globally_enabled = config_manager.get("external_plugin_enabled", {})
        if not isinstance(globally_enabled, dict):
            globally_enabled = {}
        return sorted(
            [
                plugin for plugin in plugin_scanner.get_all_plugins()
                if plugin.format == PluginFormat.LV2
                and plugin.plugin_uri
                and len(plugin.audio_input_ports) >= 2
                and len(plugin.audio_output_ports) >= 2
                and globally_enabled.get(plugin.id, True)
            ],
            key=lambda plugin: plugin.name.lower(),
        )

    def _rebuild(self):
        while child := self.content.get_first_child():
            self.content.remove(child)

        channel_config = self._channel_config()
        channel_name = self.channel_info.get("name", "Microphone")
        subtitle = Gtk.Label(label=channel_name)
        subtitle.add_css_class("fx-sidebar-channel")
        subtitle.set_halign(Gtk.Align.START)
        subtitle.set_ellipsize(3)
        self.content.append(subtitle)

        master_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        master_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        master_title = Gtk.Label(label="Enable Audio Effects")
        master_title.set_halign(Gtk.Align.START)
        master_title.add_css_class("bold")
        master_text.append(master_title)
        master_description = Gtk.Label(label="Enable processing for this channel")
        master_description.set_halign(Gtk.Align.START)
        master_description.add_css_class("dim-label")
        master_description.add_css_class("caption")
        master_text.append(master_description)
        master_text.set_hexpand(True)
        master_row.append(master_text)
        master_enabled = bool(channel_config.get("enabled", False))
        master_switch = Gtk.Switch(active=master_enabled)
        master_switch.set_valign(Gtk.Align.CENTER)
        master_row.append(master_switch)
        self.content.append(master_row)

        effects_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        effects_box.set_sensitive(master_enabled)
        self.content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self.content.append(effects_box)

        def on_master_toggled(switch, _param):
            self._save_setting("enabled", switch.get_active())
            self._rebuild()

        master_switch.connect("notify::active", on_master_toggled)

        for effect_key, title, description, default in self.EFFECTS:
            if not config_manager.get(effect_key, default):
                continue
            active = bool(channel_config.get(effect_key, config_manager.get(effect_key, default)))
            intensity = channel_config.get(f"{effect_key}_intensity", 50)
            self._append_effect_row(effects_box, effect_key, title, description, active, intensity)

        external_plugins = self._hostable_external_plugins()
        if external_plugins:
            effects_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            external_title = Gtk.Label(label="External LV2 Plugins")
            external_title.add_css_class("wave-sidebar-section-title")
            external_title.set_halign(Gtk.Align.START)
            effects_box.append(external_title)
            enabled = channel_config.get("external_plugins", {})
            intensities = channel_config.get("external_plugin_intensity", {})
            for plugin in external_plugins:
                self._append_effect_row(
                    effects_box,
                    plugin.id,
                    plugin.name,
                    f"{plugin.category.value} - LV2",
                    bool(enabled.get(plugin.id, False)),
                    intensities.get(plugin.id, 50),
                    external=True,
                )

    def _append_effect_row(self, container, effect_key, title, description, active, intensity, external=False):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(label=title)
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(3)
        label.set_hexpand(True)
        title_row.append(label)
        toggle = Gtk.Switch(active=active)
        toggle.set_valign(Gtk.Align.CENTER)
        title_row.append(toggle)
        text.append(title_row)
        detail = Gtk.Label(label=description)
        detail.add_css_class("dim-label")
        detail.add_css_class("caption")
        detail.set_halign(Gtk.Align.START)
        detail.set_ellipsize(3)
        text.append(detail)
        row.append(text)
        slider_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        slider.set_value(intensity)
        slider.set_draw_value(False)
        slider.set_hexpand(True)
        slider.set_sensitive(active)
        slider.add_css_class("fx-intensity-slider")
        slider.set_tooltip_text(f"{title} Intensity: {intensity}%")
        slider_row.append(slider)
        value_label = Gtk.Label(label=f"{intensity}%")
        value_label.add_css_class("fx-intensity-value")
        value_label.set_size_request(34, -1)
        value_label.set_xalign(1.0)
        slider_row.append(value_label)
        row.append(slider_row)

        def on_toggle(switch, _param):
            slider.set_sensitive(switch.get_active())
            if external:
                self._save_external_enabled(effect_key, switch.get_active())
            else:
                self._save_setting(effect_key, switch.get_active())

        debounce = {"id": None}

        def on_intensity_changed(scale):
            value = int(round(scale.get_value()))
            value_label.set_label(f"{value}%")
            slider.set_tooltip_text(f"{title} Intensity: {value}%")

            def commit():
                debounce["id"] = None
                self._save_intensity(effect_key, value, external=external)
                self._apply_intensity(effect_key, value, external=external)
                return False
            if debounce["id"] is not None:
                GLib.source_remove(debounce["id"])
            debounce["id"] = GLib.timeout_add(120, commit)

        toggle.connect("notify::active", on_toggle)
        slider.connect("value-changed", on_intensity_changed)
        container.append(row)