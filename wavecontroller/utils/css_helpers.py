import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk

_installed_palette_prefixes = set()


def install_palette_css(palette, css_prefix: str, radius_px: int, extra_css: str = ""):
    """Installs one solid-color CSS class per (hex, label) pair in palette, once per css_prefix."""
    if css_prefix in _installed_palette_prefixes:
        return
    css_rules = [
        f".{css_prefix}{hex_c.replace('#', '')} {{ background-color: {hex_c}; border-radius: {radius_px}px;{extra_css} }}"
        for hex_c, _ in palette
    ]
    full_css = "\n".join(css_rules)
    prov = Gtk.CssProvider()
    if hasattr(prov, "load_from_string"):
        prov.load_from_string(full_css)
    else:
        prov.load_from_data(full_css.encode())
    display = Gdk.Display.get_default()
    if display:
        Gtk.StyleContext.add_provider_for_display(display, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _installed_palette_prefixes.add(css_prefix)
