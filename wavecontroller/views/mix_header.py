import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GObject, Adw
from wavecontroller.engine.config_manager import config_manager

AVAILABLE_MIX_COLORS = [
    ("#9146ff", "Stream Purple"),
    ("#3584e4", "Ocean Blue"),
    ("#00e5ff", "Cyber Cyan"),
    ("#3db356", "Emerald Green"),
    ("#ffb703", "Amber Gold"),
    ("#ff7800", "Sunset Orange"),
    ("#e05252", "Crimson Red"),
    ("#f72585", "Neon Pink")
]

_SWATCH_CSS_INITIALIZED = False

def _ensure_swatch_css():
    global _SWATCH_CSS_INITIALIZED
    if _SWATCH_CSS_INITIALIZED:
        return
    css_rules = [f".mix-c-{hex_c.replace('#', '')} {{ background-color: {hex_c}; border-radius: 13px; }}" for hex_c, _ in AVAILABLE_MIX_COLORS]
    full_css = "\n".join(css_rules)
    prov = Gtk.CssProvider()
    if hasattr(prov, "load_from_string"):
        prov.load_from_string(full_css)
    else:
        prov.load_from_data(full_css.encode())
    display = Gdk.Display.get_default()
    if display:
        Gtk.StyleContext.add_provider_for_display(display, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _SWATCH_CSS_INITIALIZED = True

class MixHeaderCard(Gtk.Box):
    """
    Column header card representing an output mix bus (e.g. Personal Mix / Record Mix).
    Supports customizing mix name, subtitle, and accent color, as well as deletion and reordering.
    """
    def __init__(self, mix_info: dict, pipewire_mgr=None, hardware_mgr=None, on_remove_callback=None, on_edit_callback=None, on_reorder_callback=None, on_hover_col_callback=None, on_move_left_callback=None, on_move_right_callback=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.mix_info = mix_info
        self.pipewire_mgr = pipewire_mgr
        self.hardware_mgr = hardware_mgr
        self.on_remove_callback = on_remove_callback
        self.on_edit_callback = on_edit_callback
        self.on_reorder_callback = on_reorder_callback
        self.on_hover_col_callback = on_hover_col_callback
        self.on_move_left_callback = on_move_left_callback
        self.on_move_right_callback = on_move_right_callback
        
        self.add_css_class("mix-header-card")
        self.set_hexpand(False)
        self.set_size_request(200, -1)

        top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        # Horizontal Drag Grip Handle
        self.drag_grip = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        self.drag_grip.set_pixel_size(16)
        self.drag_grip.add_css_class("mix-drag-handle")
        self.drag_grip.set_cursor_from_name("grab")
        self.drag_grip.set_tooltip_text("Click and hold to reorder mix horizontally")
        top_box.append(self.drag_grip)

        # Mix Icon
        icon_name = mix_info.get("icon", "audio-headphones-symbolic")
        self.icon_img = Gtk.Image.new_from_icon_name(icon_name)
        self.icon_img.set_pixel_size(18)
        top_box.append(self.icon_img)

        # Titles Box
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title_box.set_hexpand(True)

        header_title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.title_lbl = Gtk.Label(label=mix_info.get("name", "Mix"))
        self.title_lbl.add_css_class("mix-header-title")
        self.title_lbl.set_halign(Gtk.Align.START)
        self.title_lbl.set_ellipsize(3)
        header_title_row.append(self.title_lbl)

        self.default_badge = Gtk.Label()
        self.default_badge.add_css_class("device-badge")
        title_box.append(header_title_row)

        is_personal_mix = mix_info.get("id") in ("personal", "personal_mix")
        sub_text = self._resolve_subtitle()
        self.subtitle_lbl = Gtk.Label(label=sub_text)
        if is_personal_mix:
            self.subtitle_lbl.add_css_class("mix-header-bold-subtitle")
        else:
            self.subtitle_lbl.add_css_class("mix-header-subtitle")
        self.subtitle_lbl.set_halign(Gtk.Align.START)
        self.subtitle_lbl.set_ellipsize(3)
        title_box.append(self.subtitle_lbl)
        self.default_badge.set_halign(Gtk.Align.START)
        title_box.append(self.default_badge)

        self._refresh_default_badge()

        top_box.append(title_box)

        # Action Buttons Box (Edit Popover + Delete)
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)

        # Edit button with popover
        self.edit_btn = Gtk.MenuButton()
        self.edit_btn.set_icon_name("emblem-system-symbolic")
        self.edit_btn.add_css_class("flat")
        self.edit_btn.add_css_class("wave-icon-btn")
        self.edit_btn.set_tooltip_text(f"Edit '{mix_info.get('name')}' settings")
        self._setup_edit_popover(self.edit_btn)
        btn_box.append(self.edit_btn)

        # Delete mix button
        self.del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        self.del_btn.add_css_class("flat")
        self.del_btn.add_css_class("wave-icon-btn")
        self.del_btn.set_tooltip_text(f"Delete '{mix_info.get('name')}'")
        self.del_btn.connect("clicked", self._on_delete_clicked)
        btn_box.append(self.del_btn)

        top_box.append(btn_box)
        self.append(top_box)

        # Master Volume Slider & VU Meter (Controls WaveController_<mix_id>_Sink/Source)
        fader_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        fader_box.set_margin_top(4)
        fader_box.set_margin_bottom(2)

        vol = self.pipewire_mgr.get_mix_master_volume(mix_info["id"]) if self.pipewire_mgr else 100
        muted = self.pipewire_mgr.get_mix_master_mute(mix_info["id"]) if self.pipewire_mgr else False

        self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic" if not muted else "audio-volume-muted-symbolic")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.add_css_class("wave-icon-btn")
        self.mute_btn.set_valign(Gtk.Align.CENTER)
        self.mute_btn.set_tooltip_text(f"Mute {mix_info.get('name')} Bus")
        if muted:
            self.mute_btn.add_css_class("muted")
        self.mute_btn.connect("clicked", self._on_mute_clicked)
        fader_box.append(self.mute_btn)

        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale.set_value(vol)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.add_css_class("wave-mix-master-fader")
        self._scale_handler_id = self.scale.connect("value-changed", self._on_scale_value_changed)
        fader_box.append(self.scale)

        self.vol_lbl = Gtk.Label(label=f"{vol}%")
        self.vol_lbl.add_css_class("mix-header-subtitle")
        self.vol_lbl.set_size_request(32, -1)
        self.vol_lbl.set_halign(Gtk.Align.END)
        fader_box.append(self.vol_lbl)

        self.append(fader_box)

        # Mix Accent Color Indicator Line
        self.color_bar = Gtk.Box()
        self.color_bar.set_size_request(-1, 3)
        self.color_bar.add_css_class("mix-color-indicator")
        self._apply_indicator_color(mix_info.get("color", "#9146ff"))
        self.append(self.color_bar)

        # -------------------------------------------------------------
        # Horizontal Drag & Drop Controller Setup (Attached to Grip & Card)
        # -------------------------------------------------------------
        self.drag_source = Gtk.DragSource.new()
        self.drag_source.set_actions(Gdk.DragAction.MOVE)

        def on_drag_prepare(src, x, y):
            return Gdk.ContentProvider.new_for_value(self.mix_info["id"])

        def on_drag_begin(src, drag):
            paintable = Gtk.WidgetPaintable.new(self)
            src.set_icon(paintable, int(self.get_width() / 2), 20)
            self.add_css_class("drag-source-active")

        def on_drag_end(src, drag, delete_data):
            self.remove_css_class("drag-source-active")
            if self.on_hover_col_callback:
                self.on_hover_col_callback(self.mix_info["id"], False)

        self.drag_source.connect("prepare", on_drag_prepare)
        self.drag_source.connect("drag-begin", on_drag_begin)
        self.drag_source.connect("drag-end", on_drag_end)
        self.drag_grip.add_controller(self.drag_source)

        self.drop_target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)

        def on_drop_enter(target, x, y):
            if self.on_hover_col_callback:
                self.on_hover_col_callback(self.mix_info["id"], True)
            return Gdk.DragAction.MOVE

        def on_drop_motion(target, x, y):
            if self.on_hover_col_callback:
                self.on_hover_col_callback(self.mix_info["id"], True)
            return Gdk.DragAction.MOVE

        def on_drop_leave(target):
            if self.on_hover_col_callback:
                self.on_hover_col_callback(self.mix_info["id"], False)

        def on_drop(target, value, x, y):
            if self.on_hover_col_callback:
                self.on_hover_col_callback(self.mix_info["id"], False)
            source_mix_id = value
            target_mix_id = self.mix_info["id"]
            if source_mix_id and source_mix_id != target_mix_id and self.on_reorder_callback:
                self.on_reorder_callback(source_mix_id, target_mix_id)
                return True
            return False

        self.drop_target.connect("enter", on_drop_enter)
        self.drop_target.connect("motion", on_drop_motion)
        self.drop_target.connect("leave", on_drop_leave)
        self.drop_target.connect("drop", on_drop)
        self.add_controller(self.drop_target)

    def set_volume(self, volume: int):
        vol = max(0, min(100, int(volume)))
        if hasattr(self, "_scale_handler_id") and self._scale_handler_id:
            self.scale.handler_block(self._scale_handler_id)
            try:
                self.scale.set_value(vol)
            finally:
                self.scale.handler_unblock(self._scale_handler_id)
        else:
            self.scale.set_value(vol)
        self.vol_lbl.set_text(f"{vol}%")
        self.scale.queue_draw()

    def update_ui_state(self):
        if not self.pipewire_mgr:
            return
        vol = self.pipewire_mgr.get_mix_master_volume(self.mix_info["id"])
        muted = self.pipewire_mgr.get_mix_master_mute(self.mix_info["id"])

        if hasattr(self, "_scale_handler_id") and self._scale_handler_id:
            self.scale.handler_block(self._scale_handler_id)
            try:
                self.scale.set_value(vol)
            finally:
                self.scale.handler_unblock(self._scale_handler_id)
        else:
            self.scale.set_value(vol)

        self.vol_lbl.set_text(f"{int(vol)}%")

        if muted:
            self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.mute_btn.add_css_class("muted")
            self.add_css_class("muted")
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")
            self.remove_css_class("muted")

    def _on_mute_clicked(self, btn):
        if self.pipewire_mgr:
            new_mute = self.pipewire_mgr.toggle_mix_master_mute(self.mix_info["id"])
            if self.hardware_mgr and (self.mix_info.get("type") == "sink" or "personal" in self.mix_info.get("id", "")):
                target = self.mix_info.get("target_device", "")
                if "wave" in str(target).lower() or "elgato" in str(target).lower() or target in ("default", ""):
                    self.hardware_mgr.set_mode_mute("hp", new_mute, transient=True)
            if new_mute:
                self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
                self.mute_btn.add_css_class("muted")
                self.add_css_class("muted")
            else:
                self.mute_btn.set_icon_name("audio-volume-high-symbolic")
                self.mute_btn.remove_css_class("muted")
                self.remove_css_class("muted")

    def _on_scale_value_changed(self, scale):
        vol = int(scale.get_value())
        self.vol_lbl.set_text(f"{vol}%")
        if self.pipewire_mgr:
            self.pipewire_mgr.set_mix_master_volume(self.mix_info["id"], vol)
        if self.hardware_mgr and (self.mix_info.get("type") == "sink" or "personal" in self.mix_info.get("id", "")):
            target = self.mix_info.get("target_device", "")
            if "wave" in str(target).lower() or "elgato" in str(target).lower() or target in ("default", ""):
                self.hardware_mgr.set_output_volume(volume_pct=vol, transient=True)

    def _apply_indicator_color(self, hex_code: str):
        if not hasattr(self, "_color_provider") or not self._color_provider:
            self._color_provider = Gtk.CssProvider()
            display = Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(display, self._color_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 10)
        clean_id = self.mix_info.get("id", "default").replace(" ", "_").replace("-", "_")
        css_class = f"mix-ind-{clean_id}"
        css = f".{css_class} {{ background-color: {hex_code}; border-radius: 2px; }}"
        if hasattr(self._color_provider, "load_from_string"):
            self._color_provider.load_from_string(css)
        else:
            self._color_provider.load_from_data(css.encode())
        self.color_bar.add_css_class(css_class)

    def _on_delete_clicked(self, b):
        mix_name = self.mix_info.get("name", "Mix")
        root_win = self.get_root()
        if not isinstance(root_win, Gtk.Window):
            root_win = self.get_native() if isinstance(self.get_native(), Gtk.Window) else None

        dialog = Adw.MessageDialog(
            transient_for=root_win,
            heading=f"Delete '{mix_name}' Mix?",
            body=f"Are you sure you want to delete the '{mix_name}' mix? This will remove its sub-mix bus and tear down its virtual audio routing."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete Mix")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def _on_response(d, response):
            if response == "delete":
                if self.pipewire_mgr:
                    self.pipewire_mgr.remove_mix(self.mix_info["id"])
                if self.on_remove_callback:
                    self.on_remove_callback(self.mix_info["id"])

        dialog.connect("response", _on_response)
        dialog.present()

    def _refresh_default_badge(self):
        if not hasattr(self, "default_badge"):
            return
        if not config_manager.get("system_defaults_enabled", False):
            self.default_badge.set_visible(False)
            return
        is_default = self.pipewire_mgr.is_mix_system_default(self.mix_info["id"]) if self.pipewire_mgr else False
        m_type = self.mix_info.get("type", "source" if self.mix_info.get("id") != "personal" else "sink")
        if is_default:
            self.default_badge.set_visible(True)
            self.default_badge.remove_css_class("primary")
            self.default_badge.add_css_class("online")
            if m_type == "sink" or self.mix_info.get("id") == "personal":
                self.default_badge.set_text("Default Output")
            else:
                self.default_badge.set_text("Default Input")
        else:
            self.default_badge.set_visible(False)

    def _setup_edit_popover(self, menu_btn: Gtk.MenuButton):
        popover = Gtk.Popover()
        popover.set_autohide(False)
        popover.set_cascade_popdown(False)
        popover.add_css_class("wave-popover")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(260, -1)

        head_lbl = Gtk.Label(label="Edit Mix Settings")
        head_lbl.add_css_class("mix-header-title")
        head_lbl.set_halign(Gtk.Align.START)
        box.append(head_lbl)

        # Mix Type (Fixed on creation)
        m_type = self.mix_info.get("type", "source" if self.mix_info.get("id") != "personal" else "sink")
        type_str = "Source (Microphone / Input)" if m_type == "source" else "Sink (Speaker / Output)"
        
        type_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        type_lbl = Gtk.Label(label=f"Type: {type_str}")
        type_lbl.add_css_class("mix-header-subtitle")
        type_box.append(type_lbl)
        box.append(type_box)

        is_personal_mix = self.mix_info.get("id") in ("personal", "personal_mix")

        # Mix Name
        name_entry = Gtk.Entry(text=self.mix_info.get("name", ""))
        name_entry.set_placeholder_text("Mix Name")
        box.append(name_entry)

        # Subtitle (Omitted for Personal Mix since it dynamically mirrors default monitor device)
        sub_entry = None
        if not is_personal_mix:
            sub_entry = Gtk.Entry(text=self.mix_info.get("subtitle", ""))
            sub_entry.set_placeholder_text("Subtitle (e.g. Broadcast / Headphones)")
            box.append(sub_entry)

        # Accent Color
        # Accent Color Palette (Visual Swatches)
        color_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        color_lbl = Gtk.Label(label="Color:")
        color_lbl.add_css_class("mix-header-subtitle")
        color_lbl.set_halign(Gtk.Align.START)
        color_box.append(color_lbl)

        color_swatch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        color_swatch_box.add_css_class("color-palette-grid")

        _ensure_swatch_css()
        self.selected_color = self.mix_info.get("color", "#9146ff")
        color_buttons = {}

        def select_color(hex_code):
            self.selected_color = hex_code
            for c_hex, btn in color_buttons.items():
                if c_hex.lower() == hex_code.lower():
                    btn.add_css_class("selected")
                else:
                    btn.remove_css_class("selected")
            self._apply_indicator_color(hex_code)

        for hex_code, col_name in AVAILABLE_MIX_COLORS:
            c_btn = Gtk.Button()
            c_btn.add_css_class("flat")
            c_btn.add_css_class("color-palette-btn")
            c_btn.set_size_request(26, 26)
            c_btn.set_tooltip_text(col_name)
            c_btn.add_css_class(f"mix-c-{hex_code.replace('#', '')}")

            if hex_code.lower() == self.selected_color.lower():
                c_btn.add_css_class("selected")

            c_btn.connect("clicked", lambda b, h=hex_code: select_color(h))
            color_buttons[hex_code] = c_btn
            color_swatch_box.append(c_btn)

        # Custom Color Picker Button
        self.mix_color_dialog_btn = Gtk.ColorDialogButton()
        color_dialog = Gtk.ColorDialog.new()
        color_dialog.set_title(f"Custom Color for {self.mix_info.get('name')}")
        color_dialog.set_with_alpha(False)
        self.mix_color_dialog_btn.set_dialog(color_dialog)
        self.mix_color_dialog_btn.add_css_class("flat")
        self.mix_color_dialog_btn.add_css_class("color-palette-btn")
        self.mix_color_dialog_btn.set_size_request(26, 26)
        self.mix_color_dialog_btn.set_tooltip_text("Custom Color...")
        init_rgba = Gdk.RGBA()
        init_rgba.parse(self.selected_color)
        self.mix_color_dialog_btn.set_rgba(init_rgba)

        def on_custom_color_notify(btn, *args):
            rgba = btn.get_rgba()
            r, g, b = int(rgba.red * 255), int(rgba.green * 255), int(rgba.blue * 255)
            h = f"#{r:02X}{g:02X}{b:02X}"
            select_color(h)

        self.mix_color_dialog_btn.connect("notify::rgba", on_custom_color_notify)
        color_swatch_box.append(self.mix_color_dialog_btn)

        color_box.append(color_swatch_box)
        box.append(color_box)

        # Physical Output Target Routing (For Sink / Speaker mixes only)
        target_dev_combo = None
        target_dev_keys = []
        if m_type == "sink" and not is_personal_mix:
            target_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            target_lbl = Gtk.Label(label="Target:")
            target_lbl.add_css_class("mix-header-subtitle")
            target_lbl.set_size_request(45, -1)
            target_lbl.set_halign(Gtk.Align.START)
            target_row.append(target_lbl)

            target_dev_keys = ["none", "default"]
            target_dev_combo = Gtk.DropDown()
            target_dev_combo.set_hexpand(True)
            target_row.append(target_dev_combo)
            box.append(target_row)

            def refresh_header_targets():
                nonlocal target_dev_keys
                target_options = [("none", "None (Virtual Only)"), ("default", "Default Output")]
                if self.hardware_mgr:
                    for dev in self.hardware_mgr.get_tracked_output_devices():
                        key = dev.get("device_key", dev.get("name", ""))
                        name = dev.get("display_name", dev.get("name", "Audio Device"))
                        target_options.append((key, name))

                target_dev_keys = [opt[0] for opt in target_options]
                target_dev_labels = [opt[1] for opt in target_options]
                target_dev_combo.set_model(Gtk.StringList.new(target_dev_labels))

                curr_target = self.mix_info.get("target_device", "none")
                sel_target_idx = 0
                for i, k in enumerate(target_dev_keys):
                    if k == curr_target:
                        sel_target_idx = i
                        break
                target_dev_combo.set_selected(sel_target_idx)

            refresh_header_targets()
            popover.connect("notify::visible", lambda p, *args: refresh_header_targets() if p.get_visible() else None)
            self._refresh_header_targets = refresh_header_targets

        # Hardware LED Controls for Elgato Wave device (Headphone Volume Mode)
        if is_personal_mix and self.hardware_mgr and (getattr(self.hardware_mgr, "is_elgato", False) or getattr(self.hardware_mgr, "is_connected", False)):
            box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            from .led_color_picker import LEDColorButton

            hp_led_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hp_led_lbl = Gtk.Label(label="Headphone Mode Ring Color:", hexpand=True, halign=Gtk.Align.START)
            hp_led_lbl.add_css_class("mix-header-subtitle")
            hp_led_btn = LEDColorButton(self.hardware_mgr, "hp", title="Headphone LED", parent_popover=popover)
            hp_led_row.append(hp_led_lbl)
            hp_led_row.append(hp_led_btn)
            box.append(hp_led_row)

        # Minimal Symbolic Icon Palette (Pure Vector Icons, No Text Labels, Zero Emojis)
        AVAILABLE_MIX_ICONS = [
            "personal-symbolic",               # Personal Mix (User silhouette)
            "user-available-symbolic",         # Chat / Discord
            "camera-web-symbolic",             # Stream / OBS
            "input-gaming-symbolic",           # Gaming
            "applications-multimedia-symbolic",# Music / Media
            "audio-headphones-symbolic",       # Headphones
            "audio-input-microphone-symbolic", # Microphone
            "audio-headset-symbolic",          # Headset
            "audio-speakers-symbolic",         # Speakers
            "applications-internet-symbolic",  # Web
            "preferences-system-symbolic",     # System SFX
            "media-record-symbolic",           # Recording
            "radio-symbolic"                   # Broadcast
        ]

        icon_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        icon_lbl = Gtk.Label(label="Icon:")
        icon_lbl.add_css_class("mix-header-subtitle")
        icon_lbl.set_halign(Gtk.Align.START)
        icon_box.append(icon_lbl)

        palette_grid = Gtk.Grid(row_spacing=6, column_spacing=6)
        palette_grid.add_css_class("icon-palette-grid")
        palette_grid.set_halign(Gtk.Align.START)

        self.selected_icon = self.mix_info.get("icon", "audio-headphones-symbolic")
        icon_buttons = {}

        def select_icon(icon_name):
            self.selected_icon = icon_name
            for name, btn in icon_buttons.items():
                if name == icon_name:
                    btn.add_css_class("selected")
                else:
                    btn.remove_css_class("selected")
            # Live visual preview update
            self.icon_img.set_from_icon_name(icon_name)

        cols_per_row = 6
        for idx, icon_name in enumerate(AVAILABLE_MIX_ICONS):
            row = idx // cols_per_row
            col = idx % cols_per_row

            btn = Gtk.Button()
            btn.add_css_class("flat")
            btn.add_css_class("icon-palette-btn")
            btn.set_size_request(34, 34)
            btn.set_tooltip_text(icon_name.replace("-symbolic", ""))

            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(18)
            btn.set_child(img)

            if icon_name == self.selected_icon:
                btn.add_css_class("selected")

            btn.connect("clicked", lambda b, ic=icon_name: select_icon(ic))
            icon_buttons[icon_name] = btn
            palette_grid.attach(btn, col, row, 1, 1)

        icon_box.append(palette_grid)
        box.append(icon_box)

        # Reorder Mix Position Controls (Left/Right Arrows)
        reorder_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        reorder_row.set_margin_top(4)

        reorder_lbl = Gtk.Label(label="Reorder Mix:", hexpand=True, halign=Gtk.Align.START)
        reorder_lbl.add_css_class("mix-header-subtitle")
        reorder_row.append(reorder_lbl)

        btn_move_left = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        btn_move_left.add_css_class("flat")
        btn_move_left.add_css_class("wave-icon-btn")
        btn_move_left.set_tooltip_text("Move Mix Left")
        
        btn_move_right = Gtk.Button.new_from_icon_name("go-next-symbolic")
        btn_move_right.add_css_class("flat")
        btn_move_right.add_css_class("wave-icon-btn")
        btn_move_right.set_tooltip_text("Move Mix Right")

        def on_move_l(b):
            popover.popdown()
            if self.on_move_left_callback:
                self.on_move_left_callback(self.mix_info["id"])

        def on_move_r(b):
            popover.popdown()
            if self.on_move_right_callback:
                self.on_move_right_callback(self.mix_info["id"])

        btn_move_left.connect("clicked", on_move_l)
        btn_move_right.connect("clicked", on_move_r)
        reorder_row.append(btn_move_left)
        reorder_row.append(btn_move_right)
        box.append(reorder_row)

        save_btn = Gtk.Button(label="Save Changes")
        save_btn.add_css_class("suggested-action")
        
        def on_save(b):
            new_name = name_entry.get_text().strip()
            if is_personal_mix:
                new_sub = self._resolve_subtitle()
            else:
                new_sub = sub_entry.get_text().strip() if sub_entry else "Custom Mix"
                if not new_sub:
                    new_sub = "Custom Mix"

            new_color = getattr(self, "selected_color", "#9146ff")
            new_icon = self.selected_icon
            if target_dev_combo and target_dev_keys:
                idx = target_dev_combo.get_selected()
                if idx < len(target_dev_keys):
                    new_target = target_dev_keys[idx]
            else:
                new_target = self.mix_info.get("target_device", "default" if is_personal_mix else "none")

            if new_name:
                self.mix_info["icon"] = new_icon
                self.mix_info["name"] = new_name
                self.mix_info["subtitle"] = new_sub
                self.mix_info["color"] = new_color
                self.mix_info["target_device"] = new_target
                self.pipewire_mgr.update_mix(self.mix_info["id"], name=new_name, subtitle=new_sub, color=new_color, icon=new_icon, target_device=new_target)
                self.title_lbl.set_text(new_name)
                self.subtitle_lbl.set_text(new_sub)
                self.icon_img.set_from_icon_name(new_icon)
                self._apply_indicator_color(new_color)
                popover.popdown()
                if self.on_edit_callback:
                    self.on_edit_callback(self.mix_info["id"])

        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions_box.set_margin_top(4)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("destructive-action")
        cancel_btn.set_hexpand(True)
        cancel_btn.connect("clicked", lambda b: popover.popdown())
        actions_box.append(cancel_btn)

        save_btn.set_hexpand(True)
        save_btn.connect("clicked", on_save)
        actions_box.append(save_btn)

        box.append(actions_box)

        popover.set_child(box)
        menu_btn.set_popover(popover)

    def _resolve_subtitle(self) -> str:
        if self.mix_info.get("id") in ("personal", "personal_mix"):
            dev_key = config_manager.get("default_output_device", "default")
            if self.hardware_mgr:
                tracked = self.hardware_mgr.get_tracked_output_devices()
                for d in tracked:
                    k = d.get("device_key", d.get("name", ""))
                    if k == dev_key or (dev_key != "default" and dev_key in k):
                        return d.get("display_name", d.get("name", "Audio Device"))
                if dev_key == "default" and tracked:
                    return tracked[0].get("display_name", tracked[0].get("name", "Default Output"))
            return "Default Output"
        return self.mix_info.get("subtitle", "Custom Mix")

    def refresh_device_targets(self):
        if self.mix_info.get("id") in ("personal", "personal_mix"):
            self.subtitle_lbl.set_text(self._resolve_subtitle())
        if hasattr(self, "_refresh_header_targets"):
            self._refresh_header_targets()
        self._refresh_default_badge()

    def update_ui_state(self):
        if self.mix_info.get("id") in ("personal", "personal_mix"):
            self.subtitle_lbl.set_text(self._resolve_subtitle())
        self._refresh_default_badge()
        if hasattr(self, "def_switch") and self.def_switch:
            is_active = self.pipewire_mgr.is_mix_system_default(self.mix_info["id"]) if self.pipewire_mgr else False
            if hasattr(self, "_def_switch_handler_id") and self._def_switch_handler_id:
                self.def_switch.handler_block(self._def_switch_handler_id)
                self.def_switch.set_active(is_active)
                self.def_switch.handler_unblock(self._def_switch_handler_id)
            else:
                self.def_switch.set_active(is_active)
