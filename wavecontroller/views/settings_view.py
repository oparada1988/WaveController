import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

from ..engine.config_manager import config_manager

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

        tray_row = Adw.SwitchRow(title="Close to System Tray", subtitle="Keep sub-mixing engine active in background")
        tray_row.set_active(True)
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
        self.append(pref_page)

    def _on_theme_toggled(self, row, *args):
        is_sys = row.get_active()
        config_manager.set("use_system_theme", is_sys, immediate=True)
        if self.on_theme_changed:
            self.on_theme_changed()
