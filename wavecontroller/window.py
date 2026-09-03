import os
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gdk, Pango, GLib

from .views.mixer_matrix import MixerMatrixView
from .views.device_settings import UnifiedDeviceSettingsView, AddDeviceDialog, SelectDefaultDeviceDialog
from .views.effects_view import EffectsView
from .views.settings_view import SettingsView
from .views.setup_wizard import SetupWizardDialog
from .engine.config_manager import config_manager
from .utils.logger import get_logger

log = get_logger("Window")

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
        if self.hardware_mgr and self.pipewire_mgr:
            self.hardware_mgr.pipewire_mgr = self.pipewire_mgr
            self.pipewire_mgr.hardware_mgr = self.hardware_mgr

        self.device_views = {}
        self.device_buttons = {}
        self._sidebar_text_widgets = []
        self._sidebar_collapsed = config_manager.get("sidebar_collapsed", False)

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

        sidebar_toggle = Gtk.Button.new_from_icon_name("sidebar-show-symbolic")
        sidebar_toggle.add_css_class("flat")
        sidebar_toggle.add_css_class("wave-icon-btn")
        sidebar_toggle.set_tooltip_text("Collapse Sidebar")
        sidebar_toggle.connect("clicked", lambda btn: self._toggle_sidebar(btn, sidebar))
        header_bar.pack_start(sidebar_toggle)
        self._sidebar_toggle_btn = sidebar_toggle
        self._apply_sidebar_state(sidebar)

        # 2. Main Content Stack
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)

        self.mixer_view = MixerMatrixView(
            self.pipewire_mgr,
            self.peak_monitor,
            self.hardware_mgr,
            on_mix_list_changed=self._on_mix_list_changed
        )
        self.stack.add_named(self.mixer_view, "mixes")

        self.effects_view = EffectsView()
        self.stack.add_named(self.effects_view, "effects")

        self.settings_view = SettingsView(
            self.hardware_mgr,
            pipewire_mgr=self.pipewire_mgr,
            on_theme_changed=self._apply_theme,
            on_hw_defaults_changed=self._on_system_defaults_changed
        )
        self.stack.add_named(self.settings_view, "settings")

        main_box.append(self.stack)

        toolbar_view.set_content(main_box)
        self.set_content(toolbar_view)

        # Populate dynamic device views and sidebar list
        self._rebuild_device_views()

        self.hardware_mgr.on_device_renamed_callback = lambda *a: GLib.idle_add(self._on_device_renamed)
        self.hardware_mgr.on_devices_changed_callback = lambda *a: GLib.idle_add(self._on_devices_changed)
        self.hardware_mgr.on_new_device_detected_callback = lambda dev_info: GLib.idle_add(self._on_new_device_detected, dev_info)

        # Check for first-time OOBE setup
        GLib.timeout_add(300, self._check_first_run_setup)

        # Check for untracked connected devices (such as Wave XLR) on launch
        GLib.timeout_add(800, self._check_initial_untracked_devices)

    def _apply_theme(self):
        """Applies either the default Midnight Dark theme or follows standard GTK/Libadwaita system theme."""
        use_sys = config_manager.get("use_system_theme", False)
        if use_sys:
            self.remove_css_class("theme-midnight")
        else:
            self.add_css_class("theme-midnight")

    def save_window_state(self, immediate: bool = False):
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
        }, immediate=immediate)

    def _on_window_size_changed(self, *args):
        # Debounced: avoids a disk write on every pixel during a live resize drag
        self.save_window_state()

    def _on_close_request(self, win):
        # Immediate: ensures the final geometry isn't lost if the app quits before the debounce timer fires
        self.save_window_state(immediate=True)
        close_to_tray = config_manager.get("close_to_tray", True)
        if close_to_tray:
            self.set_visible(False)
            return True  # Prevents default destruction and keeps daemon running
        return False

    def _build_sidebar(self) -> Gtk.Box:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        sidebar.add_css_class("wave-sidebar")
        sidebar.set_size_request(68 if self._sidebar_collapsed else 225, -1)
        sidebar.set_hexpand(False)

        # Section 1: Mixes & Effects (Top / 1st Position)
        sec1_lbl = Gtk.Label(label="Mixes & Effects")
        sec1_lbl.add_css_class("wave-sidebar-section-title")
        self._register_sidebar_text(sec1_lbl)
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
        self._register_sidebar_text(mix_lbl)
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
        self._register_sidebar_text(fx_lbl)
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
        self._register_sidebar_text(sec2_dev_lbl)
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
        self._register_sidebar_text(set_lbl)
        set_lbl.set_halign(Gtk.Align.START)
        set_lbl.set_hexpand(True)
        set_box.append(set_icon)
        set_box.append(set_lbl)
        self.settings_btn.set_child(set_box)
        self.settings_btn.connect("clicked", lambda b: self._switch_view("settings", self.settings_btn))
        sidebar.append(self.settings_btn)

        sidebar.set_margin_bottom(12)
        return sidebar

    def _register_sidebar_text(self, widget):
        self._sidebar_text_widgets.append(widget)

    def _toggle_sidebar(self, button, sidebar):
        self._sidebar_collapsed = not self._sidebar_collapsed
        config_manager.set("sidebar_collapsed", self._sidebar_collapsed, immediate=True)
        self._animate_sidebar_state(sidebar)

    def _apply_sidebar_state(self, sidebar):
        """Instantly apply the collapsed/expanded state (used on initial build, no animation)."""
        collapsed = self._sidebar_collapsed
        sidebar.set_size_request(68 if collapsed else 225, -1)
        for widget in self._sidebar_text_widgets:
            if widget.get_parent() is not None:
                widget.set_visible(not collapsed)
        self._update_sidebar_toggle_button(collapsed)
        sidebar.remove_css_class("sidebar-collapsed")
        if collapsed:
            sidebar.add_css_class("sidebar-collapsed")

    def _update_sidebar_toggle_button(self, collapsed):
        if hasattr(self, "_sidebar_toggle_btn"):
            self._sidebar_toggle_btn.set_icon_name("sidebar-show-symbolic")
            self._sidebar_toggle_btn.set_tooltip_text(
                "Expand Sidebar" if collapsed else "Collapse Sidebar"
            )

    def _animate_sidebar_state(self, sidebar):
        """Animates the sidebar width smoothly between collapsed/expanded states."""
        collapsed = self._sidebar_collapsed
        target_width = 68 if collapsed else 225
        start_width = sidebar.get_width() or (225 if collapsed else 68)

        self._update_sidebar_toggle_button(collapsed)
        sidebar.remove_css_class("sidebar-collapsed")
        if collapsed:
            # Hide labels immediately so they don't wrap/overflow while shrinking
            for widget in self._sidebar_text_widgets:
                if widget.get_parent() is not None:
                    widget.set_visible(False)

        def on_tick(value):
            sidebar.set_size_request(int(value), -1)

        def on_done(*_args):
            sidebar.set_size_request(target_width, -1)
            if collapsed:
                sidebar.add_css_class("sidebar-collapsed")
            else:
                for widget in self._sidebar_text_widgets:
                    if widget.get_parent() is not None:
                        widget.set_visible(True)

        if getattr(self, "_sidebar_animation", None):
            self._sidebar_animation.pause()

        target = Adw.CallbackAnimationTarget.new(on_tick)
        animation = Adw.TimedAnimation.new(sidebar, start_width, target_width, 220, target)
        animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        animation.connect("done", on_done)
        self._sidebar_animation = animation
        animation.play()

    def _teardown_device_views(self):
        """Removes all cached device views from the stack and unregisters their hardware listeners."""
        for v_name, v in list(self.device_views.items()):
            if self.stack.get_child_by_name(v_name):
                self.stack.remove(v)
            if hasattr(v, "cleanup"):
                v.cleanup()
        self.device_views.clear()

    def _rebuild_device_views(self, select_device_key: str = None):
        """Rebuilds the sidebar device buttons and views stack for all tracked devices."""
        # 1. Capture the currently active view name BEFORE tearing down views
        target_view = None
        if select_device_key:
            target_view = f"device_{select_device_key}"
        else:
            curr_visible = self.stack.get_visible_child_name()
            if curr_visible:
                target_view = curr_visible

        # Clear previous dynamic buttons
        while True:
            child = self.device_list_box.get_first_child()
            if not child:
                break
            self.device_list_box.remove(child)

        self.device_buttons.clear()

        tracked_devices = self.hardware_mgr.get_tracked_devices()
        tracked_view_names = set()

        for dev in tracked_devices:
            key = dev["device_key"]
            view_name = f"device_{key}"
            tracked_view_names.add(view_name)

            # In-place view reuse: update existing view without tearing it down from stack
            if view_name in self.device_views:
                view = self.device_views[view_name]
                if hasattr(view, "update_device_info"):
                    view.update_device_info(dev)
            else:
                view = UnifiedDeviceSettingsView(
                    device_info=dev,
                    hardware_mgr=self.hardware_mgr,
                    peak_monitor=self.peak_monitor,
                    pipewire_mgr=self.pipewire_mgr,
                    on_device_renamed=self._refresh_sidebar_device_names,
                    on_device_removed=self._on_device_removed,
                    on_make_default=self._on_make_device_default
                )
                self.device_views[view_name] = view
                self.stack.add_named(view, view_name)

            # Create Sidebar Row Button
            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.add_css_class("wave-sidebar-row")

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

            is_connected = dev.get("connected", True)
            icon_name = dev.get("icon", "audio-headset-symbolic")
            icon_img = Gtk.Image.new_from_icon_name(icon_name)
            icon_img.set_pixel_size(24)
            if not is_connected:
                icon_img.set_opacity(0.55)
            row_box.append(icon_img)

            lbl = Gtk.Label(label=dev.get("display_name", dev.get("name", "Device")))
            self._register_sidebar_text(lbl)
            lbl.set_visible(not self._sidebar_collapsed)
            lbl.set_halign(Gtk.Align.START)
            lbl.set_hexpand(True)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            row_box.append(lbl)

            dtype = dev.get("type", "duplex")
            if dtype == "input":
                badge_text = "Input"
            elif dtype == "output":
                badge_text = "Output"
            else:
                badge_text = "In / Out"

            badge_lbl = Gtk.Label(label=badge_text)
            badge_lbl.add_css_class("device-badge")
            self._register_sidebar_text(badge_lbl)
            badge_lbl.set_visible(not self._sidebar_collapsed)
            
            if not is_connected:
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

        # Remove only deleted device views from stack
        for old_view_name in list(self.device_views.keys()):
            if old_view_name not in tracked_view_names:
                old_view = self.device_views.pop(old_view_name)
                if self.stack.get_child_by_name(old_view_name):
                    self.stack.remove(old_view)
                if hasattr(old_view, "cleanup"):
                    old_view.cleanup()

        # Handle View Selection & Persistence
        if target_view and target_view in self.device_buttons:
            self._switch_view(target_view, self.device_buttons[target_view])
        elif target_view in ("mixes", "settings", "effects"):
            btn = self.mixes_btn if target_view == "mixes" else (self.fx_btn if target_view == "effects" else self.settings_btn)
            self._switch_view(target_view, btn)
        else:
            self._switch_view("mixes", self.mixes_btn)

        if hasattr(self, "mixer_view") and self.mixer_view:
            self.mixer_view.refresh_device_names()

    def _show_device_context_menu(self, widget, device_key: str):
        pop = Gtk.Popover()
        pop.set_parent(widget)
        pop.set_autohide(True)
        pop.set_cascade_popdown(True)
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

    def _on_make_device_default(self, device_key: str):
        """Promotes a device to primary default, provisions Mic channel & Personal Mix, and rebuilds views."""
        log.info(f"[WaveController.Window] _on_make_device_default invoked for device '{device_key}'")
        config_manager.set("default_selection_dismissed", False, immediate=True)

        if self.hardware_mgr:
            self.hardware_mgr.set_primary_default_device(device_key)
            dev_name = self.hardware_mgr.get_device_display_name(device_key)
            dev_info = self.hardware_mgr.discovered_devices.get(device_key, {})
            dtype = dev_info.get("type", "duplex")
            has_in = dtype in ("input", "duplex") or bool(dev_info.get("sources") or dev_info.get("primary_source_id"))
            has_out = dtype in ("output", "duplex") or bool(dev_info.get("sinks") or dev_info.get("primary_sink_id"))
            log.info(f"[WaveController.Window] Device '{device_key}' ({dev_name}): type={dtype}, has_in={has_in}, has_out={has_out}")

            if self.pipewire_mgr:
                log.info(f"[WaveController.Window] Dispatching provision_default_device_channels_and_mix for '{dev_name}' (device_key={device_key})")
                self.pipewire_mgr.provision_default_device_channels_and_mix(
                    device_key=device_key,
                    device_name=dev_name,
                    is_input=has_in,
                    is_output=has_out
                )

        # Destroy cached views to recreate them with updated default state
        self._teardown_device_views()

        self._rebuild_device_views()
        if hasattr(self, "settings_view") and hasattr(self.settings_view, "refresh_device_list"):
            self.settings_view.refresh_device_list()
        if hasattr(self, "mixer_view") and self.mixer_view:
            self.mixer_view.rebuild_matrix()
        self._switch_view("mixes", self.mixes_btn)

    def _on_device_added(self, device_key: str):
        has_default = self.hardware_mgr.has_default_device() if self.hardware_mgr else False
        log.info(f"[WaveController.Window] _on_device_added '{device_key}': has_default={has_default}")

        # If no default device is active (e.g. 0 devices previously existed or default was deleted), prompt user to make it default
        if not has_default:
            dev_name = self.hardware_mgr.get_device_display_name(device_key) if self.hardware_mgr else "Audio Device"
            log.info(f"[WaveController.Window] Presenting Make Default dialog for '{dev_name}' ({device_key})")
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading=f"Make '{dev_name}' Default Device?",
                body=f"Would you like to set '{dev_name}' as your primary default device? This will create a dedicated Microphone channel and Personal Mix for this device."
            )
            dialog.add_response("secondary", "Add as Secondary")
            dialog.add_response("default", "Make Default")
            dialog.set_response_appearance("default", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("default")

            def _on_resp(d, resp):
                log.info(f"[WaveController.Window] Make Default dialog response: '{resp}' for '{device_key}'")
                if resp == "default":
                    GLib.idle_add(lambda: self._on_make_device_default(device_key))
                else:
                    self._rebuild_device_views(select_device_key=device_key)
                    if hasattr(self, "settings_view") and hasattr(self.settings_view, "refresh_device_list"):
                        self.settings_view.refresh_device_list()
                    if hasattr(self, "mixer_view") and self.mixer_view:
                        self.mixer_view.refresh_device_names()
                    self._switch_view("mixes", self.mixes_btn)

            dialog.connect("response", _on_resp)
            dialog.present()
        else:
            log.info(f"[WaveController.Window] Default device already active. Adding '{device_key}' as secondary device.")
            self._rebuild_device_views(select_device_key=device_key)
            if hasattr(self, "settings_view") and hasattr(self.settings_view, "refresh_device_list"):
                self.settings_view.refresh_device_list()
            if hasattr(self, "mixer_view") and self.mixer_view:
                self.mixer_view.refresh_device_names()
            self._switch_view("mixes", self.mixes_btn)

    def _on_device_removed(self, device_key: str):
        is_default = self.hardware_mgr.is_default_device(device_key) if self.hardware_mgr and hasattr(self.hardware_mgr, "is_default_device") else False
        log.info(f"[WaveController.Window] _on_device_removed '{device_key}': is_default={is_default}")

        if not is_default:
            # Secondary/tertiary device removed: remove any channels and mixes associated with this secondary device
            log.info(f"[WaveController.Window] Secondary device '{device_key}' removed. Removing tied channels/mixes and keeping remaining configuration.")
            if self.pipewire_mgr:
                self.pipewire_mgr.remove_device_associated_channels_and_mixes(device_key)

            self._teardown_device_views()

            self._rebuild_device_views()
            if hasattr(self, "settings_view") and hasattr(self.settings_view, "refresh_device_list"):
                self.settings_view.refresh_device_list()
            if hasattr(self, "mixer_view") and self.mixer_view:
                self.mixer_view.rebuild_matrix()
            self._switch_view("mixes", self.mixes_btn)
            return

        # Default device was removed:
        remaining_tracked = self.hardware_mgr.get_remaining_tracked_devices(device_key) if self.hardware_mgr else []
        log.info(f"[WaveController.Window] Default device '{device_key}' removed. Remaining tracked: {len(remaining_tracked)} devices.")

        if not remaining_tracked:
            # Case 1: Zero remaining devices -> remove Personal Mix & Microphone channel
            log.info("[WaveController.Window] Zero devices remaining. Removing default Personal Mix and Mic channel.")
            if self.pipewire_mgr:
                self.pipewire_mgr.remove_default_device_channels_and_mix()
            config_manager.set("default_input_device", "", immediate=False)
            config_manager.set("default_output_device", "", immediate=False)
            config_manager.set("primary_device_key", "", immediate=False)
            config_manager.set("default_selection_dismissed", False, immediate=True)

            self._teardown_device_views()

            self._rebuild_device_views()
            if hasattr(self, "settings_view") and hasattr(self.settings_view, "refresh_device_list"):
                self.settings_view.refresh_device_list()
            if hasattr(self, "mixer_view") and self.mixer_view:
                self.mixer_view.rebuild_matrix()
            self._switch_view("mixes", self.mixes_btn)
        else:
            # Case 2: Remaining devices exist -> prompt user to pick the new default device
            log.info(f"[WaveController.Window] Presenting SelectDefaultDeviceDialog for remaining devices: {[d.get('name') for d in remaining_tracked]}")
            def _on_new_default_selected(new_dev_key: str):
                log.info(f"[WaveController.Window] New default device selected from modal: '{new_dev_key}'")
                if self.pipewire_mgr:
                    self.pipewire_mgr.remove_default_device_channels_and_mix()
                GLib.idle_add(lambda: self._on_make_device_default(new_dev_key))

            def _on_default_selection_cancelled():
                # User declined to make any remaining device default -> remove orphaned channels/mixes
                log.info("[WaveController.Window] Default device selection modal cancelled. Removing default channels and flagging default_selection_dismissed=True")
                if self.pipewire_mgr:
                    self.pipewire_mgr.remove_default_device_channels_and_mix()
                config_manager.set("default_input_device", "", immediate=False)
                config_manager.set("default_output_device", "", immediate=False)
                config_manager.set("primary_device_key", "", immediate=False)
                config_manager.set("default_selection_dismissed", True, immediate=True)

                # Recreate device views so "Make Default" button appears on remaining devices
                self._teardown_device_views()

                self._rebuild_device_views()
                if hasattr(self, "settings_view") and hasattr(self.settings_view, "refresh_device_list"):
                    self.settings_view.refresh_device_list()
                if hasattr(self, "mixer_view") and self.mixer_view:
                    self.mixer_view.rebuild_matrix()
                self._switch_view("mixes", self.mixes_btn)

            dialog = SelectDefaultDeviceDialog(
                self.hardware_mgr,
                remaining_devices=remaining_tracked,
                on_selected_callback=_on_new_default_selected,
                on_cancel_callback=_on_default_selection_cancelled
            )
            dialog.set_transient_for(self)
            dialog.present()

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
            if hasattr(self, "settings_view") and hasattr(self.settings_view, "refresh_device_list"):
                self.settings_view.refresh_device_list()

    def _refresh_sidebar_device_names(self):
        for view in self.device_views.values():
            view.refresh_device_names()
        if hasattr(self, "mixer_view") and self.mixer_view:
            self.mixer_view.refresh_device_names()
        self._rebuild_device_views()

    def _on_devices_changed(self):
        self._rebuild_device_views()
        if hasattr(self, "mixer_view") and self.mixer_view:
            self.mixer_view.refresh_device_names()
            self.mixer_view.refresh_all_faders()
        if hasattr(self, "settings_view") and hasattr(self.settings_view, "refresh_device_list"):
            self.settings_view.refresh_device_list()

    def _on_system_defaults_changed(self):
        if hasattr(self, "mixer_view") and self.mixer_view:
            self.mixer_view.refresh_all_faders()
        if hasattr(self, "settings_view") and hasattr(self.settings_view, "refresh_mix_defaults"):
            self.settings_view.refresh_mix_defaults()

    def _on_mix_list_changed(self):
        if hasattr(self, "settings_view") and hasattr(self.settings_view, "refresh_mix_defaults"):
            self.settings_view.refresh_mix_defaults()

    def _on_device_renamed(self, *a):
        self._refresh_sidebar_device_names()

    def _check_initial_untracked_devices(self) -> bool:
        if not config_manager.get("first_run_completed", False):
            return False
        untracked = self.hardware_mgr.get_available_untracked_devices()
        for dev in untracked:
            if dev.get("is_elgato"):
                self.show_device_detected_dialog(dev)
                break
        return False

    def _on_new_device_detected(self, dev_info: dict):
        if not self.get_visible() or not config_manager.get("first_run_completed", False):
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
                log.info(f"[WaveController.Window] Auto-detection dialog confirmed: Adding device '{device_key}'")
                self.hardware_mgr.add_tracked_device(device_key)
                self._on_device_added(device_key)

        dialog.connect("response", _on_response)
        dialog.present()

    def _check_first_run_setup(self):
        first_run_done = config_manager.get("first_run_completed", False)
        if not first_run_done:
            wizard = SetupWizardDialog(
                parent_window=self,
                hardware_mgr=self.hardware_mgr,
                pipewire_mgr=self.pipewire_mgr,
                on_complete_callback=self._on_setup_wizard_completed
            )
            wizard.present()
        return False

    def _on_setup_wizard_completed(self):
        self._rebuild_device_views()
        if hasattr(self, "mixer_view"):
            self.mixer_view.rebuild_matrix()


