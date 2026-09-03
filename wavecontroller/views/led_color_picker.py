import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib
from ..utils.css_helpers import install_palette_css

LED_PALETTE = [
    ("#FFFFFF", "White (Default)"),
    ("#00E5FF", "Cyber Cyan"),
    ("#9146FF", "Stream Purple"),
    ("#2ECC71", "Emerald Green"),
    ("#FFB703", "Amber Gold"),
    ("#FF7800", "Sunset Orange"),
    ("#FF0000", "Crimson Red"),
    ("#F72585", "Neon Pink"),
]

def _ensure_led_palette_css():
    install_palette_css(LED_PALETTE, "dot-", 7, extra_css=" border: 1px solid rgba(255,255,255,0.2);")

class LEDColorButton(Gtk.MenuButton):
    """
    Compact 32px MenuButton with display-brightness-symbolic icon
    and live color dot, opening a popover with the 8-color palette + custom picker.
    """
    def __init__(self, hardware_mgr, mode_key: str, title: str = "Hardware LED Ring Color", parent_popover: Gtk.Popover = None):
        super().__init__()
        self.hardware_mgr = hardware_mgr
        self.mode_key = mode_key # "gain", "hp", "mix", "mute"
        self.title_text = title
        self.parent_popover = parent_popover
        self.popover = None
        self.custom_dot = None

        self.add_css_class("flat")
        self.add_css_class("wave-icon-btn")
        self.set_tooltip_text(f"{title} ({mode_key.capitalize()} Mode)")

        # Button content: Light icon + color indicator dot
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.icon_img = Gtk.Image.new_from_icon_name("display-brightness-symbolic")
        self.icon_img.set_pixel_size(16)
        btn_box.append(self.icon_img)

        self.color_dot = Gtk.Box()
        self.color_dot.set_size_request(8, 8)
        self.color_dot.set_valign(Gtk.Align.CENTER)
        self.color_dot.add_css_class("wave-color-dot")
        btn_box.append(self.color_dot)
        self.set_child(btn_box)

        self._setup_popover()
        self.update_color_preview()

        self._hw_listener_cb = None
        if self.hardware_mgr and hasattr(self.hardware_mgr, "add_hardware_listener"):
            self._hw_listener_cb = lambda curr, changed: GLib.idle_add(self._on_hw_sync, curr, changed)
            self.hardware_mgr.add_hardware_listener(self._hw_listener_cb)

    def cleanup(self):
        """Unregisters the hardware listener; call when this widget is torn down."""
        if self._hw_listener_cb and self.hardware_mgr and hasattr(self.hardware_mgr, "remove_hardware_listener"):
            self.hardware_mgr.remove_hardware_listener(self._hw_listener_cb)
            self._hw_listener_cb = None

    def _setup_popover(self):
        popover = Gtk.Popover()
        popover.set_autohide(True)
        popover.set_cascade_popdown(True)
        popover.add_css_class("wave-popover")
        self.popover = popover

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_size_request(200, -1)

        # Title
        t_lbl = Gtk.Label(label=self.title_text)
        t_lbl.add_css_class("mix-header-title")
        t_lbl.set_halign(Gtk.Align.START)
        box.append(t_lbl)

        sub_lbl = Gtk.Label(label=f"Active when dial is in {self.mode_key.capitalize()} mode")
        sub_lbl.add_css_class("mix-header-subtitle")
        sub_lbl.set_halign(Gtk.Align.START)
        box.append(sub_lbl)

        _ensure_led_palette_css()

        # Palette list
        for hex_code, name in LED_PALETTE:
            item_btn = Gtk.Button()
            item_btn.add_css_class("flat")
            item_btn.add_css_class("wave-sidebar-row")

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            dot = Gtk.Box()
            dot.set_size_request(14, 14)
            dot.set_valign(Gtk.Align.CENTER)
            dot.add_css_class(f"dot-{hex_code.replace('#', '')}")
            row_box.append(dot)

            lbl = Gtk.Label(label=name)
            lbl.set_halign(Gtk.Align.START)
            lbl.set_hexpand(True)
            row_box.append(lbl)

            item_btn.set_child(row_box)

            def make_click_handler(c_hex):
                def handler(b):
                    self.hardware_mgr.set_led_color(self.mode_key, c_hex)
                    self.update_color_preview()
                    popover.popdown()
                return handler

            item_btn.connect("clicked", make_click_handler(hex_code))
            box.append(item_btn)

        # Custom Color Dialog
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        custom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        c_lbl = Gtk.Label(label="Custom Color:")
        c_lbl.add_css_class("mix-header-subtitle")
        c_lbl.set_hexpand(True)
        c_lbl.set_halign(Gtk.Align.START)
        custom_row.append(c_lbl)

        color_dialog = Gtk.ColorDialog.new()
        color_dialog.set_with_alpha(False)
        color_dialog.set_title("Pick LED Color")
        self.color_dialog_btn = Gtk.ColorDialogButton.new(color_dialog)
        
        curr_hex = self.hardware_mgr.get_led_color(self.mode_key) if self.hardware_mgr else "#FFFFFF"
        rgba = Gdk.RGBA()
        rgba.parse(curr_hex)
        self.color_dialog_btn.set_rgba(rgba)
        self.color_dialog_btn.connect("notify::rgba", self._on_custom_color_selected)
        custom_row.append(self.color_dialog_btn)
        box.append(custom_row)

        popover.set_child(box)
        self.set_popover(popover)

    def _on_custom_color_selected(self, btn, *args):
        rgba = btn.get_rgba()
        r = int(rgba.red * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue * 255)
        hex_code = f"#{r:02X}{g:02X}{b:02X}"
        if self.hardware_mgr:
            self.hardware_mgr.set_led_color(self.mode_key, hex_code)
        self.update_color_preview()

    def update_color_preview(self):
        curr_hex = self.hardware_mgr.get_led_color(self.mode_key) if self.hardware_mgr else "#FFFFFF"
        if not hasattr(self, "_dot_provider") or not self._dot_provider:
            self._dot_provider = Gtk.CssProvider()
            display = Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(display, self._dot_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        clean_key = self.mode_key.replace(" ", "_")
        css_cls = f"wave-color-dot-{clean_key}"
        dot_css = f".{css_cls} {{ background-color: {curr_hex}; border-radius: 4px; border: 1px solid rgba(255,255,255,0.3); }}"
        if hasattr(self._dot_provider, "load_from_string"):
            self._dot_provider.load_from_string(dot_css)
        else:
            self._dot_provider.load_from_data(dot_css.encode())
        self.color_dot.add_css_class(css_cls)

        if hasattr(self, "color_dialog_btn") and self.color_dialog_btn:
            rgba = Gdk.RGBA()
            rgba.parse(curr_hex)
            self.color_dialog_btn.set_rgba(rgba)

    def _on_hw_sync(self, curr: dict, changed: dict):
        if "led_colors" in changed:
            self.update_color_preview()


def build_led_color_row(hardware_mgr, mode_key: str, label_text: str, led_title: str, parent_popover: Gtk.Popover = None):
    """Builds a labeled row pairing a subtitle Label with an LEDColorButton; returns (row, button)."""
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    lbl = Gtk.Label(label=label_text, hexpand=True, halign=Gtk.Align.START)
    lbl.add_css_class("mix-header-subtitle")
    btn = LEDColorButton(hardware_mgr, mode_key, title=led_title, parent_popover=parent_popover)
    row.append(lbl)
    row.append(btn)
    return row, btn
