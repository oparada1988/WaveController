from datetime import datetime
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib

from ..engine.config_manager import config_manager
from ..utils.logger import get_log_file_path, get_log_dir_path, get_log_size_str, export_logs_to, clear_logs

class SettingsView(Gtk.Box):
    """
    Application & Audio Engine Preferences.
    """
    def __init__(self, hardware_mgr, on_theme_changed=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.hardware_mgr = hardware_mgr
        self.on_theme_changed = on_theme_changed

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
        autostart_row.set_active(True)
        grp_gen.add(autostart_row)

        tray_active = config_manager.get("close_to_tray", True)
        tray_row = Adw.SwitchRow(title="Close to System Tray", subtitle="Keep sub-mixing engine active in background")
        tray_row.set_active(tray_active)
        tray_row.connect("notify::active", lambda r, p: config_manager.set("close_to_tray", r.get_active(), immediate=True))
        grp_gen.add(tray_row)

        pref_page.add(grp_gen)

        # Group 2: Stream Deck & Integration
        grp_sd = Adw.PreferencesGroup(title="Stream Deck &amp; Volume Controller Plus Integration")

        ipc_row = Adw.ActionRow(title="Volume Controller Plus IPC Server", subtitle="Unix Socket active at /tmp/wavecontroller.sock")
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
                "Export WaveController Logs",
                self.get_root(),
                Gtk.FileChooserAction.SAVE,
                "Export",
                "Cancel"
            )
            date_str = datetime.now().strftime("%Y-%m-%d")
            dialog.set_current_name(f"WaveController_Logs_{date_str}.log")

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
            title="Clear Logs",
            subtitle="Reset and truncate the active log file"
        )
        
        clear_btn = Gtk.Button(label="Clear Logs")
        clear_btn.set_icon_name("edit-clear-symbolic")
        clear_btn.add_css_class("flat")
        clear_btn.set_valign(Gtk.Align.CENTER)

        def on_clear_clicked(btn):
            clear_logs()
            self._update_log_info()
            clear_btn.set_label("Cleared!")
            GLib.timeout_add(1500, lambda: (clear_btn.set_label("Clear Logs"), False))

        clear_btn.connect("clicked", on_clear_clicked)
        clear_row.add_suffix(clear_btn)
        grp_diag.add(clear_row)

        pref_page.add(grp_diag)

        self.append(pref_page)

        # Center-Aligned About Footer (Zero emojis, clean typography & links)
        about_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        about_box.set_halign(Gtk.Align.CENTER)
        about_box.set_margin_top(12)
        about_box.set_margin_bottom(24)

        app_title_lbl = Gtk.Label(label="WaveController v0.0.1")
        app_title_lbl.add_css_class("heading")
        app_title_lbl.set_halign(Gtk.Align.CENTER)
        about_box.append(app_title_lbl)

        github_link = Gtk.LinkButton(
            uri="https://github.com/oparada1988/WaveController",
            label="GitHub"
        )
        github_link.add_css_class("flat")
        github_link.set_halign(Gtk.Align.CENTER)
        about_box.append(github_link)

        issues_link = Gtk.LinkButton(
            uri="https://github.com/oparada1988/WaveController/issues",
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
