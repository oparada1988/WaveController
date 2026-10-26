import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

SHORTCUTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<interface>
  <object class="GtkShortcutsWindow" id="shortcuts_window">
    <property name="modal">True</property>
    <child>
      <object class="GtkShortcutsSection">
        <property name="visible">True</property>
        <property name="section-name">shortcuts</property>
        <property name="max-height">12</property>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="visible">True</property>
            <property name="title">Navigation &amp; Views</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="visible">True</property>
                <property name="accelerator">&lt;Control&gt;m</property>
                <property name="title">Toggle Compact Mode</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="visible">True</property>
                <property name="accelerator">Escape</property>
                <property name="title">Return to Full Mode</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="visible">True</property>
                <property name="accelerator">&lt;Control&gt;b</property>
                <property name="title">Toggle Sidebar Collapse</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="visible">True</property>
                <property name="accelerator">&lt;Control&gt;1</property>
                <property name="title">Switch to Mixes View</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="visible">True</property>
                <property name="accelerator">&lt;Control&gt;2</property>
                <property name="title">Switch to Effects Manager</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="visible">True</property>
                <property name="accelerator">&lt;Control&gt;3</property>
                <property name="title">Switch to Audio Flow View</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="visible">True</property>
                <property name="accelerator">&lt;Control&gt;4</property>
                <property name="title">Switch to Settings</property>
              </object>
            </child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="visible">True</property>
            <property name="title">Mixer Controls</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="visible">True</property>
                <property name="title">Reset Fader to 100% (Unity Gain)</property>
                <property name="subtitle">Double-click volume fader</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="visible">True</property>
                <property name="title">Adjust Volume (±2% / ±5%)</property>
                <property name="subtitle">Mouse scroll wheel (Shift for ±5%)</property>
              </object>
            </child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="visible">True</property>
            <property name="title">Application</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="visible">True</property>
                <property name="accelerator">F1</property>
                <property name="title">Show Keyboard Shortcuts</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="visible">True</property>
                <property name="accelerator">&lt;Control&gt;q</property>
                <property name="title">Close / Minimize to System Tray</property>
              </object>
            </child>
          </object>
        </child>
      </object>
    </child>
  </object>
</interface>
"""

class ShortcutsManager:
    """
    Manages creation and presentation of the native GTK4 GtkShortcutsWindow.
    Constructed via GtkBuilder to protect internal composite template structures (search bar).
    Intercepts close-request to toggle visibility rather than destroying internal C widgets.
    """
    def __init__(self, parent_window):
        self._parent = parent_window
        self._window = None

    def show(self, transient_parent=None):
        parent = transient_parent or self._parent
        if self._window is None:
            builder = Gtk.Builder.new_from_string(SHORTCUTS_XML, -1)
            self._window = builder.get_object("shortcuts_window")
            if parent:
                self._window.set_transient_for(parent)
            self._window.connect("close-request", self._on_close_request)
        else:
            if parent:
                self._window.set_transient_for(parent)
        self._window.present()

    def _on_close_request(self, win):
        win.set_visible(False)
        return True

    def destroy(self):
        if self._window:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None
