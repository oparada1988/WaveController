import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from .channel_card import ChannelCard
from .mix_header import MixHeaderCard
from .matrix_cell import MatrixCell

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
        
        self.channel_cards = {}
        self.matrix_cells = {} # {(channel_id, mix_id): MatrixCell}
        self.pipewire_mgr.on_external_change_callback = self._on_external_sync
        
        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(20)
        self.set_margin_end(20)

        # 1. Header Bar: Title + Output Device Selector
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        
        title_lbl = Gtk.Label(label="Mixes")
        title_lbl.add_css_class("wave-main-title")
        header_box.append(title_lbl)

        header_box.append(Gtk.Box(hexpand=True)) # Spacer

        # Output Target Selector Dropdown
        out_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        out_names = [self.hardware_mgr.get_device_display_name(d) for d in self.hardware_mgr.output_devices] or ["Default Output"]
        self.out_dropdown = Gtk.DropDown.new_from_strings(out_names)
        self.out_dropdown.add_css_class("wave-output-dropdown")
        for idx, d in enumerate(self.hardware_mgr.output_devices):
            if d.get("is_default"):
                self.out_dropdown.set_selected(idx)
                break
        self.out_dropdown.connect("notify::selected", self._on_output_dropdown_changed)
        out_box.append(self.out_dropdown)

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
        test_sound_btn.connect("clicked", lambda b: self.hardware_mgr.test_output_chime())
        out_box.append(test_sound_btn)

        header_box.append(out_box)
        self.append(header_box)

        # 2. Main Matrix Grid
        self.grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        self.grid.set_hexpand(True)
        self.grid.set_vexpand(True)

        self._build_grid()

        # Scrolled container for channels
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.grid)
        scrolled.set_vexpand(True)
        self.append(scrolled)

        # 40 FPS Timer to refresh meter levels and sync states
        GLib.timeout_add(25, self._on_ui_tick)

    def _build_grid(self):
        # Top-left empty header cell
        spacer = Gtk.Box()
        spacer.set_size_request(280, 48)
        self.grid.attach(spacer, 0, 0, 1, 1)

        # Mix Column Headers (Row 0, Columns 1..N)
        for col_idx, mix in enumerate(self.pipewire_mgr.mixes, start=1):
            mix_header = MixHeaderCard(mix)
            self.grid.attach(mix_header, col_idx, 0, 1, 1)

        # Create Mix (+) Button Card (Column N+1)
        create_mix_col = len(self.pipewire_mgr.mixes) + 1
        create_mix_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        create_mix_card.add_css_class("mix-header-card")
        create_mix_card.set_size_request(52, 48)
        create_mix_card.set_valign(Gtk.Align.CENTER)
        
        plus_btn = Gtk.MenuButton()
        plus_btn.set_icon_name("list-add-symbolic")
        plus_btn.add_css_class("flat")
        plus_btn.add_css_class("wave-icon-btn")
        plus_btn.set_tooltip_text("Add Custom Mix Output")
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
                on_channel_removed_callback=lambda ch_id: GLib.idle_add(self._rebuild_grid),
                on_channel_renamed_callback=lambda ch_id, name: GLib.idle_add(self._rebuild_grid)
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
        self._setup_create_channel_popover(create_btn)
        self.grid.attach(create_btn, 0, bottom_row, 1, 1)

    def _setup_create_mix_popover(self, menu_btn: Gtk.MenuButton):
        popover = Gtk.Popover()
        popover.add_css_class("wave-popover")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(240, -1)

        head_lbl = Gtk.Label(label="Add Custom Output Mix")
        head_lbl.add_css_class("mix-header-title")
        head_lbl.set_halign(Gtk.Align.START)
        box.append(head_lbl)

        name_entry = Gtk.Entry(placeholder_text="Mix Name (e.g. Stream Mix, VOD Mix)")
        box.append(name_entry)

        sub_entry = Gtk.Entry(placeholder_text="Subtitle (e.g. OBS Studio / Headphones)")
        box.append(sub_entry)

        color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        color_lbl = Gtk.Label(label="Color:")
        color_lbl.add_css_class("mix-header-subtitle")
        color_row.append(color_lbl)

        color_combo = Gtk.DropDown.new_from_strings(["Blue", "Green", "Red", "Purple", "Orange"])
        color_row.append(color_combo)
        box.append(color_row)

        add_btn = Gtk.Button(label="Create Mix")
        add_btn.add_css_class("suggested-action")
        
        def on_add(b):
            name = name_entry.get_text().strip()
            sub = sub_entry.get_text().strip() or "Custom Mix"
            if name:
                colors = ["#3584e4", "#3db356", "#e05252", "#9146ff", "#ff7800"]
                c_idx = color_combo.get_selected()
                c = colors[c_idx] if c_idx < len(colors) else "#3584e4"
                self.pipewire_mgr.add_mix(name, subtitle=sub, color=c)
                popover.popdown()
                GLib.idle_add(self._rebuild_grid)

        add_btn.connect("clicked", on_add)
        box.append(add_btn)

        popover.set_child(box)
        menu_btn.set_popover(popover)

    def _setup_create_channel_popover(self, menu_btn: Gtk.MenuButton):
        popover = Gtk.Popover()
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

        # ==========================================
        # Dynamic Refresh Handlers
        # ==========================================
        def show_app_page(b=None):
            while apps_list_container.get_first_child():
                apps_list_container.remove(apps_list_container.get_first_child())

            running_apps = self.pipewire_mgr.get_active_application_streams()
            if running_apps:
                for app_info in running_apps:
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
                no_apps = Gtk.Label(label="No audio applications running.\nLaunch an app (Spotify, Discord, etc.) or create custom below:")
                no_apps.add_css_class("mix-header-subtitle")
                no_apps.set_halign(Gtk.Align.START)
                no_apps.set_wrap(True)
                apps_list_container.append(no_apps)

            stack.set_visible_child_name("app_page")

        def show_device_page(b=None):
            while dev_list_container.get_first_child():
                dev_list_container.remove(dev_list_container.get_first_child())

            input_devs = self.hardware_mgr.input_devices if self.hardware_mgr else []
            if input_devs:
                for dev_info in input_devs:
                    dev_name = self.hardware_mgr.get_device_display_name(dev_info)
                    dev_item_btn = Gtk.Button()
                    dev_item_btn.add_css_class("flat")
                    dev_item_btn.add_css_class("wave-sidebar-row")

                    d_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                    d_img = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
                    d_img.set_pixel_size(18)
                    d_lbl = Gtk.Label(label=dev_name)
                    d_lbl.set_hexpand(True)
                    d_lbl.set_halign(Gtk.Align.START)
                    d_lbl.set_ellipsize(3)

                    d_add_ic = Gtk.Image.new_from_icon_name("list-add-symbolic")
                    d_add_ic.set_pixel_size(14)

                    d_row.append(d_img)
                    d_row.append(d_lbl)
                    d_row.append(d_add_ic)
                    dev_item_btn.set_child(d_row)

                    def make_dev_click_handler(name):
                        def handler(btn):
                            self.pipewire_mgr.add_channel(name, icon="audio-input-microphone-symbolic", ch_type="source")
                            popover.popdown()
                            GLib.idle_add(self._rebuild_grid)
                        return handler

                    dev_item_btn.connect("clicked", make_dev_click_handler(dev_name))
                    dev_list_container.append(dev_item_btn)
            else:
                no_devs = Gtk.Label(label="No hardware input devices found.")
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

        apps_list_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
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

        dev_list_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        dev_page_box.append(dev_list_container)

        stack.add_named(dev_page_box, "device_page")

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
            for m in self.pipewire_mgr.mixes:
                cell = self.matrix_cells.get((channel_id, m["id"]))
                if cell:
                    cell.update_ui_state()
            card = self.channel_cards.get(channel_id)
            if card:
                card.update_ui_state()

    def _on_external_sync(self):
        for ch_id, card in self.channel_cards.items():
            card.update_ui_state()
        for (channel_id, mix_id), cell in self.matrix_cells.items():
            cell.update_ui_state()

    def _on_link_toggled(self, channel_id: str, is_linked: bool):
        for m in self.pipewire_mgr.mixes:
            cell = self.matrix_cells.get((channel_id, m["id"]))
            if cell:
                cell.update_ui_state()

    def _rebuild_grid(self):
        while self.grid.get_first_child():
            self.grid.remove(self.grid.get_first_child())
        self.channel_cards.clear()
        self.matrix_cells.clear()
        self._build_grid()

    def _on_output_dropdown_changed(self, dropdown, *args):
        idx = dropdown.get_selected()
        if idx < len(self.hardware_mgr.output_devices):
            dev = self.hardware_mgr.output_devices[idx]
            self.hardware_mgr.set_active_output_device(dev["id"])
            sink_id = dev["id"]
            is_muted = self.hardware_mgr.get_output_mute(sink_id)
            self._update_out_mute_btn(is_muted)

    def _get_selected_output_sink_id(self):
        idx = self.out_dropdown.get_selected()
        if idx < len(self.hardware_mgr.output_devices):
            return self.hardware_mgr.output_devices[idx]["id"]
        return None

    def _on_output_mute_clicked(self, btn):
        sink_id = self._get_selected_output_sink_id()
        is_muted = self.hardware_mgr.toggle_output_mute(sink_id)
        self._update_out_mute_btn(is_muted)

    def _update_out_mute_btn(self, is_muted: bool):
        if is_muted:
            self.out_mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.out_mute_btn.add_css_class("muted")
            self.out_mute_btn.set_tooltip_text("Unmute Output")
        else:
            self.out_mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.out_mute_btn.remove_css_class("muted")
            self.out_mute_btn.set_tooltip_text("Mute Output")

    def refresh_device_names(self):
        out_names = [self.hardware_mgr.get_device_display_name(d) for d in self.hardware_mgr.output_devices] or ["Default Output"]
        curr_selected = self.out_dropdown.get_selected()
        self.out_dropdown.set_model(Gtk.StringList.new(out_names))
        if curr_selected < len(out_names):
            self.out_dropdown.set_selected(curr_selected)

        for ch_id, card in self.channel_cards.items():
            if hasattr(card, "refresh_name"):
                card.refresh_name()

    def _on_ui_tick(self) -> bool:
        # 1. Sync Output Mute Icon
        sink_id = self._get_selected_output_sink_id()
        is_muted = self.hardware_mgr.get_output_mute(sink_id)
        self._update_out_mute_btn(is_muted)

        # 2. Push real-time stereo peaks to channel cards (left column)
        for ch_id, card in self.channel_cards.items():
            peak_l, peak_r = self.peak_monitor.get_channel_stereo_peaks(ch_id)
            card.update_peaks(peak_l, peak_r)

        # 3. Push real-time stereo peaks to each sub-mix cell
        for (channel_id, mix_id), cell in self.matrix_cells.items():
            peak_l, peak_r = self.peak_monitor.get_channel_stereo_peaks(channel_id)
            cell.update_peaks(peak_l, peak_r)
        return True

