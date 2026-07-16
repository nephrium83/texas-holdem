"""Login / onboarding flow — Nickname  →  Avatar  →  Lobby.

Three tk.Frame-based screens packed into the root Tk window before the
main Holdem table is created.  Call::

    OnboardingFlow(root, on_solo=callback)

When the user clicks *Practice (Solo)* the callback receives
``(nickname, avatar_idx, avatar_path)`` and this frame is destroyed so
the caller can create ``Holdem(root)`` in its place.
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import settings as cfg

# ----------------------------------------------------------------- avatars

AVATARS: list[tuple[str, str, str, str]] = [
    # (bg_color, text_color, symbol, label)
    ("#e33b6d", "#f4f5fa", "♠", "Spades"),
    ("#00ffd0", "#07070f", "♦", "Diamonds"),
    ("#c62828", "#fdfdf7", "♥", "Hearts"),
    ("#226b45", "#eef4ef", "♣", "Clubs"),
    ("#2a4a8c", "#e8f0ff", "★", "Star"),
    ("#ffd166", "#07070f", "A",  "Ace"),
    ("#8c2f39", "#fdfdf7", "K",  "King"),
    ("#5c2333", "#ffe8ee", "Q",  "Queen"),
    ("#1d1d3d", "#d6d6f0", "J",  "Jack"),
    ("#39e7ff", "#07070f", "10", "Ten"),
    ("#07070f", "#00ffd0", "D",  "Dealer"),
    ("#4b3a24", "#ffd166", "BB", "Big Blind"),
    ("#3f3f86", "#c4b8ff", "SB", "Small Blind"),
    ("#1a3327", "#eef4ef", "BTN","Button"),
    ("#2a1a3a", "#ffd166", "UTG","UTG"),
    ("#1a1a2e", "#e33b6d", "MP", "Middle"),
]

# --------------------------------------------------------- colour palette
# Matches the Cyberpunk theme in gui.py.
_BG       = "#07070f"
_PANEL    = "#0c0c1a"
_FELT     = "#12123a"
_ACCENT   = "#00ffd0"
_GOLD     = "#ffd166"
_TEXT     = "#d6d6f0"
_DIM      = "#6f6f92"
_BTN      = "#1b1b3a"
_BTN_TXT  = "#e8e8ff"
_SEL_RING = "#39e7ff"   # selection highlight for avatar cells


# ----------------------------------------------------------------- helpers

def _btn(parent: tk.Widget, text: str, command, *, accent=False,
         **kw) -> tk.Button:
    """Styled button matching the game theme."""
    defaults: dict = dict(
        relief="flat", cursor="hand2", padx=14, pady=6,
        font=("Segoe UI", 10),
    )
    if accent:
        defaults.update(bg=_ACCENT, fg="#04040c",
                        activebackground=_GOLD,
                        font=("Segoe UI", 11, "bold"), padx=22, pady=8)
    else:
        defaults.update(bg=_BTN, fg=_BTN_TXT)
    defaults.update(kw)
    return tk.Button(parent, text=text, command=command, **defaults)


def _draw_avatar(canvas: tk.Canvas, size: int, idx: int) -> None:
    """Draw built-in avatar *idx* onto a canvas of pixel size ``size×size``."""
    canvas.delete("all")
    r = size // 2 - 3
    cx = cy = size // 2
    if 0 <= idx < len(AVATARS):
        bg, fg, sym, _ = AVATARS[idx]
    else:
        bg, fg, sym = _ACCENT, _BG, "?"
    canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                       fill=bg, outline=_PANEL, width=2)
    fsize = max(9, size // 4)
    canvas.create_text(cx, cy, text=sym, fill=fg,
                       font=("Segoe UI", fsize, "bold"))


def _load_photo(path: str, target: int) -> tk.PhotoImage | None:
    """Load *path* and subsample to ≈ *target* pixels.  Returns None on
    failure (missing file, unsupported format, etc.)."""
    if not path or not os.path.isfile(path):
        return None
    try:
        img = tk.PhotoImage(file=path)
        w, h = img.width(), img.height()
        factor = max(1, max(w, h) // target)
        if factor > 1:
            img = img.subsample(factor, factor)
        return img
    except Exception:
        return None


# --------------------------------------------------------------- main class

class OnboardingFlow:
    """Three-screen onboarding sequence inside the root window."""

    # -------------------------------------------------- construction

    def __init__(self, root: tk.Tk, on_solo,
                 on_online=None) -> None:
        """
        Parameters
        ----------
        root        The application's root Tk window.
        on_solo     Callable(nickname, avatar_idx, avatar_path) invoked
                    when the user chooses *Practice (Solo)*.
        on_online   Callable for future P2P join (currently unused).
        """
        self.root      = root
        self.on_solo   = on_solo
        self.on_online = on_online

        root.title("Texas Hold'em")
        root.configure(bg=_BG)
        root.minsize(900, 620)

        # Load persisted identity
        stored = cfg.load()
        cl = stored["client"]
        self.nickname:    str = cl.get("nickname",   "")
        self.avatar_idx:  int = cl.get("avatar_idx", 0)
        self.avatar_path: str = cl.get("avatar_path", "")

        # Outer frame; owns the window during onboarding
        self.frame = tk.Frame(root, bg=_BG)
        self.frame.pack(fill="both", expand=True)

        # Per-screen image references (prevent GC)
        self._photo_refs: list = []

        self._show_nickname()

    # ----------------------------------------------- persistence

    def _save(self) -> None:
        """Persist identity fields to the settings file."""
        stored = cfg.load()
        cl = stored["client"]
        cl["nickname"]    = self.nickname
        cl["avatar_idx"]  = self.avatar_idx
        cl["avatar_path"] = self.avatar_path
        cfg.save(cl, stored["last_table"])

    # ----------------------------------------------- screen management

    def _clear(self) -> None:
        """Destroy all child widgets of the outer frame."""
        for w in self.frame.winfo_children():
            w.destroy()
        self._photo_refs = []

    # ============================================================ Screen 1 — Nickname

    def _show_nickname(self) -> None:
        self._clear()

        outer = tk.Frame(self.frame, bg=_BG)
        outer.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(outer, text="TEXAS HOLD'EM",
                 bg=_BG, fg=_ACCENT,
                 font=("Segoe UI", 28, "bold")).pack(pady=(0, 4))
        tk.Label(outer, text="your poker room, your identity",
                 bg=_BG, fg=_DIM,
                 font=("Segoe UI", 10)).pack(pady=(0, 36))

        tk.Label(outer, text="What do they call you at the table?",
                 bg=_BG, fg=_TEXT,
                 font=("Segoe UI", 12)).pack(pady=(0, 10))

        entry_box = tk.Frame(outer, bg=_PANEL, padx=2, pady=2)
        entry_box.pack()
        self._sv = tk.StringVar(value=self.nickname)
        entry = tk.Entry(
            entry_box, textvariable=self._sv,
            width=22, font=("Segoe UI", 14),
            bg=_PANEL, fg=_TEXT,
            insertbackground=_TEXT,
            relief="flat", justify="center",
            bd=0)
        entry.pack(ipady=8, ipadx=6)

        # Enforce 20-char max
        def _cap(*_):
            v = self._sv.get()
            if len(v) > 20:
                self._sv.set(v[:20])
        self._sv.trace_add("write", _cap)

        tk.Label(outer, text="max 20 characters",
                 bg=_BG, fg=_DIM,
                 font=("Segoe UI", 8)).pack(pady=(4, 28))

        _btn(outer, "Continue  →", self._nick_next, accent=True).pack()

        entry.bind("<Return>", lambda _e: self._nick_next())
        entry.focus_set()

    def _nick_next(self) -> None:
        name = self._sv.get().strip()
        if not name:
            messagebox.showwarning(
                "Nickname required",
                "Please enter a nickname to continue.",
                parent=self.root)
            return
        self.nickname = name
        self._save()
        self._show_avatar()

    # ============================================================ Screen 2 — Avatar

    _CELL  = 82    # avatar cell canvas size (px)
    _COLS  = 4     # columns in the grid
    _PREV  = 128   # preview canvas size (px)

    def _show_avatar(self) -> None:
        self._clear()

        outer = tk.Frame(self.frame, bg=_BG)
        outer.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(outer, text="Choose your avatar",
                 bg=_BG, fg=_ACCENT,
                 font=("Segoe UI", 20, "bold")).pack(pady=(0, 4))
        tk.Label(outer, text=f"Welcome, {self.nickname}",
                 bg=_BG, fg=_DIM,
                 font=("Segoe UI", 10)).pack(pady=(0, 22))

        row_frame = tk.Frame(outer, bg=_BG)
        row_frame.pack()

        # ---- avatar grid (left)
        grid = tk.Frame(row_frame, bg=_BG)
        grid.pack(side="left", padx=(0, 36))

        self._av_cells: list[tk.Canvas] = []
        self._av_selected  = self.avatar_idx
        self._custom_active = bool(self.avatar_path)

        def _select(idx: int) -> None:
            self._av_selected   = idx
            self.avatar_path    = ""
            self._custom_active = False
            self._refresh_grid()
            self._refresh_preview()

        COLS = self._COLS
        for i in range(len(AVATARS)):
            r, c = divmod(i, COLS)
            cell_bg = tk.Frame(grid, bg=_BG, padx=3, pady=3)
            cell_bg.grid(row=r, column=c)
            cv = tk.Canvas(cell_bg, width=self._CELL, height=self._CELL,
                           bg=_BG, highlightthickness=0, cursor="hand2")
            cv.pack()
            _draw_avatar(cv, self._CELL, i)
            cv.bind("<Button-1>", lambda _e, idx=i: _select(idx))
            self._av_cells.append(cv)

        # ---- preview panel (right)
        pane = tk.Frame(row_frame, bg=_BG)
        pane.pack(side="left", anchor="n")

        tk.Label(pane, text="PREVIEW", bg=_BG, fg=_DIM,
                 font=("Segoe UI", 8, "bold")).pack(pady=(0, 6))

        self._prev_cv = tk.Canvas(pane,
                                  width=self._PREV, height=self._PREV,
                                  bg=_BG, highlightthickness=0)
        self._prev_cv.pack()

        self._prev_lbl = tk.Label(pane, text="", bg=_BG, fg=_TEXT,
                                  font=("Segoe UI", 10, "bold"))
        self._prev_lbl.pack(pady=(8, 4))

        _btn(pane, "Browse image…", self._browse_avatar).pack(pady=(14, 0))
        tk.Label(pane, text="PNG or GIF supported",
                 bg=_BG, fg=_DIM,
                 font=("Segoe UI", 7)).pack(pady=(2, 0))

        # ---- navigation row
        nav = tk.Frame(outer, bg=_BG)
        nav.pack(pady=(22, 0))
        _btn(nav, "← Back",      self._show_nickname).pack(side="left", padx=8)
        _btn(nav, "Continue  →", self._avatar_next, accent=True).pack(
            side="left", padx=8)

        self._refresh_grid()
        self._refresh_preview()

    def _refresh_grid(self) -> None:
        """Redraw all avatar cells; put a highlight ring on the selected one."""
        for i, cv in enumerate(self._av_cells):
            _draw_avatar(cv, self._CELL, i)
            if i == self._av_selected and not self._custom_active:
                r = self._CELL - 4
                cv.create_oval(2, 2, r, r,
                               outline=_SEL_RING, width=3, fill="")

    def _refresh_preview(self) -> None:
        """Redraw the large preview canvas."""
        self._prev_cv.delete("all")
        self._photo_refs = []   # release old images

        if self._custom_active and self.avatar_path:
            img = _load_photo(self.avatar_path, self._PREV)
            if img is not None:
                self._photo_refs.append(img)
                cx = cy = self._PREV // 2
                self._prev_cv.create_image(cx, cy, image=img, anchor="center")
                self._prev_lbl.config(text="Custom")
                return
            # Fall through if image failed to load
            self._custom_active = False

        _draw_avatar(self._prev_cv, self._PREV, self._av_selected)
        label = AVATARS[self._av_selected][3] if self._av_selected < len(AVATARS) else ""
        self._prev_lbl.config(text=label)

    def _browse_avatar(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose avatar image",
            filetypes=[
                ("Image files", "*.png *.gif *.ppm *.pgm"),
                ("PNG files",   "*.png"),
                ("GIF files",   "*.gif"),
                ("All files",   "*.*"),
            ],
            parent=self.root)
        if path:
            self.avatar_path    = path
            self._custom_active = True
            self._refresh_grid()
            self._refresh_preview()

    def _avatar_next(self) -> None:
        self.avatar_idx = self._av_selected
        self._save()
        self._show_lobby()

    # ============================================================ Screen 3 — Lobby

    def _show_lobby(self) -> None:
        self._clear()

        outer = tk.Frame(self.frame, bg=_BG)
        outer.pack(fill="both", expand=True)

        # ---- header bar
        header = tk.Frame(outer, bg=_PANEL, pady=10)
        header.pack(fill="x")

        av_cv = tk.Canvas(header, width=48, height=48,
                          bg=_PANEL, highlightthickness=0)
        av_cv.pack(side="left", padx=(20, 12))
        _draw_avatar(av_cv, 48, self.avatar_idx)

        tk.Label(header, text=self.nickname,
                 bg=_PANEL, fg=_TEXT,
                 font=("Segoe UI", 15, "bold")).pack(side="left")

        tk.Label(header, text="TEXAS HOLD'EM  ·  LOBBY",
                 bg=_PANEL, fg=_ACCENT,
                 font=("Segoe UI", 10, "bold")).pack(side="right", padx=24)

        # ---- body
        body = tk.Frame(outer, bg=_BG)
        body.pack(fill="both", expand=True, padx=28, pady=18)

        # ---- game-type selector
        gt_frame = tk.Frame(body, bg=_BG)
        gt_frame.pack(fill="x", pady=(0, 18))
        tk.Label(gt_frame, text="GAME TYPE",
                 bg=_BG, fg=_DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        # Single type for now; styled as a selected pill
        tk.Label(gt_frame, text="  Texas Hold'em  ",
                 bg=_ACCENT, fg="#04040c",
                 font=("Segoe UI", 10, "bold"),
                 padx=6, pady=4,
                 cursor="hand2").pack(anchor="w", pady=(5, 0))

        # ---- open-tables list
        tk.Label(body, text="OPEN TABLES",
                 bg=_BG, fg=_DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(0, 5))

        tbl_wrap = tk.Frame(body, bg=_BG)
        tbl_wrap.pack(fill="both", expand=True)

        # Style the Treeview to match the dark theme
        style = ttk.Style()
        try:
            style.theme_use("default")
        except tk.TclError:
            pass
        style.configure("Lobby.Treeview",
                        background=_PANEL, fieldbackground=_PANEL,
                        foreground=_TEXT, rowheight=30, borderwidth=0,
                        font=("Segoe UI", 9))
        style.configure("Lobby.Treeview.Heading",
                        background=_BTN, foreground=_DIM,
                        font=("Segoe UI", 8, "bold"), relief="flat")
        style.map("Lobby.Treeview",
                  background=[("selected", _FELT)],
                  foreground=[("selected", _ACCENT)])

        cols = ("name", "players", "stakes", "blinds")
        tree = ttk.Treeview(tbl_wrap, columns=cols,
                            show="headings", style="Lobby.Treeview",
                            height=7, selectmode="browse")

        col_spec = [
            ("name",    "Table",   220, "w"),
            ("players", "Players",  80, "center"),
            ("stakes",  "Stakes",  130, "w"),
            ("blinds",  "Blinds",  130, "w"),
        ]
        for col, label, width, anchor in col_spec:
            tree.heading(col, text=label)
            tree.column(col, width=width, anchor=anchor)

        vsb = ttk.Scrollbar(tbl_wrap, orient="vertical",
                            command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # Placeholder row
        tree.insert("", "end", iid="ph",
                    values=("No tables open — create one or play solo",
                            "", "", ""))
        tree.tag_configure("ph_tag", foreground=_DIM)
        tree.item("ph", tags=("ph_tag",))

        self._lobby_tree = tree

        # ---- action buttons
        btn_row = tk.Frame(body, bg=_BG)
        btn_row.pack(fill="x", pady=(16, 4))

        _btn(btn_row, "Create Table…",
             self._create_table_dialog).pack(side="left", padx=(0, 8))
        _btn(btn_row, "Join Table",
             command=lambda: messagebox.showinfo(
                 "Coming soon",
                 "Joining live tables requires the multiplayer\n"
                 "layer, which isn't implemented yet.\n\n"
                 "Use Practice (Solo) to play now.",
                 parent=self.root),
             state="disabled").pack(side="left")

        _btn(btn_row, "← Back",
             self._show_avatar).pack(side="right", padx=(8, 0))
        _btn(btn_row, "Practice  (Solo)",
             self._start_solo, accent=True).pack(side="right")

    # ---- create-table dialog

    def _create_table_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Create Table")
        win.configure(bg=_PANEL)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        self.root.update_idletasks()
        dw, dh = 400, 370
        rx = self.root.winfo_rootx() + self.root.winfo_width()  // 2 - dw // 2
        ry = self.root.winfo_rooty() + self.root.winfo_height() // 2 - dh // 2
        win.geometry(f"{dw}x{dh}+{max(0, rx)}+{max(0, ry)}")

        tk.Label(win, text="CREATE TABLE",
                 bg=_PANEL, fg=_ACCENT,
                 font=("Segoe UI", 14, "bold")).pack(pady=(16, 14))

        body = tk.Frame(win, bg=_PANEL)
        body.pack(fill="both", expand=True, padx=22)

        def _row(label: str, var: tk.Variable, lo=None, hi=None):
            fr = tk.Frame(body, bg=_PANEL)
            fr.pack(fill="x", pady=5)
            tk.Label(fr, text=label, bg=_PANEL, fg=_TEXT,
                     font=("Segoe UI", 9), width=22, anchor="e").pack(
                side="left", padx=(0, 8))
            if lo is not None:
                w = tk.Spinbox(fr, textvariable=var,
                               from_=lo, to=hi, width=10,
                               bg=_BG, fg=_TEXT,
                               buttonbackground=_BTN,
                               insertbackground=_TEXT,
                               relief="flat", justify="center")
            else:
                w = tk.Entry(fr, textvariable=var, width=18,
                             bg=_BG, fg=_TEXT,
                             insertbackground=_TEXT, relief="flat")
            w.pack(side="left")

        v_name    = tk.StringVar(value="Table #1")
        v_players = tk.IntVar(value=6)
        v_sb      = tk.IntVar(value=10)
        v_bb      = tk.IntVar(value=20)
        v_clock   = tk.IntVar(value=25)

        _row("Table name",           v_name)
        _row("Max players",          v_players, lo=2,  hi=9)
        _row("Small blind",          v_sb,      lo=1,  hi=5000)
        _row("Big blind",            v_bb,      lo=2,  hi=10000)
        _row("Seconds per action",   v_clock,   lo=10, hi=120)

        tk.Label(body,
                 text="Networking is not yet implemented.\n"
                      "Tables will appear in the lobby once\n"
                      "the P2P layer is added.",
                 bg=_PANEL, fg=_DIM,
                 font=("Segoe UI", 8), justify="center").pack(pady=(14, 0))

        bar = tk.Frame(win, bg=_PANEL)
        bar.pack(fill="x", padx=22, pady=14)
        _btn(bar, "Cancel", win.destroy).pack(side="left")
        _btn(bar, "Create Table",
             lambda: (
                 messagebox.showinfo(
                     "Table created",
                     "Your table settings have been saved.\n\n"
                     "Multiplayer networking isn't wired up yet —\n"
                     "use Practice (Solo) to play now.",
                     parent=win),
                 win.destroy()),
             accent=True).pack(side="right")

    # ---- start solo

    def _start_solo(self) -> None:
        """Persist, tear down the onboarding frame, and call back."""
        self._save()
        cb           = self.on_solo
        nickname     = self.nickname
        avatar_idx   = self.avatar_idx
        avatar_path  = self.avatar_path
        self.frame.destroy()
        cb(nickname, avatar_idx, avatar_path)
