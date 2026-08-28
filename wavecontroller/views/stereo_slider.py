import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk
import cairo

class StereoSlider(Gtk.DrawingArea):
    """
    High-performance dual-channel stereo volume fader and live audio level meter.
    Features silky-smooth 60 FPS drag interaction, mouse wheel scroll tuning,
    double-click reset, and real-time Left/Right VU meter animations.
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
        self._drag_start_x = 0.0
        self._drag_start_vol = self.volume

        self.set_content_width(120)
        self.set_content_height(20)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.CENTER)
        self.set_cursor_from_name("pointer")
        self.set_draw_func(self._draw)

        # Single unified GestureDrag for both click-to-seek and smooth dragging
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

        # Critically damped broadcast ballistics: smooth rise on beats, natural acoustic gravity release
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

        # Only trigger GTK repaint when levels actually change, eliminating idle redraw CPU burn
        if abs(self.peak_l - prev_l) > 0.002 or abs(self.peak_r - prev_r) > 0.002:
            self.queue_draw()

    def set_sync_peaks(self, sync: bool):
        if self.sync_peaks != sync:
            self.sync_peaks = sync
            if sync:
                p = max(self.peak_l, self.peak_r)
                self.peak_l = p
                self.peak_r = p
            self.queue_draw()

    def _calc_vol_from_x(self, x: float) -> int:
        width = float(self.get_width())
        margin = 6.0
        track_w = width - (2.0 * margin)
        if track_w <= 0:
            return self.volume
        pct = max(0.0, min(1.0, (x - margin) / track_w))
        return int(round(pct * 100))

    def _on_drag_begin(self, gesture, start_x, start_y):
        self.is_dragging = True
        self._drag_start_x = start_x
        new_vol = self._calc_vol_from_x(start_x)
        if new_vol != self.volume:
            self.volume = new_vol
            self.queue_draw()
            if self.on_volume_changed:
                self.on_volume_changed(self.volume)

    def _on_drag_update(self, gesture, offset_x, offset_y):
        curr_x = self._drag_start_x + offset_x
        new_vol = self._calc_vol_from_x(curr_x)
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
        delta = -step if dy > 0 else (step if dy < 0 else 0)
        if delta != 0:
            new_vol = max(0, min(100, self.volume + delta))
            if new_vol != self.volume:
                self.volume = new_vol
                self.queue_draw()
                if self.on_volume_changed:
                    self.on_volume_changed(self.volume)
        return True

    def _on_click_released(self, gesture, n_press, x, y):
        if n_press == 2:  # Double click to reset to 100% (unity gain)
            if self.volume != 100:
                self.volume = 100
                self.queue_draw()
                if self.on_volume_changed:
                    self.on_volume_changed(self.volume)

    def _draw(self, area, cr, width, height):
        margin = 6.0
        track_w = max(10.0, float(width) - (2.0 * margin))
        track_h = 3.0
        gap = 3.0
        
        y_top = (float(height) - (2.0 * track_h + gap)) / 2.0
        y_bot = y_top + track_h + gap

        vol_pct = float(self.volume) / 100.0
        vol_w = track_w * vol_pct
        thumb_x = margin + vol_w
        thumb_y = float(height) / 2.0
        thumb_r = 5.0

        alpha = 0.35 if self.is_muted else 1.0

        # Recessed background tracks (batched for Left & Right)
        cr.set_source_rgba(0.09, 0.09, 0.11, alpha)
        cr.rectangle(margin, y_top, track_w, track_h)
        cr.rectangle(margin, y_bot, track_w, track_h)
        cr.fill()

        # Active volume tracks (dark slate guide, batched)
        cr.set_source_rgba(0.18, 0.18, 0.22, alpha)
        cr.rectangle(margin, y_top, vol_w, track_h)
        cr.rectangle(margin, y_bot, vol_w, track_h)
        cr.fill()

        # Live VU meter bars bounded by fader knob (fader pushes back the meter)
        if not self.is_muted and vol_w > 0.0 and (self.peak_l > 0.005 or self.peak_r > 0.005):
            meter_l = min(vol_w, vol_w * self.peak_l)
            meter_r = min(vol_w, vol_w * self.peak_r)
            if meter_l > 0.5 or meter_r > 0.5:
                if getattr(self, "_cached_gradient_vol_w", None) != vol_w:
                    pat = cairo.LinearGradient(margin, 0, margin + vol_w, 0)
                    pat.add_color_stop_rgba(0.00, 0.24, 0.70, 0.34, 1.0)   # Vivid Emerald Green #3db356
                    pat.add_color_stop_rgba(0.65, 0.24, 0.70, 0.34, 1.0)  # Green up to 65%
                    pat.add_color_stop_rgba(0.85, 0.95, 0.75, 0.20, 1.0)  # Warm Yellow at 85%
                    pat.add_color_stop_rgba(1.00, 0.95, 0.30, 0.25, 1.0)  # Studio Red at 100%
                    self._cached_gradient = pat
                    self._cached_gradient_vol_w = vol_w
                else:
                    pat = self._cached_gradient
                cr.set_source(pat)
                if meter_l > 0.5:
                    cr.rectangle(margin, y_top, meter_l, track_h)
                if meter_r > 0.5:
                    cr.rectangle(margin, y_bot, meter_r, track_h)
                cr.fill()

        # Draw Draggable Blue Knob
        if self.is_muted:
            cr.set_source_rgba(0.35, 0.35, 0.40, 0.8)
        else:
            # Drop shadow
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.4)
            cr.arc(thumb_x + 0.5, thumb_y + 0.8, thumb_r, 0, 6.28318)
            cr.fill()
            
            # Knob body
            cr.set_source_rgba(0.21, 0.52, 0.89, 1.0)

        cr.arc(thumb_x, thumb_y, thumb_r, 0, 6.28318)
        cr.fill()

        # White border highlight
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.7 if not self.is_muted else 0.3)
        cr.set_line_width(1.0)
        cr.arc(thumb_x, thumb_y, thumb_r, 0, 6.28318)
        cr.stroke()
