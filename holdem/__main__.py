"""Godot player entry point: python -m holdem (see --help for source assets)."""
from .desktop_launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
