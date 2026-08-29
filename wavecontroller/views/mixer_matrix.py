import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

from .channel_card import ChannelCard
from .mix_header import MixHeaderCard
from .matrix_cell import MatrixCell
from .led_color_picker import LEDColorButton

class MixerMatrixView(Gtk.Box):
    """
    Main Matrix Sub-Mixer view with dual-track stereo volume sliders,
    live green audio meters, custom mix creation (+), and smart app channel creation.
    """
    def __init__(self, pipewire_mgr, peak_monitor, hardware_mgr):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.pipewire_mgr = pipewire_mgr
        self.peak_monitor = peak_monitor
        self.hardware_mgr = hardware_mgr
        self._syncing_hw_balance = False
        
        self.channel_cards = {}
        self.matrix_cells = {} # {(channel_id, mix_id): MatrixCell}
        self.mix_headers = {} # {mix_id: MixHeaderCard}
        self.pipewire_mgr.on_external_change_callback = self._on_external_sync
        if self.hardware_mgr and hasattr(self.hardware_mgr, "add_hardware_listener"):
            self.hardware_mgr.add_hardware_listener(lambda curr, changed: GLib.idle_add(self._on_hardware_sync, curr, changed))
        
        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(20)
        self.set_margin_end(20)

        # 1. Header Toolbar (Navigation & Controls)
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        
        title_lbl = Gtk.Label(label="Mixes")
        title_lbl.add_css_class("wave-view-title")
        header_box.append(title_lbl)

        header_box.append(Gtk.Box(hexpand=True)) # Spacer

        # Output Target Selector Dropdown (Configured Devices Only)
        out_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.output_devices_list = self.hardware_mgr.get_tracked_output_devices() if self.hardware_mgr else []
        out_names = [d.get("display_name", d.get("name", "Output")) for d in self.output_devices_list] or ["No Configured Output"]
        self.out_dropdown = Gtk.DropDown.new_from_strings(out_names)
        self.out_dropdown.add_css_class("wave-output-dropdown")
        for idx, d in enumerate(self.output_devices_list):
            if d.get("is_default"):
                self.out_dropdown.set_selected(idx)
                break
        self.out_dropdown.connect("notify::selected", self._on_output_dropdown_changed)
        out_box.append(self.out_dropdown)

        # Exposed Hardware Balance Fader & Dynamic LED Dropdown (Visible only for Wave devices)
        self.balance_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.balance_box.set_valign(Gtk.Align.CENTER)

        bal_icon = Gtk.Image.new_from_icon_name("power-profile-balanced-symbolic")
        bal_icon.set_pixel_size(16)
        bal_icon.set_tooltip_text("Direct Monitor Mix (Mic / PC Balance)")
        self.balance_box.append(bal_icon)

        self.balance_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.balance_scale.set_size_request(110, -1)
        self.balance_scale.set_draw_value(False)
        self.balance_scale.add_mark(50, Gtk.PositionType.BOTTOM, None)
        self.balance_scale.add_css_class("wave-balance-fader")
        init_mix = self.hardware_mgr.get_monitor_mix() if self.hardware_mgr else 50
        self.balance_scale.set_value(init_mix)
        self._bal_scale_handler = self.balance_scale.connect("value-changed", self._on_balance_slider_changed)
        
        # Double-click to snap Direct Monitor Balance back to 50/50
        bal_click = Gtk.GestureClick.new()
        def on_bal_double_click(gesture, n_press, x, y):
            if n_press == 2:
                self.balance_scale.set_value(50)
        bal_click.connect("released", on_bal_double_click)
        self.balance_scale.add_controller(bal_click)

        self.balance_box.append(self.balance_scale)

        self.balance_lbl = Gtk.Label(label="50/50")
        self.balance_lbl.add_css_class("mix-header-subtitle")
        self.balance_lbl.set_size_request(74, -1)
        self.balance_lbl.set_xalign(0.5)
        self._format_balance_label(init_mix)
        self.balance_box.append(self.balance_lbl)

        self.balance_led_btn = LEDColorButton(self.hardware_mgr, "mix", title="Hardware LED")
        self.balance_box.append(self.balance_led_btn)

        out_box.append(self.balance_box)
        self._update_balance_visibility()

        # Output Mute Toggle Button
        self.out_mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        self.out_mute_btn.add_css_class("flat")
        self.out_mute_btn.add_css_class("wave-icon-btn")
        self.out_mute_btn.set_tooltip_text("Mute Output")
        self.out_mute_btn.connect("clicked", self._on_output_mute_clicked)
        out_box.append(self.out_mute_btn)

        # Output Test Chime Button
        test_sound_btn = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        test_sound_btn.add_css_class("flat")
        test_sound_btn.add_css_class("wave-icon-btn")
        test_sound_btn.set_tooltip_text("Test Output (Play Chime)")
        
        def on_header_chime_clicked(b):
            sink_id = self._get_selected_output_sink_id()
            self.hardware_mgr.test_output_chime(sink_id)

        test_sound_btn.connect("clicked", on_header_chime_clicked)
        out_box.append(test_sound_btn)

        header_box.append(out_box)
        self.append(header_box)

        # 2. Main Matrix Grid
        self.grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        self.grid.set_halign(Gtk.Align.START)
        self.grid.set_valign(Gtk.Align.START)
        self.grid.set_hexpand(False)
        self.grid.set_vexpand(True)

        self._build_grid()

        # Scrolled container for channels
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.grid)
        scrolled.set_vexpand(True)
        self.append(scrolled)

        # 40 FPS Timer to refresh meter levels and sync states
        GLib.timeout_add(25, self._on_ui_tick)

    def _build_grid(self):
        # Top-left empty header cell
        spacer = Gtk.Box()
        spacer.set_hexpand(False)
        spacer.set_size_request(370, 48)
        self.grid.attach(spacer, 0, 0, 1, 1)

        # Mix Column Headers (Row 0, Columns 1..N)
        for col_idx, mix in enumerate(self.pipewire_mgr.mixes, start=1):
            mix_header = MixHeaderCard(
                mix,
                pipewire_mgr=self.pipewire_mgr,
                hardware_mgr=self.hardware_mgr,
                on_remove_callback=lambda m_id: GLib.idle_add(self._rebuild_grid),
                on_edit_callback=None,
                on_reorder_callback=self._on_reorder_mix,
                on_hover_col_callback=self._on_hover_col,
                on_move_left_callback=self._on_move_mix_left,
                on_move_right_callback=self._on_move_mix_right
            )
            self.mix_headers[mix["id"]] = mix_header
            self.grid.attach(mix_header, col_idx, 0, 1, 1)

        # Create Mix (+) Button Card (Column N+1)
        create_mix_col = len(self.pipewire_mgr.mixes) + 1
        create_mix_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        create_mix_card.add_css_class("mix-header-card")
        create_mix_card.set_hexpand(False)
        create_mix_card.set_size_request(52, 48)
        create_mix_card.set_valign(Gtk.Align.CENTER)
        
        plus_btn = Gtk.MenuButton()
        plus_btn.set_icon_name("list-add-symbolic")
        plus_btn.add_css_class("flat")
        plus_btn.add_css_class("wave-icon-btn")
        plus_btn.set_tooltip_text("Add Custom Mix")
        self._setup_create_mix_popover(plus_btn)
        create_mix_card.append(plus_btn)
        self.grid.attach(create_mix_card, create_mix_col, 0, 1, 1)

        # Channel Rows (Rows 1..N)
        for row_idx, ch in enumerate(self.pipewire_mgr.channels, start=1):
            # Left Header Card
            card = ChannelCard(
                ch,
                self.pipewire_mgr,
                self.hardware_mgr,
                on_link_toggle_callback=self._on_link_toggled,
                on_sync_meter_callback=self._on_sync_meter_toggled,
                on_channel_removed_callback=self._on_channel_deleted,
                on_channel_renamed_callback=lambda ch_id, name: GLib.idle_add(self._rebuild_grid),
                on_reorder_callback=self._on_reorder_channel,
                on_hover_row_callback=self._on_hover_row
            )
            self.channel_cards[ch["id"]] = card
            self.grid.attach(card, 0, row_idx, 1, 1)

            # Sub-Mix Cells
            for col_idx, mix in enumerate(self.pipewire_mgr.mixes, start=1):
                cell = MatrixCell(ch["id"], mix["id"], self.pipewire_mgr, on_change_callback=self._on_cell_changed)
                self.matrix_cells[(ch["id"], mix["id"])] = cell
                self.grid.attach(cell, col_idx, row_idx, 1, 1)

        # Bottom "+ Create channel" button with dark Popover
        bottom_row = len(self.pipewire_mgr.channels) + 1
        create_btn = Gtk.MenuButton()
        create_btn.set_label("+ Create channel")
        create_btn.add_css_class("create-channel-btn")
        create_btn.set_hexpand(False)
        create_btn.set_size_request(340, -1)
        self._setup_create_channel_popover(create_btn)
        self.grid.attach(create_btn, 0, bottom_row, 1, 1)

    def _on_hover_row(self, ch_id: str, is_hovered: bool):
        card = self.channel_cards.get(ch_id)
        if card:
            if is_hovered:
                card.add_css_class("drop-target-active")
            else:
                card.remove_css_class("drop-target-active")
        for mix in self.pipewire_mgr.mixes:
            cell = self.matrix_cells.get((ch_id, mix["id"]))
            if cell:
                if is_hovered:
                    cell.add_css_class("drop-target-active")
                else:
                    cell.remove_css_class("drop-target-active")

    def _on_reorder_channel(self, src_ch_id: str, dest_ch_id: str):
        if self.pipewire_mgr.reorder_channels_by_id(src_ch_id, dest_ch_id):
            self._rebuild_grid_with_highlight(src_ch_id)

    def _on_move_channel_up(self, ch_id: str):
        if self.pipewire_mgr.move_channel_up(ch_id):
            self._rebuild_grid_with_highlight(ch_id)

    def _on_move_channel_down(self, ch_id: str):
        if self.pipewire_mgr.move_channel_down(ch_id):
            self._rebuild_grid_with_highlight(ch_id)

    def _rebuild_grid_with_highlight(self, highlight_ch_id: str = None):
        self._rebuild_grid()
        if highlight_ch_id:
            card = self.channel_cards.get(highlight_ch_id)
            if card:
                card.add_css_class("drop-target-active")
            for mix in self.pipewire_mgr.mixes:
                cell = self.matrix_cells.get((highlight_ch_id, mix["id"]))
                if cell:
                    cell.add_css_class("drop-target-active")
            
            def remove_highlight():
                if card:
                    card.remove_css_class("drop-target-active")
                for mix in self.pipewire_mgr.mixes:
                    cell = self.matrix_cells.get((highlight_ch_id, mix["id"]))
                    if cell:
                        cell.remove_css_class("drop-target-active")
                return False
            GLib.timeout_add(450, remove_highlight)

    def _on_reorder_mix(self, src_mix_id: str, dest_mix_id: str):
        if self.pipewire_mgr.reorder_mixes_by_id(src_mix_id, dest_mix_id):
            self._rebuild_grid_with_mix_highlight(src_mix_id)

    def _on_move_mix_left(self, mix_id: str):
        if self.pipewire_mgr.move_mix_left(mix_id):
            self._rebuild_grid_with_mix_highlight(mix_id)

    def _on_move_mix_right(self, mix_id: str):
        if self.pipewire_mgr.move_mix_right(mix_id):
            self._rebuild_grid_with_mix_highlight(mix_id)

    def _on_hover_col(self, mix_id: str, is_hovered: bool):
        header = self.mix_headers.get(mix_id)
        if header:
            if is_hovered:
                header.add_css_class("drop-target-active")
            else:
                header.remove_css_class("drop-target-active")
        for ch in self.pipewire_mgr.channels:
            cell = self.matrix_cells.get((ch["id"], mix_id))
            if cell:
                if is_hovered:
                    cell.add_css_class("drop-target-active")
                else:
                    cell.remove_css_class("drop-target-active")

    def _rebuild_grid_with_mix_highlight(self, highlight_mix_id: str = None):
        self._rebuild_grid()
        if highlight_mix_id:
            header = self.mix_headers.get(highlight_mix_id)
            if header:
                header.add_css_class("drop-target-active")
            for ch in self.pipewire_mgr.channels:
                cell = self.matrix_cells.get((ch["id"], highlight_mix_id))
                if cell:
                    cell.add_css_class("drop-target-active")
            
            def remove_highlight():
                if header:
                    header.remove_css_class("drop-target-active")
                for ch in self.pipewire_mgr.channels:
                    c = self.matrix_cells.get((ch["id"], highlight_mix_id))
                    if c:
                        c.remove_css_class("drop-target-active")
                return False
            GLib.timeout_add(450, remove_highlight)

    def _setup_create_mix_popover(self, menu_btn: Gtk.MenuButton):
        popover = Gtk.Popover()
        popover.set_autohide(True)
        popover.add_css_class("wave-popover")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(260, -1)

        head_lbl = Gtk.Label(label="Add Custom Audio Mix")
        head_lbl.add_css_class("mix-header-title")
        head_lbl.set_halign(Gtk.Align.START)
        box.append(head_lbl)

        name_entry = Gtk.Entry(placeholder_text="Mix Name (e.g. Stream Mix, Discord Mic)")
        box.append(name_entry)

        sub_entry = Gtk.Entry(placeholder_text="Subtitle (e.g. Broadcast / Voice Chat)")
        box.append(sub_entry)

        # Mix Device Type (Source/Mic vs Sink/Speaker)
        type_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        type_lbl = Gtk.Label(label="Type:")
        type_lbl.add_css_class("mix-header-subtitle")
        type_lbl.set_size_request(45, -1)
        type_lbl.set_halign(Gtk.Align.START)
        type_row.append(type_lbl)

        type_combo = Gtk.DropDown.new_from_strings([
            "Source (Microphone / Input)",
            "Sink (Speaker / Output)"
        ])
        type_combo.set_selected(0)
        type_combo.set_hexpand(True)
        type_row.append(type_combo)
        box.append(type_row)

        # Physical Output Target Routing (For Sink / Speaker mixes)
        target_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        target_lbl = Gtk.Label(label="Target:")
        target_lbl.add_css_class("mix-header-subtitle")
        target_lbl.set_size_request(45, -1)
        target_lbl.set_halign(Gtk.Align.START)
        target_row.append(target_lbl)

        target_dev_keys = ["unselected"]
        target_combo = Gtk.DropDown()
        target_combo.set_hexpand(True)
        target_row.append(target_combo)
        target_row.set_visible(False)
        box.append(target_row)

        add_btn = Gtk.Button(label="Create Mix")
        add_btn.add_css_class("suggested-action")
        add_btn.set_sensitive(False) # Initial state disabled until name and valid target are selected

        def update_add_btn_state(*args):
            name_valid = bool(name_entry.get_text().strip())
            is_sink = (type_combo.get_selected() == 1)
            if not name_valid:
                add_btn.set_sensitive(False)
                return

            if is_sink:
                t_idx = target_combo.get_selected()
                sel_key = target_dev_keys[t_idx] if t_idx < len(target_dev_keys) else "unselected"
                add_btn.set_sensitive(sel_key != "unselected")
            else:
                add_btn.set_sensitive(True)

        def refresh_create_mix_targets():
            nonlocal target_dev_keys
            target_options = [("unselected", "Select Output Device...")]
            if self.hardware_mgr:
                for dev in self.hardware_mgr.get_tracked_output_devices():
                    key = dev.get("device_key", dev.get("name", ""))
                    name = dev.get("display_name", dev.get("name", "Audio Device"))
                    target_options.append((key, name))

            target_dev_keys = [opt[0] for opt in target_options]
            target_dev_labels = [opt[1] for opt in target_options]
            target_combo.set_model(Gtk.StringList.new(target_dev_labels))
            target_combo.set_selected(0)
            update_add_btn_state()

        def reset_create_mix_form():
            name_entry.set_text("")
            sub_entry.set_text("")
            type_combo.set_selected(0)
            target_row.set_visible(False)
            target_combo.set_selected(0)
            select_icon("user-available-symbolic")
            select_create_color("#9146ff")
            update_add_btn_state()

        refresh_create_mix_targets()
        popover.connect("notify::visible", lambda p, *args: refresh_create_mix_targets() if p.get_visible() else None)
        popover.connect("closed", lambda p: reset_create_mix_form())
        self._refresh_create_mix_targets = refresh_create_mix_targets

        def on_type_changed(combo, *args):
            is_sink = (combo.get_selected() == 1)
            target_row.set_visible(is_sink)
            update_add_btn_state()

        type_combo.connect("notify::selected", on_type_changed)
        target_combo.connect("notify::selected", update_add_btn_state)

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

        selected_icon_holder = {"icon": "user-available-symbolic"}
        icon_buttons = {}

        def select_icon(icon_name):
            selected_icon_holder["icon"] = icon_name
            for name, btn in icon_buttons.items():
                if name == icon_name:
                    btn.add_css_class("selected")
                else:
                    btn.remove_css_class("selected")

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

            if icon_name == selected_icon_holder["icon"]:
                btn.add_css_class("selected")

            btn.connect("clicked", lambda b, ic=icon_name: select_icon(ic))
            icon_buttons[icon_name] = btn
            palette_grid.attach(btn, col, row, 1, 1)

        icon_box.append(palette_grid)
        box.append(icon_box)

        def on_name_changed(entry):
            txt = entry.get_text().strip()
            if txt:
                m_type = "source" if type_combo.get_selected() == 0 else "sink"
                suggested_icon = self.pipewire_mgr.resolve_smart_mix_icon(txt, m_type)
                if suggested_icon in icon_buttons:
                    select_icon(suggested_icon)
            update_add_btn_state()

        name_entry.connect("changed", on_name_changed)

        # Accent Color Palette (Visual Swatches)
        color_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        color_lbl = Gtk.Label(label="Accent Color:")
        color_lbl.add_css_class("mix-header-subtitle")
        color_lbl.set_halign(Gtk.Align.START)
        color_box.append(color_lbl)

        color_swatch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        color_swatch_box.add_css_class("color-palette-grid")

        CREATE_MIX_COLORS = [
            ("#9146ff", "Stream Purple"),
            ("#3584e4", "Ocean Blue"),
            ("#00e5ff", "Cyber Cyan"),
            ("#3db356", "Emerald Green"),
            ("#ffb703", "Amber Gold"),
            ("#ff7800", "Sunset Orange"),
            ("#e05252", "Crimson Red"),
            ("#f72585", "Neon Pink")
        ]

        selected_color_holder = {"color": "#9146ff"}
        color_buttons = {}

        def select_create_color(hex_code):
            selected_color_holder["color"] = hex_code
            for c_hex, btn in color_buttons.items():
                if c_hex.lower() == hex_code.lower():
                    btn.add_css_class("selected")
                else:
                    btn.remove_css_class("selected")

        for hex_code, col_name in CREATE_MIX_COLORS:
            c_btn = Gtk.Button()
            c_btn.add_css_class("flat")
            c_btn.add_css_class("color-palette-btn")
            c_btn.set_size_request(26, 26)
            c_btn.set_tooltip_text(col_name)

            dot_css = f".mix-c-{hex_code.replace('#', '')} {{ background-color: {hex_code}; border-radius: 13px; }}"
            c_prov = Gtk.CssProvider()
            c_prov.load_from_data(dot_css.encode())
            c_btn.get_style_context().add_provider(c_prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            c_btn.add_css_class(f"mix-c-{hex_code.replace('#', '')}")

            if hex_code == selected_color_holder["color"]:
                c_btn.add_css_class("selected")

            c_btn.connect("clicked", lambda b, h=hex_code: select_create_color(h))
            color_buttons[hex_code] = c_btn
            color_swatch_box.append(c_btn)

        color_box.append(color_swatch_box)
        box.append(color_box)
        
        def on_add(b):
            name = name_entry.get_text().strip()
            sub = sub_entry.get_text().strip() or "Custom Mix"
            if name:
                c = selected_color_holder["color"]
                
                selected_icon = selected_icon_holder["icon"]
                mix_type = "source" if type_combo.get_selected() == 0 else "sink"
                
                selected_target = "none"
                if mix_type == "sink":
                    t_idx = target_combo.get_selected()
                    if t_idx < len(target_dev_keys):
                        selected_target = target_dev_keys[t_idx]
                        if selected_target == "unselected":
                            return

                self.pipewire_mgr.add_mix(name, subtitle=sub, mix_type=mix_type, color=c, icon=selected_icon, target_device=selected_target)
                reset_create_mix_form()
                popover.popdown()
                GLib.idle_add(self._rebuild_grid)

        add_btn.connect("clicked", on_add)

        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions_box.set_margin_top(4)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("destructive-action")
        cancel_btn.set_hexpand(True)
        cancel_btn.connect("clicked", lambda b: (reset_create_mix_form(), popover.popdown()))
        actions_box.append(cancel_btn)

        add_btn.set_hexpand(True)
        actions_box.append(add_btn)

        box.append(actions_box)

        popover.set_child(box)
        menu_btn.set_popover(popover)

    def _setup_create_channel_popover(self, menu_btn: Gtk.MenuButton):
        popover = Gtk.Popover()
        popover.set_autohide(True)
        popover.add_css_class("wave-popover")

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        stack.set_transition_duration(150)

        # ==========================================
        # PAGE 1: Category Selector (Application / Input Device)
        # ==========================================
        cat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        cat_box.set_margin_top(12)
        cat_box.set_margin_bottom(12)
        cat_box.set_margin_start(12)
        cat_box.set_margin_end(12)
        cat_box.set_size_request(270, -1)

        head_lbl = Gtk.Label(label="Add Audio Channel")
        head_lbl.add_css_class("mix-header-title")
        head_lbl.set_halign(Gtk.Align.START)
        cat_box.append(head_lbl)

        sub_lbl = Gtk.Label(label="Choose channel category:")
        sub_lbl.add_css_class("mix-header-subtitle")
        sub_lbl.set_halign(Gtk.Align.START)
        cat_box.append(sub_lbl)

        # Category 1: Application
        app_cat_btn = Gtk.Button()
        app_cat_btn.add_css_class("flat")
        app_cat_btn.add_css_class("wave-sidebar-row")

        app_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        app_icon = Gtk.Image.new_from_icon_name("applications-multimedia-symbolic")
        app_icon.set_pixel_size(22)
        app_row.append(app_icon)

        app_text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        app_text_box.set_hexpand(True)
        app_title = Gtk.Label(label="Application")
        app_title.add_css_class("channel-title")
        app_title.set_halign(Gtk.Align.START)
        app_text_box.append(app_title)

        app_desc = Gtk.Label(label="Route audio from running apps")
        app_desc.add_css_class("mix-header-subtitle")
        app_desc.set_halign(Gtk.Align.START)
        app_text_box.append(app_desc)
        app_row.append(app_text_box)

        arrow_app = Gtk.Image.new_from_icon_name("go-next-symbolic")
        arrow_app.set_pixel_size(14)
        app_row.append(arrow_app)
        app_cat_btn.set_child(app_row)
        cat_box.append(app_cat_btn)

        # Category 2: Input Device
        dev_cat_btn = Gtk.Button()
        dev_cat_btn.add_css_class("flat")
        dev_cat_btn.add_css_class("wave-sidebar-row")

        dev_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        dev_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
        dev_icon.set_pixel_size(22)
        dev_row.append(dev_icon)

        dev_text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        dev_text_box.set_hexpand(True)
        dev_title = Gtk.Label(label="Input Device")
        dev_title.add_css_class("channel-title")
        dev_title.set_halign(Gtk.Align.START)
        dev_text_box.append(dev_title)

        dev_desc = Gtk.Label(label="Route physical mic or line-in")
        dev_desc.add_css_class("mix-header-subtitle")
        dev_desc.set_halign(Gtk.Align.START)
        dev_text_box.append(dev_desc)
        dev_row.append(dev_text_box)

        arrow_dev = Gtk.Image.new_from_icon_name("go-next-symbolic")
        arrow_dev.set_pixel_size(14)
        dev_row.append(arrow_dev)

        dev_cat_btn.set_child(dev_row)
        cat_box.append(dev_cat_btn)

        cat_cancel_btn = Gtk.Button(label="Cancel")
        cat_cancel_btn.add_css_class("destructive-action")
        cat_cancel_btn.set_margin_top(4)
        cat_cancel_btn.connect("clicked", lambda b: popover.popdown())
        cat_box.append(cat_cancel_btn)

        stack.add_named(cat_box, "cat_page")

        # Container for Dynamic App List
        apps_list_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        # Container for Dynamic Hardware Device List
        dev_list_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        # ==========================================
        # Dynamic Refresh Handlers
        # ==========================================
        def show_app_page(b=None):
            while apps_list_container.get_first_child():
                apps_list_container.remove(apps_list_container.get_first_child())

            running_apps = self.pipewire_mgr.get_active_application_streams()

            # Gather all applications already assigned to existing channels
            configured_apps = set()
            for ch in self.pipewire_mgr.channels:
                configured_apps.add(ch.get("id", "").lower())
                configured_apps.add(ch.get("name", "").lower())
            for app_list in self.pipewire_mgr.assigned_apps.values():
                for a in app_list:
                    configured_apps.add(a.lower())

            available_apps = []
            if running_apps:
                for app_info in running_apps:
                    app_name = app_info["name"]
                    app_bin = app_info.get("binary", "")
                    name_low = app_name.lower()
                    bin_low = app_bin.lower()

                    is_already_added = (
                        name_low in configured_apps
                        or bin_low in configured_apps
                        or any(c in name_low or name_low in c for c in configured_apps if len(c) > 2)
                        or any(c in bin_low or bin_low in c for c in configured_apps if len(c) > 2)
                    )
                    if not is_already_added:
                        available_apps.append(app_info)

            if available_apps:
                for app_info in available_apps:
                    app_name = app_info["name"]
                    item_btn = Gtk.Button()
                    item_btn.add_css_class("flat")
                    item_btn.add_css_class("wave-sidebar-row")

                    item_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                    icon_name = app_info.get("icon") or self.pipewire_mgr.resolve_icon_for_app(app_name)
                    i_img = Gtk.Image.new_from_icon_name(icon_name)
                    i_img.set_pixel_size(18)
                    i_lbl = Gtk.Label(label=app_name)
                    i_lbl.set_hexpand(True)
                    i_lbl.set_halign(Gtk.Align.START)

                    add_ic = Gtk.Image.new_from_icon_name("list-add-symbolic")
                    add_ic.set_pixel_size(14)

                    item_row.append(i_img)
                    item_row.append(i_lbl)
                    item_row.append(add_ic)
                    item_btn.set_child(item_row)

                    def make_app_click_handler(name):
                        def handler(btn):
                            self.pipewire_mgr.add_channel(name, ch_type="sink", assigned_apps=[name])
                            popover.popdown()
                            GLib.idle_add(self._rebuild_grid)
                        return handler

                    item_btn.connect("clicked", make_app_click_handler(app_name))
                    apps_list_container.append(item_btn)
            else:
                if running_apps:
                    msg = "All running audio applications are already added to channels.\nLaunch another app or create a custom channel below:"
                else:
                    msg = "No audio applications running.\nLaunch an app (Spotify, Discord, etc.) or create custom below:"
                no_apps = Gtk.Label(label=msg)
                no_apps.add_css_class("mix-header-subtitle")
                no_apps.set_halign(Gtk.Align.START)
                no_apps.set_wrap(True)
                apps_list_container.append(no_apps)

            stack.set_visible_child_name("app_page")

        def show_device_page(b=None):
            while dev_list_container.get_first_child():
                dev_list_container.remove(dev_list_container.get_first_child())

            if self.hardware_mgr:
                self.hardware_mgr.detect_connected_hardware()

            input_devs = self.hardware_mgr.get_tracked_input_devices() if self.hardware_mgr else []
            if not input_devs and self.hardware_mgr:
                input_devs = self.hardware_mgr.input_devices
            
            # Filter out hardware devices already configured as channels
            configured_dev_names = {c.get("name", "").lower() for c in self.pipewire_mgr.channels}
            for ch in self.pipewire_mgr.channels:
                configured_dev_names.add(ch.get("id", "").lower())

            available_devs = [
                d for d in input_devs
                if self.hardware_mgr.get_device_display_name(d).lower() not in configured_dev_names
                and not any(c in self.hardware_mgr.get_device_display_name(d).lower() for c in configured_dev_names if len(c) > 3)
            ]

            if available_devs:
                for dev_info in available_devs:
                    dev_name = self.hardware_mgr.get_device_display_name(dev_info)
                    dev_icon = dev_info.get("icon", "")
                    if not dev_icon or dev_icon == "network-offline-symbolic":
                        dev_icon = self.hardware_mgr.get_device_icon(dev_info.get("device_key", dev_name))
                    is_connected = dev_info.get("connected", True)

                    dev_item_btn = Gtk.Button()
                    dev_item_btn.add_css_class("flat")
                    dev_item_btn.add_css_class("wave-sidebar-row")

                    d_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                    d_img = Gtk.Image.new_from_icon_name(dev_icon)
                    d_img.set_pixel_size(18)
                    if not is_connected:
                        d_img.set_opacity(0.55)

                    d_lbl = Gtk.Label(label=dev_name)
                    d_lbl.set_hexpand(True)
                    d_lbl.set_halign(Gtk.Align.START)
                    d_lbl.set_ellipsize(3)

                    d_row.append(d_img)
                    d_row.append(d_lbl)

                    if not is_connected:
                        badge_lbl = Gtk.Label(label="Offline")
                        badge_lbl.add_css_class("device-badge")
                        badge_lbl.add_css_class("offline")
                        badge_lbl.set_valign(Gtk.Align.CENTER)
                        d_row.append(badge_lbl)

                    d_add_ic = Gtk.Image.new_from_icon_name("list-add-symbolic")
                    d_add_ic.set_pixel_size(14)
                    d_row.append(d_add_ic)

                    dev_item_btn.set_child(d_row)

                    def make_dev_click_handler(name, icon, d_info):
                        def handler(btn):
                            dev_k = d_info.get("device_key", "")
                            dev_raw_name = d_info.get("name", "")
                            assigned = [name]
                            if dev_k and dev_k not in assigned:
                                assigned.append(dev_k)
                            if dev_raw_name and dev_raw_name not in assigned:
                                assigned.append(dev_raw_name)
                            self.pipewire_mgr.add_channel(name, icon=icon, ch_type="source", assigned_apps=assigned)
                            popover.popdown()
                            GLib.idle_add(self._rebuild_grid)
                        return handler

                    dev_item_btn.connect("clicked", make_dev_click_handler(dev_name, dev_icon, dev_info))
                    dev_list_container.append(dev_item_btn)
            else:
                if input_devs:
                    msg = "All connected hardware inputs are already added as channels."
                else:
                    msg = "No hardware input devices found."
                no_devs = Gtk.Label(label=msg)
                no_devs.add_css_class("mix-header-subtitle")
                no_devs.set_halign(Gtk.Align.START)
                dev_list_container.append(no_devs)

            stack.set_visible_child_name("device_page")

        app_cat_btn.connect("clicked", show_app_page)
        dev_cat_btn.connect("clicked", show_device_page)

        # ==========================================
        # PAGE 2: Application Channel Creator
        # ==========================================
        app_page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        app_page_box.set_margin_top(12)
        app_page_box.set_margin_bottom(12)
        app_page_box.set_margin_start(12)
        app_page_box.set_margin_end(12)
        app_page_box.set_size_request(270, -1)

        app_top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        back_app_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        back_app_btn.add_css_class("flat")
        back_app_btn.add_css_class("wave-icon-btn")
        back_app_btn.connect("clicked", lambda b: stack.set_visible_child_name("cat_page"))
        app_top_box.append(back_app_btn)

        app_head_lbl = Gtk.Label(label="Application Channel")
        app_head_lbl.add_css_class("mix-header-title")
        app_head_lbl.set_halign(Gtk.Align.START)
        app_top_box.append(app_head_lbl)
        app_page_box.append(app_top_box)

        app_sub_lbl = Gtk.Label(label="Running applications:")
        app_sub_lbl.add_css_class("mix-header-subtitle")
        app_sub_lbl.set_halign(Gtk.Align.START)
        app_page_box.append(app_sub_lbl)

        app_page_box.append(apps_list_container)

        # Custom Channel Entry
        app_page_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        cust_app_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        app_entry = Gtk.Entry(placeholder_text="Custom app channel name...")
        app_entry.set_hexpand(True)
        cust_app_box.append(app_entry)

        cust_app_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        cust_app_btn.add_css_class("suggested-action")
        
        def on_cust_app_add(b):
            name = app_entry.get_text().strip()
            if name:
                self.pipewire_mgr.add_channel(name, ch_type="sink", assigned_apps=[name])
                popover.popdown()
                GLib.idle_add(self._rebuild_grid)

        cust_app_btn.connect("clicked", on_cust_app_add)
        app_entry.connect("activate", on_cust_app_add)
        cust_app_box.append(cust_app_btn)
        app_page_box.append(cust_app_box)

        app_cancel_btn = Gtk.Button(label="Cancel")
        app_cancel_btn.add_css_class("destructive-action")
        app_cancel_btn.connect("clicked", lambda b: popover.popdown())
        app_page_box.append(app_cancel_btn)

        stack.add_named(app_page_box, "app_page")

        # ==========================================
        # PAGE 3: Input Device Channel Creator
        # ==========================================
        dev_page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        dev_page_box.set_margin_top(12)
        dev_page_box.set_margin_bottom(12)
        dev_page_box.set_margin_start(12)
        dev_page_box.set_margin_end(12)
        dev_page_box.set_size_request(270, -1)

        dev_top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        back_dev_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        back_dev_btn.add_css_class("flat")
        back_dev_btn.add_css_class("wave-icon-btn")
        back_dev_btn.connect("clicked", lambda b: stack.set_visible_child_name("cat_page"))
        dev_top_box.append(back_dev_btn)

        dev_head_lbl = Gtk.Label(label="Input Device Channel")
        dev_head_lbl.add_css_class("mix-header-title")
        dev_head_lbl.set_halign(Gtk.Align.START)
        dev_top_box.append(dev_head_lbl)
        dev_page_box.append(dev_top_box)

        dev_sub_lbl = Gtk.Label(label="Available hardware inputs:")
        dev_sub_lbl.add_css_class("mix-header-subtitle")
        dev_sub_lbl.set_halign(Gtk.Align.START)
        dev_page_box.append(dev_sub_lbl)

        dev_page_box.append(dev_list_container)

        dev_cancel_btn = Gtk.Button(label="Cancel")
        dev_cancel_btn.add_css_class("destructive-action")
        dev_cancel_btn.connect("clicked", lambda b: popover.popdown())
        dev_page_box.append(dev_cancel_btn)

        stack.add_named(dev_page_box, "device_page")

        def reset_create_channel_popover():
            stack.set_visible_child_name("cat_page")
            app_entry.set_text("")

        popover.connect("closed", lambda p: reset_create_channel_popover())

        stack.set_visible_child_name("cat_page")
        popover.set_child(stack)
        menu_btn.set_popover(popover)

    def _on_sync_meter_toggled(self, channel_id: str, is_synced: bool):
        for m in self.pipewire_mgr.mixes:
            cell = self.matrix_cells.get((channel_id, m["id"]))
            if cell:
                cell.set_sync_peaks(is_synced)

    def _on_cell_changed(self, channel_id: str, mix_id: str):
        state = self.pipewire_mgr.get_channel_state(channel_id, mix_id)
        if state.get("linked", True):
            if channel_id in self.channel_cards:
                self.channel_cards[channel_id].update_ui_state()
            for m in self.pipewire_mgr.mixes:
                cell = self.matrix_cells.get((channel_id, m["id"]))
                if cell:
                    cell.update_ui_state()

    def _on_external_sync(self, target_type: str = None, target_id: str = None):
        if target_type == "channel" and target_id:
            if target_id in self.channel_cards:
                self.channel_cards[target_id].update_ui_state()
            for (ch, m), cell in self.matrix_cells.items():
                if ch == target_id:
                    cell.update_ui_state()
            return
        elif target_type == "mix" and target_id:
            if target_id in self.mix_headers:
                self.mix_headers[target_id].update_ui_state()
            for (ch, m), cell in self.matrix_cells.items():
                if m == target_id:
                    cell.update_ui_state()
            return

        for card in self.channel_cards.values():
            card.update_ui_state()
        for cell in self.matrix_cells.values():
            cell.update_ui_state()
        for header in self.mix_headers.values():
            header.update_ui_state()

    def _on_hardware_sync(self, curr: dict, changed: dict):
        dial_mode = curr.get("dial_mode", "gain")

        # 1. Update 48V Phantom Power badge
        if "phantom_power" in changed:
            for ch_id, card in list(self.channel_cards.items()):
                if getattr(card, "is_wave_channel", False):
                    card.update_phantom_state(bool(changed["phantom_power"]))

        # 2. Update hardware mute badges in UI based on dial mode
        if "mute" in changed and "dial_mode" not in changed:
            is_muted = bool(changed["mute"])
            if dial_mode == "gain":
                # Setting 1 (LED 1): Mic Input only
                for ch_id, card in list(self.channel_cards.items()):
                    if getattr(card, "is_wave_channel", False) or ch_id in ("mic", "elgato_wave_xlr"):
                        card.set_muted(is_muted)
                        for mx in self.pipewire_mgr.mixes:
                            cell = self.matrix_cells.get((ch_id, mx["id"]))
                            if cell and hasattr(cell, "update_ui_state"):
                                cell.update_ui_state()

            elif dial_mode == "hp":
                # Setting 2 (LED 2): Headphone Output Mix only
                elgato_mix_id = "personal_mix"
                if self.hardware_mgr and hasattr(self.hardware_mgr, "_get_elgato_output_mix_id"):
                    elgato_mix_id = self.hardware_mgr._get_elgato_output_mix_id()
                target_header = self.mix_headers.get(elgato_mix_id)
                if not target_header:
                    for m_id, header in self.mix_headers.items():
                        if m_id in (elgato_mix_id, "personal", "personal_mix"):
                            target_header = header
                            break
                if target_header:
                    target_header.update_ui_state()

                sink_id = self._get_selected_output_sink_id()
                if sink_id and self.hardware_mgr:
                    self._update_out_mute_btn(is_muted)

            elif dial_mode == "mix":
                # Setting 3 (LED 3): BOTH Mic Input and Headphone Output Mix
                for ch_id, card in list(self.channel_cards.items()):
                    if getattr(card, "is_wave_channel", False) or ch_id in ("mic", "elgato_wave_xlr"):
                        card.set_muted(is_muted)
                        for mx in self.pipewire_mgr.mixes:
                            cell = self.matrix_cells.get((ch_id, mx["id"]))
                            if cell and hasattr(cell, "update_ui_state"):
                                cell.update_ui_state()

                elgato_mix_id = "personal_mix"
                if self.hardware_mgr and hasattr(self.hardware_mgr, "_get_elgato_output_mix_id"):
                    elgato_mix_id = self.hardware_mgr._get_elgato_output_mix_id()
                target_header = self.mix_headers.get(elgato_mix_id)
                if not target_header:
                    for m_id, header in self.mix_headers.items():
                        if m_id in (elgato_mix_id, "personal", "personal_mix"):
                            target_header = header
                            break
                if target_header:
                    target_header.update_ui_state()

                sink_id = self._get_selected_output_sink_id()
                if sink_id and self.hardware_mgr:
                    self._update_out_mute_btn(is_muted)

        # 3. Update Preamp Gain ONLY when knob is in Gain Mode (Mode 1 / 1st LED on Wave hardware)
        if "gain_db" in changed and dial_mode == "gain":
            val = float(changed["gain_db"])
            vol_pct = max(0, min(100, int(round((val / 75.0) * 100))))
            for ch_id, card in list(self.channel_cards.items()):
                if getattr(card, "is_wave_channel", False) or ch_id in ("mic", "elgato_wave_xlr"):
                    self.pipewire_mgr.set_channel_master_volume(ch_id, vol_pct)
                    card.set_master_volume(vol_pct, self.pipewire_mgr.get_channel_master_mute(ch_id))
                    if self.pipewire_mgr.is_channel_linked(ch_id):
                        for mx in self.pipewire_mgr.mixes:
                            cell = self.matrix_cells.get((ch_id, mx["id"]))
                            if cell and hasattr(cell, "update_ui_state"):
                                cell.update_ui_state()

        # 4. Update Headphone Monitor Mix Header ONLY when knob is in Output / HP Mode (Mode 2 / 2nd LED on Wave hardware)
        if "hp_volume_pct" in changed and dial_mode == "hp":
            hp_vol = int(round(changed["hp_volume_pct"]))
            assigned_mix = "personal"
            if self.hardware_mgr:
                assigned_mix = self.hardware_mgr.get_device_assigned_mix("Wave XLR") or self.hardware_mgr.get_device_assigned_mix(self.hardware_mgr.device_name) or "personal"
            
            target_header = self.mix_headers.get(assigned_mix)
            if not target_header:
                for m_id, header in self.mix_headers.items():
                    if m_id in (assigned_mix, "personal", "personal_mix") or "personal" in str(header.mix_info.get("name", "")).lower() or "wave" in str(header.mix_info.get("name", "")).lower():
                        target_header = header
                        assigned_mix = m_id
                        break
            if not target_header and self.mix_headers:
                target_header = list(self.mix_headers.values())[0]
                assigned_mix = list(self.mix_headers.keys())[0]

            if target_header:
                self.pipewire_mgr.set_mix_master_volume(assigned_mix, hp_vol)
                target_header.set_volume(hp_vol)

        # 5. Update Monitor Balance Fader when physical knob turns in Mode 3 (or any balance change)
        if "monitor_mix_pct" in changed and hasattr(self, "balance_scale"):
            val = int(round(changed["monitor_mix_pct"]))
            if int(round(self.balance_scale.get_value())) != val:
                self._syncing_hw_balance = True
                try:
                    if hasattr(self, "_bal_scale_handler") and self._bal_scale_handler:
                        self.balance_scale.handler_block(self._bal_scale_handler)
                        try:
                            self.balance_scale.set_value(val)
                        finally:
                            self.balance_scale.handler_unblock(self._bal_scale_handler)
                    else:
                        self.balance_scale.set_value(val)
                finally:
                    self._syncing_hw_balance = False
            self._format_balance_label(val)

    def _on_balance_slider_changed(self, scale):
        if getattr(self, "_syncing_hw_balance", False):
            return
        val = int(scale.get_value())
        self._format_balance_label(val)
        if self.hardware_mgr:
            self.hardware_mgr.set_monitor_mix(val, transient=True)

    def _format_balance_label(self, val: int):
        val = max(0, min(100, int(val)))
        if val == 50:
            self.balance_lbl.set_label("50/50")
        elif val == 0:
            self.balance_lbl.set_label("100% Mic")
        elif val == 100:
            self.balance_lbl.set_label("100% PC")
        else:
            mic_pct = 100 - val
            pc_pct = val
            self.balance_lbl.set_label(f"{mic_pct}/{pc_pct}")

    def _update_balance_visibility(self):
        idx = self.out_dropdown.get_selected()
        is_wave = False
        if idx < len(self.output_devices_list):
            dev = self.output_devices_list[idx]
            d_name = dev.get("name", "").lower()
            d_disp = dev.get("display_name", "").lower()
            d_key = str(dev.get("device_key", "")).lower()
            is_wave = dev.get("is_elgato", False) or "wave" in d_name or "elgato" in d_name or "wave" in d_disp or "wave" in d_key
        if hasattr(self, "balance_box"):
            self.balance_box.set_visible(is_wave)

    def _on_channel_deleted(self, ch_id: str):
        self.pipewire_mgr.remove_channel(ch_id)
        GLib.idle_add(self._rebuild_grid)

    def _on_link_toggled(self, channel_id: str, is_linked: bool):
        if channel_id in self.channel_cards:
            self.channel_cards[channel_id].update_ui_state()
        for m in self.pipewire_mgr.mixes:
            cell = self.matrix_cells.get((channel_id, m["id"]))
            if cell:
                cell.update_ui_state()

    def _rebuild_grid(self):
        while self.grid.get_first_child():
            self.grid.remove(self.grid.get_first_child())
        self.channel_cards.clear()
        self.matrix_cells.clear()
        self.mix_headers.clear()
        self._build_grid()

    def _on_output_dropdown_changed(self, dropdown, *args):
        idx = dropdown.get_selected()
        if idx < len(self.output_devices_list):
            dev = self.output_devices_list[idx]
            sink_id = dev.get("primary_sink_id") or (dev.get("sinks", [{}])[0].get("id") if dev.get("sinks") else None) or dev.get("id")
            if sink_id:
                self.hardware_mgr.set_active_output_device(sink_id)
                is_muted = self.hardware_mgr.get_output_mute(sink_id)
                self._update_out_mute_btn(is_muted)
        self._update_balance_visibility()

    def _get_selected_output_sink_id(self):
        idx = self.out_dropdown.get_selected()
        if idx < len(self.output_devices_list):
            dev = self.output_devices_list[idx]
            return dev.get("primary_sink_id") or (dev.get("sinks", [{}])[0].get("id") if dev.get("sinks") else None) or dev.get("id")
        return None

    def _on_output_mute_clicked(self, btn):
        sink_id = self._get_selected_output_sink_id()
        is_muted = self.hardware_mgr.toggle_output_mute(sink_id, transient=True)
        self._update_out_mute_btn(is_muted)

    def _update_out_mute_btn(self, is_muted: bool):
        if getattr(self, "_last_out_mute_state", None) == is_muted:
            return
        self._last_out_mute_state = is_muted

        if is_muted:
            self.out_mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.out_mute_btn.add_css_class("muted")
            self.out_mute_btn.set_tooltip_text("Unmute Output")
        else:
            self.out_mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.out_mute_btn.remove_css_class("muted")
            self.out_mute_btn.set_tooltip_text("Mute Output")

    def refresh_device_names(self):
        self.output_devices_list = self.hardware_mgr.get_tracked_output_devices() if self.hardware_mgr else []
        out_names = [d.get("display_name", d.get("name", "Output")) for d in self.output_devices_list] or ["No Configured Output"]
        curr_selected = self.out_dropdown.get_selected()
        self.out_dropdown.set_model(Gtk.StringList.new(out_names))
        if curr_selected < len(out_names):
            self.out_dropdown.set_selected(curr_selected)
        elif len(out_names) > 0:
            self.out_dropdown.set_selected(0)
        self._update_balance_visibility()

        if hasattr(self, "_refresh_create_mix_targets"):
            self._refresh_create_mix_targets()

        for header in self.mix_headers.values():
            if hasattr(header, "refresh_device_targets"):
                header.refresh_device_targets()

        for ch_id, card in self.channel_cards.items():
            if hasattr(card, "refresh_name"):
                card.refresh_name()

    def _on_ui_tick(self) -> bool:
        # Visibility Guard: Drop background render cycles when hidden / minimized / in other tabs
        if not self.get_mapped():
            return True

        # 1. Periodically verify physical hardware connectivity and mute state (~1s interval)
        self._hw_tick_counter = getattr(self, "_hw_tick_counter", 0) + 1
        if self._hw_tick_counter % 30 == 0:
            sink_id = self._get_selected_output_sink_id()
            if sink_id and self.hardware_mgr:
                is_muted = self.hardware_mgr.get_output_mute(sink_id)
                self._update_out_mute_btn(is_muted)
            for card in self.channel_cards.values():
                if hasattr(card, "refresh_hardware_state"):
                    card.refresh_hardware_state()

        # 2. Query each active channel's stereo peaks once per frame (deduplicated, lock-free)
        cached_peaks = {}
        for ch_id, card in self.channel_cards.items():
            peaks = self.peak_monitor.get_channel_stereo_peaks(ch_id)
            cached_peaks[ch_id] = peaks
            card.update_peaks(peaks[0], peaks[1])

        # 3. Push cached peaks to each sub-mix cell (attenuated by per-cell volume and mute state)
        for (channel_id, mix_id), cell in self.matrix_cells.items():
            p_l, p_r = cached_peaks.get(channel_id, (0.0, 0.0))
            st = self.pipewire_mgr.get_channel_state(channel_id, mix_id)
            if st.get("muted", False) or not st.get("enabled", True):
                cell.update_peaks(0.0, 0.0)
            else:
                vol_scale = max(0.0, min(1.5, st.get("volume", 80) / 100.0))
                cell.update_peaks(p_l * vol_scale, p_r * vol_scale)

        return True

    def refresh_all_faders(self):
        """Forces all channel cards, matrix submix cells, and mix headers to synchronize with PipeWire and hardware."""
        for ch_id, card in list(self.channel_cards.items()):
            vol = self.pipewire_mgr.get_channel_master_volume(ch_id)
            muted = self.pipewire_mgr.get_channel_master_mute(ch_id)
            card.set_master_volume(vol, muted)
            if hasattr(card, "refresh_hardware_state"):
                card.refresh_hardware_state()

        for (channel_id, mix_id), cell in list(self.matrix_cells.items()):
            if hasattr(cell, "update_ui_state"):
                cell.update_ui_state()

        for mix_id, header in list(self.mix_headers.items()):
            if hasattr(header, "update_ui_state"):
                header.update_ui_state()
