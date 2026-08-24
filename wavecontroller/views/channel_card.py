import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GObject, Adw, GLib

from .stereo_slider import StereoSlider

class ChannelCard(Gtk.Box):
    """
    Channel identifier card displayed on the left column of the matrix.
    Contains the drag grip handle, hardware-accurate channel icon, title, settings popover,
    48V phantom power quick toggle badge, mute button, dual-track stereo volume slider
    with real-time VU meters, and link toggle.
    """
    def __init__(self, channel_info: dict, pipewire_mgr, hardware_mgr=None, on_link_toggle_callback=None, on_sync_meter_callback=None, on_channel_removed_callback=None, on_channel_renamed_callback=None, on_reorder_callback=None, on_hover_row_callback=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.channel_info = channel_info
        self.pipewire_mgr = pipewire_mgr
        self.hardware_mgr = hardware_mgr
        self.on_link_toggle_callback = on_link_toggle_callback
        self.on_sync_meter_callback = on_sync_meter_callback
        self.on_channel_removed_callback = on_channel_removed_callback
        self.on_channel_renamed_callback = on_channel_renamed_callback
        self.on_reorder_callback = on_reorder_callback
        self.on_hover_row_callback = on_hover_row_callback
        
        self.add_css_class("channel-row-card")
        self.set_valign(Gtk.Align.CENTER)
        self.set_hexpand(False)
        self.set_size_request(370, -1)

        # Identify if this channel corresponds to Elgato Wave hardware
        ch_id = str(channel_info.get("id", ""))
        ch_name = str(channel_info.get("name", "")).lower()
        self.is_wave_channel = (ch_id in ("mic", "elgato_wave_xlr") or "wave" in ch_id.lower() or "wave" in ch_name) and (
            getattr(self.hardware_mgr, "is_elgato", False) or 
            getattr(self.hardware_mgr, "device_type", "") == "elgato" or 
            "wave" in str(getattr(self.hardware_mgr, "device_name", "")).lower()
        )

        # 1. Dedicated Vertical 6-Dots Drag Grip Handle (list-drag-handle-symbolic)
        self.drag_grip = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        self.drag_grip.set_pixel_size(16)
        self.drag_grip.add_css_class("channel-drag-handle")
        self.drag_grip.set_cursor_from_name("grab")
        self.drag_grip.set_tooltip_text("Click and hold to reorder channel vertically")
        self.append(self.drag_grip)

        # 2. Channel icon (Auto-resolve from dedicated hardware or assigned apps)
        icon_name = self._resolve_icon()
        self.icon_img = Gtk.Image.new_from_icon_name(icon_name)
        self.icon_img.set_pixel_size(20)
        self.append(self.icon_img)

        # 3. Channel Title + Subtitle Box
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title_box.set_hexpand(False)
        title_box.set_size_request(104, -1)

        display_name = channel_info.get("name", "Channel")
        if self.is_wave_channel and self.hardware_mgr and self.hardware_mgr.device_name:
            display_name = self.hardware_mgr.get_device_display_name(self.hardware_mgr.device_name)

        self.title_lbl = Gtk.Label(label=display_name)
        self.title_lbl.add_css_class("channel-title")
        self.title_lbl.set_halign(Gtk.Align.START)
        self.title_lbl.set_ellipsize(3)
        title_box.append(self.title_lbl)

        # Assigned apps subtitle
        assigned = self.pipewire_mgr.get_assigned_apps(channel_info["id"])
        sub_text = ", ".join(assigned[:2]) if assigned else ("System capture" if (self.is_wave_channel or channel_info.get("type") == "source") else "No apps assigned")
        self.sub_lbl = Gtk.Label(label=sub_text)
        self.sub_lbl.add_css_class("mix-header-subtitle")
        self.sub_lbl.set_halign(Gtk.Align.START)
        self.sub_lbl.set_ellipsize(3)
        title_box.append(self.sub_lbl)

        self.append(title_box)

        # 4. 48V Phantom Power Quick Toggle (Wave XLR Only)
        if self.is_wave_channel and self.hardware_mgr:
            self.phantom_btn = Gtk.Button(label="48V")
            self.phantom_btn.add_css_class("flat")
            self.phantom_btn.add_css_class("wave-48v-badge")
            self.phantom_btn.set_valign(Gtk.Align.CENTER)
            self.phantom_btn.connect("clicked", self._on_phantom_clicked)
            self.update_phantom_state(self.hardware_mgr.phantom_power_48v)
            self.append(self.phantom_btn)

        # 5. Channel settings gear popover button
        self.settings_btn = Gtk.MenuButton()
        self.settings_btn.set_icon_name("emblem-system-symbolic")
        self.settings_btn.add_css_class("flat")
        self.settings_btn.add_css_class("wave-icon-btn")
        self.settings_btn.set_tooltip_text(f"Configure '{display_name}'")
        self._setup_channel_popover()
        self.append(self.settings_btn)

        # 6. Mute button
        self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.add_css_class("wave-icon-btn")
        self.mute_btn.set_valign(Gtk.Align.CENTER)
        self.mute_btn.connect("clicked", self._on_mute_clicked)
        self.append(self.mute_btn)

        # 7. Stereo Split Volume Slider & VU Meter (Master Channel Gain)
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

        # 8. Link/Unlink multi-mix toggle button
        self.link_btn = Gtk.Button.new_from_icon_name("insert-link-symbolic")
        self.link_btn.add_css_class("flat")
        self.link_btn.add_css_class("wave-icon-btn")
        self.link_btn.set_tooltip_text("Link volume across mixes")
        self.link_btn.connect("clicked", self._on_link_clicked)
        self.append(self.link_btn)

        # -------------------------------------------------------------
        # Vertical Drag & Drop Controller Setup (Attached ONLY to Grip)
        # -------------------------------------------------------------
        self.drag_source = Gtk.DragSource.new()
        self.drag_source.set_actions(Gdk.DragAction.MOVE)

        def on_drag_prepare(src, x, y):
            return Gdk.ContentProvider.new_for_value(self.channel_info["id"])

        def on_drag_begin(src, drag):
            paintable = Gtk.WidgetPaintable.new(self)
            src.set_icon(paintable, 20, int(self.get_height() / 2))
            self.add_css_class("drag-source-active")

        def on_drag_end(src, drag, delete_data):
            self.remove_css_class("drag-source-active")
            if self.on_hover_row_callback:
                self.on_hover_row_callback(self.channel_info["id"], False)

        self.drag_source.connect("prepare", on_drag_prepare)
        self.drag_source.connect("drag-begin", on_drag_begin)
        self.drag_source.connect("drag-end", on_drag_end)
        self.drag_grip.add_controller(self.drag_source)

        self.drop_target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)

        def on_drop_enter(target, x, y):
            if self.on_hover_row_callback:
                self.on_hover_row_callback(self.channel_info["id"], True)
            return Gdk.DragAction.MOVE

        def on_drop_motion(target, x, y):
            if self.on_hover_row_callback:
                self.on_hover_row_callback(self.channel_info["id"], True)
            return Gdk.DragAction.MOVE

        def on_drop_leave(target):
            if self.on_hover_row_callback:
                self.on_hover_row_callback(self.channel_info["id"], False)

        def on_drop(target, value, x, y):
            if self.on_hover_row_callback:
                self.on_hover_row_callback(self.channel_info["id"], False)
            source_ch_id = value
            target_ch_id = self.channel_info["id"]
            if source_ch_id and source_ch_id != target_ch_id and self.on_reorder_callback:
                self.on_reorder_callback(source_ch_id, target_ch_id)
                return True
            return False

        self.drop_target.connect("enter", on_drop_enter)
        self.drop_target.connect("motion", on_drop_motion)
        self.drop_target.connect("leave", on_drop_leave)
        self.drop_target.connect("drop", on_drop)
        self.add_controller(self.drop_target)

    def _resolve_icon(self) -> str:
        if self.is_wave_channel and self.hardware_mgr:
            dev_icon = self.hardware_mgr.get_device_icon(self.hardware_mgr.device_name)
            if dev_icon and dev_icon != "audio-input-microphone-symbolic":
                return dev_icon
            return "elgato-wave-xlr-symbolic"
        assigned = self.pipewire_mgr.get_assigned_apps(self.channel_info["id"])
        primary_app = assigned[0] if assigned else self.channel_info.get("name", "")
        return self.channel_info.get("icon") or self.pipewire_mgr.resolve_icon_for_app(primary_app)

    def _on_phantom_clicked(self, btn):
        if not self.hardware_mgr:
            return
        is_active = self.hardware_mgr.phantom_power_48v
        if not is_active:
            root_win = self.get_root()
            if not isinstance(root_win, Gtk.Window):
                root_win = self.get_native() if isinstance(self.get_native(), Gtk.Window) else None
            dialog = Adw.MessageDialog(
                transient_for=root_win,
                heading="Enable 48V Phantom Power?",
                body="48V Phantom Power provides voltage to XLR condenser microphones. Ensure your microphone requires 48V power. Do NOT enable 48V for ribbon microphones."
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("enable", "Enable 48V")
            dialog.set_response_appearance("enable", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")

            def _on_response(d, resp):
                if resp == "enable":
                    new_val = self.hardware_mgr.set_phantom_power(True)
                    self.update_phantom_state(new_val)

            dialog.connect("response", _on_response)
            dialog.present()
        else:
            new_val = self.hardware_mgr.set_phantom_power(False)
            self.update_phantom_state(new_val)

    def _on_hardware_state_sync(self, curr: dict, changed: dict):
        if self.is_wave_channel:
            dial_mode = curr.get("dial_mode", "gain")
            if "phantom_power" in changed:
                self.update_phantom_state(bool(changed["phantom_power"]))
            if "mute" in changed:
                self.set_muted(bool(changed["mute"]))
            if "gain_db" in changed and dial_mode == "gain":
                vol_pct = max(0, min(100, int(round((float(changed["gain_db"]) / 75.0) * 100))))
                self.set_master_volume(vol_pct, self.pipewire_mgr.get_channel_master_mute(self.channel_info["id"]))

    def update_phantom_state(self, is_active: bool):
        if hasattr(self, "phantom_btn") and self.phantom_btn:
            if is_active:
                self.phantom_btn.set_label("⚡48V")
                self.phantom_btn.add_css_class("active-48v")
                self.phantom_btn.remove_css_class("dimmed-48v")
                self.phantom_btn.set_tooltip_text("48V Phantom Power Active (Click to disable)")
            else:
                self.phantom_btn.set_label("48V")
                self.phantom_btn.add_css_class("dimmed-48v")
                self.phantom_btn.remove_css_class("active-48v")
                self.phantom_btn.set_tooltip_text("Enable 48V Phantom Power for Condenser Mics")

    def _setup_channel_popover(self):
        popover = Gtk.Popover()
        popover.add_css_class("wave-popover")

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vbox.set_margin_top(8)
        vbox.set_margin_bottom(8)
        vbox.set_margin_start(8)
        vbox.set_margin_end(8)

        lbl = Gtk.Label(label=f"Channel: {self.channel_info.get('name')}")
        lbl.add_css_class("heading")
        lbl.set_halign(Gtk.Align.START)
        vbox.append(lbl)

        # Rename entry
        rename_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry = Gtk.Entry(text=self.channel_info.get("name", ""))
        entry.set_hexpand(True)
        rename_btn = Gtk.Button(label="Rename")
        rename_btn.add_css_class("suggested-action")
        
        def on_rename(b):
            new_name = entry.get_text().strip()
            if new_name:
                self.pipewire_mgr.rename_channel(self.channel_info["id"], new_name)
                self.refresh_name()
                if self.on_channel_renamed_callback:
                    self.on_channel_renamed_callback(self.channel_info["id"], new_name)
                popover.popdown()

        rename_btn.connect("clicked", on_rename)
        rename_box.append(entry)
        rename_box.append(rename_btn)
        vbox.append(rename_box)

        # Peak meter sync toggle switch
        sync_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sync_lbl = Gtk.Label(label="Mirror L/R Peak Meters", hexpand=True, halign=Gtk.Align.START)
        sync_switch = Gtk.Switch(active=self.pipewire_mgr.get_channel_sync_meter(self.channel_info["id"]))
        sync_switch.connect("state-set", self._on_sync_meter_toggled)
        sync_row.append(sync_lbl)
        sync_row.append(sync_switch)
        vbox.append(sync_row)

        # Hardware LED Color Dropdown for Elgato Wave device
        if self.is_wave_channel and self.hardware_mgr:
            vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            led_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            led_lbl = Gtk.Label(label="Hardware LED (Mic Gain):", hexpand=True, halign=Gtk.Align.START)
            led_lbl.add_css_class("mix-header-subtitle")
            from .led_color_picker import LEDColorButton
            led_btn = LEDColorButton(self.hardware_mgr, "gain", title="Hardware LED")
            led_row.append(led_lbl)
            led_row.append(led_btn)
            vbox.append(led_row)

        # Delete Channel button (Available for all custom and device channels)
        remove_btn = Gtk.Button(label="Delete Channel")
        remove_btn.add_css_class("destructive-action")
        remove_btn.connect("clicked", self._on_remove_clicked)
        vbox.append(remove_btn)

        popover.set_child(vbox)
        self.settings_btn.set_popover(popover)

    def _on_sync_meter_toggled(self, switch, state):
        self.pipewire_mgr.set_channel_sync_meter(self.channel_info["id"], state)
        self.slider.sync_peaks = state
        if self.on_sync_meter_callback:
            self.on_sync_meter_callback(self.channel_info["id"], state)
        return False

    def _on_remove_clicked(self, btn):
        self.settings_btn.get_popover().popdown()
        if self.on_channel_removed_callback:
            self.on_channel_removed_callback(self.channel_info["id"])

    def _on_slider_volume_changed(self, new_vol):
        ch_id = self.channel_info["id"]
        self.pipewire_mgr.set_channel_master_volume(ch_id, new_vol)
        if self.is_wave_channel and self.hardware_mgr:
            gain_db = int(round((new_vol / 100.0) * 75.0))
            self.hardware_mgr.set_gain(gain_db, transient=True)
        if self.pipewire_mgr.is_channel_linked(ch_id) and self.on_link_toggle_callback:
            self.on_link_toggle_callback(ch_id, True)

    def set_master_volume(self, volume: int, is_muted: bool = False):
        self.slider.set_volume(volume, is_muted)

    def set_muted(self, is_muted: bool):
        vol = self.pipewire_mgr.get_channel_master_volume(self.channel_info["id"])
        self.slider.set_volume(vol, is_muted)
        if is_muted:
            self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.mute_btn.add_css_class("muted")
            self.add_css_class("muted")
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")
            self.remove_css_class("muted")

    def _on_mute_clicked(self, btn):
        ch_id = self.channel_info["id"]
        self.pipewire_mgr.toggle_channel_master_mute(ch_id)
        self.update_ui_state()
        if self.pipewire_mgr.is_channel_linked(ch_id) and self.on_link_toggle_callback:
            self.on_link_toggle_callback(ch_id, True)

    def _on_link_clicked(self, btn):
        ch_id = self.channel_info["id"]
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
            self.add_css_class("muted")
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")
            self.remove_css_class("muted")

        if linked:
            self.link_btn.set_icon_name("insert-link-symbolic")
            self.link_btn.add_css_class("active")
        else:
            self.link_btn.set_icon_name("mail-attachment-symbolic")
            self.link_btn.remove_css_class("active")

        if self.is_wave_channel and self.hardware_mgr:
            self.update_phantom_state(self.hardware_mgr.phantom_power_48v)

    def refresh_name(self):
        display_name = self.channel_info.get("name", "Channel")
        if self.is_wave_channel and self.hardware_mgr and self.hardware_mgr.device_name:
            display_name = self.hardware_mgr.get_device_display_name(self.hardware_mgr.device_name)
        self.title_lbl.set_text(display_name)
        self.icon_img.set_from_icon_name(self._resolve_icon())

    def update_peaks(self, peak_l: float, peak_r: float):
        if hasattr(self, "slider") and self.slider:
            self.slider.set_peaks(peak_l, peak_r)
