import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

class DeviceSettingsView(Gtk.Box):
    """
    Hardware DSP and Device Settings view for Wave XLR, Wave:3, and generic USB microphones.
    """
    def __init__(self, hardware_mgr):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.hardware_mgr = hardware_mgr

        self.set_margin_top(20)
        self.set_margin_bottom(20)
        self.set_margin_start(24)
        self.set_margin_end(24)

        # Title
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_lbl = Gtk.Label(label=f"Device Settings — {self.hardware_mgr.device_name}")
        title_lbl.add_css_class("wave-main-title")
        title_box.append(title_lbl)
        self.append(title_box)

        # Preferences Groups Container
        pref_page = Adw.PreferencesPage()

        # Group 1: Hardware Preamp & DSP
        grp_dsp = Adw.PreferencesGroup(title="Hardware Audio & DSP")

        # Preamp Gain
        self.gain_row = Adw.ActionRow(title="Preamp Gain", subtitle=f"{self.hardware_mgr.hardware_gain_db} dB")
        self.gain_adj = Gtk.Adjustment(value=self.hardware_mgr.hardware_gain_db, lower=0, upper=75, step_increment=1, page_increment=5)
        self.gain_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.gain_adj)
        self.gain_slider.set_size_request(200, -1)
        self.gain_slider.set_valign(Gtk.Align.CENTER)
        self.gain_slider.connect("value-changed", self._on_gain_changed)
        self.gain_row.add_suffix(self.gain_slider)
        grp_dsp.add(self.gain_row)

        # 48V Phantom Power (Wave XLR)
        self.phantom_row = Adw.SwitchRow(title="48V Phantom Power", subtitle="Requires condenser XLR microphone")
        self.phantom_row.set_active(self.hardware_mgr.phantom_power_48v)
        self.phantom_row.connect("notify::active", self._on_phantom_toggled)
        if self.hardware_mgr.device_type != "elgato":
            self.phantom_row.set_sensitive(False)
            self.phantom_row.set_subtitle("Not applicable on USB microphone")
        grp_dsp.add(self.phantom_row)

        # Clipguard Anti-Clipping Limiter
        self.clipguard_row = Adw.SwitchRow(title="Clipguard", subtitle="Dual-stage limiter to prevent vocal distortion")
        self.clipguard_row.set_active(self.hardware_mgr.clipguard_enabled)
        self.clipguard_row.connect("notify::active", self._on_clipguard_toggled)
        grp_dsp.add(self.clipguard_row)

        # Low-Cut Filter
        self.low_cut_row = Adw.ComboRow(title="Enhanced Low-Cut Filter", subtitle="Remove low-frequency rumble")
        self.low_cut_model = Gtk.StringList.new(["Off", "80 Hz", "120 Hz"])
        self.low_cut_row.set_model(self.low_cut_model)
        self.low_cut_row.set_selected(1 if self.hardware_mgr.low_cut_filter == "80Hz" else (2 if self.hardware_mgr.low_cut_filter == "120Hz" else 0))
        self.low_cut_row.connect("notify::selected", self._on_low_cut_changed)
        grp_dsp.add(self.low_cut_row)

        pref_page.add(grp_dsp)

        # Group 2: Headphone & Monitoring
        grp_mon = Adw.PreferencesGroup(title="Headphone Monitoring")

        # Headphone Volume
        self.hp_row = Adw.ActionRow(title="Headphone Output Level", subtitle=f"{self.hardware_mgr.headphone_volume}%")
        self.hp_adj = Gtk.Adjustment(value=self.hardware_mgr.headphone_volume, lower=0, upper=100, step_increment=1, page_increment=5)
        self.hp_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.hp_adj)
        self.hp_slider.set_size_request(200, -1)
        self.hp_slider.set_valign(Gtk.Align.CENTER)
        self.hp_slider.connect("value-changed", self._on_hp_changed)
        self.hp_row.add_suffix(self.hp_slider)
        grp_mon.add(self.hp_row)

        # Mic / PC Crossfade
        self.fade_row = Adw.ActionRow(title="Mic / PC Crossfade", subtitle=f"{self.hardware_mgr.mic_pc_crossfade}%")
        self.fade_adj = Gtk.Adjustment(value=self.hardware_mgr.mic_pc_crossfade, lower=0, upper=100, step_increment=1, page_increment=5)
        self.fade_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.fade_adj)
        self.fade_slider.set_size_request(200, -1)
        self.fade_slider.set_valign(Gtk.Align.CENTER)
        self.fade_slider.connect("value-changed", self._on_fade_changed)
        self.fade_row.add_suffix(self.fade_slider)
        grp_mon.add(self.fade_row)

        pref_page.add(grp_mon)
        self.append(pref_page)

    def _on_gain_changed(self, scale):
        val = int(self.gain_adj.get_value())
        self.hardware_mgr.set_gain(val)
        self.gain_row.set_subtitle(f"{val} dB")

    def _on_phantom_toggled(self, row, *args):
        active = row.get_active()
        self.hardware_mgr.phantom_power_48v = active

    def _on_clipguard_toggled(self, row, *args):
        active = row.get_active()
        self.hardware_mgr.clipguard_enabled = active

    def _on_low_cut_changed(self, row, *args):
        idx = row.get_selected()
        mode = "Off" if idx == 0 else ("80Hz" if idx == 1 else "120Hz")
        self.hardware_mgr.set_low_cut(mode)

    def _on_hp_changed(self, scale):
        val = int(self.hp_adj.get_value())
        self.hardware_mgr.headphone_volume = val
        self.hp_row.set_subtitle(f"{val}%")

    def _on_fade_changed(self, scale):
        val = int(self.fade_adj.get_value())
        self.hardware_mgr.mic_pc_crossfade = val
        self.fade_row.set_subtitle(f"{val}%")
