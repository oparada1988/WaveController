import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from ..engine.config_manager import config_manager

class SetupWizardDialog(Gtk.Window):
    """
    First-Time Setup Wizard (OOBE) Dialog for configuring primary microphone and monitor DAC on initial launch.
    """
    def __init__(self, parent_window=None, hardware_mgr=None, pipewire_mgr=None, on_complete_callback=None):
        top_parent = parent_window if isinstance(parent_window, Gtk.Window) else None
        super().__init__(
            transient_for=top_parent,
            modal=True,
            title="Welcome to WaveController",
            default_width=520,
            default_height=480,
            resizable=False
        )
        self.hardware_mgr = hardware_mgr
        self.pipewire_mgr = pipewire_mgr
        self.on_complete_callback = on_complete_callback

        self.add_css_class("wave-window")

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        main_box.set_margin_top(24)
        main_box.set_margin_bottom(24)
        main_box.set_margin_start(28)
        main_box.set_margin_end(28)

        # Header Icon & Title
        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header_box.set_halign(Gtk.Align.CENTER)

        icon_img = Gtk.Image.new_from_icon_name("audio-headphones-symbolic")
        icon_img.set_pixel_size(48)
        icon_img.add_css_class("accent")
        header_box.append(icon_img)

        title_lbl = Gtk.Label(label="Welcome to WaveController")
        title_lbl.add_css_class("wave-main-title")
        title_lbl.set_halign(Gtk.Align.CENTER)
        header_box.append(title_lbl)

        sub_lbl = Gtk.Label(label="Let's configure your primary microphone and monitor headphones to get started.")
        sub_lbl.add_css_class("mix-header-subtitle")
        sub_lbl.set_wrap(True)
        sub_lbl.set_justify(Gtk.Justification.CENTER)
        sub_lbl.set_halign(Gtk.Align.CENTER)
        header_box.append(sub_lbl)

        main_box.append(header_box)

        # Device Selection Preferences Group
        pref_page = Adw.PreferencesPage()
        grp_hw = Adw.PreferencesGroup(title="Primary Hardware Devices")

        self.mic_combo = Adw.ComboRow(
            title="Primary Microphone",
            subtitle="Capture device for main microphone channel and telemetry"
        )
        self.output_combo = Adw.ComboRow(
            title="Primary Monitor Output",
            subtitle="Headphones / DAC for Personal Mix and fallback audio"
        )

        # Populate Input Devices
        input_opts = []
        if self.hardware_mgr:
            for dev in self.hardware_mgr.get_tracked_input_devices():
                k = dev.get("device_key", dev.get("name", ""))
                name = dev.get("display_name", dev.get("name", "Microphone"))
                input_opts.append((k, name))
        if not input_opts:
            input_opts = [("default", "Default Microphone (System)")]

        self._input_dev_keys = [opt[0] for opt in input_opts]
        self.mic_combo.set_model(Gtk.StringList.new([opt[1] for opt in input_opts]))

        # Auto-select Elgato mic if detected, else first device
        sel_in_idx = 0
        for idx, (k, name) in enumerate(input_opts):
            if "wave" in name.lower() or "elgato" in name.lower() or "wave" in k.lower():
                sel_in_idx = idx
                break
        self.mic_combo.set_selected(sel_in_idx)

        # Populate Output Devices
        output_opts = []
        if self.hardware_mgr:
            for dev in self.hardware_mgr.get_tracked_output_devices():
                k = dev.get("device_key", dev.get("name", ""))
                name = dev.get("display_name", dev.get("name", "Audio Device"))
                output_opts.append((k, name))
        if not output_opts:
            output_opts = [("default", "Default Output (System)")]

        self._output_dev_keys = [opt[0] for opt in output_opts]
        self.output_combo.set_model(Gtk.StringList.new([opt[1] for opt in output_opts]))

        # Auto-select Elgato output if detected, else first device
        sel_out_idx = 0
        for idx, (k, name) in enumerate(output_opts):
            if "wave" in name.lower() or "elgato" in name.lower() or "wave" in k.lower():
                sel_out_idx = idx
                break
        self.output_combo.set_selected(sel_out_idx)

        grp_hw.add(self.mic_combo)
        grp_hw.add(self.output_combo)
        pref_page.add(grp_hw)

        main_box.append(pref_page)

        # Bottom Action Bar
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(8)

        finish_btn = Gtk.Button(label="Complete Setup & Launch")
        finish_btn.add_css_class("suggested-action")
        finish_btn.set_size_request(200, 42)
        finish_btn.connect("clicked", self._on_finish_clicked)

        btn_box.append(finish_btn)
        main_box.append(btn_box)

        self.set_child(main_box)

    def _on_finish_clicked(self, btn):
        in_idx = self.mic_combo.get_selected()
        out_idx = self.output_combo.get_selected()

        sel_mic_key = self._input_dev_keys[in_idx] if 0 <= in_idx < len(self._input_dev_keys) else "default"
        sel_out_key = self._output_dev_keys[out_idx] if 0 <= out_idx < len(self._output_dev_keys) else "default"

        # Get display names
        mic_name = "Microphone"
        if self.hardware_mgr:
            for dev in self.hardware_mgr.get_tracked_input_devices():
                if dev.get("device_key") == sel_mic_key:
                    mic_name = dev.get("display_name", dev.get("name", "Microphone"))
                    break

        # Save config preferences
        config_manager.set("first_run_completed", True)
        config_manager.set("default_input_device", sel_mic_key)
        config_manager.set("default_output_device", sel_out_key, immediate=True)

        if self.pipewire_mgr:
            self.pipewire_mgr.selected_monitor_device = sel_out_key
            self.pipewire_mgr.default_input_device = sel_mic_key

            # Update initial mic channel name to match hardware device
            with self.pipewire_mgr._lock:
                if self.pipewire_mgr.channels:
                    first_ch = self.pipewire_mgr.channels[0]
                    if first_ch.get("type") == "source" or first_ch.get("id") in ("mic", "elgato_wave_xlr"):
                        first_ch["name"] = mic_name
                        self.pipewire_mgr.assigned_apps[first_ch["id"]] = [mic_name, sel_mic_key]

                # Update Personal Mix target_device
                for m in self.pipewire_mgr.mixes:
                    if m.get("id") in ("personal", "personal_mix") or m.get("type") == "sink":
                        m["target_device"] = sel_out_key
                        break

            self.pipewire_mgr._save_state_to_config(immediate=True)
            self.pipewire_mgr._ensure_virtual_mix_nodes()
            self.pipewire_mgr._sync_channel_audio_routing()

        self.close()

        if self.on_complete_callback:
            self.on_complete_callback()
