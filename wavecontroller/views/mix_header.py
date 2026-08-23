import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

class MixHeaderCard(Gtk.Box):
    """
    Column header card representing an output mix bus (e.g. Personal Mix / Record Mix).
    Supports customizing mix name, subtitle, and accent color, as well as deletion.
    """
    def __init__(self, mix_info: dict, pipewire_mgr=None, on_remove_callback=None, on_edit_callback=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.mix_info = mix_info
        self.pipewire_mgr = pipewire_mgr
        self.on_remove_callback = on_remove_callback
        self.on_edit_callback = on_edit_callback
        
        self.add_css_class("mix-header-card")
        self.set_hexpand(True)

        top_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

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
        self.title_lbl.set_ellipsize(3)
        title_box.append(self.title_lbl)

        self.subtitle_lbl = Gtk.Label(label=mix_info.get("subtitle", "1 output"))
        self.subtitle_lbl.add_css_class("mix-header-subtitle")
        self.subtitle_lbl.set_halign(Gtk.Align.START)
        self.subtitle_lbl.set_ellipsize(3)
        title_box.append(self.subtitle_lbl)

        top_box.append(title_box)

        # Edit Mix Settings Button
        if self.pipewire_mgr:
            self.edit_btn = Gtk.MenuButton()
            self.edit_btn.set_icon_name("emblem-system-symbolic")
            self.edit_btn.add_css_class("flat")
            self.edit_btn.add_css_class("wave-icon-btn")
            self.edit_btn.set_tooltip_text(f"Edit '{mix_info.get('name')}'")
            self._setup_edit_popover(self.edit_btn)
            top_box.append(self.edit_btn)

        # Delete Mix Button
        if self.on_remove_callback:
            del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            del_btn.add_css_class("flat")
            del_btn.add_css_class("wave-icon-btn")
            del_btn.set_tooltip_text(f"Delete '{mix_info.get('name')}'")
            del_btn.connect("clicked", lambda b: self.on_remove_callback(mix_info["id"]))
            top_box.append(del_btn)

        self.append(top_box)

        # Active underline accent
        self.indicator = Gtk.Box()
        self.indicator.add_css_class("mix-header-indicator-active")
        self._apply_indicator_color(mix_info.get("color", "#9146ff"))
        self.append(self.indicator)

    def _apply_indicator_color(self, color: str):
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(f".indicator-{self.mix_info['id']} {{ background-color: {color}; }}".encode('utf-8'))
        self.indicator.add_css_class(f"indicator-{self.mix_info['id']}")
        Gtk.StyleContext.add_provider_for_display(Gtk.Widget.get_display(self), css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _setup_edit_popover(self, menu_btn: Gtk.MenuButton):
        popover = Gtk.Popover()
        popover.add_css_class("wave-popover")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_size_request(250, -1)

        head_lbl = Gtk.Label(label="Edit Mix Settings")
        head_lbl.add_css_class("mix-header-title")
        head_lbl.set_halign(Gtk.Align.START)
        box.append(head_lbl)

        # Mix Type (Fixed on creation)
        m_type = self.mix_info.get("type", "source" if self.mix_info.get("id") != "personal" else "sink")
        type_str = "🎙️ Source (Microphone / Input)" if m_type == "source" else "🎧 Sink (Speaker / Output)"
        
        type_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        type_lbl = Gtk.Label(label=f"Type: {type_str}")
        type_lbl.add_css_class("mix-header-subtitle")
        type_box.append(type_lbl)
        box.append(type_box)

        # Mix Name
        name_entry = Gtk.Entry(text=self.mix_info.get("name", ""))
        name_entry.set_placeholder_text("Mix Name")
        box.append(name_entry)

        # Subtitle
        sub_entry = Gtk.Entry(text=self.mix_info.get("subtitle", ""))
        sub_entry.set_placeholder_text("Subtitle (e.g. Broadcast / Headphones)")
        box.append(sub_entry)

        # Accent Color
        color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        color_lbl = Gtk.Label(label="Color:")
        color_lbl.add_css_class("mix-header-subtitle")
        color_lbl.set_size_request(45, -1)
        color_lbl.set_halign(Gtk.Align.START)
        color_row.append(color_lbl)

        colors_map = [
            ("#9146ff", "Purple"),
            ("#3584e4", "Blue"),
            ("#3db356", "Green"),
            ("#ff7800", "Orange"),
            ("#e05252", "Red")
        ]
        color_combo = Gtk.DropDown.new_from_strings([c[1] for c in colors_map])
        
        curr_color = self.mix_info.get("color", "#9146ff")
        sel_idx = 0
        for i, (hex_code, _) in enumerate(colors_map):
            if hex_code.lower() == curr_color.lower():
                sel_idx = i
                break
        color_combo.set_selected(sel_idx)
        color_combo.set_hexpand(True)
        color_row.append(color_combo)
        box.append(color_row)

        save_btn = Gtk.Button(label="Save Changes")
        save_btn.add_css_class("suggested-action")
        
        def on_save(b):
            new_name = name_entry.get_text().strip()
            new_sub = sub_entry.get_text().strip() or "Custom Mix"
            c_idx = color_combo.get_selected()
            new_color = colors_map[c_idx][0] if c_idx < len(colors_map) else "#9146ff"

            if new_name:
                self.pipewire_mgr.update_mix(self.mix_info["id"], name=new_name, subtitle=new_sub, color=new_color)
                self.title_lbl.set_text(new_name)
                self.subtitle_lbl.set_text(new_sub)
                self._apply_indicator_color(new_color)
                popover.popdown()
                if self.on_edit_callback:
                    self.on_edit_callback(self.mix_info["id"])

        save_btn.connect("clicked", on_save)
        box.append(save_btn)

        popover.set_child(box)
        menu_btn.set_popover(popover)
