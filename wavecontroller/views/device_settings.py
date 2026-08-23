import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

class InputDeviceSettingsView(Gtk.Box):
    """
    Dedicated view for Audio Input / Microphone device selection,
    custom nicknames, real-time vocal VU meter diagnostics, direct loopback test,
    and hardware preamp DSP controls (Gain, Clipguard, Low-Cut, Phantom Power).
    """
    def __init__(self, hardware_mgr, peak_monitor, on_device_renamed=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.hardware_mgr = hardware_mgr
        self.peak_monitor = peak_monitor
        self.on_device_renamed = on_device_renamed

        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(24)
        self.set_margin_end(24)

        # Title
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_lbl = Gtk.Label(label="Microphone & Vocal Input Settings")
        title_lbl.add_css_class("wave-main-title")
        title_box.append(title_lbl)
        self.append(title_box)

        pref_page = Adw.PreferencesPage()

        # Group 1: Device Assignment & Custom Name
        grp_assign = Adw.PreferencesGroup(title="Active Microphone Device &amp; Nickname")

        self.input_row = Adw.ComboRow(title="Active Microphone / Audio Input", subtitle="Select hardware input for WaveController")
        input_names = [self.hardware_mgr.get_device_display_name(d) for d in self.hardware_mgr.input_devices]
        self.input_model = Gtk.StringList.new(input_names)
        self.input_row.set_model(self.input_model)
        
        for idx, d in enumerate(self.hardware_mgr.input_devices):
            if d.get("is_default"):
                self.input_row.set_selected(idx)
                break
        self.input_row.connect("notify::selected", self._on_input_device_changed)
        grp_assign.add(self.input_row)

        self.input_name_row = Adw.EntryRow(title="Microphone Nickname")
        self._refresh_input_nickname()
        self.input_name_row.connect("apply", self._on_input_nickname_applied)
        self.input_name_row.connect("entry-activated", self._on_input_nickname_applied)
        grp_assign.add(self.input_name_row)

        pref_page.add(grp_assign)

        # Group 2: Real-Time Input Level & Live Voice Feedback
        grp_meter = Adw.PreferencesGroup(title="Live Microphone Signal &amp; Diagnostics")

        meter_row = Adw.ActionRow(title="Vocal Input Signal", subtitle="Real-time audio activity")
        meter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        meter_box.set_size_request(240, -1)
        meter_box.set_valign(Gtk.Align.CENTER)

        self.meter_bar = Gtk.ProgressBar()
        self.meter_bar.set_fraction(0.0)
        self.meter_bar.set_size_request(160, 10)
        self.meter_bar.add_css_class("wave-slider")
        meter_box.append(self.meter_bar)

        self.db_label = Gtk.Label(label="-∞ dB")
        self.db_label.set_size_request(60, -1)
        self.db_label.add_css_class("mix-header-subtitle")
        meter_box.append(self.db_label)

        meter_row.add_suffix(meter_box)
        grp_meter.add(meter_row)

        listen_row = Adw.ActionRow(title="Mic Test (Direct Loopback)", subtitle="Hear your live voice in headphones to verify levels")
        self.listen_btn = Gtk.Button(label="Listen to Mic")
        self.listen_btn.set_icon_name("audio-headset-symbolic")
        self.listen_btn.set_valign(Gtk.Align.CENTER)
        self.listen_btn.connect("clicked", self._on_toggle_mic_listen)
        listen_row.add_suffix(self.listen_btn)
        grp_meter.add(listen_row)

        pref_page.add(grp_meter)

        # Group 3: Hardware Preamp & DSP
        grp_dsp = Adw.PreferencesGroup(title="Hardware Audio &amp; DSP Controls")

        self.gain_row = Adw.ActionRow(title="Preamp Gain", subtitle=f"{self.hardware_mgr.hardware_gain_db} dB")
        self.gain_adj = Gtk.Adjustment(value=self.hardware_mgr.hardware_gain_db, lower=0, upper=75, step_increment=1, page_increment=5)
        self.gain_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.gain_adj)
        self.gain_slider.set_size_request(180, -1)
        self.gain_slider.set_valign(Gtk.Align.CENTER)
        self.gain_slider.connect("value-changed", self._on_gain_changed)
        self.gain_row.add_suffix(self.gain_slider)
        grp_dsp.add(self.gain_row)

        self.phantom_row = Adw.SwitchRow(title="48V Phantom Power", subtitle="Requires condenser XLR microphone")
        self.phantom_row.set_active(self.hardware_mgr.phantom_power_48v)
        self.phantom_row.connect("notify::active", self._on_phantom_toggled)
        if self.hardware_mgr.device_type != "elgato":
            self.phantom_row.set_sensitive(False)
            self.phantom_row.set_subtitle("Not applicable on USB microphone")
        grp_dsp.add(self.phantom_row)

        self.clipguard_row = Adw.SwitchRow(title="Clipguard Protection", subtitle="Dual-stage analog/software limiter to prevent vocal clipping")
        self.clipguard_row.set_active(self.hardware_mgr.clipguard_enabled)
        self.clipguard_row.connect("notify::active", self._on_clipguard_toggled)
        grp_dsp.add(self.clipguard_row)

        self.low_cut_row = Adw.ComboRow(title="Enhanced Low-Cut Filter", subtitle="Remove low-frequency rumble")
        self.low_cut_model = Gtk.StringList.new(["Off", "80 Hz", "120 Hz"])
        self.low_cut_row.set_model(self.low_cut_model)
        self.low_cut_row.set_selected(1 if self.hardware_mgr.low_cut_filter == "80Hz" else (2 if self.hardware_mgr.low_cut_filter == "120Hz" else 0))
        self.low_cut_row.connect("notify::selected", self._on_low_cut_changed)
        grp_dsp.add(self.low_cut_row)

        pref_page.add(grp_dsp)
        self.append(pref_page)

        GLib.timeout_add(25, self._on_meter_tick)

    def _on_input_device_changed(self, row, *args):
        idx = row.get_selected()
        if idx < len(self.hardware_mgr.input_devices):
            dev = self.hardware_mgr.input_devices[idx]
            self.hardware_mgr.set_active_input_device(dev["id"])
            self._refresh_input_nickname()

    def _get_selected_input_device(self):
        idx = self.input_row.get_selected()
        if idx < len(self.hardware_mgr.input_devices):
            return self.hardware_mgr.input_devices[idx]
        return None

    def _refresh_input_nickname(self):
        dev = self._get_selected_input_device()
        if dev:
            alias = self.hardware_mgr.get_device_display_name(dev)
            real_name = dev.get("name", "")
            self.input_name_row.set_text(alias if alias != real_name else "")
            self.input_name_row.set_tooltip_text(f"Hardware device: {real_name}")

    def _on_input_nickname_applied(self, row, *args):
        dev = self._get_selected_input_device()
        if dev:
            new_alias = self.input_name_row.get_text()
            self.hardware_mgr.set_device_custom_name(dev["name"], new_alias)
            if self.on_device_renamed:
                self.on_device_renamed()

    def refresh_device_names(self):
        input_names = [self.hardware_mgr.get_device_display_name(d) for d in self.hardware_mgr.input_devices]
        curr_idx = self.input_row.get_selected()
        self.input_row.set_model(Gtk.StringList.new(input_names))
        if curr_idx < len(input_names):
            self.input_row.set_selected(curr_idx)
        self._refresh_input_nickname()

    def _on_toggle_mic_listen(self, btn):
        active = self.hardware_mgr.toggle_mic_monitoring()
        if active:
            self.listen_btn.set_label("Stop Listening")
            self.listen_btn.add_css_class("suggested-action")
        else:
            self.listen_btn.set_label("Listen to Mic")
            self.listen_btn.remove_css_class("suggested-action")

    def _on_meter_tick(self) -> bool:
        peak = self.peak_monitor.get_channel_peak("mic")
        self.meter_bar.set_fraction(peak)
        
        if peak > 0.01:
            import math
            db = 20 * math.log10(peak)
            self.db_label.set_text(f"{db:.1f} dB")
        else:
            self.db_label.set_text("-∞ dB")
        return True

    def _on_gain_changed(self, scale):
        val = int(self.gain_adj.get_value())
        self.hardware_mgr.set_gain(val)
        self.gain_row.set_subtitle(f"{val} dB")

    def _on_phantom_toggled(self, row, *args):
        self.hardware_mgr.phantom_power_48v = row.get_active()

    def _on_clipguard_toggled(self, row, *args):
        self.hardware_mgr.clipguard_enabled = row.get_active()

    def _on_low_cut_changed(self, row, *args):
        idx = row.get_selected()
        mode = "Off" if idx == 0 else ("80Hz" if idx == 1 else "120Hz")
        self.hardware_mgr.set_low_cut(mode)


