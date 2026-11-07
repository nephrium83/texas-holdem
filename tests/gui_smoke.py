"""Headless GUI smoke test.

Boots the real Tkinter app, lets the AI play every seat at instant speed,
and fails on any Tk callback exception or chip-count drift. Requires a
display; in CI it runs under `xvfb-run`.
"""
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk

from holdem import gui

HANDS = 15
PLAYERS = 6
STACK = 400
TOTAL_TIMEOUT = 120
PROGRESS_TIMEOUT = 20


def _completion_error(app, settled):
    engine = app.engine
    if engine is None:
        return "no engine was created"
    total = sum(player.stack + player.total for player in engine.players)
    if total != PLAYERS * STACK:
        return f"chip-count drift: {total}/{PLAYERS * STACK}"
    if len(settled) >= HANDS:
        return None
    winners = [player for player in engine.players if player.stack > 0]
    if (settled and app.game_over and app.hand_over and len(winners) == 1
            and winners[0].stack == PLAYERS * STACK
            and all(player.total == 0 for player in engine.players)):
        return None
    return f"insufficient completed hands: {len(settled)}/{HANDS}; game_over={app.game_over}"


def _play():
    root = tk.Tk()
    root.geometry("1400x900")
    errors = []
    root.report_callback_exception = lambda e, v, t: errors.append(
        "".join(traceback.format_exception(e, v, t)))

    app = gui.Holdem(root)
    app.hand_logger._history_path = Path(os.environ["HOLDEM_CONFIG_DIR"]) / "history.jsonl"
    app.v_players.set(PLAYERS)
    app.v_stack.set(STACK)
    app.v_speed.set("Instant")
    app.v_observe.set(True)
    app.v_auto.set(True)
    app.v_odds.set(False)
    app.v_mode.set("Tournament")
    app.new_game()

    settled = set()
    last_progress = [time.monotonic()]
    original_showdown = app.showdown
    def record_showdown(*args, **kwargs):
        original_showdown(*args, **kwargs)
        if app.hand_over and app.result is not None:
            settled.add(app.engine.hand_no)
            last_progress[0] = time.monotonic()
    app.showdown = record_showdown

    def watch():
        if time.monotonic() - last_progress[0] >= PROGRESS_TIMEOUT:
            errors.append(f"no hand completed within {PROGRESS_TIMEOUT}s")
        if errors or app.game_over or len(settled) >= HANDS:
            root.quit()
            return
        root.after(20, watch)

    root.after(150, watch)
    root.mainloop()

    try:
        print(f"started={app.engine.hand_no} completed={len(settled)}")
        if errors:
            print(errors[0])
            return 1
        error = _completion_error(app, settled)
        if error:
            print(error)
            return 1
        print("gui smoke: OK")
        return 0
    finally:
        root.destroy()


def main():
    # The parent deadline still works when Tk's event loop itself is blocked.
    # A generated child-only config keeps this standalone smoke off user data.
    with tempfile.TemporaryDirectory(prefix="holdem-gui-smoke-") as config:
        try:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                    env=dict(os.environ, HOLDEM_CONFIG_DIR=config),
                                    capture_output=True, text=True, timeout=TOTAL_TIMEOUT)
        except subprocess.TimeoutExpired:
            print(f"gui smoke: external deadline exceeded ({TOTAL_TIMEOUT}s)")
            return 1
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(_play() if sys.argv[1:] == ["--worker"] else main())
