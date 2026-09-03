import os
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib
import math
from ..engine.config_manager import config_manager
from ..utils.gtk_helpers import blocked_handler

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
    def __init__(self, device_info: dict, hardware_mgr, peak_monitor, pipewire_mgr=None, on_device_renamed=None, on_device_removed=None, on_make_default=None, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, **kwargs)
        self.device_info = device_info
        self.hardware_mgr = hardware_mgr
        self.peak_monitor = peak_monitor
        self.pipewire_mgr = pipewire_mgr
        self.on_device_renamed = on_device_renamed
        self.on_device_removed = on_device_removed
        self.on_make_default = on_make_default

        self.device_key = device_info.get("device_key", "")
        self.device_type = device_info.get("type", "duplex")
        dev_name_low = str(device_info.get("name", "")).lower()
        self.is_elgato = device_info.get("is_elgato", False) or any(w in dev_name_low for w in ("elgato", "wave xlr", "wave xlr mk2", "wave:3", "wave:1", "wave neo")) or "0fd9" in self.device_key.lower() or "00b6" in self.device_key.lower()
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

        self.pref_page = pref_page
        self.grp_default = Adw.PreferencesGroup(title="Primary Default Device")
        self.pref_page.add(self.grp_default)
        self._default_action_row = None
        self._build_default_device_section()

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

            if self.is_elgato:
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
            else:
                # Generic Device Input Capture Level (0 to 100%)
                curr_inp_vol = self.hardware_mgr.get_output_volume(self.device_key) # Reuses wpctl query
                self.gain_row = Adw.ActionRow(title="Input Capture Level", subtitle=f"{curr_inp_vol}%")
                self.gain_adj = Gtk.Adjustment(value=curr_inp_vol, lower=0, upper=100, step_increment=1, page_increment=5)
                self.gain_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.gain_adj)
                self.gain_slider.set_size_request(180, -1)
                self.gain_slider.set_valign(Gtk.Align.CENTER)
                self._gain_handler_id = self.gain_slider.connect("value-changed", self._on_gain_changed)
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

            pref_page.add(grp_mic)

        # Group 3: Headphone Monitor (Output) Section (Duplex or Output-Only)
        if self.device_type in ["duplex", "output"]:
            grp_out = Adw.PreferencesGroup(title="Headphone Monitor &amp; Audio Output")

            # Output Volume
            vol_row = Adw.ActionRow(title="Output / Monitor Volume", subtitle="Adjust sound card / DAC amplifier level")
            curr_vol = self.hardware_mgr.get_output_volume(self.device_key)
            self.vol_adj = Gtk.Adjustment(value=curr_vol, lower=0, upper=100, step_increment=1, page_increment=5)
            self.vol_slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.vol_adj)
            self.vol_slider.set_size_request(180, -1)
            self.vol_slider.set_valign(Gtk.Align.CENTER)
            self._vol_handler_id = self.vol_slider.connect("value-changed", self._on_output_volume_changed)
            vol_row.add_suffix(self.vol_slider)
            grp_out.add(vol_row)

            if self.is_elgato:
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

        # Group 6: Dynamic Hardware Diagnostics & Specifications
        grp_diag = Adw.PreferencesGroup(title="Hardware Diagnostics &amp; Specifications")
        
        diag = self.hardware_mgr.get_device_diagnostics(self.device_key) if hasattr(self.hardware_mgr, "get_device_diagnostics") else {}
        cat = diag.get("category", "generic_usb")

        if cat == "elgato":
            fw_version = diag.get("firmware_version", "v3.7.3 (USB DFU 1.10)")
            serial = diag.get("serial", "DS16M2A01160")
            dial_mode = diag.get("dial_mode", "Gain")
            vendor = diag.get("vendor_info", "0x0FD9 (Elgato Systems GmbH)")

            fw_row = Adw.ActionRow(title="Firmware Version", subtitle=f"Installed: {fw_version}")
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

            usb_row = Adw.ActionRow(title="USB Interface &amp; Protocol", subtitle=f"{vendor} • {diag.get('bus_path', 'USB')}")
            grp_diag.add(usb_row)

        elif cat == "generic_usb":
            arch_row = Adw.ActionRow(title="Hardware Architecture", subtitle=diag.get("architecture", "USB Audio Class (UAC)"))
            grp_diag.add(arch_row)

            id_row = Adw.ActionRow(title="USB Hardware Identification", subtitle=f"Vendor: {diag.get('vendor_info')} • Product: {diag.get('product_info')}")
            grp_diag.add(id_row)

            serial_row = Adw.ActionRow(title="Hardware Serial Number", subtitle=diag.get("serial", "Standard USB Audio Class (UAC)"))
            grp_diag.add(serial_row)

            bus_row = Adw.ActionRow(title="USB Bus Path &amp; Port", subtitle=diag.get("bus_path", "USB Audio Bus"))
            grp_diag.add(bus_row)

            drv_row = Adw.ActionRow(title="Audio Subsystem &amp; Driver", subtitle=diag.get("driver_info", "Linux snd_usb_audio / PipeWire Module"))
            grp_diag.add(drv_row)

        else: # pci_audio
            arch_row = Adw.ActionRow(title="Hardware Architecture", subtitle=diag.get("architecture", "PCI Express High Definition Audio (HDA)"))
            grp_diag.add(arch_row)

            chipset_row = Adw.ActionRow(title="Audio Chipset &amp; Controller", subtitle=diag.get("chipset", "HD-Audio Generic"))
            grp_diag.add(chipset_row)

            pci_row = Adw.ActionRow(title="PCI Express Bus Location", subtitle=diag.get("bus_path", "PCI Bus Address"))
            grp_diag.add(pci_row)

            vend_row = Adw.ActionRow(title="Controller Vendor", subtitle=diag.get("vendor_info", "Integrated Motherboard Audio"))
            grp_diag.add(vend_row)

            drv_row = Adw.ActionRow(title="Audio Subsystem &amp; Driver", subtitle=diag.get("driver_info", "Linux snd_hda_intel"))
            grp_diag.add(drv_row)

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
        self._hw_listener_cb = None
        if self.hardware_mgr and hasattr(self.hardware_mgr, "add_hardware_listener"):
            self._hw_listener_cb = lambda curr, changed: GLib.idle_add(self._on_hardware_synced, curr, changed)
            self.hardware_mgr.add_hardware_listener(self._hw_listener_cb)

        # Start live meter timer if mic is available
        if self.device_type in ["duplex", "input"]:
            GLib.timeout_add(25, self._on_meter_tick)

    def cleanup(self):
        """Unregisters the hardware listener; call before dropping the last reference to this view."""
        if self._hw_listener_cb and self.hardware_mgr and hasattr(self.hardware_mgr, "remove_hardware_listener"):
            self.hardware_mgr.remove_hardware_listener(self._hw_listener_cb)
            self._hw_listener_cb = None

    def _on_hardware_synced(self, curr: dict, changed: dict):
        """Called when physical rotary dial, 48V, or touch mute is adjusted on the hardware."""
        if not self.get_mapped():
            return
        self._syncing_from_hw = True
        try:
            if "hp_volume_pct" in changed and hasattr(self, "vol_adj") and hasattr(self, "vol_slider"):
                val = int(round(changed["hp_volume_pct"]))
                with blocked_handler(self.vol_slider, getattr(self, "_vol_handler_id", None)):
                    self.vol_adj.set_value(val)

            if "dial_mode" in changed and hasattr(self, "dial_mode_row"):
                self.dial_mode_row.set_subtitle(f"Active Mode: {str(changed['dial_mode']).capitalize()}")

            if "phantom_power" in changed and hasattr(self, "phantom_row"):
                val = bool(changed["phantom_power"])
                if self.phantom_row.get_active() != val:
                    with blocked_handler(self.phantom_row, getattr(self, "_phantom_handler_id", None)):
                        self.phantom_row.set_active(val)

            if "clipguard" in changed and hasattr(self, "clipguard_row"):
                val = bool(changed["clipguard"])
                if self.clipguard_row.get_active() != val:
                    with blocked_handler(self.clipguard_row, getattr(self, "_clipguard_handler_id", None)):
                        self.clipguard_row.set_active(val)

            if "low_cut" in changed and hasattr(self, "low_cut_row"):
                mode = str(changed["low_cut"])
                sel = 1 if mode == "80Hz" else (2 if mode == "120Hz" else 0)
                if self.low_cut_row.get_selected() != sel:
                    with blocked_handler(self.low_cut_row, getattr(self, "_low_cut_handler_id", None)):
                        self.low_cut_row.set_selected(sel)

            if "low_impedance" in changed and hasattr(self, "low_z_row"):
                val = bool(changed["low_impedance"])
                if self.low_z_row.get_active() != val:
                    with blocked_handler(self.low_z_row, getattr(self, "_low_z_handler_id", None)):
                        self.low_z_row.set_active(val)

            if "gain_db" in changed and hasattr(self, "gain_adj") and hasattr(self, "gain_slider"):
                val = int(round(changed["gain_db"]))
                if int(round(self.gain_adj.get_value())) != val:
                    with blocked_handler(self.gain_slider, getattr(self, "_gain_handler_id", None)):
                        self.gain_adj.set_value(val)
                    if hasattr(self, "gain_row"):
                        self.gain_row.set_subtitle(f"{val}%")

            if "monitor_mix_pct" in changed and hasattr(self, "bal_adj") and hasattr(self, "bal_slider"):
                val = int(round(changed["monitor_mix_pct"]))
                if int(round(self.bal_adj.get_value())) != val:
                    with blocked_handler(self.bal_slider, getattr(self, "_bal_handler_id", None)):
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
                        with blocked_handler(self.phantom_row, getattr(self, "_phantom_handler_id", None)):
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

    def _build_default_device_section(self):
        """Constructs or refreshes the Primary Default Device management group."""
        if not hasattr(self, "grp_default") or not self.grp_default:
            return

        if hasattr(self, "_default_action_row") and self._default_action_row:
            try:
                self.grp_default.remove(self._default_action_row)
            except Exception:
                pass
            self._default_action_row = None

        is_default = self.hardware_mgr.is_default_device(self.device_key) if hasattr(self.hardware_mgr, "is_default_device") else False
        has_default_in_system = self.hardware_mgr.has_default_device() if hasattr(self.hardware_mgr, "has_default_device") else False

        if is_default:
            self.grp_default.set_visible(True)
            self._default_action_row = Adw.ActionRow(
                title="Current Primary Default Device",
                subtitle="Assigned as default for dedicated Microphone channel and Personal Mix output"
            )
            active_badge = Gtk.Label(label="Active Default")
            active_badge.add_css_class("device-badge")
            active_badge.add_css_class("online")
            active_badge.set_valign(Gtk.Align.CENTER)
            self._default_action_row.add_suffix(active_badge)
            self.grp_default.add(self._default_action_row)
        elif not has_default_in_system:
            # Only appear when there is NO default device in the system (e.g. default was deleted, and modal was cancelled)
            self.grp_default.set_visible(True)
            self._default_action_row = Adw.ActionRow(
                title="Set as Default Device",
                subtitle="Assign this device as the default for dedicated Microphone channel and Personal Mix output"
            )
            make_def_btn = Gtk.Button(label="Make Default")
            make_def_btn.add_css_class("suggested-action")
            make_def_btn.set_valign(Gtk.Align.CENTER)
            make_def_btn.connect("clicked", self._on_make_default_clicked)
            self._default_action_row.add_suffix(make_def_btn)
            self._default_action_row.set_activatable_widget(make_def_btn)
            self.grp_default.add(self._default_action_row)
        else:
            self.grp_default.set_visible(False)

    def _on_make_default_clicked(self, btn):
        if self.on_make_default:
            GLib.idle_add(lambda: self.on_make_default(self.device_key))

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
        if hasattr(self, "gain_adj"):
            val = int(self.gain_adj.get_value())
            self.hardware_mgr.set_gain(val, self.device_key, transient=True)
            if hasattr(self, "gain_row"):
                self.gain_row.set_subtitle(f"{val}%")

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
            self.pipewire_mgr.update_mix(mix["id"], target_device=self.device_key)
            self.hardware_mgr.set_device_assigned_mix(self.device_key, mix["id"])

    def _on_meter_tick(self) -> bool:
        if not self.get_mapped():
            return True

        # 1. If device is disconnected or offline, strictly zero!
        if not self.device_info.get("connected", True):
            self.meter_bar.set_fraction(0.0)
            self.db_label.set_text("-∞ dB")
            return True

        # 2. Query direct physical microphone capture for this specific device
        peak = self.peak_monitor.get_channel_peak(self.device_key)

        # Fallback to checking any assigned channel for this device
        if peak <= 0.0 and self.pipewire_mgr:
            for ch in getattr(self.pipewire_mgr, "channels", []):
                assigned = self.pipewire_mgr.get_assigned_apps(ch["id"]) if hasattr(self.pipewire_mgr, "get_assigned_apps") else []
                if self.device_key in assigned or self.device_info.get("name", "") in assigned or ch["id"] in self.device_key.lower():
                    p = self.peak_monitor.get_channel_peak(ch["id"])
                    if p > 0.0:
                        peak = p
                        break

        # Fallback to 'mic' for Elgato devices if device_key peak is unpopulated
        if self.is_elgato and peak <= 0.0:
            peak = self.peak_monitor.get_channel_peak("mic")

        # 3. Live Physical VU Display
        self.meter_bar.set_fraction(peak)
        if peak > 0.002:
            db = -54.0 + (peak * 54.0)
            self.db_label.set_text(f"{db:.1f} dB")
        else:
            self.db_label.set_text("-∞ dB")
        return True

    def _setup_icon_popover(self, menu_btn: Gtk.MenuButton):
        popover = Gtk.Popover()
        popover.set_autohide(True)
        popover.set_cascade_popdown(True)
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
        title_text = self.title_lbl.get_text().lower()
        key_text = self.device_key.lower()
        is_wave_3 = "wave:3" in title_text or "wave 3" in title_text or "wave_3" in key_text or "wave3" in key_text
        is_wave_xlr_mk2 = "wave xlr mk2" in title_text or "wave_xlr_mk2" in key_text or "mk2" in title_text or "mk2" in key_text or "00b6" in key_text
        if is_wave_3:
            candidates = [
                os.path.join(os.path.dirname(__file__), "..", "..", "assets", "icons", "ElgatoWave3.png"),
                os.path.expanduser("~/.local/share/wavecontroller/assets/icons/ElgatoWave3.png"),
                os.path.expanduser("~/Project stuf/Elgato.WaveLink_3.2.10.4073_x64/Assets/ElgatoWave3.png"),
            ]
        elif is_wave_xlr_mk2:
            candidates = [
                os.path.join(os.path.dirname(__file__), "..", "..", "assets", "icons", "ElgatoWaveXLRMK2_small.png"),
                os.path.join(os.path.dirname(__file__), "..", "..", "assets", "icons", "ElgatoWaveXLRMK2.png"),
                os.path.join(os.path.dirname(__file__), "..", "..", "assets", "icons", "elgato-wave-xlr-mk2.png"),
                os.path.expanduser("~/.local/share/wavecontroller/assets/icons/ElgatoWaveXLRMK2_small.png"),
                os.path.expanduser("~/.local/share/wavecontroller/assets/icons/ElgatoWaveXLRMK2.png"),
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

    def update_device_info(self, device_info: dict):
        """Updates connection state and live labels in-place without tearing down UI widgets."""
        self.device_info = device_info
        self.device_type = device_info.get("type", "duplex")
        is_conn = device_info.get("connected", True)
        status_text = "🟢 Connected" if is_conn else "🟡 Disconnected / Offline"
        if hasattr(self, "sub_lbl"):
            self.sub_lbl.set_text(status_text)

        display_name = self.hardware_mgr.get_device_display_name(self.device_key)
        if hasattr(self, "title_lbl"):
            self.title_lbl.set_text(display_name)

        self._build_default_device_section()


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


class SelectDefaultDeviceDialog(Adw.Window):
    """
    Modal preferences dialog prompted when the primary default device is removed,
    asking the user to select one of the remaining tracked devices as the new default.
    """
    def __init__(self, hardware_mgr, remaining_devices: list, on_selected_callback=None, on_cancel_callback=None, **kwargs):
        super().__init__(title="Select Default Audio Device", modal=True, **kwargs)
        self.hardware_mgr = hardware_mgr
        self.remaining_devices = remaining_devices or []
        self.on_selected_callback = on_selected_callback
        self.on_cancel_callback = on_cancel_callback
        self._selected_device_key = self.remaining_devices[0].get("device_key") if self.remaining_devices else None

        self.set_default_size(500, 420)

        toolbar_view = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_show_title(True)
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("flat")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        header.pack_start(cancel_btn)

        set_btn = Gtk.Button(label="Set as Default")
        set_btn.add_css_class("suggested-action")
        set_btn.connect("clicked", self._on_set_clicked)
        header.pack_end(set_btn)

        toolbar_view.add_top_bar(header)

        # Scrolled content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content_box.set_margin_top(20)
        content_box.set_margin_bottom(24)
        content_box.set_margin_start(24)
        content_box.set_margin_end(24)

        # Description label
        desc_lbl = Gtk.Label(
            label="The previous default audio device was removed.\nSelect a remaining device to use for your Microphone channel and Personal Mix:"
        )
        desc_lbl.set_wrap(True)
        desc_lbl.set_justify(Gtk.Justification.CENTER)
        desc_lbl.add_css_class("dim-label")
        desc_lbl.set_margin_bottom(8)
        content_box.append(desc_lbl)

        # List of remaining devices with radio check buttons
        pref_group = Adw.PreferencesGroup(title="Remaining Audio Devices")
        first_btn = None

        for idx, dev in enumerate(self.remaining_devices):
            row = Adw.ActionRow(title=dev.get("display_name", dev.get("name", "Audio Device")))
            dtype = dev.get("type", "duplex")
            badge = dev.get("badge", "In / Out" if dtype == "duplex" else ("Input" if dtype == "input" else "Output"))
            desc = dev.get("description") or dev.get("device_key", "")
            row.set_subtitle(f"[{badge}] • {desc}")
            row.set_icon_name(dev.get("icon", "audio-card-symbolic"))

            check = Gtk.CheckButton()
            if first_btn is None:
                first_btn = check
                check.set_active(True)
            else:
                check.set_group(first_btn)

            k = dev.get("device_key")
            check.connect("toggled", self._make_toggled_handler(k))
            row.add_prefix(check)
            row.set_activatable_widget(check)

            pref_group.add(row)

        content_box.append(pref_group)
        scrolled.set_child(content_box)
        toolbar_view.set_content(scrolled)
        self.set_content(toolbar_view)

    def _make_toggled_handler(self, device_key: str):
        def _handler(chk):
            if chk.get_active():
                self._selected_device_key = device_key
        return _handler

    def _on_set_clicked(self, btn):
        self.close()
        if self.on_selected_callback and self._selected_device_key:
            self.on_selected_callback(self._selected_device_key)

    def _on_cancel_clicked(self, btn):
        self.close()
        if self.on_cancel_callback:
            self.on_cancel_callback()


# Legacy Compatibility Aliases
InputDeviceSettingsView = UnifiedDeviceSettingsView
OutputDeviceSettingsView = UnifiedDeviceSettingsView
DeviceSettingsView = UnifiedDeviceSettingsView
