import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, Adw, GLib, Pango
import cairo
import math
from .stereo_slider import StereoSlider
from .shortcuts_dialog import ShortcutsManager
from ..engine.config_manager import config_manager
from ..utils.logger import get_logger

log = get_logger("CompactMixer")

TAU = math.tau


class VerticalStereoSlider(Gtk.DrawingArea):
    """
    High-performance vertical dual-channel stereo volume fader and live audio level meter.
    Supports smooth 60 FPS drag interaction, mouse wheel scroll tuning,
    double-click reset to 100%, and real-time Left/Right VU meter animations.
    """
    def __init__(self, volume: int = 80, is_muted: bool = False, sync_peaks: bool = False, on_volume_changed=None):
        super().__init__()
        self.volume = max(0, min(100, volume))
        self.is_muted = is_muted
        self.sync_peaks = sync_peaks
        self.peak_l = 0.0
        self.peak_r = 0.0
        self.on_volume_changed = on_volume_changed
        self.is_dragging = False
        self._drag_start_y = 0.0
        self._drag_start_vol = self.volume

        self.set_content_width(34)
        self.set_content_height(170)
        self.set_vexpand(True)
        self.set_hexpand(False)
        self.set_valign(Gtk.Align.FILL)
        self.set_cursor_from_name("pointer")
        self.set_draw_func(self._draw)

        # Drag gesture (up increases volume, down decreases volume)
        drag_gesture = Gtk.GestureDrag()
        drag_gesture.connect("drag-begin", self._on_drag_begin)
        drag_gesture.connect("drag-update", self._on_drag_update)
        drag_gesture.connect("drag-end", self._on_drag_end)
        self.add_controller(drag_gesture)

        # Mouse scroll wheel controller (±2% volume, ±5% with Shift)
        scroll_ctrl = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll_ctrl.connect("scroll", self._on_scroll)
        self.add_controller(scroll_ctrl)

        # Double-click to reset volume to 100% (unity gain)
        click_gesture = Gtk.GestureClick.new()
        click_gesture.connect("released", self._on_click_released)
        self.add_controller(click_gesture)

    def set_volume(self, volume: int, is_muted: bool = False):
        if self.is_dragging:
            return
        new_vol = max(0, min(100, volume))
        if self.volume != new_vol or self.is_muted != is_muted:
            self.volume = new_vol
            self.is_muted = is_muted
            self.queue_draw()

    def set_peaks(self, peak_l: float, peak_r: float):
        prev_l, prev_r = self.peak_l, self.peak_r
        if self.sync_peaks:
            p = max(peak_l, peak_r)
            target_l, target_r = p, p
        else:
            target_l, target_r = peak_l, peak_r

        # Critically damped broadcast ballistics: smooth rise on beats, natural gravity release
        if target_l > self.peak_l:
            self.peak_l = min(1.0, self.peak_l + (target_l - self.peak_l) * 0.45)
        else:
            self.peak_l = max(0.0, self.peak_l + (target_l - self.peak_l) * 0.20)
            if self.peak_l < 0.005:
                self.peak_l = 0.0

        if target_r > self.peak_r:
            self.peak_r = min(1.0, self.peak_r + (target_r - self.peak_r) * 0.45)
        else:
            self.peak_r = max(0.0, self.peak_r + (target_r - self.peak_r) * 0.20)
            if self.peak_r < 0.005:
                self.peak_r = 0.0

        # Trigger GTK repaint whenever peaks are active or changing
        if (self.peak_l > 0.0 or prev_l > 0.0 or target_l > 0.0 or
            self.peak_r > 0.0 or prev_r > 0.0 or target_r > 0.0 or
            abs(self.peak_l - prev_l) > 0.001 or abs(self.peak_r - prev_r) > 0.001):
            self.queue_draw()

    def _calc_vol_from_y(self, y: float) -> int:
        height = float(self.get_height())
        margin = 12.0
        track_h = height - (2.0 * margin)
        if track_h <= 0:
            return self.volume
        # Top = 100%, Bottom = 0%
        pct = max(0.0, min(1.0, 1.0 - ((y - margin) / track_h)))
        return int(round(pct * 100))

    def _on_drag_begin(self, gesture, start_x, start_y):
        self.is_dragging = True
        self._drag_start_y = start_y
        new_vol = self._calc_vol_from_y(start_y)
        if new_vol != self.volume:
            self.volume = new_vol
            self.queue_draw()
            if self.on_volume_changed:
                self.on_volume_changed(self.volume)

    def _on_drag_update(self, gesture, offset_x, offset_y):
        curr_y = self._drag_start_y + offset_y
        new_vol = self._calc_vol_from_y(curr_y)
        if new_vol != self.volume:
            self.volume = new_vol
            self.queue_draw()
            if self.on_volume_changed:
                self.on_volume_changed(self.volume)

    def _on_drag_end(self, gesture, offset_x, offset_y):
        self.is_dragging = False

    def _on_scroll(self, controller, dx, dy):
        state = controller.get_current_event_state()
        step = 5 if (state & Gdk.ModifierType.SHIFT_MASK) else 2
        delta = step if dy < 0 else (-step if dy > 0 else 0)
        if delta != 0:
            new_vol = max(0, min(100, self.volume + delta))
            if new_vol != self.volume:
                self.volume = new_vol
                self.queue_draw()
                if self.on_volume_changed:
                    self.on_volume_changed(self.volume)
        return True

    def _on_click_released(self, gesture, n_press, x, y):
        if n_press == 2:  # Double-click to reset to 100% (unity gain)
            if self.volume != 100:
                self.volume = 100
                self.queue_draw()
                if self.on_volume_changed:
                    self.on_volume_changed(self.volume)

    def _draw(self, area, cr, width, height):
        margin_y = 12.0
        track_h = max(10.0, float(height) - (2.0 * margin_y))
        track_w = 3.0
        gap = 4.0

        x_left = (float(width) - (2.0 * track_w + gap)) / 2.0
        x_right = x_left + track_w + gap

        vol_pct = float(self.volume) / 100.0
        vol_h = track_h * vol_pct

        thumb_x = float(width) / 2.0
        thumb_y = margin_y + track_h * (1.0 - vol_pct)
        thumb_r = 6.5

        alpha = 0.35 if self.is_muted else 1.0

        # Recessed background tracks
        cr.set_source_rgba(0.09, 0.09, 0.11, alpha)
        cr.rectangle(x_left, margin_y, track_w, track_h)
        cr.rectangle(x_right, margin_y, track_w, track_h)
        cr.fill()

        # Active volume tracks (dark slate guide from bottom up to fader)
        cr.set_source_rgba(0.18, 0.18, 0.22, alpha)
        cr.rectangle(x_left, margin_y + track_h - vol_h, track_w, vol_h)
        cr.rectangle(x_right, margin_y + track_h - vol_h, track_w, vol_h)
        cr.fill()

        # Live VU meter bars bounded by fader knob position
        if not self.is_muted and vol_h > 0.0 and (self.peak_l > 0.005 or self.peak_r > 0.005):
            meter_h_l = min(vol_h, track_h * self.peak_l)
            meter_h_r = min(vol_h, track_h * self.peak_r)
            if meter_h_l > 0.5 or meter_h_r > 0.5:
                # Vertical gradient: bottom (green) -> middle (yellow) -> top (red)
                pat = cairo.LinearGradient(0, margin_y + track_h, 0, margin_y)
                pat.add_color_stop_rgba(0.00, 0.24, 0.70, 0.34, 1.0)   # Vivid Emerald Green #3db356
                pat.add_color_stop_rgba(0.65, 0.24, 0.70, 0.34, 1.0)  # Green up to 65%
                pat.add_color_stop_rgba(0.85, 0.95, 0.75, 0.20, 1.0)  # Warm Yellow at 85%
                pat.add_color_stop_rgba(1.00, 0.95, 0.30, 0.25, 1.0)  # Studio Red at 100%

                cr.set_source(pat)
                if meter_h_l > 0.5:
                    cr.rectangle(x_left, margin_y + track_h - meter_h_l, track_w, meter_h_l)
                if meter_h_r > 0.5:
                    cr.rectangle(x_right, margin_y + track_h - meter_h_r, track_w, meter_h_r)
                cr.fill()

        # Draggable Blue Fader Knob
        if self.is_muted:
            cr.set_source_rgba(0.35, 0.35, 0.40, 0.8)
        else:
            # Drop shadow
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.4)
            cr.arc(thumb_x + 0.5, thumb_y + 0.8, thumb_r, 0, TAU)
            cr.fill()

            # Knob body
            cr.set_source_rgba(0.21, 0.52, 0.89, 1.0)

        cr.arc(thumb_x, thumb_y, thumb_r, 0, TAU)
        cr.fill()

        # White border highlight
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.7 if not self.is_muted else 0.3)
        cr.set_line_width(1.0)
        cr.arc(thumb_x, thumb_y, thumb_r, 0, TAU)
        cr.stroke()


