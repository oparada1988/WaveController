import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

class MatrixCell(Gtk.Box):
    """
    An individual sub-mix fader cell containing a mute button, level slider, and VU meter.
    """
    def __init__(self, channel_id: str, mix_id: str, pipewire_mgr, on_change_callback=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.channel_id = channel_id
        self.mix_id = mix_id
        self.pipewire_mgr = pipewire_mgr
        self.on_change_callback = on_change_callback
        
        self.add_css_class("matrix-cell-card")
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.CENTER)

        # Mute button
        self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.add_css_class("wave-icon-btn")
        self.mute_btn.set_valign(Gtk.Align.CENTER)
        self.mute_btn.connect("clicked", self._on_mute_clicked)
        self.append(self.mute_btn)

        # Slider + Meter container
        slider_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        slider_box.set_hexpand(True)
        slider_box.set_valign(Gtk.Align.CENTER)

        # Level slider
        state = self.pipewire_mgr.get_channel_state(self.channel_id, self.mix_id)
        self.adj = Gtk.Adjustment(value=state.get("volume", 80), lower=0, upper=100, step_increment=1, page_increment=5)
        self.slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adj)
        self.slider.add_css_class("wave-slider")
        self.slider.set_hexpand(True)
        self.slider.set_draw_value(False)
        self.slider.connect("value-changed", self._on_slider_changed)
        slider_box.append(self.slider)

        self.append(slider_box)

        self.update_ui_state()

    def _on_mute_clicked(self, btn):
        is_muted = self.pipewire_mgr.toggle_channel_mute(self.channel_id, self.mix_id)
        self.update_ui_state()
        if self.on_change_callback:
            self.on_change_callback(self.channel_id, self.mix_id)

    def _on_slider_changed(self, scale):
        vol = int(self.adj.get_value())
        self.pipewire_mgr.set_channel_volume(self.channel_id, self.mix_id, vol)
        if self.on_change_callback:
            self.on_change_callback(self.channel_id, self.mix_id)

    def update_ui_state(self):
        state = self.pipewire_mgr.get_channel_state(self.channel_id, self.mix_id)
        vol = state.get("volume", 80)
        muted = state.get("muted", False)
        
        if abs(self.adj.get_value() - vol) > 0.5:
            self.adj.set_value(vol)
            
        if muted:
            self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.mute_btn.add_css_class("muted")
            self.add_css_class("muted")
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")
            self.remove_css_class("muted")
