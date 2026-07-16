"""Hand history logging and replay for Texas Hold'em.

HandRecord   – dataclass capturing one hand's full transcript.
HandLogger   – attaches to a Holdem GUI instance, builds records from the
               engine event stream, and persists them to JSONL.

Usage (wired in gui.py):
    logger = HandLogger()
    # on deal():
    logger.on_hand_start(engine.players, engine)
    # in flush_log():
    logger.feed(events)       # before draining
    # in showdown():
    logger.on_settle(result, engine)
"""
from __future__ import annotations

import json
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set


# ----------------------------------------------------------------- record

@dataclass
class HandRecord:
    hand_id: str
    timestamp: float
    seats: List[dict]           # [{name, stack_start}]
    actions: List[dict]         # [{seat, action_type, amount, street, text}]
    community: List[List[str]]  # cards per street: [[flop...],[turn],[river]]
    hole_cards: Dict[int, List[str]]   # seat_idx -> [card_str, ...]
    winners: Set[int]
    pot_final: int

    @property
    def ts_label(self) -> str:
        import datetime
        return datetime.datetime.fromtimestamp(self.timestamp).strftime(
            "%Y-%m-%d %H:%M")

    @property
    def winner_names(self) -> str:
        return ", ".join(
            s["name"] for i, s in enumerate(self.seats)
            if i in self.winners
            # seats list may be indexed differently from winner seat indices
        )


# ----------------------------------------------------------------- logger

class HandLogger:
    """Collects per-hand data from the engine event stream."""

    def __init__(self,
                 history_path: Optional[Path] = None,
                 maxlen: int = 50):
        self.records: deque[HandRecord] = deque(maxlen=maxlen)
        self._history_path: Path = (
            history_path
            or (Path.home() / ".texas_holdem_history.jsonl")
        )
        self._current: Optional[HandRecord] = None
        self._current_street: str = "preflop"
        self._name_to_seat: dict[str, int] = {}
        self._seat_to_name: dict[int, str] = {}
        self._community_this_street: List[str] = []
        self._flop_done = False
        self._turn_done = False
        self._river_done = False

    # -------------------------------------------------------------- API

    def on_hand_start(self, players, engine) -> None:
        """Call immediately after engine.start_hand() returns True."""
        seated = [p for p in players if p.in_seat]
        seats_info = []
        for p in seated:
            # stack_start = what they had before posting blinds this hand
            seats_info.append({
                "name": p.name,
                "stack_start": p.stack + p.bet + p.total_live + p.total_dead,
            })

        self._name_to_seat = {p.name: p.idx for p in players}
        self._seat_to_name = {p.idx: p.name for p in players}
        self._current = HandRecord(
            hand_id=str(uuid.uuid4()),
            timestamp=time.time(),
            seats=seats_info,
            actions=[],
            community=[],
            hole_cards={},
            winners=set(),
            pot_final=0,
        )
        self._current_street = "preflop"
        self._community_this_street = []
        self._flop_done = self._turn_done = self._river_done = False

    def feed(self, events) -> None:
        """Process a batch of (kind, text) events from engine.drain()."""
        if self._current is None:
            return
        for kind, text in events:
            self._process(kind, text)

    def on_settle(self, result, engine) -> None:
        """Call after engine.settle() to finalise and store the record."""
        if self._current is None:
            return
        rec = self._current

        # Flush any remaining community cards
        if self._community_this_street:
            rec.community.append(list(self._community_this_street))
            self._community_this_street = []

        # Hole cards at showdown
        for p in engine.players:
            if p.in_seat and not p.folded and p.hole:
                if p.idx in result.get("shown", set()):
                    rec.hole_cards[p.idx] = [str(c) for c in p.hole]

        rec.winners = set(result.get("winners", set()))
        rec.pot_final = sum(
            pt.get("amount", 0) for pt in result.get("pots", [])
        )

        self.records.append(rec)
        self._persist(rec)
        self._current = None

    def last_n(self, n: int = 10) -> List[HandRecord]:
        """Return up to *n* most-recent hands, newest first."""
        lst = list(self.records)
        return list(reversed(lst))[:n]

    # -------------------------------------------------------------- internals

    def _process(self, kind: str, text: str) -> None:
        rec = self._current
        if rec is None:
            return

        if kind == "street":
            # "=== FLOP  [2♦ 5♥ K♠] ===" / TURN / RIVER / showdown
            if "FLOP" in text:
                cards = self._parse_board_cards(text)
                rec.community.append(cards)
                self._current_street = "flop"
            elif "TURN" in text:
                cards = self._parse_board_cards(text)
                rec.community.append(cards)
                self._current_street = "turn"
            elif "RIVER" in text:
                cards = self._parse_board_cards(text)
                rec.community.append(cards)
                self._current_street = "river"
            elif "SHOWDOWN" in text or "showdown" in text.lower():
                self._current_street = "showdown"
            return

        if kind in ("fold", "check", "bet", "raise"):
            action_rec = self._parse_action(kind, text)
            if action_rec:
                rec.actions.append(action_rec)

    @staticmethod
    def _parse_board_cards(text: str) -> List[str]:
        """Extract card tokens between [ and ] in a street-header event."""
        m = re.search(r"\[(.+?)\]", text)
        if not m:
            return []
        return m.group(1).split()

    def _parse_action(self, kind: str, text: str) -> Optional[dict]:
        for name, seat_idx in self._name_to_seat.items():
            if not text.startswith(name + " "):
                continue
            rest = text[len(name) + 1:]
            street = self._current_street
            if kind == "fold":
                return dict(seat=seat_idx, action_type="fold",
                            amount=0, street=street, text=text)
            if kind == "check":
                return dict(seat=seat_idx, action_type="check",
                            amount=0, street=street, text=text)
            if kind == "bet":
                m = re.match(r"calls (\d+)", rest)
                amt = int(m.group(1)) if m else 0
                return dict(seat=seat_idx, action_type="call",
                            amount=amt, street=street, text=text)
            if kind == "raise":
                m = re.match(r"(bets|raises to|is all-in for) (\d+)", rest)
                if m:
                    amt = int(m.group(2))
                    atype = "bet" if m.group(1) == "bets" else "raise"
                else:
                    amt = 0
                    atype = "raise"
                return dict(seat=seat_idx, action_type=atype,
                            amount=amt, street=street, text=text)
        return None

    def _persist(self, rec: HandRecord) -> None:
        try:
            with open(self._history_path, "a", encoding="utf-8") as fh:
                obj = {
                    "hand_id": rec.hand_id,
                    "timestamp": rec.timestamp,
                    "seats": rec.seats,
                    "actions": rec.actions,
                    "community": rec.community,
                    "hole_cards": {
                        str(k): v for k, v in rec.hole_cards.items()
                    },
                    "winners": sorted(rec.winners),
                    "pot_final": rec.pot_final,
                }
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except Exception:
            pass


