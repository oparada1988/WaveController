import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk
import math

TAU = math.tau
# Standard audio-knob sweep: 270 degrees, starting at bottom-left (135deg) clockwise to bottom-right (45deg)
START_ANGLE = math.pi * 0.75
SWEEP_ANGLE = math.pi * 1.5


class RotaryDial(Gtk.DrawingArea):
    """
    Compact rotary intensity dial (0-100). Click-and-drag vertically to adjust,
    mouse wheel to nudge, double-click to reset to the default value.
    """
    def __init__(self, value: int = 50, default_value: int = 50, on_value_changed=None):
        super().__init__()
        self.value = max(0, min(100, value))
        self.default_value = max(0, min(100, default_value))
        self.on_value_changed = on_value_changed
        self.is_dragging = False
        self._drag_start_y = 0.0
        self._drag_start_value = self.value

        self.set_content_width(28)
        self.set_content_height(28)
        self.set_valign(Gtk.Align.CENTER)
        self.set_cursor_from_name("ns-resize")
        self.set_tooltip_text(f"Effect Intensity: {self.value}%")
        self.set_draw_func(self._draw)

        drag_gesture = Gtk.GestureDrag()
        drag_gesture.connect("drag-begin", self._on_drag_begin)
        drag_gesture.connect("drag-update", self._on_drag_update)
        drag_gesture.connect("drag-end", self._on_drag_end)
        self.add_controller(drag_gesture)

        scroll_ctrl = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll_ctrl.connect("scroll", self._on_scroll)
        self.add_controller(scroll_ctrl)

        click_gesture = Gtk.GestureClick.new()
        click_gesture.connect("released", self._on_click_released)
        self.add_controller(click_gesture)

    def set_value(self, value: int):
        new_val = max(0, min(100, int(value)))
        if self.is_dragging:
            return
        if self.value != new_val:
            self.value = new_val
            self.set_tooltip_text(f"Effect Intensity: {self.value}%")
            self.queue_draw()

    def get_value(self) -> int:
        return self.value

    def _emit_changed(self):
        self.set_tooltip_text(f"Effect Intensity: {self.value}%")
        if self.on_value_changed:
            self.on_value_changed(self.value)

    def _on_drag_begin(self, gesture, start_x, start_y):
        self.is_dragging = True
        self._drag_start_y = start_y
        self._drag_start_value = self.value

    def _on_drag_update(self, gesture, offset_x, offset_y):
        # 150px of vertical drag = full 0-100 sweep; dragging up increases value
        delta = int(round((-offset_y / 150.0) * 100))
        new_val = max(0, min(100, self._drag_start_value + delta))
        if new_val != self.value:
            self.value = new_val
            self.queue_draw()
            self._emit_changed()

    def _on_drag_end(self, gesture, offset_x, offset_y):
        self.is_dragging = False

    def _on_scroll(self, controller, dx, dy):
        state = controller.get_current_event_state()
        step = 5 if (state & Gdk.ModifierType.SHIFT_MASK) else 2
        delta = -step if dy > 0 else (step if dy < 0 else 0)
        if delta != 0:
            new_val = max(0, min(100, self.value + delta))
            if new_val != self.value:
                self.value = new_val
                self.queue_draw()
                self._emit_changed()
        return True

    def _on_click_released(self, gesture, n_press, x, y):
        if n_press == 2 and self.value != self.default_value:
            self.value = self.default_value
            self.queue_draw()
            self._emit_changed()

    def _draw(self, area, cr, width, height):
        cx = float(width) / 2.0
        cy = float(height) / 2.0
        radius = min(cx, cy) - 2.0
        pct = float(self.value) / 100.0
        value_angle = START_ANGLE + (SWEEP_ANGLE * pct)
        dim = 1.0 if self.get_sensitive() else 0.35

        # Recessed track (full sweep)
        cr.set_source_rgba(0.16, 0.16, 0.19, dim)
        cr.set_line_width(3.0)
        cr.arc(cx, cy, radius, START_ANGLE, START_ANGLE + SWEEP_ANGLE)
        cr.stroke()

        # Default-value marker (e.g. 50/50 neutral position)
        default_angle = START_ANGLE + (SWEEP_ANGLE * (float(self.default_value) / 100.0))
        marker_inner = radius - 5.0
        marker_outer = radius + 2.0
        cr.set_source_rgba(0.75, 0.75, 0.78, 0.9 * dim)
        cr.set_line_width(1.5)
        cr.move_to(cx + math.cos(default_angle) * marker_inner, cy + math.sin(default_angle) * marker_inner)
        cr.line_to(cx + math.cos(default_angle) * marker_outer, cy + math.sin(default_angle) * marker_outer)
        cr.stroke()

        # Active value arc (desaturated to gray when disabled)
        if self.get_sensitive():
            cr.set_source_rgba(0.57, 0.27, 1.0, 1.0)  # #9146ff accent
        else:
            cr.set_source_rgba(0.45, 0.45, 0.48, 1.0)
        cr.set_line_width(3.0)
        cr.arc(cx, cy, radius, START_ANGLE, value_angle)
        cr.stroke()

        # Knob body
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.35 * dim)
        cr.arc(cx + 0.4, cy + 0.6, radius - 4.0, 0, TAU)
        cr.fill()
        cr.set_source_rgba(0.20, 0.20, 0.24, dim)
        cr.arc(cx, cy, radius - 4.0, 0, TAU)
        cr.fill()

        # Indicator pointer
        ind_x = cx + math.cos(value_angle) * (radius - 6.0)
        ind_y = cy + math.sin(value_angle) * (radius - 6.0)
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.9 * dim)
        cr.set_line_width(2.0)
        cr.move_to(cx, cy)
        cr.line_to(ind_x, ind_y)
        cr.stroke()
