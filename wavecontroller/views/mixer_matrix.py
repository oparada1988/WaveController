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
        out_names = [d["name"] for d in self.hardware_mgr.output_devices] or ["Default Output"]
        self.out_dropdown = Gtk.DropDown.new_from_strings(out_names)
        self.out_dropdown.add_css_class("wave-output-dropdown")
        for idx, d in enumerate(self.hardware_mgr.output_devices):
            if d.get("is_default"):
                self.out_dropdown.set_selected(idx)
                break
        self.out_dropdown.connect("notify::selected", self._on_output_dropdown_changed)
        out_box.append(self.out_dropdown)

        test_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        test_btn.add_css_class("flat")
        test_btn.add_css_class("wave-icon-btn")
        test_btn.set_tooltip_text("Test Output (Play Chime)")
        test_btn.connect("clicked", lambda b: self.hardware_mgr.test_output_chime())
        out_box.append(test_btn)

        popout_btn = Gtk.Button.new_from_icon_name("external-link-symbolic")
        popout_btn.add_css_class("flat")
        popout_btn.add_css_class("wave-icon-btn")
        popout_btn.set_tooltip_text("Routing & Patchbay")
        out_box.append(popout_btn)

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
            card = ChannelCard(ch, self.pipewire_mgr, self.hardware_mgr, on_link_toggle_callback=self._on_link_toggled)
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

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(260, -1)

        head_lbl = Gtk.Label(label="Add Audio Channel")
        head_lbl.add_css_class("mix-header-title")
        head_lbl.set_halign(Gtk.Align.START)
        box.append(head_lbl)

        sub_lbl = Gtk.Label(label="Click to add running application:")
        sub_lbl.add_css_class("mix-header-subtitle")
        sub_lbl.set_halign(Gtk.Align.START)
        box.append(sub_lbl)

        # Running applications quick add buttons
        active_apps = self.pipewire_mgr.get_active_application_streams()
        common_apps = ["Spotify", "Discord", "Steam", "Chromium", "Firefox", "VLC"]
        
        shown_apps = []
        for a in active_apps:
            if a["name"] not in shown_apps:
                shown_apps.append(a["name"])
        for ca in common_apps:
            if ca not in shown_apps:
                shown_apps.append(ca)

        app_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for app_name in shown_apps[:6]:
            app_btn = Gtk.Button()
            app_btn.add_css_class("flat")
            app_btn.add_css_class("wave-sidebar-row")

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            icon_name = self.pipewire_mgr.resolve_icon_for_app(app_name)
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(18)
            lbl = Gtk.Label(label=app_name)
            lbl.set_hexpand(True)
            lbl.set_halign(Gtk.Align.START)

            add_icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
            add_icon.set_pixel_size(14)

            row_box.append(img)
            row_box.append(lbl)
            row_box.append(add_icon)
            app_btn.set_child(row_box)

            def make_click_handler(name):
                def handler(b):
                    self.pipewire_mgr.add_channel(name)
                    popover.popdown()
                    GLib.idle_add(self._rebuild_grid)
                return handler

            app_btn.connect("clicked", make_click_handler(app_name))
            app_list_box.append(app_btn)

        box.append(app_list_box)

        # Custom Channel Entry
        cust_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry = Gtk.Entry(placeholder_text="Custom channel name...")
        entry.set_hexpand(True)
        cust_box.append(entry)

        cust_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        cust_btn.add_css_class("suggested-action")
        
        def on_cust_add(b):
            name = entry.get_text().strip()
            if name:
                self.pipewire_mgr.add_channel(name)
                popover.popdown()
                GLib.idle_add(self._rebuild_grid)

        cust_btn.connect("clicked", on_cust_add)
        cust_box.append(cust_btn)
        box.append(cust_box)

        popover.set_child(box)
        menu_btn.set_popover(popover)

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

    def _on_ui_tick(self) -> bool:
        # Push real-time stereo peaks to channel cards (left column)
        for ch_id, card in self.channel_cards.items():
            peak_l, peak_r = self.peak_monitor.get_channel_stereo_peaks(ch_id)
            card.update_peaks(peak_l, peak_r)

        # Push real-time stereo peaks to each sub-mix cell
        for (channel_id, mix_id), cell in self.matrix_cells.items():
            peak_l, peak_r = self.peak_monitor.get_channel_stereo_peaks(channel_id)
            cell.update_peaks(peak_l, peak_r)
        return True
