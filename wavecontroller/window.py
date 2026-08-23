import os
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, Pango, GLib

from .views.mixer_matrix import MixerMatrixView
from .views.device_settings import InputDeviceSettingsView, OutputDeviceSettingsView
from .views.effects_view import EffectsView
from .views.settings_view import SettingsView
from .engine.config_manager import config_manager

class WaveMainWindow(Adw.ApplicationWindow):
    """
    Main WaveController Desktop Window with unified Adw.HeaderBar, compact sidebar,
    separated Input/Output device settings, and multi-mix matrix sub-mixing layout.
    """
    def __init__(self, app, pipewire_mgr, peak_monitor, hardware_mgr, **kwargs):
        super().__init__(application=app, title="WaveController", **kwargs)
        self.pipewire_mgr = pipewire_mgr
        self.peak_monitor = peak_monitor
        self.hardware_mgr = hardware_mgr

        # Restore saved window size & maximized state
        win_state = config_manager.get("window_state", {"width": 1280, "height": 780, "maximized": False})
        self.set_default_size(win_state.get("width", 1280), win_state.get("height", 780))
        if win_state.get("maximized", False):
            self.maximize()

        self.add_css_class("wave-window")
        self.connect("close-request", self._on_close_request)

        # Load Custom CSS
        css_path = os.path.join(os.path.dirname(__file__), "utils", "style.css")
        if os.path.exists(css_path):
            provider = Gtk.CssProvider()
            provider.load_from_path(css_path)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # ToolbarView providing standard title bar + window controls (Minimize, Maximize, Close)
        toolbar_view = Adw.ToolbarView()

        # Top HeaderBar
        header_bar = Adw.HeaderBar()
        header_bar.set_show_title(True)
        
        # Window Title Widget (Text only, no icon)
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        app_lbl = Gtk.Label(label="WaveController")
        app_lbl.add_css_class("wave-sidebar-title")
        title_box.append(app_lbl)
        header_bar.set_title_widget(title_box)

        toolbar_view.add_top_bar(header_bar)

        # Main Split Box (Sidebar + Content View)
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        
        # 1. Left Compact Sidebar
        sidebar = self._build_sidebar()
        main_box.append(sidebar)

        # 2. Main Content Stack
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)

        self.mixer_view = MixerMatrixView(self.pipewire_mgr, self.peak_monitor, self.hardware_mgr)
        self.stack.add_named(self.mixer_view, "mixes")

        self.input_view = InputDeviceSettingsView(self.hardware_mgr, self.peak_monitor, on_device_renamed=self._refresh_sidebar_devices)
        self.stack.add_named(self.input_view, "input_settings")

        self.output_view = OutputDeviceSettingsView(self.hardware_mgr, on_device_renamed=self._refresh_sidebar_devices)
        self.stack.add_named(self.output_view, "output_settings")

        self.effects_view = EffectsView()
        self.stack.add_named(self.effects_view, "effects")

        self.settings_view = SettingsView(self.hardware_mgr)
        self.stack.add_named(self.settings_view, "settings")

        main_box.append(self.stack)

        toolbar_view.set_content(main_box)
        self.set_content(toolbar_view)

        self.hardware_mgr.on_device_renamed_callback = lambda *a: GLib.idle_add(self._refresh_sidebar_devices)

    def _on_close_request(self, win):
        config_manager.set("window_state", {
            "width": self.get_width(),
            "height": self.get_height(),
            "maximized": self.is_maximized()
        }, immediate=True)
        return False

    def _build_sidebar(self) -> Gtk.Box:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sidebar.add_css_class("wave-sidebar")
        sidebar.set_size_request(200, -1)
        sidebar.set_hexpand(False)

        # Section 1: Audio Hardware Devices
        sec1_lbl = Gtk.Label(label="Audio Devices")
        sec1_lbl.add_css_class("wave-sidebar-section-title")
        sec1_lbl.set_halign(Gtk.Align.START)
        sidebar.append(sec1_lbl)

        # 1. Audio Inputs Button
        self.inputs_btn = Gtk.Button()
        self.inputs_btn.add_css_class("flat")
        self.inputs_btn.add_css_class("wave-sidebar-row")
        in_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        in_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
        in_lbl = Gtk.Label(label="Audio Inputs")
        in_lbl.set_halign(Gtk.Align.START)
        in_lbl.set_hexpand(True)
        in_box.append(in_icon)
        in_box.append(in_lbl)
        self.inputs_btn.set_child(in_box)
        self.inputs_btn.connect("clicked", lambda b: self._switch_view("input_settings", self.inputs_btn))
        sidebar.append(self.inputs_btn)

        # 2. Audio Outputs Button
        self.outputs_btn = Gtk.Button()
        self.outputs_btn.add_css_class("flat")
        self.outputs_btn.add_css_class("wave-sidebar-row")
        out_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        out_icon = Gtk.Image.new_from_icon_name("audio-headphones-symbolic")
        out_lbl = Gtk.Label(label="Audio Outputs")
        out_lbl.set_halign(Gtk.Align.START)
        out_lbl.set_hexpand(True)
        out_box.append(out_icon)
        out_box.append(out_lbl)
        self.outputs_btn.set_child(out_box)
        self.outputs_btn.connect("clicked", lambda b: self._switch_view("output_settings", self.outputs_btn))
        sidebar.append(self.outputs_btn)

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
        mix_lbl.set_halign(Gtk.Align.START)
        mix_lbl.set_hexpand(True)
        mix_box.append(mix_icon)
        mix_box.append(mix_lbl)
        self.mixes_btn.set_child(mix_box)
        self.mixes_btn.connect("clicked", lambda b: self._switch_view("mixes", self.mixes_btn))
        sidebar.append(self.mixes_btn)

        self.fx_btn = Gtk.Button()
        self.fx_btn.add_css_class("flat")
        self.fx_btn.add_css_class("wave-sidebar-row")
        
        fx_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        fx_icon = Gtk.Image.new_from_icon_name("system-run-symbolic")
        fx_lbl = Gtk.Label(label="Audio Effects (DSP)")
        fx_lbl.set_halign(Gtk.Align.START)
        fx_lbl.set_hexpand(True)
        fx_box.append(fx_icon)
        fx_box.append(fx_lbl)
        self.fx_btn.set_child(fx_box)
        self.fx_btn.connect("clicked", lambda b: self._switch_view("effects", self.fx_btn))
        sidebar.append(self.fx_btn)

        sidebar.append(Gtk.Box(vexpand=True)) # Spacer

        # Bottom Footer Navigation: Settings
        self.settings_btn = Gtk.Button()
        self.settings_btn.add_css_class("flat")
        self.settings_btn.add_css_class("wave-sidebar-row")
        
        set_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        set_icon = Gtk.Image.new_from_icon_name("emblem-system-symbolic")
        set_lbl = Gtk.Label(label="Settings")
        set_lbl.set_halign(Gtk.Align.START)
        set_lbl.set_hexpand(True)
        set_box.append(set_icon)
        set_box.append(set_lbl)
        self.settings_btn.set_child(set_box)
        self.settings_btn.connect("clicked", lambda b: self._switch_view("settings", self.settings_btn))
        sidebar.append(self.settings_btn)

        sidebar.set_margin_bottom(12)
        return sidebar

    def _switch_view(self, name: str, active_btn=None):
        self.stack.set_visible_child_name(name)
        
        # Clear selected styling from all buttons
        self.inputs_btn.remove_css_class("selected")
        self.outputs_btn.remove_css_class("selected")
        self.mixes_btn.remove_css_class("selected")
        self.fx_btn.remove_css_class("selected")
        self.settings_btn.remove_css_class("selected")

        if active_btn:
            active_btn.add_css_class("selected")
        elif name == "input_settings":
            self.inputs_btn.add_css_class("selected")
        elif name == "output_settings":
            self.outputs_btn.add_css_class("selected")
        elif name == "mixes":
            self.mixes_btn.add_css_class("selected")
        elif name == "effects":
            self.fx_btn.add_css_class("selected")
        elif name == "settings":
            self.settings_btn.add_css_class("selected")

    def _refresh_sidebar_devices(self):
        if hasattr(self, "input_view") and self.input_view:
            self.input_view.refresh_device_names()
        if hasattr(self, "output_view") and self.output_view:
            self.output_view.refresh_device_names()
        if hasattr(self, "mixer_view") and self.mixer_view:
            self.mixer_view.refresh_device_names()


