import os
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, Pango, GLib

from .views.mixer_matrix import MixerMatrixView
from .views.device_settings import DeviceSettingsView
from .views.effects_view import EffectsView
from .views.settings_view import SettingsView

class WaveMainWindow(Adw.ApplicationWindow):
    """
    Main WaveController Desktop Window with unified Adw.HeaderBar, compact sidebar,
    and multi-mix matrix sub-mixing layout.
    """
    def __init__(self, app, pipewire_mgr, peak_monitor, hardware_mgr, **kwargs):
        super().__init__(application=app, title="WaveController", **kwargs)
        self.pipewire_mgr = pipewire_mgr
        self.peak_monitor = peak_monitor
        self.hardware_mgr = hardware_mgr

        self.set_default_size(1280, 780)
        self.add_css_class("wave-window")

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
        
        # Window Title Widget
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        app_icon = Gtk.Image.new_from_icon_name("audio-card-symbolic")
        app_lbl = Gtk.Label(label="WaveController")
        app_lbl.add_css_class("wave-sidebar-title")
        title_box.append(app_icon)
        title_box.append(app_lbl)
        header_bar.set_title_widget(title_box)

        toolbar_view.add_top_bar(header_bar)

        # Main Split Box (Sidebar + Content View)
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        
        # 1. Left Compact Sidebar
        self.dev_buttons = []
        self.dev_labels = []
        self.sidebar_dev_box = None
        sidebar = self._build_sidebar()
        main_box.append(sidebar)

        # 2. Main Content Stack
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)

        self.mixer_view = MixerMatrixView(self.pipewire_mgr, self.peak_monitor, self.hardware_mgr)
        self.stack.add_named(self.mixer_view, "mixes")

        self.device_view = DeviceSettingsView(self.hardware_mgr, self.peak_monitor, on_device_renamed=self._refresh_sidebar_devices)
        self.stack.add_named(self.device_view, "device")

        self.effects_view = EffectsView()
        self.stack.add_named(self.effects_view, "effects")

        self.settings_view = SettingsView(self.hardware_mgr)
        self.stack.add_named(self.settings_view, "settings")

        main_box.append(self.stack)

        toolbar_view.set_content(main_box)
        self.set_content(toolbar_view)

        self.hardware_mgr.on_device_renamed_callback = lambda *a: GLib.idle_add(self._refresh_sidebar_devices)

    def _build_sidebar(self) -> Gtk.Box:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sidebar.add_css_class("wave-sidebar")
        sidebar.set_size_request(200, -1)
        sidebar.set_hexpand(False)

        # Section 1: Connected Devices (Audio Only)
        sec1_lbl = Gtk.Label(label="Audio Devices")
        sec1_lbl.add_css_class("wave-sidebar-section-title")
        sec1_lbl.set_halign(Gtk.Align.START)
        sidebar.append(sec1_lbl)

        self.sidebar_dev_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._populate_sidebar_devices()
        sidebar.append(self.sidebar_dev_box)

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
        for btn in self.dev_buttons:
            btn.remove_css_class("selected")
        self.fx_btn.remove_css_class("selected")
        self.settings_btn.remove_css_class("selected")

        if name == "mixes":
            self.mixes_btn.add_css_class("selected")
        elif name == "device":
            if self.dev_buttons:
                self.dev_buttons[0].add_css_class("selected")
        elif name == "effects":
            self.fx_btn.add_css_class("selected")
        elif name == "settings":
            self.settings_btn.add_css_class("selected")

    def _populate_sidebar_devices(self):
        self.dev_buttons = []
        for dev in self.hardware_mgr.connected_audio_devices:
            dev_btn = Gtk.Button()
            dev_btn.add_css_class("flat")
            dev_btn.add_css_class("wave-sidebar-row")
            
            dev_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            dev_icon = Gtk.Image.new_from_icon_name(dev.get("icon", "audio-input-microphone-symbolic"))
            
            display_name = self.hardware_mgr.get_device_display_name(dev)
            real_name = dev.get("name", "Audio Device")

            dev_lbl = Gtk.Label(label=display_name)
            dev_lbl.set_ellipsize(Pango.EllipSizeMode.END)
            dev_lbl.set_max_width_chars(15)
            dev_lbl.set_hexpand(True)
            dev_lbl.set_halign(Gtk.Align.START)
            
            tooltip = f"{display_name} ({real_name})" if display_name != real_name else real_name
            dev_btn.set_tooltip_text(tooltip)

            dev_box.append(dev_icon)
            dev_box.append(dev_lbl)
            dev_btn.set_child(dev_box)
            dev_btn.connect("clicked", lambda b, d=dev: self._switch_view("device"))
            self.sidebar_dev_box.append(dev_btn)
            self.dev_buttons.append(dev_btn)

    def _refresh_sidebar_devices(self):
        if not self.sidebar_dev_box:
            return
        while self.sidebar_dev_box.get_first_child():
            self.sidebar_dev_box.remove(self.sidebar_dev_box.get_first_child())
        self._populate_sidebar_devices()
        if hasattr(self, "device_view") and self.device_view:
            self.device_view.refresh_device_names()
        if hasattr(self, "mixer_view") and self.mixer_view:
            self.mixer_view.refresh_device_names()

