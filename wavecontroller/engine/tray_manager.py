"""
Native GDBus StatusNotifierItem (SNI) & DBusMenu Tray Manager for WaveController.
Implements org.kde.StatusNotifierItem and com.canonical.dbusmenu for Linux system trays.
"""

import os
import logging
from typing import Callable, Optional
import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

logger = logging.getLogger("WaveController.TrayManager")

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="u" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <method name="ContextMenu">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="Activate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="Scroll">
      <arg type="i" name="delta" direction="in"/>
      <arg type="s" name="orientation" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewStatus">
      <arg type="s" name="status"/>
    </signal>
  </interface>
</node>
"""

DBUS_MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="Status" type="s" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
  </interface>
</node>
"""

class TrayManager:
    """
    Manages the desktop system tray icon and status notifier DBus service.
    """
    def __init__(
        self,
        on_activate: Optional[Callable[[], None]] = None,
        on_open_settings: Optional[Callable[[], None]] = None,
        on_toggle_mic_mute: Optional[Callable[[], None]] = None,
        on_toggle_all_mute: Optional[Callable[[], None]] = None,
        get_mic_muted: Optional[Callable[[], bool]] = None,
        get_all_muted: Optional[Callable[[], bool]] = None,
        on_quit: Optional[Callable[[], None]] = None
    ):
        self.on_activate = on_activate
        self.on_open_settings = on_open_settings
        self.on_toggle_mic_mute = on_toggle_mic_mute
        self.on_toggle_all_mute = on_toggle_all_mute
        self.get_mic_muted = get_mic_muted
        self.get_all_muted = get_all_muted
        self.on_quit = on_quit

        self.bus: Optional[Gio.DBusConnection] = None
        self.sni_reg_id: int = 0
        self.menu_reg_id: int = 0
        self.revision: int = 1

        self.icons_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "assets", "icons")
        )

        self._sni_interface = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        self._menu_interface = Gio.DBusNodeInfo.new_for_xml(DBUS_MENU_XML).interfaces[0]

    def start(self):
        """Connect to session DBus and export the StatusNotifierItem and DBusMenu interfaces."""
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            if not self.bus:
                logger.warning("Could not connect to session DBus for tray icon.")
                return

            self.sni_reg_id = self.bus.register_object(
                "/StatusNotifierItem",
                self._sni_interface,
                self._handle_sni_method,
                self._handle_sni_get_prop,
                None
            )

            self.menu_reg_id = self.bus.register_object(
                "/MenuBar",
                self._menu_interface,
                self._handle_menu_method,
                self._handle_menu_get_prop,
                None
            )

            # Register with StatusNotifierWatcher
            self._register_with_watcher()
            logger.info("WaveController TrayManager active on DBus.")
        except Exception as e:
            logger.error(f"Failed to start TrayManager: {e}")

    def _register_with_watcher(self):
        if not self.bus:
            return
        try:
            self.bus.call_sync(
                "org.kde.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher",
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", ("/StatusNotifierItem",)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None
            )
            logger.info("Registered StatusNotifierItem with org.kde.StatusNotifierWatcher")
        except Exception as e:
            logger.warning(f"Could not register with StatusNotifierWatcher: {e}")

    def stop(self):
        """Unregister DBus objects and clean up."""
        if self.bus:
            if self.sni_reg_id:
                try:
                    self.bus.unregister_object(self.sni_reg_id)
                except Exception:
                    pass
                self.sni_reg_id = 0
            if self.menu_reg_id:
                try:
                    self.bus.unregister_object(self.menu_reg_id)
                except Exception:
                    pass
                self.menu_reg_id = 0

    # -------------------------------------------------------------
    # StatusNotifierItem Property & Method Handlers
    # -------------------------------------------------------------
    def _handle_sni_get_prop(self, connection, sender, path, iface, prop):
        props = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "com.oparada.WaveController"),
            "Title": GLib.Variant("s", "WaveController"),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("u", 0),
            "IconName": GLib.Variant("s", "wavecontroller-tray-symbolic"),
            "IconThemePath": GLib.Variant("s", self.icons_dir),
            "Menu": GLib.Variant("o", "/MenuBar"),
            "ItemIsMenu": GLib.Variant("b", False),
        }
        return props.get(prop, None)

    def _handle_sni_method(self, connection, sender, path, iface, method, params, invocation):
        if method == "Activate":
            if self.on_activate:
                GLib.idle_add(self.on_activate)
            invocation.return_value(None)
        elif method in ("ContextMenu", "SecondaryActivate"):
            if self.on_activate:
                GLib.idle_add(self.on_activate)
            invocation.return_value(None)
        elif method == "Scroll":
            invocation.return_value(None)
        else:
            invocation.return_value(None)

    # -------------------------------------------------------------
    # DBusMenu Property & Method Handlers
    # -------------------------------------------------------------
    def _handle_menu_get_prop(self, connection, sender, path, iface, prop):
        if prop == "Version":
            return GLib.Variant("u", 3)
        if prop == "Status":
            return GLib.Variant("s", "normal")
        return None

    def _build_menu_items(self) -> list:
        mic_muted = self.get_mic_muted() if self.get_mic_muted else False
        all_muted = self.get_all_muted() if self.get_all_muted else False

        items = [
            {
                "id": 1,
                "props": {
                    "label": GLib.Variant("s", "Open WaveController"),
                    "icon-name": GLib.Variant("s", "com.oparada.WaveController"),
                    "enabled": GLib.Variant("b", True),
                    "visible": GLib.Variant("b", True)
                }
            },
            {
                "id": 2,
                "props": {
                    "type": GLib.Variant("s", "separator"),
                    "enabled": GLib.Variant("b", True),
                    "visible": GLib.Variant("b", True)
                }
            },
            {
                "id": 3,
                "props": {
                    "label": GLib.Variant("s", "Mute Microphone" if not mic_muted else "Unmute Microphone"),
                    "toggle-type": GLib.Variant("s", "checkmark"),
                    "toggle-state": GLib.Variant("i", 1 if mic_muted else 0),
                    "icon-name": GLib.Variant("s", "audio-input-microphone-symbolic"),
                    "enabled": GLib.Variant("b", True),
                    "visible": GLib.Variant("b", True)
                }
            },
            {
                "id": 4,
                "props": {
                    "label": GLib.Variant("s", "Mute All Channels" if not all_muted else "Unmute All Channels"),
                    "toggle-type": GLib.Variant("s", "checkmark"),
                    "toggle-state": GLib.Variant("i", 1 if all_muted else 0),
                    "icon-name": GLib.Variant("s", "audio-volume-muted-symbolic"),
                    "enabled": GLib.Variant("b", True),
                    "visible": GLib.Variant("b", True)
                }
            },
            {
                "id": 5,
                "props": {
                    "type": GLib.Variant("s", "separator"),
                    "enabled": GLib.Variant("b", True),
                    "visible": GLib.Variant("b", True)
                }
            },
            {
                "id": 6,
                "props": {
                    "label": GLib.Variant("s", "Settings..."),
                    "icon-name": GLib.Variant("s", "emblem-system-symbolic"),
                    "enabled": GLib.Variant("b", True),
                    "visible": GLib.Variant("b", True)
                }
            },
            {
                "id": 7,
                "props": {
                    "type": GLib.Variant("s", "separator"),
                    "enabled": GLib.Variant("b", True),
                    "visible": GLib.Variant("b", True)
                }
            },
            {
                "id": 8,
                "props": {
                    "label": GLib.Variant("s", "Quit WaveController"),
                    "icon-name": GLib.Variant("s", "application-exit-symbolic"),
                    "enabled": GLib.Variant("b", True),
                    "visible": GLib.Variant("b", True)
                }
            }
        ]
        return items

    def _handle_menu_method(self, connection, sender, path, iface, method, params, invocation):
        if method == "GetLayout":
            root_props = {"children-display": GLib.Variant("s", "submenu")}
            raw_items = self._build_menu_items()
            
            children_variants = []
            for item in raw_items:
                v_item = GLib.Variant("(ia{sv}av)", (item["id"], item["props"], []))
                children_variants.append(v_item)

            layout = (0, root_props, children_variants)
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, layout)))

        elif method == "GetGroupProperties":
            ids = params[0]
            raw_items = {item["id"]: item["props"] for item in self._build_menu_items()}
            
            result = []
            for item_id in ids:
                if item_id == 0:
                    result.append((0, {"children-display": GLib.Variant("s", "submenu")}))
                elif item_id in raw_items:
                    result.append((item_id, raw_items[item_id]))

            invocation.return_value(GLib.Variant("(a(ia{sv}))", (result,)))

        elif method == "GetProperty":
            item_id = params[0]
            prop_name = params[1]
            raw_items = {item["id"]: item["props"] for item in self._build_menu_items()}
            val = raw_items.get(item_id, {}).get(prop_name, GLib.Variant("s", ""))
            invocation.return_value(GLib.Variant("(v)", (val,)))

        elif method == "AboutToShow":
            self.revision += 1
            invocation.return_value(GLib.Variant("(b)", (False,)))

        elif method == "Event":
            item_id = params[0]
            event_id = params[1]
            if event_id == "clicked":
                if item_id == 1:
                    if self.on_activate:
                        GLib.idle_add(self.on_activate)
                elif item_id == 3:
                    if self.on_toggle_mic_mute:
                        GLib.idle_add(self.on_toggle_mic_mute)
                elif item_id == 4:
                    if self.on_toggle_all_mute:
                        GLib.idle_add(self.on_toggle_all_mute)
                elif item_id == 6:
                    if self.on_open_settings:
                        GLib.idle_add(self.on_open_settings)
                elif item_id == 8:
                    if self.on_quit:
                        GLib.idle_add(self.on_quit)
            invocation.return_value(None)
        else:
            invocation.return_value(None)

    def notify_menu_updated(self):
        """Emit LayoutUpdated signal over DBus when menu state changes."""
        if not self.bus:
            return
        self.revision += 1
        try:
            self.bus.emit_signal(
                None,
                "/MenuBar",
                "com.canonical.dbusmenu",
                "LayoutUpdated",
                GLib.Variant("(ui)", (self.revision, 0))
            )
        except Exception:
            pass
