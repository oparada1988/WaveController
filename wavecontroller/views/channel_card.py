import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from .stereo_slider import StereoSlider

class ChannelCard(Gtk.Box):
    """
    Channel identifier card displayed on the left column of the matrix.
    Contains the channel icon, title, settings popover (running app routing, mono/stereo sync toggle, delete),
    mute button, dual-track stereo volume slider with real-time VU meters, and link toggle.
    """
    def __init__(self, channel_info: dict, pipewire_mgr, hardware_mgr=None, on_link_toggle_callback=None, on_sync_meter_callback=None, on_channel_removed_callback=None, on_channel_renamed_callback=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.channel_info = channel_info
        self.pipewire_mgr = pipewire_mgr
        self.hardware_mgr = hardware_mgr
        self.on_link_toggle_callback = on_link_toggle_callback
        self.on_sync_meter_callback = on_sync_meter_callback
        self.on_channel_removed_callback = on_channel_removed_callback
        self.on_channel_renamed_callback = on_channel_renamed_callback
        
        self.add_css_class("channel-row-card")
        self.set_valign(Gtk.Align.CENTER)
        self.set_hexpand(False)
        self.set_size_request(340, -1)

        # Channel icon (Auto-resolve from assigned apps or channel name)
        assigned = self.pipewire_mgr.get_assigned_apps(channel_info["id"])
        primary_app = assigned[0] if assigned else channel_info.get("name", "")
        icon_name = channel_info.get("icon") or self.pipewire_mgr.resolve_icon_for_app(primary_app)
        self.icon_img = Gtk.Image.new_from_icon_name(icon_name)
        self.icon_img.set_pixel_size(20)
        self.append(self.icon_img)

        # Channel Title + Subtitle Box
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title_box.set_hexpand(False)
        title_box.set_size_request(130, -1)

        display_name = channel_info.get("name", "Channel")
        if channel_info["id"] == "mic" and self.hardware_mgr:
            display_name = self.hardware_mgr.get_device_display_name(self.hardware_mgr.device_name)

        self.title_lbl = Gtk.Label(label=display_name)
        self.title_lbl.add_css_class("channel-title")
        self.title_lbl.set_halign(Gtk.Align.START)
        self.title_lbl.set_ellipsize(3)
        title_box.append(self.title_lbl)

        # Assigned apps subtitle
        sub_text = ", ".join(assigned[:2]) if assigned else ("System capture" if channel_info["id"] == "mic" else "No apps assigned")
        self.sub_lbl = Gtk.Label(label=sub_text)
        self.sub_lbl.add_css_class("mix-header-subtitle")
        self.sub_lbl.set_halign(Gtk.Align.START)
        self.sub_lbl.set_ellipsize(3)
        title_box.append(self.sub_lbl)

        self.append(title_box)

        # Channel settings gear popover button
        self.settings_btn = Gtk.MenuButton()
        self.settings_btn.set_icon_name("emblem-system-symbolic")
        self.settings_btn.add_css_class("flat")
        self.settings_btn.add_css_class("wave-icon-btn")
        self.settings_btn.set_tooltip_text(f"Configure '{display_name}'")
        self._setup_channel_popover()
        self.append(self.settings_btn)

        # Mute button
        self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.add_css_class("wave-icon-btn")
        self.mute_btn.set_valign(Gtk.Align.CENTER)
        self.mute_btn.connect("clicked", self._on_mute_clicked)
        self.append(self.mute_btn)

        # Stereo Split Volume Slider & VU Meter (Master Channel Gain)
        vol = self.pipewire_mgr.get_channel_master_volume(self.channel_info["id"])
        muted = self.pipewire_mgr.get_channel_master_mute(self.channel_info["id"])
        is_synced = self.pipewire_mgr.get_channel_sync_meter(self.channel_info["id"])
        self.slider = StereoSlider(
            volume=vol,
            is_muted=muted,
            sync_peaks=is_synced,
            on_volume_changed=self._on_slider_volume_changed
        )
        self.slider.set_hexpand(False)
        self.slider.set_size_request(85, 20)
        self.append(self.slider)

        # Link/Unlink multi-mix toggle button
        self.link_btn = Gtk.Button.new_from_icon_name("insert-link-symbolic")
        self.link_btn.add_css_class("flat")
        self.link_btn.add_css_class("wave-icon-btn")
        self.link_btn.set_tooltip_text("Link volume across mixes")
        self.link_btn.connect("clicked", self._on_link_clicked)
        self.append(self.link_btn)

        self.update_ui_state()

    def _setup_channel_popover(self):
        popover = Gtk.Popover()
        popover.add_css_class("wave-popover")
        
        pop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        pop_box.set_margin_top(12)
        pop_box.set_margin_bottom(12)
        pop_box.set_margin_start(12)
        pop_box.set_margin_end(12)
        pop_box.set_size_request(260, -1)

        # 1. Header
        head_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head_lbl = Gtk.Label(label="Channel Settings")
        head_lbl.add_css_class("mix-header-title")
        head_lbl.set_halign(Gtk.Align.START)
        head_box.append(head_lbl)

        head_box.append(Gtk.Box(hexpand=True))
        ch_type = self.channel_info.get("type", "sink")
        type_badge = Gtk.Label(label="App" if ch_type != "source" else "Input")
        type_badge.add_css_class("mix-header-subtitle")
        head_box.append(type_badge)
        pop_box.append(head_box)

        # 2. Rename Row
        rename_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        name_entry = Gtk.Entry()
        name_entry.set_text(self.channel_info.get("name", "Channel"))
        name_entry.set_placeholder_text("Channel name...")
        name_entry.set_hexpand(True)
        rename_box.append(name_entry)

        save_btn = Gtk.Button.new_from_icon_name("object-select-symbolic")
        save_btn.add_css_class("flat")
        save_btn.add_css_class("wave-icon-btn")
        save_btn.set_tooltip_text("Rename Channel")

        def on_rename(*args):
            new_name = name_entry.get_text().strip()
            if new_name and new_name != self.channel_info.get("name"):
                self.channel_info["name"] = new_name
                self.pipewire_mgr.rename_channel(self.channel_info["id"], new_name)
                self.title_lbl.set_text(new_name)
                if self.on_channel_renamed_callback:
                    self.on_channel_renamed_callback(self.channel_info["id"], new_name)

        save_btn.connect("clicked", on_rename)
        name_entry.connect("activate", on_rename)
        rename_box.append(save_btn)
        pop_box.append(rename_box)

        pop_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # 3. Application Routing (for playback channels)
        if ch_type != "source":
            apps_title = Gtk.Label(label="Active Running Applications:")
            apps_title.add_css_class("mix-header-subtitle")
            apps_title.set_halign(Gtk.Align.START)
            pop_box.append(apps_title)

            app_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            pop_box.append(app_list_box)

            def refresh_apps_list(p=None):
                while app_list_box.get_first_child():
                    app_list_box.remove(app_list_box.get_first_child())
                active_streams = self.pipewire_mgr.get_active_application_streams()
                assigned = set(self.pipewire_mgr.get_assigned_apps(self.channel_info["id"]))
                if active_streams:
                    for stream in active_streams:
                        app_name = stream["name"]
                        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                        icon_name = stream.get("icon") or self.pipewire_mgr.resolve_icon_for_app(app_name)
                        img = Gtk.Image.new_from_icon_name(icon_name)
                        img.set_pixel_size(16)
                        chk = Gtk.CheckButton(label=app_name)
                        chk.set_active(app_name in assigned or app_name.lower() in [a.lower() for a in assigned])
                        chk.connect("toggled", self._on_app_toggled, app_name)
                        chk.set_hexpand(True)

                        row.append(img)
                        row.append(chk)
                        app_list_box.append(row)
                else:
                    no_apps_lbl = Gtk.Label(label="No active audio apps detected.\nStart an app (e.g. Spotify, Games) to route it.")
                    no_apps_lbl.add_css_class("mix-header-subtitle")
                    no_apps_lbl.set_halign(Gtk.Align.START)
                    app_list_box.append(no_apps_lbl)

            refresh_apps_list()
            popover.connect("show", refresh_apps_list)
            pop_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # 4. VU Meter Physics: Sync L/R Channels (Mono Mode)
        meter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        meter_lbl_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meter_lbl_box.set_hexpand(True)

        m_title = Gtk.Label(label="Sync L/R Meter (Mono)")
        m_title.add_css_class("channel-title")
        m_title.set_halign(Gtk.Align.START)
        meter_lbl_box.append(m_title)

        m_sub = Gtk.Label(label="Lock Left & Right bars to max peak")
        m_sub.add_css_class("mix-header-subtitle")
        m_sub.set_halign(Gtk.Align.START)
        meter_lbl_box.append(m_sub)
        meter_box.append(meter_lbl_box)

        sync_switch = Gtk.Switch()
        is_synced = self.pipewire_mgr.get_channel_sync_meter(self.channel_info["id"])
        sync_switch.set_active(is_synced)
        sync_switch.set_valign(Gtk.Align.CENTER)

        def on_sync_toggled(sw, *args):
            active = sw.get_active()
            self.channel_info["sync_meter"] = active
            self.pipewire_mgr.set_channel_sync_meter(self.channel_info["id"], active)
            self.slider.set_sync_peaks(active)
            if self.on_sync_meter_callback:
                self.on_sync_meter_callback(self.channel_info["id"], active)

        sync_switch.connect("notify::active", on_sync_toggled)
        meter_box.append(sync_switch)
        pop_box.append(meter_box)

        # 5. Delete Channel Button (Available for all channels)
        pop_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        del_btn = Gtk.Button(label="Delete Channel")
        del_btn.add_css_class("destructive-action")
        
        def on_delete(b):
            popover.popdown()
            dialog = Adw.MessageDialog(
                transient_for=self.get_root() if isinstance(self.get_root(), Gtk.Window) else None,
                heading=f"Delete '{self.title_lbl.get_text()}'?",
                body="This channel strip will be removed from your mixer matrix and its audio streams unrouted."
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("delete", "Delete")
            dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")

            def on_dialog_response(d, resp):
                if resp == "delete":
                    self.pipewire_mgr.remove_channel(self.channel_info["id"])
                    if self.on_channel_removed_callback:
                        self.on_channel_removed_callback(self.channel_info["id"])

            dialog.connect("response", on_dialog_response)
            dialog.present()

        del_btn.connect("clicked", on_delete)
        pop_box.append(del_btn)

        popover.set_child(pop_box)
        self.settings_btn.set_popover(popover)

    def _on_app_toggled(self, chk, app_name):
        ch_id = self.channel_info["id"]
        if chk.get_active():
            self.pipewire_mgr.assign_app_to_channel(ch_id, app_name)
        else:
            assigned = self.pipewire_mgr.get_assigned_apps(ch_id)
            if app_name in assigned:
                assigned.remove(app_name)
                self.pipewire_mgr.assigned_apps[ch_id] = assigned
        
        assigned_list = self.pipewire_mgr.get_assigned_apps(ch_id)
        self.sub_lbl.set_text(", ".join(assigned_list[:2]) if assigned_list else "No apps assigned")
        if assigned_list:
            new_icon = self.pipewire_mgr.resolve_icon_for_app(assigned_list[0])
            self.icon_img.set_from_icon_name(new_icon)

    def _on_slider_volume_changed(self, vol: int):
        ch_id = self.channel_info["id"]
        self.pipewire_mgr.set_channel_master_volume(ch_id, vol)
        if self.pipewire_mgr.is_channel_linked(ch_id) and self.on_link_toggle_callback:
            self.on_link_toggle_callback(ch_id, True)

    def update_peaks(self, peak_l: float, peak_r: float):
        self.slider.set_peaks(peak_l, peak_r)

    def set_sync_peaks(self, sync: bool):
        self.slider.set_sync_peaks(sync)

    def _on_mute_clicked(self, btn):
        ch_id = self.channel_info["id"]
        self.pipewire_mgr.toggle_channel_master_mute(ch_id)
        self.update_ui_state()
        if self.pipewire_mgr.is_channel_linked(ch_id) and self.on_link_toggle_callback:
            self.on_link_toggle_callback(ch_id, True)

    def _on_link_clicked(self, btn):
        ch_id = self.channel_info["id"]
        # Toggle link state across sub-mixes for this channel
        curr_linked = any(s.get("linked", True) for s in self.pipewire_mgr.channel_states.get(ch_id, {}).values())
        new_val = not curr_linked
        master_vol = self.pipewire_mgr.get_channel_master_volume(ch_id)
        master_muted = self.pipewire_mgr.get_channel_master_mute(ch_id)
        for m_id, s in self.pipewire_mgr.channel_states.get(ch_id, {}).items():
            s["linked"] = new_val
            if new_val:
                s["volume"] = master_vol
                s["muted"] = master_muted
        self.pipewire_mgr._save_state_to_config(immediate=True)
        self.update_ui_state()
        if self.on_link_toggle_callback:
            self.on_link_toggle_callback(ch_id, new_val)

    def update_ui_state(self):
        ch_id = self.channel_info["id"]
        vol = self.pipewire_mgr.get_channel_master_volume(ch_id)
        muted = self.pipewire_mgr.get_channel_master_mute(ch_id)
        linked = any(s.get("linked", True) for s in self.pipewire_mgr.channel_states.get(ch_id, {}).values())

        self.slider.set_volume(vol, muted)

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

    def refresh_name(self):
        display_name = self.channel_info.get("name", "Channel")
        if self.channel_info["id"] == "mic" and self.hardware_mgr:
            display_name = self.hardware_mgr.get_device_display_name(self.hardware_mgr.device_name)
        self.title_lbl.set_text(display_name)

    def update_peaks(self, peak_l: float, peak_r: float):
        if hasattr(self, "slider") and self.slider:
            self.slider.set_peaks(peak_l, peak_r)