class CompactChannelStrip(Gtk.Box):
    """
    Vertical channel strip used in Horizontal Compact Mode.
    Contains:
    - Channel Icon
    - Channel Name (centered, ellipsized)
    - Mute Button
    - Vertical Stereo Slider & VU Meter
    - Volume Percentage Label
    """
    def __init__(self, channel_info: dict, target_mode: str, pipewire_mgr, on_change_callback=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.channel_info = channel_info
        self.channel_id = channel_info["id"]
        self.target_mode = target_mode  # "all" or specific mix_id
        self.pipewire_mgr = pipewire_mgr
        self.on_change_callback = on_change_callback

        self.add_css_class("compact-strip-card")
        self.set_size_request(108, 340)
        self.set_valign(Gtk.Align.FILL)
        self.set_hexpand(False)

        # 1. Channel Icon
        icon_name = self._resolve_icon()
        self.icon_img = Gtk.Image.new_from_icon_name(icon_name)
        self.icon_img.set_pixel_size(24)
        self.icon_img.set_halign(Gtk.Align.CENTER)
        self.append(self.icon_img)

        # 2. Channel Name Label (wrapped up to 2 lines to fit full channel/submix names)
        display_name = channel_info.get("name", "Channel")
        self.name_lbl = Gtk.Label(label=display_name)
        self.name_lbl.add_css_class("compact-channel-name")
        self.name_lbl.set_halign(Gtk.Align.CENTER)
        self.name_lbl.set_justify(Gtk.Justification.CENTER)
        self.name_lbl.set_wrap(True)
        self.name_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.name_lbl.set_lines(2)
        self.name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.name_lbl.set_max_width_chars(14)
        self.name_lbl.set_size_request(96, 30)
        self.name_lbl.set_tooltip_text(display_name)
        self.append(self.name_lbl)

        # 3. Mute Button
        self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.add_css_class("wave-icon-btn")
        self.mute_btn.add_css_class("compact-mute-btn")
        self.mute_btn.set_halign(Gtk.Align.CENTER)
        self.mute_btn.set_tooltip_text("Mute")
        self.mute_btn.connect("clicked", self._on_mute_clicked)
        self.append(self.mute_btn)

        # 4. Vertical Slider & Real-time VU Meter
        vol, muted = self._get_current_state()
        is_synced = self.pipewire_mgr.get_channel_sync_meter(self.channel_id)
        self.slider = VerticalStereoSlider(
            volume=vol,
            is_muted=muted,
            sync_peaks=is_synced,
            on_volume_changed=self._on_volume_changed
        )
        self.append(self.slider)

        # 5. Volume % Label
        self.vol_lbl = Gtk.Label(label=f"{vol}%")
        self.vol_lbl.add_css_class("compact-vol-label")
        self.vol_lbl.set_halign(Gtk.Align.CENTER)
        self.append(self.vol_lbl)

        self._apply_mute_ui(muted)

    def _resolve_icon(self) -> str:
        ch_icon = self.channel_info.get("icon")
        if ch_icon and ch_icon not in ("network-offline-symbolic", "audio-card-symbolic", "audio-x-generic-symbolic"):
            return ch_icon
        assigned = self.pipewire_mgr.get_assigned_apps(self.channel_id) if self.pipewire_mgr else []
        primary_app = assigned[0] if assigned else self.channel_info.get("name", "")
        resolved = self.pipewire_mgr.resolve_icon_for_app(primary_app) if self.pipewire_mgr else None
        return resolved or ch_icon or "audio-speakers-symbolic"

    def _get_current_state(self):
        if self.target_mode == "all":
            vol = self.pipewire_mgr.get_channel_master_volume(self.channel_id)
            muted = self.pipewire_mgr.get_channel_master_mute(self.channel_id)
        else:
            vol = self.pipewire_mgr.get_channel_volume(self.channel_id, self.target_mode)
            muted = self.pipewire_mgr.get_channel_mute(self.channel_id, self.target_mode)
        return vol, muted

    def _apply_mute_ui(self, muted: bool):
        if muted:
            self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.mute_btn.add_css_class("muted")
            self.add_css_class("muted")
            self.icon_img.set_opacity(0.4)
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")
            self.remove_css_class("muted")
            self.icon_img.set_opacity(1.0)

    def _on_mute_clicked(self, btn):
        vol, curr_muted = self._get_current_state()
        new_muted = not curr_muted
        if self.target_mode == "all":
            self.pipewire_mgr.set_channel_master_mute(self.channel_id, new_muted)
        else:
            self.pipewire_mgr.set_channel_mute(self.channel_id, self.target_mode, new_muted)
        self.slider.set_volume(vol, new_muted)
        self._apply_mute_ui(new_muted)
        if self.on_change_callback:
            self.on_change_callback()

    def _on_volume_changed(self, volume: int):
        self.vol_lbl.set_label(f"{volume}%")
        if self.target_mode == "all":
            self.pipewire_mgr.set_channel_master_volume(self.channel_id, volume)
        else:
            self.pipewire_mgr.set_channel_volume(self.channel_id, self.target_mode, volume)
        if self.on_change_callback:
            self.on_change_callback()

    def update_peaks(self, raw_peak_l: float, raw_peak_r: float):
        vol, muted = self._get_current_state()
        if muted:
            self.slider.set_peaks(0.0, 0.0)
        else:
            scale = vol / 100.0
            self.slider.set_peaks(raw_peak_l * scale, raw_peak_r * scale)

    def sync_state(self):
        vol, muted = self._get_current_state()
        self.slider.set_volume(vol, muted)
        self.vol_lbl.set_label(f"{vol}%")
        self._apply_mute_ui(muted)


class CompactChannelRow(Gtk.Box):
    """
    Horizontal channel row used in Vertical Compact Mode.
    Contains:
    - Channel Icon
    - Channel Name (left-aligned, bold)
    - Mute Button
    - Horizontal Stereo Slider & VU Meter
    - Volume Percentage Label
    """
    def __init__(self, channel_info: dict, target_mode: str, pipewire_mgr, on_change_callback=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.channel_info = channel_info
        self.channel_id = channel_info["id"]
        self.target_mode = target_mode  # "all" or specific mix_id
        self.pipewire_mgr = pipewire_mgr
        self.on_change_callback = on_change_callback

        self.add_css_class("compact-row-card")
        self.set_size_request(380, 46)
        self.set_valign(Gtk.Align.CENTER)
        self.set_hexpand(True)

        # 1. Channel Icon
        icon_name = self._resolve_icon()
        self.icon_img = Gtk.Image.new_from_icon_name(icon_name)
        self.icon_img.set_pixel_size(20)
        self.icon_img.set_valign(Gtk.Align.CENTER)
        self.append(self.icon_img)

        # 2. Channel Name
        display_name = channel_info.get("name", "Channel")
        self.name_lbl = Gtk.Label(label=display_name)
        self.name_lbl.add_css_class("compact-channel-name")
        self.name_lbl.set_halign(Gtk.Align.START)
        self.name_lbl.set_valign(Gtk.Align.CENTER)
        self.name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.name_lbl.set_width_chars(15)
        self.name_lbl.set_max_width_chars(20)
        self.name_lbl.set_tooltip_text(display_name)
        self.append(self.name_lbl)

        # 3. Mute Button
        self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.add_css_class("wave-icon-btn")
        self.mute_btn.add_css_class("compact-mute-btn")
        self.mute_btn.set_valign(Gtk.Align.CENTER)
        self.mute_btn.set_tooltip_text("Mute")
        self.mute_btn.connect("clicked", self._on_mute_clicked)
        self.append(self.mute_btn)

        # 4. Horizontal Stereo Slider & Real-time VU Meter
        vol, muted = self._get_current_state()
        is_synced = self.pipewire_mgr.get_channel_sync_meter(self.channel_id)
        self.slider = StereoSlider(
            volume=vol,
            is_muted=muted,
            sync_peaks=is_synced,
            on_volume_changed=self._on_volume_changed
        )
        self.slider.set_content_width(110)
        self.slider.set_hexpand(True)
        self.append(self.slider)

        # 5. Volume % Label
        self.vol_lbl = Gtk.Label(label=f"{vol}%")
        self.vol_lbl.add_css_class("compact-vol-label")
        self.vol_lbl.set_valign(Gtk.Align.CENTER)
        self.vol_lbl.set_width_chars(4)
        self.append(self.vol_lbl)

        self._apply_mute_ui(muted)

    def _resolve_icon(self) -> str:
        ch_icon = self.channel_info.get("icon")
        if ch_icon and ch_icon not in ("network-offline-symbolic", "audio-card-symbolic", "audio-x-generic-symbolic"):
            return ch_icon
        assigned = self.pipewire_mgr.get_assigned_apps(self.channel_id) if self.pipewire_mgr else []
        primary_app = assigned[0] if assigned else self.channel_info.get("name", "")
        resolved = self.pipewire_mgr.resolve_icon_for_app(primary_app) if self.pipewire_mgr else None
        return resolved or ch_icon or "audio-speakers-symbolic"

    def _get_current_state(self):
        if self.target_mode == "all":
            vol = self.pipewire_mgr.get_channel_master_volume(self.channel_id)
            muted = self.pipewire_mgr.get_channel_master_mute(self.channel_id)
        else:
            vol = self.pipewire_mgr.get_channel_volume(self.channel_id, self.target_mode)
            muted = self.pipewire_mgr.get_channel_mute(self.channel_id, self.target_mode)
        return vol, muted

    def _apply_mute_ui(self, muted: bool):
        if muted:
            self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.mute_btn.add_css_class("muted")
            self.add_css_class("muted")
            self.icon_img.set_opacity(0.4)
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")
            self.remove_css_class("muted")
            self.icon_img.set_opacity(1.0)

    def _on_mute_clicked(self, btn):
        vol, curr_muted = self._get_current_state()
        new_muted = not curr_muted
        if self.target_mode == "all":
            self.pipewire_mgr.set_channel_master_mute(self.channel_id, new_muted)
        else:
            self.pipewire_mgr.set_channel_mute(self.channel_id, self.target_mode, new_muted)
        self.slider.set_volume(vol, new_muted)
        self._apply_mute_ui(new_muted)
        if self.on_change_callback:
            self.on_change_callback()

    def _on_volume_changed(self, volume: int):
        self.vol_lbl.set_label(f"{volume}%")
        if self.target_mode == "all":
            self.pipewire_mgr.set_channel_master_volume(self.channel_id, volume)
        else:
            self.pipewire_mgr.set_channel_volume(self.channel_id, self.target_mode, volume)
        if self.on_change_callback:
            self.on_change_callback()

    def update_peaks(self, raw_peak_l: float, raw_peak_r: float):
        vol, muted = self._get_current_state()
        if muted:
            self.slider.set_peaks(0.0, 0.0)
        else:
            scale = vol / 100.0
            self.slider.set_peaks(raw_peak_l * scale, raw_peak_r * scale)

    def sync_state(self):
        vol, muted = self._get_current_state()
        self.slider.set_volume(vol, muted)
        self.vol_lbl.set_label(f"{vol}%")
        self._apply_mute_ui(muted)


class CompactMixMasterStrip(Gtk.Box):
    """
    Vertical mix master output strip shown at the end of the channel list in Horizontal Compact Mode
    when a specific submix is selected as target.
    """
    def __init__(self, mix_info: dict, pipewire_mgr, on_change_callback=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.mix_info = mix_info
        self.mix_id = mix_info["id"]
        self.pipewire_mgr = pipewire_mgr
        self.on_change_callback = on_change_callback

        self.add_css_class("compact-strip-card")
        self.add_css_class("compact-mix-master-card")
        self.set_size_request(108, 340)
        self.set_valign(Gtk.Align.FILL)
        self.set_hexpand(False)

        # 1. Mix Icon
        icon_name = mix_info.get("icon", "personal-symbolic")
        self.icon_img = Gtk.Image.new_from_icon_name(icon_name)
        self.icon_img.set_pixel_size(24)
        self.icon_img.set_halign(Gtk.Align.CENTER)
        self.append(self.icon_img)

        # 2. Mix Name Label
        display_name = f"{mix_info.get('name', 'Mix')} Output"
        self.name_lbl = Gtk.Label(label=display_name)
        self.name_lbl.add_css_class("compact-channel-name")
        self.name_lbl.set_halign(Gtk.Align.CENTER)
        self.name_lbl.set_justify(Gtk.Justification.CENTER)
        self.name_lbl.set_wrap(True)
        self.name_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.name_lbl.set_lines(2)
        self.name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.name_lbl.set_max_width_chars(14)
        self.name_lbl.set_size_request(96, 30)
        self.name_lbl.set_tooltip_text(display_name)
        self.append(self.name_lbl)

        # 3. Mute Button
        vol = self.pipewire_mgr.get_mix_master_volume(self.mix_id)
        muted = self.pipewire_mgr.get_mix_master_mute(self.mix_id)

        self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.add_css_class("wave-icon-btn")
        self.mute_btn.add_css_class("compact-mute-btn")
        self.mute_btn.set_halign(Gtk.Align.CENTER)
        self.mute_btn.set_tooltip_text(f"Mute {mix_info.get('name', 'Mix')} Output")
        if muted:
            self.mute_btn.add_css_class("muted")
            self.add_css_class("muted")
        self.mute_btn.connect("clicked", self._on_mute_clicked)
        self.append(self.mute_btn)

        # 4. Vertical Slider
        self.slider = VerticalStereoSlider(
            volume=vol,
            is_muted=muted,
            sync_peaks=False,
            on_volume_changed=self._on_volume_changed
        )
        self.append(self.slider)

        # 5. Volume % Label
        self.vol_lbl = Gtk.Label(label=f"{vol}%")
        self.vol_lbl.add_css_class("compact-vol-label")
        self.vol_lbl.set_halign(Gtk.Align.CENTER)
        self.append(self.vol_lbl)

    def _on_mute_clicked(self, btn):
        is_muted = self.pipewire_mgr.toggle_mix_master_mute(self.mix_id)
        vol = self.pipewire_mgr.get_mix_master_volume(self.mix_id)
        self.slider.set_volume(vol, is_muted)
        self._apply_mute_ui(is_muted)
        if self.on_change_callback:
            self.on_change_callback()

    def _on_volume_changed(self, volume: int):
        self.vol_lbl.set_label(f"{volume}%")
        self.pipewire_mgr.set_mix_master_volume(self.mix_id, volume)
        if self.on_change_callback:
            self.on_change_callback()

    def _apply_mute_ui(self, muted: bool):
        if muted:
            self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.mute_btn.add_css_class("muted")
            self.add_css_class("muted")
            self.icon_img.set_opacity(0.4)
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")
            self.remove_css_class("muted")
            self.icon_img.set_opacity(1.0)

    def sync_state(self):
        vol = self.pipewire_mgr.get_mix_master_volume(self.mix_id)
        muted = self.pipewire_mgr.get_mix_master_mute(self.mix_id)
        self.slider.set_volume(vol, muted)
        self.vol_lbl.set_label(f"{vol}%")
        self._apply_mute_ui(muted)


class CompactMixMasterRow(Gtk.Box):
    """
    Horizontal mix master output row shown at the bottom of the channel list in Vertical Compact Mode
    when a specific submix is selected as target.
    """
    def __init__(self, mix_info: dict, pipewire_mgr, on_change_callback=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.mix_info = mix_info
        self.mix_id = mix_info["id"]
        self.pipewire_mgr = pipewire_mgr
        self.on_change_callback = on_change_callback

        self.add_css_class("compact-row-card")
        self.add_css_class("compact-mix-master-card")
        self.set_size_request(380, 46)
        self.set_valign(Gtk.Align.CENTER)
        self.set_hexpand(True)

        # 1. Mix Icon
        icon_name = mix_info.get("icon", "personal-symbolic")
        self.icon_img = Gtk.Image.new_from_icon_name(icon_name)
        self.icon_img.set_pixel_size(20)
        self.icon_img.set_valign(Gtk.Align.CENTER)
        self.append(self.icon_img)

        # 2. Mix Name
        display_name = f"{mix_info.get('name', 'Mix')} Output"
        self.name_lbl = Gtk.Label(label=display_name)
        self.name_lbl.add_css_class("compact-channel-name")
        self.name_lbl.set_halign(Gtk.Align.START)
        self.name_lbl.set_valign(Gtk.Align.CENTER)
        self.name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.name_lbl.set_width_chars(15)
        self.name_lbl.set_max_width_chars(20)
        self.name_lbl.set_tooltip_text(display_name)
        self.append(self.name_lbl)

        # 3. Mute Button
        vol = self.pipewire_mgr.get_mix_master_volume(self.mix_id)
        muted = self.pipewire_mgr.get_mix_master_mute(self.mix_id)

        self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.add_css_class("wave-icon-btn")
        self.mute_btn.add_css_class("compact-mute-btn")
        self.mute_btn.set_valign(Gtk.Align.CENTER)
        self.mute_btn.set_tooltip_text(f"Mute {mix_info.get('name', 'Mix')} Output")
        if muted:
            self.mute_btn.add_css_class("muted")
            self.add_css_class("muted")
        self.mute_btn.connect("clicked", self._on_mute_clicked)
        self.append(self.mute_btn)

        # 4. Horizontal Stereo Slider
        self.slider = StereoSlider(
            volume=vol,
            is_muted=muted,
            sync_peaks=False,
            on_volume_changed=self._on_volume_changed
        )
        self.slider.set_content_width(110)
        self.slider.set_hexpand(True)
        self.append(self.slider)

        # 5. Volume % Label
        self.vol_lbl = Gtk.Label(label=f"{vol}%")
        self.vol_lbl.add_css_class("compact-vol-label")
        self.vol_lbl.set_halign(Gtk.Align.END)
        self.vol_lbl.set_valign(Gtk.Align.CENTER)
        self.vol_lbl.set_size_request(36, -1)
        self.append(self.vol_lbl)

    def _on_mute_clicked(self, btn):
        is_muted = self.pipewire_mgr.toggle_mix_master_mute(self.mix_id)
        vol = self.pipewire_mgr.get_mix_master_volume(self.mix_id)
        self.slider.set_volume(vol, is_muted)
        self._apply_mute_ui(is_muted)
        if self.on_change_callback:
            self.on_change_callback()

    def _on_volume_changed(self, volume: int):
        self.vol_lbl.set_label(f"{volume}%")
        self.pipewire_mgr.set_mix_master_volume(self.mix_id, volume)
        if self.on_change_callback:
            self.on_change_callback()

    def _apply_mute_ui(self, muted: bool):
        if muted:
            self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.mute_btn.add_css_class("muted")
            self.add_css_class("muted")
            self.icon_img.set_opacity(0.4)
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")
            self.remove_css_class("muted")
            self.icon_img.set_opacity(1.0)

    def sync_state(self):
        vol = self.pipewire_mgr.get_mix_master_volume(self.mix_id)
        muted = self.pipewire_mgr.get_mix_master_mute(self.mix_id)
        self.slider.set_volume(vol, muted)
        self.vol_lbl.set_label(f"{vol}%")
        self._apply_mute_ui(muted)


class CompactMixerWindow(Adw.Window):
    """
    Dedicated compact mode window for WaveController.
    Features:
    - Seamless switching between Horizontal (vertical strips) and Vertical (stacked rows) layouts.
    - Target mode selector: 'All Channels' (master faders) or specific submix (only enabled channels in that submix).
    - 60 FPS hardware-synchronized VU meters with broadcast ballistics.
    - Full Mode launcher button to return to the full application.
    """
    def __init__(self, pipewire_mgr, peak_monitor, hardware_mgr=None, restore_callback=None):
        super().__init__()
        self.pipewire_mgr = pipewire_mgr
        self.peak_monitor = peak_monitor
        self.hardware_mgr = hardware_mgr
        self.restore_callback = restore_callback

        self.channel_widgets = {}  # {channel_id: widget}
        self.mix_widget = None
        self.current_layout = config_manager.get("compact_mode_layout", "horizontal")
        self.current_target = config_manager.get("compact_mode_target", "all")
        self._shortcuts_mgr = ShortcutsManager(self)

        self.set_title("WaveController")
        self.set_resizable(True)
        self.add_css_class("compact-mixer-window")
        self.add_css_class("theme-midnight")

        self.connect("close-request", self._on_close_request)

        # Root ToolbarView
        toolbar_view = Adw.ToolbarView()

        # HeaderBar
        self.header_bar = Adw.HeaderBar()
        self.header_bar.set_show_title(False)
        self.header_bar.add_css_class("compact-header-bar")

        # 1. Target Selector (Left / Start)
        self._build_target_selector()
        self.header_bar.pack_start(self.target_btn)

        # 2. Options Menu Button (Right / End)
        self._build_options_menu()
        self.header_bar.pack_end(self.menu_btn)

        # 3. Full Mode Button (Right / End)
        self.full_mode_btn = Gtk.Button.new_from_icon_name("full-mode-symbolic")
        self.full_mode_btn.add_css_class("flat")
        self.full_mode_btn.add_css_class("wave-icon-btn")
        self.full_mode_btn.set_tooltip_text("Return to Full Mode (Ctrl+M / Esc)")
        self.full_mode_btn.connect("clicked", lambda b: self._restore_full_mode())
        self.header_bar.pack_end(self.full_mode_btn)

        # 4. Layout Switcher Button (Right / End)
        self.layout_btn = Gtk.Button()
        self.layout_btn.add_css_class("flat")
        self.layout_btn.add_css_class("wave-icon-btn")
        self.layout_btn.connect("clicked", self._toggle_layout)
        self._update_layout_btn_icon()
        self.header_bar.pack_end(self.layout_btn)

        toolbar_view.add_top_bar(self.header_bar)

        # Content Scrolled Window
        self.scrolled = Gtk.ScrolledWindow()
        self.scrolled.set_hexpand(True)
        self.scrolled.set_vexpand(True)
        self.scrolled.add_css_class("compact-scrolled-window")

        self.content_container = Gtk.Box()
        self.scrolled.set_child(self.content_container)
        toolbar_view.set_content(self.scrolled)

        self.set_content(toolbar_view)

        # Register keyboard accelerators (Ctrl+M and Esc to return to Full Mode, F1 for Shortcuts)
        shortcut_ctrl = Gtk.ShortcutController.new()
        shortcut_ctrl.set_scope(Gtk.ShortcutScope.LOCAL)

        def _action_wrap(cb):
            return Gtk.CallbackAction.new(lambda *a: (cb(), True)[1])

        shortcuts = [
            ("<Control>m", lambda: self._restore_full_mode()),
            ("Escape", lambda: self._restore_full_mode()),
            ("F1", lambda: self.show_shortcuts_window()),
            ("<Control>question", lambda: self.show_shortcuts_window()),
            ("<Control>slash", lambda: self.show_shortcuts_window()),
        ]

        for trig_str, cb in shortcuts:
            trigger = Gtk.ShortcutTrigger.parse_string(trig_str)
            if trigger:
                shortcut_ctrl.add_shortcut(Gtk.Shortcut.new(trigger, _action_wrap(cb)))
        self.add_controller(shortcut_ctrl)

        # Initial layout build and window geometry
        self._rebuild_content()
        self._apply_geometry_for_layout()

        # Telemetry Tick Timer (~40 FPS for smooth VU meters without CPU overhead)
        self._tick_source = GLib.timeout_add(25, self._on_ui_tick)
        self.connect("destroy", lambda w: self.cleanup())

        # External IPC & Hardware Event Listeners
        if self.pipewire_mgr and hasattr(self.pipewire_mgr, "add_external_change_listener"):
            self.pipewire_mgr.add_external_change_listener(self._on_external_sync)
        if self.hardware_mgr and hasattr(self.hardware_mgr, "add_hardware_listener"):
            self._hw_listener = lambda curr, changed: GLib.idle_add(self._on_hardware_sync, curr, changed)
            self.hardware_mgr.add_hardware_listener(self._hw_listener)

    def _build_target_selector(self):
        """Constructs the Target selector dropdown button and popover."""
        self.target_btn = Gtk.MenuButton()
        self.target_btn.add_css_class("flat")
        self.target_btn.add_css_class("compact-target-btn")
        self.target_btn.set_tooltip_text("Select Mix or Channels to control")

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.target_icon = Gtk.Image()
        self.target_icon.set_pixel_size(18)
        self.target_label = Gtk.Label()
        self.target_label.add_css_class("compact-target-label")

        chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        chevron.set_pixel_size(12)
        chevron.set_opacity(0.65)

        btn_box.append(self.target_icon)
        btn_box.append(self.target_label)
        btn_box.append(chevron)
        self.target_btn.set_child(btn_box)

        # Target Popover Menu
        self.target_popover = Gtk.Popover()
        self.target_popover.add_css_class("compact-target-popover")
        self._rebuild_target_popover_content()
        self.target_btn.set_popover(self.target_popover)

        self._update_target_btn_ui()

    def _rebuild_target_popover_content(self):
        popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        popover_box.set_margin_top(6)
        popover_box.set_margin_bottom(6)
        popover_box.set_margin_start(6)
        popover_box.set_margin_end(6)
        popover_box.set_size_request(190, -1)

        title_lbl = Gtk.Label(label="Control Target")
        title_lbl.add_css_class("wave-sidebar-section-title")
        title_lbl.set_halign(Gtk.Align.START)
        popover_box.append(title_lbl)

        # 1. "All Channels" row
        all_btn = self._create_target_row_btn("all", "All Channels", "all-channels-symbolic")
        popover_box.append(all_btn)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(4)
        sep.set_margin_bottom(4)
        popover_box.append(sep)

        mixes_lbl = Gtk.Label(label="Submixes")
        mixes_lbl.add_css_class("wave-sidebar-section-title")
        mixes_lbl.set_halign(Gtk.Align.START)
        popover_box.append(mixes_lbl)

        # 2. Dynamic Mixes rows
        mixes = getattr(self.pipewire_mgr, "mixes", [])
        for m in mixes:
            m_id = m.get("id")
            m_name = m.get("name", "Mix")
            m_icon = m.get("icon", "personal-symbolic")
            mix_btn = self._create_target_row_btn(m_id, m_name, m_icon)
            popover_box.append(mix_btn)

        self.target_popover.set_child(popover_box)

    def _create_target_row_btn(self, target_id: str, label_text: str, icon_name: str) -> Gtk.Button:
        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.add_css_class("compact-target-row")
        if self.current_target == target_id:
            btn.add_css_class("selected")

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        img = Gtk.Image.new_from_icon_name(icon_name)
        img.set_pixel_size(18)
        lbl = Gtk.Label(label=label_text)
        lbl.set_halign(Gtk.Align.START)
        lbl.set_hexpand(True)

        row.append(img)
        row.append(lbl)

        if self.current_target == target_id:
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.set_pixel_size(14)
            check.add_css_class("accent-icon")
            row.append(check)

        btn.set_child(row)
        btn.connect("clicked", lambda b, tid=target_id: self._select_target(tid))
        return btn

    def _select_target(self, target_id: str):
        self.current_target = target_id
        config_manager.set("compact_mode_target", target_id, immediate=True)
        self.target_popover.popdown()
        self._rebuild_target_popover_content()
        self._update_target_btn_ui()
        self._rebuild_content()

    def _update_target_btn_ui(self):
        if self.current_target == "all":
            self.target_icon.set_from_icon_name("all-channels-symbolic")
            self.target_label.set_label("All Channels")
        else:
            mix = next((m for m in getattr(self.pipewire_mgr, "mixes", []) if m.get("id") == self.current_target), None)
            if mix:
                self.target_icon.set_from_icon_name(mix.get("icon", "personal-symbolic"))
                self.target_label.set_label(mix.get("name", "Mix"))
            else:
                self.target_icon.set_from_icon_name("all-channels-symbolic")
                self.target_label.set_label("All Channels")
                self.current_target = "all"

    def _build_options_menu(self):
        """Constructs the [ ⋮ ] menu button with options."""
        self.menu_btn = Gtk.MenuButton()
        self.menu_btn.set_icon_name("open-menu-symbolic")
        self.menu_btn.add_css_class("flat")
        self.menu_btn.add_css_class("wave-icon-btn")
        self.menu_btn.set_tooltip_text("Menu")

        menu_popover = Gtk.Popover()
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        menu_box.set_margin_top(8)
        menu_box.set_margin_bottom(8)
        menu_box.set_margin_start(8)
        menu_box.set_margin_end(8)
        menu_box.set_size_request(180, -1)

        # Layout Toggle Row
        layout_btn = Gtk.Button()
        layout_btn.add_css_class("flat")
        layout_btn.add_css_class("compact-target-row")
        layout_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        layout_box.append(Gtk.Image.new_from_icon_name("preferences-desktop-display-symbolic"))
        l_lbl = Gtk.Label(label="Switch Layout")
        l_lbl.set_halign(Gtk.Align.START)
        layout_box.append(l_lbl)
        layout_btn.set_child(layout_box)
        layout_btn.connect("clicked", lambda b: (menu_popover.popdown(), self._toggle_layout(b)))
        menu_box.append(layout_btn)

        # Full Mode Row
        full_btn = Gtk.Button()
        full_btn.add_css_class("flat")
        full_btn.add_css_class("compact-target-row")
        full_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        full_box.append(Gtk.Image.new_from_icon_name("full-mode-symbolic"))
        f_lbl = Gtk.Label(label="Full Mode (Ctrl+M)")
        f_lbl.set_halign(Gtk.Align.START)
        full_box.append(f_lbl)
        full_btn.set_child(full_box)
        full_btn.connect("clicked", lambda b: (menu_popover.popdown(), self._restore_full_mode()))
        menu_box.append(full_btn)

        # Keyboard Shortcuts Row
        sc_btn = Gtk.Button()
        sc_btn.add_css_class("flat")
        sc_btn.add_css_class("compact-target-row")
        sc_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sc_box.append(Gtk.Image.new_from_icon_name("help-about-symbolic"))
        sc_lbl = Gtk.Label(label="Shortcuts (F1)")
        sc_lbl.set_halign(Gtk.Align.START)
        sc_box.append(sc_lbl)
        sc_btn.set_child(sc_box)
        sc_btn.connect("clicked", lambda b: (menu_popover.popdown(), self.show_shortcuts_window()))
        menu_box.append(sc_btn)

        menu_popover.set_child(menu_box)
        self.menu_btn.set_popover(menu_popover)

    def show_shortcuts_window(self):
        """Displays the native GTK4/Libadwaita Keyboard Shortcuts overview dialog."""
        self._shortcuts_mgr.show(transient_parent=self)

    def _update_layout_btn_icon(self):
        if self.current_layout == "horizontal":
            self.layout_btn.set_icon_name("view-list-symbolic")
            self.layout_btn.set_tooltip_text("Switch to Vertical Layout (List)")
        else:
            self.layout_btn.set_icon_name("view-column-symbolic")
            self.layout_btn.set_tooltip_text("Switch to Horizontal Layout (Cards)")

    def _toggle_layout(self, btn=None):
        self.current_layout = "vertical" if self.current_layout == "horizontal" else "horizontal"
        config_manager.set("compact_mode_layout", self.current_layout, immediate=True)
        self._update_layout_btn_icon()
        self._apply_geometry_for_layout()
        self._rebuild_content()

    def _apply_geometry_for_layout(self):
        if self.current_layout == "horizontal":
            # Horizontal cards: wide and moderately tall
            self.scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
            self.set_default_size(520, 420)
        else:
            # Vertical rows: narrow and taller
            self.scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.set_default_size(420, 460)

    def _rebuild_content(self):
        """Reconstructs the active layout: horizontal strips or vertical rows."""
        self.channel_widgets.clear()
        self.mix_widget = None

        # Clear existing container
        while self.content_container.get_first_child():
            self.content_container.remove(self.content_container.get_first_child())

        # Set container orientation
        if self.current_layout == "horizontal":
            self.content_container.set_orientation(Gtk.Orientation.HORIZONTAL)
            self.content_container.set_spacing(10)
            self.content_container.add_css_class("compact-horizontal-box")
            self.content_container.remove_css_class("compact-vertical-box")
        else:
            self.content_container.set_orientation(Gtk.Orientation.VERTICAL)
            self.content_container.set_spacing(8)
            self.content_container.add_css_class("compact-vertical-box")
            self.content_container.remove_css_class("compact-horizontal-box")

        all_channels = getattr(self.pipewire_mgr, "channels", [])

        # Filter channels based on target mode:
        # If "all", show all channels.
        # If a mix ID, ONLY show channels enabled in this mix.
        if self.current_target == "all":
            channels_to_show = all_channels
        else:
            channels_to_show = [
                c for c in all_channels
                if self.pipewire_mgr.is_channel_mix_enabled(c["id"], self.current_target)
            ]

        if not channels_to_show:
            empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            empty_box.set_halign(Gtk.Align.CENTER)
            empty_box.set_valign(Gtk.Align.CENTER)
            empty_box.set_hexpand(True)
            empty_box.set_vexpand(True)
            empty_box.set_margin_top(40)
            empty_box.set_margin_bottom(40)

            empty_icon = Gtk.Image.new_from_icon_name("audio-volume-muted-symbolic")
            empty_icon.set_pixel_size(36)
            empty_icon.set_opacity(0.4)
            empty_box.append(empty_icon)

            msg = "No channels routed to this mix." if self.current_target != "all" else "No channels configured."
            lbl = Gtk.Label(label=msg)
            lbl.add_css_class("dim-label")
            empty_box.append(lbl)

            hint_btn = Gtk.Button(label="Open Full Mode to Route Channels")
            hint_btn.add_css_class("flat")
            hint_btn.add_css_class("accent-btn")
            hint_btn.connect("clicked", lambda b: self._restore_full_mode())
            empty_box.append(hint_btn)

            self.content_container.append(empty_box)
            return

        for ch in channels_to_show:
            ch_id = ch["id"]
            if self.current_layout == "horizontal":
                widget = CompactChannelStrip(
                    channel_info=ch,
                    target_mode=self.current_target,
                    pipewire_mgr=self.pipewire_mgr,
                    on_change_callback=self._on_channel_state_changed
                )
            else:
                widget = CompactChannelRow(
                    channel_info=ch,
                    target_mode=self.current_target,
                    pipewire_mgr=self.pipewire_mgr,
                    on_change_callback=self._on_channel_state_changed
                )
            self.channel_widgets[ch_id] = widget
            self.content_container.append(widget)

        # If a specific mix is targeted, add the Mix Master Output Strip/Row at the end
        if self.current_target != "all":
            mix_info = next((m for m in getattr(self.pipewire_mgr, "mixes", []) if m.get("id") == self.current_target), None)
            if mix_info:
                sep = Gtk.Separator(
                    orientation=Gtk.Orientation.VERTICAL if self.current_layout == "horizontal" else Gtk.Orientation.HORIZONTAL
                )
                sep.set_margin_start(2)
                sep.set_margin_end(2)
                sep.set_margin_top(2)
                sep.set_margin_bottom(2)
                self.content_container.append(sep)

                if self.current_layout == "horizontal":
                    self.mix_widget = CompactMixMasterStrip(
                        mix_info=mix_info,
                        pipewire_mgr=self.pipewire_mgr,
                        on_change_callback=self._on_channel_state_changed
                    )
                else:
                    self.mix_widget = CompactMixMasterRow(
                        mix_info=mix_info,
                        pipewire_mgr=self.pipewire_mgr,
                        on_change_callback=self._on_channel_state_changed
                    )
                self.content_container.append(self.mix_widget)

    def _on_channel_state_changed(self):
        """Called when user tweaks a fader or mute in compact mode."""
        pass

    def _on_ui_tick(self) -> bool:
        """Periodic real-time VU meter and telemetry update."""
        if not self.get_mapped():
            return True

        if not self.peak_monitor:
            return True

        for ch_id, widget in self.channel_widgets.items():
            peaks = self.peak_monitor.get_channel_stereo_peaks(ch_id)
            widget.update_peaks(peaks[0], peaks[1])

        return True

    def refresh(self):
        """Re-reads channels, mixes, and syncs all UI elements."""
        self._rebuild_target_popover_content()
        self._update_target_btn_ui()
        self._rebuild_content()

    def sync_all_faders(self):
        for widget in self.channel_widgets.values():
            if hasattr(widget, "sync_state"):
                widget.sync_state()
        if hasattr(self, "mix_widget") and self.mix_widget and hasattr(self.mix_widget, "sync_state"):
            self.mix_widget.sync_state()

    def _on_hardware_sync(self, curr: dict, changed: dict):
        """Called when Elgato hardware dials or capacitive mute change."""
        self.sync_all_faders()

    def _on_external_sync(self, target_type: str = None, target_id: str = None, value = None):
        """Called when VolumeControllerPlus or external IPC modifies channels or mixes."""
        if target_type == "channel" and target_id:
            widget = self.channel_widgets.get(target_id)
            if widget and hasattr(widget, "sync_state"):
                widget.sync_state()
            else:
                if self.current_target != "all":
                    is_enabled = self.pipewire_mgr.is_channel_mix_enabled(target_id, self.current_target)
                    if is_enabled:
                        self._rebuild_content()
            return
        elif target_type == "mix" and target_id:
            if hasattr(self, "mix_widget") and self.mix_widget and hasattr(self.mix_widget, "sync_state"):
                if getattr(self.mix_widget, "mix_id", None) == target_id:
                    self.mix_widget.sync_state()
            if self.current_target == target_id:
                self.sync_all_faders()
            return

        self.sync_all_faders()

    def _restore_full_mode(self):
        self.set_visible(False)
        if self.restore_callback:
            self.restore_callback()

    def _on_close_request(self, win):
        self.set_visible(False)
        if self.restore_callback:
            self.restore_callback()
        return True

    def cleanup(self):
        """Cancels tick timers and detaches resources on destruction."""
        if hasattr(self, "_tick_source") and self._tick_source is not None:
            GLib.source_remove(self._tick_source)
            self._tick_source = None
        if hasattr(self, "pipewire_mgr") and self.pipewire_mgr and hasattr(self.pipewire_mgr, "remove_external_change_listener"):
            self.pipewire_mgr.remove_external_change_listener(self._on_external_sync)
        if hasattr(self, "_hw_listener") and self.hardware_mgr and hasattr(self.hardware_mgr, "remove_hardware_listener"):
            self.hardware_mgr.remove_hardware_listener(self._hw_listener)

