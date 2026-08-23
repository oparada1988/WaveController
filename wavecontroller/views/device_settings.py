import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango
import math

class UnifiedDeviceSettingsView(Gtk.Box):
    """
    Unified device management view for a specific physical hardware device.
    Dynamically renders Microphone (Capture) and Headphone Monitor (Playback)
    controls based on device capabilities (Duplex, Input Only, Output Only).
    """
    def __init__(self, device_info: dict, hardware_mgr, peak_monitor, pipewire_mgr=None, on_device_renamed=None, on_device_removed=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.device_info = device_info
        self.hardware_mgr = hardware_mgr
        self.peak_monitor = peak_monitor
        self.pipewire_mgr = pipewire_mgr
        self.on_device_renamed = on_device_renamed
        self.on_device_removed = on_device_removed

        self.device_key = device_info.get("device_key", "")
        self.device_type = device_info.get("type", "duplex") # "duplex", "input", "output"

        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(24)
        self.set_margin_end(24)

        # 1. Header Area with Device Title, Status & Remove Action
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        
        icon_img = Gtk.Image.new_from_icon_name(device_info.get("icon", "audio-headset-symbolic"))
        icon_img.set_pixel_size(32)
        icon_img.set_valign(Gtk.Align.CENTER)
        header_box.append(icon_img)

        title_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_vbox.set_hexpand(True)

        display_name = self.hardware_mgr.get_device_display_name(self.device_key)
        self.title_lbl = Gtk.Label(label=display_name)
        self.title_lbl.add_css_class("wave-main-title")
        self.title_lbl.set_halign(Gtk.Align.START)
        title_vbox.append(self.title_lbl)

        # Status & Capabilities subtitle
        is_conn = device_info.get("connected", True)
        badge_text = device_info.get("badge", "In / Out")
        desc = device_info.get("description", device_info.get("name", "Audio Device"))
        status_text = f"🟢 Connected • {badge_text} ({desc})" if is_conn else "🟡 Disconnected / Offline"
        self.sub_lbl = Gtk.Label(label=status_text)
        self.sub_lbl.add_css_class("mix-header-subtitle")
        self.sub_lbl.set_halign(Gtk.Align.START)
        title_vbox.append(self.sub_lbl)

        header_box.append(title_vbox)

        # Remove Device Button
        remove_btn = Gtk.Button(label="Remove Device")
        remove_btn.set_icon_name("user-trash-symbolic")
        remove_btn.add_css_class("destructive-action")
        remove_btn.set_valign(Gtk.Align.CENTER)
        remove_btn.connect("clicked", self._on_remove_clicked)
        header_box.append(remove_btn)

        self.append(header_box)

        # 2. Preferences Page
        pref_page = Adw.PreferencesPage()

        # Group 1: Nickname & Identification
        grp_ident = Adw.PreferencesGroup(title="Device Identification &amp; Nickname")
        
        self.name_entry = Adw.EntryRow(title="Custom Device Nickname")
        curr_alias = self.hardware_mgr.get_device_display_name(self.device_key)
        raw_name = device_info.get("name", "")
        self.name_entry.set_text(curr_alias if curr_alias != raw_name else "")
        self.name_entry.set_tooltip_text(f"Original hardware: {raw_name}")
        self.name_entry.connect("apply", self._on_nickname_applied)
        self.name_entry.connect("entry-activated", self._on_nickname_applied)
        grp_ident.add(self.name_entry)

        hw_row = Adw.ActionRow(title="Hardware Identifier", subtitle=self.device_key)
        grp_ident.add(hw_row)

        pref_page.add(grp_ident)

        # Group 2: Microphone (Input) Section (Duplex or Input-Only)
        if self.device_type in ["duplex", "input"]:
            grp_mic = Adw.PreferencesGroup(title="Microphone (Audio Input) &amp; Live Diagnostics")

            # VU Meter
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
            grp_mic.add(meter_row)

            # Preamp Gain Slider
            self.gain_row = Adw.ActionRow(title="Preamp Gain", subtitle=f"{self.hardware_mgr.hardware_gain_db} dB")
            self.gain_adj = Gtk.Adjustment(value=self.hardware_mgr.hardware_gain_db, lower=0, upper=75, step_increment=1, page_increment=5)
            self.gain_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.gain_adj)
            self.gain_slider.set_size_request(180, -1)
            self.gain_slider.set_valign(Gtk.Align.CENTER)
            self.gain_slider.connect("value-changed", self._on_gain_changed)
            self.gain_row.add_suffix(self.gain_slider)
            grp_mic.add(self.gain_row)

            # Mic Test Direct Loopback
            listen_row = Adw.ActionRow(title="Mic Test (Direct Loopback)", subtitle="Hear your live voice in headphones to verify levels")
            self.listen_btn = Gtk.Button(label="Listen to Mic")
            self.listen_btn.set_icon_name("audio-headset-symbolic")
            self.listen_btn.set_valign(Gtk.Align.CENTER)
            self.listen_btn.connect("clicked", self._on_toggle_mic_listen)
            listen_row.add_suffix(self.listen_btn)
            grp_mic.add(listen_row)

            # Hardware DSP & Filters
            self.clipguard_row = Adw.SwitchRow(title="Clipguard Protection", subtitle="Dual-stage limiter to prevent vocal clipping")
            self.clipguard_row.set_active(self.hardware_mgr.clipguard_enabled)
            self.clipguard_row.connect("notify::active", lambda r, *a: self.hardware_mgr.toggle_clipguard())
            grp_mic.add(self.clipguard_row)

            self.low_cut_row = Adw.ComboRow(title="Enhanced Low-Cut Filter", subtitle="Remove low-frequency rumble")
            self.low_cut_model = Gtk.StringList.new(["Off", "80 Hz", "120 Hz"])
            self.low_cut_row.set_model(self.low_cut_model)
            self.low_cut_row.set_selected(1 if self.hardware_mgr.low_cut_filter == "80Hz" else (2 if self.hardware_mgr.low_cut_filter == "120Hz" else 0))
            self.low_cut_row.connect("notify::selected", self._on_low_cut_changed)
            grp_mic.add(self.low_cut_row)

            pref_page.add(grp_mic)

        # Group 3: Headphone Monitor (Output) Section (Duplex or Output-Only)
        if self.device_type in ["duplex", "output"]:
            grp_out = Adw.PreferencesGroup(title="Headphone Monitor &amp; Audio Output")

            # Output Volume
            vol_row = Adw.ActionRow(title="Output / Monitor Volume", subtitle="Adjust overall headphone/speaker level")
            curr_vol = self.hardware_mgr.get_output_volume(self.device_key)
            self.vol_adj = Gtk.Adjustment(value=curr_vol, lower=0, upper=100, step_increment=1, page_increment=5)
            self.vol_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.vol_adj)
            self.vol_slider.set_size_request(180, -1)
            self.vol_slider.set_valign(Gtk.Align.CENTER)
            self.vol_slider.connect("value-changed", self._on_output_volume_changed)
            vol_row.add_suffix(self.vol_slider)
            grp_out.add(vol_row)

            # Output Mute
            mute_row = Adw.ActionRow(title="Output Mute", subtitle="Mute sound output to this device")
            self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
            self.mute_btn.add_css_class("flat")
            self.mute_btn.add_css_class("wave-icon-btn")
            self.mute_btn.set_valign(Gtk.Align.CENTER)
            self.mute_btn.connect("clicked", self._on_output_mute_clicked)
            self._update_mute_button_state()
            mute_row.add_suffix(self.mute_btn)
            grp_out.add(mute_row)

            # Assigned Mix Selection
            if self.pipewire_mgr:
                self.mix_row = Adw.ComboRow(title="Assigned Output Mix", subtitle="Select which WaveController mix routes to this output")
                mix_names = [m.get("name", "Mix") for m in self.pipewire_mgr.mixes]
                self.mix_model = Gtk.StringList.new(mix_names)
                self.mix_row.set_model(self.mix_model)
                
                assigned_id = self.hardware_mgr.get_device_assigned_mix(self.device_key)
                for idx, m in enumerate(self.pipewire_mgr.mixes):
                    if m.get("id") == assigned_id:
                        self.mix_row.set_selected(idx)
                        break
                self.mix_row.connect("notify::selected", self._on_assigned_mix_changed)
                grp_out.add(self.mix_row)

            # Test Sound Chime
            test_row = Adw.ActionRow(title="Audio Test Chime", subtitle="Play clean stereo chime to verify output")
            test_sound_btn = Gtk.Button(label="Test Output")
            test_sound_btn.set_icon_name("media-playback-start-symbolic")
            test_sound_btn.set_valign(Gtk.Align.CENTER)
            test_sound_btn.connect("clicked", lambda b: self.hardware_mgr.test_output_chime(self.device_key))
            test_row.add_suffix(test_sound_btn)
            grp_out.add(test_row)

            pref_page.add(grp_out)

        # Group 4: Device Management / Removal (Explicit Manual Control)
        grp_danger = Adw.PreferencesGroup(title="Device Management")
        remove_row = Adw.ActionRow(
            title="Remove from WaveController",
            subtitle="Remove this device from your sidebar. You can manually add it back anytime."
        )
        remove_btn = Gtk.Button(label="Remove Device")
        remove_btn.set_icon_name("user-trash-symbolic")
        remove_btn.add_css_class("destructive-action")
        remove_btn.set_valign(Gtk.Align.CENTER)
        remove_btn.connect("clicked", self._on_remove_clicked)
        remove_row.add_suffix(remove_btn)
        grp_danger.add(remove_row)
        pref_page.add(grp_danger)

        self.append(pref_page)

        # Prominent Remove Button at bottom of page
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_box.set_margin_top(16)
        btn_box.set_margin_bottom(24)

        big_remove_btn = Gtk.Button(label="Remove Device from WaveController")
        big_remove_btn.set_icon_name("user-trash-symbolic")
        big_remove_btn.add_css_class("destructive-action")
        big_remove_btn.set_size_request(320, 44)
        big_remove_btn.connect("clicked", self._on_remove_clicked)
        btn_box.append(big_remove_btn)
        self.append(btn_box)

        # Start live meter timer if mic is available
        if self.device_type in ["duplex", "input"]:
            GLib.timeout_add(25, self._on_meter_tick)

    def _on_nickname_applied(self, row, *args):
        new_alias = self.name_entry.get_text().strip()
        self.hardware_mgr.set_device_custom_name(self.device_key, new_alias)
        display_name = self.hardware_mgr.get_device_display_name(self.device_key)
        self.title_lbl.set_text(display_name)
        if self.on_device_renamed:
            self.on_device_renamed()

    def _on_remove_clicked(self, btn):
        dialog = Adw.MessageDialog(
            transient_for=self.get_root() if isinstance(self.get_root(), Gtk.Window) else None,
            heading=f"Remove '{self.title_lbl.get_text()}'?",
            body="This device will be removed from WaveController. You can add it back at any time from the 'Add Audio Device' menu."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove Device")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def _on_response(d, response):
            if response == "remove":
                self.hardware_mgr.remove_tracked_device(self.device_key)
                if self.on_device_removed:
                    self.on_device_removed(self.device_key)

        dialog.connect("response", _on_response)
        dialog.present()

    def _on_gain_changed(self, scale):
        val = int(self.gain_adj.get_value())
        self.hardware_mgr.set_gain(val, self.device_key)
        self.gain_row.set_subtitle(f"{val} dB")

    def _on_low_cut_changed(self, row, *args):
        idx = row.get_selected()
        mode = "Off" if idx == 0 else ("80Hz" if idx == 1 else "120Hz")
        self.hardware_mgr.set_low_cut(mode)

    def _on_toggle_mic_listen(self, btn):
        active = self.hardware_mgr.toggle_mic_monitoring()
        if active:
            self.listen_btn.set_label("Stop Listening")
            self.listen_btn.add_css_class("suggested-action")
        else:
            self.listen_btn.set_label("Listen to Mic")
            self.listen_btn.remove_css_class("suggested-action")

    def _on_output_volume_changed(self, scale):
        val = int(self.vol_adj.get_value())
        self.hardware_mgr.set_output_volume(self.device_key, val)

    def _on_output_mute_clicked(self, btn):
        self.hardware_mgr.toggle_output_mute(self.device_key)
        self._update_mute_button_state()

    def _update_mute_button_state(self):
        is_muted = self.hardware_mgr.get_output_mute(self.device_key)
        if is_muted:
            self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.mute_btn.add_css_class("muted")
            self.mute_btn.set_tooltip_text("Unmute Output")
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")
            self.mute_btn.set_tooltip_text("Mute Output")

    def _on_assigned_mix_changed(self, row, *args):
        idx = row.get_selected()
        if self.pipewire_mgr and idx < len(self.pipewire_mgr.mixes):
            mix = self.pipewire_mgr.mixes[idx]
            self.hardware_mgr.set_device_assigned_mix(self.device_key, mix["id"])

    def _on_meter_tick(self) -> bool:
        if not self.get_mapped():
            return True
        peak = self.peak_monitor.get_channel_peak("mic")
        self.meter_bar.set_fraction(peak)
        
        if peak > 0.01:
            db = 20 * math.log10(peak)
            self.db_label.set_text(f"{db:.1f} dB")
        else:
            self.db_label.set_text("-∞ dB")
        return True

    def refresh_device_names(self):
        display_name = self.hardware_mgr.get_device_display_name(self.device_key)
        self.title_lbl.set_text(display_name)


class AddDeviceDialog(Adw.PreferencesDialog):
    """
    Modal preferences dialog allowing users to discover and add untracked
    hardware audio devices (Duplex, Input Only, Output Only) into WaveController.
    """
    def __init__(self, hardware_mgr, on_device_added_callback=None, **kwargs):
        super().__init__(title="Add Audio Device", **kwargs)
        self.hardware_mgr = hardware_mgr
        self.on_device_added_callback = on_device_added_callback
        self.set_size_request(540, 480)

        self._build_content()

    def _build_content(self):
        untracked = self.hardware_mgr.get_available_untracked_devices()
        page = Adw.PreferencesPage()

        if not untracked:
            status = Adw.StatusPage()
            status.set_icon_name("audio-volume-high-symbolic")
            status.set_title("All Devices Added")
            status.set_description("All detected physical audio devices are currently added to WaveController.")
            self.set_child(status)
            return

        duplex_devs = [d for d in untracked if d.get("type") == "duplex"]
        input_devs = [d for d in untracked if d.get("type") == "input"]
        output_devs = [d for d in untracked if d.get("type") == "output"]

        if duplex_devs:
            grp = Adw.PreferencesGroup(title="Duplex Devices (Microphone + Headphone Monitor)")
            for dev in duplex_devs:
                grp.add(self._create_device_row(dev))
            page.add(grp)

        if input_devs:
            grp = Adw.PreferencesGroup(title="Microphones &amp; Vocal Inputs")
            for dev in input_devs:
                grp.add(self._create_device_row(dev))
            page.add(grp)

        if output_devs:
            grp = Adw.PreferencesGroup(title="Speakers &amp; Headphones (Outputs)")
            for dev in output_devs:
                grp.add(self._create_device_row(dev))
            page.add(grp)

        self.add(page)

    def _create_device_row(self, dev: dict) -> Adw.ActionRow:
        row = Adw.ActionRow(title=dev.get("name", "Audio Device"))
        desc = dev.get("description") or dev.get("device_key")
        badge = dev.get("badge", "")
        row.set_subtitle(f"[{badge}] • {desc}")
        row.set_icon_name(dev.get("icon", "audio-card-symbolic"))

        add_btn = Gtk.Button(label="Add")
        add_btn.set_icon_name("list-add-symbolic")
        add_btn.add_css_class("suggested-action")
        add_btn.set_valign(Gtk.Align.CENTER)
        add_btn.connect("clicked", lambda b: self._on_add_clicked(dev["device_key"]))
        row.add_suffix(add_btn)

        return row

    def _on_add_clicked(self, device_key: str):
        self.hardware_mgr.add_tracked_device(device_key)
        self.close()
        if self.on_device_added_callback:
            self.on_device_added_callback(device_key)


# Legacy Compatibility Aliases
InputDeviceSettingsView = UnifiedDeviceSettingsView
OutputDeviceSettingsView = UnifiedDeviceSettingsView
DeviceSettingsView = UnifiedDeviceSettingsView


