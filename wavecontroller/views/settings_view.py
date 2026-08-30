from datetime import datetime
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib

from ..engine.config_manager import config_manager
from ..utils.logger import get_log_file_path, get_log_dir_path, get_log_size_str, export_logs_to, clear_logs
from ..utils.autostart import is_autostart_enabled, set_autostart_enabled
from .. import __version__, __github__, __issues__

class SettingsView(Gtk.Box):
    """
    Application & Audio Engine Preferences.
    """
    def __init__(self, hardware_mgr, pipewire_mgr=None, on_theme_changed=None, on_hw_defaults_changed=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.hardware_mgr = hardware_mgr
        self.pipewire_mgr = pipewire_mgr
        self.on_theme_changed = on_theme_changed
        self.on_hw_defaults_changed = on_hw_defaults_changed

        self.set_margin_top(20)
        self.set_margin_bottom(20)
        self.set_margin_start(24)
        self.set_margin_end(24)

        # Title
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_lbl = Gtk.Label(label="Preferences")
        title_lbl.add_css_class("wave-main-title")
        title_box.append(title_lbl)
        self.append(title_box)

        pref_page = Adw.PreferencesPage()

        # Group 0: Default Hardware Devices
        grp_hw = Adw.PreferencesGroup(title="Default Hardware Devices")

        hw_refresh_btn = Gtk.Button(label="Refresh Devices")
        hw_refresh_btn.set_icon_name("view-refresh-symbolic")
        hw_refresh_btn.add_css_class("flat")
        hw_refresh_btn.set_valign(Gtk.Align.CENTER)
        grp_hw.set_header_suffix(hw_refresh_btn)

        self.mic_combo = Adw.ComboRow(
            title="Primary Microphone",
            subtitle="Default hardware capture device for main microphone channel and telemetry"
        )
        self.output_combo = Adw.ComboRow(
            title="Primary Monitor Output",
            subtitle="Default hardware DAC / headphones for Personal Mix and fallback audio"
        )

        self._input_dev_keys = []
        self._output_dev_keys = []
        self._refreshing_combos = False

        def _refresh_hw_combos():
            self._refreshing_combos = True
            try:
                if self.hardware_mgr and hasattr(self.hardware_mgr, "detect_connected_hardware"):
                    self.hardware_mgr.detect_connected_hardware()

                # Populate inputs (strictly added/tracked devices in WaveController)
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

                saved_in = config_manager.get("default_input_device", "default")
                sel_in_idx = 0
                for idx, k in enumerate(self._input_dev_keys):
                    if k == saved_in or (saved_in in k):
                        sel_in_idx = idx
                        break
                self.mic_combo.set_selected(sel_in_idx)

                # Populate outputs (strictly added/tracked devices in WaveController)
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

                saved_out = config_manager.get("default_output_device", "default")
                sel_out_idx = 0
                for idx, k in enumerate(self._output_dev_keys):
                    if k == saved_out or (saved_out in k):
                        sel_out_idx = idx
                        break
                self.output_combo.set_selected(sel_out_idx)
            finally:
                self._refreshing_combos = False

        self.refresh_device_list = _refresh_hw_combos
        _refresh_hw_combos()

        def _on_manual_refresh_clicked(btn):
            _refresh_hw_combos()
            btn.set_label("Refreshed!")
            GLib.timeout_add(1500, lambda: (btn.set_label("Refresh Devices"), False))

        hw_refresh_btn.connect("clicked", _on_manual_refresh_clicked)

        def _on_mic_changed(row, param):
            if self._refreshing_combos:
                return
            idx = row.get_selected()
            if 0 <= idx < len(self._input_dev_keys):
                new_k = self._input_dev_keys[idx]
                config_manager.set("default_input_device", new_k, immediate=True)
                if self.pipewire_mgr:
                    self.pipewire_mgr.default_input_device = new_k
                    self.pipewire_mgr._sync_channel_audio_routing()
                if self.on_hw_defaults_changed:
                    self.on_hw_defaults_changed()

        def _on_output_changed(row, param):
            if self._refreshing_combos:
                return
            idx = row.get_selected()
            if 0 <= idx < len(self._output_dev_keys):
                new_k = self._output_dev_keys[idx]
                config_manager.set("default_output_device", new_k, immediate=True)
                if self.pipewire_mgr:
                    self.pipewire_mgr.selected_monitor_device = new_k
                    # Update Personal Mix target_device
                    for m in self.pipewire_mgr.mixes:
                        if m.get("id") in ("personal", "personal_mix") or m.get("type") == "sink":
                            m["target_device"] = new_k
                            break
                    self.pipewire_mgr._save_state_to_config(immediate=True)
                    self.pipewire_mgr._sync_channel_audio_routing()
                if self.on_hw_defaults_changed:
                    self.on_hw_defaults_changed()

        self.mic_combo.connect("notify::selected", _on_mic_changed)
        self.output_combo.connect("notify::selected", _on_output_changed)

        grp_hw.add(self.mic_combo)
        grp_hw.add(self.output_combo)
        pref_page.add(grp_hw)

        # Group 1: Appearance & Theme
        grp_theme = Adw.PreferencesGroup(title="Appearance &amp; Theme")

        self.theme_row = Adw.SwitchRow(
            title="Use System Theme",
            subtitle="Follow system GTK4 / Libadwaita theme instead of Midnight Dark"
        )
        use_sys = config_manager.get("use_system_theme", False)
        self.theme_row.set_active(use_sys)
        self.theme_row.connect("notify::active", self._on_theme_toggled)
        grp_theme.add(self.theme_row)

        pref_page.add(grp_theme)

        # Group 2: General
        grp_gen = Adw.PreferencesGroup(title="General")

        autostart_row = Adw.SwitchRow(title="Start Automatically on Login", subtitle="Launch WaveController daemon in background")
        autostart_row.set_active(is_autostart_enabled())
        autostart_row.connect("notify::active", lambda r, p: set_autostart_enabled(r.get_active()))
        grp_gen.add(autostart_row)

        tray_active = config_manager.get("close_to_tray", True)
        tray_row = Adw.SwitchRow(title="Close to System Tray", subtitle="Keep sub-mixing engine active in background")
        tray_row.set_active(tray_active)
        tray_row.connect("notify::active", lambda r, p: config_manager.set("close_to_tray", r.get_active(), immediate=True))
        grp_gen.add(tray_row)

        pref_page.add(grp_gen)

        # Group 2: Stream Deck & Integration
        grp_sd = Adw.PreferencesGroup(title="Stream Deck &amp; Volume Controller Plus Integration")

        ipc_row = Adw.ActionRow(title="Volume Controller Plus IPC Server", subtitle="Unix Socket active at ~/.config/WaveController/wavecontroller.sock")
        ipc_status = Gtk.Label(label="Connected")
        ipc_status.add_css_class("wave-icon-btn")
        ipc_status.add_css_class("active")
        ipc_row.add_suffix(ipc_status)
        grp_sd.add(ipc_row)

        pref_page.add(grp_sd)

        # Group 3: Audio Engine
        grp_audio = Adw.PreferencesGroup(title="PipeWire Audio Engine")

        rate_row = Adw.ComboRow(title="Sample Rate", subtitle="Engine processing frequency")
        rate_row.set_model(Gtk.StringList.new(["48,000 Hz (Broadcast standard)", "44,100 Hz", "96,000 Hz (Hi-Res)"]))
        rate_row.set_selected(0)
        grp_audio.add(rate_row)

        buffer_row = Adw.ComboRow(title="Buffer Size / Latency", subtitle="Lower values reduce monitoring latency")
        buffer_row.set_model(Gtk.StringList.new(["64 samples (1.3 ms)", "128 samples (2.7 ms - Recommended)", "256 samples (5.3 ms)", "512 samples (10.7 ms)"]))
        buffer_row.set_selected(1)
        grp_audio.add(buffer_row)

        pref_page.add(grp_audio)

        # Group 4: Diagnostics & Troubleshooting
        grp_diag = Adw.PreferencesGroup(title="Diagnostics &amp; Troubleshooting")

        # Row 1: Active Log File Info + Open Folder
        self.log_info_row = Adw.ActionRow(
            title="Application Log",
            subtitle=f"{get_log_file_path()} ({get_log_size_str()})"
        )
        
        open_folder_btn = Gtk.Button(label="Open Folder")
        open_folder_btn.set_icon_name("folder-open-symbolic")
        open_folder_btn.add_css_class("flat")
        open_folder_btn.set_valign(Gtk.Align.CENTER)
        
        def on_open_folder_clicked(btn):
            log_dir = get_log_dir_path()
            try:
                Gio.AppInfo.launch_default_for_uri(f"file://{log_dir}", None)
            except Exception:
                import subprocess
                subprocess.Popen(["xdg-open", log_dir])

        open_folder_btn.connect("clicked", on_open_folder_clicked)
        self.log_info_row.add_suffix(open_folder_btn)
        grp_diag.add(self.log_info_row)

        # Row 2: Export Logs
        export_row = Adw.ActionRow(
            title="Export Diagnostics &amp; Logs",
            subtitle="Save active log file to disk for troubleshooting or bug reporting"
        )
        
        export_btn = Gtk.Button(label="Export Logs...")
        export_btn.set_icon_name("document-save-symbolic")
        export_btn.add_css_class("suggested-action")
        export_btn.set_valign(Gtk.Align.CENTER)

        def on_export_clicked(btn):
            dialog = Gtk.FileChooserNative.new(
                "Export Diagnostics & Logs",
                self.get_root() if hasattr(self, "get_root") else None,
                Gtk.FileChooserAction.SAVE,
                "_Save",
                "_Cancel"
            )
            import time
            dialog.set_current_name(f"wavecontroller-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.log")

            def on_response(dlg, response_id):
                if response_id == Gtk.ResponseType.ACCEPT:
                    target_file = dlg.get_file()
                    if target_file:
                        dest_path = target_file.get_path()
                        if export_logs_to(dest_path):
                            export_btn.set_label("Exported!")
                            GLib.timeout_add(2000, lambda: (export_btn.set_label("Export Logs..."), False))
                dlg.destroy()

            dialog.connect("response", on_response)
            dialog.show()

        export_btn.connect("clicked", on_export_clicked)
        export_row.add_suffix(export_btn)
        grp_diag.add(export_row)

        # Row 3: Clear Logs
        clear_row = Adw.ActionRow(
            title="Clear Active Log",
            subtitle="Truncates the log file to reclaim disk space"
        )
        
        clear_btn = Gtk.Button(label="Clear Log")
        clear_btn.set_icon_name("user-trash-symbolic")
        clear_btn.add_css_class("destructive-action")
        clear_btn.set_valign(Gtk.Align.CENTER)

        def on_clear_clicked(btn):
            clear_logs()
            self._update_log_info()
            clear_btn.set_label("Cleared!")
            GLib.timeout_add(1500, lambda: (clear_btn.set_label("Clear Log"), False))

        clear_btn.connect("clicked", on_clear_clicked)
        clear_row.add_suffix(clear_btn)
        grp_diag.add(clear_row)

        pref_page.add(grp_diag)

        # Group 5: Configuration Backup & Data Management
        grp_backup = Adw.PreferencesGroup(title="Configuration Backup &amp; Data Management")

        # Row 1: Export Backup
        backup_export_row = Adw.ActionRow(
            title="Export Configuration Backup",
            subtitle="Save your channels, mixes, hardware parameters, and LED colors to a JSON file"
        )
        backup_export_btn = Gtk.Button(label="Export Backup...")
        backup_export_btn.set_icon_name("document-save-as-symbolic")
        backup_export_btn.add_css_class("suggested-action")
        backup_export_btn.set_valign(Gtk.Align.CENTER)

        def on_backup_export_clicked(btn):
            dialog = Gtk.FileChooserNative.new(
                "Export Configuration Backup",
                self.get_root(),
                Gtk.FileChooserAction.SAVE,
                "Export",
                "Cancel"
            )
            date_str = datetime.now().strftime("%Y-%m-%d")
            dialog.set_current_name(f"WaveController_Backup_{date_str}.json")
            
            # JSON Filter
            json_filter = Gtk.FileFilter()
            json_filter.set_name("JSON Configuration (*.json)")
            json_filter.add_pattern("*.json")
            dialog.add_filter(json_filter)

            def on_export_resp(dlg, response_id):
                if response_id == Gtk.ResponseType.ACCEPT:
                    target_file = dlg.get_file()
                    if target_file:
                        dest_path = target_file.get_path()
                        if config_manager.export_backup(dest_path):
                            backup_export_btn.set_label("Exported!")
                            GLib.timeout_add(2000, lambda: (backup_export_btn.set_label("Export Backup..."), False))
                dlg.destroy()

            dialog.connect("response", on_export_resp)
            dialog.show()

        backup_export_btn.connect("clicked", on_backup_export_clicked)
        backup_export_row.add_suffix(backup_export_btn)
        grp_backup.add(backup_export_row)

        # Row 2: Import Backup
        backup_import_row = Adw.ActionRow(
            title="Restore from Backup",
            subtitle="Import a previously saved WaveController backup JSON file"
        )
        backup_import_btn = Gtk.Button(label="Restore Backup...")
        backup_import_btn.set_icon_name("document-open-symbolic")
        backup_import_btn.add_css_class("flat")
        backup_import_btn.set_valign(Gtk.Align.CENTER)

        def on_backup_import_clicked(btn):
            dialog = Gtk.FileChooserNative.new(
                "Restore Configuration from Backup",
                self.get_root(),
                Gtk.FileChooserAction.OPEN,
                "Restore",
                "Cancel"
            )
            json_filter = Gtk.FileFilter()
            json_filter.set_name("JSON Configuration (*.json)")
            json_filter.add_pattern("*.json")
            dialog.add_filter(json_filter)

            def on_import_resp(dlg, response_id):
                if response_id == Gtk.ResponseType.ACCEPT:
                    src_file = dlg.get_file()
                    if src_file:
                        src_path = src_file.get_path()
                        if config_manager.import_backup(src_path):
                            backup_import_btn.set_label("Restored!")
                            GLib.timeout_add(2000, lambda: (backup_import_btn.set_label("Restore Backup..."), False))
                dlg.destroy()

            dialog.connect("response", on_import_resp)
            dialog.show()

        backup_import_btn.connect("clicked", on_backup_import_clicked)
        backup_import_row.add_suffix(backup_import_btn)
        grp_backup.add(backup_import_row)

        # Row 3: Factory Reset
        reset_row = Adw.ActionRow(
            title="Reset to Factory Defaults",
            subtitle="Restore all channel assignments, mixes, and hardware settings to defaults"
        )
        reset_btn = Gtk.Button(label="Reset Defaults")
        reset_btn.set_icon_name("edit-clear-all-symbolic")
        reset_btn.add_css_class("destructive-action")
        reset_btn.set_valign(Gtk.Align.CENTER)

        def on_reset_clicked(btn):
            if config_manager.reset_to_defaults():
                reset_btn.set_label("Reset!")
                GLib.timeout_add(2000, lambda: (reset_btn.set_label("Reset Defaults"), False))

        reset_btn.connect("clicked", on_reset_clicked)
        reset_row.add_suffix(reset_btn)
        grp_backup.add(reset_row)

        pref_page.add(grp_backup)

        self.append(pref_page)

        # Center-Aligned About Footer (Zero emojis, clean typography & links)
        about_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        about_box.set_halign(Gtk.Align.CENTER)
        about_box.set_margin_top(12)
        about_box.set_margin_bottom(24)

        app_title_lbl = Gtk.Label(label=f"WaveController v{__version__}")
        app_title_lbl.add_css_class("heading")
        app_title_lbl.set_halign(Gtk.Align.CENTER)
        about_box.append(app_title_lbl)

        github_link = Gtk.LinkButton(
            uri=__github__,
            label="GitHub"
        )
        github_link.add_css_class("flat")
        github_link.set_halign(Gtk.Align.CENTER)
        about_box.append(github_link)

        issues_link = Gtk.LinkButton(
            uri=__issues__,
            label="Submit Issue"
        )
        issues_link.add_css_class("flat")
        issues_link.set_halign(Gtk.Align.CENTER)
        about_box.append(issues_link)

        self.append(about_box)

    def _update_log_info(self):
        self.log_info_row.set_subtitle(f"{get_log_file_path()} ({get_log_size_str()})")

    def _on_theme_toggled(self, row, *args):
        is_sys = row.get_active()
        config_manager.set("use_system_theme", is_sys, immediate=True)
        if self.on_theme_changed:
            self.on_theme_changed()
