import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from .stereo_slider import StereoSlider

class MatrixCell(Gtk.Box):
    """
    An individual sub-mix cell that supports two states:
    1. Active (Routed): Contains mute button, dual-track stereo volume slider, real-time VU meters, and a remove button.
    2. Unrouted (Empty Slot): Clean dark slot matching Wave Link with a '+' button to route this channel into the mix.
    """
    def __init__(self, channel_id: str, mix_id: str, pipewire_mgr, on_change_callback=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.channel_id = channel_id
        self.mix_id = mix_id
        self.pipewire_mgr = pipewire_mgr
        self.on_change_callback = on_change_callback
        
        self.set_hexpand(False)
        self.set_valign(Gtk.Align.CENTER)
        self.set_size_request(200, 40)

        self.slider = None
        self.mute_btn = None
        self.del_btn = None
        self.add_btn = None

        self._build_cell_content()

    def _build_cell_content(self):
        # Clear existing children
        while self.get_first_child():
            self.remove(self.get_first_child())

        is_compatible = self.pipewire_mgr.is_channel_mix_compatible(self.channel_id, self.mix_id)
        is_enabled = self.pipewire_mgr.is_channel_mix_enabled(self.channel_id, self.mix_id)

        if is_enabled and is_compatible:
            self.remove_css_class("matrix-cell-empty")
            self.remove_css_class("matrix-cell-incompatible")
            self.add_css_class("matrix-cell-card")

            # 1. Mute button
            self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
            self.mute_btn.add_css_class("flat")
            self.mute_btn.add_css_class("wave-icon-btn")
            self.mute_btn.set_valign(Gtk.Align.CENTER)
            self.mute_btn.connect("clicked", self._on_mute_clicked)
            self.append(self.mute_btn)

            # 2. Stereo Split Volume Slider & VU Meter
            state = self.pipewire_mgr.get_channel_state(self.channel_id, self.mix_id)
            is_synced = self.pipewire_mgr.get_channel_sync_meter(self.channel_id)
            self.slider = StereoSlider(
                volume=state.get("volume", 80),
                is_muted=state.get("muted", False),
                sync_peaks=is_synced,
                on_volume_changed=self._on_slider_volume_changed
            )
            self.append(self.slider)

            # 3. Subtle Remove from Mix button (Eject / Unroute)
            self.del_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
            self.del_btn.add_css_class("flat")
            self.del_btn.add_css_class("wave-icon-btn")
            self.del_btn.add_css_class("matrix-cell-eject-btn")
            self.del_btn.set_tooltip_text("Remove channel from this mix")
            self.del_btn.set_valign(Gtk.Align.CENTER)
            self.del_btn.connect("clicked", self._on_remove_clicked)
            self.append(self.del_btn)

            self.update_ui_state()
        elif is_compatible:
            # Compatible but unrouted slot: render '+' button
            self.remove_css_class("matrix-cell-card")
            self.remove_css_class("matrix-cell-incompatible")
            self.remove_css_class("muted")
            self.add_css_class("matrix-cell-empty")

            self.slider = None
            self.mute_btn = None
            self.del_btn = None

            # Empty Slot centered '+' button
            empty_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            empty_box.set_halign(Gtk.Align.CENTER)
            empty_box.set_hexpand(True)
            empty_box.set_valign(Gtk.Align.CENTER)

            self.add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
            self.add_btn.add_css_class("flat")
            self.add_btn.add_css_class("matrix-cell-add-btn")
            self.add_btn.set_tooltip_text("Add channel to this mix")
            self.add_btn.connect("clicked", self._on_add_clicked)
            empty_box.append(self.add_btn)

            self.append(empty_box)
        else:
            # Incompatible stream type: solid matching placeholder card (same color/border as channel card)
            self.remove_css_class("matrix-cell-empty")
            self.remove_css_class("matrix-cell-incompatible")
            self.remove_css_class("muted")
            self.add_css_class("matrix-cell-card")
            self.add_css_class("matrix-cell-placeholder")

            self.slider = None
            self.mute_btn = None
            self.del_btn = None
            self.add_btn = None

            placeholder_box = Gtk.Box()
            placeholder_box.set_hexpand(True)
            self.append(placeholder_box)

    def _on_add_clicked(self, btn):
        self.pipewire_mgr.set_channel_mix_enabled(self.channel_id, self.mix_id, True)
        self._build_cell_content()
        if self.on_change_callback:
            self.on_change_callback(self.channel_id, self.mix_id)

    def _on_remove_clicked(self, btn):
        self.pipewire_mgr.set_channel_mix_enabled(self.channel_id, self.mix_id, False)
        self._build_cell_content()
        if self.on_change_callback:
            self.on_change_callback(self.channel_id, self.mix_id)

    def set_sync_peaks(self, sync: bool):
        if self.slider:
            self.slider.set_sync_peaks(sync)

    def _on_mute_clicked(self, btn):
        is_muted = self.pipewire_mgr.toggle_channel_mute(self.channel_id, self.mix_id)
        self.update_ui_state()
        if self.on_change_callback:
            self.on_change_callback(self.channel_id, self.mix_id)

    def _on_slider_volume_changed(self, vol: int):
        self.pipewire_mgr.set_channel_volume(self.channel_id, self.mix_id, vol)
        if self.on_change_callback:
            self.on_change_callback(self.channel_id, self.mix_id)

    def update_peaks(self, peak_l: float, peak_r: float):
        if self.slider:
            self.slider.set_peaks(peak_l, peak_r)

    def update_ui_state(self):
        if not self.slider or not self.mute_btn:
            return
        state = self.pipewire_mgr.get_channel_state(self.channel_id, self.mix_id)
        vol = state.get("volume", 80)
        muted = state.get("muted", False)
        
        self.slider.set_volume(vol, muted)
            
        if muted:
            self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.mute_btn.add_css_class("muted")
            self.add_css_class("muted")
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")
            self.remove_css_class("muted")
