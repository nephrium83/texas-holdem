import base64
import io
import math
import random
import threading
import datetime
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk, colorchooser

from .engine import (Engine, Player, Brain, equity, evaluate, hand_name,
                    AI_STYLES, SUIT_GLYPHS, RANK_STR, Card as _Card,
                    FULL_DECK as _FULL_DECK)

# ---------------------------------------------------------------------------
# Multiplayer card helpers
# ---------------------------------------------------------------------------
_SUIT_CHARS = "cdhs"                        # index = Card.s
_RANK_VALS  = {v: k for k, v in RANK_STR.items()}
_STUB_CARD  = _Card(2, 0)                   # placeholder for opponent backs


def _card_to_str(card: _Card) -> str:
    return RANK_STR[card.v] + _SUIT_CHARS[card.s]


def _str_to_card(s: str) -> _Card:
    if len(s) == 3:          # "10c"
        r, su = s[:2], s[2]
    else:
        r, su = s[0], s[1]
    return _Card(_RANK_VALS[r], _SUIT_CHARS.index(su))


from . import settings as cfg
from . import notes as _notes
from .onboarding import OnboardingFlow
from .hand_history import HandLogger, open_history_viewer
from .session_stats import SessionStats
import holdem.audio as _audio

try:
    from PIL import Image, ImageTk as _ImageTk
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

# Placeholder colors for AI seat avatars (one per seat index, cycling).
_AI_AVATAR_COLORS = [
    "#e33b6d", "#2a4a8c", "#226b45", "#8c2f39",
    "#ffd166", "#39e7ff", "#c62828", "#4b3a24", "#3f3f86",
]

CLOCK_BASE = 25          # seconds per action
BANK_START = 60          # starting time bank
BANK_TOPUP = 10          # added each time you post the BB
BANK_CAP = 120

# ----------------------------------------------------------------- themes

THEMES = {
    "Cyberpunk": dict(
        bg="#07070f", panel="#0c0c1a", felt="#12123a", felt_edge="#39e7ff",
        rail="#1d1d3d", text="#d6d6f0", dim="#6f6f92", accent="#00ffd0",
        gold="#ffd166", seat="#14142c", seat_hero="#0a2c46",
        seat_fold="#101018", active="#39e7ff", win="#00ffd0",
        card="#f4f5fa", card_edge="#8b8bc8", back="#2a2a5c", back2="#3f3f86",
        red="#e33b6d", black="#1c1c2c", chip="#ffd166", loss="#e33b6d",
        btn="#1b1b3a", btn_text="#e8e8ff",
    ),
    "Classic Felt": dict(
        bg="#14201a", panel="#1b2a22", felt="#226b45", felt_edge="#8c6239",
        rail="#4b3a24", text="#eef4ef", dim="#93ab9c", accent="#ffd166",
        gold="#ffd166", seat="#1a3327", seat_hero="#2a5a3c",
        seat_fold="#182018", active="#ffd166", win="#ffe08a",
        card="#fdfdf7", card_edge="#9a9a86", back="#8c2f39", back2="#b04652",
        red="#c62828", black="#1e1e28", chip="#ffd166", loss="#e07a5f",
        btn="#2a4636", btn_text="#f2f7f3",
    ),
}

BLIND_LEVELS = [(10, 20), (15, 30), (25, 50), (50, 100), (75, 150),
                (100, 200), (150, 300), (200, 400), (300, 600),
                (500, 1000), (800, 1600), (1200, 2400)]

SPEEDS = {"Slow": 950, "Normal": 550, "Fast": 260, "Instant": 60}

HERO = 0   # default seat for solo play; see Holdem.hero_seat property for MP