class OutputDeviceSettingsView(Gtk.Box):
    """
    Dedicated view for Audio Output / Headphones / Speakers settings,
    custom nicknames, master volume slider, mute controls, and audio test chime.
    """
    def __init__(self, hardware_mgr, on_device_renamed=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.hardware_mgr = hardware_mgr
        self.on_device_renamed = on_device_renamed

        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(24)
        self.set_margin_end(24)

        # Title
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_lbl = Gtk.Label(label="Monitor Output & Headphones Settings")
        title_lbl.add_css_class("wave-main-title")
        title_box.append(title_lbl)
        self.append(title_box)

        pref_page = Adw.PreferencesPage()

        # Group 1: Device Assignment & Custom Name
        grp_assign = Adw.PreferencesGroup(title="Active Output Device &amp; Nickname")

        self.output_row = Adw.ComboRow(title="Active Monitor Output / Headphones", subtitle="Destination for Personal Mix listening")
        output_names = [self.hardware_mgr.get_device_display_name(d) for d in self.hardware_mgr.output_devices]
        self.output_model = Gtk.StringList.new(output_names)
        self.output_row.set_model(self.output_model)
        for idx, d in enumerate(self.hardware_mgr.output_devices):
            if d.get("is_default"):
                self.output_row.set_selected(idx)
                break
        self.output_row.connect("notify::selected", self._on_output_device_changed)
        grp_assign.add(self.output_row)

        self.output_name_row = Adw.EntryRow(title="Output Device Nickname")
        self._refresh_output_nickname()
        self.output_name_row.connect("apply", self._on_output_nickname_applied)
        self.output_name_row.connect("entry-activated", self._on_output_nickname_applied)
        grp_assign.add(self.output_name_row)

        pref_page.add(grp_assign)

        # Group 2: Master Output Volume & Diagnostic Test
        grp_vol = Adw.PreferencesGroup(title="Master Output &amp; Diagnostics")

        vol_row = Adw.ActionRow(title="Master Output Volume", subtitle="Adjust overall headphone/speaker level")
        curr_vol = self.hardware_mgr.get_output_volume()
        self.vol_adj = Gtk.Adjustment(value=curr_vol, lower=0, upper=100, step_increment=1, page_increment=5)
        self.vol_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.vol_adj)
        self.vol_slider.set_size_request(180, -1)
        self.vol_slider.set_valign(Gtk.Align.CENTER)
        self.vol_slider.connect("value-changed", self._on_output_volume_changed)
        vol_row.add_suffix(self.vol_slider)
        grp_vol.add(vol_row)

        # Mute Output Toggle
        mute_row = Adw.ActionRow(title="Output Mute", subtitle="Mute all sound output to this device")
        self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.add_css_class("wave-icon-btn")
        self.mute_btn.set_valign(Gtk.Align.CENTER)
        self.mute_btn.connect("clicked", self._on_output_mute_clicked)
        self._update_mute_button_state()
        mute_row.add_suffix(self.mute_btn)
        grp_vol.add(mute_row)

        # Test Sound Chime
        test_row = Adw.ActionRow(title="Audio Test Chime", subtitle="Play clean stereo chime to verify output")
        test_sound_btn = Gtk.Button(label="Test Output")
        test_sound_btn.set_icon_name("media-playback-start-symbolic")
        test_sound_btn.set_valign(Gtk.Align.CENTER)
        test_sound_btn.connect("clicked", lambda b: self.hardware_mgr.test_output_chime(self._get_selected_output_device()["id"] if self._get_selected_output_device() else None))
        test_row.add_suffix(test_sound_btn)
        grp_vol.add(test_row)

        pref_page.add(grp_vol)

        # Group 3: Audio Engine Latency & Format
        grp_engine = Adw.PreferencesGroup(title="Audio Processing &amp; Latency")

        rate_row = Adw.ComboRow(title="Output Sample Rate", subtitle="Hardware digital audio rate")
        rate_row.set_model(Gtk.StringList.new(["48,000 Hz (Broadcast standard)", "44,100 Hz", "96,000 Hz (Hi-Res)"]))
        rate_row.set_selected(0)
        grp_engine.add(rate_row)

        buffer_row = Adw.ComboRow(title="Buffer Size / Latency", subtitle="Lower values reduce monitoring latency")
        buffer_row.set_model(Gtk.StringList.new(["64 samples (1.3 ms)", "128 samples (2.7 ms - Recommended)", "256 samples (5.3 ms)", "512 samples (10.7 ms)"]))
        buffer_row.set_selected(1)
        grp_engine.add(buffer_row)

        pref_page.add(grp_engine)
        self.append(pref_page)

    def _get_selected_output_device(self):
        idx = self.output_row.get_selected()
        if idx < len(self.hardware_mgr.output_devices):
            return self.hardware_mgr.output_devices[idx]
        return None

    def _on_output_device_changed(self, row, *args):
        idx = row.get_selected()
        if idx < len(self.hardware_mgr.output_devices):
            dev = self.hardware_mgr.output_devices[idx]
            self.hardware_mgr.set_active_output_device(dev["id"])
            self._refresh_output_nickname()
            self._update_mute_button_state()
            self.vol_adj.set_value(self.hardware_mgr.get_output_volume(dev["id"]))

    def _refresh_output_nickname(self):
        dev = self._get_selected_output_device()
        if dev:
            alias = self.hardware_mgr.get_device_display_name(dev)
            real_name = dev.get("name", "")
            self.output_name_row.set_text(alias if alias != real_name else "")
            self.output_name_row.set_tooltip_text(f"Hardware device: {real_name}")

    def _on_output_nickname_applied(self, row, *args):
        dev = self._get_selected_output_device()
        if dev:
            new_alias = self.output_name_row.get_text()
            self.hardware_mgr.set_device_custom_name(dev["name"], new_alias)
            if self.on_device_renamed:
                self.on_device_renamed()

    def _on_output_volume_changed(self, scale):
        val = int(self.vol_adj.get_value())
        dev = self._get_selected_output_device()
        sink_id = dev["id"] if dev else None
        self.hardware_mgr.set_output_volume(sink_id, val)

    def _on_output_mute_clicked(self, btn):
        dev = self._get_selected_output_device()
        sink_id = dev["id"] if dev else None
        self.hardware_mgr.toggle_output_mute(sink_id)
        self._update_mute_button_state()

    def _update_mute_button_state(self):
        dev = self._get_selected_output_device()
        sink_id = dev["id"] if dev else None
        is_muted = self.hardware_mgr.get_output_mute(sink_id)
        if is_muted:
            self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.mute_btn.add_css_class("muted")
            self.mute_btn.set_tooltip_text("Unmute Output")
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")
            self.mute_btn.set_tooltip_text("Mute Output")

    def refresh_device_names(self):
        output_names = [self.hardware_mgr.get_device_display_name(d) for d in self.hardware_mgr.output_devices]
        curr_idx = self.output_row.get_selected()
        self.output_row.set_model(Gtk.StringList.new(output_names))
        if curr_idx < len(output_names):
            self.output_row.set_selected(curr_idx)
        self._refresh_output_nickname()

# Backward compatibility alias
DeviceSettingsView = InputDeviceSettingsView


