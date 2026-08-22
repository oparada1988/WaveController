import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, Graphene
import cairo

class StereoSlider(Gtk.DrawingArea):
    """
    High-performance dual-channel stereo volume fader and live audio level meter.
    Features silky-smooth 60 FPS drag interaction and real-time Left/Right VU meter animations.
    """
    def __init__(self, volume: int = 80, is_muted: bool = False, on_volume_changed=None):
        super().__init__()
        self.volume = max(0, min(100, volume))
        self.is_muted = is_muted
        self.peak_l = 0.0
        self.peak_r = 0.0
        self.on_volume_changed = on_volume_changed
        self.is_dragging = False

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

    def set_volume(self, volume: int, is_muted: bool = False):
        new_vol = max(0, min(100, volume))
        if self.volume != new_vol or self.is_muted != is_muted:
            self.volume = new_vol
            self.is_muted = is_muted
            self.queue_draw()

    def set_peaks(self, peak_l: float, peak_r: float):
        if abs(self.peak_l - peak_l) > 0.008 or abs(self.peak_r - peak_r) > 0.008:
            self.peak_l = max(0.0, min(1.0, peak_l))
            self.peak_r = max(0.0, min(1.0, peak_r))
            self.queue_draw()

    def _set_pos_from_x(self, x: float):
        width = float(self.get_width())
        margin = 6.0
        track_w = width - (2.0 * margin)
        if track_w <= 0:
            return
        pct = max(0.0, min(1.0, (x - margin) / track_w))
        new_vol = int(pct * 100)
        if new_vol != self.volume:
            self.volume = new_vol
            self.queue_draw()
            if self.on_volume_changed:
                self.on_volume_changed(self.volume)

    def _on_drag_begin(self, gesture, start_x, start_y):
        self.is_dragging = True
        self._set_pos_from_x(start_x)

    def _on_drag_update(self, gesture, offset_x, offset_y):
        start_x, _ = gesture.get_start_point()
        self._set_pos_from_x(start_x + offset_x)

    def _on_drag_end(self, gesture, offset_x, offset_y):
        self.is_dragging = False

    def _draw(self, area, cr, width, height):
        margin = 6.0
        track_w = max(10.0, float(width) - (2.0 * margin))
        track_h = 3.0
        gap = 3.0
        
        y_top = (float(height) - (2.0 * track_h + gap)) / 2.0
        y_bot = y_top + track_h + gap

        thumb_x = margin + track_w * (float(self.volume) / 100.0)
        thumb_y = float(height) / 2.0
        thumb_r = 5.0

        alpha = 0.35 if self.is_muted else 1.0

        # Helper to draw a single channel track
        def draw_track(y_pos, peak_val):
            # Recessed background track
            cr.set_source_rgba(0.09, 0.09, 0.11, alpha)
            cr.rectangle(margin, y_pos, track_w, track_h)
            cr.fill()

            # Active volume track
            cr.set_source_rgba(0.18, 0.18, 0.22, alpha)
            vol_w = track_w * (float(self.volume) / 100.0)
            cr.rectangle(margin, y_pos, vol_w, track_h)
            cr.fill()

            # Live VU meter bar
            if not self.is_muted and peak_val > 0.005:
                meter_w = min(vol_w, track_w * peak_val)
                if meter_w > 0:
                    if peak_val > 0.85:
                        cr.set_source_rgba(0.95, 0.30, 0.25, 1.0) # Red
                    elif peak_val > 0.70:
                        cr.set_source_rgba(0.95, 0.75, 0.20, 1.0) # Yellow
                    else:
                        cr.set_source_rgba(0.24, 0.70, 0.34, 1.0) # Emerald Green #3db356
                    cr.rectangle(margin, y_pos, meter_w, track_h)
                    cr.fill()

        # Left (top) & Right (bottom)
        draw_track(y_top, self.peak_l)
        draw_track(y_bot, self.peak_r)

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
