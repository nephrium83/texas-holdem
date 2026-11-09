# Godot tavern embedding example

Open this Godot 4 project and press F6 on `tavern.tscn` or F5 to run the
project. Select **Play a demonstration hand**. Ranger and Innkeeper check
or call through showdown. The scene displays their play-chip balances after
settlement and confirms that the table closed. Each click starts a new visit.

Install the poker package in a Python 3.10+ environment first. From the
repository root, run `python -m pip install --no-deps .`. Set `POKER_PYTHON`
to that interpreter's absolute path if Godot cannot find it on PATH.
Godot defaults to `python` on Windows and `python3` elsewhere.

Keep the project directory beside `godot_tavern_hand.py` and `rpg_tavern.py`
when copying it to another location. The project can then run with the
installed package from outside the checkout.

The scene launches a short Python process for each visit. Python uses
`holdem.embedding.PokerTable` through the existing tavern adapter, settles a
play-chip receipt in memory, checks that a retry cannot pay twice, and exits.
Godot waits for that process before displaying the result. This synchronous
example demonstrates a complete local visit with automated turns.

Run the same scene and handler without opening a window:

```powershell
godot --headless --path examples/godot_tavern -- --self-test
```

A successful run prints the close receipt and balances totaling 2,000 chips
and exits with code zero.
