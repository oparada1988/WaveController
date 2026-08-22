import os
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk

from .views.mixer_matrix import MixerMatrixView
from .views.device_settings import DeviceSettingsView
from .views.effects_view import EffectsView
from .views.settings_view import SettingsView

class WaveMainWindow(Adw.ApplicationWindow):
    """
    Main WaveController Desktop Window with modern Libadwaita layout matching Wave Link.
    """
    def __init__(self, app, pipewire_mgr, peak_monitor, hardware_mgr, **kwargs):
        super().__init__(application=app, title="WaveController", **kwargs)
        self.pipewire_mgr = pipewire_mgr
        self.peak_monitor = peak_monitor
        self.hardware_mgr = hardware_mgr

        self.set_default_size(1050, 680)
        self.add_css_class("wave-window")

        # Load Custom CSS
        css_path = os.path.join(os.path.dirname(__file__), "utils", "style.css")
        if os.path.exists(css_path):
            provider = Gtk.CssProvider()
            provider.load_from_path(css_path)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Main Split Box (Sidebar + Content View)
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        
        # 1. Left Sidebar
        sidebar = self._build_sidebar()
        main_box.append(sidebar)

        # 2. Main Content Stack
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)

        self.mixer_view = MixerMatrixView(self.pipewire_mgr, self.peak_monitor, self.hardware_mgr)
        self.stack.add_named(self.mixer_view, "mixes")

        self.device_view = DeviceSettingsView(self.hardware_mgr)
        self.stack.add_named(self.device_view, "device")

        self.effects_view = EffectsView()
        self.stack.add_named(self.effects_view, "effects")

        self.settings_view = SettingsView(self.hardware_mgr)
        self.stack.add_named(self.settings_view, "settings")

        main_box.append(self.stack)

        self.set_content(main_box)

    def _build_sidebar(self) -> Gtk.Box:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sidebar.add_css_class("wave-sidebar")
        sidebar.set_size_request(220, -1)

        # App Brand Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.add_css_class("wave-sidebar-header")
        
        logo_icon = Gtk.Image.new_from_icon_name("audio-card-symbolic")
        logo_icon.set_pixel_size(22)
        header.append(logo_icon)

        title = Gtk.Label(label="WaveController")
        title.add_css_class("wave-sidebar-title")
        header.append(title)
        sidebar.append(header)

        # Section 1: Connected Devices
        sec1_lbl = Gtk.Label(label="Devices")
        sec1_lbl.add_css_class("wave-sidebar-section-title")
        sec1_lbl.set_halign(Gtk.Align.START)
        sidebar.append(sec1_lbl)

        self.dev_btn = Gtk.Button()
        self.dev_btn.add_css_class("flat")
        self.dev_btn.add_css_class("wave-sidebar-row")
        
        dev_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dev_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
        dev_lbl = Gtk.Label(label=self.hardware_mgr.device_name)
        dev_box.append(dev_icon)
        dev_box.append(dev_lbl)
        self.dev_btn.set_child(dev_box)
        self.dev_btn.connect("clicked", lambda b: self._switch_view("device"))
        sidebar.append(self.dev_btn)

        # Section 2: Mixes & Effects
        sec2_lbl = Gtk.Label(label="Mixes & Effects")
        sec2_lbl.add_css_class("wave-sidebar-section-title")
        sec2_lbl.set_halign(Gtk.Align.START)
        sidebar.append(sec2_lbl)

        self.mixes_btn = Gtk.Button()
        self.mixes_btn.add_css_class("flat")
        self.mixes_btn.add_css_class("wave-sidebar-row")
        self.mixes_btn.add_css_class("selected")
        
        mix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mix_icon = Gtk.Image.new_from_icon_name("view-grid-symbolic")
        mix_lbl = Gtk.Label(label="Mixes")
        mix_box.append(mix_icon)
        mix_box.append(mix_lbl)
        self.mixes_btn.set_child(mix_box)
        self.mixes_btn.connect("clicked", lambda b: self._switch_view("mixes"))
        sidebar.append(self.mixes_btn)

        self.fx_btn = Gtk.Button()
        self.fx_btn.add_css_class("flat")
        self.fx_btn.add_css_class("wave-sidebar-row")
        
        fx_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fx_icon = Gtk.Image.new_from_icon_name("system-run-symbolic")
        fx_lbl = Gtk.Label(label="Audio Effects (DSP)")
        fx_box.append(fx_icon)
        fx_box.append(fx_lbl)
        self.fx_btn.set_child(fx_box)
        self.fx_btn.connect("clicked", lambda b: self._switch_view("effects"))
        sidebar.append(self.fx_btn)

        sidebar.append(Gtk.Box(vexpand=True)) # Spacer

        # Bottom Footer Navigation: Settings
        self.settings_btn = Gtk.Button()
        self.settings_btn.add_css_class("flat")
        self.settings_btn.add_css_class("wave-sidebar-row")
        
        set_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        set_icon = Gtk.Image.new_from_icon_name("emblem-system-symbolic")
        set_lbl = Gtk.Label(label="Settings")
        set_box.append(set_icon)
        set_box.append(set_lbl)
        self.settings_btn.set_child(set_box)
        self.settings_btn.connect("clicked", lambda b: self._switch_view("settings"))
        sidebar.append(self.settings_btn)

        sidebar.set_margin_bottom(12)
        return sidebar

    def _switch_view(self, name: str):
        self.stack.set_visible_child_name(name)
        # Update selected styling
        self.mixes_btn.remove_css_class("selected")
        self.dev_btn.remove_css_class("selected")
        self.fx_btn.remove_css_class("selected")
        self.settings_btn.remove_css_class("selected")

        if name == "mixes":
            self.mixes_btn.add_css_class("selected")
        elif name == "device":
            self.dev_btn.add_css_class("selected")
        elif name == "effects":
            self.fx_btn.add_css_class("selected")
        elif name == "settings":
            self.settings_btn.add_css_class("selected")
