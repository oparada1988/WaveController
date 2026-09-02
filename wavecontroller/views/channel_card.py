import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GObject, Adw

from .stereo_slider import StereoSlider

class ChannelCard(Gtk.Box):
    """
    Channel identifier card displayed on the left column of the matrix.
    Contains the drag grip handle, hardware-accurate channel icon, title, settings popover,
    48V phantom power quick toggle badge, mute button, dual-track stereo volume slider
    with real-time VU meters, and link toggle.
    """
    def __init__(self, channel_info: dict, pipewire_mgr, hardware_mgr=None, on_link_toggle_callback=None, on_sync_meter_callback=None, on_channel_removed_callback=None, on_channel_renamed_callback=None, on_reorder_callback=None, on_hover_row_callback=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.channel_info = channel_info
        self.pipewire_mgr = pipewire_mgr
        self.hardware_mgr = hardware_mgr
        self.on_link_toggle_callback = on_link_toggle_callback
        self.on_sync_meter_callback = on_sync_meter_callback
        self.on_channel_removed_callback = on_channel_removed_callback
        self.on_channel_renamed_callback = on_channel_renamed_callback
        self.on_reorder_callback = on_reorder_callback
        self.on_hover_row_callback = on_hover_row_callback
        
        self.set_valign(Gtk.Align.CENTER)
        self.set_hexpand(False)
        self.set_size_request(510, -1)

        # Header card containing standard strip controls
        self.header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.header_box.add_css_class("channel-row-card")
        self.header_box.set_valign(Gtk.Align.CENTER)
        self.header_box.set_hexpand(False)
        self.header_box.set_size_request(510, -1)

        # Identify if this channel corresponds to Elgato Wave hardware
        ch_id = str(channel_info.get("id", ""))
        ch_name = str(channel_info.get("name", "")).lower()
        ch_type = str(channel_info.get("type", "sink")).lower()
        is_source = ch_type in ("source", "hardware")
        assigned_apps = self.pipewire_mgr.get_assigned_apps(ch_id) if self.pipewire_mgr else []
        has_elgato_token = any("elgato" in a.lower() or "wave xlr" in a.lower() or "wave:3" in a.lower() or "wave:1" in a.lower() or "0fd9" in a.lower() for a in assigned_apps) or any(k in ch_name for k in ("elgato", "wave xlr", "wave:3", "wave:1", "wave neo"))
        is_hardware_elgato = bool(self.hardware_mgr and getattr(self.hardware_mgr, "is_elgato", False))
        self.is_mic_channel = is_source or any(k in ch_id.lower() for k in ("mic", "fefine", "fifine", "capture", "input")) or (ch_type not in ("sink", "app", "group") and any(k in ch_name for k in ("mic", "fefine", "fifine", "capture", "input", "microphone")))
        
        # 48V Phantom Power is exclusively available on Wave XLR hardware and XLR audio interfaces
        has_xlr_token = any("wave xlr" in a.lower() or "wave_xlr" in a.lower() or "0fd9:007d" in a.lower() for a in assigned_apps) or any(k in ch_name for k in ("wave xlr", "wave_xlr", "xlr"))
        self.supports_phantom_power = is_source and (has_xlr_token or (is_hardware_elgato and getattr(self.hardware_mgr, "is_wave_xlr", True) and not any(k in ch_name for k in ("fifine", "fefine", "usb", "hyperx", "yeti", "shure", "rode", "realtek", "pci", "analog", "headset", "wave:3", "wave:1"))))

        if assigned_apps:
            self.is_wave_channel = is_source and has_elgato_token
        else:
            self.is_wave_channel = is_source and (has_elgato_token or (is_hardware_elgato and not any(k in ch_name for k in ("fifine", "fefine", "usb", "hyperx", "yeti", "shure", "rode", "realtek", "pci", "analog", "headset"))))

        # 1. Dedicated Vertical 6-Dots Drag Grip Handle (list-drag-handle-symbolic)
        self.drag_grip = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        self.drag_grip.set_pixel_size(16)
        self.drag_grip.add_css_class("channel-drag-handle")
        self.drag_grip.set_cursor_from_name("grab")
        self.drag_grip.set_tooltip_text("Click and hold to reorder channel vertically")
        self.header_box.append(self.drag_grip)

        # 2. Channel icon (Auto-resolve from dedicated hardware or assigned apps)
        icon_name = self._resolve_icon()
        self.icon_img = Gtk.Image.new_from_icon_name(icon_name)
        self.icon_img.set_pixel_size(20)
        self.header_box.append(self.icon_img)

        # 3. Channel Title + Subtitle / Offline Badge Box
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_box.set_hexpand(False)
        title_box.set_size_request(104, -1)

        display_name = channel_info.get("name", "Channel")

        self.title_lbl = Gtk.Label(label=display_name)
        self.title_lbl.add_css_class("channel-title")
        self.title_lbl.set_halign(Gtk.Align.START)
        self.title_lbl.set_ellipsize(3)
        title_box.append(self.title_lbl)

        # Determine if this is a group channel (bundles multiple apps and/or exposes a system device)
        self.is_group_channel = (
            not self.is_mic_channel and (
                channel_info.get("type") == "group" or
                channel_info.get("icon") == "folder-symbolic" or
                "group" in ch_id.lower() or
                "group" in ch_name
            )
        )

        self.is_offline = self._is_channel_offline()

        if self.is_mic_channel:
            # Physical Microphone: show explicit [ Online ] (green) or [ Offline ] (amber) badge
            self.badge_lbl = Gtk.Label(label="Offline" if self.is_offline else "Online")
            self.badge_lbl.add_css_class("device-badge")
            self.badge_lbl.add_css_class("offline" if self.is_offline else "online")
            self.badge_lbl.set_halign(Gtk.Align.START)
            self.badge_lbl.set_valign(Gtk.Align.CENTER)
            title_box.append(self.badge_lbl)
            self.sub_lbl = None
            if self.is_offline:
                self.icon_img.set_opacity(0.55)
            else:
                self.icon_img.set_opacity(1.0)
        else:
            # Application playback channel: show assigned apps subtitle
            assigned = self.pipewire_mgr.get_assigned_apps(channel_info["id"]) if self.pipewire_mgr else []
            clean_assigned = [a for a in assigned if not a.startswith("usb-") and not a.startswith("alsa_card.")]
            sub_text = ", ".join(clean_assigned[:2]) if clean_assigned else "No apps assigned"
            self.sub_lbl = Gtk.Label(label=sub_text)
            self.sub_lbl.add_css_class("mix-header-subtitle")
            self.sub_lbl.set_halign(Gtk.Align.START)
            self.sub_lbl.set_ellipsize(3)
            title_box.append(self.sub_lbl)
            self.badge_lbl = None

        self.header_box.append(title_box)

        # 4. 48V Phantom Power Quick Toggle (Strictly for hardware supporting 48V Phantom Power)
        if self.supports_phantom_power and self.hardware_mgr and hasattr(self.hardware_mgr, "phantom_power_48v"):
            self.phantom_btn = Gtk.Button(label="48V")
            self.phantom_btn.add_css_class("flat")
            self.phantom_btn.add_css_class("wave-48v-badge")
            self.phantom_btn.set_valign(Gtk.Align.CENTER)
            self.phantom_btn.connect("clicked", self._on_phantom_clicked)
            self.update_phantom_state(self.hardware_mgr.phantom_power_48v)
            self.header_box.append(self.phantom_btn)

        # 5. Channel settings gear popover button
        self.settings_btn = Gtk.MenuButton()
        self.settings_btn.set_icon_name("emblem-system-symbolic")
        self.settings_btn.add_css_class("flat")
        self.settings_btn.add_css_class("wave-icon-btn")
        self.settings_btn.set_tooltip_text(f"Configure '{display_name}'")
        self._setup_channel_popover()
        self.header_box.append(self.settings_btn)

        # 5b. Group Channel App Expand / Collapse Toggle Chevron
        if self.is_group_channel:
            self.expand_btn = Gtk.Button.new_from_icon_name("pan-down-symbolic")
            self.expand_btn.add_css_class("flat")
            self.expand_btn.add_css_class("wave-icon-btn")
            self.expand_btn.set_valign(Gtk.Align.CENTER)
            self.expand_btn.set_tooltip_text("Expand member applications")
            self.expand_btn.connect("clicked", self._on_toggle_drawer)
            self.header_box.append(self.expand_btn)
        else:
            self.expand_btn = None

        # 6. Mute button
        self.mute_btn = Gtk.Button.new_from_icon_name("audio-volume-high-symbolic")
        self.mute_btn.add_css_class("flat")
        self.mute_btn.add_css_class("wave-icon-btn")
        self.mute_btn.set_tooltip_text("Mute master channel")
        self.mute_btn.set_valign(Gtk.Align.CENTER)
        self.mute_btn.connect("clicked", self._on_mute_clicked)
        self.header_box.append(self.mute_btn)

        # 7. Stereo Split Volume Slider & VU Meter (Master Channel Gain)
        vol = self.pipewire_mgr.get_channel_master_volume(self.channel_info["id"])
        if self.is_wave_channel and self.hardware_mgr:
            vol = max(0, min(100, int(round((self.hardware_mgr.hardware_gain_db / 75.0) * 100))))
        muted = self.pipewire_mgr.get_channel_master_mute(self.channel_info["id"])
        is_synced = self.pipewire_mgr.get_channel_sync_meter(self.channel_info["id"])
        if muted:
            self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
            self.mute_btn.add_css_class("muted")
            self.add_css_class("muted")
            self.header_box.add_css_class("muted")
        self.slider = StereoSlider(
            volume=vol,
            is_muted=muted,
            sync_peaks=is_synced,
            on_volume_changed=self._on_slider_volume_changed
        )
        self.slider.set_hexpand(False)
        self.slider.set_size_request(85, 20)
        self.header_box.append(self.slider)

        # 8. Link/Unlink multi-mix toggle button
        self.link_btn = Gtk.Button.new_from_icon_name("insert-link-symbolic")
        self.link_btn.add_css_class("flat")
        self.link_btn.add_css_class("wave-icon-btn")
        self.link_btn.set_tooltip_text("Link volume across mixes")
        self.link_btn.connect("clicked", self._on_link_clicked)
        self.header_box.append(self.link_btn)

        # 9. Expandable Drawer for Group Channel Applications (Layered directly behind card)
        if self.is_group_channel:
            self.overlay = Gtk.Overlay()
            self.overlay.set_size_request(510, -1)

            self.base_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.top_spacer = Gtk.Box()
            self.top_spacer.set_size_request(510, 56)
            self.base_container.append(self.top_spacer)

            self.drawer_revealer = Gtk.Revealer()
            self.drawer_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
            self.drawer_revealer.set_transition_duration(200)
            self.drawer_revealer.set_reveal_child(False)

            self.drawer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            self.drawer_box.add_css_class("group-drawer-container")
            self.drawer_revealer.set_child(self.drawer_box)
            self.base_container.append(self.drawer_revealer)

            self.overlay.set_child(self.base_container)
            self.header_box.set_valign(Gtk.Align.START)
            self.overlay.add_overlay(self.header_box)
            self.append(self.overlay)
        else:
            self.append(self.header_box)

        self.app_sub_widgets = {}
        if self.is_group_channel:
            self._build_app_sub_strips()

        # Hook live state changes from physical hardware dial & mute
        if self.is_wave_channel and self.hardware_mgr and hasattr(self.hardware_mgr, "add_hardware_listener"):
            self.hardware_mgr.add_hardware_listener(lambda curr, changed: GLib.idle_add(self._on_hardware_synced, curr, changed))

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

    def _is_channel_offline(self) -> bool:
        ch_id = str(self.channel_info.get("id", "")).lower()
        ch_type = self.channel_info.get("type", "sink")

        # Wave XLR primary hardware
        if self.is_wave_channel:
            return not getattr(self.hardware_mgr, "is_connected", True)

        # If it's a physical source or hardware input channel
        if ch_type == "source" or any(k in ch_id for k in ("mic", "fefine", "fifine", "capture", "input")):
            discovered = getattr(self.hardware_mgr, "discovered_devices", {})
            assigned = self.pipewire_mgr.get_assigned_apps(self.channel_info["id"]) if self.pipewire_mgr else []

            for a in assigned:
                if a in discovered:
                    return False
            for dev_k, dev in discovered.items():
                d_name = dev.get("name", "").lower()
                if ch_id in dev_k.lower() or ch_id in d_name:
                    return False
                ch_name = str(self.channel_info.get("name", "")).lower()
                if len(ch_name) >= 3 and (ch_name in d_name or ch_name in dev_k.lower()):
                    return False
            return True

        return False

    def _resolve_icon(self) -> str:
        if self.is_wave_channel and self.hardware_mgr:
            dev_icon = self.hardware_mgr.get_device_icon(self.hardware_mgr.device_name)
            if dev_icon and dev_icon not in ("audio-input-microphone-symbolic", "network-offline-symbolic"):
                return dev_icon
            return "elgato-wave-xlr-symbolic"

        ch_icon = self.channel_info.get("icon")
        if ch_icon and ch_icon not in ("network-offline-symbolic", "elgato-wave-xlr-symbolic"):
            return ch_icon
        elif ch_icon == "elgato-wave-xlr-symbolic" and self.is_wave_channel:
            return ch_icon

        assigned = self.pipewire_mgr.get_assigned_apps(self.channel_info["id"]) if self.pipewire_mgr else []
        primary_app = assigned[0] if assigned else self.channel_info.get("name", "")
        resolved = self.pipewire_mgr.resolve_icon_for_app(primary_app) if self.pipewire_mgr else None
        if not resolved or resolved in ("network-offline-symbolic", "audio-card-symbolic", "audio-x-generic-symbolic"):
            if ch_icon and ch_icon != "network-offline-symbolic" and (ch_icon != "elgato-wave-xlr-symbolic" or self.is_wave_channel):
                return ch_icon
            return "audio-input-microphone-symbolic" if self.channel_info.get("type") == "source" else "audio-card-symbolic"
        return resolved

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
            if "mute" in changed and "dial_mode" not in changed and dial_mode == "gain":
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
        popover.set_autohide(True)
        popover.set_cascade_popdown(True)
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

        # Group Channels Exclusive Options: Virtual System Audio Device & Grouped Applications
        if self.is_group_channel:
            vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

            sink_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            sink_title = Gtk.Label(label="Expose as System Audio Device", hexpand=True, halign=Gtk.Align.START)
            sink_title.add_css_class("mix-header-subtitle")
            sink_switch = Gtk.Switch(active=self.pipewire_mgr.is_channel_sink_exposed(self.channel_info["id"]))

            def _on_sink_switch_toggled(sw, state):
                self.pipewire_mgr.set_channel_sink_exposed(self.channel_info["id"], state)
                self.refresh_apps()
                return False

            sink_switch.connect("state-set", _on_sink_switch_toggled)
            sink_row.append(sink_title)
            sink_row.append(sink_switch)
            vbox.append(sink_row)

            # Grouped Applications List
            vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            apps_head = Gtk.Label(label="Grouped Applications:")
            apps_head.add_css_class("heading")
            apps_head.set_halign(Gtk.Align.START)
            vbox.append(apps_head)

            assigned_scroll = Gtk.ScrolledWindow()
            assigned_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            assigned_scroll.set_propagate_natural_height(True)
            assigned_scroll.set_max_content_height(140)

            apps_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            assigned_scroll.set_child(apps_container)
            vbox.append(assigned_scroll)

            # Available apps expander section
            avail_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            avail_section.set_visible(False)
            avail_section.set_margin_top(4)

            avail_head = Gtk.Label(label="Available Audio Applications:")
            avail_head.add_css_class("dim-label")
            avail_head.set_halign(Gtk.Align.START)
            avail_section.append(avail_head)

            avail_scroll = Gtk.ScrolledWindow()
            avail_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            avail_scroll.set_propagate_natural_height(True)
            avail_scroll.set_max_content_height(180) # Capped at ~5 apps

            avail_apps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            avail_scroll.set_child(avail_apps_box)
            avail_section.append(avail_scroll)

            # Custom App Name Entry Box for adding unlisted apps (e.g. Discord, Spotify, Games)
            custom_app_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            custom_app_box.set_margin_top(4)
            custom_app_entry = Gtk.Entry(placeholder_text="Custom application name...")
            custom_app_entry.set_hexpand(True)
            custom_add_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
            custom_add_btn.add_css_class("suggested-action")
            custom_add_btn.set_tooltip_text("Add custom application to group")
            custom_app_box.append(custom_app_entry)
            custom_app_box.append(custom_add_btn)
            avail_section.append(custom_app_box)

            # Toggle button for inline app selector
            toggle_add_btn = Gtk.Button()
            toggle_add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            toggle_add_icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
            toggle_add_icon.set_pixel_size(14)
            toggle_add_box.append(toggle_add_icon)
            toggle_add_lbl = Gtk.Label(label="Add Application to Group")
            toggle_add_box.append(toggle_add_lbl)
            toggle_add_arrow = Gtk.Image.new_from_icon_name("pan-down-symbolic")
            toggle_add_arrow.set_pixel_size(12)
            toggle_add_box.append(toggle_add_arrow)
            toggle_add_btn.set_child(toggle_add_box)
            toggle_add_btn.add_css_class("suggested-action")
            toggle_add_btn.set_margin_top(4)

            def refresh_available_apps():
                while avail_apps_box.get_first_child():
                    avail_apps_box.remove(avail_apps_box.get_first_child())

                ch_id = self.channel_info["id"]
                # Build set of ALL app names assigned to ANY channel (not just this one)
                all_assigned_names = set()
                for other_ch_id, apps in self.pipewire_mgr.assigned_apps.items():
                    for app in apps:
                        all_assigned_names.add(str(app).strip().lower())
                # Also include dynamically connected apps for this channel
                for a in self.pipewire_mgr.get_channel_all_apps(ch_id):
                    all_assigned_names.add(a["name"].lower())
                running_apps = self.pipewire_mgr.get_detected_apps()

                avail = [a for a in running_apps if a["name"].lower() not in all_assigned_names]
                if not avail:
                    none_lbl = Gtk.Label(label="No other active audio apps found")
                    none_lbl.add_css_class("dim-label")
                    avail_apps_box.append(none_lbl)
                else:
                    for app_info in avail:
                        aname = app_info["name"]
                        ibtn = Gtk.Button()
                        ibtn.add_css_class("flat")
                        ibtn.add_css_class("wave-sidebar-row")

                        irow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                        i_icon = app_info.get("icon") or self.pipewire_mgr.resolve_icon_for_app(aname)
                        i_img = Gtk.Image.new_from_icon_name(i_icon)
                        i_img.set_pixel_size(16)
                        irow.append(i_img)

                        ilbl = Gtk.Label(label=aname, hexpand=True, halign=Gtk.Align.START)
                        irow.append(ilbl)

                        plus_ic = Gtk.Image.new_from_icon_name("list-add-symbolic")
                        plus_ic.set_pixel_size(12)
                        irow.append(plus_ic)

                        ibtn.set_child(irow)

                        def make_assign(n):
                            def _assign(b):
                                self.pipewire_mgr.assign_app_to_channel(ch_id, n)
                                rebuild_app_list()
                                refresh_available_apps()
                                self.refresh_apps()
                            return _assign

                        ibtn.connect("clicked", make_assign(aname))
                        avail_apps_box.append(ibtn)

            def rebuild_app_list():
                while apps_container.get_first_child():
                    apps_container.remove(apps_container.get_first_child())

                ch_id = self.channel_info["id"]
                current_apps = self.pipewire_mgr.get_channel_all_apps(ch_id)

                if not current_apps:
                    empty_lbl = Gtk.Label(label="No applications assigned yet")
                    empty_lbl.add_css_class("dim-label")
                    empty_lbl.set_halign(Gtk.Align.START)
                    apps_container.append(empty_lbl)
                else:
                    for app_dict in current_apps:
                        app_name = app_dict["name"]
                        app_src = app_dict.get("source", "manual")

                        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                        row.add_css_class("wave-app-row")

                        icon_name = app_dict.get("icon") or self.pipewire_mgr.resolve_icon_for_app(app_name)
                        ic = Gtk.Image.new_from_icon_name(icon_name)
                        ic.set_pixel_size(16)
                        row.append(ic)

                        nlbl = Gtk.Label(label=app_name, hexpand=True, halign=Gtk.Align.START)
                        nlbl.set_ellipsize(3)
                        row.append(nlbl)

                        if app_src == "sink":
                            badge = Gtk.Label(label="In-App")
                            badge.add_css_class("device-badge")
                            badge.add_css_class("online")
                            badge.set_tooltip_text("Connected via in-app output setting or Desktop Settings")
                            row.append(badge)

                        del_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
                        del_btn.add_css_class("flat")
                        del_btn.add_css_class("wave-icon-btn")
                        del_btn.set_tooltip_text(f"Remove '{app_name}' from this group")

                        def make_unassign(aname):
                            def _unassign(b):
                                self.pipewire_mgr.unassign_app_from_channel(ch_id, aname)
                                rebuild_app_list()
                                refresh_available_apps()
                                self.refresh_apps()
                            return _unassign

                        del_btn.connect("clicked", make_unassign(app_name))
                        row.append(del_btn)

                        apps_container.append(row)

            def on_toggle_add_clicked(b):
                is_vis = not avail_section.get_visible()
                avail_section.set_visible(is_vis)
                toggle_add_arrow.set_from_icon_name("pan-up-symbolic" if is_vis else "pan-down-symbolic")
                if is_vis:
                    refresh_available_apps()

            toggle_add_btn.connect("clicked", on_toggle_add_clicked)

            def on_custom_app_add(b=None):
                cname = custom_app_entry.get_text().strip()
                if cname:
                    ch_id = self.channel_info["id"]
                    self.pipewire_mgr.assign_app_to_channel(ch_id, cname)
                    custom_app_entry.set_text("")
                    rebuild_app_list()
                    refresh_available_apps()
                    self.refresh_apps()

            custom_add_btn.connect("clicked", on_custom_app_add)
            custom_app_entry.connect("activate", on_custom_app_add)

            rebuild_app_list()
            vbox.append(toggle_add_btn)
            vbox.append(avail_section)

        # Hardware LED Controls for Elgato Wave device
        if self.is_wave_channel and self.hardware_mgr:
            vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            from .led_color_picker import LEDColorButton
            
            # Mic Gain Mode LED Ring Color
            gain_led_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            gain_led_lbl = Gtk.Label(label="Mic Gain Mode Ring Color:", hexpand=True, halign=Gtk.Align.START)
            gain_led_lbl.add_css_class("mix-header-subtitle")
            gain_led_btn = LEDColorButton(self.hardware_mgr, "gain", title="Mic Gain LED", parent_popover=popover)
            gain_led_row.append(gain_led_lbl)
            gain_led_row.append(gain_led_btn)
            vbox.append(gain_led_row)

        vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

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
        popover = self.settings_btn.get_popover()
        if popover:
            popover.popdown()

        ch_name = self.channel_info.get("name", "Channel")
        if self.is_wave_channel and self.hardware_mgr and self.hardware_mgr.device_name:
            ch_name = self.hardware_mgr.get_device_display_name(self.hardware_mgr.device_name)

        root_win = self.get_root()
        if not isinstance(root_win, Gtk.Window):
            root_win = self.get_native() if isinstance(self.get_native(), Gtk.Window) else None

        dialog = Adw.MessageDialog(
            transient_for=root_win,
            heading=f"Delete '{ch_name}' Channel?",
            body=f"Are you sure you want to delete the '{ch_name}' channel? This will remove its channel strip and tear down its virtual audio routing across all mixes."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete Channel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def _on_response(d, response):
            if response == "delete":
                if self.on_channel_removed_callback:
                    self.on_channel_removed_callback(self.channel_info["id"])

        dialog.connect("response", _on_response)
        dialog.present()

    def _on_slider_volume_changed(self, new_vol):
        ch_id = self.channel_info["id"]
        self.pipewire_mgr.set_channel_master_volume(ch_id, new_vol)
        if self.is_wave_channel and self.hardware_mgr and getattr(self.hardware_mgr, "is_elgato", False):
            gain_db = int(round((new_vol / 100.0) * 75.0))
            self.hardware_mgr.set_gain(gain_db, transient=False)
        if self.pipewire_mgr.is_channel_linked(ch_id) and self.on_link_toggle_callback:
            self.on_link_toggle_callback(ch_id, True)

    def set_master_volume(self, volume: int, is_muted: bool = False):
        ch_id = self.channel_info["id"]
        self.slider.set_volume(volume, is_muted)
        self.pipewire_mgr.set_channel_master_volume(ch_id, volume)
        if self.pipewire_mgr.is_channel_linked(ch_id) and self.on_link_toggle_callback:
            self.on_link_toggle_callback(ch_id, True)

    def set_muted(self, is_muted: bool):
        vol = self.pipewire_mgr.get_channel_master_volume(self.channel_info["id"])
        self.slider.set_volume(vol, is_muted)
        if getattr(self, "_last_mute_state", None) != is_muted:
            self._last_mute_state = is_muted
            if is_muted:
                self.mute_btn.set_icon_name("audio-volume-muted-symbolic")
                self.mute_btn.add_css_class("muted")
                self.add_css_class("muted")
                self.header_box.add_css_class("muted")
                self.mute_btn.set_tooltip_text("Unmute master channel")
            else:
                self.mute_btn.set_icon_name("audio-volume-high-symbolic")
                self.mute_btn.remove_css_class("muted")
                self.remove_css_class("muted")
                self.header_box.remove_css_class("muted")
                self.mute_btn.set_tooltip_text("Mute master channel")

    def _on_mute_clicked(self, btn):
        ch_id = self.channel_info["id"]
        is_muted = self.pipewire_mgr.toggle_channel_master_mute(ch_id)
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
            self.header_box.add_css_class("muted")
        else:
            self.mute_btn.set_icon_name("audio-volume-high-symbolic")
            self.mute_btn.remove_css_class("muted")
            self.remove_css_class("muted")
            self.header_box.remove_css_class("muted")

        if linked:
            self.link_btn.set_icon_name("insert-link-symbolic")
            self.link_btn.add_css_class("active")
        else:
            self.link_btn.set_icon_name("mail-attachment-symbolic")
            self.link_btn.remove_css_class("active")

        if self.is_wave_channel and self.hardware_mgr:
            self.update_phantom_state(self.hardware_mgr.phantom_power_48v)

    def _on_hardware_synced(self, curr: dict, changed: dict):
        if not (getattr(self, "is_mic_channel", False) or getattr(self, "is_wave_channel", False)):
            return
        if "gain_db" in changed and self.hardware_mgr:
            gain_db = changed["gain_db"]
            vol_pct = max(0, min(100, int(round((gain_db / 75.0) * 100))))
            is_muted = self.pipewire_mgr.get_channel_master_mute(self.channel_info["id"])
            if self.slider.volume != vol_pct:
                self.slider.set_volume(vol_pct, is_muted)
        if "phantom_power" in changed and hasattr(self, "phantom_btn"):
            self.update_phantom_state(bool(changed["phantom_power"]))

    def refresh_hardware_state(self):
        if not getattr(self, "is_mic_channel", False):
            return

        is_offline = self._is_channel_offline()
        if getattr(self, "_last_is_offline", None) == is_offline:
            return
        self._last_is_offline = is_offline
        self.is_offline = is_offline
        if is_offline:
            self.icon_img.set_opacity(0.55)
            if hasattr(self, "badge_lbl") and self.badge_lbl:
                self.badge_lbl.set_text("Offline")
                self.badge_lbl.remove_css_class("online")
                self.badge_lbl.add_css_class("offline")
        else:
            self.icon_img.set_opacity(1.0)
            if hasattr(self, "badge_lbl") and self.badge_lbl:
                self.badge_lbl.set_text("Online")
                self.badge_lbl.remove_css_class("offline")
                self.badge_lbl.add_css_class("online")

    def _on_toggle_drawer(self, btn):
        if not hasattr(self, "drawer_revealer"):
            return
        is_revealed = self.drawer_revealer.get_reveal_child()
        new_state = not is_revealed
        self.drawer_revealer.set_reveal_child(new_state)
        if hasattr(self, "expand_btn") and self.expand_btn:
            self.expand_btn.set_icon_name("pan-up-symbolic" if new_state else "pan-down-symbolic")
            self.expand_btn.set_tooltip_text("Collapse member applications" if new_state else "Expand member applications")

    def _resolve_app_icon_name(self, app_name: str) -> str:
        if self.pipewire_mgr and hasattr(self.pipewire_mgr, "resolve_icon_for_app"):
            return self.pipewire_mgr.resolve_icon_for_app(app_name)
        return "applications-multimedia-symbolic"

    def _resolve_app_classification(self, app_name: str) -> str:
        app_low = str(app_name).lower()
        if "chrome" in app_low or "firefox" in app_low or "browser" in app_low:
            return "Web Browser"
        elif "discord" in app_low or "teams" in app_low or "zoom" in app_low or "slack" in app_low:
            return "Voice & Communication"
        elif "spotify" in app_low or "music" in app_low or "vlc" in app_low:
            return "Media Playback"
        elif "steam" in app_low or "game" in app_low:
            return "Gaming Audio"
        return "Application Stream"

    def _build_app_sub_strips(self):
        if not hasattr(self, "drawer_box"):
            return

        ch_id = self.channel_info.get("id", "")
        assigned = self.pipewire_mgr.get_assigned_apps(ch_id) if self.pipewire_mgr else []
        clean_assigned = [a for a in assigned if not a.startswith("usb-") and not a.startswith("alsa_card.")]

        # If the member applications list hasn't changed, update volume/mute in-place without rebuilding DOM
        if tuple(clean_assigned) == getattr(self, "_last_clean_assigned", None) and getattr(self, "app_sub_widgets", None):
            for app_name, w in self.app_sub_widgets.items():
                app_vol = self.pipewire_mgr.get_app_volume(app_name) if hasattr(self.pipewire_mgr, "get_app_volume") else 80
                app_muted = self.pipewire_mgr.get_app_mute(app_name) if hasattr(self.pipewire_mgr, "get_app_mute") else False
                if "slider" in w and w["slider"]:
                    w["slider"].set_volume(app_vol, app_muted)
                if "mute_btn" in w and w["mute_btn"]:
                    if app_muted:
                        w["mute_btn"].set_icon_name("audio-volume-muted-symbolic")
                        w["mute_btn"].add_css_class("muted")
                    else:
                        w["mute_btn"].set_icon_name("audio-volume-high-symbolic")
                        w["mute_btn"].remove_css_class("muted")
            return

        self._last_clean_assigned = tuple(clean_assigned)

        while True:
            first = self.drawer_box.get_first_child()
            if not first:
                break
            self.drawer_box.remove(first)
        self.app_sub_widgets = {}

        if not clean_assigned:
            empty_lbl = Gtk.Label(label="No applications in this group")
            empty_lbl.add_css_class("dim-label")
            empty_lbl.set_margin_top(4)
            empty_lbl.set_margin_bottom(4)
            self.drawer_box.append(empty_lbl)
            return

        for app_name in clean_assigned:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.add_css_class("group-app-sub-row")
            row.set_size_request(510, 32)
            row.set_valign(Gtk.Align.CENTER)

            # Left spacer
            sp = Gtk.Box()
            sp.set_size_request(16, -1)
            row.append(sp)

            # App icon
            ic_name = self._resolve_app_icon_name(app_name)
            ic_img = Gtk.Image.new_from_icon_name(ic_name)
            ic_img.set_pixel_size(20)
            row.append(ic_img)

            # Title / Subtitle Box
            tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            tb.set_hexpand(True)
            tb.set_size_request(96, -1)
            t_lbl = Gtk.Label(label=app_name)
            t_lbl.add_css_class("channel-title")
            t_lbl.set_halign(Gtk.Align.START)
            t_lbl.set_ellipsize(3)
            s_lbl = Gtk.Label(label=self._resolve_app_classification(app_name))
            s_lbl.add_css_class("mix-header-subtitle")
            s_lbl.set_halign(Gtk.Align.START)
            s_lbl.set_ellipsize(3)
            tb.append(t_lbl)
            tb.append(s_lbl)
            row.append(tb)

            # App Mute Button
            app_muted = self.pipewire_mgr.get_app_mute(app_name) if hasattr(self.pipewire_mgr, "get_app_mute") else False
            m_btn = Gtk.Button.new_from_icon_name("audio-volume-muted-symbolic" if app_muted else "audio-volume-high-symbolic")
            m_btn.add_css_class("flat")
            m_btn.add_css_class("wave-icon-btn")
            if app_muted:
                m_btn.add_css_class("muted")
            m_btn.set_valign(Gtk.Align.CENTER)
            m_btn.set_tooltip_text(f"Mute {app_name}")

            # App Volume Slider & VU Meter
            app_vol = self.pipewire_mgr.get_app_volume(app_name) if hasattr(self.pipewire_mgr, "get_app_volume") else 80
            app_slider = StereoSlider(
                volume=app_vol,
                is_muted=app_muted,
                sync_peaks=True,
                on_volume_changed=lambda v, a=app_name: self.pipewire_mgr.set_app_volume(a, v) if hasattr(self.pipewire_mgr, "set_app_volume") else None
            )
            app_slider.set_hexpand(False)
            app_slider.set_size_request(85, 20)

            def make_app_mute_handler(a, btn, sldr):
                def _toggle(b):
                    curr_m = sldr.is_muted
                    new_m = not curr_m
                    if hasattr(self.pipewire_mgr, "set_app_mute"):
                        self.pipewire_mgr.set_app_mute(a, new_m)
                    if new_m:
                        btn.set_icon_name("audio-volume-muted-symbolic")
                        btn.add_css_class("muted")
                        btn.set_tooltip_text(f"Unmute {a}")
                    else:
                        btn.set_icon_name("audio-volume-high-symbolic")
                        btn.remove_css_class("muted")
                        btn.set_tooltip_text(f"Mute {a}")
                    sldr.set_volume(sldr.volume, new_m)
                return _toggle

            m_btn.connect("clicked", make_app_mute_handler(app_name, m_btn, app_slider))
            row.append(m_btn)
            row.append(app_slider)

            # End Spacer
            end_sp = Gtk.Box()
            end_sp.set_size_request(24, -1)
            row.append(end_sp)

            self.drawer_box.append(row)
            self.app_sub_widgets[app_name] = {"slider": app_slider, "mute_btn": m_btn}

    def refresh_apps(self):
        if getattr(self, "is_mic_channel", False):
            return
        if not hasattr(self, "sub_lbl") or self.sub_lbl is None:
            return

        ch_id = self.channel_info["id"]
        all_apps = self.pipewire_mgr.get_channel_all_apps(ch_id) if self.pipewire_mgr else []
        if all_apps:
            app_names = [a["name"] for a in all_apps]
            if len(app_names) > 2:
                sub_text = f"{', '.join(app_names[:2])} (+{len(app_names)-2})"
            else:
                sub_text = ", ".join(app_names)
        elif self.pipewire_mgr and self.pipewire_mgr.is_channel_sink_exposed(ch_id):
            sub_text = "System Audio Device"
        else:
            sub_text = "No apps assigned"

        self.sub_lbl.set_label(sub_text)
        if self.is_group_channel:
            self._build_app_sub_strips()

    def refresh_name(self):
        display_name = self.channel_info.get("name", "Channel")
        self.title_lbl.set_text(display_name)
        self.icon_img.set_from_icon_name(self._resolve_icon())
        self.refresh_hardware_state()
        self.refresh_apps()

    def update_peaks(self, peak_l: float, peak_r: float):
        if hasattr(self, "slider") and self.slider:
            self.slider.set_peaks(peak_l, peak_r)
        if getattr(self, "is_group_channel", False) and getattr(self, "drawer_revealer", None) and self.drawer_revealer.get_reveal_child() and getattr(self, "app_sub_widgets", None):
            for app_name, w in self.app_sub_widgets.items():
                if "slider" in w and w["slider"]:
                    a_l, a_r = self.pipewire_mgr.get_app_peaks(app_name) if hasattr(self.pipewire_mgr, "get_app_peaks") else (0.0, 0.0)
                    w["slider"].set_peaks(a_l, a_r)
