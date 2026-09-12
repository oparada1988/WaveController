import os
import subprocess
import threading
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk

from ..engine.config_manager import config_manager
from ..engine.plugins import plugin_scanner, PluginFormat, PluginCategory, AudioPlugin
from ..utils.logger import get_logger

log = get_logger("EffectsView")

# Bundled LADSPA binaries copied to ~/.ladspa by install.sh to power WaveController's own
# Built-in DSP chain. The generic LADSPA scanner re-discovers these as raw plugins; hide that
# duplicate from the user-facing plugin list since they already appear as Built-in toggles above.
_BUNDLED_INTERNAL_LADSPA_FILES = {
    "fast_lookahead_limiter_1913.so",
    "gate_1410.so",
    "librnnoise_ladspa.so",
    "sc4m_1916.so",
}


def _is_hostable_lv2_plugin(plugin: AudioPlugin) -> bool:
    return (
        plugin.format == PluginFormat.LV2 and
        bool(plugin.plugin_uri) and
        len(plugin.audio_input_ports) >= 2 and
        len(plugin.audio_output_ports) >= 2
    )


def _plugin_display_key(plugin: AudioPlugin) -> str:
    return "".join(ch for ch in plugin.name.lower() if ch.isalnum())


class EffectsView(Gtk.Box):
    """
    Audio Effects & VST/LV2 Plugin Rack for WaveController.
    Provides controls for native PipeWire vocal DSP chain and indexes
    external VST3, LV2, and CLAP plugins.
    """
    def __init__(self, pipewire_mgr=None, on_global_fx_changed_callback=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.pipewire_mgr = pipewire_mgr
        self.on_global_fx_changed_callback = on_global_fx_changed_callback

        self.set_margin_top(20)
        self.set_margin_bottom(20)
        self.set_margin_start(24)
        self.set_margin_end(24)

        self._plugin_rows = []

        # Title & Header
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_lbl = Gtk.Label(label="Effects Manager")
        title_lbl.add_css_class("wave-main-title")
        title_box.append(title_lbl)
        self.append(title_box)

        # Scrolled container for settings
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        pref_page = Adw.PreferencesPage()

        # Group 1: Native Microphone Vocal DSP Chain (PipeWire Filter-Chain)
        grp_fx = Adw.PreferencesGroup(
            title="Microphone DSP Chain (PipeWire Filter-Chain)",
            description="Hardware-accelerated vocal processing applied directly to your microphone pre-fader."
        )

        def _on_toggle(key, r):
            config_manager.set(key, r.get_active(), immediate=True)
            if self.pipewire_mgr:
                self.pipewire_mgr.reload_channel_fx()
            # Rebuild per-channel FX popovers so their effect list reflects this
            # global toggle immediately, without recreating channels or restarting.
            if self.on_global_fx_changed_callback:
                GLib.idle_add(self.on_global_fx_changed_callback)

        # Low-Cut / High-Pass Filter (80 Hz)
        highpass_row = Adw.SwitchRow(
            title="Low-Cut / High-Pass Filter (80 Hz)",
            subtitle="Filters out low-frequency mechanical rumble, desk thumps, and handling noise"
        )
        highpass_row.set_active(config_manager.get("dsp_highpass", True))
        highpass_row.connect("notify::active", lambda r, *a: _on_toggle("dsp_highpass", r))
        grp_fx.add(highpass_row)

        # RNNoise Noise Suppression
        noise_row = Adw.SwitchRow(
            title="AI Noise Suppression (RNNoise)",
            subtitle="Eliminates background keyboard clicks, fans, and room noise"
        )
        noise_row.set_active(config_manager.get("dsp_noise_suppression", True))
        noise_row.connect("notify::active", lambda r, *a: _on_toggle("dsp_noise_suppression", r))
        grp_fx.add(noise_row)

        # Broadcast Noise Gate
        gate_row = Adw.SwitchRow(
            title="Broadcast Noise Gate",
            subtitle="Mutes microphone when below speech volume threshold"
        )
        gate_row.set_active(config_manager.get("dsp_noise_gate", False))
        gate_row.connect("notify::active", lambda r, *a: _on_toggle("dsp_noise_gate", r))
        grp_fx.add(gate_row)

        # Parametric EQ
        eq_row = Adw.SwitchRow(
            title="Parametric Vocal Equalizer",
            subtitle="3-Band broadcast tone shaping (Bass warmth, Presence, Vocal Air)"
        )
        eq_row.set_active(config_manager.get("dsp_equalizer", True))
        eq_row.connect("notify::active", lambda r, *a: _on_toggle("dsp_equalizer", r))
        grp_fx.add(eq_row)

        # Studio Compressor
        comp_row = Adw.SwitchRow(
            title="Broadcast Vocal Compressor",
            subtitle="Smooths dynamic volume spikes, elevates quiet whispers, and boosts broadcast presence"
        )
        comp_row.set_active(config_manager.get("dsp_compressor", True))
        comp_row.connect("notify::active", lambda r, *a: _on_toggle("dsp_compressor", r))
        grp_fx.add(comp_row)

        # De-Esser
        deess_row = Adw.SwitchRow(
            title="Vocal De-Esser",
            subtitle="Attenuates harsh high-frequency sibilance ('s' and 't' sounds)"
        )
        deess_row.set_active(config_manager.get("dsp_deesser", False))
        deess_row.connect("notify::active", lambda r, *a: _on_toggle("dsp_deesser", r))
        grp_fx.add(deess_row)

        # Brickwall Limiter
        limit_row = Adw.SwitchRow(
            title="Broadcast Peak Limiter",
            subtitle="Prevents digital clipping and overs on sudden vocal peaks (-0.5 dBFS ceiling guard)"
        )
        limit_row.set_active(config_manager.get("dsp_limiter", True))
        limit_row.connect("notify::active", lambda r, *a: _on_toggle("dsp_limiter", r))
        grp_fx.add(limit_row)

        pref_page.add(grp_fx)

        # Group 2: External VST3 & LV2 Plugin Management
        self.grp_vst = Adw.PreferencesGroup(
            title="External VST3 &amp; LV2 Plugin Directory",
            description="Scans standard user directories (~/.vst3, ~/.lv2) and system directories (/usr/lib/vst3, /usr/lib/lv2)"
        )

        # Action row with Install, Scan, and Open Folder buttons
        self.vst_scan_row = Adw.ActionRow(
            title="Plugin Scanner &amp; Indexer",
            subtitle="Discover installed audio plugins across system directories"
        )

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_valign(Gtk.Align.CENTER)

        # Open ~/.vst3 folder button
        open_folder_btn = Gtk.Button(icon_name="folder-open-symbolic")
        open_folder_btn.set_tooltip_text("Open ~/.vst3 plugin folder in file manager")
        open_folder_btn.connect("clicked", self._on_open_plugin_folder)
        btn_box.append(open_folder_btn)

        # Install Effect button (VST3/LV2 bundle folder picker)
        install_btn = Gtk.Button(label="Install Effect")
        install_btn.set_tooltip_text("Select a .vst3 or .lv2 plugin bundle folder to install")
        install_btn.connect("clicked", self._on_install_effect_clicked)
        btn_box.append(install_btn)

        # Scan button & spinner
        self.scan_spinner = Gtk.Spinner()
        self.scan_spinner.set_spinning(False)
        self.scan_spinner.set_visible(False)
        btn_box.append(self.scan_spinner)

        self.scan_btn = Gtk.Button(label="Scan Plugins")
        self.scan_btn.add_css_class("suggested-action")
        self.scan_btn.connect("clicked", self._on_scan_clicked)
        btn_box.append(self.scan_btn)

        self.vst_scan_row.add_suffix(btn_box)
        self.grp_vst.add(self.vst_scan_row)

        # Search filter entry row
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_hexpand(True)
        self.search_entry.set_placeholder_text("Filter plugins by name, format, category...")
        self.search_entry.connect("search-changed", self._on_search_changed)

        search_row = Adw.ActionRow(title="Search Indexed Plugins")
        search_row.add_suffix(self.search_entry)
        self.grp_vst.add(search_row)

        pref_page.add(self.grp_vst)

        # Group 2b: Custom Scan Paths
        self.grp_custom_paths = Adw.PreferencesGroup(
            title="Custom Scan Paths",
            description="Extra directories to search for VST3/LV2 bundles (e.g. Flatpak or DAW-bundled plugin folders)"
        )
        self._custom_path_rows = []

        add_path_row = Adw.ActionRow(title="Add Scan Path")
        add_path_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_path_btn.add_css_class("flat")
        add_path_btn.connect("clicked", self._on_add_custom_path_clicked)
        add_path_row.add_suffix(add_path_btn)
        self.grp_custom_paths.add(add_path_row)
        pref_page.add(self.grp_custom_paths)
        self._refresh_custom_paths_display()

        # Group 3: Discovered Plugins List
        self.grp_plugins_list = Adw.PreferencesGroup(
            title="Indexed Plugins Library",
            description="Installed stereo LV2 plugins can be enabled per channel from the FX popover. VST3/CLAP plugins are indexed for library management only."
        )
        pref_page.add(self.grp_plugins_list)

        # Drag-and-drop install target over the plugins list
        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect("drop", self._on_plugin_drop)
        self.grp_plugins_list.add_controller(drop_target)

        scroll.set_child(pref_page)
        self.append(scroll)

        # Populate initial plugins from scanner cache
        self._refresh_plugins_display()

    def _on_open_plugin_folder(self, _btn):
        vst_dir = os.path.expanduser("~/.vst3")
        os.makedirs(vst_dir, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", vst_dir])
        except Exception as e:
            log.warning(f"Failed to open {vst_dir}: {e}")

    def _on_install_effect_clicked(self, _btn):
        dialog = Gtk.FileChooserNative.new(
            "Select a VST3 or LV2 plugin bundle folder",
            self.get_root() if hasattr(self, "get_root") else None,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "_Install",
            "_Cancel"
        )

        def _on_response(dlg, response_id):
            if response_id == Gtk.ResponseType.ACCEPT:
                folder = dlg.get_file()
                if folder:
                    self._install_plugin_from_path(folder.get_path())
            dlg.destroy()

        dialog.connect("response", _on_response)
        dialog.show()

    def _on_plugin_drop(self, drop_target, value, x, y):
        try:
            files = value.get_files()
        except Exception:
            return False
        for gfile in files:
            path = gfile.get_path()
            if path:
                self._install_plugin_from_path(path, silent=True)
        return True

    def _install_plugin_from_path(self, path: str, silent: bool = False) -> None:
        """Validates and installs a plugin bundle off the GTK main thread (bundle
        validation walks the folder tree, which could otherwise stutter the UI
        on large or deeply-nested bundles), then dispatches the result back."""
        def _bg():
            success, message = plugin_scanner.install_plugin_from_path(path)

            def _finish():
                self.vst_scan_row.set_subtitle(message)
                if success:
                    self._on_install_effect_rescan()
                return False

            GLib.idle_add(_finish)

        threading.Thread(target=_bg, daemon=True, name="WaveController-PluginInstall").start()

    def _on_install_effect_rescan(self):
        if plugin_scanner.is_scanning:
            return
        plugin_scanner.scan_async(self._on_scan_finished)

    def _on_remove_plugin_clicked(self, _btn, plugin_id: str):
        success, message = plugin_scanner.remove_installed_plugin(plugin_id)
        self.vst_scan_row.set_subtitle(message)
        if success:
            self._on_install_effect_rescan()

    def _on_external_plugin_enabled_toggled(self, row, _gparam, plugin_id: str):
        states = dict(config_manager.get("external_plugin_enabled", {}))
        states[plugin_id] = row.get_active()
        config_manager.set("external_plugin_enabled", states, immediate=True)
        if self.pipewire_mgr:
            self.pipewire_mgr.reload_channel_fx()
        if self.on_global_fx_changed_callback:
            GLib.idle_add(self.on_global_fx_changed_callback)

    def _on_add_custom_path_clicked(self, _btn):
        dialog = Gtk.FileChooserNative.new(
            "Select an additional plugin scan directory",
            self.get_root() if hasattr(self, "get_root") else None,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "_Add",
            "_Cancel"
        )

        def _on_response(dlg, response_id):
            if response_id == Gtk.ResponseType.ACCEPT:
                folder = dlg.get_file()
                if folder and plugin_scanner.add_custom_scan_path(folder.get_path()):
                    self._refresh_custom_paths_display()
                    self._on_install_effect_rescan()
            dlg.destroy()

        dialog.connect("response", _on_response)
        dialog.show()

    def _on_remove_custom_path_clicked(self, _btn, path: str):
        plugin_scanner.remove_custom_scan_path(path)
        self._refresh_custom_paths_display()
        self._on_install_effect_rescan()

    def _refresh_custom_paths_display(self):
        for row in self._custom_path_rows:
            self.grp_custom_paths.remove(row)
        self._custom_path_rows.clear()

        for path in config_manager.get("custom_plugin_paths", []):
            row = Adw.ActionRow(title=path)
            remove_btn = Gtk.Button(icon_name="user-trash-symbolic")
            remove_btn.add_css_class("flat")
            remove_btn.set_tooltip_text("Remove this scan path")
            remove_btn.connect("clicked", self._on_remove_custom_path_clicked, path)
            row.add_suffix(remove_btn)
            self.grp_custom_paths.add(row)
            self._custom_path_rows.append(row)

    def _on_scan_clicked(self, _btn):
        if plugin_scanner.is_scanning:
            return

        self.scan_btn.set_sensitive(False)
        self.scan_spinner.set_visible(True)
        self.scan_spinner.set_spinning(True)
        self.vst_scan_row.set_subtitle("Scanning system directories for VST3 and LV2 plugins...")

        plugin_scanner.scan_async(self._on_scan_finished)

    def _on_scan_finished(self, plugins):
        self.scan_spinner.set_spinning(False)
        self.scan_spinner.set_visible(False)
        self.scan_btn.set_sensitive(True)

        vst_count = len([p for p in plugins if p.format == PluginFormat.VST3])
        lv2_count = len([p for p in plugins if p.format == PluginFormat.LV2])
        builtin_count = len([p for p in plugins if p.format == PluginFormat.BUILTIN])
        total = len(plugins)

        status_msg = f"Indexed {total} plugins ({vst_count} VST3, {lv2_count} LV2, {builtin_count} Built-in)"
        self.vst_scan_row.set_subtitle(status_msg)
        self._refresh_plugins_display()
        if self.on_global_fx_changed_callback:
            GLib.idle_add(self.on_global_fx_changed_callback)

    def _refresh_plugins_display(self):
        # Clear existing rows
        for row in self._plugin_rows:
            self.grp_plugins_list.remove(row)
        self._plugin_rows.clear()

        query = self.search_entry.get_text().strip().lower() if hasattr(self, "search_entry") else ""
        plugins = plugin_scanner.get_all_plugins()

        # Update scanner subtitle if not currently scanning
        if not plugin_scanner.is_scanning:
            vst_count = len([p for p in plugins if p.format == PluginFormat.VST3])
            lv2_count = len([p for p in plugins if p.format == PluginFormat.LV2])
            builtin_count = len([p for p in plugins if p.format == PluginFormat.BUILTIN])
            self.vst_scan_row.set_subtitle(
                f"Indexed {len(plugins)} plugins ({vst_count} VST3, {lv2_count} LV2, {builtin_count} Built-in)"
            )

        # Sort: VST3 first, then LV2, alphabetical (Built-ins are shown as toggles above,
        # not duplicated in this user-facing plugin list)
        def _sort_key(p: AudioPlugin):
            fmt_order = {PluginFormat.VST3: 0, PluginFormat.LV2: 1}
            return (fmt_order.get(p.format, 2), p.name.lower())

        def _is_internal(p: AudioPlugin) -> bool:
            if p.is_builtin:
                return True
            if p.format == PluginFormat.LADSPA and not p.is_user_installed:
                binary_name = os.path.basename(p.binary_path or p.path or "")
                if binary_name in _BUNDLED_INTERNAL_LADSPA_FILES:
                    return True
            return False

        visible_candidates = [p for p in plugins if not _is_internal(p)]
        hostable_keys = {_plugin_display_key(p) for p in visible_candidates if _is_hostable_lv2_plugin(p)}

        def _should_show(p: AudioPlugin) -> bool:
            if _is_hostable_lv2_plugin(p):
                return True
            if not p.is_user_installed:
                return False
            return _plugin_display_key(p) not in hostable_keys

        sorted_plugins = sorted([p for p in visible_candidates if _should_show(p)], key=_sort_key)

        for p in sorted_plugins:
            # Filter match
            search_str = f"{p.name} {p.vendor} {p.format.value} {p.category.value}".lower()
            if query and query not in search_str:
                continue

            sub = f"{p.category.value} • {p.vendor} • v{p.version}"
            if p.path:
                sub += f" ({os.path.basename(p.path)})"

            row = Adw.ActionRow(title=p.name, subtitle=sub)

            if _is_hostable_lv2_plugin(p):
                states = config_manager.get("external_plugin_enabled", {})
                if not isinstance(states, dict):
                    states = {}
                row = Adw.SwitchRow(title=p.name, subtitle=sub)
                row.set_active(states.get(p.id, True))
                row.connect("notify::active", self._on_external_plugin_enabled_toggled, p.id)

            # Badges box
            badge_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            badge_box.set_valign(Gtk.Align.CENTER)

            # Category pill
            cat_lbl = Gtk.Label(label=p.category.value)
            cat_lbl.add_css_class("caption")
            cat_lbl.add_css_class("dim-label")
            badge_box.append(cat_lbl)

            # Format pill
            fmt_lbl = Gtk.Label(label=p.format.value)
            fmt_lbl.add_css_class("caption")
            if p.format == PluginFormat.BUILTIN:
                fmt_lbl.add_css_class("accent")
            else:
                fmt_lbl.add_css_class("success")
            badge_box.append(fmt_lbl)

            row.add_suffix(badge_box)

            # Only plugins installed via the Install Effect flow are removable here;
            # built-ins and anything found in a system/user directory are left alone.
            if p.is_user_installed:
                remove_btn = Gtk.Button(icon_name="user-trash-symbolic")
                remove_btn.add_css_class("flat")
                remove_btn.set_valign(Gtk.Align.CENTER)
                remove_btn.set_tooltip_text(f"Remove '{p.name}' (installed via WaveController)")
                remove_btn.connect("clicked", self._on_remove_plugin_clicked, p.id)
                row.add_suffix(remove_btn)

            self.grp_plugins_list.add(row)
            self._plugin_rows.append(row)

    def _on_search_changed(self, _entry):
        self._refresh_plugins_display()