# ----------------------------------------------------------------- viewer

def open_history_viewer(parent, logger: HandLogger, theme: dict) -> None:
    """Open a Toplevel hand-history replay dialog.

    parent – tk widget that owns the window
    logger – HandLogger instance
    theme  – GUI theme dict (same keys as THEMES in gui.py)
    """
    import tkinter as tk
    from tkinter import ttk

    t = theme
    win = tk.Toplevel(parent)
    win.title("Hand History")
    win.configure(bg=t["bg"])
    win.transient(parent)
    win.resizable(True, True)
    win.minsize(680, 440)

    # Center on parent
    parent.update_idletasks()
    px, py = parent.winfo_rootx(), parent.winfo_rooty()
    pw, ph = parent.winfo_width(), parent.winfo_height()
    win.geometry(f"760x520+{px + pw//2 - 380}+{py + ph//2 - 260}")

    tk.Label(win, text="HAND HISTORY", bg=t["bg"], fg=t["accent"],
             font=("Segoe UI", 13, "bold")).pack(pady=(12, 6))

    body = tk.Frame(win, bg=t["bg"])
    body.pack(fill="both", expand=True, padx=12, pady=(0, 8))
    body.columnconfigure(0, minsize=220)
    body.columnconfigure(1, weight=1)
    body.rowconfigure(0, weight=1)

    # ---- Left: hand list -----------------------------------------------
    lf = tk.Frame(body, bg=t["panel"], width=220)
    lf.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
    lf.pack_propagate(False)

    tk.Label(lf, text="Last hands", bg=t["panel"], fg=t["dim"],
             font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=8, pady=(8, 2))

    lb_frame = tk.Frame(lf, bg=t["panel"])
    lb_frame.pack(fill="both", expand=True, padx=4)
    lb_scroll = tk.Scrollbar(lb_frame, width=8)
    lb_scroll.pack(side="right", fill="y")
    lb = tk.Listbox(lb_frame, bg=t["bg"], fg=t["text"],
                    selectbackground=t["active"], selectforeground=t["bg"],
                    font=("Consolas", 8), relief="flat",
                    yscrollcommand=lb_scroll.set, activestyle="none",
                    borderwidth=0)
    lb.pack(side="left", fill="both", expand=True)
    lb_scroll.config(command=lb.yview)

    hands = logger.last_n(10)
    for rec in hands:
        name_part = ", ".join(
            s["name"] for i, s in enumerate(rec.seats)
            if i in rec.winners
        )
        # Build winner display using seat index -> name mapping
        winner_names = []
        seat_by_idx = {i: s["name"] for i, s in enumerate(rec.seats)}
        # rec.winners has engine seat indices; match by seat name via seats_info
        all_names = {s["name"] for s in rec.seats}
        for w_idx in sorted(rec.winners):
            if w_idx < len(rec.seats):
                winner_names.append(rec.seats[w_idx]["name"])
        label = (f"{rec.ts_label}  pot {rec.pot_final}\n"
                 f"  ⇒ {', '.join(winner_names) or '?'}")
        lb.insert("end", label)

    # ---- Right: action detail ------------------------------------------
    rf = tk.Frame(body, bg=t["panel"])
    rf.grid(row=0, column=1, sticky="nsew")

    nav = tk.Frame(rf, bg=t["panel"])
    nav.pack(fill="x", padx=8, pady=(8, 4))

    # step index within the selected hand's "lines" list
    _state = {
        "hand_idx": None,
        "lines": [],        # flat list of (label, text) for action log
        "step": 0,
        "showing_all": False,
    }

    log_frame = tk.Frame(rf, bg=t["panel"])
    log_frame.pack(fill="both", expand=True, padx=8)
    log_scroll = tk.Scrollbar(log_frame, width=8)
    log_scroll.pack(side="right", fill="y")
    log_txt = tk.Text(log_frame, bg=t["bg"], fg=t["text"],
                      font=("Consolas", 9), relief="flat", wrap="word",
                      state="disabled", yscrollcommand=log_scroll.set)
    log_txt.pack(side="left", fill="both", expand=True)
    log_scroll.config(command=log_txt.yview)

    # tags
    log_txt.tag_config("street", foreground=t["gold"],
                       font=("Consolas", 9, "bold"))
    log_txt.tag_config("fold",   foreground=t["dim"])
    log_txt.tag_config("check",  foreground=t["dim"])
    log_txt.tag_config("call",   foreground=t["text"])
    log_txt.tag_config("raise",  foreground=t["loss"])
    log_txt.tag_config("bet",    foreground=t["loss"])
    log_txt.tag_config("info",   foreground=t["accent"])
    log_txt.tag_config("hole",   foreground=t["gold"])

    def _clear_log():
        log_txt.config(state="normal")
        log_txt.delete("1.0", "end")
        log_txt.config(state="disabled")

    def _append(text, tag="call"):
        log_txt.config(state="normal")
        log_txt.insert("end", text + "\n", tag)
        log_txt.see("end")
        log_txt.config(state="disabled")

    def _build_lines(rec: HandRecord):
        """Flatten a HandRecord into a list of (tag, text) display lines."""
        lines = []
        lines.append(("info",
                      f"Hand  {rec.ts_label}  ·  pot {rec.pot_final}"))
        lines.append(("street", "── Pre-flop ──"))
        cur_street = "preflop"
        for act in rec.actions:
            st = act.get("street", "preflop")
            if st != cur_street:
                street_label = st.capitalize()
                if st == "flop" and rec.community:
                    board = " ".join(rec.community[0]) if rec.community else ""
                    lines.append(("street", f"── Flop  [{board}] ──"))
                elif st == "turn" and len(rec.community) >= 2:
                    card = " ".join(rec.community[1])
                    lines.append(("street", f"── Turn  [{card}] ──"))
                elif st == "river" and len(rec.community) >= 3:
                    card = " ".join(rec.community[2])
                    lines.append(("street", f"── River [{card}] ──"))
                elif st == "showdown":
                    lines.append(("street", "── Showdown ──"))
                else:
                    lines.append(("street", f"── {street_label} ──"))
                cur_street = st
            tag = act.get("action_type", "call")
            if tag not in ("fold", "check", "call", "bet", "raise"):
                tag = "call"
            lines.append((tag, "  " + act.get("text", "")))

        # showdown hole cards
        if rec.hole_cards:
            lines.append(("street", "── Showdown ──"))
            for seat_idx, cards in sorted(rec.hole_cards.items()):
                seat_name = (rec.seats[seat_idx]["name"]
                             if seat_idx < len(rec.seats) else f"S{seat_idx}")
                lines.append(("hole",
                              f"  {seat_name}: {' '.join(cards)}"))
        # winners
        winner_names = []
        for w_idx in sorted(rec.winners):
            if w_idx < len(rec.seats):
                winner_names.append(rec.seats[w_idx]["name"])
        if winner_names:
            lines.append(("info",
                          f"Winner: {', '.join(winner_names)}  +{rec.pot_final}"))
        return lines

    def _show_up_to_step(step):
        _clear_log()
        lines = _state["lines"]
        limit = min(step + 1, len(lines)) if not _state["showing_all"] else len(lines)
        for i in range(limit):
            tag, text = lines[i]
            _append(text, tag)
        # update nav label
        total = len(lines)
        shown = limit
        l_step.config(text=f"{shown}/{total}")
        b_prev.config(state="normal" if step > 0 and not _state["showing_all"] else "disabled")
        b_next.config(state="normal" if limit < total and not _state["showing_all"] else "disabled")

    def _on_select(evt=None):
        sel = lb.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(hands):
            return
        rec = hands[idx]
        _state["hand_idx"] = idx
        _state["lines"] = _build_lines(rec)
        _state["step"] = 0
        _state["showing_all"] = False
        _show_up_to_step(0)

    def _prev():
        if _state["step"] > 0:
            _state["step"] -= 1
            _show_up_to_step(_state["step"])

    def _next():
        lines = _state["lines"]
        if _state["step"] < len(lines) - 1:
            _state["step"] += 1
            _show_up_to_step(_state["step"])

    def _show_all():
        _state["showing_all"] = True
        _show_up_to_step(len(_state["lines"]))

    # nav widgets
    b_prev = tk.Button(nav, text="◀ Prev", command=_prev, relief="flat",
                       bg=t["btn"], fg=t["btn_text"], font=("Segoe UI", 8),
                       cursor="hand2", padx=6, state="disabled")
    b_prev.pack(side="left", padx=(0, 4))
    b_next = tk.Button(nav, text="Next ▶", command=_next, relief="flat",
                       bg=t["btn"], fg=t["btn_text"], font=("Segoe UI", 8),
                       cursor="hand2", padx=6, state="disabled")
    b_next.pack(side="left", padx=(0, 4))
    tk.Button(nav, text="Show all", command=_show_all, relief="flat",
              bg=t["btn"], fg=t["btn_text"], font=("Segoe UI", 8),
              cursor="hand2", padx=6).pack(side="left")
    l_step = tk.Label(nav, text="", bg=t["panel"], fg=t["dim"],
                      font=("Segoe UI", 8))
    l_step.pack(side="left", padx=8)

    # bottom bar
    bb = tk.Frame(win, bg=t["bg"])
    bb.pack(fill="x", padx=12, pady=(0, 10))
    tk.Button(bb, text="Close", command=win.destroy, relief="flat",
              bg=t["btn"], fg=t["btn_text"], font=("Segoe UI", 9),
              cursor="hand2", padx=12, pady=4).pack(side="right")
    if not hands:
        _append("No hands recorded yet.", "info")

    lb.bind("<<ListboxSelect>>", _on_select)
    if hands:
        lb.selection_set(0)
        _on_select()
    try:
        win.grab_set()
    except Exception:
        pass
