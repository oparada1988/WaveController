import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, Graphene
import cairo

class StereoSlider(Gtk.DrawingArea):
    """
    Dual-channel stereo volume fader and live audio level meter.
    Top bar = Left channel, Bottom bar = Right channel.
    Features an interactive draggable blue knob and real-time bouncing green meters.
    """
    def __init__(self, volume: int = 80, is_muted: bool = False, on_volume_changed=None):
        super().__init__()
        self.volume = max(0, min(100, volume))
        self.is_muted = is_muted
        self.peak_l = 0.0
        self.peak_r = 0.0
        self.on_volume_changed = on_volume_changed
        self.is_dragging = False

        self.set_content_width(140)
        self.set_content_height(22)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.CENTER)
        self.set_cursor_from_name("pointer")
        self.set_draw_func(self._draw)

        # Gestures for Click and Drag
        click_gesture = Gtk.GestureClick()
        click_gesture.connect("pressed", self._on_pressed)
        self.add_controller(click_gesture)

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
        # Only queue draw if peaks changed significantly
        if abs(self.peak_l - peak_l) > 0.01 or abs(self.peak_r - peak_r) > 0.01:
            self.peak_l = max(0.0, min(1.0, peak_l))
            self.peak_r = max(0.0, min(1.0, peak_r))
            self.queue_draw()

    def _update_volume_from_x(self, x: float):
        width = self.get_width()
        margin = 8.0
        track_w = width - 2 * margin
        if track_w <= 0:
            return
        pct = max(0.0, min(1.0, (x - margin) / track_w))
        new_vol = int(pct * 100)
        if new_vol != self.volume:
            self.volume = new_vol
            self.queue_draw()
            if self.on_volume_changed:
                self.on_volume_changed(self.volume)

    def _on_pressed(self, gesture, n_press, x, y):
        self._update_volume_from_x(x)

    def _on_drag_begin(self, gesture, start_x, start_y):
        self.is_dragging = True
        self._update_volume_from_x(start_x)

    def _on_drag_update(self, gesture, offset_x, offset_y):
        start_x, _ = gesture.get_start_point()
        curr_x = start_x + offset_x
        self._update_volume_from_x(curr_x)

    def _on_drag_end(self, gesture, offset_x, offset_y):
        self.is_dragging = False

    def _draw(self, area, cr, width, height):
        margin = 8.0
        track_w = max(10.0, width - 2 * margin)
        track_h = 3.5
        gap = 3.0
        
        y_top = height / 2.0 - track_h - gap / 2.0
        y_bot = height / 2.0 + gap / 2.0

        # Thumb position
        thumb_x = margin + track_w * (self.volume / 100.0)
        thumb_y = height / 2.0
        thumb_r = 5.5

        # Dim factors when muted
        alpha = 0.35 if self.is_muted else 1.0

        # Helper to draw rounded track
        def draw_track(y_pos, peak_val):
            # 1. Dark Recessed Groove Track Background
            cr.set_source_rgba(0.08, 0.08, 0.10, alpha)
            cr.rectangle(margin, y_pos, track_w, track_h)
            cr.fill()

            # 2. Inactive Track up to Volume Slider
            cr.set_source_rgba(0.18, 0.18, 0.22, alpha)
            vol_w = track_w * (self.volume / 100.0)
            cr.rectangle(margin, y_pos, vol_w, track_h)
            cr.fill()

            # 3. Live Green VU Meter Level
            if not self.is_muted and peak_val > 0.005:
                # Level reaches up to volume slider
                meter_w = min(vol_w, track_w * peak_val)
                if meter_w > 0:
                    if peak_val > 0.85:
                        cr.set_source_rgba(0.95, 0.35, 0.25, 1.0) # Red
                    elif peak_val > 0.70:
                        cr.set_source_rgba(0.95, 0.75, 0.20, 1.0) # Yellow
                    else:
                        cr.set_source_rgba(0.24, 0.70, 0.34, 1.0) # Emerald Green #3db356
                    cr.rectangle(margin, y_pos, meter_w, track_h)
                    cr.fill()

        # Draw Left track (Top)
        draw_track(y_top, self.peak_l)

        # Draw Right track (Bottom)
        draw_track(y_bot, self.peak_r)

        # 4. Draw Draggable Blue Knob (Spanning across both tracks)
        if self.is_muted:
            cr.set_source_rgba(0.40, 0.40, 0.45, 0.8)
        else:
            # Soft subtle drop shadow
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.35)
            cr.arc(thumb_x + 0.5, thumb_y + 1.0, thumb_r, 0, 6.28318)
            cr.fill()
            
            cr.set_source_rgba(0.21, 0.52, 0.89, 1.0) # #3584e4 Blue

        cr.arc(thumb_x, thumb_y, thumb_r, 0, 6.28318)
        cr.fill()

        # Subtle white knob border highlight
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.7 if not self.is_muted else 0.3)
        cr.set_line_width(1.0)
        cr.arc(thumb_x, thumb_y, thumb_r, 0, 6.28318)
        cr.stroke()
