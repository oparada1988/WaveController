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
    
    is_daemon = False
    clean_argv = [sys.argv[0]]
    for arg in sys.argv[1:]:
        if arg in ("--daemon", "--service", "-d"):
            is_daemon = True
        else:
            clean_argv.append(arg)

    if is_daemon:
        logger.info("Starting WaveController Background Daemon...")
    else:
        logger.info("Starting WaveController Application...")

    app = WaveControllerApp(is_daemon=is_daemon)
    return app.run(clean_argv)

if __name__ == "__main__":
    sys.exit(main())
