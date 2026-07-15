import math
import random
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .engine import (Engine, Player, Brain, equity, evaluate, hand_name,
                    AI_STYLES, SUIT_GLYPHS, RANK_STR)

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

HERO = 0


def rrect(cv, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle via a smoothed polygon."""
    r = min(r, abs(x2 - x1) / 2, abs(y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r,
           x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
           x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class Holdem:
    def __init__(self, root):
        self.root = root
        root.title("Texas Hold'em")
        root.minsize(1180, 760)

        self.rng = random.Random()
        self.brain = Brain(self.rng)
        self.engine = None
        self.theme = THEMES["Cyberpunk"]

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

        self.wrap = tk.Frame(root)
        self.wrap.pack(fill="both", expand=True)
        self.setup = tk.Frame(self.wrap)
        self.table = tk.Frame(self.wrap)
        self._build_setup()
        self._build_table()
        self._show(self.setup)

    # ------------------------------------------------------------- screens

    def _show(self, frame):
        for f in (self.setup, self.table):
            f.pack_forget()
        frame.pack(fill="both", expand=True)

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

    def _build_controls(self, f):
        t = self.theme
        bar = tk.Frame(f, bg=t["panel"], height=88)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

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

        right = tk.Frame(bar, bg=t["panel"])
        right.pack(side="right", padx=16, pady=8)

        sizes = tk.Frame(right, bg=t["panel"])
        sizes.grid(row=0, column=0, columnspan=4, sticky="e", pady=(0, 4))
        self.size_btns = []
        for label, frac in (("½ pot", 0.5), ("¾ pot", 0.75),
                            ("Pot", 1.0), ("All-in", None)):
            b = tk.Button(sizes, text=label, width=6, relief="flat",
                          bg=t["btn"], fg=t["btn_text"], font=("Segoe UI", 8),
                          cursor="hand2",
                          command=lambda fr=frac: self.preset(fr))
            b.pack(side="left", padx=2)
            self.size_btns.append(b)

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
        for kind, text in self.engine.drain():
            self.say(kind, text)

    # ---------------------------------------------------------- game set-up

    def new_game(self):
        n = max(2, min(9, self.v_players.get()))
        sb = max(1, self.v_sb.get())
        bb = max(sb + 1, self.v_bb.get())
        cash = self.v_mode.get() == "Cash"
        stack = max(20, self.v_stack.get())
        if cash:                                  # table stakes: 40-100 BB
            stack = max(40 * bb, min(stack, 100 * bb))
        self.theme = THEMES[self.v_theme.get()]

        base = self.v_level.get()
        players = [Player(0, "You", stack, style="Hero", level=3, human=True)]
        for i in range(1, n):
            lvl = self.rng.randint(1, 3) if self.v_chaos.get() else base
            players.append(Player(i, f"P{i+1}", stack,
                                  style=self.rng.choice(AI_STYLES), level=lvl))

        self.engine = Engine(players, sb=sb, bb=bb,
                             structure=self.v_struct.get(), rng=self.rng,
                             bb_ante=(not cash) and self.v_ante.get(),
                             deal_sitting_out=not cash)
        self.buyin = stack
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
                 f"  ·  AI {'mixed' if self.v_chaos.get() else base}")
        self._show(self.table)
        self.say("hand", "=== New game ===")
        self.level_gen = getattr(self, "level_gen", 0) + 1
        if self.v_mode.get() == "Tournament":
            self.root.after(1000, lambda g=self.level_gen: self._level_tick(g))
        self.root.after(120, self.deal)

    def _retheme(self):
        t = self.theme
        for w in (self.table, self.cv):
            w.configure(bg=t["bg"])

    def deal(self):
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

        hero = e.players[HERO]
        if cash and hero.stack == 0 and not hero.sitting_out:
            if messagebox.askyesno(
                    "Rebuy", f"You're felted. Rebuy for {self.buyin:,}?"):
                e.add_chips(HERO, self.buyin)
            else:
                e.sit_out(HERO)
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

        if not e.start_hand(straddle_fn=straddle_fn):
            self.finish_game(live)
            return
        if e.bb_i == HERO and e.players[HERO].in_seat:
            self.bank = min(BANK_CAP, self.bank + BANK_TOPUP)
        self.flush_log()
        self.l_blinds.config(
            text=f"Blinds {e.sb}/{e.bb}"
                 + (f" · ante {e.bb}" if e.bb_ante else ""))
        self.b_next.config(state="disabled")
        self.b_add.config(state="disabled")
        self.loop()

    def finish_game(self, live):
        self.stop_clock()
        self.game_over = True
        self.hand_over = True
        self.lock()
        self.b_next.config(state="disabled")
        if live and live[0].idx == HERO:
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

        hero = e.players[HERO]
        if i == HERO and hero.sitting_out:      # sitting out: check or fold
            self.lock()
            act = "call" if e.legal(HERO)["can_check"] else "fold"
            self.root.after(max(60, self.delay // 3),
                            lambda a=act: self._forced(a))
            return

        if i == HERO and not self.v_observe.get():
            self.start_equity()
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
        if e is None or e.actor != HERO:
            return
        e.act(HERO, action, 0)
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
            mode = self.v_rit.get()
            hero_in = (any(p.idx == HERO for p in e.contested())
                       and not self.v_observe.get()
                       and not e.players[HERO].sitting_out)
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
        e.act(i, act, amt)
        self.flush_log()
        self.redraw()
        self.root.after(max(60, self.delay // 3), self.loop)

    # --------------------------------------------------------- hero actions

    def lock(self):
        for b in (self.b_fold, self.b_call, self.b_raise, *self.size_btns):
            b.config(state="disabled")
        self.slider.config(state="disabled")

    def unlock(self):
        e = self.engine
        lg = e.legal(HERO)
        t = self.theme

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
        if self.v_hint.get():
            self.l_hint.config(text="Hint: " + self.hint(lg))
        else:
            self.l_hint.config(text="")

    def _sync_raise_label(self):
        e = self.engine
        if e is None or e.actor != HERO:
            return
        lg = e.legal(HERO)
        amt = self.v_bet.get()
        if amt >= lg["max_to"] and lg["max_to"] > 0:
            self.b_raise.config(text=f"All-in {amt}")
        elif e.current_bet == 0:
            self.b_raise.config(text=f"Bet {amt}")
        else:
            self.b_raise.config(text=f"Raise to {amt}")

    def preset(self, frac):
        e = self.engine
        if e is None or e.actor != HERO:
            return
        lg = e.legal(HERO)
        if frac is None:
            amt = lg["max_to"]
        else:
            call = lg["to_call"]
            amt = int(e.current_bet + (lg["pot"] + call) * frac)
        self.v_bet.set(max(lg["min_to"], min(amt, lg["max_to"])))
        self._sync_raise_label()

    def hero(self, action):
        e = self.engine
        if e is None or e.actor != HERO or self.v_observe.get():
            return
        self.stop_clock()
        amt = self.v_bet.get() if action == "raise" else 0
        self.lock()
        e.act(HERO, action, amt)
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
        if self.engine is None or self.engine.actor != HERO:
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
        self.lock()
        self.stop_clock()
        res = e.settle(runs=runs, force_tabled=tabled)
        self.result = res
        self.flush_log()
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
            nm = "You" if p.idx == HERO else p.name
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

        if HERO in res["winners"]:
            self.l_status.config(text=f"You win {e.players[HERO].won}.")
        elif not e.players[HERO].in_seat:
            if e.players[HERO].sitting_out or e.players[HERO].wait_for_bb:
                self.l_status.config(text="Sitting out.")
            else:
                self.l_status.config(text="You're out — the table plays on.")
        elif e.players[HERO].folded:
            self.l_status.config(text="You folded. Next hand?")
        else:
            self.l_status.config(text="You lose the pot.")
        self.l_hint.config(text="")

        if (self.v_rabbit.get() and len(e.board) < 5
                and self.v_mode.get() == "Cash"):
            self.b_rabbit.config(state="normal")
        if self.v_mode.get() == "Cash":
            hero = e.players[HERO]
            if hero.stack + hero.total < 100 * e.bb:
                self.b_add.config(state="normal")
        self.redraw()

        alive_next = [p for p in e.players if p.stack > 0]
        if len(alive_next) < 2:
            self.root.after(self.delay * 2,
                            lambda: self.finish_game(alive_next))
            return

        hero = e.players[HERO]
        hero_idle = (hero.stack <= 0 and self.v_mode.get() != "Cash") \
            or hero.sitting_out or hero.wait_for_bb
        self.b_next.config(state="normal")
        if self.v_auto.get() or self.v_observe.get() or hero_idle \
                or (hero.stack <= 0 and self.v_mode.get() == "Cash"):
            self.root.after(max(900, self.delay * 3), self.deal)


    # --------------------------------------------------------------- clock

    def start_clock(self):
        self.stop_clock()
        e = self.engine
        if (not self.v_clock.get() or self.v_observe.get() or e is None
                or e.players[HERO].sitting_out):
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
        if e is None or e.actor != HERO or self.clock_phase == "off":
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
                lg = e.legal(HERO)
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

    # ------------------------------------------------------- table actions

    def toggle_sit(self):
        e = self.engine
        if e is None or self.game_over:
            return
        hero = e.players[HERO]
        if not hero.sitting_out:
            e.sit_out(HERO)
            self.b_sit.config(text="I'm back")
            self.say("fold", "You sit out.")
        else:
            if self.v_mode.get() == "Cash" and (hero.owes_bb or hero.owes_sb):
                post = messagebox.askyesno(
                    "Return", "Post the missed blinds now?\n"
                              "(No = wait for the big blind)")
                e.sit_in(HERO, post_now=post)
            else:
                e.sit_in(HERO)
            self.b_sit.config(text="Sit out")
            self.say("hand", "You're back.")

    def add_chips_dialog(self):
        e = self.engine
        if e is None or not self.hand_over or self.v_mode.get() != "Cash":
            return
        hero = e.players[HERO]
        cap = 100 * e.bb - hero.stack
        if cap <= 0:
            messagebox.showinfo("Add chips", "You're at the table max.")
            return
        amt = simpledialog.askinteger(
            "Add chips", f"Add how much? (max {cap:,})",
            minvalue=1, maxvalue=cap,
            initialvalue=min(cap, self.buyin))
        if amt and e.add_chips(HERO, amt):
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
        x = self.root.winfo_rootx() + self.root.winfo_width() // 2 - 260
        y = self.root.winfo_rooty() + self.root.winfo_height() // 2 - 230
        win.geometry(f"520x460+{max(0, x)}+{max(0, y)}")

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

        for name in ("Display", "Table"):
            b = tk.Button(nav, text=name.upper(), relief="flat",
                          bg=t["panel"], fg=t["dim"],
                          activebackground=t["panel"],
                          activeforeground=t["accent"],
                          font=("Segoe UI", 10, "bold"), anchor="w",
                          cursor="hand2", width=9,
                          command=lambda n=name: show(n))
            b.pack(anchor="w", pady=2)
            navbtns[name] = b

        d = page("Display")

        def lab(parent, txt, top=8):
            tk.Label(parent, text=txt, bg=t["bg"], fg=t["text"],
                     font=("Segoe UI", 9)).pack(anchor="w", pady=(top, 1))

        lab(d, "Theme", 4)
        cb_theme = ttk.Combobox(d, values=list(THEMES), state="readonly",
                                width=20)
        cb_theme.set(self.v_theme.get())
        cb_theme.pack(anchor="w")
        cb_theme.bind("<<ComboboxSelected>>",
                      lambda _e: (self.v_theme.set(cb_theme.get()),
                                  self.apply_theme(cb_theme.get())))
        lab(d, "Game speed")
        ttk.Combobox(d, textvariable=self.v_speed, values=list(SPEEDS),
                     state="readonly", width=20).pack(anchor="w")
        tk.Label(d, text="Theme changes apply immediately.",
                 bg=t["bg"], fg=t["dim"],
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(14, 0))

        tb = page("Table")
        for txt, var in (("Action clock (25s + time bank)", self.v_clock),
                         ("Coaching hints", self.v_hint),
                         ("Live equity readout", self.v_odds),
                         ("Rabbit hunting", self.v_rabbit),
                         ("Auto-deal next hand", self.v_auto),
                         ("Allow straddles (cash)", self.v_straddles),
                         ("AI auto top-up (cash)", self.v_topup)):
            tk.Checkbutton(tb, text=txt, variable=var, bg=t["bg"],
                           fg=t["text"], selectcolor=t["panel"],
                           activebackground=t["bg"],
                           activeforeground=t["text"],
                           font=("Segoe UI", 9)).pack(anchor="w", pady=1)
        lab(tb, "Show cards at")
        ttk.Combobox(tb, textvariable=self.v_reveal,
                     values=["Winner only", "Realistic (muck losers)",
                             "Everyone"],
                     state="readonly", width=22).pack(anchor="w")
        lab(tb, "Run it twice")
        ttk.Combobox(tb, textvariable=self.v_rit,
                     values=["Ask", "Always", "Never"],
                     state="readonly", width=22).pack(anchor="w")

        show("Display")

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
        if (e is not None and e.actor == HERO and not self.hand_over
                and not self.v_observe.get()
                and not e.players[HERO].sitting_out):
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
            self.root.destroy()

    # -------------------------------------------------------------- equity

    def start_equity(self):
        e = self.engine
        self.eq_gen += 1
        gen = self.eq_gen
        hero = e.players[HERO]
        if not self.v_odds.get() or not hero.hole or hero.folded:
            self.eq_text = "-"
            self.eq_bars = (0, 0, 1)
            return
        opp = len([p for p in e.contested() if p.idx != HERO])
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
        col = t["red"] if card.red else t["black"]
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
        if e is None:
            return
        W = max(cv.winfo_width(), 600)
        H = max(cv.winfo_height(), 420)
        cx, cy = W / 2, H * 0.46
        rx, ry = W * 0.36, H * 0.27

        # felt
        cv.create_oval(cx - rx - 16, cy - ry - 16, cx + rx + 16, cy + ry + 16,
                       fill=t["rail"], outline="")
        cv.create_oval(cx - rx, cy - ry, cx + rx, cy + ry,
                       fill=t["felt"], outline=t["felt_edge"], width=2)

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

        hero = e.players[HERO]
        self.l_stack.config(text=f"{hero.stack:,}"
                            + ("  ALL-IN" if hero.all_in else ""))
        self.draw_equity()

    def seat(self, p, x, y, cx, cy):
        e = self.engine
        cv = self.cv
        t = self.theme
        hero = p.idx == HERO
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

        name = "You" if hero else p.name
        sub = "" if hero else f"  {p.style[0]}{p.level}"
        cv.create_text(x - w / 2 + 10, y - h / 2 + 13, anchor="w",
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
        cv.create_text(x - w / 2 + 10, y - h / 2 + 28, anchor="w",
                       text=stack_txt,
                       fill=t["dim"] if p.folded else t["gold"],
                       font=("Segoe UI", 10, "bold"))

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

    # ---------------------------------------------------------------- hints

    def hint(self, lg):
        e = self.engine
        p = e.players[HERO]
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
    Holdem(root)
    root.mainloop()


if __name__ == "__main__":
    main()