def rrect(cv, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle via a smoothed polygon."""
    r = min(r, abs(x2 - x1) / 2, abs(y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r,
           x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
           x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class Holdem:
    def __init__(self, root, mp_session=None, is_host=True, local_seat=0):
        self.root = root
        root.title("Texas Hold'em")
        root.minsize(1180, 760)

        # Multiplayer state
        self._mp_session    = mp_session
        self._mp_mode       = mp_session is not None
        self._mp_is_host    = is_host
        self._mp_local_seat = local_seat
        self._mp_remote_state: dict | None = None
        self._mp_hole_cards: list = []
        self._hand_num = 0
        # seat_idx -> peer connection-id; empty in solo mode
        self._mp_seat_to_peer: dict[int, str] = {}

        self.rng = random.Random()
        self.brain = Brain(self.rng)
        self.engine = None
        self.theme = THEMES["Cyberpunk"]   # replaced after config load below

        # settings
        self.v_players = tk.IntVar(value=6)
        self.v_stack = tk.IntVar(value=1000)
        self.v_sb = tk.IntVar(value=10)
        self.v_bb = tk.IntVar(value=20)
        self.v_mode = tk.StringVar(value="Cash")
        self.v_struct = tk.StringVar(value="No-Limit")
        self.v_level = tk.IntVar(value=2)
        self.v_chaos = tk.BooleanVar(value=True)
        self.v_reveal = tk.StringVar(value="Realistic (muck losers)")
        self.v_odds = tk.BooleanVar(value=True)
        self.v_hint = tk.BooleanVar(value=True)
        self.v_theme = tk.StringVar(value="Cyberpunk")
        self.v_speed = tk.StringVar(value="Normal")
        self.v_auto = tk.BooleanVar(value=False)
        self.v_observe = tk.BooleanVar(value=False)
        self.v_bet = tk.IntVar(value=0)
        self.v_ante = tk.BooleanVar(value=True)       # BB ante (tournament)
        self.v_lvlmin = tk.IntVar(value=8)            # minutes per level
        self.v_clock = tk.BooleanVar(value=True)      # action clock
        self.v_rit = tk.StringVar(value="Ask")        # run it twice
        self.v_straddles = tk.BooleanVar(value=False) # allow straddles (cash)
        self.v_rabbit = tk.BooleanVar(value=True)     # rabbit hunting
        self.v_topup = tk.BooleanVar(value=True)      # AI auto top-up (cash)
        self.v_training = tk.BooleanVar(value=True)   # table rule: aids ok
        self.v_fullscreen = tk.BooleanVar(value=True)  # CLIENT: start zoomed
        self.v_sounds_enabled = tk.BooleanVar(value=True)
        self.v_sound_volume = tk.IntVar(value=70)
        self.v_four_color_deck = tk.BooleanVar(value=False)
        self.v_felt_color = tk.StringVar(value="#35654d")
        self.v_bet_buttons = tk.StringVar(value="0.5,1,2,3")

        # option key -> tk var, scopes per holdem.settings.SPEC
        self._varmap = {
            "theme": self.v_theme, "speed": self.v_speed,
            "reveal": self.v_reveal, "hints": self.v_hint,
            "odds": self.v_odds, "auto_deal": self.v_auto,
            "clock_on": self.v_clock, "observe": self.v_observe,
            "ai_topup": self.v_topup, "ai_mixed": self.v_chaos,
            "ai_level": self.v_level,
            "fullscreen": self.v_fullscreen,
            "sounds_enabled": self.v_sounds_enabled,
            "sound_volume": self.v_sound_volume,
            "four_color_deck": self.v_four_color_deck,
            "felt_color": self.v_felt_color,
            "bet_buttons": self.v_bet_buttons,
            "mode": self.v_mode, "structure": self.v_struct,
            "sb": self.v_sb, "bb": self.v_bb, "stack": self.v_stack,
            "players": self.v_players, "bb_ante": self.v_ante,
            "level_minutes": self.v_lvlmin, "rit": self.v_rit,
            "straddles": self.v_straddles, "rabbit": self.v_rabbit,
            "training_aids": self.v_training,
        }
        stored = cfg.load()
        for key, val in {**stored["client"], **stored["last_table"]}.items():
            var = self._varmap.get(key)
            if var is not None:
                var.set(val)
        self.table_rules = cfg.table_rules(**stored["last_table"])
        self.joined_table = False     # True once seated at a live P2P table
        self.theme = THEMES.get(self.v_theme.get(), THEMES["Cyberpunk"])

        # audio
        _audio.set_enabled(self.v_sounds_enabled.get())
        _audio.set_volume(self.v_sound_volume.get() / 100)
        self._last_hero_eq = 0.0    # most recent win equity fraction for hero
        self._allin_announced: set = set()  # seats already given allin sound

        # daily bonus
        today = datetime.date.today().isoformat()
        if cfg.get('last_daily_bonus_date') != today:
            level = cfg.get('player_level')
            bonus = 500 * level
            cfg.set('bankroll', cfg.get('bankroll') + bonus)
            cfg.set('last_daily_bonus_date', today)
            self._pending_daily_bonus_msg = f'Daily bonus! +{bonus:,} chips'
        else:
            self._pending_daily_bonus_msg = ''

        # avatar – loaded from settings (written by onboarding); may be
        # overridden by main() after OnboardingFlow sets it.
        self.avatar_b64: str = stored["client"].get("avatar_b64", "")
        # PhotoImage refs for seat avatars, rebuilt on every redraw() call.
        self._seat_photo_refs: list = []

        # session state
        self.buyin = 0
        self.bank = float(BANK_START)
        self.clock_job = None
        self.clock_until = 0.0
        self.clock_phase = "off"        # off | base | bank
        self.level_started = 0.0
        self.straddle_armed = False
        self.rabbit_cards = None
        self.paused = False
        self.settings_win = None
        self.hand_logger = HandLogger()
        self.session_stats = SessionStats()
        # track whether VPIP/PFR already counted this hand for each seat
        self._vpip_counted: set[int] = set()
        self._pfr_counted: set[int] = set()

        # per-hand ui state
        self.result = None
        self.reveal = set()
        self.highlight = set()      # (v,s) of the winning five
        self.eq_gen = 0
        self.eq_text = "-"
        self.eq_bars = (0.0, 0.0, 1.0)
        self.hand_over = True
        self.game_over = False
        self.level_idx = 0
        self.pending = None
        self._cashout_offered = False
        self._tourn_tick_gen = 0
        self._tourn_overlay_ids = []

        self.wrap = tk.Frame(root)
        self.wrap.pack(fill="both", expand=True)
        self.setup = tk.Frame(self.wrap)
        self.table = tk.Frame(self.wrap)
        self._build_setup()
        self._build_table()

        if self._mp_mode:
            self._mp_setup_callbacks()
            self._mp_new_game()
        else:
            self._show(self.setup)

    # -------------------------------------------------------------- properties

    @property
    def hero_seat(self) -> int:
        """M-4: Local player's seat index.  In MP mode this is _mp_local_seat;
        in solo mode it is always HERO (0)."""
        return self._mp_local_seat if self._mp_mode else HERO

    # ------------------------------------------------------------- screens

    def _show(self, frame):
        for f in (self.setup, self.table):
            f.pack_forget()
        frame.pack(fill="both", expand=True)

    def _maybe_show_daily_bonus(self):
        msg = getattr(self, "_pending_daily_bonus_msg", "")
        if not msg:
            return
        self._pending_daily_bonus_msg = ''
        cv = self.cv
        W = max(cv.winfo_width(), 400)
        t = self.theme
        item = cv.create_text(W // 2, 40, text=msg,
                              fill=t["gold"], font=("Segoe UI", 12, "bold"),
                              tags="daily_bonus")
        def _fade(step=0):
            if step >= 30:
                try: cv.delete(item)
                except Exception: pass
                return
            self.root.after(100, lambda: _fade(step + 1))
        self.root.after(3000, _fade)

    def _build_setup(self):
        t = self.theme
        f = self.setup
        f.configure(bg=t["bg"])

        box = tk.Frame(f, bg=t["bg"])
        box.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(box, text="TEXAS HOLD'EM", bg=t["bg"], fg=t["accent"],
                 font=("Segoe UI", 26, "bold")).grid(
            row=0, column=0, columnspan=4, pady=(0, 4))
        tk.Label(box, text="six-max no-limit, opponents that actually play back",
                 bg=t["bg"], fg=t["dim"],
                 font=("Segoe UI", 10)).grid(row=1, column=0, columnspan=4,
                                             pady=(0, 22))

        self._sw = {}
        r = 2

        def row(label, widget, col=0):
            tk.Label(box, text=label, bg=t["bg"], fg=t["text"],
                     font=("Segoe UI", 10), anchor="e", width=17).grid(
                row=r, column=col, sticky="e", padx=(0, 8), pady=5)
            widget.grid(row=r, column=col + 1, sticky="w", pady=5, padx=(0, 26))

        def combo(var, values, w=16):
            c = ttk.Combobox(box, textvariable=var, values=values,
                             state="readonly", width=w)
            return c

        def spin(var, lo, hi, step=1, w=8):
            return tk.Spinbox(box, textvariable=var, from_=lo, to=hi,
                              increment=step, width=w, justify="center",
                              bg=t["panel"], fg=t["text"],
                              buttonbackground=t["btn"],
                              insertbackground=t["text"], relief="flat")

        row("Players", spin(self.v_players, 2, 9))
        row("Starting stack", spin(self.v_stack, 20, 100000, 50), 2)
        r += 1
        row("Small blind", spin(self.v_sb, 1, 5000))
        row("Big blind", spin(self.v_bb, 2, 10000), 2)
        r += 1
        row("Game", combo(self.v_mode, ["Cash", "Tournament"]))
        row("Betting", combo(self.v_struct,
                             ["No-Limit", "Pot-Limit", "Fixed-Limit"]), 2)
        r += 1
        row("AI skill (1-3)", spin(self.v_level, 1, 3))
        row("Show cards at", combo(self.v_reveal,
                                   ["Winner only",
                                    "Realistic (muck losers)",
                                    "Everyone"]), 2)
        r += 1
        row("Theme", combo(self.v_theme, list(THEMES)))
        row("Speed", combo(self.v_speed, list(SPEEDS)), 2)
        r += 1
        row("Run it twice", combo(self.v_rit, ["Ask", "Always", "Never"]))
        row("Level minutes", spin(self.v_lvlmin, 3, 30), 2)
        r += 1

        checks = tk.Frame(box, bg=t["bg"])
        checks.grid(row=r, column=0, columnspan=4, pady=(14, 4))
        opts = (("Mixed AI skill levels", self.v_chaos),
                ("Live equity readout", self.v_odds),
                ("Coaching hints", self.v_hint),
                ("Auto-deal next hand", self.v_auto),
                ("Observe mode (AI plays your seat)", self.v_observe),
                ("Action clock (25s + time bank)", self.v_clock),
                ("Big blind ante (tournament)", self.v_ante),
                ("Allow straddles (cash, big-bet)", self.v_straddles),
                ("Rabbit hunting", self.v_rabbit),
                ("AI auto top-up (cash)", self.v_topup))
        for k, (txt, var) in enumerate(opts):
            tk.Checkbutton(checks, text=txt, variable=var, bg=t["bg"],
                           fg=t["text"], selectcolor=t["panel"],
                           activebackground=t["bg"], activeforeground=t["text"],
                           font=("Segoe UI", 9)).grid(
                row=k % 5, column=k // 5, sticky="w", padx=(0, 18))
        r += 1

        b = tk.Button(box, text="DEAL ME IN", command=self.new_game,
                      bg=t["accent"], fg="#04040c", relief="flat",
                      font=("Segoe UI", 13, "bold"), width=20, pady=6,
                      activebackground=t["gold"], cursor="hand2")
        b.grid(row=r, column=0, columnspan=4, pady=(20, 0))
        self.b_deal = b
        self.b_resume = tk.Button(box, text="Resume current game",
                                  command=lambda: self._show(self.table),
                                  bg=t["btn"], fg=t["btn_text"],
                                  relief="flat", font=("Segoe UI", 9),
                                  cursor="hand2")
        self.b_resume.grid(row=r + 1, column=0, columnspan=4, pady=(10, 0))
        self.b_resume.grid_remove()

    def _build_table(self):
        t = self.theme
        f = self.table
        f.configure(bg=t["bg"])

        top = tk.Frame(f, bg=t["bg"], height=34)
        top.pack(fill="x", padx=10, pady=(8, 0))
        self.l_summary = tk.Label(top, text="", bg=t["bg"], fg=t["dim"],
                                  font=("Segoe UI", 9))
        self.l_summary.pack(side="left")
        tk.Button(top, text="Settings", command=self.open_settings,
                  bg=t["btn"], fg=t["btn_text"], relief="flat",
                  font=("Segoe UI", 9), cursor="hand2").pack(side="right")
        tk.Button(top, text="Preferences", command=self.open_preferences,
                  bg=t["btn"], fg=t["btn_text"], relief="flat",
                  font=("Segoe UI", 9), cursor="hand2").pack(
            side="right", padx=(0, 6))
        tk.Button(top, text="Player Notes", command=self.open_notes_viewer,
                  bg=t["btn"], fg=t["btn_text"], relief="flat",
                  font=("Segoe UI", 9), cursor="hand2").pack(
            side="right", padx=(0, 6))
        tk.Button(top, text="Last hand", command=self.open_history,
                  bg=t["btn"], fg=t["btn_text"], relief="flat",
                  font=("Segoe UI", 9), cursor="hand2").pack(
            side="right", padx=(0, 6))
        self.l_blinds = tk.Label(top, text="", bg=t["bg"], fg=t["gold"],
                                 font=("Segoe UI", 9, "bold"))
        self.l_blinds.pack(side="right", padx=14)

        mid = tk.Frame(f, bg=t["bg"])
        mid.pack(fill="both", expand=True, padx=10, pady=6)

        self.cv = tk.Canvas(mid, bg=t["bg"], highlightthickness=0)
        self.cv.pack(side="left", fill="both", expand=True)
        self.cv.bind("<Configure>", lambda e: self.redraw())

        side = tk.Frame(mid, bg=t["panel"], width=286)
        side.pack(side="right", fill="y", padx=(10, 0))
        side.pack_propagate(False)
        self._build_side(side)
        self._build_controls(f)

        for k in "fcbrn":
            self.root.bind(f"<Key-{k}>", self._hotkey)
            self.root.bind(f"<Key-{k.upper()}>", self._hotkey)
        self.root.bind("<space>", self._hotkey)
        self.root.bind("<Escape>", self._esc)

        # fire daily bonus toast after UI is ready
        self.root.after(800, self._maybe_show_daily_bonus)

    def _build_host_controls(self, f) -> None:
        """Build the host admin bar above the action buttons (mp host only)."""
        t = self.theme
        hbar = tk.Frame(f, bg=t["panel"], pady=3)
        hbar.pack(fill="x", side="bottom")
        self._host_bar = hbar

        tk.Label(hbar, text="HOST:", bg=t["panel"], fg=t["gold"],
                 font=("Segoe UI", 8, "bold")).pack(side="left", padx=(8, 6))

        def hbtn(txt, cmd, **kw):
            b = tk.Button(hbar, text=txt, command=cmd, relief="flat",
                          bg=t["btn"], fg=t["btn_text"],
                          font=("Segoe UI", 8), cursor="hand2", padx=6, **kw)
            b.pack(side="left", padx=2)
            return b

        self._btn_host_pause  = hbtn("⏸ Pause",        self._mp_host_pause)
        self._btn_host_resume = hbtn("▶ Resume",        self._mp_host_resume)
        self._btn_host_resume.config(state="disabled")
        hbtn("Kick player",    self._mp_host_kick_dialog)
        hbtn("Adjust blinds",  self._mp_host_adjust_blinds_dialog)

    # ---------------------------------------- Host admin actions

    def _mp_host_pause(self) -> None:
        """Host pauses the game and notifies all peers."""
        from .p2p import transport as _t
        _t.broadcast({"type": "pause", "payload": {"reason": "host paused"}})
        self._mp_game_paused = True
        self.paused = True
        self._btn_host_pause.config(state="disabled")
        self._btn_host_resume.config(state="normal")
        self.lock()
        self._mp_show_pause_overlay()

    def _mp_host_resume(self) -> None:
        """Host resumes the game and notifies all peers."""
        from .p2p import transport as _t
        _t.broadcast({"type": "resume", "payload": {}})
        self._mp_game_paused = False
        self.paused = False
        self._btn_host_pause.config(state="normal")
        self._btn_host_resume.config(state="disabled")
        self._mp_hide_pause_overlay()
        # Unlock controls if it's the host's turn
        e = self.engine
        if e is not None and e.actor == self._mp_local_seat:
            self._mp_unlock()

    def _mp_host_kick_dialog(self) -> None:
        """Host opens a dialog to select and kick a connected player."""
        sess = self._mp_session
        if sess is None:
            return
        t = self.theme

        win = tk.Toplevel(self.root)
        win.title("Kick Player")
        win.configure(bg=t["panel"])
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        self.root.update_idletasks()
        dw, dh = 320, 260
        rx = self.root.winfo_rootx() + self.root.winfo_width()  // 2 - dw // 2
        ry = self.root.winfo_rooty() + self.root.winfo_height() // 2 - dh // 2
        win.geometry(f"{dw}x{dh}+{max(0, rx)}+{max(0, ry)}")

        tk.Label(win, text="Kick Player", bg=t["panel"], fg=t["accent"],
                 font=("Segoe UI", 12, "bold")).pack(pady=(12, 8))

        with sess._lock:
            others = [(p.conn_id, p.nickname)
                      for p in sess.players.values() if not p.is_host]

        lb = tk.Listbox(win, bg=t["bg"], fg=t["text"], relief="flat",
                        selectmode="single", height=6,
                        font=("Segoe UI", 10), highlightthickness=0)
        lb.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        conn_map: dict[str, str] = {}
        for cid, nick in others:
            lb.insert("end", nick)
            conn_map[nick] = cid

        def _do_kick():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("No selection",
                                       "Please select a player to kick.",
                                       parent=win)
                return
            nick = lb.get(sel[0])
            cid  = conn_map.get(nick, "")
            if not cid:
                return
            from .p2p import transport as _t
            _t.broadcast({"type": "kick",
                           "payload": {"conn_id": cid, "nickname": nick}})
            _t.disconnect(cid)
            win.destroy()

        bbar = tk.Frame(win, bg=t["panel"])
        bbar.pack(fill="x", padx=16, pady=(0, 12))
        tk.Button(bbar, text="Cancel", command=win.destroy, relief="flat",
                  bg=t["btn"], fg=t["btn_text"],
                  font=("Segoe UI", 9), cursor="hand2").pack(side="left")
        tk.Button(bbar, text="Kick", command=_do_kick, relief="flat",
                  bg="#5c2333", fg="#ffe8ee",
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  padx=10).pack(side="right")

    def _mp_host_adjust_blinds_dialog(self) -> None:
        """Host opens a dialog to change SB/BB for the next hand."""
        t = self.theme
        e = self.engine
        cur_sb = e.sb if e else 10
        cur_bb = e.bb if e else 20

        win = tk.Toplevel(self.root)
        win.title("Adjust Blinds")
        win.configure(bg=t["panel"])
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        self.root.update_idletasks()
        dw, dh = 290, 210
        rx = self.root.winfo_rootx() + self.root.winfo_width()  // 2 - dw // 2
        ry = self.root.winfo_rooty() + self.root.winfo_height() // 2 - dh // 2
        win.geometry(f"{dw}x{dh}+{max(0, rx)}+{max(0, ry)}")

        tk.Label(win, text="Adjust Blinds (next hand)",
                 bg=t["panel"], fg=t["accent"],
                 font=("Segoe UI", 11, "bold")).pack(pady=(12, 10))

        body = tk.Frame(win, bg=t["panel"])
        body.pack(fill="x", padx=20)

        v_sb = tk.IntVar(value=cur_sb)
        v_bb = tk.IntVar(value=cur_bb)

        def _row(label, var):
            fr = tk.Frame(body, bg=t["panel"])
            fr.pack(fill="x", pady=4)
            tk.Label(fr, text=label, bg=t["panel"], fg=t["text"],
                     font=("Segoe UI", 9), width=14, anchor="e").pack(
                side="left")
            tk.Spinbox(fr, textvariable=var, from_=1, to=10000, width=8,
                       bg=t["bg"], fg=t["text"], insertbackground=t["text"],
                       relief="flat", justify="center").pack(
                side="left", padx=(6, 0))

        _row("Small blind:", v_sb)
        _row("Big blind:",   v_bb)

        def _confirm():
            sb = v_sb.get()
            bb = v_bb.get()
            if bb <= sb:
                messagebox.showwarning(
                    "Invalid blinds",
                    "Big blind must be greater than small blind.",
                    parent=win)
                return
            from .p2p import transport as _t
            _t.broadcast({"type": "adjust_blinds",
                           "payload": {"sb": sb, "bb": bb}})
            if self.engine:
                self.engine.sb = sb
                self.engine.bb = bb
            self.l_blinds.config(text=f"Blinds {sb}/{bb}")
            win.destroy()

        bbar = tk.Frame(win, bg=t["panel"])
        bbar.pack(fill="x", padx=20, pady=(8, 12))
        tk.Button(bbar, text="Cancel", command=win.destroy, relief="flat",
                  bg=t["btn"], fg=t["btn_text"],
                  font=("Segoe UI", 9), cursor="hand2").pack(side="left")
        tk.Button(bbar, text="Confirm", command=_confirm, relief="flat",
                  bg=t["accent"], fg="#04040c",
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  padx=10).pack(side="right")

    def _build_side(self, side):
        t = self.theme

        self.l_status = tk.Label(side, text="", bg=t["panel"], fg=t["accent"],
                                 font=("Segoe UI", 10, "bold"), wraplength=262,
                                 justify="left", anchor="w")
        self.l_status.pack(fill="x", padx=12, pady=(12, 2))

        self.l_hint = tk.Label(side, text="", bg=t["panel"], fg=t["gold"],
                               font=("Segoe UI", 9), wraplength=262,
                               justify="left", anchor="w")
        self.l_hint.pack(fill="x", padx=12, pady=(0, 8))

        tk.Label(side, text="EQUITY", bg=t["panel"], fg=t["dim"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(
            fill="x", padx=12)
        self.eq_cv = tk.Canvas(side, height=16, bg=t["panel"],
                               highlightthickness=0)
        self.eq_cv.pack(fill="x", padx=12, pady=(2, 2))
        self.l_eq = tk.Label(side, text="-", bg=t["panel"], fg=t["text"],
                             font=("Segoe UI", 9), wraplength=262,
                             justify="left", anchor="w")
        self.l_eq.pack(fill="x", padx=12, pady=(0, 10))

        tk.Label(side, text="TABLE LOG", bg=t["panel"], fg=t["dim"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(
            fill="x", padx=12)
        lf = tk.Frame(side, bg=t["panel"])
        lf.pack(fill="both", expand=True, padx=12, pady=(2, 10))
        sb = tk.Scrollbar(lf, width=8)
        sb.pack(side="right", fill="y")
        self.log = tk.Text(lf, bg=t["bg"], fg=t["text"], relief="flat",
                           font=("Consolas", 9), wrap="word", height=12,
                           yscrollcommand=sb.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        sb.config(command=self.log.yview)
        for tag, col in (("hand", t["accent"]), ("street", t["gold"]),
                         ("fold", t["dim"]), ("raise", t["loss"]),
                         ("bet", t["text"]), ("check", t["dim"]),
                         ("pot", t["win"]), ("blind", t["dim"]),
                         ("you", t["accent"]), ("show", t["text"])):
            self.log.tag_config(tag, foreground=col)

        tk.Label(side, text="SHOWDOWN", bg=t["panel"], fg=t["dim"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(
            fill="x", padx=12)
        self.l_show = tk.Label(side, text="-", bg=t["bg"], fg=t["text"],
                               font=("Consolas", 9), wraplength=258,
                               justify="left", anchor="nw", height=7)
        self.l_show.pack(fill="x", padx=12, pady=(2, 12))

        # Player Notes button (always accessible)
        tk.Button(side, text="Player Notes",
                  command=self.open_notes_viewer, relief="flat",
                  bg=t["btn"], fg=t["btn_text"],
                  font=("Segoe UI", 8), cursor="hand2").pack(
            fill="x", padx=12, pady=(4, 4))

        if self._mp_mode:
            self._build_chat(side)

    def _build_controls(self, f):
        t = self.theme
        bar = tk.Frame(f, bg=t["panel"], height=88)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        # Host admin bar sits above the action bar (mp host only)
        self._host_bar_built = False
        if self._mp_mode and self._mp_is_host:
            self._build_host_controls(f)
            self._host_bar_built = True

        left = tk.Frame(bar, bg=t["panel"])
        left.pack(side="left", padx=16, pady=10)
        toprow = tk.Frame(left, bg=t["panel"])
        toprow.pack(anchor="w")
        self.l_stack = tk.Label(toprow, text="", bg=t["panel"], fg=t["gold"],
                                font=("Segoe UI", 14, "bold"))
        self.l_stack.pack(side="left")
        self.l_clock = tk.Label(toprow, text="", bg=t["panel"], fg=t["accent"],
                                font=("Segoe UI", 11, "bold"))
        self.l_clock.pack(side="left", padx=(14, 0))
        self.l_keys = tk.Label(
            left, text="F fold   C check/call   R raise   N next hand",
            bg=t["panel"], fg=t["dim"], font=("Segoe UI", 8))
        self.l_keys.pack(anchor="w")
        btnrow = tk.Frame(left, bg=t["panel"])
        btnrow.pack(anchor="w", pady=(3, 0))

        def small(txt, cmd):
            b = tk.Button(btnrow, text=txt, command=cmd, relief="flat",
                          bg=t["btn"], fg=t["btn_text"], font=("Segoe UI", 8),
                          cursor="hand2", padx=6)
            b.pack(side="left", padx=(0, 5))
            return b

        self.b_sit = small("Sit out", self.toggle_sit)
        self.b_add = small("Add chips", self.add_chips_dialog)
        self.b_straddle = small("Straddle: off", self.toggle_straddle)
        self.b_rabbit = small("Rabbit", self.rabbit)

        # Emote / reaction tray (hidden until the human's turn)
        self._emote_frame = tk.Frame(bar, bg=t["panel"])
        self._emote_frame.pack(side="left", padx=8, pady=6)
        for _em in ("\U0001f44d", "\U0001f62e", "\U0001f602",
                    "\U0001f624", "\U0001f911", "\U0001fae0"):
            tk.Button(self._emote_frame, text=_em,
                      font=("Segoe UI", 16), bg=t["panel"],
                      relief="flat", cursor="hand2", bd=0,
                      command=lambda em=_em: self._show_emote(self.hero_seat, em)
                      ).pack(side="left", padx=1)
        self._emote_frame.pack_forget()

        right = tk.Frame(bar, bg=t["panel"])
        right.pack(side="right", padx=16, pady=8)

        self.sizes = tk.Frame(right, bg=t["panel"])
        self.sizes.grid(row=0, column=0, columnspan=6, sticky="e", pady=(0, 4))
        self.size_btns = []
        self._rebuild_bet_buttons()

        # Pre-action checkboxes (shown when it is NOT the human player's turn)
        self.v_pre_cf = tk.BooleanVar(value=False)   # Check / Fold
        self.v_pre_fa = tk.BooleanVar(value=False)   # Fold to any bet
        self.v_pre_ca = tk.BooleanVar(value=False)   # Call any
        self._pre_vars = [self.v_pre_cf, self.v_pre_fa, self.v_pre_ca]

        self.pre_row = tk.Frame(right, bg=t["panel"])
        # placed at same row as sizes; grid_remove() until needed
        self.pre_row.grid(row=0, column=0, columnspan=6, sticky="e",
                          pady=(0, 4))
        self.pre_row.grid_remove()

        def _pre_select(chosen):
            """Radio-button behaviour: clear siblings when one is ticked."""
            for v in self._pre_vars:
                if v is not chosen:
                    v.set(False)

        for txt, var in (("Check / Fold", self.v_pre_cf),
                         ("Fold to any bet", self.v_pre_fa),
                         ("Call any", self.v_pre_ca)):
            ttk.Checkbutton(self.pre_row, text=txt, variable=var,
                            command=lambda v=var: _pre_select(v)).pack(
                side="left", padx=6)

        self.slider = tk.Scale(right, from_=0, to=100, orient="horizontal",
                               variable=self.v_bet, length=250, showvalue=False,
                               bg=t["panel"], fg=t["text"], troughcolor=t["bg"],
                               highlightthickness=0, relief="flat",
                               activebackground=t["accent"],
                               command=lambda _v: self._sync_raise_label())
        self.slider.grid(row=1, column=0, columnspan=2, padx=(0, 8))

        self.b_fold = tk.Button(right, text="Fold", width=9, relief="flat",
                                bg="#5c2333", fg="#ffe8ee", cursor="hand2",
                                font=("Segoe UI", 10, "bold"), pady=4,
                                command=lambda: self.hero("fold"))
        self.b_fold.grid(row=1, column=2, padx=3)

        self.b_call = tk.Button(right, text="Check", width=11, relief="flat",
                                bg="#1f5c46", fg="#e6fff4", cursor="hand2",
                                font=("Segoe UI", 10, "bold"), pady=4,
                                command=lambda: self.hero("call"))
        self.b_call.grid(row=1, column=3, padx=3)

        self.b_raise = tk.Button(right, text="Raise", width=13, relief="flat",
                                 bg="#2a4a8c", fg="#e8f0ff", cursor="hand2",
                                 font=("Segoe UI", 10, "bold"), pady=4,
                                 command=lambda: self.hero("raise"))
        self.b_raise.grid(row=1, column=4, padx=3)

        self.b_next = tk.Button(right, text="Next hand  (N)", width=14,
                                relief="flat", bg=t["accent"], fg="#04040c",
                                font=("Segoe UI", 10, "bold"), pady=4,
                                cursor="hand2", command=self.deal)
        self.b_next.grid(row=1, column=5, padx=(10, 0))

    # -------------------------------------------------------------- helpers

    @property
    def delay(self):
        return SPEEDS[self.v_speed.get()]

    def say(self, kind, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", kind)
        self.log.see("end")
        self.log.config(state="disabled")

    def flush_log(self):
        evts = self.engine.drain()
        for kind, text in evts:
            self.say(kind, text)
            # Mirror notable game events into the chat panel (mp mode only)
            if self._mp_mode and kind in (
                    "hand", "blind", "fold", "check", "call",
                    "bet", "raise", "pot", "show"):
                self._append_chat_event(text)
                if self._mp_is_host:
                    # Broadcast dealer events so peers see them in their chat panel
                    try:
                        from .p2p import transport as _t
                        _t.broadcast({
                            "type":    "chat",
                            "payload": {"nickname": "[Dealer]", "text": text},
                        })
                    except Exception:
                        pass
        self.hand_logger.feed(evts)

    # ---------------------------------------------------------- game set-up

    def new_game(self):
        self.table_rules = cfg.table_rules(**self._bucket(cfg.TABLE_RULE))
        self.save_config()
        r = self.table_rules
        n = r["players"]
        sb = r["sb"]
        bb = max(sb + 1, r["bb"])
        cash = r["mode"] == "Cash"
        stack = r["stack"]
        if cash:                                  # table stakes rule
            stack = max(r["buyin_min_bb"] * bb,
                        min(stack, r["buyin_max_bb"] * bb))
        self.theme = THEMES[self.v_theme.get()]

        base = self.v_level.get()
        hero_name = getattr(self, "nickname", None) or "You"
        players = [Player(0, hero_name, stack, style="Hero", level=3, human=True)]
        for i in range(1, n):
            lvl = self.rng.randint(1, 3) if self.v_chaos.get() else base
            players.append(Player(i, f"P{i+1}", stack,
                                  style=self.rng.choice(AI_STYLES), level=lvl))

        self.engine = Engine(players, sb=sb, bb=bb,
                             structure=self.v_struct.get(), rng=self.rng,
                             bb_ante=(not cash) and self.v_ante.get(),
                             deal_sitting_out=not cash)
        self.buyin = stack
        # deduct buy-in from persistent bankroll (cash only)
        if cash:
            br = cfg.get('bankroll')
            cfg.set('bankroll', max(0, br - stack))
        self.bank = float(BANK_START)
        self.level_started = time.time()
        self.level_idx = 0
        self.straddle_armed = False
        self.rabbit_cards = None
        self.paused = False
        self.settings_win = None
        self.game_over = False
        self.hand_over = True
        self.result = None
        self.reveal = set()
        self.highlight = set()
        self.stop_clock()
        self.session_stats = SessionStats()
        self._vpip_counted = set()
        self._pfr_counted = set()

        self.b_sit.config(text="Sit out")
        self.b_straddle.config(text="Straddle: off")
        can_straddle = (cash and self.v_straddles.get()
                        and self.v_struct.get() != "Fixed-Limit" and n >= 3)
        self.b_straddle.config(
            state="normal" if can_straddle else "disabled")
        self.b_add.config(state="disabled")
        self.b_rabbit.config(state="disabled")

        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

        self._retheme()
        self.l_summary.config(
            text=f"{n}-handed  ·  {self.v_struct.get()}  ·  {self.v_mode.get()}"
                 f"  ·  AI {'mixed' if self.v_chaos.get() else base}"
                 f"  ·  table #{cfg.rules_hash(self.table_rules)}")
        self._show(self.table)
        self.say("hand", "=== New game ===")
        self.level_gen = getattr(self, "level_gen", 0) + 1
        if self.v_mode.get() == "Tournament":
            self.root.after(1000, lambda g=self.level_gen: self._level_tick(g))
            self._tourn_tick_gen += 1
            _ttg = self._tourn_tick_gen
            self.root.after(1000, lambda g=_ttg: self._tick_tournament_overlay(g))
        self.root.after(120, self.deal)

    def _retheme(self):
        t = self.theme
        for w in (self.table, self.cv):
            w.configure(bg=t["bg"])

    # ======================================================== Multiplayer

    def _mp_setup_callbacks(self) -> None:
        """Wire session callbacks based on host/peer role."""
        sess = self._mp_session
        if self._mp_is_host:
            sess.on_shuffle_ready = lambda deck: self.root.after(
                0, lambda d=deck: self._mp_on_shuffle_ready(d))
        else:
            sess.on_game_state   = lambda s: self.root.after(
                0, lambda st=s: self._mp_apply_state(st))
            sess.on_deal_private = lambda d: self.root.after(
                0, lambda pd=d: self._mp_on_deal_private(pd))
            sess.on_shuffle_deal = lambda d: self.root.after(
                0, lambda pd=d: self._mp_on_shuffle_deal(pd))
        sess.on_chat = lambda nick, txt: self.root.after(
            0, lambda n=nick, t=txt: self._append_chat(n, t))
        sess.on_host_changed = lambda am_host: self.root.after(
            0, lambda h=am_host: self._mp_on_host_changed(h))
        sess.on_pause = lambda: self.root.after(0, self._mp_on_pause)
        sess.on_resume = lambda: self.root.after(0, self._mp_on_resume)
        sess.on_kick = lambda p: self.root.after(
            0, lambda payload=p: self._mp_on_kick(payload))
        sess.on_adjust_blinds = lambda p: self.root.after(
            0, lambda payload=p: self._mp_on_adjust_blinds(payload))

    def _mp_new_game(self) -> None:
        """Create the Engine for a multiplayer session (host or peer)."""
        sess = self._mp_session
        ts   = getattr(sess, '_last_table_settings',
                       {"sb": 10, "bb": 20, "stack": 1000})
        sb        = ts.get("sb",        10)
        bb        = ts.get("bb",        20)
        stack     = ts.get("stack",   1000)
        structure = ts.get("structure", "No-Limit")

        # H-10/M-13: store run-it-twice and straddle rules so loop/deal use them
        self._mp_rit       = ts.get("rit",       "Ask")
        self._mp_straddles = ts.get("straddles",  False)

        seat_order = sess._seat_order or list(sess.players.keys())
        n = max(2, len(seat_order))

        self._mp_seat_to_peer = {}
        players: list[Player] = []
        for i in range(n):
            cid       = seat_order[i] if i < len(seat_order) else ""
            sp        = sess.players.get(cid)
            name      = sp.nickname if sp else f"P{i+1}"
            is_local  = (i == self._mp_local_seat)
            if is_local:
                name = getattr(self, "nickname", None) or name
            self._mp_seat_to_peer[i] = cid
            if cid and not is_local:
                _notes.update_nickname(cid, name)
            p = Player(i, name, stack,
                       style="Hero" if is_local else "Solid",
                       level=3        if is_local else 2,
                       human=is_local)
            players.append(p)

        # H-10: pass betting structure to Engine (was always defaulting to No-Limit)
        self.engine = Engine(players, sb=sb, bb=bb, structure=structure)

        # Common state reset
        self.buyin  = stack
        self.bank   = float(BANK_START)
        self.level_started    = time.time()
        self.straddle_armed   = False
        self.rabbit_cards     = None
        self.paused           = False
        self._mp_game_paused  = False
        self.game_over        = False
        self.hand_over        = True
        self.result           = None
        self.reveal           = set()
        self.highlight        = set()
        self.session_stats    = SessionStats()
        self._vpip_counted    = set()
        self._pfr_counted     = set()
        self._allin_announced = set()
        self._last_hero_eq    = 0.0
        self.stop_clock()
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self._retheme()
        self._show(self.table)

        if self._mp_is_host:
            sess._engine  = self.engine
            sess._seat_order = seat_order
            sess.on_action = lambda seat, act, amt: self.root.after(
                0, lambda s=seat, a=act, m=amt: self._mp_peer_acted(s, a, m))
            self.say("hand", "=== Multiplayer Game (Host) ===")
            self.b_next.config(state="disabled")
            self.root.after(120, self.deal)
        else:
            self.say("hand", "=== Multiplayer Game ===")
            self.l_status.config(text="Waiting for host to deal…")
            self.lock()
            self.b_next.config(state="disabled")

    def _mp_after_deal(self) -> None:
        """Host: after dealing, send hole cards (encrypted) and broadcast state."""
        e    = self.engine
        sess = self._mp_session
        seat_order = sess._seat_order
        for i, cid in enumerate(seat_order):
            sp = sess.players.get(cid)
            if sp and not sp.is_host and i < len(e.players):
                cards_str = [_card_to_str(c) for c in e.players[i].hole]
                # Prefer encrypted send; falls back to plaintext if no pubkey
                sess.send_encrypted_hole_cards(cid, i, cards_str)
        sess.broadcast_game_state()

    def _mp_apply_state(self, state: dict) -> None:
        """Peer: update stub engine from a received game_state and redraw."""
        self._mp_remote_state = state
        e = self.engine
        if e is None:
            return

        e.board  = [_str_to_card(s) for s in state.get("community", [])]
        e.street = state.get("street", "idle")

        stacks = state.get("stacks",  [])
        bets   = state.get("bets",    [])
        folded = state.get("folded",  [])
        allin  = state.get("allin",   [])

        for i, p in enumerate(e.players):
            p.stack   = stacks[i] if i < len(stacks) else p.stack
            p.bet     = bets[i]   if i < len(bets)   else 0
            p.folded  = folded[i] if i < len(folded)  else False
            p.all_in  = allin[i]  if i < len(allin)   else False
            p.in_seat = (p.stack > 0 or p.bet > 0) or not p.folded
            p.total_live = p.bet  # for pot display
            # hole cards
            if i == self._mp_local_seat:
                p.hole = list(self._mp_hole_cards)
            elif not p.folded:
                p.hole = [_STUB_CARD, _STUB_CARD]
            else:
                p.hole = []

        action_on     = state.get("action_on", -1)
        e.actor       = action_on if 0 <= action_on < len(e.players) else None
        e.current_bet = state.get("call_amount", 0)
        e.min_raise   = state.get("min_raise",   e.bb)
        self.hand_over = e.street in ("idle", "showdown")

        last = state.get("last_action")
        if last:
            si  = last.get("seat",   -1)
            act = last.get("action", "")
            amt = last.get("amount", 0)
            if 0 <= si < len(e.players):
                pl = e.players[si]
                if act == "fold":
                    pl.last_action = "FOLD"
                elif act in ("call", "check"):
                    pl.last_action = f"CALL {amt}" if amt else "CHECK"
                elif act == "raise":
                    pl.last_action = f"RAISE {pl.bet}"

        self.redraw()

        if action_on == self._mp_local_seat:
            self._mp_unlock()
        else:
            self.lock()

    def _mp_on_deal_private(self, data: dict) -> None:
        """Peer: store received hole cards and update display."""
        if data.get("seat") != self._mp_local_seat:
            return
        self._mp_hole_cards = [_str_to_card(s)
                               for s in data.get("hole_cards", [])]
        e = self.engine
        if e and self._mp_local_seat < len(e.players):
            e.players[self._mp_local_seat].hole = list(self._mp_hole_cards)
        self.redraw()

    def _mp_unlock(self) -> None:
        """Peer: enable action buttons when it's the local seat's turn."""
        e     = self.engine
        local = self._mp_local_seat
        if e is None or local >= len(e.players):
            return
        lg = e.legal(local)
        self._show_pre_actions(False)
        for b in (self.b_fold, self.b_call):
            b.config(state="normal")
        self.b_call.config(
            text="Check" if lg["can_check"] else f"Call {lg['to_call']}")
        self.b_fold.config(text="Fold")
        if lg["can_raise"]:
            self.b_raise.config(state="normal")
            for b in self.size_btns:
                b.config(state="normal")
            self.slider.config(state="normal",
                               from_=lg["min_to"], to=lg["max_to"],
                               resolution=max(1, e.bb // 4))
            pot_bet = int(e.current_bet + lg["pot"] * 0.6)
            self.v_bet.set(max(lg["min_to"], min(pot_bet, lg["max_to"])))
        else:
            self.b_raise.config(state="disabled")
            for b in self.size_btns:
                b.config(state="disabled")
            self.slider.config(state="disabled")
        self._sync_raise_label()
        self.l_status.config(
            text=f"{e.street.capitalize()} — your move."
                 + (f"  {lg['to_call']} to call." if lg["to_call"] else ""))

    def _mp_peer_acted(self, seat: int, action: str, amount: int) -> None:
        """Host: apply a validated action from a peer and continue the loop."""
        e = self.engine
        if e is None or e.actor != seat:
            return
        lg = e.legal(seat)
        if action == "fold":
            _audio.play("fold")
        elif action == "call":
            _audio.play("check" if lg["can_check"] else "call")
        elif action == "raise":
            _audio.play("raise_sound")
        e.act(seat, action, amount)
        self.flush_log()
        self._mp_session.broadcast_game_state()
        self.redraw()
        self.root.after(max(80, self.delay // 3), self.loop)

    def _mp_send_action(self, action: str, amount: int = 0) -> None:
        """Peer: pack and broadcast an action message to the host."""
        import json as _json
        from .p2p import wire as _wire
        from .p2p import transport as _t
        msg = _json.loads(_wire.pack("action", {
            "seat":   self._mp_local_seat,
            "action": action,
            "amount": amount,
        }))
        _t.broadcast(msg)

    def _mp_send_chat(self, text: str) -> None:
        """Send a chat message; host shows it locally, peers wait for echo."""
        import json as _json
        from .p2p import wire as _wire
        from .p2p import transport as _t
        nickname = getattr(self, "nickname", "Player")
        msg = _json.loads(_wire.pack("chat",
                                     {"text": text, "nickname": nickname}))
        _t.broadcast(msg)
        if self._mp_is_host:
            self._append_chat(nickname, text)

    def _append_chat(self, nickname: str, text: str) -> None:
        """Append a line to the chat log (mp mode only)."""
        if not hasattr(self, "chat_log"):
            return
        self.chat_log.config(state="normal")
        if nickname == "[Dealer]":
            self.chat_log.insert("end", f"[Dealer] {text}\n", "dealer_ev")
        else:
            self.chat_log.insert("end", f"{nickname}: {text}\n")
        self.chat_log.see("end")
        self.chat_log.config(state="disabled")

    def _append_chat_event(self, text: str) -> None:
        """Append a dealer event line to the chat log."""
        if not hasattr(self, "chat_log") or not self._mp_mode:
            return
        self.chat_log.config(state="normal")
        self.chat_log.insert("end", f"[Dealer] {text}\n", "dealer_ev")
        self.chat_log.see("end")
        self.chat_log.config(state="disabled")

    # ---------------------------------------- Chat pane (mp mode only)

    def _build_chat(self, parent) -> None:
        """Build the in-game chat panel inside *parent* (mp mode only)."""
        t = self.theme
        tk.Label(parent, text="CHAT", bg=t["panel"], fg=t["dim"],
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(
            fill="x", padx=12, pady=(4, 0))
        cf = tk.Frame(parent, bg=t["panel"])
        cf.pack(fill="both", expand=True, padx=12, pady=(2, 2))
        sb = tk.Scrollbar(cf, width=8)
        sb.pack(side="right", fill="y")
        self.chat_log = tk.Text(
            cf, bg=t["bg"], fg=t["text"], relief="flat",
            font=("Consolas", 9), wrap="word", height=8,
            yscrollcommand=sb.set, state="disabled")
        self.chat_log.pack(side="left", fill="both", expand=True)
        sb.config(command=self.chat_log.yview)
        self.chat_log.tag_config("dealer_ev", foreground=t["dim"])

        entry_row = tk.Frame(parent, bg=t["panel"])
        entry_row.pack(fill="x", padx=12, pady=(0, 8))
        self._chat_var = tk.StringVar()
        chat_entry = tk.Entry(
            entry_row, textvariable=self._chat_var,
            bg=t["bg"], fg=t["text"], relief="flat",
            font=("Consolas", 9), insertbackground=t["text"])
        chat_entry.pack(side="left", fill="x", expand=True)
        chat_entry.bind("<Return>", lambda _e: self._on_chat_send())
        tk.Button(
            entry_row, text="Send", relief="flat",
            bg=t["btn"], fg=t["btn_text"],
            font=("Segoe UI", 8), cursor="hand2",
            command=self._on_chat_send).pack(side="left", padx=(4, 0))

    def _on_chat_send(self) -> None:
        """Send the content of the chat entry box."""
        text = getattr(self, "_chat_var", None)
        if text is None:
            return
        msg = text.get().strip()
        if not msg:
            return
        text.set("")
        self._mp_send_chat(msg)

    # ---------------------------------------- Host-change / banner

    def _mp_on_host_changed(self, am_new_host: bool) -> None:
        """Called when the active host drops and a new host is elected."""
        if am_new_host:
            self._mp_is_host = True
            self._show_banner("Host left — you are now hosting", duration_ms=4000)
            # H-11: wire the session engine and action callback so broadcast works
            if self._mp_session is not None and self.engine is not None:
                self._mp_session._engine = self.engine
                # Re-apply the last known game state so the engine is up to date
                if self._mp_remote_state:
                    self._mp_apply_state(self._mp_remote_state)
                self._mp_session.on_action = lambda seat, act, amt: self.root.after(
                    0, lambda s=seat, a=act, m=amt: self._mp_peer_acted(s, a, m))
            self._mp_broadcast_state()
            # Show the host controls bar if not already visible
            if not getattr(self, "_host_bar_built", False):
                self._build_host_controls(self.table)
                self._host_bar_built = True
        else:
            self._show_banner("Host reconnecting…", duration_ms=2000)

    def _mp_broadcast_state(self) -> None:
        """Re-broadcast current game state to all peers (called after becoming host)."""
        if self._mp_session and self._mp_is_host:
            self._mp_session.broadcast_game_state()

    def _show_banner(self, text: str, duration_ms: int = 3000) -> None:
        """Draw a transient text banner in the canvas centre."""
        cv = self.cv
        W = max(cv.winfo_width(), 400)
        H = max(cv.winfo_height(), 300)
        t = self.theme
        bg_id = cv.create_rectangle(
            W // 2 - 270, H // 2 - 30, W // 2 + 270, H // 2 + 30,
            fill=t["panel"], outline=t["accent"], width=2, tags="banner")
        txt_id = cv.create_text(
            W // 2, H // 2, text=text,
            fill=t["accent"], font=("Segoe UI", 13, "bold"), tags="banner")

        def _remove():
            try:
                cv.delete(bg_id)
                cv.delete(txt_id)
            except Exception:
                pass
        self.root.after(duration_ms, _remove)

    # ---------------------------------------- Pause overlay helpers

    def _mp_show_pause_overlay(self) -> None:
        """Draw a semi-transparent pause overlay on the canvas."""
        cv = self.cv
        W = max(cv.winfo_width(), 400)
        H = max(cv.winfo_height(), 300)
        t = self.theme
        cv.create_rectangle(0, 0, W, H, fill=t["bg"], stipple="gray50",
                             tags="pause_overlay")
        cv.create_text(W // 2, H // 2, text="Game paused by host",
                       fill=t["gold"], font=("Segoe UI", 22, "bold"),
                       tags="pause_overlay")

    def _mp_hide_pause_overlay(self) -> None:
        try:
            self.cv.delete("pause_overlay")
        except Exception:
            pass

    # ---------------------------------------- Peer admin-message handlers

    def _mp_on_pause(self) -> None:
        """Peer: host has paused the game."""
        self._mp_game_paused = True
        self.paused = True
        self.lock()
        self._mp_show_pause_overlay()

    def _mp_on_resume(self) -> None:
        """Peer: host has resumed the game."""
        self._mp_game_paused = False
        self.paused = False
        self._mp_hide_pause_overlay()
        if self._mp_remote_state:
            self._mp_apply_state(self._mp_remote_state)

    def _mp_on_kick(self, payload: dict) -> None:
        """Peer: check if we're the kicked player; if so, close the window."""
        my_cid = getattr(self._mp_session, "local_conn_id", "")
        if payload.get("conn_id") and payload["conn_id"] == my_cid:
            messagebox.showinfo("Removed", "You were removed by the host.")
            self.root.destroy()

    def _mp_on_adjust_blinds(self, payload: dict) -> None:
        """Peer: host has changed the blinds for the next hand."""
        sb = payload.get("sb", 10)
        bb = payload.get("bb", 20)
        if self.engine:
            self.engine.sb = sb
            self.engine.bb = bb
        self.l_blinds.config(text=f"Blinds {sb}/{bb}")

    # -------------------------------------------------------- XP / level

    def update_xp(self, hands=0, pots_won=0, tourney_won=False):
        """Award XP and recompute level; persist immediately."""
        xp = cfg.get("xp") + 10 * hands + 50 * pots_won + (200 if tourney_won else 0)
        level = min(50, int(math.sqrt(xp / 100)) + 1)
        cfg.set("xp", xp)
        cfg.set("player_level", level)

    def deal(self):
        if self._mp_mode and not self._mp_is_host:
            return   # peer: host deals for us
        if self._defer(self.deal):
            return
        e = self.engine
        if e is None or self.game_over or not self.hand_over:
            return
        cash = self.v_mode.get() == "Cash"

        if cash and self.v_topup.get():           # AI auto top-up
            for p in e.players:
                if not p.human and 0 <= p.stack < self.buyin:
                    e.add_chips(p.idx, self.buyin - p.stack)

        local = self.hero_seat
        hero = e.players[local]
        if cash and hero.stack == 0 and not hero.sitting_out:
            if messagebox.askyesno(
                    "Rebuy", f"You're felted. Rebuy for {self.buyin:,}?"):
                e.add_chips(local, self.buyin)
            else:
                e.sit_out(local)
                self.b_sit.config(text="I'm back")

        live = [p for p in e.players if p.stack > 0]
        if len(live) < 2:
            self.finish_game(live)
            return

        if not cash:                              # timed blind levels
            mins = max(1, self.v_lvlmin.get())
            want = min(len(BLIND_LEVELS) - 1,
                       int((time.time() - self.level_started) // (mins * 60)))
            if want != self.level_idx:
                self.level_idx = want
                e.sb, e.bb = BLIND_LEVELS[want]
                self.say("street", f"** Blinds up: {e.sb}/{e.bb} **")

        self.result = None
        self.reveal = set()
        self.highlight = set()
        self.rabbit_cards = None
        self.b_rabbit.config(state="disabled")
        self.eq_text = "-"
        self.eq_bars = (0, 0, 1)
        self.l_show.config(text="-")
        self.hand_over = False
        self._cashout_offered = False

        allow_straddle = (cash and self.v_straddles.get()
                          and str(self.b_straddle["state"]) == "normal")

        def straddle_fn(utg):
            if not allow_straddle:
                return False
            p = e.players[utg]
            if p.human:
                return self.straddle_armed and not self.v_observe.get()
            return self.rng.random() < {"Maniac": 0.4, "Loose": 0.15}.get(
                p.style, 0.0)

        if self._mp_mode and self._mp_is_host:
            # Verifiable shuffle: kick off the commit-reveal protocol.
            # _mp_on_shuffle_ready() will call _mp_do_start_hand() once all
            # peers have revealed their seeds.
            self._pending_straddle_fn = straddle_fn
            self._mp_session.start_shuffle()
            return

        # Solo / local AI game: deal directly with the RNG-shuffled deck.
        if not e.start_hand(straddle_fn=straddle_fn):
            self.finish_game(live)
            return
        self._post_start_hand()

    def _post_start_hand(self):
        """Run the common post-start_hand bookkeeping (solo and MP-host paths)."""
        e     = self.engine
        local = self.hero_seat
        self.hand_logger.on_hand_start(e.players, e)
        self.session_stats.record_hand_start(e.players)
        self._vpip_counted = set()
        self._pfr_counted = set()
        self._allin_announced = set()
        self._last_hero_eq = 0.0
        _audio.play("deal")
        cfg.set('hands_played_total', cfg.get('hands_played_total') + 1)
        self.update_xp(hands=1)
        if e.bb_i == local and e.players[local].in_seat:
            self.bank = min(BANK_CAP, self.bank + BANK_TOPUP)
        self.flush_log()
        self.l_blinds.config(
            text=f"Blinds {e.sb}/{e.bb}"
                 + (f" · ante {e.bb}" if e.bb_ante else ""))
        self.b_next.config(state="disabled")
        self.b_add.config(state="disabled")
        self.loop()
        if self._mp_mode and self._mp_is_host:
            self._mp_after_deal()

    # ---------------------------------------- Verifiable-shuffle callbacks

    def _mp_on_shuffle_ready(self, deck_indices: list) -> None:
        """Host: all peers revealed — build the verified deck and start the hand."""
        from .engine import Deck as _Deck
        e = self.engine
        if e is None or self.game_over or not self.hand_over:
            return
        deck = _Deck.from_indices(deck_indices)
        straddle_fn = getattr(self, "_pending_straddle_fn", None)
        if not e.start_hand(straddle_fn=straddle_fn, deck=deck):
            live = [p for p in e.players if p.stack > 0]
            self.finish_game(live)
            return
        self._post_start_hand()

    def _mp_on_shuffle_deal(self, data: dict) -> None:
        """Peer: decrypt encrypted hole cards received from the host."""
        if data.get("seat") != self._mp_local_seat:
            return
        encrypted_hex = data.get("encrypted_hex", "")
        if not encrypted_hex:
            return
        try:
            from holdem.p2p import identity as _id
            from holdem.p2p.shuffle import decrypt_hole_cards
            blob = bytes.fromhex(encrypted_hex)
            cards_str = decrypt_hole_cards(blob, _id.x25519_private_key())
            self._mp_hole_cards = [_str_to_card(s) for s in cards_str]
            e = self.engine
            if e and self._mp_local_seat < len(e.players):
                e.players[self._mp_local_seat].hole = list(self._mp_hole_cards)
            self.redraw()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "shuffle_deal decrypt failed: %s — trying plaintext fallback", exc)

    def finish_game(self, live):
        self.stop_clock()
        self.game_over = True
        self.hand_over = True
        self.lock()
        self.b_next.config(state="disabled")
        if live and live[0].idx == self.hero_seat:
            self.l_status.config(text="You took the whole table. Nice.")
            self.say("pot", "*** You win — everyone else is broke. ***")
        else:
            who = live[0].name if live else "nobody"
            self.l_status.config(
                text=f"You're out of chips. {who} takes it. "
                     f"Open Settings to start again.")
            self.say("pot", f"*** {who} wins the game ***")
        self.redraw()

    # ------------------------------------------------------------- the loop

    def loop(self):
        if self._mp_mode and not self._mp_is_host:
            return   # peers are driven by game_state messages, not the local loop
        if self._defer(self.loop):
            return
        e = self.engine
        if e is None:
            return

        if len(e.contested()) <= 1 or e.street == "showdown":
            self.showdown(tabled=e.betting_locked())
            return

        if e.actor is None:                     # betting round closed
            if e.betting_locked() and len(e.board) < 5:
                self.locked_runout()
                return
            e.next_street()
            self.flush_log()
            self.redraw()
            self.start_equity()
            self.root.after(self.delay, self.loop)
            return

        i = e.actor
        self.redraw()

        local = self.hero_seat
        hero = e.players[local]
        if i == local and hero.sitting_out:     # sitting out: check or fold
            self.lock()
            act = "call" if e.legal(local)["can_check"] else "fold"
            self.root.after(max(60, self.delay // 3),
                            lambda a=act: self._forced(a))
            return

        if i == local and not self.v_observe.get():
            self.start_equity()
            if self._maybe_fire_pre_action():
                return    # pre-action fired; loop() will be called again
            self.unlock()
            self.start_clock()
        else:
            self.lock()
            self.root.after(self.delay,
                            lambda seat=i: self.ai_turn(seat))

    def _forced(self, action):
        """A protocol action taken for the hero: sit-out or clock timeout."""
        if self._defer(lambda a=action: self._forced(a)):
            return
        e = self.engine
        local = self.hero_seat
        if e is None or e.actor != local:
            return
        e.act(local, action, 0)
        self.flush_log()
        self.redraw()
        self.root.after(max(60, self.delay // 3), self.loop)

    def locked_runout(self):
        """No more betting possible, board incomplete: table the hands and
        settle in one run or two."""
        if self._defer(self.locked_runout):
            return
        e = self.engine
        self.lock()
        self.reveal |= {p.idx for p in e.contested()}     # force-table
        self.redraw()

        runs = 1
        board_left = len(e.board) < 5
        if board_left:
            hero_in = (any(p.idx == self.hero_seat for p in e.contested())
                       and not self.v_observe.get()
                       and not e.players[self.hero_seat].sitting_out)
            if hero_in and self._maybe_offer_cashout(e):
                return   # hand settled via EV cashout
            mode = self.v_rit.get()
            if mode == "Always":
                runs = 2
            elif mode == "Ask":
                if hero_in:
                    runs = 2 if messagebox.askyesno(
                        "Run it twice",
                        "All in. Run the board twice?") else 1
                else:
                    runs = 2                     # the AIs always consent
        self.root.after(self.delay, lambda: self.showdown(runs=runs,
                                                          tabled=True))

    def ai_turn(self, i):
        if self._defer(lambda i=i: self.ai_turn(i)):
            return
        e = self.engine
        if e is None or e.actor != i:
            return
        act, amt = self.brain.decide(e, i)
        if e.street == "preflop":
            self._record_vpip_pfr(i, act, e.legal(i))
        e.act(i, act, amt)
        if e.players[i].all_in and i not in self._allin_announced:
            self._allin_announced.add(i)
            _audio.play("allin")
        self.flush_log()
        self.redraw()
        # ~20% chance of a contextual emote from this AI seat
        # M-12: use the seeded rng to keep replay deterministic
        if self.rng.random() < 0.20:
            _p = e.players[i]
            _emote = None
            if _p.style == "Maniac":
                _emote = ("\U0001f911" if act == "raise"
                          else ("\U0001f624" if act == "fold" else None))
            elif _p.style == "Nit":
                if act in ("call", "fold"):
                    _emote = "\U0001f624"
            elif _p.style == "Loose":
                _emote = "\U0001f602"
            elif _p.style == "Solid":
                if act == "raise":
                    _emote = "\U0001f44d"
            if _emote:
                self.root.after(
                    max(60, self.delay // 2),
                    lambda si=i, em=_emote: self._show_emote(si, em))
        self.root.after(max(60, self.delay // 3), self.loop)

    # --------------------------------------------------------- hero actions

    def _show_pre_actions(self, show: bool) -> None:
        """Toggle between the size-preset buttons and pre-action checkboxes."""
        if show:
            self.sizes.grid_remove()
            self.pre_row.grid(row=0, column=0, columnspan=6,
                              sticky="e", pady=(0, 4))
        else:
            self.pre_row.grid_remove()
            self.sizes.grid(row=0, column=0, columnspan=6,
                            sticky="e", pady=(0, 4))

    def _clear_pre_actions(self) -> None:
        for v in self._pre_vars:
            v.set(False)

    def _maybe_fire_pre_action(self) -> bool:
        """If a pre-action checkbox is armed and still valid, fire it.
        Returns True if an action was dispatched (caller should return)."""
        e = self.engine
        local = self.hero_seat
        if e is None or e.actor != local or self.v_observe.get():
            return False
        lg = e.legal(local)

        if self.v_pre_cf.get():
            # Check / Fold: check when free, otherwise fold
            action = "call" if lg["can_check"] else "fold"
            self._clear_pre_actions()
            self._fire_pre_action(action)
            return True

        if self.v_pre_fa.get():
            self._clear_pre_actions()
            self._fire_pre_action("fold")
            return True

        if self.v_pre_ca.get():
            self._clear_pre_actions()
            self._fire_pre_action("call")
            return True

        return False

    def _fire_pre_action(self, action: str) -> None:
        """Execute a pre-selected action for the hero."""
        e = self.engine
        local = self.hero_seat
        if e is None or e.actor != local:
            return
        self.stop_clock()
        self.lock()
        lg = e.legal(local)
        if e.street == "preflop":
            self._record_vpip_pfr(local, action, lg)
        e.act(local, action, 0)
        if action == "fold":
            _audio.play("fold")
        elif action == "call":
            _audio.play("check" if lg["can_check"] else "call")
        self.flush_log()
        self.redraw()
        self.root.after(max(80, self.delay // 3), self.loop)

    def _record_vpip_pfr(self, seat_idx: int, action: str, lg: dict) -> None:
        """Update session VPIP/PFR counters for one preflop action.
        Guards against double-counting within the same hand via _vpip_counted
        and _pfr_counted sets."""
        if action == "raise" and seat_idx not in self._pfr_counted:
            self.session_stats.record_pfr(seat_idx)
            self._pfr_counted.add(seat_idx)
        voluntary = action == "raise" or (action == "call" and lg["to_call"] > 0)
        if voluntary and seat_idx not in self._vpip_counted:
            self.session_stats.record_voluntary_action(seat_idx)
            self._vpip_counted.add(seat_idx)

    def lock(self):
        for b in (self.b_fold, self.b_call, self.b_raise, *self.size_btns):
            b.config(state="disabled")
        self.slider.config(state="disabled")
        # Show pre-action checkboxes only while a hand is actively running
        self._show_pre_actions(not self.hand_over)
        if hasattr(self, '_emote_frame'):
            self._emote_frame.pack_forget()

    def unlock(self):
        e = self.engine
        lg = e.legal(self.hero_seat)
        t = self.theme

        # Hero's turn: switch back to size-preset buttons
        self._show_pre_actions(False)

        for b in (self.b_fold, self.b_call):
            b.config(state="normal")

        self.b_call.config(
            text="Check" if lg["can_check"] else f"Call {lg['to_call']}")
        self.b_fold.config(text="Fold")

        if lg["can_raise"]:
            self.b_raise.config(state="normal")
            for b in self.size_btns:
                b.config(state="normal")
            if e.structure == "Fixed-Limit":
                self.slider.config(state="disabled")
                self.v_bet.set(lg["min_to"])
            else:
                self.slider.config(state="normal",
                                   from_=lg["min_to"], to=lg["max_to"],
                                   resolution=max(1, e.bb // 4))
                cur = self.v_bet.get()
                if not (lg["min_to"] <= cur <= lg["max_to"]):
                    pot_bet = int(e.current_bet + lg["pot"] * 0.6)
                    self.v_bet.set(max(lg["min_to"],
                                       min(pot_bet, lg["max_to"])))
        else:
            self.b_raise.config(state="disabled")
            for b in self.size_btns:
                b.config(state="disabled")
            self.slider.config(state="disabled")

        self._sync_raise_label()
        self.l_status.config(
            text=f"{e.street.capitalize()} — your move."
                 + (f"  {lg['to_call']} to call." if lg["to_call"] else ""))
        if self.v_hint.get() and self.aids_ok():
            self.l_hint.config(text="Hint: " + self.hint(lg))
        else:
            self.l_hint.config(text="")
        self._emote_frame.pack(side="left", padx=8, pady=6)

    def _show_emote(self, seat_idx, emoji):
        """Animate an emoji floating up from the given seat position."""
        e = self.engine
        if e is None:
            return
        cv = self.cv
        W = max(cv.winfo_width(), 600)
        H = max(cv.winfo_height(), 420)
        cx, cy = W / 2, H * 0.46
        rx, ry = W * 0.36, H * 0.27
        srx, sry = rx * 1.08, ry * 1.32
        n = len(e.players)
        ang = math.pi / 2 - seat_idx * 2 * math.pi / n
        sx = cx + srx * math.cos(ang)
        sy = cy + sry * math.sin(ang)
        item = cv.create_text(sx, sy - 55, text=emoji,
                              font=("Segoe UI", 22), tags="emote")
        steps = 12          # 12 steps × 100 ms = 1.2 s total
        dy = 30.0 / steps   # 30 px upward total

        def _move(step):
            if step >= steps:
                try:
                    cv.delete(item)
                except Exception:
                    pass
                return
            try:
                cv.move(item, 0, -dy)
            except Exception:
                return
            self.root.after(100, lambda: _move(step + 1))

        self.root.after(100, lambda: _move(0))

    def _sync_raise_label(self):
        e = self.engine
        local = self.hero_seat
        if e is None or e.actor != local:
            return
        lg = e.legal(local)
        amt = self.v_bet.get()
        if amt >= lg["max_to"] and lg["max_to"] > 0:
            self.b_raise.config(text=f"All-in {amt}")
        elif e.current_bet == 0:
            self.b_raise.config(text=f"Bet {amt}")
        else:
            self.b_raise.config(text=f"Raise to {amt}")

    def preset(self, frac):
        e = self.engine
        local = self.hero_seat
        if e is None or e.actor != local:
            return
        lg = e.legal(local)
        if frac is None:
            amt = lg["max_to"]
        else:
            call = lg["to_call"]
            amt = int(e.current_bet + (lg["pot"] + call) * frac)
        self.v_bet.set(max(lg["min_to"], min(amt, lg["max_to"])))
        self._sync_raise_label()

    def hero(self, action):
        e = self.engine
        local = self.hero_seat
        if e is None or e.actor != local or self.v_observe.get():
            return
        self.stop_clock()
        amt = self.v_bet.get() if action == "raise" else 0
        lg = e.legal(local)
        self.lock()
        if self._mp_mode and not self._mp_is_host:
            # Peer: send action to host; wait for game_state echo
            self._mp_send_action(action, amt)
            return
        if e.street == "preflop":
            self._record_vpip_pfr(local, action, lg)
        e.act(local, action, amt)
        # sound effects
        if action == "fold":
            _audio.play("fold")
        elif action == "call":
            _audio.play("check" if lg["can_check"] else "call")
        elif action == "raise":
            if e.players[local].all_in and local not in self._allin_announced:
                self._allin_announced.add(local)
                _audio.play("allin")
            else:
                _audio.play("raise_sound")
        self.flush_log()
        self.redraw()
        self.root.after(max(80, self.delay // 3), self.loop)

    def _hotkey(self, ev):
        if isinstance(self.root.focus_get(), (tk.Entry, tk.Spinbox, ttk.Combobox)):
            return
        k = ev.keysym.lower()
        if k in ("n", "space"):
            if str(self.b_next["state"]) == "normal":
                self.deal()
            return
        if self.engine is None or self.engine.actor != self.hero_seat:
            return
        if k == "f" and str(self.b_fold["state"]) == "normal":
            self.hero("fold")
        elif k in ("c",) and str(self.b_call["state"]) == "normal":
            self.hero("call")
        elif k in ("r", "b") and str(self.b_raise["state"]) == "normal":
            self.hero("raise")

    # ------------------------------------------------------------ showdown

    def showdown(self, runs=1, tabled=False):
        if self._defer(lambda r=runs, tb=tabled:
                       self.showdown(runs=r, tabled=tb)):
            return
        e = self.engine
        local = self.hero_seat
        self.lock()
        self.stop_clock()
        res = e.settle(runs=runs, force_tabled=tabled)
        self.result = res
        self.flush_log()
        self.hand_logger.on_settle(res, e)
        for w_idx in res.get("winners", set()):
            self.session_stats.record_win(w_idx)
        self.hand_over = True

        alive = [p for p in e.players if p.in_seat and not p.folded]
        mode = self.v_reveal.get()
        if len(alive) > 1:
            if mode == "Everyone":
                self.reveal = {p.idx for p in e.players if p.in_seat}
            elif mode == "Winner only":
                self.reveal = set(res["winners"])
            else:                                # Realistic (muck losers)
                self.reveal = set(res["shown"])
        else:
            self.reveal = set()

        for pt in res["pots"]:
            for r_idx, run in enumerate(pt.get("runs", [])):
                if r_idx >= len(res["runs"]):
                    continue
                info = res["runs"][r_idx]
                for w in run["winners"]:
                    if w in info["best"]:
                        self.highlight |= {(c.v, c.s)
                                           for c in info["best"][w]}

        lines = []
        nruns = len(res["runs"])
        for p in sorted(alive, key=lambda q: -q.won):
            tag = "WIN " if p.idx in res["winners"] else "    "
            nm = "You" if p.idx == local else p.name
            if p.idx in res.get("mucked", ()):
                lines.append(f"    {nm:<4} mucks")
            elif nruns >= 2:
                names = "/".join(hand_name(r["scores"][p.idx])
                                 for r in res["runs"])
                lines.append(f"{tag}{nm:<4} {names}"
                             + (f"  +{p.won}" if p.won else ""))
            elif nruns == 1:
                sc = res["runs"][0]["scores"].get(p.idx)
                lines.append(f"{tag}{nm:<4} {hand_name(sc)}"
                             + (f"  +{p.won}" if p.won else ""))
            else:
                lines.append(f"{tag}{nm:<4} wins uncontested  +{p.won}")
        self.l_show.config(text="\n".join(lines) if lines else "-")

        if local in res["winners"]:
            self.l_status.config(text=f"You win {e.players[local].won}.")
            _audio.play("win")
            # update persistent bankroll with winnings
            won = e.players[local].won
            if won > 0:
                cfg.set('bankroll', cfg.get('bankroll') + won)
            self.update_xp(pots_won=1)
        elif not e.players[local].in_seat:
            if e.players[local].sitting_out or e.players[local].wait_for_bb:
                self.l_status.config(text="Sitting out.")
            else:
                self.l_status.config(text="You're out — the table plays on.")
        elif e.players[local].folded:
            self.l_status.config(text="You folded. Next hand?")
        else:
            self.l_status.config(text="You lose the pot.")
            if self._last_hero_eq > 0.70:
                _audio.play("bad_beat")
        self.l_hint.config(text="")

        if (self.v_rabbit.get() and len(e.board) < 5
                and self.v_mode.get() == "Cash"):
            self.b_rabbit.config(state="normal")
        if self.v_mode.get() == "Cash":
            hero = e.players[local]
            cap_bb = self.table_rules.get("buyin_max_bb", 100)
            if hero.stack + hero.total < cap_bb * e.bb:
                self.b_add.config(state="normal")
        self.redraw()
        if self._mp_mode and self._mp_is_host:
            self._mp_session.broadcast_game_state()

        alive_next = [p for p in e.players if p.stack > 0]
        if len(alive_next) < 2:
            self.root.after(self.delay * 2,
                            lambda: self.finish_game(alive_next))
            return

        hero = e.players[local]
        hero_idle = (hero.stack <= 0 and self.v_mode.get() != "Cash") \
            or hero.sitting_out or hero.wait_for_bb
        self.b_next.config(state="normal")
        if self._mp_mode and not self._mp_is_host:
            self.b_next.config(state="disabled")   # peer cannot initiate next deal
        elif self.v_auto.get() or self.v_observe.get() or hero_idle \
                or (hero.stack <= 0 and self.v_mode.get() == "Cash"):
            self.root.after(max(900, self.delay * 3), self.deal)


    # --------------------------------------------------------------- clock

    def start_clock(self):
        self.stop_clock()
        e = self.engine
        if (not self.v_clock.get() or self.v_observe.get() or e is None
                or e.players[self.hero_seat].sitting_out):
            return
        self.clock_phase = "base"
        self.clock_until = time.time() + CLOCK_BASE
        self._clock_tick()

    def stop_clock(self):
        if self.clock_job is not None:
            try:
                self.root.after_cancel(self.clock_job)
            except Exception:
                pass
            self.clock_job = None
        if self.clock_phase == "bank":
            self.bank = max(0.0, self.clock_until - time.time())
        self.clock_phase = "off"
        if hasattr(self, "l_clock"):
            self.l_clock.config(text="")

    def _clock_tick(self):
        e = self.engine
        if e is None or e.actor != self.hero_seat or self.clock_phase == "off":
            return
        now = time.time()
        left = self.clock_until - now
        if left <= 0:
            if self.clock_phase == "base" and self.bank > 0.5:
                self.clock_phase = "bank"
                self.clock_until = now + self.bank
                left = self.bank
            else:
                if self.clock_phase == "bank":
                    self.bank = 0.0
                self.clock_phase = "off"
                self.l_clock.config(text="0:00")
                lg = e.legal(self.hero_seat)
                if lg["can_check"]:
                    self.say("check", "Clock ran out — checked.")
                    self._forced("call")
                else:
                    self.say("fold", "Clock ran out — folded.")
                    self._forced("fold")
                return
        m, s = divmod(int(left + 0.999), 60)
        txt = f"{m}:{s:02d}"
        if self.clock_phase == "base" and self.bank > 0.5:
            bm, bs = divmod(int(self.bank), 60)
            txt += f"  \u00b7  bank {bm}:{bs:02d}"
        elif self.clock_phase == "bank":
            txt = f"bank {txt}"
        self.l_clock.config(text=txt)
        self.clock_job = self.root.after(200, self._clock_tick)

    def _level_tick(self, gen):
        e = self.engine
        if (gen != self.level_gen or e is None or self.game_over
                or self.v_mode.get() == "Cash"):
            return
        mins = max(1, self.v_lvlmin.get())
        if self.level_idx >= len(BLIND_LEVELS) - 1:
            suffix = ""
        else:
            nxt = ((self.level_idx + 1) * mins * 60
                   - (time.time() - self.level_started))
            m, s = divmod(max(0, int(nxt)), 60)
            suffix = f"  \u00b7  next {m}:{s:02d}"
        self.l_blinds.config(
            text=f"Blinds {e.sb}/{e.bb}"
                 + (f" \u00b7 ante {e.bb}" if e.bb_ante else "") + suffix)
        self.root.after(1000, lambda: self._level_tick(gen))

    def _draw_tournament_overlay(self):
        """Draw tournament info (blinds, countdown, rank) in the canvas
        top-right corner.  No-ops when not in tournament mode."""
        if self.v_mode.get() != "Tournament" or self.engine is None:
            self._tourn_overlay_ids = []
            return
        e = self.engine
        cv = self.cv
        t = self.theme
        W = max(cv.winfo_width(), 600)

        # Countdown to next level
        mins = max(1, self.v_lvlmin.get())
        if self.level_idx < len(BLIND_LEVELS) - 1:
            nxt_secs = max(0.0, ((self.level_idx + 1) * mins * 60
                                 - (time.time() - self.level_started)))
            m, s = divmod(int(nxt_secs), 60)
            countdown = f"Next level: {m}:{s:02d}"
        else:
            countdown = "Final level"

        # Rank (1 = chip leader) among all players
        ranked = sorted(e.players, key=lambda p: p.stack, reverse=True)
        rank = next((i + 1 for i, p in enumerate(ranked) if p.idx == self.hero_seat),
                    "-")
        total = len(e.players)

        # Box coordinates (top-right corner)
        ox2, oy1 = W - 12, 12
        ox1, oy2 = ox2 - 200, oy1 + 80

        ids = []
        ids.append(rrect(cv, ox1, oy1, ox2, oy2, 8,
                         fill=t["panel"], outline=t["dim"], width=1,
                         stipple="gray50"))
        ids.append(cv.create_text(ox1 + 10, oy1 + 14, anchor="w",
                                  text=f"Blinds {e.sb}/{e.bb}",
                                  fill=t["gold"],
                                  font=("Segoe UI", 9, "bold")))
        ids.append(cv.create_text(ox1 + 10, oy1 + 36, anchor="w",
                                  text=countdown,
                                  fill=t["text"],
                                  font=("Segoe UI", 9)))
        ids.append(cv.create_text(ox1 + 10, oy1 + 58, anchor="w",
                                  text=f"Rank: {rank}/{total}",
                                  fill=t["accent"],
                                  font=("Segoe UI", 9)))
        self._tourn_overlay_ids = ids

    def _tick_tournament_overlay(self, gen):
        """Refresh just the tournament overlay canvas items every second."""
        if (gen != self._tourn_tick_gen or self.engine is None
                or self.game_over
                or self.v_mode.get() != "Tournament"):
            return
        for iid in getattr(self, '_tourn_overlay_ids', []):
            try:
                self.cv.delete(iid)
            except Exception:
                pass
        self._tourn_overlay_ids = []
        self._draw_tournament_overlay()
        self.root.after(1000, lambda: self._tick_tournament_overlay(gen))

    # ------------------------------------------------------- table actions

    def toggle_sit(self):
        e = self.engine
        if e is None or self.game_over:
            return
        local = self.hero_seat
        hero = e.players[local]
        if not hero.sitting_out:
            e.sit_out(local)
            self.b_sit.config(text="I'm back")
            self.say("fold", "You sit out.")
        else:
            if self.v_mode.get() == "Cash" and (hero.owes_bb or hero.owes_sb):
                post = messagebox.askyesno(
                    "Return", "Post the missed blinds now?\n"
                              "(No = wait for the big blind)")
                e.sit_in(local, post_now=post)
            else:
                e.sit_in(local)
            self.b_sit.config(text="Sit out")
            self.say("hand", "You're back.")

    def add_chips_dialog(self):
        e = self.engine
        if e is None or not self.hand_over or self.v_mode.get() != "Cash":
            return
        local = self.hero_seat
        hero = e.players[local]
        cap = self.table_rules.get("buyin_max_bb", 100) * e.bb - hero.stack
        if cap <= 0:
            messagebox.showinfo("Add chips", "You're at the table max.")
            return
        amt = simpledialog.askinteger(
            "Add chips", f"Add how much? (max {cap:,})",
            minvalue=1, maxvalue=cap,
            initialvalue=min(cap, self.buyin))
        if amt and e.add_chips(local, amt):
            self.say("pot", f"You add {amt:,}.")
            self.redraw()

    def toggle_straddle(self):
        self.straddle_armed = not self.straddle_armed
        self.b_straddle.config(
            text="Straddle: on" if self.straddle_armed else "Straddle: off")

    def rabbit(self):
        e = self.engine
        if (e is None or not self.hand_over or not self.v_rabbit.get()
                or len(e.board) >= 5):
            return
        self.rabbit_cards = e.peek_runout()
        self.b_rabbit.config(state="disabled")
        self.say("street", "Rabbit: "
                 + " ".join(str(c) for c in self.rabbit_cards))
        self.redraw()

    # ---------------------------------------------------- EV cashout

    def _maybe_offer_cashout(self, e):
        """Offer a 1%-fee EV cashout when the hero is all-in with ≥55% equity
        and the pot ≥ 10 BB.  Returns True if accepted (hand settled),
        False otherwise.  Must be called from the main (Tkinter) thread."""
        if self._cashout_offered:
            return False
        local = self.hero_seat
        hero = e.players[local]
        if not hero.all_in or hero.folded or not hero.in_seat:
            return False
        if self.v_observe.get() or hero.sitting_out:
            return False
        pot = e.pot
        if pot < 10 * e.bb:
            return False
        opp = [p for p in e.contested() if p.idx != local]
        n_opp = len(opp)
        if n_opp == 0:
            return False

        # Compute hero equity synchronously (small sim count for speed)
        from .engine import equity as _equity
        result = _equity(list(hero.hole), list(e.board),
                         n_opp, 800, random.Random())
        if result is None:
            return False
        _, _, hero_eq = result
        if hero_eq < 0.55:
            return False

        self._cashout_offered = True
        gross = int(hero_eq * pot)
        net = int(gross * 0.99)

        msg = (f"Cash out for {net:,} chips?\n\n"
               f"Your equity: {hero_eq:.0%} × {pot:,} pot = {gross:,} chips\n"
               f"After 1% fee: {net:,} chips\n\n"
               f"Yes = take the money now\n"
               f"No  = run it out")
        if not messagebox.askyesno("EV Cashout", msg):
            return False

        # --- Award chips ---
        hero.stack += net
        hero.won = net
        remaining = pot - net
        per_opp, leftover = divmod(remaining, n_opp) if n_opp else (0, 0)
        for k, opp_p in enumerate(opp):
            share = per_opp + (1 if k < leftover else 0)
            opp_p.stack += share
            opp_p.won = share

        # Clear all betting state (bypassing engine.settle)
        for p in e.players:
            p.total_live = 0
            p.total_dead = 0
            p.bet = 0
            p.all_in = (p.stack == 0)
        e.street = "idle"

        self.hand_over = True
        self.result = {
            "winners": {local}, "pots": [{"amount": pot, "eligible": [local], "runs": []}],
            "runs": [], "shown": {local}, "mucked": set(),
            "order": [local], "refund": None, "tabled": False,
        }
        # H-8: record the cashout in hand history (was previously bypassed)
        self.hand_logger.on_settle(self.result, e)
        self.l_status.config(text=f"Cashed out for {net:,}  "
                                   f"({hero_eq:.0%} equity).")
        self.l_hint.config(text="")
        self.b_next.config(state="normal")
        self.say("pot",
                 f"EV cashout: You take {net:,} "
                 f"({hero_eq:.0%} × {pot:,} − 1% fee)")
        # update persistent bankroll
        if net > 0:
            cfg.set('bankroll', cfg.get('bankroll') + net)
        self.redraw()

        alive_next = [p for p in e.players if p.stack > 0]
        if len(alive_next) < 2:
            self.root.after(self.delay * 2,
                            lambda: self.finish_game(alive_next))
        elif self.v_auto.get() or self.v_observe.get():
            self.root.after(max(900, self.delay * 3), self.deal)
        return True

    # ------------------------------------------------------- persistence

    def _bucket(self, scope):
        out = {}
        for key, var in self._varmap.items():
            if cfg.SPEC[key]["scope"] != scope:
                continue
            try:
                out[key] = var.get()
            except tk.TclError:
                pass
        return out

    def save_config(self):
        # Load existing first so onboarding keys (nickname, avatar_*) are
        # preserved — they're not in _varmap and would otherwise be wiped.
        existing = cfg.load()
        client_data = {**existing["client"], **self._bucket(cfg.CLIENT)}
        cfg.save(client_data, self._bucket(cfg.TABLE_RULE))

    def aids_ok(self):
        """Training aids (hints, live equity) are a table rule; the client
        toggles only apply where the table allows them."""
        return bool(self.table_rules.get("training_aids", True))

    # ------------------------------------------------------------ settings

    THEMEABLE = ("background", "foreground", "activebackground",
                 "activeforeground", "selectcolor", "troughcolor",
                 "insertbackground", "highlightbackground")

    def _defer(self, fn):
        """While the settings pause is up, requeue game callbacks."""
        if self.paused:
            self.root.after(200, fn)
            return True
        return False

    def _walk(self, w):
        yield w
        for c in w.winfo_children():
            yield from self._walk(c)

    def apply_theme(self, name):
        old = self.theme
        new = THEMES[name]
        if new is old:
            return
        remap = {old[k]: new[k] for k in new if k in old}
        roots = [self.root]
        if self.settings_win is not None and self.settings_win.winfo_exists():
            roots.append(self.settings_win)
        for r in roots:
            for w in self._walk(r):
                for opt in self.THEMEABLE:
                    try:
                        cur = str(w.cget(opt))
                    except tk.TclError:
                        continue
                    if cur in remap:
                        try:
                            w.config(**{opt: remap[cur]})
                        except tk.TclError:
                            pass
        self.theme = new
        # palette values can collide across roles (accent == win in one
        # theme, accent == gold in the other); pin the widgets whose role
        # matters after the generic remap
        for w, opt, key in ((self.b_next, "bg", "accent"),
                            (self.b_deal, "bg", "accent"),
                            (self.b_deal, "activebackground", "gold"),
                            (self.l_status, "fg", "accent"),
                            (self.l_clock, "fg", "accent"),
                            (self.l_blinds, "fg", "gold"),
                            (self.l_stack, "fg", "gold"),
                            (self.l_hint, "fg", "gold")):
            try:
                w.config(**{opt: new[key]})
            except tk.TclError:
                pass
        for tag, key in (("hand", "accent"), ("street", "gold"),
                         ("fold", "dim"), ("raise", "loss"),
                         ("bet", "text"), ("check", "dim"),
                         ("pot", "win"), ("blind", "dim"),
                         ("you", "accent"), ("show", "text")):
            self.log.tag_config(tag, foreground=new[key])
        self.redraw()
        self.draw_equity()

    def _esc(self, _ev=None):
        if (self.settings_win is not None
                and self.settings_win.winfo_exists()):
            self.close_settings()
        elif (self.engine is not None and not self.game_over
                and self.table.winfo_ismapped()):
            self.open_settings()

    def open_settings(self):
        if (self.settings_win is not None
                and self.settings_win.winfo_exists()):
            self.settings_win.lift()
            return
        self.paused = True
        self.stop_clock()
        t = self.theme
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("Settings")
        win.configure(bg=t["panel"])
        win.transient(self.root)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self.close_settings)
        win.bind("<Escape>", lambda _e: self.close_settings())
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + self.root.winfo_width() // 2 - 280
        y = self.root.winfo_rooty() + self.root.winfo_height() // 2 - 280
        win.geometry(f"560x560+{max(0, x)}+{max(0, y)}")

        tk.Label(win, text="SETTINGS", bg=t["panel"], fg=t["accent"],
                 font=("Segoe UI", 15, "bold")).pack(pady=(14, 6))

        body = tk.Frame(win, bg=t["panel"])
        body.pack(fill="both", expand=True, padx=14)
        nav = tk.Frame(body, bg=t["panel"])
        nav.pack(side="left", fill="y", padx=(0, 12))
        pane = tk.Frame(body, bg=t["bg"])
        pane.pack(side="left", fill="both", expand=True)

        pages = {}
        navbtns = {}

        def page(name):
            f = tk.Frame(pane, bg=t["bg"])
            pages[name] = f
            return f

        def show(name):
            for f in pages.values():
                f.pack_forget()
            pages[name].pack(fill="both", expand=True, padx=14, pady=10)
            for nm, btn in navbtns.items():
                btn.config(fg=t["accent"] if nm == name else t["dim"])

        for name in ("Client", "Table rules"):
            b = tk.Button(nav, text=name.upper(), relief="flat",
                          bg=t["panel"], fg=t["dim"],
                          activebackground=t["panel"],
                          activeforeground=t["accent"],
                          font=("Segoe UI", 10, "bold"), anchor="w",
                          cursor="hand2", width=11,
                          command=lambda n=name: show(n))
            b.pack(anchor="w", pady=2)
            navbtns[name] = b

        def lab(parent, txt, top=8):
            tk.Label(parent, text=txt, bg=t["bg"], fg=t["text"],
                     font=("Segoe UI", 9)).pack(anchor="w", pady=(top, 1))

        def check(parent, key, on_change=None):
            s = cfg.SPEC[key]
            b = tk.Checkbutton(parent, text=s["label"],
                               variable=self._varmap[key], bg=t["bg"],
                               fg=t["text"], selectcolor=t["panel"],
                               activebackground=t["bg"],
                               activeforeground=t["text"],
                               font=("Segoe UI", 9),
                               command=on_change)
            b.pack(anchor="w", pady=1)
            return b

        def choice(parent, key, on_change=None, width=22):
            s = cfg.SPEC[key]
            lab(parent, s["label"])
            c = ttk.Combobox(parent, textvariable=self._varmap[key],
                             values=s["choices"], state="readonly",
                             width=width)
            c.pack(anchor="w")
            if on_change:
                c.bind("<<ComboboxSelected>>", lambda _e: on_change())
            return c

        # ---- CLIENT: this machine only, persisted -------------------
        d = page("Client")
        lab(d, cfg.SPEC["theme"]["label"], 4)
        cb_theme = ttk.Combobox(d, values=list(THEMES), state="readonly",
                                width=20)
        cb_theme.set(self.v_theme.get())
        cb_theme.pack(anchor="w")
        cb_theme.bind("<<ComboboxSelected>>",
                      lambda _e: (self.v_theme.set(cb_theme.get()),
                                  self.apply_theme(cb_theme.get())))
        choice(d, "speed", width=20)
        choice(d, "reveal")
        b_hints = check(d, "hints")
        b_odds = check(d, "odds")
        check(d, "auto_deal")
        check(d, "clock_on")
        check(d, "ai_topup")

        # Sound settings
        lab(d, "Audio", 10)
        def _on_sounds_toggle():
            _audio.set_enabled(self.v_sounds_enabled.get())
        tk.Checkbutton(d, text=cfg.SPEC["sounds_enabled"]["label"],
                       variable=self.v_sounds_enabled, bg=t["bg"],
                       fg=t["text"], selectcolor=t["panel"],
                       activebackground=t["bg"], activeforeground=t["text"],
                       font=("Segoe UI", 9),
                       command=_on_sounds_toggle).pack(anchor="w", pady=1)
        vol_row = tk.Frame(d, bg=t["bg"])
        vol_row.pack(anchor="w", pady=(2, 0))
        tk.Label(vol_row, text="Volume:", bg=t["bg"], fg=t["text"],
                 font=("Segoe UI", 9)).pack(side="left")
        def _on_vol_change(_v=None):
            _audio.set_volume(self.v_sound_volume.get() / 100)
        tk.Scale(vol_row, variable=self.v_sound_volume,
                 from_=0, to=100, orient="horizontal", length=160,
                 showvalue=True, bg=t["bg"], fg=t["text"],
                 troughcolor=t["panel"], highlightthickness=0,
                 activebackground=t["accent"], relief="flat",
                 command=_on_vol_change).pack(side="left", padx=(6, 0))

        tk.Label(d, text="Saved to " + str(cfg.config_path()),
                 bg=t["bg"], fg=t["dim"], wraplength=250, justify="left",
                 font=("Segoe UI", 7)).pack(anchor="w", pady=(10, 0))

        # ---- TABLE RULES: the contract every seat plays under -------
        tb = page("Table rules")
        l_hash = tk.Label(tb, text="", bg=t["bg"], fg=t["gold"],
                          font=("Consolas", 9, "bold"))
        l_hash.pack(anchor="w", pady=(2, 4))
        r = self.table_rules
        contract = (f"{r['mode']} · {r['structure']} · "
                    f"blinds {r['sb']}/{r['bb']}\n"
                    f"{r['players']} seats · stack {r['stack']:,} · "
                    f"buy-in {r['buyin_min_bb']}-{r['buyin_max_bb']} BB\n"
                    f"clock {r['clock_base']}s · bank {r['bank_start']}s "
                    f"(+{r['bank_topup']}/orbit, cap {r['bank_cap']})"
                    + (f"\nBB ante · {r['level_minutes']} min levels"
                       if r["mode"] == "Tournament" else ""))
        tk.Label(tb, text=contract, bg=t["bg"], fg=t["dim"],
                 justify="left", font=("Segoe UI", 8)).pack(anchor="w")

        def sync_aids():
            state = "normal" if self.v_training.get() else "disabled"
            b_hints.config(state=state)
            b_odds.config(state=state)

        def amend():
            """Live table-rule change (single-player only): update the
            contract and its hash. At a live table this needs unanimous
            signed consent instead."""
            self.table_rules = cfg.table_rules(
                **self._bucket(cfg.TABLE_RULE))
            l_hash.config(
                text=f"Table #{cfg.rules_hash(self.table_rules)}")
            base = self.l_summary.cget("text").rsplit("  ·  table #", 1)[0]
            self.l_summary.config(
                text=base + f"  ·  table #{cfg.rules_hash(self.table_rules)}")
            sync_aids()

        lab(tb, "Amendable between hands", 10)
        live_widgets = [
            choice(tb, "rit", on_change=amend),
            check(tb, "straddles", on_change=amend),
            check(tb, "rabbit", on_change=amend),
            check(tb, "training_aids", on_change=amend),
        ]
        if self.joined_table:
            for w in live_widgets:
                w.config(state="disabled")
            tk.Label(tb, text="Fixed by the join code at a live table.",
                     bg=t["bg"], fg=t["dim"],
                     font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 0))
        else:
            tk.Label(tb, text="Everything else is fixed for this game;\n"
                              "start a new game to change it.",
                     bg=t["bg"], fg=t["dim"], justify="left",
                     font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 0))
        amend()
        sync_aids()

        show("Client")

        bar = tk.Frame(win, bg=t["panel"])
        bar.pack(fill="x", padx=14, pady=12)
        tk.Button(bar, text="Quit to desktop", relief="flat",
                  bg="#5c2333", fg="#ffe8ee", font=("Segoe UI", 9, "bold"),
                  cursor="hand2", padx=10, pady=4,
                  command=self.quit_to_desktop).pack(side="left")
        tk.Button(bar, text="Resume", relief="flat", bg=t["accent"],
                  fg="#04040c", font=("Segoe UI", 10, "bold"),
                  cursor="hand2", padx=16, pady=4,
                  command=self.close_settings).pack(side="right")
        tk.Button(bar, text="New game\u2026", relief="flat", bg=t["btn"],
                  fg=t["btn_text"], font=("Segoe UI", 9),
                  cursor="hand2", padx=10, pady=4,
                  command=self.abandon_to_setup).pack(side="right", padx=8)
        try:
            win.grab_set()
        except tk.TclError:
            pass                        # not yet viewable; transient is enough

    def close_settings(self):
        self.save_config()
        if self.settings_win is not None:
            try:
                self.settings_win.grab_release()
                self.settings_win.destroy()
            except tk.TclError:
                pass
            self.settings_win = None
        self.paused = False
        if not self.v_hint.get():
            self.l_hint.config(text="")
        if not self.v_odds.get():
            self.eq_text = "-"
            self.eq_bars = (0, 0, 1)
            self.draw_equity()
        e = self.engine
        cash = self.v_mode.get() == "Cash"
        can_straddle = (cash and self.v_straddles.get()
                        and self.v_struct.get() != "Fixed-Limit"
                        and e is not None and len(e.players) >= 3)
        self.b_straddle.config(
            state="normal" if can_straddle else "disabled")
        if (e is not None and e.actor == self.hero_seat and not self.hand_over
                and not self.v_observe.get()
                and not e.players[self.hero_seat].sitting_out):
            self.start_clock()

    def _sync_resume(self):
        if self.engine is not None and not self.game_over:
            self.b_resume.grid()
        else:
            self.b_resume.grid_remove()

    def abandon_to_setup(self):
        if self.engine is not None and not self.game_over:
            if not messagebox.askyesno(
                    "New game", "Abandon the current game?",
                    parent=self.settings_win):
                return
        self.close_settings()
        self._sync_resume()
        self._show(self.setup)

    def quit_to_desktop(self):
        if messagebox.askyesno("Quit", "Quit to desktop?",
                               parent=self.settings_win):
            self.save_config()
            self.root.destroy()

    # -------------------------------------------------------------- equity

    def start_equity(self):
        e = self.engine
        self.eq_gen += 1
        gen = self.eq_gen
        hero = e.players[self.hero_seat]
        if (not self.v_odds.get() or not self.aids_ok()
                or not hero.hole or hero.folded):
            self.eq_text = "-"
            self.eq_bars = (0, 0, 1)
            return
        opp = len([p for p in e.contested() if p.idx != self.hero_seat])
        if opp == 0:
            self.eq_text = "You're the only one left."
            self.eq_bars = (1, 0, 0)
            return
        hole = list(hero.hole)
        board = list(e.board)
        sims = 3000 if len(board) >= 3 else 1600

        def work():
            r = equity(hole, board, opp, sims, random.Random())
            self.root.after(0, lambda: self._equity_done(gen, r, board,
                                                         hole, opp))
        threading.Thread(target=work, daemon=True).start()

    def _equity_done(self, gen, r, board, hole, opp):
        if gen != self.eq_gen or r is None:
            return
        win, tie, eq = r
        made = hand_name(evaluate(hole + board)) if len(board) >= 3 else "-"
        self.eq_bars = (win, tie, max(0.0, 1 - win - tie))
        self.eq_text = (f"{eq*100:.0f}% vs {opp} "
                        f"({'opponent' if opp == 1 else 'opponents'})   "
                        f"win {win*100:.0f} / tie {tie*100:.0f}\n"
                        f"{'Holding: ' + made if made != '-' else ''}")
        self._last_hero_eq = eq   # track for bad-beat detection
        self.draw_equity()

    def draw_equity(self):
        t = self.theme
        c = self.eq_cv
        c.delete("all")
        w = max(1, c.winfo_width())
        win, tie, lose = self.eq_bars
        x = 0
        for frac, col in ((win, t["win"]), (tie, t["gold"]), (lose, t["loss"])):
            ww = w * frac
            if ww > 0.5:
                c.create_rectangle(x, 2, x + ww, 14, fill=col, width=0)
            x += ww
        self.l_eq.config(text=self.eq_text)

    # -------------------------------------------------------------- drawing

    def card(self, x, y, w, h, card, face_up=True, dim=False, glow=False):
        t = self.theme
        cv = self.cv
        if glow:
            rrect(cv, x - 3, y - 3, x + w + 3, y + h + 3, 7,
                  fill=t["win"], outline="")
        if not face_up:
            rrect(cv, x, y, x + w, y + h, 6,
                  fill=t["back"], outline=t["back2"], width=2)
            step = 7
            for k in range(-int(h / step), int(w / step) + 1):
                cv.create_line(x + k * step + 4, y + 4,
                               x + k * step + h - 8, y + h - 4,
                               fill=t["back2"], width=1)
            rrect(cv, x + 4, y + 4, x + w - 4, y + h - 4, 4,
                  fill="", outline=t["back2"], width=1)
            return
        if card is None:
            rrect(cv, x, y, x + w, y + h, 6, fill="", outline=t["rail"],
                  width=1, dash=(3, 4))
            return
        face = "#dcdce4" if dim else t["card"]
        col = self._suit_color(card.s)
        if dim:
            col = t["dim"]
        rrect(cv, x, y, x + w, y + h, 6, fill=face,
              outline=t["card_edge"], width=1)
        big = max(11, int(h * 0.34))
        small = max(8, int(h * 0.20))
        cv.create_text(x + w * 0.30, y + h * 0.26, text=card.rank, fill=col,
                       font=("Segoe UI", small, "bold"))
        cv.create_text(x + w * 0.58, y + h * 0.66, text=card.suit, fill=col,
                       font=("Segoe UI Symbol", big))

    def chips(self, x, y, amount):
        if amount <= 0:
            return
        t = self.theme
        cv = self.cv
        txt = str(amount)
        w = 16 + 7 * len(txt)
        rrect(cv, x - w / 2, y - 9, x + w / 2, y + 9, 9,
              fill=t["panel"], outline=t["chip"], width=1)
        cv.create_oval(x - w / 2 + 3, y - 5, x - w / 2 + 13, y + 5,
                       fill=t["chip"], outline="")
        cv.create_text(x + 5, y, text=txt, fill=t["chip"],
                       font=("Segoe UI", 8, "bold"))

    def redraw(self):
        e = self.engine
        cv = self.cv
        t = self.theme
        cv.delete("all")
        self._seat_photo_refs = []   # release previous-frame PhotoImage refs
        if e is None:
            return
        W = max(cv.winfo_width(), 600)
        H = max(cv.winfo_height(), 420)
        cx, cy = W / 2, H * 0.46
        rx, ry = W * 0.36, H * 0.27

        # felt
        cv.create_oval(cx - rx - 16, cy - ry - 16, cx + rx + 16, cy + ry + 16,
                       fill=t["rail"], outline="")
        _felt_col = self.v_felt_color.get() or t["felt"]
        cv.create_oval(cx - rx, cy - ry, cx + rx, cy + ry,
                       fill=_felt_col, outline=t["felt_edge"], width=2)

        # board (one row, or two smaller rows when the pot ran twice)
        if e.board2:
            cw, ch, gap = 46, 65, 7
            bx = cx - (5 * cw + 4 * gap) / 2
            for r, brd in enumerate((e.board, e.board2)):
                by_r = cy - ch - 12 + r * (ch + 10)
                cv.create_text(bx - 14, by_r + ch / 2, text=f"R{r+1}",
                               fill=t["dim"], font=("Segoe UI", 8, "bold"))
                for k in range(5):
                    c = brd[k] if k < len(brd) else None
                    self.card(bx + k * (cw + gap), by_r, cw, ch, c,
                              face_up=True,
                              glow=(c is not None
                                    and (c.v, c.s) in self.highlight))
            by = cy + 8
            ch = 65
        else:
            cw, ch = 58, 82
            gap = 8
            bx = cx - (5 * cw + 4 * gap) / 2
            by = cy - ch / 2 - 6
            for k in range(5):
                c = e.board[k] if k < len(e.board) else None
                if c is None and self.rabbit_cards:
                    kk = k - len(e.board)
                    if 0 <= kk < len(self.rabbit_cards):
                        self.card(bx + k * (cw + gap), by, cw, ch,
                                  self.rabbit_cards[kk], face_up=True,
                                  dim=True)
                        continue
                self.card(bx + k * (cw + gap), by, cw, ch, c, face_up=True,
                          glow=(c is not None
                                and (c.v, c.s) in self.highlight))

        pot = e.pot
        cv.create_text(cx, by + ch + 22, text=f"POT  {pot}", fill=t["accent"],
                       font=("Segoe UI", 15, "bold"))
        if self.result and len(self.result["pots"]) > 1:
            parts = " · ".join(f"{p['amount']}" for p in self.result["pots"])
            cv.create_text(cx, by + ch + 40, text=f"side pots: {parts}",
                           fill=t["dim"], font=("Segoe UI", 8))

        n = len(e.players)
        srx, sry = rx * 1.08, ry * 1.32
        for p in e.players:
            ang = math.pi / 2 - p.idx * 2 * math.pi / n
            sx = cx + srx * math.cos(ang)
            sy = cy + sry * math.sin(ang)
            self.seat(p, sx, sy, cx, cy)

        # dealer button
        for p in e.players:
            if p.idx == e.button and p.in_seat:
                ang = math.pi / 2 - p.idx * 2 * math.pi / n
                sx = cx + srx * 0.74 * math.cos(ang)
                sy = cy + sry * 0.74 * math.sin(ang)
                cv.create_oval(sx - 11, sy - 11, sx + 11, sy + 11,
                               fill="#f2f2e6", outline=t["dim"], width=1)
                cv.create_text(sx, sy, text="D", fill="#1a1a1a",
                               font=("Segoe UI", 9, "bold"))

        hero = e.players[self.hero_seat]
        self.l_stack.config(text=f"{hero.stack:,}"
                            + ("  ALL-IN" if hero.all_in else ""))
        self.draw_equity()
        self._draw_tournament_overlay()
        if getattr(self, "_mp_game_paused", False):
            self._mp_show_pause_overlay()

    def seat(self, p, x, y, cx, cy):
        e = self.engine
        cv = self.cv
        t = self.theme
        hero = p.idx == self.hero_seat
        w, h = (150, 104) if hero else (132, 92)

        if not p.in_seat:
            rrect(cv, x - w / 2, y - h / 2, x + w / 2, y + h / 2, 10,
                  fill=t["seat_fold"], outline=t["rail"], width=1)
            if p.stack > 0:
                nm = "You" if hero else p.name
                what = ("waiting for BB" if p.wait_for_bb else "sitting out")
                cv.create_text(x, y - 8, text=f"{nm}  {p.stack:,}",
                               fill=t["dim"], font=("Segoe UI", 9, "bold"))
                cv.create_text(x, y + 10, text=what, fill=t["dim"],
                               font=("Segoe UI", 8))
            else:
                cv.create_text(x, y, text="empty", fill=t["dim"],
                               font=("Segoe UI", 9))
            return

        active = e.actor == p.idx and not self.hand_over
        winner = self.result and p.idx in self.result["winners"]
        if p.folded:
            fill, edge, ew = t["seat_fold"], t["rail"], 1
        elif winner:
            fill, edge, ew = t["seat_hero"] if hero else t["seat"], t["win"], 3
        elif active:
            fill, edge, ew = (t["seat_hero"] if hero else t["seat"],
                              t["active"], 3)
        else:
            fill, edge, ew = (t["seat_hero"] if hero else t["seat"],
                              t["rail"], 1)
        rrect(cv, x - w / 2, y - h / 2, x + w / 2, y + h / 2, 10,
              fill=fill, outline=edge, width=ew)

        # ---- avatar (28×28 px, top-left corner of the seat box) ----------
        av_size = 28
        av_pad  = 5
        av_x1   = x - w / 2 + av_pad
        av_y1   = y - h / 2 + av_pad
        av_x2   = av_x1 + av_size
        av_y2   = av_y1 + av_size
        av_cx   = (av_x1 + av_x2) / 2
        av_cy   = (av_y1 + av_y2) / 2

        avatar_drawn = False
        if hero and _PIL_OK and getattr(self, "avatar_b64", ""):
            try:
                raw = base64.b64decode(self.avatar_b64)
                pil_img = Image.open(io.BytesIO(raw)).convert("RGBA")
                pil_img = pil_img.resize((av_size, av_size), Image.LANCZOS)
                photo = _ImageTk.PhotoImage(pil_img)
                self._seat_photo_refs.append(photo)
                cv.create_image(av_cx, av_cy, image=photo, anchor="center")
                avatar_drawn = True
            except Exception:
                pass
        if not avatar_drawn:
            # Colored circle placeholder: accent tint for hero, unique per AI.
            av_color = (t.get("accent", "#00ffd0") if hero
                        else _AI_AVATAR_COLORS[p.idx % len(_AI_AVATAR_COLORS)])
            cv.create_oval(av_x1, av_y1, av_x2, av_y2,
                           fill=av_color, outline=t["panel"], width=1)
            init = "Y" if hero else p.name[:1].upper()
            cv.create_text(av_cx, av_cy, text=init,
                           fill=t["text"], font=("Segoe UI", 9, "bold"))

        # Color ring label (MP mode, opponent seats only)
        if self._mp_mode and not hero:
            _pid = self._mp_seat_to_peer.get(p.idx, "")
            if _pid:
                _nc = _notes.get(_pid).get("color", "none")
                _ring_hex = {
                    "red": "#e53935", "orange": "#fb8c00",
                    "yellow": "#fdd835", "green": "#43a047",
                    "blue": "#1e88e5", "purple": "#8e24aa",
                }.get(_nc)
                if _ring_hex:
                    _rr = av_size / 2 + 4
                    cv.create_oval(av_cx - _rr, av_cy - _rr,
                                   av_cx + _rr, av_cy + _rr,
                                   fill="", outline=_ring_hex, width=3)

        # ---- name + stack (shifted right to clear the avatar) -------------
        txt_off = av_pad + av_size + 4   # = 37 px from left edge of seat
        name = "You" if hero else p.name
        sub = f" Lv.{cfg.get('player_level')}" if hero else f"  {p.style[0]}{p.level}"
        cv.create_text(x - w / 2 + txt_off, y - h / 2 + 13, anchor="w",
                       text=name + sub,
                       fill=t["dim"] if p.folded else t["text"],
                       font=("Segoe UI", 9, "bold"))

        pos = ""
        if p.idx == e.sb_i:
            pos = "SB"
        elif p.idx == e.bb_i:
            pos = "BB"
        if p.idx == e.button:
            pos = "BTN" if not pos else pos + "/BTN"
        if pos:
            cv.create_text(x + w / 2 - 8, y - h / 2 + 13, anchor="e",
                           text=pos, fill=t["gold"], font=("Segoe UI", 7,
                                                           "bold"))

        stack_txt = "ALL-IN" if p.all_in and p.stack == 0 else f"{p.stack:,}"
        cv.create_text(x - w / 2 + txt_off, y - h / 2 + 28, anchor="w",
                       text=stack_txt,
                       fill=t["dim"] if p.folded else t["gold"],
                       font=("Segoe UI", 10, "bold"))

        # ---- session HUD stats (VPIP% · hands) ----------------------------
        hud = self.session_stats.hud_line(p.idx)
        if hud:
            # right-align against the card zone so text never overlaps cards
            cwid_hud, _chg = (40, 56) if hero else (32, 45)
            cxx_hud = x + w / 2 - (cwid_hud * 2 + 5) - 8
            hud_color = t["accent"] if hero else t["dim"]
            cv.create_text(cxx_hud - 4, y - h / 2 + 42, anchor="e",
                           text=hud,
                           fill=hud_color,
                           font=("Segoe UI", 9))

        # hole cards
        show = (hero or p.idx in self.reveal) and not p.folded
        cwid, chg = (40, 56) if hero else (32, 45)
        total = cwid * 2 + 5
        cxx = x + w / 2 - total - 8
        cyy = y - chg / 2 + 8
        for k in range(2):
            c = p.hole[k] if len(p.hole) > k else None
            if c is None:
                continue
            self.card(cxx + k * (cwid + 5), cyy, cwid, chg, c,
                      face_up=show, dim=p.folded,
                      glow=(show and (c.v, c.s) in self.highlight
                            and self.result is not None
                            and p.idx in self.result["winners"]))

        if p.last_action:
            col = t["loss"] if p.last_action.startswith(
                ("RAISE", "BET", "ALL-IN")) else t["dim"]
            cv.create_text(x - w / 2 + 10, y + h / 2 - 12, anchor="w",
                           text=p.last_action, fill=col,
                           font=("Segoe UI", 8, "bold"))
        if self.result and p.idx in self.result.get("mucked", ()):
            cv.create_text(x + w / 2 - 8, y + h / 2 - 12, anchor="e",
                           text="muck", fill=t["dim"],
                           font=("Segoe UI", 8, "bold"))

        if p.bet > 0:
            f = 0.40
            bxp = x + (cx - x) * f
            byp = y + (cy - y) * f
            self.chips(bxp, byp, p.bet)

        if self.result and p.won > 0:
            cv.create_text(x, y - h / 2 - 12, text=f"+{p.won:,}",
                           fill=t["win"], font=("Segoe UI", 11, "bold"))

        # Right-click hit region (MP mode, opponent seats only)
        if self._mp_mode and not hero and p.in_seat:
            _htag = f"seat_{p.idx}_hit"
            cv.create_rectangle(
                x - w / 2, y - h / 2, x + w / 2, y + h / 2,
                fill="", outline="", tags=(_htag,))
            cv.tag_bind(_htag, "<Button-3>",
                        lambda _ev, si=p.idx: self._seat_right_click(_ev, si))

    # ------------------------------------------------------ suit / deck helper

    def _suit_color(self, suit_idx: int) -> str:
        """Return the hex color for a card suit.

        suit_idx: 0=clubs, 1=diamonds, 2=hearts, 3=spades
        In four-color mode clubs are green and diamonds are blue.
        """
        if self.v_four_color_deck.get():
            return ["#2e7d32", "#1565c0", "#d32f2f", "#1a1a1a"][suit_idx]
        t = self.theme
        return t["red"] if suit_idx in (1, 2) else t["black"]

    # -------------------------------------------------- bet button helpers

    def _rebuild_bet_buttons(self):
        """Destroy existing bet-size buttons and rebuild from settings."""
        for b in self.size_btns:
            try:
                b.destroy()
            except Exception:
                pass
        self.size_btns = []
        t = self.theme
        raw = self.v_bet_buttons.get() if hasattr(self, "v_bet_buttons") else "0.5,1,2,3"
        fracs: list[float] = []
        for tok in raw.split(","):
            tok = tok.strip()
            try:
                fracs.append(float(tok))
            except ValueError:
                pass
        if not fracs:
            fracs = [0.5, 1.0, 2.0, 3.0]

        def _frac_label(f: float) -> str:
            if f == 0.5:
                return "1/2 pot"
            if f == 0.75:
                return "3/4 pot"
            if f == 1.0:
                return "Pot"
            n = int(f) if f == int(f) else f
            return f"{n}x pot"

        for frac in fracs:
            lbl = _frac_label(frac)
            b = tk.Button(self.sizes, text=lbl, width=6, relief="flat",
                          bg=t["btn"], fg=t["btn_text"], font=("Segoe UI", 8),
                          cursor="hand2",
                          command=lambda fr=frac: self.preset(fr))
            b.pack(side="left", padx=2)
            self.size_btns.append(b)
        # All-in is always the last button
        b_ai = tk.Button(self.sizes, text="All-in", width=6, relief="flat",
                         bg=t["btn"], fg=t["btn_text"], font=("Segoe UI", 8),
                         cursor="hand2", command=lambda: self.preset(None))
        b_ai.pack(side="left", padx=2)
        self.size_btns.append(b_ai)

    # ------------------------------------------------- preferences dialog

    def open_preferences(self):
        """Open the Preferences dialog for UI polish options."""
        t = self.theme
        win = tk.Toplevel(self.root)
        win.title("Preferences")
        win.configure(bg=t["panel"])
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        self.root.update_idletasks()
        dw, dh = 440, 340
        rx = self.root.winfo_rootx() + self.root.winfo_width()  // 2 - dw // 2
        ry = self.root.winfo_rooty() + self.root.winfo_height() // 2 - dh // 2
        win.geometry(f"{dw}x{dh}+{max(0, rx)}+{max(0, ry)}")

        tk.Label(win, text="PREFERENCES", bg=t["panel"], fg=t["accent"],
                 font=("Segoe UI", 13, "bold")).pack(pady=(12, 6))

        body = tk.Frame(win, bg=t["panel"])
        body.pack(fill="both", expand=True, padx=20)

        def _section(txt):
            tk.Label(body, text=txt, bg=t["panel"], fg=t["gold"],
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(10, 2))

        # Table section
        _section("TABLE")
        felt_row = tk.Frame(body, bg=t["panel"])
        felt_row.pack(anchor="w", pady=2)
        tk.Label(felt_row, text="Felt color:", bg=t["panel"], fg=t["text"],
                 font=("Segoe UI", 9), width=16, anchor="e").pack(side="left")
        _felt_preview = tk.Label(felt_row, width=4,
                                 bg=self.v_felt_color.get(), relief="solid")
        _felt_preview.pack(side="left", padx=(4, 0))

        def _pick_felt():
            cur = self.v_felt_color.get()
            result = colorchooser.askcolor(color=cur,
                                           title="Table Felt Color",
                                           parent=win)
            if result and result[1]:
                hex_col = result[1]
                self.v_felt_color.set(hex_col)
                cfg.set("felt_color", hex_col)
                _felt_preview.config(bg=hex_col)
                self.redraw()

        tk.Button(felt_row, text="Pick...", relief="flat",
                  bg=t["btn"], fg=t["btn_text"],
                  font=("Segoe UI", 8), cursor="hand2",
                  command=_pick_felt).pack(side="left", padx=(6, 0))

        tk.Checkbutton(
            body, text="Four-color deck  (clubs=green, diamonds=blue)",
            variable=self.v_four_color_deck, bg=t["panel"],
            fg=t["text"], selectcolor=t["bg"],
            activebackground=t["panel"], activeforeground=t["text"],
            font=("Segoe UI", 9),
            command=lambda: (cfg.set("four_color_deck",
                                     self.v_four_color_deck.get()),
                             self.redraw())
        ).pack(anchor="w", pady=2)

        # Betting section
        _section("BETTING")
        bet_row = tk.Frame(body, bg=t["panel"])
        bet_row.pack(anchor="w", pady=2)
        tk.Label(bet_row, text="Bet shortcuts:", bg=t["panel"], fg=t["text"],
                 font=("Segoe UI", 9), width=16, anchor="e").pack(side="left")
        v_bb_entry = tk.StringVar(value=self.v_bet_buttons.get())
        bet_entry = tk.Entry(bet_row, textvariable=v_bb_entry, width=22,
                             bg=t["bg"], fg=t["text"], relief="flat",
                             insertbackground=t["text"],
                             font=("Consolas", 9))
        bet_entry.pack(side="left", padx=(4, 0))
        tk.Label(body, text="Comma-separated fractions, e.g. 0.5,1,2,3",
                 bg=t["panel"], fg=t["dim"],
                 font=("Segoe UI", 8)).pack(anchor="w", padx=(138, 0))

        # Sound section
        _section("SOUND")

        def _on_sounds_toggle():
            _audio.set_enabled(self.v_sounds_enabled.get())

        tk.Checkbutton(
            body, text="Sound effects enabled",
            variable=self.v_sounds_enabled, bg=t["panel"],
            fg=t["text"], selectcolor=t["bg"],
            activebackground=t["panel"], activeforeground=t["text"],
            font=("Segoe UI", 9), command=_on_sounds_toggle
        ).pack(anchor="w", pady=2)
        vol_row2 = tk.Frame(body, bg=t["panel"])
        vol_row2.pack(anchor="w", pady=2)
        tk.Label(vol_row2, text="Volume:", bg=t["panel"], fg=t["text"],
                 font=("Segoe UI", 9), width=16, anchor="e").pack(side="left")

        def _on_vol(_v=None):
            _audio.set_volume(self.v_sound_volume.get() / 100)

        tk.Scale(vol_row2, variable=self.v_sound_volume,
                 from_=0, to=100, orient="horizontal", length=180,
                 showvalue=True, bg=t["panel"], fg=t["text"],
                 troughcolor=t["bg"], highlightthickness=0,
                 activebackground=t["accent"], relief="flat",
                 command=_on_vol).pack(side="left", padx=(4, 0))

        def _save_prefs():
            raw = v_bb_entry.get().strip()
            valid = []
            for tok in raw.split(","):
                try:
                    valid.append(str(float(tok.strip())))
                except ValueError:
                    pass
            if valid:
                clean = ",".join(valid)
                self.v_bet_buttons.set(clean)
                cfg.set("bet_buttons", clean)
                self._rebuild_bet_buttons()
            cfg.set("four_color_deck", self.v_four_color_deck.get())
            cfg.set("sounds_enabled", self.v_sounds_enabled.get())
            cfg.set("sound_volume", self.v_sound_volume.get())
            win.destroy()

        bbar = tk.Frame(win, bg=t["panel"])
        bbar.pack(fill="x", padx=20, pady=(8, 12))
        tk.Button(bbar, text="Cancel", command=win.destroy, relief="flat",
                  bg=t["btn"], fg=t["btn_text"],
                  font=("Segoe UI", 9), cursor="hand2").pack(side="left")
        tk.Button(bbar, text="Save", command=_save_prefs, relief="flat",
                  bg=t["accent"], fg="#04040c",
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  padx=12).pack(side="right")

    # -------------------------------------------------- notes viewer dialog

    def open_notes_viewer(self):
        """Open a viewer listing all saved opponent notes."""
        t = self.theme
        win = tk.Toplevel(self.root)
        win.title("Player Notes")
        win.configure(bg=t["panel"])
        win.resizable(True, True)
        win.transient(self.root)
        self.root.update_idletasks()
        dw, dh = 520, 360
        rx = self.root.winfo_rootx() + self.root.winfo_width()  // 2 - dw // 2
        ry = self.root.winfo_rooty() + self.root.winfo_height() // 2 - dh // 2
        win.geometry(f"{dw}x{dh}+{max(0, rx)}+{max(0, ry)}")

        tk.Label(win, text="PLAYER NOTES", bg=t["panel"], fg=t["accent"],
                 font=("Segoe UI", 13, "bold")).pack(pady=(12, 4))

        entries = _notes.all()
        if not entries:
            tk.Label(win, text="No notes saved yet.",
                     bg=t["panel"], fg=t["dim"],
                     font=("Segoe UI", 10)).pack(pady=40)
            tk.Button(win, text="Close", command=win.destroy, relief="flat",
                      bg=t["btn"], fg=t["btn_text"],
                      font=("Segoe UI", 9), cursor="hand2").pack(pady=8)
            return

        _COLOR_HEX = {
            "red": "#e53935", "orange": "#fb8c00", "yellow": "#fdd835",
            "green": "#43a047", "blue": "#1e88e5", "purple": "#8e24aa",
            "none": t["panel"],
        }

        lf = tk.Frame(win, bg=t["panel"])
        lf.pack(fill="both", expand=True, padx=12, pady=(4, 4))
        sb = tk.Scrollbar(lf, width=8)
        sb.pack(side="right", fill="y")
        lb_cv = tk.Canvas(lf, bg=t["panel"], highlightthickness=0,
                          yscrollcommand=sb.set)
        lb_cv.pack(side="left", fill="both", expand=True)
        sb.config(command=lb_cv.yview)
        rows_frame = tk.Frame(lb_cv, bg=t["panel"])
        lb_cv.create_window(0, 0, anchor="nw", window=rows_frame)

        def _on_configure(_e):
            lb_cv.config(scrollregion=lb_cv.bbox("all"))

        rows_frame.bind("<Configure>", _on_configure)

        def _edit_entry(pid, nick):
            win.destroy()
            self._note_edit_dialog(pid, nick)

        for entry in entries:
            pid = entry["peer_id"]
            nick = entry.get("nickname") or pid[:8]
            color = entry.get("color", "none")
            note = entry.get("note", "")
            preview = (note[:40] + "...") if len(note) > 40 else note

            row = tk.Frame(rows_frame, bg=t["bg"], pady=4, padx=6,
                           cursor="hand2")
            row.pack(fill="x", pady=1, padx=2)

            # Color dot
            dot_col = _COLOR_HEX.get(color, t["panel"])
            tk.Label(row, width=2, bg=dot_col, relief="flat").pack(
                side="left", padx=(0, 6))
            # Initial
            init_ch = nick[:1].upper() if nick else "?"
            tk.Label(row, text=init_ch, bg=t["seat"], fg=t["text"],
                     font=("Segoe UI", 9, "bold"), width=2).pack(side="left")
            # Nickname
            tk.Label(row, text=nick, bg=t["bg"], fg=t["text"],
                     font=("Segoe UI", 9, "bold"), width=14,
                     anchor="w").pack(side="left", padx=(4, 0))
            # Note preview
            tk.Label(row, text=preview, bg=t["bg"], fg=t["dim"],
                     font=("Segoe UI", 9), anchor="w").pack(
                side="left", padx=(4, 0), fill="x", expand=True)
            # Bind click to edit
            for child in (row,) + tuple(row.winfo_children()):
                child.bind("<Button-1>",
                           lambda _e, p=pid, n=nick: _edit_entry(p, n))

        bbar2 = tk.Frame(win, bg=t["panel"])
        bbar2.pack(fill="x", padx=12, pady=(4, 10))
        tk.Button(bbar2, text="Close", command=win.destroy, relief="flat",
                  bg=t["btn"], fg=t["btn_text"],
                  font=("Segoe UI", 9), cursor="hand2").pack(side="right")

    # ---------------------------------------- seat right-click context menu

    def _seat_right_click(self, event, seat_idx: int):
        """Show context menu on right-click of an opponent seat (MP only)."""
        if not self._mp_mode:
            return
        peer_id = self._mp_seat_to_peer.get(seat_idx, "")
        if not peer_id:
            return
        e = self.engine
        nick = (e.players[seat_idx].name
                if e and seat_idx < len(e.players) else f"P{seat_idx + 1}")
        t = self.theme

        menu = tk.Menu(self.root, tearoff=0, bg=t["panel"], fg=t["text"],
                       activebackground=t["seat"], activeforeground=t["accent"],
                       relief="flat", font=("Segoe UI", 9))
        menu.add_command(
            label="Add/Edit Note...",
            command=lambda: self._note_edit_dialog(peer_id, nick))

        color_menu = tk.Menu(menu, tearoff=0, bg=t["panel"], fg=t["text"],
                             activebackground=t["seat"],
                             activeforeground=t["accent"],
                             relief="flat", font=("Segoe UI", 9))
        _COLORS = [
            ("red",    "Red"),
            ("orange", "Orange"),
            ("yellow", "Yellow"),
            ("green",  "Green"),
            ("blue",   "Blue"),
            ("purple", "Purple"),
        ]
        for ckey, clabel in _COLORS:
            color_menu.add_command(
                label=clabel,
                command=lambda ck=ckey: (
                    _notes.set_color(peer_id, ck, nick), self.redraw()))
        color_menu.add_separator()
        color_menu.add_command(
            label="None (clear)",
            command=lambda: (_notes.set_color(peer_id, "none", nick),
                             self.redraw()))
        menu.add_cascade(label="Set Color Label", menu=color_menu)
        menu.add_command(
            label="View Stats",
            command=lambda: self._stats_popup(seat_idx))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _note_edit_dialog(self, peer_id: str, nickname: str):
        """Open a dialog to add or edit a note for the given peer."""
        t = self.theme
        existing = _notes.get(peer_id)
        win = tk.Toplevel(self.root)
        win.title(f"Note - {nickname}")
        win.configure(bg=t["panel"])
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        self.root.update_idletasks()
        dw, dh = 380, 240
        rx = self.root.winfo_rootx() + self.root.winfo_width()  // 2 - dw // 2
        ry = self.root.winfo_rooty() + self.root.winfo_height() // 2 - dh // 2
        win.geometry(f"{dw}x{dh}+{max(0, rx)}+{max(0, ry)}")

        tk.Label(win, text=nickname, bg=t["panel"], fg=t["accent"],
                 font=("Segoe UI", 12, "bold")).pack(pady=(12, 4))
        tk.Label(win, text="Note:", bg=t["panel"], fg=t["text"],
                 font=("Segoe UI", 9), anchor="w").pack(fill="x", padx=16)
        txt = tk.Text(win, height=4, bg=t["bg"], fg=t["text"],
                      font=("Consolas", 9), relief="flat",
                      insertbackground=t["text"], wrap="word")
        txt.pack(fill="x", padx=16, pady=(2, 4))
        txt.insert("1.0", existing.get("note", ""))

        def _save():
            note_text = txt.get("1.0", "end-1c").strip()
            _notes.set_note(peer_id, note_text, nickname)
            self.redraw()
            win.destroy()

        bbar = tk.Frame(win, bg=t["panel"])
        bbar.pack(fill="x", padx=16, pady=(4, 12))
        tk.Button(bbar, text="Cancel", command=win.destroy, relief="flat",
                  bg=t["btn"], fg=t["btn_text"],
                  font=("Segoe UI", 9), cursor="hand2").pack(side="left")
        tk.Button(bbar, text="Save", command=_save, relief="flat",
                  bg=t["accent"], fg="#04040c",
                  font=("Segoe UI", 9, "bold"), cursor="hand2",
                  padx=10).pack(side="right")

    def _stats_popup(self, seat_idx: int):
        """Show a small popup with session stats for the given seat."""
        t = self.theme
        e = self.engine
        if e is None or seat_idx >= len(e.players):
            return
        p = e.players[seat_idx]
        vpip = f"{self.session_stats.vpip_pct(seat_idx):.0f}%"
        pfr = f"{self.session_stats.pfr_pct(seat_idx):.0f}%"
        hands = str(self.session_stats.hands_dealt(seat_idx))

        win = tk.Toplevel(self.root)
        win.title(f"Stats - {p.name}")
        win.configure(bg=t["panel"])
        win.resizable(False, False)
        win.transient(self.root)
        self.root.update_idletasks()
        dw, dh = 260, 180
        rx = self.root.winfo_rootx() + self.root.winfo_width()  // 2 - dw // 2
        ry = self.root.winfo_rooty() + self.root.winfo_height() // 2 - dh // 2
        win.geometry(f"{dw}x{dh}+{max(0, rx)}+{max(0, ry)}")

        tk.Label(win, text=p.name, bg=t["panel"], fg=t["accent"],
                 font=("Segoe UI", 12, "bold")).pack(pady=(12, 8))
        for label, val in (("Hands dealt:", hands),
                           ("VPIP:", vpip),
                           ("PFR:", pfr)):
            row = tk.Frame(win, bg=t["panel"])
            row.pack(fill="x", padx=20, pady=2)
            tk.Label(row, text=label, bg=t["panel"], fg=t["text"],
                     font=("Segoe UI", 9), width=14, anchor="e").pack(
                side="left")
            tk.Label(row, text=val, bg=t["panel"], fg=t["gold"],
                     font=("Segoe UI", 9, "bold"), anchor="w").pack(
                side="left", padx=(4, 0))
        tk.Button(win, text="Close", command=win.destroy, relief="flat",
                  bg=t["btn"], fg=t["btn_text"],
                  font=("Segoe UI", 9), cursor="hand2").pack(pady=(14, 10))

    # ------------------------------------------------------- hand history

    def open_history(self):
        open_history_viewer(self.root, self.hand_logger, self.theme)

    # ---------------------------------------------------------------- hints

    def hint(self, lg):
        e = self.engine
        p = e.players[self.hero_seat]
        pot = lg["pot"]
        call = lg["to_call"]

        if e.street == "preflop":
            from .engine import chen
            sc = chen(p.hole)
            if sc >= 12:
                return "premium — raise for value"
            if sc >= 9:
                return "strong — raise, or 3-bet a single raiser"
            if sc >= 7:
                return "playable — open in late position, fold to heat"
            if call == 0:
                return "weak, but checking is free"
            return "weak — folding is fine"

        win, tie, eq = self.eq_bars[0], self.eq_bars[1], 0
        eq = win + tie / 2
        if call == 0:
            if eq > 0.7:
                return f"~{eq*100:.0f}% equity — bet for value"
            if eq > 0.45:
                return f"~{eq*100:.0f}% — a small bet or a check both work"
            return f"~{eq*100:.0f}% — check and see a free card"
        odds = call / (pot + call)
        if eq > odds + 0.12:
            return (f"~{eq*100:.0f}% vs {odds*100:.0f}% pot odds — "
                    f"call, raising is fine too")
        if eq > odds:
            return f"~{eq*100:.0f}% vs {odds*100:.0f}% pot odds — thin call"
        return f"~{eq*100:.0f}% vs {odds*100:.0f}% pot odds — fold"


def main():
    root = tk.Tk()

    # Apply persisted fullscreen preference; default is maximised (zoomed).
    # True fullscreen (no taskbar) is intentionally avoided — zoomed is
    # friendlier and still honours the user's taskbar.
    _stored = cfg.load()
    _fs_active: list[bool] = [bool(_stored["client"].get("fullscreen", True))]
    if _fs_active[0]:
        root.state("zoomed")

    # Reference to the Holdem instance, set once onboarding completes.
    _holdem_ref: list = [None]

    def _toggle_fullscreen(event=None):
        """F11 toggles between zoomed/maximised and normal windowed mode."""
        _fs_active[0] = not _fs_active[0]
        root.state("zoomed" if _fs_active[0] else "normal")
        app = _holdem_ref[0]
        if app is not None:
            # Keep the tk var in sync so save_config() persists the right value.
            app.v_fullscreen.set(_fs_active[0])
            app.save_config()
        else:
            # Toggled during onboarding — persist directly.
            s = cfg.load()
            s["client"]["fullscreen"] = _fs_active[0]
            cfg.save(s["client"], s["last_table"])

    root.bind("<F11>", _toggle_fullscreen)

    def _start_solo(nickname: str, avatar_idx: int, avatar_path: str) -> None:
        """Called by OnboardingFlow when the user clicks Practice (Solo)."""
        app = Holdem(root)
        _holdem_ref[0] = app
        # Sync the tk var with the actual window state (the user may have
        # toggled F11 during onboarding before this callback fires).
        app.v_fullscreen.set(_fs_active[0])
        app.nickname    = nickname
        app.avatar_idx  = avatar_idx
        app.avatar_path = avatar_path

        def on_close():
            app.save_config()
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_close)

    def on_close_during_onboarding():
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close_during_onboarding)
    OnboardingFlow(root, on_solo=_start_solo)
    root.mainloop()


if __name__ == "__main__":
    main()
