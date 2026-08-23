import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

class MixHeaderCard(Gtk.Box):
    """
    Column header card representing an output mix bus (e.g. Personal Mix / Record Mix).
    """
    def __init__(self, mix_info: dict, on_remove_callback=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.mix_info = mix_info
        self.on_remove_callback = on_remove_callback
        
        self.add_css_class("mix-header-card")
        self.set_hexpand(True)

        top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        # Mix Icon
        icon_name = mix_info.get("icon", "audio-headphones-symbolic")
        self.icon_img = Gtk.Image.new_from_icon_name(icon_name)
        self.icon_img.set_pixel_size(18)
        top_box.append(self.icon_img)

        # Titles Box
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title_box.set_hexpand(True)

        self.title_lbl = Gtk.Label(label=mix_info.get("name", "Mix"))
        self.title_lbl.add_css_class("mix-header-title")
        self.title_lbl.set_halign(Gtk.Align.START)
        title_box.append(self.title_lbl)

        self.subtitle_lbl = Gtk.Label(label=mix_info.get("subtitle", "1 output"))
        self.subtitle_lbl.add_css_class("mix-header-subtitle")
        self.subtitle_lbl.set_halign(Gtk.Align.START)
        title_box.append(self.subtitle_lbl)

        top_box.append(title_box)

        # Listen / Monitor indicator icon or Delete button
        if mix_info.get("id") == "personal":
            self.listen_icon = Gtk.Image.new_from_icon_name("audio-headset-symbolic")
            self.listen_icon.set_tooltip_text("Listening on Monitor Output")
            top_box.append(self.listen_icon)
        elif self.on_remove_callback:
            del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            del_btn.add_css_class("flat")
            del_btn.add_css_class("wave-icon-btn")
            del_btn.set_tooltip_text("Delete Mix")
            del_btn.connect("clicked", lambda b: self.on_remove_callback(mix_info["id"]))
            top_box.append(del_btn)

        self.append(top_box)

        # Active underline accent
        self.indicator = Gtk.Box()
        self.indicator.add_css_class("mix-header-indicator-active")
        if mix_info.get("color"):
            # Set background color dynamically via CSS
            css_provider = Gtk.CssProvider()
            css_provider.load_from_data(f".indicator-{mix_info['id']} {{ background-color: {mix_info['color']}; }}".encode('utf-8'))
            self.indicator.add_css_class(f"indicator-{mix_info['id']}")
            Gtk.StyleContext.add_provider_for_display(Gtk.Widget.get_display(self), css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        
        self.append(self.indicator)
