import os
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk, Gdk, GLib

from .window import WaveMainWindow
from .engine.pipewire_manager import PipeWireManager
from .engine.peak_monitor import MultiChannelPeakMonitor
from .engine.usb_hardware import USBHardwareManager
from .engine.ipc_server import IPCServer
from .engine.tray_manager import TrayManager
from .utils.logger import get_logger

log = get_logger("App")

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
        self._system_bus = None
        self._sleep_sub_id = None

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
        self.peak_monitor = MultiChannelPeakMonitor(pipewire_mgr=self.pipewire_mgr, hardware_mgr=self.hardware_mgr)
        self.pipewire_mgr.set_peak_monitor(self.peak_monitor)
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
        self._setup_power_monitor()

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

    def _setup_power_monitor(self):
        """Subscribes to systemd-logind PrepareForSleep signal to handle sleep/resume lifecycle."""
        try:
            self._system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            if self._system_bus:
                self._sleep_sub_id = self._system_bus.signal_subscribe(
                    "org.freedesktop.login1",
                    "org.freedesktop.login1.Manager",
                    "PrepareForSleep",
                    "/org/freedesktop/login1",
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_prepare_for_sleep,
                    None
                )
                log.info("[WaveController.Power] Subscribed to systemd-logind PrepareForSleep signal")
        except Exception as e:
            log.warning(f"[WaveController.Power] Failed to subscribe to system power monitor: {e}")

    def _on_prepare_for_sleep(self, conn, sender, path, iface, signal, params, user_data):
        try:
            is_sleep = params.get_child_value(0).get_boolean()
        except Exception:
            return

        if is_sleep:
            log.info("[WaveController.Power] System preparing for sleep/suspend...")
            if hasattr(self, "hardware_mgr") and self.hardware_mgr:
                self.hardware_mgr.on_system_suspend()
            if hasattr(self, "pipewire_mgr") and self.pipewire_mgr:
                self.pipewire_mgr.on_system_suspend()
            if hasattr(self, "peak_monitor") and self.peak_monitor:
                self.peak_monitor.on_system_suspend()
        else:
            log.info("[WaveController.Power] System resumed from sleep/suspend. Triggering immediate restoration...")
            # 1. Hardware restore starts IMMEDIATELY upon wake signal with fast-polling (no 1.2s delay)
            if hasattr(self, "hardware_mgr") and self.hardware_mgr:
                self.hardware_mgr.on_system_resume()

            # 2. PipeWire audio routing & UI refresh runs with a 1.5s settling window for daemon restart
            GLib.timeout_add(1500, self._on_system_resume_delayed)

    def _on_system_resume_delayed(self):
        try:
            log.info("[WaveController.Power] Restoring audio routing and UI following resume...")
            if hasattr(self, "pipewire_mgr") and self.pipewire_mgr:
                self.pipewire_mgr.on_system_resume()
            if hasattr(self, "peak_monitor") and self.peak_monitor:
                self.peak_monitor.on_system_resume()

            # Refresh UI faders and rebuild sidebar device views if window is active
            win = self.props.active_window
            if win:
                if hasattr(win, "_rebuild_device_views"):
                    win._rebuild_device_views()
                view = getattr(win, "mixer_view", None) or getattr(win, "matrix_view", None)
                if view and hasattr(view, "refresh_all_faders"):
                    view.refresh_all_faders()
        except Exception as e:
            log.error(f"[WaveController.Power] Error during system resume restoration: {e}")

        # Schedule a secondary retry to catch late-arriving PipeWire nodes
        GLib.timeout_add(2500, self._on_system_resume_secondary)
        return False  # Run once in GLib main loop

    def _on_system_resume_secondary(self):
        """Secondary resume pass to catch PipeWire nodes that arrive late after wake."""
        try:
            log.info("[WaveController.Power] Secondary resume pass: re-syncing routing...")
            if hasattr(self, "pipewire_mgr") and self.pipewire_mgr:
                self.pipewire_mgr._refresh_node_cache()
                self.pipewire_mgr._sync_channel_audio_routing()
            if hasattr(self, "peak_monitor") and self.peak_monitor:
                self.peak_monitor.on_system_resume()
        except Exception as e:
            log.error(f"[WaveController.Power] Error during secondary resume: {e}")
        return False  # Run once in GLib main loop

    def do_shutdown(self):
        if self._sleep_sub_id and self._system_bus:
            try:
                self._system_bus.signal_unsubscribe(self._sleep_sub_id)
            except Exception:
                pass
        for win in self.get_windows():
            if hasattr(win, "save_window_state"):
                win.save_window_state()
        self.tray_mgr.stop()
        self.ipc_server.stop()
        self.peak_monitor.stop()
        self.pipewire_mgr.stop()
        Adw.Application.do_shutdown(self)
