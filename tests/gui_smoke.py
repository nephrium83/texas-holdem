"""Headless GUI smoke test.

Boots the real Tkinter app, lets the AI play every seat at instant speed,
and fails on any Tk callback exception or chip-count drift. Requires a
display; in CI it runs under `xvfb-run`.
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk

from holdem import gui

HANDS = 15
PLAYERS = 6
STACK = 400


def main():
    root = tk.Tk()
    root.geometry("1400x900")
    errors = []
    root.report_callback_exception = lambda e, v, t: errors.append(
        "".join(traceback.format_exception(e, v, t)))

    app = gui.Holdem(root)
    app.v_players.set(PLAYERS)
    app.v_stack.set(STACK)
    app.v_speed.set("Instant")
    app.v_observe.set(True)
    app.v_auto.set(True)
    app.v_mode.set("Tournament")
    app.new_game()

    def watch():
        if errors or app.game_over or app.engine.hand_no >= HANDS:
            root.quit()
            return
        root.after(20, watch)

    root.after(150, watch)
    root.mainloop()

    total = sum(p.stack + p.total for p in app.engine.players)
    bank = PLAYERS * STACK
    print(f"hands={app.engine.hand_no} chips={total}/{bank}")
    if errors:
        print(errors[0])
        return 1
    if total != bank:
        print("chip-count drift")
        return 1
    print("gui smoke: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
