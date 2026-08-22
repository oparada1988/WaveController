import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

from .stereo_slider import StereoSlider

class MatrixCell(Gtk.Box):
    """
    An individual sub-mix fader cell containing a mute button and a dual-track
    stereo volume slider with real-time bouncing green level meters.
    """
    def __init__(self, channel_id: str, mix_id: str, pipewire_mgr, on_change_callback=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
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

        # Stereo Split Volume Slider & VU Meter
        state = self.pipewire_mgr.get_channel_state(self.channel_id, self.mix_id)
        self.slider = StereoSlider(
            volume=state.get("volume", 80),
            is_muted=state.get("muted", False),
            on_volume_changed=self._on_slider_volume_changed
        )
        self.append(self.slider)

        self.update_ui_state()

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
        self.slider.set_peaks(peak_l, peak_r)

    def update_ui_state(self):
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
