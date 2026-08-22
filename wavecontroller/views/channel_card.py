import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

class ChannelCard(Gtk.Box):
    """
    Channel identifier card displayed on the left column of the matrix.
    """
    def __init__(self, channel_info: dict, pipewire_mgr, on_link_toggle_callback=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.channel_info = channel_info
        self.pipewire_mgr = pipewire_mgr
        self.on_link_toggle_callback = on_link_toggle_callback
        
        self.add_css_class("channel-row-card")
        self.set_valign(Gtk.Align.CENTER)
        self.set_size_request(180, -1)

        # Channel icon
        icon_name = channel_info.get("icon", "audio-x-generic-symbolic")
        self.icon_img = Gtk.Image.new_from_icon_name(icon_name)
        self.icon_img.set_pixel_size(20)
        self.append(self.icon_img)

        # Channel title
        self.title_lbl = Gtk.Label(label=channel_info.get("name", "Channel"))
        self.title_lbl.add_css_class("channel-title")
        self.title_lbl.set_halign(Gtk.Align.START)
        self.title_lbl.set_hexpand(True)
        self.append(self.title_lbl)

        # Master mute button for input
        self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.add_css_class("wave-icon-btn")
        self.mute_btn.connect("clicked", self._on_mute_clicked)
        self.append(self.mute_btn)

        # Link/Unlink multi-mix toggle button
        self.link_btn = Gtk.Button.new_from_icon_name("insert-link-symbolic")
        self.link_btn.add_css_class("flat")
        self.link_btn.add_css_class("wave-icon-btn")
        self.link_btn.set_tooltip_text("Link volume across mixes")
        self.link_btn.connect("clicked", self._on_link_clicked)
        self.append(self.link_btn)

        self.update_ui_state()

    def _on_mute_clicked(self, btn):
        ch_id = self.channel_info["id"]
        # Toggle mute across all mixes
        is_muted = self.pipewire_mgr.toggle_channel_mute(ch_id, "personal")
        for mx in self.pipewire_mgr.mixes:
            self.pipewire_mgr.set_channel_mute(ch_id, mx["id"], is_muted)
        self.update_ui_state()

    def _on_link_clicked(self, btn):
        ch_id = self.channel_info["id"]
        is_linked = self.pipewire_mgr.toggle_channel_link(ch_id, "personal")
        self.update_ui_state()
        if self.on_link_toggle_callback:
            self.on_link_toggle_callback(ch_id, is_linked)

    def update_ui_state(self):
        ch_id = self.channel_info["id"]
        state = self.pipewire_mgr.get_channel_state(ch_id, "personal")
        muted = state.get("muted", False)
        linked = state.get("linked", True)

        if muted:
            self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.mute_btn.add_css_class("muted")
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")

        if linked:
            self.link_btn.set_icon_name("insert-link-symbolic")
            self.link_btn.add_css_class("active")
        else:
            self.link_btn.set_icon_name("mail-attachment-symbolic")
            self.link_btn.remove_css_class("active")
