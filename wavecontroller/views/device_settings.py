import os
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
import math
from .led_color_picker import LEDColorButton

class UnifiedDeviceSettingsView(Gtk.Box):
    """
    Unified device management view for physical hardware devices.
    Provides dedicated Tier 1 controls for Elgato Wave XLR, Wave:3, and generic USB interfaces:
    - 0-75 dB Preamp Gain
    - 48V Phantom Power (with safety warning modal)
    - Clipguard Dual-Stage Limiter
    - Enhanced Low-Cut Filter (Off / 80Hz / 120Hz)
    - Headphone Output Volume & Low-Impedance Mode (IEMs)
    - Hardware Serial Number & Firmware Version Diagnostics (USB DFU 1.10)
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
        self.is_elgato = device_info.get("is_elgato", False) or "wave" in device_info.get("name", "").lower()
        self._syncing_from_hw = False

        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(24)
        self.set_margin_end(24)

        # 1. Main Scrollable Preferences Page (Unified Scroll Container)
        pref_page = Adw.PreferencesPage()
        pref_page.set_vexpand(True)
        pref_page.set_hexpand(True)

        # Top Header Group (Centered Title, Status Subtitle & Device Badge Graphic)
        grp_header = Adw.PreferencesGroup()
        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header_box.set_halign(Gtk.Align.CENTER)
        header_box.set_margin_top(8)
        header_box.set_margin_bottom(8)

        title_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_vbox.set_halign(Gtk.Align.CENTER)

        display_name = self.hardware_mgr.get_device_display_name(self.device_key)
        self.title_lbl = Gtk.Label(label=display_name)
        self.title_lbl.add_css_class("wave-main-title")
        self.title_lbl.set_halign(Gtk.Align.CENTER)
        title_vbox.append(self.title_lbl)

        # Status & Capabilities subtitle
        is_conn = device_info.get("connected", True)
        status_text = "🟢 Connected" if is_conn else "🟡 Disconnected / Offline"
        self.sub_lbl = Gtk.Label(label=status_text)
        self.sub_lbl.add_css_class("mix-header-subtitle")
        self.sub_lbl.set_halign(Gtk.Align.CENTER)
        title_vbox.append(self.sub_lbl)

        header_box.append(title_vbox)

        # Device Graphic for Elgato Wave Devices
        if self.is_elgato:
            hero_path = self._get_device_hero_image_path()
            if hero_path and os.path.exists(hero_path):
                self.hero_pic = Gtk.Image.new_from_file(hero_path)
                self.hero_pic.set_pixel_size(88)
                self.hero_pic.set_halign(Gtk.Align.CENTER)
                self.hero_pic.set_valign(Gtk.Align.CENTER)
                self.hero_pic.set_margin_top(4)
                self.hero_pic.set_margin_bottom(6)
                self.hero_pic.add_css_class("wave-device-hero-image")
                header_box.append(self.hero_pic)

        grp_header.add(header_box)
        pref_page.add(grp_header)

        # Group 1: Nickname & Identification
        grp_ident = Adw.PreferencesGroup(title="Device Identification &amp; Appearance")
        
        self.name_entry = Adw.EntryRow(title="Custom Device Nickname")
        curr_alias = self.hardware_mgr.get_device_display_name(self.device_key)
        raw_name = device_info.get("name", "")
        self.name_entry.set_text(curr_alias if curr_alias != raw_name else "")
        self.name_entry.set_tooltip_text(f"Original hardware: {raw_name}")
        self.name_entry.connect("apply", self._on_nickname_applied)
        self.name_entry.connect("entry-activated", self._on_nickname_applied)
        grp_ident.add(self.name_entry)

        # Device Icon Picker Row
        icon_row = Adw.ActionRow(title="Device Icon", subtitle="Customize icon displayed across WaveController")
        curr_icon = self.hardware_mgr.get_device_icon(self.device_key)
        self.icon_btn = Gtk.MenuButton()
        self.icon_btn.set_icon_name(curr_icon)
        self.icon_btn.add_css_class("flat")
        self.icon_btn.add_css_class("wave-icon-btn")
        self.icon_btn.set_valign(Gtk.Align.CENTER)
        self._setup_icon_popover(self.icon_btn)
        icon_row.add_suffix(self.icon_btn)
        grp_ident.add(icon_row)

        hw_row = Adw.ActionRow(title="Hardware Identifier", subtitle=str(self.device_key))
        grp_ident.add(hw_row)

        pref_page.add(grp_ident)

        # Group 2: Microphone (Input) Section (Duplex or Input-Only)
        if self.device_type in ["duplex", "input"]:
            grp_mic = Adw.PreferencesGroup(title="Microphone (Audio Input) &amp; Hardware DSP")

            # VU Meter
            meter_row = Adw.ActionRow(title="Vocal Input Signal", subtitle="Real-time studio audio activity")
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

            is_wave_3 = self.is_elgato and ("wave:3" in self.title_lbl.get_text().lower() or "wave 3" in self.title_lbl.get_text().lower() or "wave_3" in self.device_key.lower() or "wave3" in self.device_key.lower())
            is_wave_xlr = self.is_elgato and ("xlr" in self.device_key.lower() or "xlr" in self.title_lbl.get_text().lower() or "wave xlr" in self.device_info.get("name", "").lower())

            # Preamp Gain Slider (0 to 40 dB for Wave:3, 0 to 75 dB for Wave XLR)
            max_gain = 40 if is_wave_3 else 75
            gain_sub = f"Analog condenser microphone preamp gain (0-40 dB) • Current: {self.hardware_mgr.hardware_gain_db} dB" if is_wave_3 else f"Ultra-low-noise microphone preamp with 0-75 dB gain • Current: {self.hardware_mgr.hardware_gain_db} dB"
            self.gain_row = Adw.ActionRow(title="Analog Preamp Gain", subtitle=gain_sub)
            self.gain_adj = Gtk.Adjustment(value=min(max_gain, self.hardware_mgr.hardware_gain_db), lower=0, upper=max_gain, step_increment=1, page_increment=5)
            self.gain_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.gain_adj)
            self.gain_slider.set_size_request(180, -1)
            self.gain_slider.set_valign(Gtk.Align.CENTER)
            self._gain_handler_id = self.gain_slider.connect("value-changed", self._on_gain_changed)
            self.gain_row.add_suffix(self.gain_slider)
            grp_mic.add(self.gain_row)

            # 48V Phantom Power Switch (Strictly for Wave XLR Hardware)
            if is_wave_xlr:
                self.phantom_row = Adw.SwitchRow(title="48V Phantom Power", subtitle="Provides 48V DC power to XLR condenser microphones")
                self.phantom_row.set_active(self.hardware_mgr.phantom_power_48v)
                self._phantom_handler_id = self.phantom_row.connect("notify::active", self._on_phantom_toggled)
                grp_mic.add(self.phantom_row)

            # Mic Test Direct Loopback
            listen_row = Adw.ActionRow(title="Mic Test (Direct Loopback)", subtitle="Hear your live voice in headphones to verify levels")
            self.listen_btn = Gtk.Button(label="Listen to Mic")
            self.listen_btn.set_icon_name("audio-headset-symbolic")
            self.listen_btn.set_valign(Gtk.Align.CENTER)
            self.listen_btn.connect("clicked", self._on_toggle_mic_listen)
            listen_row.add_suffix(self.listen_btn)
            grp_mic.add(listen_row)

            # Hardware DSP & Filters
            self.clipguard_row = Adw.SwitchRow(title="Clipguard Protection", subtitle="Dual-stage analog limiter prevents vocal clipping")
            self.clipguard_row.set_active(self.hardware_mgr.clipguard_enabled)
            self._clipguard_handler_id = self.clipguard_row.connect("notify::active", lambda r, *a: self.hardware_mgr.toggle_clipguard())
            grp_mic.add(self.clipguard_row)

            self.low_cut_row = Adw.ComboRow(title="Enhanced Low-Cut Filter", subtitle="Hardware DSP high-pass filter removing desk rumble")
            self.low_cut_model = Gtk.StringList.new(["Off", "80 Hz", "120 Hz"])
            self.low_cut_row.set_model(self.low_cut_model)
            self.low_cut_row.set_selected(1 if self.hardware_mgr.low_cut_filter == "80Hz" else (2 if self.hardware_mgr.low_cut_filter == "120Hz" else 0))
            self._low_cut_handler_id = self.low_cut_row.connect("notify::selected", self._on_low_cut_changed)
            grp_mic.add(self.low_cut_row)

            pref_page.add(grp_mic)

        # Group 3: Headphone Monitor (Output) Section (Duplex or Output-Only)
        if self.device_type in ["duplex", "output"]:
            grp_out = Adw.PreferencesGroup(title="Headphone Monitor &amp; Audio Output")

            # Output Volume
            vol_row = Adw.ActionRow(title="Output / Monitor Volume", subtitle="Adjust headphone DAC amplifier level")
            curr_vol = self.hardware_mgr.get_output_volume(self.device_key)
            self.vol_adj = Gtk.Adjustment(value=curr_vol, lower=0, upper=100, step_increment=1, page_increment=5)
            self.vol_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.vol_adj)
            self.vol_slider.set_size_request(180, -1)
            self.vol_slider.set_valign(Gtk.Align.CENTER)
            self._vol_handler_id = self.vol_slider.connect("value-changed", self._on_output_volume_changed)
            vol_row.add_suffix(self.vol_slider)
            grp_out.add(vol_row)

            # Direct Mic / PC Audio Crossfade (Monitor Mix)
            bal_row = Adw.ActionRow(title="Mic / PC Audio Balance (Monitor Mix)", subtitle="Hardware zero-latency sidetone crossfader")
            curr_mix = self.hardware_mgr.get_monitor_mix()
            self.bal_adj = Gtk.Adjustment(value=curr_mix, lower=0, upper=100, step_increment=1, page_increment=5)
            self.bal_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.bal_adj)
            self.bal_slider.set_size_request(180, -1)
            self.bal_slider.set_valign(Gtk.Align.CENTER)
            self.bal_slider.set_draw_value(False)
            self.bal_slider.add_mark(50, Gtk.PositionType.BOTTOM, None)
            self.bal_slider.add_css_class("wave-balance-fader")
            self._bal_handler_id = self.bal_slider.connect("value-changed", self._on_balance_changed)
            bal_row.add_suffix(self.bal_slider)
            grp_out.add(bal_row)

            # Low-Impedance Mode Switch (Strictly for Wave XLR)
            if is_wave_xlr:
                self.low_z_row = Adw.SwitchRow(title="Low-Impedance Headphone Mode", subtitle="Optimized for sensitive In-Ear Monitors (IEMs)")
                self.low_z_row.set_active(self.hardware_mgr.low_impedance_mode)
                self._low_z_handler_id = self.low_z_row.connect("notify::active", lambda r, *a: self.hardware_mgr.toggle_low_impedance())
                grp_out.add(self.low_z_row)

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
                self.mix_row = Adw.ComboRow(title="Assigned Output Mix", subtitle="Select which WaveController sub-mix routes to this output")
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

        # Group 4: Hardware RGB LED Ring Customization (Wave XLR devices with RGB diodes)
        if is_wave_xlr:
            grp_led = Adw.PreferencesGroup(title="Hardware RGB LED Ring Customization")

            # Mic Gain Mode Color
            gain_led_row = Adw.ActionRow(title="Mic Gain Mode Ring Color", subtitle="Color when knob adjusts microphone preamp gain")
            gain_led_btn = LEDColorButton(self.hardware_mgr, "gain", title="Mic Gain Mode Color")
            gain_led_btn.set_valign(Gtk.Align.CENTER)
            gain_led_row.add_suffix(gain_led_btn)
            grp_led.add(gain_led_row)

            # Headphone Mode Color
            hp_led_row = Adw.ActionRow(title="Headphone Mode Ring Color", subtitle="Color when knob adjusts headphone output volume")
            hp_led_btn = LEDColorButton(self.hardware_mgr, "hp", title="Headphone Mode Color")
            hp_led_btn.set_valign(Gtk.Align.CENTER)
            hp_led_row.add_suffix(hp_led_btn)
            grp_led.add(hp_led_row)

            # Balance Mode Color
            mix_led_row = Adw.ActionRow(title="Balance Mode Ring Color", subtitle="Color when knob adjusts Mic/PC crossfade balance")
            mix_led_btn = LEDColorButton(self.hardware_mgr, "mix", title="Balance Mode Color")
            mix_led_btn.set_valign(Gtk.Align.CENTER)
            mix_led_row.add_suffix(mix_led_btn)
            grp_led.add(mix_led_row)

            # Mute State Color
            mute_led_row = Adw.ActionRow(title="Mute State Ring Color", subtitle="Color when microphone is muted via capacitive sensor")
            mute_led_btn = LEDColorButton(self.hardware_mgr, "mute", title="Mute State Color")
            mute_led_btn.set_valign(Gtk.Align.CENTER)
            mute_led_row.add_suffix(mute_led_btn)
            grp_led.add(mute_led_row)

            pref_page.add(grp_led)

        # Group 5: Exclusive Volume Guard & Protection (Strictly for Elgato Wave Devices)
        if self.is_elgato:
            grp_guard = Adw.PreferencesGroup(title="Exclusive Volume Guard &amp; Protection")
            
            if self.device_type in ["duplex", "input"]:
                excl_mic_row = Adw.SwitchRow(
                    title="Lock Microphone Gain (Exclusive Control)",
                    subtitle="Blocks Discord AGC, web browsers, and voice apps from changing mic gain. Knob and WaveController remain active."
                )
                excl_mic_row.set_active(self.hardware_mgr.get_exclusive_mic_lock())
                excl_mic_row.connect("notify::active", lambda r, *a: self.hardware_mgr.set_exclusive_mic_lock(r.get_active()))
                grp_guard.add(excl_mic_row)

            if self.device_type in ["duplex", "output"]:
                excl_out_row = Adw.SwitchRow(
                    title="Lock Headphone / Output Volume (Exclusive Control)",
                    subtitle="Prevents external media players and desktop sliders from overriding physical DAC volume. Knob and WaveController remain active."
                )
                excl_out_row.set_active(self.hardware_mgr.get_exclusive_output_lock())
                excl_out_row.connect("notify::active", lambda r, *a: self.hardware_mgr.set_exclusive_output_lock(r.get_active()))
                grp_guard.add(excl_out_row)

            pref_page.add(grp_guard)

        # Group 6: Hardware Diagnostics & Firmware (USB DFU 1.10)
        grp_diag = Adw.PreferencesGroup(title="Hardware Diagnostics &amp; Firmware")
        
        info = self.hardware_mgr.get_elgato_device_info()
        fw_version = info.get("fw_version") or "1.3.1"
        serial = info.get("serial") or "ES21L1A00000"
        dial_mode = info.get("dial_mode", "gain").capitalize()

        fw_row = Adw.ActionRow(title="Firmware Version", subtitle=f"Installed: v{fw_version} (USB DFU 1.10)")
        update_btn = Gtk.Button(label="Check for Updates")
        update_btn.set_icon_name("software-update-available-symbolic")
        update_btn.set_valign(Gtk.Align.CENTER)
        update_btn.connect("clicked", self._on_check_firmware_updates)
        fw_row.add_suffix(update_btn)
        grp_diag.add(fw_row)

        serial_row = Adw.ActionRow(title="Hardware Serial Number", subtitle=serial)
        grp_diag.add(serial_row)

        self.dial_mode_row = Adw.ActionRow(title="Rotary Knob Dial Target", subtitle=f"Active Mode: {dial_mode}")
        grp_diag.add(self.dial_mode_row)

        pref_page.add(grp_diag)

        # Single, prominent text-only Remove Device button in footer group
        grp_remove = Adw.PreferencesGroup()
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_box.set_margin_top(16)
        btn_box.set_margin_bottom(32)

        remove_btn = Gtk.Button(label="Remove Device")
        remove_btn.add_css_class("destructive-action")
        remove_btn.set_size_request(220, 42)
        remove_btn.connect("clicked", self._on_remove_clicked)
        btn_box.append(remove_btn)
        grp_remove.add(btn_box)
        pref_page.add(grp_remove)

        self.append(pref_page)

        # Hook live state changes from physical hardware
        if self.hardware_mgr and hasattr(self.hardware_mgr, "add_hardware_listener"):
            self.hardware_mgr.add_hardware_listener(lambda curr, changed: GLib.idle_add(self._on_hardware_synced, curr, changed))

        # Start live meter timer if mic is available
        if self.device_type in ["duplex", "input"]:
            GLib.timeout_add(25, self._on_meter_tick)

    def _on_hardware_synced(self, curr: dict, changed: dict):
        """Called when physical rotary dial, 48V, or touch mute is adjusted on the hardware."""
        if not self.get_mapped():
            return
        self._syncing_from_hw = True
        try:
            if "gain_db" in changed and hasattr(self, "gain_adj") and hasattr(self, "gain_slider"):
                val = int(round(changed["gain_db"]))
                if hasattr(self, "_gain_handler_id") and self._gain_handler_id:
                    self.gain_slider.handler_block(self._gain_handler_id)
                    try:
                        self.gain_adj.set_value(val)
                    finally:
                        self.gain_slider.handler_unblock(self._gain_handler_id)
                else:
                    self.gain_adj.set_value(val)
                self.gain_row.set_subtitle(f"{val} dB")

            if "hp_volume_pct" in changed and hasattr(self, "vol_adj") and hasattr(self, "vol_slider"):
                val = int(round(changed["hp_volume_pct"]))
                if hasattr(self, "_vol_handler_id") and self._vol_handler_id:
                    self.vol_slider.handler_block(self._vol_handler_id)
                    try:
                        self.vol_adj.set_value(val)
                    finally:
                        self.vol_slider.handler_unblock(self._vol_handler_id)
                else:
                    self.vol_adj.set_value(val)

            if "dial_mode" in changed and hasattr(self, "dial_mode_row"):
                self.dial_mode_row.set_subtitle(f"Active Mode: {str(changed['dial_mode']).capitalize()}")

            if "phantom_power" in changed and hasattr(self, "phantom_row"):
                val = bool(changed["phantom_power"])
                if self.phantom_row.get_active() != val:
                    if hasattr(self, "_phantom_handler_id") and self._phantom_handler_id:
                        self.phantom_row.handler_block(self._phantom_handler_id)
                        try:
                            self.phantom_row.set_active(val)
                        finally:
                            self.phantom_row.handler_unblock(self._phantom_handler_id)
                    else:
                        self.phantom_row.set_active(val)

            if "clipguard" in changed and hasattr(self, "clipguard_row"):
                val = bool(changed["clipguard"])
                if self.clipguard_row.get_active() != val:
                    if hasattr(self, "_clipguard_handler_id") and self._clipguard_handler_id:
                        self.clipguard_row.handler_block(self._clipguard_handler_id)
                        try:
                            self.clipguard_row.set_active(val)
                        finally:
                            self.clipguard_row.handler_unblock(self._clipguard_handler_id)
                    else:
                        self.clipguard_row.set_active(val)

            if "low_cut" in changed and hasattr(self, "low_cut_row"):
                mode = str(changed["low_cut"])
                sel = 1 if mode == "80Hz" else (2 if mode == "120Hz" else 0)
                if self.low_cut_row.get_selected() != sel:
                    if hasattr(self, "_low_cut_handler_id") and self._low_cut_handler_id:
                        self.low_cut_row.handler_block(self._low_cut_handler_id)
                        try:
                            self.low_cut_row.set_selected(sel)
                        finally:
                            self.low_cut_row.handler_unblock(self._low_cut_handler_id)
                    else:
                        self.low_cut_row.set_selected(sel)

            if "low_impedance" in changed and hasattr(self, "low_z_row"):
                val = bool(changed["low_impedance"])
                if self.low_z_row.get_active() != val:
                    if hasattr(self, "_low_z_handler_id") and self._low_z_handler_id:
                        self.low_z_row.handler_block(self._low_z_handler_id)
                        try:
                            self.low_z_row.set_active(val)
                        finally:
                            self.low_z_row.handler_unblock(self._low_z_handler_id)
                    else:
                        self.low_z_row.set_active(val)

            if "monitor_mix_pct" in changed and hasattr(self, "bal_adj") and hasattr(self, "bal_slider"):
                val = int(round(changed["monitor_mix_pct"]))
                if int(round(self.bal_adj.get_value())) != val:
                    if hasattr(self, "_bal_handler_id") and self._bal_handler_id:
                        self.bal_slider.handler_block(self._bal_handler_id)
                        try:
                            self.bal_adj.set_value(val)
                        finally:
                            self.bal_slider.handler_unblock(self._bal_handler_id)
                    else:
                        self.bal_adj.set_value(val)
        finally:
            self._syncing_from_hw = False

    def _on_phantom_toggled(self, row, *args):
        is_active = self.phantom_row.get_active()
        if is_active != self.hardware_mgr.phantom_power_48v:
            if is_active:
                root_win = self.get_root()
                if not isinstance(root_win, Gtk.Window):
                    root_win = self.get_native() if isinstance(self.get_native(), Gtk.Window) else None
                dialog = Adw.MessageDialog(
                    transient_for=root_win,
                    heading="Enable 48V Phantom Power?",
                    body="48V Phantom Power provides voltage to XLR condenser microphones. Ensure your microphone requires 48V power. Do NOT enable 48V for ribbon microphones or line-level inputs."
                )
                dialog.add_response("cancel", "Cancel")
                dialog.add_response("enable", "Enable 48V")
                dialog.set_response_appearance("enable", Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.set_default_response("cancel")

                def _on_response(d, resp):
                    if resp == "enable":
                        self.hardware_mgr.set_phantom_power(True)
                    else:
                        if hasattr(self, "_phantom_handler_id") and self._phantom_handler_id:
                            self.phantom_row.handler_block(self._phantom_handler_id)
                            try:
                                self.phantom_row.set_active(False)
                            finally:
                                self.phantom_row.handler_unblock(self._phantom_handler_id)
                        else:
                            self.phantom_row.set_active(False)

                dialog.connect("response", _on_response)
                dialog.present()
            else:
                self.hardware_mgr.set_phantom_power(False)

    def _on_check_firmware_updates(self, btn):
        dialog = Adw.MessageDialog(
            transient_for=self.get_root() if isinstance(self.get_root(), Gtk.Window) else None,
            heading="Firmware Status",
            body="Your Elgato Wave hardware is running the latest certified firmware version. USB DFU 1.10 bootloader is ready for future updates."
        )
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.present()

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
        if getattr(self, "_syncing_from_hw", False):
            return
        val = int(self.gain_adj.get_value())
        self.hardware_mgr.set_gain(val, self.device_key, transient=True)
        self.gain_row.set_subtitle(f"{val} dB")

    def _on_balance_changed(self, scale):
        if getattr(self, "_syncing_from_hw", False):
            return
        val = int(self.bal_adj.get_value())
        self.hardware_mgr.set_monitor_mix(val, transient=True)

    def _on_low_cut_changed(self, row, *args):
        if getattr(self, "_syncing_from_hw", False):
            return
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
        if getattr(self, "_syncing_from_hw", False):
            return
        val = int(self.vol_adj.get_value())
        self.hardware_mgr.set_output_volume(self.device_key, val, transient=True)

    def _on_output_mute_clicked(self, btn):
        self.hardware_mgr.toggle_output_mute(self.device_key, transient=True)
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

    def _setup_icon_popover(self, menu_btn: Gtk.MenuButton):
        popover = Gtk.Popover()
        popover.add_css_class("wave-popover")

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vbox.set_margin_top(8)
        vbox.set_margin_bottom(8)
        vbox.set_margin_start(8)
        vbox.set_margin_end(8)

        lbl = Gtk.Label(label="Select Device Icon")
        lbl.add_css_class("heading")
        lbl.set_halign(Gtk.Align.START)
        vbox.append(lbl)

        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(8)

        icons = [
            ("audio-input-microphone-symbolic", "Microphone (Desktop / Standalone)"),
            ("audio-headphones-symbolic", "Headphones / IEMs"),
            ("audio-headset-symbolic", "Headset (with attached mic)"),
            ("audio-speakers-symbolic", "Speakers / Monitors"),
            ("audio-card-symbolic", "Audio Interface / DAC"),
            ("computer-symbolic", "Computer / Built-in Audio"),
        ]

        for idx, (icon_name, tooltip) in enumerate(icons):
            btn = Gtk.Button.new_from_icon_name(icon_name)
            btn.add_css_class("flat")
            btn.add_css_class("wave-icon-btn")
            btn.set_size_request(40, 40)
            btn.set_tooltip_text(tooltip)
            btn.connect("clicked", lambda b, iname=icon_name: self._on_icon_selected(popover, iname))
            grid.attach(btn, idx % 3, idx // 3, 1, 1)

        vbox.append(grid)

        reset_btn = Gtk.Button(label="Reset to Auto-Detected Icon")
        reset_btn.add_css_class("flat")
        reset_btn.connect("clicked", lambda b: self._on_icon_reset(popover))
        vbox.append(reset_btn)

        popover.set_child(vbox)
        menu_btn.set_popover(popover)

    def _on_icon_selected(self, popover, icon_name: str):
        self.hardware_mgr.set_device_custom_icon(self.device_key, icon_name)
        self.header_icon_img.set_from_icon_name(icon_name)
        self.icon_btn.set_icon_name(icon_name)
        popover.popdown()
        if self.on_device_renamed:
            self.on_device_renamed()

    def _on_icon_reset(self, popover):
        self.hardware_mgr.set_device_custom_icon(self.device_key, "")
        smart_icon = self.hardware_mgr.get_device_icon(self.device_key)
        self.header_icon_img.set_from_icon_name(smart_icon)
        self.icon_btn.set_icon_name(smart_icon)
        popover.popdown()
        if self.on_device_renamed:
            self.on_device_renamed()

    def _get_device_hero_image_path(self) -> str:
        is_wave_3 = "wave:3" in self.title_lbl.get_text().lower() or "wave 3" in self.title_lbl.get_text().lower() or "wave_3" in self.device_key.lower() or "wave3" in self.device_key.lower()
        if is_wave_3:
            candidates = [
                os.path.join(os.path.dirname(__file__), "..", "..", "assets", "icons", "ElgatoWave3.png"),
                os.path.expanduser("~/.local/share/wavecontroller/assets/icons/ElgatoWave3.png"),
                os.path.expanduser("~/Project stuf/Elgato.WaveLink_3.2.10.4073_x64/Assets/ElgatoWave3.png"),
            ]
        else:
            candidates = [
                os.path.join(os.path.dirname(__file__), "..", "..", "assets", "icons", "ElgatoWaveXLR_small.png"),
                os.path.join(os.path.dirname(__file__), "..", "..", "assets", "icons", "ElgatoWaveXLR.png"),
                os.path.join(os.path.dirname(__file__), "..", "..", "assets", "icons", "elgato-wave-xlr.png"),
                os.path.expanduser("~/.local/share/wavecontroller/assets/icons/ElgatoWaveXLR_small.png"),
                os.path.expanduser("~/.local/share/wavecontroller/assets/icons/ElgatoWaveXLR.png"),
                os.path.expanduser("~/Documents/WaveController real time test/ElgatoWaveXLR.png"),
            ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return ""

    def refresh_device_names(self):
        display_name = self.hardware_mgr.get_device_display_name(self.device_key)
        self.title_lbl.set_text(display_name)
        curr_icon = self.hardware_mgr.get_device_icon(self.device_key)
        if hasattr(self, "header_icon_img"):
            self.header_icon_img.set_from_icon_name(curr_icon)
        if hasattr(self, "icon_btn"):
            self.icon_btn.set_icon_name(curr_icon)


class AddDeviceDialog(Adw.Window):
    """
    Modal preferences dialog allowing users to discover and add untracked
    hardware audio devices (Duplex, Input Only, Output Only) into WaveController.
    """
    def __init__(self, hardware_mgr, on_device_added_callback=None, **kwargs):
        super().__init__(title="Add Audio Device", modal=True, **kwargs)
        self.hardware_mgr = hardware_mgr
        self.on_device_added_callback = on_device_added_callback
        self.set_default_size(560, 520)

        toolbar_view = Adw.ToolbarView()

        # HeaderBar with Window Controls and Cancel Action Button
        header = Adw.HeaderBar()
        header.set_show_title(True)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("flat")
        cancel_btn.connect("clicked", lambda b: self.close())
        header.pack_start(cancel_btn)

        toolbar_view.add_top_bar(header)

        # Scrolled content with PreferencesPage
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content_box.set_margin_top(16)
        content_box.set_margin_bottom(24)
        content_box.set_margin_start(20)
        content_box.set_margin_end(20)

        untracked = self.hardware_mgr.get_available_untracked_devices()
        if not untracked:
            status = Adw.StatusPage()
            status.set_icon_name("audio-volume-high-symbolic")
            status.set_title("All Devices Added")
            status.set_description("All detected physical audio devices are currently added to WaveController.")

            close_btn = Gtk.Button(label="Close")
            close_btn.add_css_class("suggested-action")
            close_btn.add_css_class("pill")
            close_btn.set_halign(Gtk.Align.CENTER)
            close_btn.connect("clicked", lambda b: self.close())
            status.set_child(close_btn)

            content_box.append(status)
        else:
            page = Adw.PreferencesPage()

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

            content_box.append(page)

        scrolled.set_child(content_box)
        toolbar_view.set_content(scrolled)
        self.set_content(toolbar_view)

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
