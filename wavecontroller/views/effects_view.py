import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

from ..engine.config_manager import config_manager

class EffectsView(Gtk.Box):
    """
    Audio Effects & VST/LV2 Plugin Rack for WaveController.
    """
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)

        self.set_margin_top(20)
        self.set_margin_bottom(20)
        self.set_margin_start(24)
        self.set_margin_end(24)

        # Title
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_lbl = Gtk.Label(label="Audio Effects & DSP Plugins")
        title_lbl.add_css_class("wave-main-title")
        title_box.append(title_lbl)
        self.append(title_box)

        pref_page = Adw.PreferencesPage()

        # Group: Active Vocal DSP Chain
        grp_fx = Adw.PreferencesGroup(title="Microphone DSP Chain (PipeWire Filter-Chain)")

        # RNNoise Noise Suppression
        noise_row = Adw.SwitchRow(title="AI Noise Suppression (RNNoise / DeepFilterNet)", subtitle="Eliminates background keyboard clicks, fans, and room noise")
        noise_row.set_active(config_manager.get("dsp_noise_suppression", True))
        noise_row.connect("notify::active", lambda r, *a: config_manager.set("dsp_noise_suppression", r.get_active(), immediate=True))
        grp_fx.add(noise_row)

        # Parametric EQ
        eq_row = Adw.SwitchRow(title="Parametric Vocal Equalizer", subtitle="3-Band broadcast tone shaping")
        eq_row.set_active(config_manager.get("dsp_equalizer", True))
        eq_row.connect("notify::active", lambda r, *a: config_manager.set("dsp_equalizer", r.get_active(), immediate=True))
        grp_fx.add(eq_row)

        # Studio Compressor
        comp_row = Adw.SwitchRow(title="Broadcast Vocal Compressor", subtitle="Smooths dynamic volume spikes and boosts presence")
        comp_row.set_active(config_manager.get("dsp_compressor", True))
        comp_row.connect("notify::active", lambda r, *a: config_manager.set("dsp_compressor", r.get_active(), immediate=True))
        grp_fx.add(comp_row)

        # De-Esser
        deess_row = Adw.SwitchRow(title="Vocal De-Esser", subtitle="Attenuates harsh sibilance ('s' and 't' sounds)")
        deess_row.set_active(config_manager.get("dsp_deesser", False))
        deess_row.connect("notify::active", lambda r, *a: config_manager.set("dsp_deesser", r.get_active(), immediate=True))
        grp_fx.add(deess_row)

        pref_page.add(grp_fx)

        # Group: VST3 / LV2 Plugin Hosting
        grp_vst = Adw.PreferencesGroup(title="External VST3 & LV2 Plugin Directory")
        vst_row = Adw.ActionRow(title="Scan VST3 / LV2 Plugins", subtitle="~/.vst3, /usr/lib/vst3, ~/.lv2")
        scan_btn = Gtk.Button(label="Scan Plugins")
        scan_btn.set_valign(Gtk.Align.CENTER)
        vst_row.add_suffix(scan_btn)
        grp_vst.add(vst_row)

        pref_page.add(grp_vst)
        self.append(pref_page)
