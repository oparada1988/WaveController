import os
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk, Gdk

from .window import WaveMainWindow
from .engine.pipewire_manager import PipeWireManager
from .engine.peak_monitor import MultiChannelPeakMonitor
from .engine.usb_hardware import USBHardwareManager
from .engine.ipc_server import IPCServer
from .engine.tray_manager import TrayManager

class WaveControllerApp(Adw.Application):
    """
    WaveController Main Adw.Application.
    """
    def __init__(self, is_daemon: bool = False):
        super().__init__(
            application_id="com.oparada.WaveController",
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )
        self.is_daemon = is_daemon
        self._daemon_started = False
        self.hardware_mgr = None
        self.pipewire_mgr = None
        self.peak_monitor = None
        self.ipc_server = None
        self.tray_mgr = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.hold() # Keep application process & PipeWire routing alive in background
        display = Gdk.Display.get_default()
        if display:
            theme = Gtk.IconTheme.get_for_display(display)
            icons_dir = os.path.join(os.path.dirname(__file__), "..", "assets", "icons")
            if os.path.exists(icons_dir):
                theme.add_search_path(icons_dir)

        self.hardware_mgr = USBHardwareManager()
        self.pipewire_mgr = PipeWireManager(hardware_mgr=self.hardware_mgr)
        self.hardware_mgr.set_pipewire_manager(self.pipewire_mgr)
        self.peak_monitor = MultiChannelPeakMonitor()
        self.ipc_server = IPCServer(self.pipewire_mgr, self.peak_monitor, self.hardware_mgr)
        self.tray_mgr = TrayManager(
            on_activate=self._on_tray_activate,
            on_open_settings=self._on_tray_settings,
            on_toggle_mic_mute=self._on_tray_toggle_mic,
            on_toggle_all_mute=self._on_tray_toggle_all,
            get_mic_muted=self._get_mic_muted,
            get_all_muted=self._get_all_muted,
            on_quit=self._on_tray_quit
        )

        self.pipewire_mgr.start()
        self.peak_monitor.start()
        self.ipc_server.start()
        self.tray_mgr.start()

    def do_activate(self):
        if self.is_daemon and not self._daemon_started:
            self._daemon_started = True
            return

        win = self.props.active_window
        if not win:
            win = WaveMainWindow(
                self,
                self.pipewire_mgr,
                self.peak_monitor,
                self.hardware_mgr
            )
        win.set_visible(True)
        win.present()

    def _on_tray_activate(self):
        win = self.props.active_window
        if not win:
            self.activate()
        else:
            if win.is_visible():
                win.set_visible(False)
            else:
                win.set_visible(True)
                win.present()

    def _on_tray_settings(self):
        win = self.props.active_window
        if not win:
            self.activate()
            win = self.props.active_window
        if win:
            win.set_visible(True)
            win.present()
            if hasattr(win, "_switch_view") and hasattr(win, "settings_btn"):
                win._switch_view("settings", win.settings_btn)

    def _get_mic_muted(self) -> bool:
        if not self.pipewire_mgr:
            return False
        for mix in self.pipewire_mgr.mixes:
            st = self.pipewire_mgr.get_channel_state("mic", mix["id"])
            if st.get("muted", False):
                return True
        return False

    def _on_tray_toggle_mic(self):
        if not self.pipewire_mgr:
            return
        is_muted = self._get_mic_muted()
        new_muted = not is_muted
        for mix in self.pipewire_mgr.mixes:
            self.pipewire_mgr.set_channel_mute("mic", mix["id"], new_muted)
        self.tray_mgr.notify_menu_updated()

    def _get_all_muted(self) -> bool:
        if not self.pipewire_mgr:
            return False
        for ch in self.pipewire_mgr.channels:
            for mix in self.pipewire_mgr.mixes:
                st = self.pipewire_mgr.get_channel_state(ch["id"], mix["id"])
                if not st.get("muted", False):
                    return False
        return True

    def _on_tray_toggle_all(self):
        if not self.pipewire_mgr:
            return
        is_all_muted = self._get_all_muted()
        new_muted = not is_all_muted
        for ch in self.pipewire_mgr.channels:
            for mix in self.pipewire_mgr.mixes:
                self.pipewire_mgr.set_channel_mute(ch["id"], mix["id"], new_muted)
        self.tray_mgr.notify_menu_updated()

    def _on_tray_quit(self):
        for win in self.get_windows():
            if hasattr(win, "save_window_state"):
                win.save_window_state()
            win.destroy()
        try:
            self.release()
        except Exception:
            pass
        self.quit()

    def do_shutdown(self):
        for win in self.get_windows():
            if hasattr(win, "save_window_state"):
                win.save_window_state()
        self.tray_mgr.stop()
        self.ipc_server.stop()
        self.peak_monitor.stop()
        self.pipewire_mgr.stop()
        Adw.Application.do_shutdown(self)
