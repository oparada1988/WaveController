import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

class MixHeaderCard(Gtk.Box):
    """
    Column header card representing an output mix bus (e.g. Personal Mix / Record Mix).
    Supports customizing mix name, subtitle, and accent color, as well as deletion.
    """
    def __init__(self, mix_info: dict, pipewire_mgr=None, hardware_mgr=None, on_remove_callback=None, on_edit_callback=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.mix_info = mix_info
        self.pipewire_mgr = pipewire_mgr
        self.hardware_mgr = hardware_mgr
        self.on_remove_callback = on_remove_callback
        self.on_edit_callback = on_edit_callback
        
        self.add_css_class("mix-header-card")
        self.set_hexpand(False)
        self.set_size_request(200, -1)

        top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        # Mix Icon
        icon_name = mix_info.get("icon", "audio-headphones-symbolic")
        self.icon_img = Gtk.Image.new_from_icon_name(icon_name)
        self.icon_img.set_pixel_size(18)
        top_box.append(self.icon_img)

        # Titles Box
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title_box.set_hexpand(True)

        self.title_lbl = Gtk.Label(label=mix_info.get("name", "Mix"))
        self.title_lbl.add_css_class("mix-header-title")
        self.title_lbl.set_halign(Gtk.Align.START)
        self.title_lbl.set_ellipsize(3)
        title_box.append(self.title_lbl)

        self.subtitle_lbl = Gtk.Label(label=mix_info.get("subtitle", "1 output"))
        self.subtitle_lbl.add_css_class("mix-header-subtitle")
        self.subtitle_lbl.set_halign(Gtk.Align.START)
        self.subtitle_lbl.set_ellipsize(3)
        title_box.append(self.subtitle_lbl)

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

    def _apply_indicator_color(self, hex_code: str):
        # We can dynamically apply custom color via CSS provider
        css = f".mix-color-indicator {{ background-color: {hex_code}; border-radius: 2px; }}"
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        self.color_bar.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 10)

    def _on_delete_clicked(self, b):
        if self.pipewire_mgr:
            self.pipewire_mgr.remove_mix(self.mix_info["id"])
        if self.on_remove_callback:
            self.on_remove_callback(self.mix_info["id"])

    def _setup_edit_popover(self, menu_btn: Gtk.MenuButton):
        popover = Gtk.Popover()
        popover.set_autohide(True)
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

        # Mix Name
        name_entry = Gtk.Entry(text=self.mix_info.get("name", ""))
        name_entry.set_placeholder_text("Mix Name")
        box.append(name_entry)

        # Subtitle
        sub_entry = Gtk.Entry(text=self.mix_info.get("subtitle", ""))
        sub_entry.set_placeholder_text("Subtitle (e.g. Broadcast / Headphones)")
        box.append(sub_entry)

        # Accent Color
        color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        color_lbl = Gtk.Label(label="Color:")
        color_lbl.add_css_class("mix-header-subtitle")
        color_lbl.set_size_request(45, -1)
        color_lbl.set_halign(Gtk.Align.START)
        color_row.append(color_lbl)

        colors_map = [
            ("#9146ff", "Purple"),
            ("#3584e4", "Blue"),
            ("#3db356", "Green"),
            ("#ff7800", "Orange"),
            ("#e05252", "Red")
        ]
        color_combo = Gtk.DropDown.new_from_strings([c[1] for c in colors_map])
        
        curr_color = self.mix_info.get("color", "#9146ff")
        sel_idx = 0
        for i, (hex_code, _) in enumerate(colors_map):
            if hex_code.lower() == curr_color.lower():
                sel_idx = i
                break
        color_combo.set_selected(sel_idx)
        color_combo.set_hexpand(True)
        color_row.append(color_combo)
        box.append(color_row)

        # Physical Output Target Routing (For Sink / Speaker mixes only)
        target_dev_combo = None
        target_dev_keys = []
        if m_type == "sink" or self.mix_info.get("id") == "personal":
            target_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            target_lbl = Gtk.Label(label="Target:")
            target_lbl.add_css_class("mix-header-subtitle")
            target_lbl.set_size_request(45, -1)
            target_lbl.set_halign(Gtk.Align.START)
            target_row.append(target_lbl)

            target_options = [("none", "None (Virtual Only)"), ("default", "Default Output")]
            if self.hardware_mgr:
                for dev in self.hardware_mgr.get_tracked_output_devices():
                    key = dev.get("device_key", dev.get("name", ""))
                    name = dev.get("display_name", dev.get("name", "Audio Device"))
                    target_options.append((key, name))

            target_dev_keys = [opt[0] for opt in target_options]
            target_dev_labels = [opt[1] for opt in target_options]

            target_dev_combo = Gtk.DropDown.new_from_strings(target_dev_labels)
            curr_target = self.mix_info.get("target_device", "none" if self.mix_info.get("id") != "personal" else "default")
            sel_target_idx = 0
            for i, k in enumerate(target_dev_keys):
                if k == curr_target:
                    sel_target_idx = i
                    break
            target_dev_combo.set_selected(sel_target_idx)
            target_dev_combo.set_hexpand(True)
            target_row.append(target_dev_combo)
            box.append(target_row)

        # Minimal Symbolic Icon Palette (Pure Vector Icons, No Text Labels, Zero Emojis)
        AVAILABLE_MIX_ICONS = [
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

        save_btn = Gtk.Button(label="Save Changes")
        save_btn.add_css_class("suggested-action")
        
        def on_save(b):
            new_name = name_entry.get_text().strip()
            new_sub = sub_entry.get_text().strip() or "Custom Mix"
            c_idx = color_combo.get_selected()
            new_color = colors_map[c_idx][0] if c_idx < len(colors_map) else "#9146ff"
            new_icon = self.selected_icon
            new_target = "none"
            if target_dev_combo and target_dev_keys:
                idx = target_dev_combo.get_selected()
                if idx < len(target_dev_keys):
                    new_target = target_dev_keys[idx]

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

        save_btn.connect("clicked", on_save)
        box.append(save_btn)

        popover.set_child(box)
        menu_btn.set_popover(popover)
