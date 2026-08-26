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
        saved_w = max(int(win_state.get("width", 1280)), 800)
        saved_h = max(int(win_state.get("height", 780)), 500)
        self._last_unmaximized_width = saved_w
        self._last_unmaximized_height = saved_h

        self.set_default_size(saved_w, saved_h)
        if win_state.get("maximized", False):
            self.maximize()

        self.add_css_class("wave-window")
        self.set_icon_name("com.oparada.WaveController")
        self._apply_theme()
        self.connect("close-request", self._on_close_request)
        self.connect("notify::default-width", self._on_window_size_changed)
        self.connect("notify::default-height", self._on_window_size_changed)
        self.connect("notify::maximized", self._on_window_size_changed)
        self.connect("notify::visible", self._on_window_size_changed)

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

        self.settings_view = SettingsView(self.hardware_mgr, on_theme_changed=self._apply_theme)
        self.stack.add_named(self.settings_view, "settings")

        main_box.append(self.stack)

        toolbar_view.set_content(main_box)
        self.set_content(toolbar_view)

        # Populate dynamic device views and sidebar list
        self._rebuild_device_views()

        self.hardware_mgr.on_device_renamed_callback = lambda *a: GLib.idle_add(self._refresh_sidebar_device_names)
        self.hardware_mgr.on_devices_changed_callback = lambda *a: GLib.idle_add(self._rebuild_device_views)
        self.hardware_mgr.on_new_device_detected_callback = lambda dev_info: GLib.idle_add(self._on_new_device_detected, dev_info)

        # Check for untracked connected devices (such as Wave XLR) on launch
        GLib.timeout_add(800, self._check_initial_untracked_devices)

    def _apply_theme(self):
        """Applies either the default Midnight Dark theme or follows standard GTK/Libadwaita system theme."""
        use_sys = config_manager.get("use_system_theme", False)
        if use_sys:
            self.remove_css_class("theme-midnight")
        else:
            self.add_css_class("theme-midnight")

    def save_window_state(self):
        """Persists current window geometry, unmaximized bounds, and maximized status."""
        is_max = self.is_maximized()
        w = self.get_width()
        h = self.get_height()
        if not is_max and w >= 400 and h >= 300:
            self._last_unmaximized_width = w
            self._last_unmaximized_height = h

        target_w = self._last_unmaximized_width or 1280
        target_h = self._last_unmaximized_height or 780

        config_manager.set("window_state", {
            "width": target_w,
            "height": target_h,
            "maximized": is_max
        }, immediate=True)

    def _on_window_size_changed(self, *args):
        self.save_window_state()

    def _on_close_request(self, win):
        self.save_window_state()
        close_to_tray = config_manager.get("close_to_tray", True)
        if close_to_tray:
            self.set_visible(False)
            return True  # Prevents default destruction and keeps daemon running
        return False

    def _build_sidebar(self) -> Gtk.Box:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sidebar.add_css_class("wave-sidebar")
        sidebar.set_size_request(225, -1)
        sidebar.set_hexpand(False)

        # Section 1: Mixes & Effects (Top / 1st Position)
        sec1_lbl = Gtk.Label(label="Mixes & Effects")
        sec1_lbl.add_css_class("wave-sidebar-section-title")
        sec1_lbl.set_halign(Gtk.Align.START)
        sidebar.append(sec1_lbl)

        self.mixes_btn = Gtk.Button()
        self.mixes_btn.add_css_class("flat")
        self.mixes_btn.add_css_class("wave-sidebar-row")
        self.mixes_btn.add_css_class("selected")
        
        mix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        mix_icon = Gtk.Image.new_from_icon_name("view-grid-symbolic")
        mix_icon.set_pixel_size(24)
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
        
        fx_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        fx_icon = Gtk.Image.new_from_icon_name("system-run-symbolic")
        fx_icon.set_pixel_size(24)
        fx_lbl = Gtk.Label(label="Audio Effects (DSP)")
        fx_lbl.set_halign(Gtk.Align.START)
        fx_lbl.set_hexpand(True)
        fx_box.append(fx_icon)
        fx_box.append(fx_lbl)
        self.fx_btn.set_child(fx_box)
        self.fx_btn.connect("clicked", lambda b: self._switch_view("effects", self.fx_btn))
        sidebar.append(self.fx_btn)

        # Section 2: Audio Hardware Devices Header with [+] button (2nd Position)
        sec2_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sec2_box.set_margin_start(16)
        sec2_box.set_margin_end(12)
        sec2_box.set_margin_top(14)
        sec2_box.set_margin_bottom(6)

        sec2_dev_lbl = Gtk.Label(label="Audio Devices")
        sec2_dev_lbl.add_css_class("wave-sidebar-section-title")
        sec2_dev_lbl.set_margin_start(0)
        sec2_dev_lbl.set_margin_end(0)
        sec2_dev_lbl.set_margin_top(0)
        sec2_dev_lbl.set_margin_bottom(0)
        sec2_dev_lbl.set_halign(Gtk.Align.START)
        sec2_dev_lbl.set_hexpand(True)
        sec2_box.append(sec2_dev_lbl)

        add_header_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        add_header_btn.add_css_class("flat")
        add_header_btn.add_css_class("wave-icon-btn")
        add_header_btn.set_tooltip_text("Add Audio Device")
        add_header_btn.connect("clicked", lambda b: self._open_add_device_dialog())
        sec2_box.append(add_header_btn)
        sidebar.append(sec2_box)

        # Container for tracked device list rows
        self.device_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sidebar.append(self.device_list_box)

        sidebar.append(Gtk.Box(vexpand=True)) # Spacer

        # Bottom Footer Navigation: Settings
        self.settings_btn = Gtk.Button()
        self.settings_btn.add_css_class("flat")
        self.settings_btn.add_css_class("wave-sidebar-row")
        
        set_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        set_icon = Gtk.Image.new_from_icon_name("emblem-system-symbolic")
        set_icon.set_pixel_size(24)
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

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

            icon_name = dev.get("icon", "audio-headset-symbolic")
            icon_img = Gtk.Image.new_from_icon_name(icon_name)
            icon_img.set_pixel_size(24)
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

            # Right-click context menu
            click_gesture = Gtk.GestureClick()
            click_gesture.set_button(3) # Secondary / Right-click
            click_gesture.connect("pressed", lambda g, n, x, y, k=key, b=btn: self._show_device_context_menu(b, k))
            btn.add_controller(click_gesture)

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

        if hasattr(self, "mixer_view") and self.mixer_view:
            self.mixer_view.refresh_device_names()

    def _show_device_context_menu(self, widget, device_key: str):
        pop = Gtk.Popover()
        pop.set_parent(widget)
        pop.add_css_class("wave-popover")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        display_name = self.hardware_mgr.get_device_display_name(device_key)

        # Open Settings Option
        open_btn = Gtk.Button(label=f"Configure {display_name}")
        open_btn.set_icon_name("emblem-system-symbolic")
        open_btn.add_css_class("flat")
        open_btn.connect("clicked", lambda b: (pop.popdown(), self._switch_view(f"device_{device_key}", self.device_buttons.get(f"device_{device_key}"))))
        box.append(open_btn)

        # Remove Device Option
        rem_btn = Gtk.Button(label="Remove from WaveController")
        rem_btn.set_icon_name("user-trash-symbolic")
        rem_btn.add_css_class("flat")
        rem_btn.add_css_class("destructive-action")
        rem_btn.connect("clicked", lambda b: (pop.popdown(), self.hardware_mgr.remove_tracked_device(device_key), self._on_device_removed(device_key)))
        box.append(rem_btn)

        pop.set_child(box)
        pop.popup()

    def _open_add_device_dialog(self):
        dialog = AddDeviceDialog(self.hardware_mgr, on_device_added_callback=self._on_device_added)
        dialog.set_transient_for(self)
        dialog.present()

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

    def _check_initial_untracked_devices(self) -> bool:
        untracked = self.hardware_mgr.get_available_untracked_devices()
        for dev in untracked:
            if dev.get("is_elgato"):
                self.show_device_detected_dialog(dev)
                break
        return False

    def _on_new_device_detected(self, dev_info: dict):
        if not self.get_visible():
            return
        self.show_device_detected_dialog(dev_info)

    def show_device_detected_dialog(self, dev_info: dict):
        device_name = dev_info.get("name", "Wave XLR")
        device_key = dev_info.get("device_key", "")

        tracked = config_manager.get("tracked_devices", [])
        if device_key in tracked:
            return

        if not hasattr(self, "_prompted_devices"):
            self._prompted_devices = set()
        if device_key in self._prompted_devices:
            return
        self._prompted_devices.add(device_key)

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=f"{device_name} Detected",
            body=f"{device_name} Detected, would you like to add the device to WaveController?"
        )
        dialog.add_response("dismiss", "Dismiss")
        dialog.add_response("add", "Add")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("add")
        dialog.set_close_response("dismiss")

        def _on_response(d, response_id):
            if response_id == "add":
                self.hardware_mgr.add_tracked_device(device_key)
                self._rebuild_device_views(select_device_key=device_key)

        dialog.connect("response", _on_response)
        dialog.present()


