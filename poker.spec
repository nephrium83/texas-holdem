"""Retired ambiguous build target; keep old commands from shipping Tkinter."""
raise SystemExit(
    "poker.spec is retired. Build the Godot player with tools/build_windows.py "
    "as described in docs/BUILD_WINDOWS.md. The explicit development-only Tkinter "
    "target is packaging/legacy_tk.spec."
)
