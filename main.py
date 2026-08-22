#!/usr/bin/env python3
import sys
import os

# Ensure package path is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wavecontroller.app import WaveControllerApp

def main():
    app = WaveControllerApp()
    return app.run(sys.argv)

if __name__ == "__main__":
    sys.exit(main())
