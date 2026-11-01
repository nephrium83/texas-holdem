"""Compatibility source entry point for the Godot player.

Build the portable game with tools/build_windows.py, not the retired poker.spec.
"""
from holdem.desktop_launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
