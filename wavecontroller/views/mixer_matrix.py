import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from .channel_card import ChannelCard
from .mix_header import MixHeaderCard
from .matrix_cell import MatrixCell

class MixerMatrixView(Gtk.Box):
    """
    Main Matrix Sub-Mixer view displaying input channels vs. output sub-mixes.
    Matches the Wave Link layout with live audio meters and independent faders.
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
        self.out_dropdown = Gtk.DropDown.new_from_strings([
            "Headphones (Starship/Matisse HD Audio)",
            "fifine Microphone Digital Stereo (IEC958)",
            "Navi 31 HDMI/DP Audio"
        ])
        self.out_dropdown.add_css_class("wave-output-dropdown")
        out_box.append(self.out_dropdown)

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
        spacer.set_size_request(180, 48)
        self.grid.attach(spacer, 0, 0, 1, 1)

        # Mix Column Headers (Row 0, Columns 1..N)
        for col_idx, mix in enumerate(self.pipewire_mgr.mixes, start=1):
            mix_header = MixHeaderCard(mix)
            self.grid.attach(mix_header, col_idx, 0, 1, 1)

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

        # Bottom "+ Create channel" button
        bottom_row = len(self.pipewire_mgr.channels) + 1
        create_btn = Gtk.Button(label="+ Create channel")
        create_btn.add_css_class("create-channel-btn")
        create_btn.connect("clicked", self._on_create_channel_clicked)
        self.grid.attach(create_btn, 0, bottom_row, 1, 1)

    def _on_cell_changed(self, channel_id: str, mix_id: str):
        # Update all cells in this channel if linked
        state = self.pipewire_mgr.get_channel_state(channel_id, mix_id)
        if state.get("linked", True):
            for m in self.pipewire_mgr.mixes:
                cell = self.matrix_cells.get((channel_id, m["id"]))
                if cell:
                    cell.update_ui_state()

    def _on_link_toggled(self, channel_id: str, is_linked: bool):
        pass

    def _on_create_channel_clicked(self, btn):
        dialog = Adw.MessageDialog(
            heading="Create Virtual Channel",
            body="Enter a name for the new sub-mix channel:"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)

        entry = Gtk.Entry(placeholder_text="e.g. Discord, Spotify, Browser")
        entry.set_margin_top(12)
        entry.set_margin_bottom(12)
        dialog.set_extra_child(entry)

        def on_response(dlg, resp):
            if resp == "create":
                name = entry.get_text().strip()
                if name:
                    self.pipewire_mgr.add_channel(name)
                    # Clear and rebuild grid
                    while self.grid.get_first_child():
                        self.grid.remove(self.grid.get_first_child())
                    self.channel_cards.clear()
                    self.matrix_cells.clear()
                    self._build_grid()

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _on_ui_tick(self) -> bool:
        # Update UI states if needed
        return True
