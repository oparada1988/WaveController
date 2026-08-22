import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

class ChannelCard(Gtk.Box):
    """
    Channel identifier card displayed on the left column of the matrix,
    including app routing popovers and multi-mix link controls.
    """
    def __init__(self, channel_info: dict, pipewire_mgr, hardware_mgr=None, on_link_toggle_callback=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.channel_info = channel_info
        self.pipewire_mgr = pipewire_mgr
        self.hardware_mgr = hardware_mgr
        self.on_link_toggle_callback = on_link_toggle_callback
        
        self.add_css_class("channel-row-card")
        self.set_valign(Gtk.Align.CENTER)
        self.set_size_request(200, -1)

        # Channel icon (Auto-resolve from assigned apps or channel name)
        assigned = self.pipewire_mgr.get_assigned_apps(channel_info["id"])
        primary_app = assigned[0] if assigned else channel_info.get("name", "")
        icon_name = channel_info.get("icon") or self.pipewire_mgr.resolve_icon_for_app(primary_app)
        self.icon_img = Gtk.Image.new_from_icon_name(icon_name)
        self.icon_img.set_pixel_size(20)
        self.append(self.icon_img)

        # Channel Title + Subtitle (Assigned Apps) Box
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title_box.set_hexpand(True)

        display_name = channel_info.get("name", "Channel")
        if channel_info["id"] == "mic" and self.hardware_mgr:
            display_name = self.hardware_mgr.device_name

        self.title_lbl = Gtk.Label(label=display_name)
        self.title_lbl.add_css_class("channel-title")
        self.title_lbl.set_halign(Gtk.Align.START)
        title_box.append(self.title_lbl)

        # Assigned apps subtitle
        assigned = self.pipewire_mgr.get_assigned_apps(channel_info["id"])
        sub_text = ", ".join(assigned[:2]) if assigned else ("System capture" if channel_info["id"] == "mic" else "No apps assigned")
        self.sub_lbl = Gtk.Label(label=sub_text)
        self.sub_lbl.add_css_class("mix-header-subtitle")
        self.sub_lbl.set_halign(Gtk.Align.START)
        title_box.append(self.sub_lbl)

        self.append(title_box)

        # App Routing / Assign Button (for playback sink channels)
        if channel_info.get("type") != "source":
            self.route_btn = Gtk.MenuButton()
            self.route_btn.set_icon_name("applications-system-symbolic")
            self.route_btn.add_css_class("flat")
            self.route_btn.add_css_class("wave-icon-btn")
            self.route_btn.set_tooltip_text("Assign Applications")
            self._setup_app_popover()
            self.append(self.route_btn)

        # Master mute button
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

    def _setup_app_popover(self):
        popover = Gtk.Popover()
        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pop_box.set_margin_top(8)
        pop_box.set_margin_bottom(8)
        pop_box.set_margin_start(8)
        pop_box.set_margin_end(8)

        head_lbl = Gtk.Label(label="Route Applications to Channel")
        head_lbl.add_css_class("mix-header-title")
        pop_box.append(head_lbl)

        # List active streams and known apps
        active_streams = self.pipewire_mgr.get_active_application_streams()
        assigned = set(self.pipewire_mgr.get_assigned_apps(self.channel_info["id"]))

        # Known common apps for quick toggle
        all_apps = ["Spotify", "Discord", "Steam", "Chromium", "Firefox", "VLC", "Teams", "OBS Studio"]
        for stream in active_streams:
            if stream["name"] not in all_apps:
                all_apps.insert(0, stream["name"])

        for app_name in all_apps:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            chk = Gtk.CheckButton(label=app_name)
            chk.set_active(app_name in assigned)
            chk.connect("toggled", self._on_app_toggled, app_name)
            row.append(chk)
            pop_box.append(row)

        popover.set_child(pop_box)
        self.route_btn.set_popover(popover)

    def _on_app_toggled(self, chk, app_name):
        ch_id = self.channel_info["id"]
        if chk.get_active():
            self.pipewire_mgr.assign_app_to_channel(ch_id, app_name)
        else:
            assigned = self.pipewire_mgr.get_assigned_apps(ch_id)
            if app_name in assigned:
                assigned.remove(app_name)
                self.pipewire_mgr.assigned_apps[ch_id] = assigned
        
        # Update subtitle and icon
        assigned_list = self.pipewire_mgr.get_assigned_apps(ch_id)
        self.sub_lbl.set_text(", ".join(assigned_list[:2]) if assigned_list else "No apps assigned")
        if assigned_list:
            new_icon = self.pipewire_mgr.resolve_icon_for_app(assigned_list[0])
            self.icon_img.set_from_icon_name(new_icon)

    def _on_mute_clicked(self, btn):
        ch_id = self.channel_info["id"]
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
