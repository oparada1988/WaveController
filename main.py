#!/usr/bin/env python3
import sys
import os

# Ensure package path is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk

GLib.set_prgname("com.oparada.WaveController")
GLib.set_application_name("WaveController")
Gtk.Window.set_default_icon_name("com.oparada.WaveController")

from wavecontroller.utils.logger import setup_logging, get_logger
from wavecontroller.app import WaveControllerApp

def main():
    setup_logging()
    logger = get_logger("Main")
    logger.info("Starting WaveController Application...")
    app = WaveControllerApp()
    return app.run(sys.argv)

if __name__ == "__main__":
    sys.exit(main())
