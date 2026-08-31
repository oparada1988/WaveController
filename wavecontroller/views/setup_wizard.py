import os
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk

from ..engine.config_manager import config_manager
from ..utils.logger import get_logger

log = get_logger("OOBE")

OOBE_ASSETS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets", "oobe"))


class SetupWizardDialog(Gtk.Window):
    """
    First-Time Setup Wizard (OOBE) Carousel Dialog for configuring primary
    microphone and monitor DAC on initial launch.
    """
    def __init__(self, parent_window=None, hardware_mgr=None, pipewire_mgr=None, on_complete_callback=None):
        top_parent = parent_window if isinstance(parent_window, Gtk.Window) else None
        super().__init__(
            transient_for=top_parent,
            modal=True,
            title="Welcome to WaveController",
            default_width=560,
            default_height=500,
            resizable=False
        )
        self.hardware_mgr = hardware_mgr
        self.pipewire_mgr = pipewire_mgr
        self.on_complete_callback = on_complete_callback

        self.add_css_class("wave-window")
        self.add_css_class("oobe-window")

        # Modal configuration (undecorated, no title bar, can only be closed on completion)
        self.set_decorated(False)
        self.set_deletable(False)
        self._can_close = False
        self.connect("close-request", self._on_close_request)

        # Main Layout Container (Rounded card with shadow)
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_box.add_css_class("oobe-main-container")
        self.set_child(self.main_box)

        # Carousel Container
        self.carousel = Adw.Carousel()
        self.carousel.set_vexpand(True)
        self.carousel.set_hexpand(True)
        self.carousel.set_allow_scroll_wheel(False)
        self.carousel.set_allow_mouse_drag(True)

        # Build 7 Onboarding Pages
        self._build_page_1_welcome()
        self._build_page_2_devices()
        self._build_page_3_mixing_board()
        self._build_page_4_hardware_control()
        self._build_page_5_device_setup()
        self._build_page_6_github()
        self._build_page_7_done()

        self.main_box.append(self.carousel)

        # Bottom Page Indicator Dots
        dots_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        dots_box.set_halign(Gtk.Align.CENTER)
        dots_box.set_margin_bottom(16)

        self.indicator_dots = Adw.CarouselIndicatorDots()
        self.indicator_dots.set_carousel(self.carousel)
        dots_box.append(self.indicator_dots)

        self.main_box.append(dots_box)

        # Keyboard Navigation Controller (Left/Right arrow keys, Space, Enter)
        key_controller = Gtk.EventControllerKey.new()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

    def _on_key_pressed(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Page_Down, Gdk.KEY_space, Gdk.KEY_Return):
            cur = int(round(self.carousel.get_position()))
            if cur < self.carousel.get_n_pages() - 1:
                self._go_to_page(cur + 1)
                return True
        elif keyval in (Gdk.KEY_Left, Gdk.KEY_Page_Up, Gdk.KEY_BackSpace):
            cur = int(round(self.carousel.get_position()))
            if cur > 0:
                self._go_to_page(cur - 1)
                return True
        return False

    def _on_close_request(self, win):
        if not self._can_close:
            return True
        return False

    def _go_to_page(self, page_index: int):
        """Smoothly animates carousel to target page index."""
        if page_index == 4:
            self._refresh_page_5_device_options()
        widget = self.carousel.get_nth_page(page_index)
        if widget:
            self.carousel.scroll_to(widget, True)

    def _build_page_1_welcome(self):
        """Page 1: Welcome Screen."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.add_css_class("oobe-page")
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)

        # App Icon
        icon_path = os.path.join(OOBE_ASSETS_DIR, "app-icon.png")
        if os.path.exists(icon_path):
            img = Gtk.Picture.new_for_filename(icon_path)
            img.set_can_shrink(True)
            img.set_content_fit(Gtk.ContentFit.CONTAIN)
            img.set_size_request(110, 110)
            box.append(img)
        else:
            fallback = Gtk.Image.new_from_icon_name("audio-headphones-symbolic")
            fallback.set_pixel_size(96)
            fallback.add_css_class("accent")
            box.append(fallback)

        # Title
        title = Gtk.Label(label="Welcome to WaveController")
        title.add_css_class("oobe-title")
        title.set_halign(Gtk.Align.CENTER)
        box.append(title)

        # Action Button
        btn = Gtk.Button(label="Get started")
        btn.add_css_class("oobe-action-btn")
        btn.set_size_request(220, 44)
        btn.set_margin_top(12)
        btn.connect("clicked", lambda _: self._go_to_page(1))
        box.append(btn)

        self.carousel.append(box)

    def _build_page_2_devices(self):
        """Page 2: Hardware & Microphones."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.add_css_class("oobe-page")
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)

        # Mics Image
        mics_path = os.path.join(OOBE_ASSETS_DIR, "mics.png")
        if os.path.exists(mics_path):
            img = Gtk.Picture.new_for_filename(mics_path)
            img.set_can_shrink(True)
            img.set_content_fit(Gtk.ContentFit.CONTAIN)
            img.set_size_request(240, 160)
            box.append(img)

        # Description
        desc = Gtk.Label(label="WaveController allows you to manage, and configure your Microphone and Audio devices")
        desc.add_css_class("oobe-description")
        desc.set_wrap(True)
        desc.set_max_width_chars(36)
        desc.set_justify(Gtk.Justification.CENTER)
        desc.set_halign(Gtk.Align.CENTER)
        box.append(desc)

        # Action Button
        btn = Gtk.Button(label="Next")
        btn.add_css_class("oobe-action-btn")
        btn.set_size_request(220, 44)
        btn.connect("clicked", lambda _: self._go_to_page(2))
        box.append(btn)

        self.carousel.append(box)

    def _build_page_3_mixing_board(self):
        """Page 3: Mixing Board with Dynamic Accent Drop Shadow."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.add_css_class("oobe-page")
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)

        # Glow Container Wrapper
        glow_box = Gtk.Box()
        glow_box.add_css_class("oobe-glow-container")
        glow_box.set_halign(Gtk.Align.CENTER)

        # Mix Board Image
        mix_path = os.path.join(OOBE_ASSETS_DIR, "mix-board.png")
        if os.path.exists(mix_path):
            img = Gtk.Picture.new_for_filename(mix_path)
            img.set_can_shrink(True)
            img.set_content_fit(Gtk.ContentFit.CONTAIN)
            img.set_size_request(345, 165)
            img.add_css_class("oobe-preview-img")
            glow_box.append(img)
        box.append(glow_box)

        # Description
        desc = Gtk.Label(label="With a powerful mixing board to fit all of your needs")
        desc.add_css_class("oobe-description")
        desc.set_wrap(True)
        desc.set_max_width_chars(36)
        desc.set_justify(Gtk.Justification.CENTER)
        desc.set_halign(Gtk.Align.CENTER)
        box.append(desc)

        # Action Button
        btn = Gtk.Button(label="Next")
        btn.add_css_class("oobe-action-btn")
        btn.set_size_request(220, 44)
        btn.connect("clicked", lambda _: self._go_to_page(3))
        box.append(btn)

        self.carousel.append(box)

    def _build_page_4_hardware_control(self):
        """Page 4: Hardware Control Overview."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.add_css_class("oobe-page")
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)

        # 1. Gear Icon (White styling)
        gear_icon = Gtk.Image.new_from_icon_name("preferences-system-symbolic")
        gear_icon.set_pixel_size(56)
        gear_icon.add_css_class("oobe-icon-white")
        box.append(gear_icon)

        # 2. Description Header
        desc = Gtk.Label(label="Total hardware control for easy management")
        desc.add_css_class("oobe-description")
        desc.set_halign(Gtk.Align.CENTER)
        box.append(desc)

        # 3. Hardware Device Card Preview with Live Devices
        detected_devices = []
        seen_keys = set()
        if self.hardware_mgr:
            get_inputs_fn = getattr(self.hardware_mgr, "get_all_available_input_devices", None) or self.hardware_mgr.get_tracked_input_devices
            get_outputs_fn = getattr(self.hardware_mgr, "get_all_available_output_devices", None) or self.hardware_mgr.get_tracked_output_devices

            for dev in get_inputs_fn() + get_outputs_fn():
                k = dev.get("device_key", dev.get("name", ""))
                if k and k not in seen_keys:
                    seen_keys.add(k)
                    detected_devices.append(dev)

        if not detected_devices:
            detected_devices = [
                {"display_name": "Elgato Wave XLR", "description": "USB Audio Interface", "icon": "elgato-wave-xlr-symbolic"},
                {"display_name": "Built-in Audio", "description": "Analog Stereo Controller", "icon": "audio-card-symbolic"}
            ]

        hw_card_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        hw_card_box.set_size_request(490, -1)
        hw_card_box.add_css_class("oobe-hw-preview-container")

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(100)
        scrolled.set_max_content_height(140)
        scrolled.set_propagate_natural_height(True)

        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        if not hasattr(self, "_selected_hw_keys"):
            self._selected_hw_keys = set()

        for dev in detected_devices:
            dev_k = dev.get("device_key", dev.get("name", ""))
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_box.add_css_class("oobe-hw-preview-row")

            icon_name = dev.get("icon") or dev.get("icon_name") or "audio-card-symbolic"
            dev_icon = Gtk.Image.new_from_icon_name(icon_name)
            dev_icon.set_pixel_size(24)
            dev_icon.add_css_class("oobe-icon-white")
            row_box.append(dev_icon)

            lbl_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            lbl_vbox.set_hexpand(True)
            title_lbl = Gtk.Label(label=dev.get("display_name", dev.get("name", "Audio Device")))
            title_lbl.set_halign(Gtk.Align.START)
            title_lbl.add_css_class("heading")

            sub_lbl = Gtk.Label(label=dev.get("description", dev.get("display_name", "Hardware Audio Device")))
            sub_lbl.set_halign(Gtk.Align.START)
            sub_lbl.add_css_class("dimmed")
            lbl_vbox.append(title_lbl)
            lbl_vbox.append(sub_lbl)
            row_box.append(lbl_vbox)

            add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
            add_btn.add_css_class("flat")
            add_btn.add_css_class("circular")
            add_btn.set_tooltip_text("Add Device")

            def make_toggle_handler(k, r_box, a_btn):
                def _on_toggle(btn):
                    if k in self._selected_hw_keys:
                        self._selected_hw_keys.remove(k)
                        r_box.remove_css_class("selected")
                        a_btn.remove_css_class("oobe-btn-added")
                        a_btn.set_icon_name("list-add-symbolic")
                        a_btn.set_tooltip_text("Add Device")
                    else:
                        self._selected_hw_keys.add(k)
                        r_box.add_css_class("selected")
                        a_btn.add_css_class("oobe-btn-added")
                        a_btn.set_icon_name("emblem-ok-symbolic")
                        a_btn.set_tooltip_text("Device Added")
                    if hasattr(self, "page4_next_btn"):
                        self.page4_next_btn.set_sensitive(len(self._selected_hw_keys) > 0)
                return _on_toggle

            add_btn.connect("clicked", make_toggle_handler(dev_k, row_box, add_btn))
            row_box.append(add_btn)

            list_box.append(row_box)

        scrolled.set_child(list_box)
        hw_card_box.append(scrolled)
        box.append(hw_card_box)

        # Action Button (Disabled until at least 1 device is added)
        self.page4_next_btn = Gtk.Button(label="Next")
        self.page4_next_btn.add_css_class("oobe-action-btn")
        self.page4_next_btn.set_size_request(220, 44)
        self.page4_next_btn.set_sensitive(len(self._selected_hw_keys) > 0)
        self.page4_next_btn.connect("clicked", lambda _: self._go_to_page(4))
        box.append(self.page4_next_btn)

        self.carousel.append(box)

    def _refresh_page_5_device_options(self):
        """Dynamically populates Page 5 dropdowns strictly based on devices selected on Page 4."""
        if not hasattr(self, "mic_combo") or not hasattr(self, "output_combo"):
            return

        selected_keys = getattr(self, "_selected_hw_keys", set())

        # 1. Inputs
        input_opts = []
        if self.hardware_mgr:
            get_inputs_fn = getattr(self.hardware_mgr, "get_all_available_input_devices", None) or self.hardware_mgr.get_tracked_input_devices
            for dev in get_inputs_fn():
                k = dev.get("device_key", dev.get("name", ""))
                if not selected_keys or k in selected_keys or any(sk in k or k in sk for sk in selected_keys):
                    name = dev.get("display_name", dev.get("name", "Microphone"))
                    input_opts.append((k, name))

        if not input_opts:
            if self.hardware_mgr:
                for dev in (getattr(self.hardware_mgr, "get_all_available_input_devices", None) or self.hardware_mgr.get_tracked_input_devices)():
                    input_opts.append((dev.get("device_key", dev.get("name", "")), dev.get("display_name", dev.get("name", "Microphone"))))
            if not input_opts:
                input_opts = [("default", "Default Microphone (System)")]

        self._input_dev_keys = [opt[0] for opt in input_opts]
        self.mic_combo.set_model(Gtk.StringList.new([opt[1] for opt in input_opts]))
        
        sel_in_idx = 0
        for idx, (k, name) in enumerate(input_opts):
            if "wave" in name.lower() or "elgato" in name.lower() or "wave" in k.lower():
                sel_in_idx = idx
                break
        self.mic_combo.set_selected(sel_in_idx)

        # 2. Outputs
        output_opts = []
        if self.hardware_mgr:
            get_outputs_fn = getattr(self.hardware_mgr, "get_all_available_output_devices", None) or self.hardware_mgr.get_tracked_output_devices
            for dev in get_outputs_fn():
                k = dev.get("device_key", dev.get("name", ""))
                if not selected_keys or k in selected_keys or any(sk in k or k in sk for sk in selected_keys):
                    name = dev.get("display_name", dev.get("name", "Audio Device"))
                    output_opts.append((k, name))

        if not output_opts:
            if self.hardware_mgr:
                for dev in (getattr(self.hardware_mgr, "get_all_available_output_devices", None) or self.hardware_mgr.get_tracked_output_devices)():
                    output_opts.append((dev.get("device_key", dev.get("name", "")), dev.get("display_name", dev.get("name", "Audio Device"))))
            if not output_opts:
                output_opts = [("default", "Default Output (System)")]

        self._output_dev_keys = [opt[0] for opt in output_opts]
        self.output_combo.set_model(Gtk.StringList.new([opt[1] for opt in output_opts]))
        
        sel_out_idx = 0
        for idx, (k, name) in enumerate(output_opts):
            if "wave" in name.lower() or "elgato" in name.lower() or "wave" in k.lower():
                sel_out_idx = idx
                break
        self.output_combo.set_selected(sel_out_idx)

    def _build_page_5_device_setup(self):
        """Page 5: Primary Device Configuration."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.add_css_class("oobe-page")
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)

        # Header Icon (White styling)
        icon_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        icon_box.set_halign(Gtk.Align.CENTER)
        icon_box.set_margin_bottom(2)

        mic_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
        mic_icon.set_pixel_size(44)
        mic_icon.add_css_class("oobe-icon-white")

        spk_icon = Gtk.Image.new_from_icon_name("audio-volume-high-symbolic")
        spk_icon.set_pixel_size(44)
        spk_icon.add_css_class("oobe-icon-white")

        icon_box.append(mic_icon)
        icon_box.append(spk_icon)
        box.append(icon_box)

        # Preference Rows Group
        pref_group = Adw.PreferencesGroup()
        pref_group.add_css_class("oobe-pref-group")
        pref_group.set_margin_top(4)
        pref_group.set_margin_bottom(4)

        self.mic_combo = Adw.ComboRow(title="Primary Input Device")
        self.output_combo = Adw.ComboRow(title="Primary Output Device")

        self._refresh_page_5_device_options()

        pref_group.add(self.mic_combo)
        pref_group.add(self.output_combo)

        pref_box = Gtk.Box()
        pref_box.set_size_request(510, -1)
        pref_box.set_halign(Gtk.Align.CENTER)
        pref_box.append(pref_group)
        box.append(pref_box)

        # Description
        desc = Gtk.Label(label="And works best when setting up default devices for it to manage.")
        desc.add_css_class("oobe-description")
        desc.set_wrap(True)
        desc.set_max_width_chars(38)
        desc.set_justify(Gtk.Justification.CENTER)
        desc.set_halign(Gtk.Align.CENTER)
        desc.set_margin_top(4)
        desc.set_margin_bottom(4)
        box.append(desc)

        # Action Button
        btn = Gtk.Button(label="Next")
        btn.add_css_class("oobe-action-btn")
        btn.set_size_request(220, 44)
        btn.connect("clicked", lambda _: self._go_to_page(5))
        box.append(btn)

        self.carousel.append(box)

    def _build_page_6_github(self):
        """Page 6: Community & GitHub Feedback."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.add_css_class("oobe-page")
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)

        # GitHub Icon
        gh_path = os.path.join(OOBE_ASSETS_DIR, "github.png")
        if os.path.exists(gh_path):
            img = Gtk.Picture.new_for_filename(gh_path)
            img.set_can_shrink(True)
            img.set_content_fit(Gtk.ContentFit.CONTAIN)
            img.set_size_request(100, 100)
            box.append(img)
        else:
            fallback = Gtk.Image.new_from_icon_name("network-wired-symbolic")
            fallback.set_pixel_size(80)
            box.append(fallback)

        # Description
        desc = Gtk.Label(label="Report any issues, or feedback in our Github Repo")
        desc.add_css_class("oobe-description")
        desc.set_wrap(True)
        desc.set_max_width_chars(32)
        desc.set_justify(Gtk.Justification.CENTER)
        desc.set_halign(Gtk.Align.CENTER)
        box.append(desc)

        # Action Button
        btn = Gtk.Button(label="Next")
        btn.add_css_class("oobe-action-btn")
        btn.set_size_request(220, 44)
        btn.connect("clicked", lambda _: self._go_to_page(6))
        box.append(btn)

        self.carousel.append(box)

    def _build_page_7_done(self):
        """Page 7: Final Setup Completion."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.add_css_class("oobe-page")
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)

        # App Icon
        icon_path = os.path.join(OOBE_ASSETS_DIR, "app-icon.png")
        if os.path.exists(icon_path):
            img = Gtk.Picture.new_for_filename(icon_path)
            img.set_can_shrink(True)
            img.set_content_fit(Gtk.ContentFit.CONTAIN)
            img.set_size_request(110, 110)
            box.append(img)

        # Title
        title = Gtk.Label(label="Enjoy!")
        title.add_css_class("oobe-title")
        title.set_halign(Gtk.Align.CENTER)
        box.append(title)

        # Action Button
        btn = Gtk.Button(label="Done")
        btn.add_css_class("oobe-action-btn")
        btn.set_size_request(220, 44)
        btn.set_margin_top(12)
        btn.connect("clicked", self._on_finish_clicked)
        box.append(btn)

        self.carousel.append(box)

    def _on_finish_clicked(self, btn):
        in_idx = self.mic_combo.get_selected()
        out_idx = self.output_combo.get_selected()

        sel_mic_key = self._input_dev_keys[in_idx] if 0 <= in_idx < len(self._input_dev_keys) else "default"
        sel_out_key = self._output_dev_keys[out_idx] if 0 <= out_idx < len(self._output_dev_keys) else "default"

        # Get display names
        mic_name = "Microphone"
        if self.hardware_mgr:
            if hasattr(self.hardware_mgr, "add_tracked_device"):
                for k in getattr(self, "_selected_hw_keys", []):
                    if k and k != "default":
                        self.hardware_mgr.add_tracked_device(k)
                if sel_mic_key != "default":
                    self.hardware_mgr.add_tracked_device(sel_mic_key)
                if sel_out_key != "default":
                    self.hardware_mgr.add_tracked_device(sel_out_key)

            get_inputs_fn = getattr(self.hardware_mgr, "get_all_available_input_devices", None) or self.hardware_mgr.get_tracked_input_devices
            for dev in get_inputs_fn():
                if dev.get("device_key") == sel_mic_key:
                    mic_name = dev.get("display_name", dev.get("name", "Microphone"))
                    break

        log.info(f"Setup Wizard completed: Input={sel_mic_key} ({mic_name}), Output={sel_out_key}")

        # Save config preferences
        config_manager.set("first_run_completed", True)
        config_manager.set("primary_device_key", sel_mic_key if sel_mic_key != "default" else sel_out_key)
        config_manager.set("default_input_device", sel_mic_key)
        config_manager.set("default_output_device", sel_out_key, immediate=True)

        if self.pipewire_mgr:
            if sel_out_key != "default":
                self.pipewire_mgr.provision_default_device_channels_and_mix(device_key=sel_out_key, is_input=False, is_output=True)
            if sel_mic_key != "default":
                self.pipewire_mgr.provision_default_device_channels_and_mix(device_key=sel_mic_key, device_name=mic_name, is_input=True, is_output=False)
            elif sel_out_key != "default":
                self.pipewire_mgr.provision_default_device_channels_and_mix(device_key=sel_out_key, device_name=mic_name, is_input=True, is_output=True)

        self._can_close = True
        self.close()

        if self.on_complete_callback:
            self.on_complete_callback()
