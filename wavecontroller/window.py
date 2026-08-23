import os
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, Pango, GLib

from .views.mixer_matrix import MixerMatrixView
from .views.device_settings import UnifiedDeviceSettingsView, AddDeviceDialog
from .views.effects_view import EffectsView
from .views.settings_view import SettingsView
from .engine.config_manager import config_manager

class WaveMainWindow(Adw.ApplicationWindow):
    """
    Main WaveController Desktop Window with unified Adw.HeaderBar, compact sidebar,
    user-curated Audio Devices Hub (Duplex/Input/Output), and multi-mix matrix sub-mixing layout.
    """
    def __init__(self, app, pipewire_mgr, peak_monitor, hardware_mgr, **kwargs):
        super().__init__(application=app, title="WaveController", **kwargs)
        self.pipewire_mgr = pipewire_mgr
        self.peak_monitor = peak_monitor
        self.hardware_mgr = hardware_mgr

        self.device_views = {}
        self.device_buttons = {}

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

        self.effects_view = EffectsView()
        self.stack.add_named(self.effects_view, "effects")

        self.settings_view = SettingsView(self.hardware_mgr)
        self.stack.add_named(self.settings_view, "settings")

        main_box.append(self.stack)

        toolbar_view.set_content(main_box)
        self.set_content(toolbar_view)

        # Populate dynamic device views and sidebar list
        self._rebuild_device_views()

        self.hardware_mgr.on_device_renamed_callback = lambda *a: GLib.idle_add(self._refresh_sidebar_device_names)
        self.hardware_mgr.on_devices_changed_callback = lambda *a: GLib.idle_add(self._rebuild_device_views)

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
        sidebar.set_size_request(210, -1)
        sidebar.set_hexpand(False)

        # Section 1: Audio Hardware Devices Header with [+] button
        sec1_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sec1_box.set_margin_start(16)
        sec1_box.set_margin_end(12)
        sec1_box.set_margin_top(14)
        sec1_box.set_margin_bottom(6)

        sec1_lbl = Gtk.Label(label="Audio Devices")
        sec1_lbl.add_css_class("wave-sidebar-section-title")
        sec1_lbl.set_margin_start(0)
        sec1_lbl.set_margin_end(0)
        sec1_lbl.set_margin_top(0)
        sec1_lbl.set_margin_bottom(0)
        sec1_lbl.set_halign(Gtk.Align.START)
        sec1_lbl.set_hexpand(True)
        sec1_box.append(sec1_lbl)

        add_header_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        add_header_btn.add_css_class("flat")
        add_header_btn.add_css_class("wave-icon-btn")
        add_header_btn.set_tooltip_text("Add Audio Device")
        add_header_btn.connect("clicked", lambda b: self._open_add_device_dialog())
        sec1_box.append(add_header_btn)
        sidebar.append(sec1_box)

        # Container for tracked device list rows
        self.device_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sidebar.append(self.device_list_box)

        # Add Audio Device Row Button
        self.add_device_row_btn = Gtk.Button()
        self.add_device_row_btn.add_css_class("flat")
        self.add_device_row_btn.add_css_class("wave-sidebar-row")
        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        add_icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
        add_lbl = Gtk.Label(label="Add Audio Device...")
        add_lbl.set_halign(Gtk.Align.START)
        add_lbl.set_hexpand(True)
        add_box.append(add_icon)
        add_box.append(add_lbl)
        self.add_device_row_btn.set_child(add_box)
        self.add_device_row_btn.connect("clicked", lambda b: self._open_add_device_dialog())
        sidebar.append(self.add_device_row_btn)

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

    def _rebuild_device_views(self, select_device_key: str = None):
        """Rebuilds the sidebar device buttons and views stack for all tracked devices."""
        # Clear previous dynamic buttons
        while True:
            child = self.device_list_box.get_first_child()
            if not child:
                break
            self.device_list_box.remove(child)

        self.device_buttons.clear()

        # Remove old device views from stack
        for view_name in list(self.device_views.keys()):
            view = self.device_views.pop(view_name)
            if self.stack.get_child_by_name(view_name):
                self.stack.remove(view)

        tracked_devices = self.hardware_mgr.get_tracked_devices()

        for dev in tracked_devices:
            key = dev["device_key"]
            view_name = f"device_{key}"

            view = UnifiedDeviceSettingsView(
                device_info=dev,
                hardware_mgr=self.hardware_mgr,
                peak_monitor=self.peak_monitor,
                pipewire_mgr=self.pipewire_mgr,
                on_device_renamed=self._refresh_sidebar_device_names,
                on_device_removed=self._on_device_removed
            )
            self.device_views[view_name] = view
            self.stack.add_named(view, view_name)

            # Create Sidebar Row Button
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.add_css_class("wave-sidebar-row")

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

            icon_name = dev.get("icon", "audio-headset-symbolic")
            icon_img = Gtk.Image.new_from_icon_name(icon_name)
            row_box.append(icon_img)

            lbl = Gtk.Label(label=dev.get("display_name", dev.get("name", "Device")))
            lbl.set_halign(Gtk.Align.START)
            lbl.set_hexpand(True)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            row_box.append(lbl)

            badge_text = dev.get("badge", "In/Out")
            badge_lbl = Gtk.Label(label=badge_text)
            badge_lbl.add_css_class("device-badge")
            
            dtype = dev.get("type", "duplex")
            if not dev.get("connected", True):
                badge_lbl.add_css_class("offline")
            elif dtype == "duplex":
                badge_lbl.add_css_class("duplex")
            elif dtype == "input":
                badge_lbl.add_css_class("input")
            elif dtype == "output":
                badge_lbl.add_css_class("output")
            row_box.append(badge_lbl)

            btn.set_child(row_box)
            btn.connect("clicked", lambda b, vn=view_name, bt=btn: self._switch_view(vn, bt))

            self.device_list_box.append(btn)
            self.device_buttons[view_name] = btn

        # Handle View Selection
        if select_device_key:
            target_view_name = f"device_{select_device_key}"
            if target_view_name in self.device_buttons:
                self._switch_view(target_view_name, self.device_buttons[target_view_name])
        else:
            curr_visible = self.stack.get_visible_child_name()
            if curr_visible and curr_visible.startswith("device_") and curr_visible not in self.device_buttons:
                self._switch_view("mixes", self.mixes_btn)

    def _open_add_device_dialog(self):
        dialog = AddDeviceDialog(self.hardware_mgr, on_device_added_callback=self._on_device_added)
        dialog.present(self)

    def _on_device_added(self, device_key: str):
        self._rebuild_device_views(select_device_key=device_key)

    def _on_device_removed(self, device_key: str):
        self._rebuild_device_views()
        self._switch_view("mixes", self.mixes_btn)

    def _switch_view(self, name: str, active_btn=None):
        if self.stack.get_child_by_name(name):
            self.stack.set_visible_child_name(name)
        
        # Clear selected styling from all buttons
        for btn in self.device_buttons.values():
            btn.remove_css_class("selected")
        self.mixes_btn.remove_css_class("selected")
        self.fx_btn.remove_css_class("selected")
        self.settings_btn.remove_css_class("selected")

        if active_btn:
            active_btn.add_css_class("selected")
        elif name in self.device_buttons:
            self.device_buttons[name].add_css_class("selected")
        elif name == "mixes":
            self.mixes_btn.add_css_class("selected")
        elif name == "effects":
            self.fx_btn.add_css_class("selected")
        elif name == "settings":
            self.settings_btn.add_css_class("selected")

    def _refresh_sidebar_device_names(self):
        for view in self.device_views.values():
            view.refresh_device_names()
        if hasattr(self, "mixer_view") and self.mixer_view:
            self.mixer_view.refresh_device_names()
        self._rebuild_device_views()


