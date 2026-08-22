import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib

from .window import WaveMainWindow
from .engine.pipewire_manager import PipeWireManager
from .engine.peak_monitor import MultiChannelPeakMonitor
from .engine.usb_hardware import USBHardwareManager
from .engine.ipc_server import IPCServer

class WaveControllerApp(Adw.Application):
    """
    WaveController Main Adw.Application.
    """
    def __init__(self):
        super().__init__(
            application_id="com.oparada.WaveController",
            flags=Gio.ApplicationFlags.FLAGS_NONE
        )
        self.pipewire_mgr = PipeWireManager()
        self.peak_monitor = MultiChannelPeakMonitor()
        self.hardware_mgr = USBHardwareManager()
        self.ipc_server = IPCServer(self.pipewire_mgr, self.peak_monitor, self.hardware_mgr)

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.pipewire_mgr.start()
        self.peak_monitor.start()
        self.ipc_server.start()

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = WaveMainWindow(
                self,
                self.pipewire_mgr,
                self.peak_monitor,
                self.hardware_mgr
            )
        win.present()

    def do_shutdown(self):
        self.ipc_server.stop()
        self.peak_monitor.stop()
        self.pipewire_mgr.stop()
        Adw.Application.do_shutdown(self)
