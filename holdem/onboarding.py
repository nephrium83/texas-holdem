"""Login / onboarding flow — Nickname  →  Avatar  →  Lobby.

Three tk.Frame-based screens packed into the root Tk window before the
main Holdem table is created.  Call::

    OnboardingFlow(root, on_solo=callback)

When the user clicks *Practice (Solo)* the callback receives
``(nickname, avatar_idx, avatar_path)`` and this frame is destroyed so
the caller can create ``Holdem(root)`` in its place.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import threading

log = logging.getLogger(__name__)

from . import settings as cfg
from .p2p import invite as _invite
from .p2p import join_auth as _join_auth
from .p2p.invite import generate_room_code, parse_room_code
from .p2p import admission as _admission
from .p2p import identity as _identity
from .p2p import transport as _transport
from .p2p import wire as _wire
from .p2p import session as _session_mod
from .p2p import _session  # noqa: F401 -- re-exported for callers
import holdem.p2p as _p2p_pkg

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

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


# -------------------------------------------------- avatar thumbnail helpers

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _render_builtin_avatar_b64(avatar_idx: int, size: int = 64) -> str:
    """Render built-in avatar *avatar_idx* to a ``size×size`` PNG and return
    the base64 string.  Requires Pillow; returns ``""`` if unavailable."""
    if not _PIL_OK:
        return ""
    if 0 <= avatar_idx < len(AVATARS):
        bg_hex, fg_hex, sym, _ = AVATARS[avatar_idx]
    else:
        bg_hex, fg_hex, sym = _ACCENT, _BG, "?"
    bg_rgb = _hex_to_rgb(bg_hex)
    fg_rgb = _hex_to_rgb(fg_hex)

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 2
    draw.ellipse([margin, margin, size - margin - 1, size - margin - 1],
                 fill=bg_rgb)

    # Try platform fonts; fall back to PIL built-in.
    font = None
    fsize = max(12, size // 3)
    for fname in ("segoeui.ttf", "SegoeUI.ttf", "Arial.ttf", "arial.ttf",
                  "DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(fname, fsize)
            break
        except Exception:
            pass
    if font is None:
        try:
            font = ImageFont.load_default(size=fsize)
        except Exception:
            font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), sym, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = (size - tw) / 2 - bbox[0]
    ty = (size - th) / 2 - bbox[1]
    draw.text((tx, ty), sym, fill=fg_rgb, font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _render_custom_avatar_b64(path: str, size: int = 64) -> str:
    """Open *path* with Pillow, center-crop, resize to ``size×size``, and
    return a base64-encoded PNG.  Returns ``""`` on any failure."""
    if not _PIL_OK or not path or not os.path.isfile(path):
        return ""
    try:
        img = Image.open(path).convert("RGBA")
        w, h = img.size
        if w != h:
            s = min(w, h)
            left = (w - s) // 2
            top = (h - s) // 2
            img = img.crop((left, top, left + s, top + s))
        img = img.resize((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""


def compute_avatar_b64(avatar_idx: int, avatar_path: str,
                       size: int = 64) -> str:
    """Return a base64 PNG thumbnail for the current avatar choice."""
    if avatar_path and os.path.isfile(avatar_path):
        result = _render_custom_avatar_b64(avatar_path, size)
        if result:
            return result
    return _render_builtin_avatar_b64(avatar_idx, size)


# --------------------------------------------------------------- main class

class OnboardingFlow:
    """Three-screen onboarding sequence inside the root window."""

    # -------------------------------------------------- construction

    def __init__(self, root: tk.Tk, on_solo,
                 on_online=None, on_mp_start=None) -> None:
        """
        Parameters
        ----------
        root        The application's root Tk window.
        on_solo     Callable(nickname, avatar_idx, avatar_path) invoked
                    when the user chooses *Practice (Solo)*.
        on_online   Callable for future P2P join (currently unused).
        """
        self.root         = root
        self.on_solo      = on_solo
        self.on_online    = on_online
        self.on_mp_start  = on_mp_start

        root.title("Texas Hold'em")
        root.configure(bg=_BG)
        root.minsize(900, 620)

        # Load persisted identity
        stored = cfg.load()
        cl = stored["client"]
        self.nickname:    str = cl.get("nickname",   "")
        self.avatar_idx:  int = cl.get("avatar_idx", 0)
        self.avatar_path: str = cl.get("avatar_path", "")
        self.avatar_b64:  str = cl.get("avatar_b64",  "")

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
        cl["avatar_b64"]  = getattr(self, "avatar_b64", "")
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
        self.avatar_b64 = compute_avatar_b64(self.avatar_idx, self.avatar_path)
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

        # Persistent level + bankroll
        _level = cfg.get("player_level")
        _bankroll = cfg.get("bankroll")
        tk.Label(header,
                 text=f"Lv. {_level}  \u00b7  {_bankroll:,} chips",
                 bg=_PANEL, fg=_GOLD,
                 font=("Segoe UI", 9)).pack(side="left", padx=(10, 0))

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

        # P2P invite code buttons
        _btn(btn_row, "Create Game",
             self._create_game_dialog).pack(side="left", padx=(16, 0))
        _btn(btn_row, "Join Game",
             self._join_game_dialog).pack(side="left", padx=(4, 0))

        _btn(btn_row, "← Back",
             self._show_avatar).pack(side="right", padx=(8, 0))
        _btn(btn_row, "Practice  (Solo)",
             self._start_solo, accent=True).pack(side="right")

    # ---- create-table dialog

    def _create_game_dialog(self) -> None:
        """Start hosting: bind transport, run STUN, generate invite code, open lobby."""
        # Start transport host (fires STUN in background)
        try:
            listen_addr = _transport.start_host()
        except Exception as exc:
            messagebox.showerror("Transport error",
                                 f"Could not start network listener:\n{exc}",
                                 parent=self.root)
            return

        # Configure relay fallback (may not be reachable yet — that's OK)
        _transport.set_relay_address("192.168.1.10", 7890)
        relay_addr = ("192.168.1.10", 7890)

        # STUN result may not be available yet — get_public_address() returns
        # None until the background query completes (~1–3 s).
        pub_addr = _transport.get_public_address()

        # Generate the V2 invite. The discovery token AND the admission
        # secret are both held stable across regeneration: the token so LAN
        # multicast keeps working, the secret so a guest who already copied
        # the code can still prove admission after STUN resolves. Rotating
        # the secret on regeneration would silently lock out everyone
        # holding the earlier code.
        _code_ref = [generate_room_code(
            public_address=pub_addr,
            relay_address=relay_addr,
        )]
        parsed = parse_room_code(_code_ref[0])
        discovery_token  = parsed["discovery_token"]
        admission_secret = parsed["admission_secret"]

        # The capability check that stands between a stranger with a socket
        # and a seat at the table. Built before the Session, because the
        # Session refuses to be a host on this transport without one.
        host_admission = _admission.HostAdmission(
            admission_secret=bytes.fromhex(admission_secret),
            host_pubkey=_identity.public_key_bytes(),
        )

        # Build a session and register it globally
        sess = _session_mod.Session(
            is_host    = True,
            nickname   = self.nickname,
            avatar_b64 = getattr(self, "avatar_b64", ""),
            admission  = host_admission,
        )
        _p2p_pkg._session = sess

        # H-12: register the host under a stable local ID derived from the
        # Ed25519 public key — NOT inside an on_connect callback (which fires
        # for the first *remote* peer, not for the host itself).
        host_local_id = _identity.peer_id()
        sess.local_conn_id = host_local_id
        sess.add_local_player(host_local_id)

        # Wire transport callbacks — stale-callback fix: clear before registering
        # so repeated dialog opens don't accumulate duplicate handlers.
        _transport.reset_callbacks()
        _transport.on_message(sess.handle_message)
        _transport.on_disconnect(lambda cid: sess.handle_disconnect(cid))

        # Announce on LAN multicast in background
        threading.Thread(
            target=_transport.announce,
            args=(discovery_token, listen_addr),
            daemon=True,
        ).start()

        # ---- Build the lobby window ----
        win = tk.Toplevel(self.root)
        win.title("Create Game — Lobby")
        win.configure(bg=_PANEL)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        self.root.update_idletasks()
        dw, dh = 480, 400
        rx = self.root.winfo_rootx() + self.root.winfo_width()  // 2 - dw // 2
        ry = self.root.winfo_rooty() + self.root.winfo_height() // 2 - dh // 2
        win.geometry(f"{dw}x{dh}+{max(0, rx)}+{max(0, ry)}")

        # Header
        tk.Label(win, text="WAITING FOR PLAYERS",
                 bg=_PANEL, fg=_ACCENT,
                 font=("Segoe UI", 13, "bold")).pack(pady=(16, 4))

        # Room code display
        code_frame = tk.Frame(win, bg=_PANEL)
        code_frame.pack(fill="x", padx=20)
        tk.Label(code_frame, text="Room Code:",
                 bg=_PANEL, fg=_DIM,
                 font=("Segoe UI", 9)).pack(side="left")
        code_lbl = tk.Label(code_frame, text=_code_ref[0],
                            bg=_PANEL, fg=_GOLD,
                            font=("Consolas", 10, "bold"))
        code_lbl.pack(side="left", padx=8)

        def _copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(_code_ref[0])
        _btn(code_frame, "Copy", _copy).pack(side="left")

        # LAN address (for manual connections)
        tk.Label(win, text=f"LAN address: {listen_addr}",
                 bg=_PANEL, fg=_DIM,
                 font=("Segoe UI", 8)).pack(pady=(2, 2))

        # Connection info: STUN + relay status
        _stun_text = (
            "STUN: discovering…"
            if pub_addr is None
            else f"STUN: {pub_addr[0]}:{pub_addr[1]}"
        )
        conn_info_lbl = tk.Label(
            win,
            text=f"{_stun_text}  |  Relay: ready",
            bg=_PANEL, fg=_DIM,
            font=("Segoe UI", 8),
        )
        conn_info_lbl.pack(pady=(0, 6))

        # Poll for STUN resolution — update code and label when it arrives
        _stun_polls = [0]

        def _poll_stun():
            if not win.winfo_exists():
                return
            addr = _transport.get_public_address()
            _stun_polls[0] += 1
            if addr is not None:
                # Regenerate with the SAME discovery token and admission
                # secret, so neither LAN multicast nor an already-shared
                # invite is invalidated by STUN completing.
                _code_ref[0] = generate_room_code(
                    public_address=addr,
                    relay_address=relay_addr,
                    discovery_token=discovery_token,
                    admission_secret=admission_secret,
                )
                code_lbl.config(text=_code_ref[0])
                conn_info_lbl.config(
                    text=f"STUN: {addr[0]}:{addr[1]}  |  Relay: ready"
                )
            elif _stun_polls[0] < 10:
                win.after(500, _poll_stun)
            else:
                conn_info_lbl.config(
                    text="STUN unavailable, direct only  |  Relay: ready"
                )

        if pub_addr is None:
            win.after(500, _poll_stun)

        # Player list
        tk.Label(win, text="PLAYERS IN LOBBY",
                 bg=_PANEL, fg=_DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=20)

        player_frame = tk.Frame(win, bg=_FELT, pady=6)
        player_frame.pack(fill="both", expand=True, padx=20, pady=(4, 8))

        player_labels: dict[str, tk.Label] = {}

        def _refresh_players(players):
            # Called from background thread — schedule on Tk main thread
            win.after(0, lambda: _update_player_ui(players))

        def _update_player_ui(players):
            for w in player_frame.winfo_children():
                w.destroy()
            player_labels.clear()
            for p in players:
                tag      = "  [HOST]" if p.is_host else ""
                rdy_sym  = "  ✓" if p.ready else "  ○"
                lbl = tk.Label(
                    player_frame,
                    text=f"  {p.nickname}{tag}{rdy_sym}",
                    bg=_FELT,
                    fg=_ACCENT if p.ready else _TEXT,
                    font=("Segoe UI", 10),
                    anchor="w",
                )
                lbl.pack(fill="x", padx=8, pady=2)
                player_labels[p.conn_id] = lbl
            # Enable Start Game only when all players ready and >= 2 seated
            start_btn.config(state="normal" if sess.all_ready else "disabled")

        sess.on_player_list_changed = _refresh_players

        # Host's own entry (host is always ready)
        host_lbl = tk.Label(
            player_frame,
            text=f"  {self.nickname}  [HOST]  ✓",
            bg=_FELT, fg=_ACCENT,
            font=("Segoe UI", 10),
            anchor="w",
        )
        host_lbl.pack(fill="x", padx=8, pady=2)

        # Bottom bar
        bar = tk.Frame(win, bg=_PANEL)
        bar.pack(fill="x", padx=20, pady=(0, 14))

        def _close():
            _transport.stop()
            _p2p_pkg._session = None
            win.destroy()

        def _start_game():
            if not sess.all_ready:
                messagebox.showwarning(
                    "Not ready",
                    "All players must be ready before the host can start.",
                    parent=win)
                return
            # H-10: include betting structure and rule flags so peers can create
            # the correct Engine on their side.
            stored_rules = cfg.load()["last_table"]
            table_settings = {
                "sb":        stored_rules.get("sb",         10),
                "bb":        stored_rules.get("bb",         20),
                "stack":     stored_rules.get("stack",    1000),
                "structure": stored_rules.get("structure", "No-Limit"),
                "rit":       stored_rules.get("rit",       "Ask"),
                "straddles": stored_rules.get("straddles",  False),
                "clock_base": stored_rules.get("clock_base", 25),
            }
            try:
                sess.start_game(table_settings)
            except Exception as exc:
                messagebox.showerror("Error", str(exc), parent=win)
                return
            win.destroy()
            self._launch_mp_game(sess, is_host=True, local_seat=0,
                                 table_settings=table_settings)

        _btn(bar, "Cancel", _close).pack(side="left")
        start_btn = _btn(bar, "Start Game  →", _start_game,
                         accent=True, state="disabled")
        start_btn.pack(side="right")

        win.protocol("WM_DELETE_WINDOW", _close)

    def _join_game_dialog(self) -> None:
        """Show a dialog to paste a room invite code and connect to a host."""
        stored = cfg.load()
        last_code = stored["client"].get("last_room_code", "")

        win = tk.Toplevel(self.root)
        win.title("Join Game")
        win.configure(bg=_PANEL)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        self.root.update_idletasks()
        dw, dh = 480, 300
        rx = self.root.winfo_rootx() + self.root.winfo_width()  // 2 - dw // 2
        ry = self.root.winfo_rooty() + self.root.winfo_height() // 2 - dh // 2
        win.geometry(f"{dw}x{dh}+{max(0, rx)}+{max(0, ry)}")

        tk.Label(win, text="Join Game",
                 bg=_PANEL, fg=_ACCENT,
                 font=("Segoe UI", 13, "bold")).pack(pady=(18, 4))

        # Room code entry
        tk.Label(win, text="Room code (from host):",
                 bg=_PANEL, fg=_TEXT,
                 font=("Segoe UI", 9)).pack(pady=(8, 2))
        v_code = tk.StringVar(value=last_code)
        tk.Entry(win, textvariable=v_code, width=38,
                 bg=_BG, fg=_GOLD, insertbackground=_TEXT,
                 relief="flat", font=("Consolas", 11),
                 justify="center").pack(pady=(0, 8), padx=20)

        # Optional manual host:port override (for internet play)
        tk.Label(win,
                 text="Host address override (optional, for internet play):",
                 bg=_PANEL, fg=_DIM,
                 font=("Segoe UI", 8)).pack(pady=(4, 2))
        v_addr = tk.StringVar()
        tk.Entry(win, textvariable=v_addr, width=30,
                 bg=_BG, fg=_TEXT, insertbackground=_TEXT,
                 relief="flat", font=("Segoe UI", 10),
                 justify="center").pack(pady=(0, 8))

        status_lbl = tk.Label(win, text="",
                               bg=_PANEL, fg=_DIM,
                               font=("Segoe UI", 9))
        status_lbl.pack(pady=(0, 4))

        # Player list (shown after connecting)
        join_player_frame = tk.Frame(win, bg=_FELT)
        join_player_frame.pack(fill="x", padx=20, pady=(0, 4))

        bar = tk.Frame(win, bg=_PANEL)
        bar.pack(fill="x", padx=20, pady=(0, 14))

        def _connect():
            code = v_code.get().strip()
            if not code:
                messagebox.showwarning("Room code required",
                                       "Please enter the room code from the host.",
                                       parent=win)
                return

            try:
                parsed = parse_room_code(code)
            except ValueError as exc:
                messagebox.showerror("Invalid code", str(exc), parent=win)
                return

            discovery_token = parsed["discovery_token"]

            # Persist the code for next time
            stored2 = cfg.load()
            stored2["client"]["last_room_code"] = code
            cfg.save(stored2["client"], stored2["last_table"])

            addr_override = v_addr.get().strip()
            connect_btn.config(state="disabled")
            status_lbl.config(text="Searching for host…", fg=_DIM)
            win.update_idletasks()

            _ready_btn_ref       = [None]
            _conn_id_ref         = [None]
            _conn_method_ref     = ["Direct connection"]
            _sess_ref            = [None]   # C-2: share Session across nested fns
            _auth_ref            = [None]   # the JoinAuthenticator

            def _ui_post(fn):
                """The ONE way work off the Tk thread touches the UI.

                _do_connect runs on a worker thread and the transport
                delivers messages on its own, so every UI effect from either
                has to be marshalled. Tkinter is not thread-safe: a direct
                widget call from here is not reliably a visible error, it is
                undefined behaviour that usually looks fine.

                That matters beyond tidiness. This worker is the thread
                running admission; an exception raised inside a stray widget
                call would abandon the handshake midway, leaving transport
                and Session state half-established -- the kind of
                unrelated-looking failure that turns into a security-path
                bug later. Routing every effect through one seam keeps the
                authentication flow the only thing that can interrupt it.

                Pinned structurally by tests/test_join_auth.py.
                """
                win.after(0, fn)


            # ---- authentication is armed BEFORE anything is dialed ----
            #
            # Ordering is the whole point. The transport's reader thread
            # starts as part of connect(), so registering callbacks
            # afterwards leaves a window in which the first frames from the
            # far end reach either nothing or a Session that has not yet
            # been told to distrust them. Construct the pin, construct the
            # Session already refusing non-handshake traffic, install the
            # callbacks, and only then open a socket.
            #
            # The driver itself lives in holdem/p2p/join_auth.py so the
            # shipped ordering is the ordering under test; as closures in a
            # Tk callback it could only be exercised by driving the GUI.
            joiner_admission = _join_auth.joiner_admission_from_invite(parsed)
            sess = _session_mod.Session(
                is_host    = False,
                nickname   = self.nickname,
                avatar_b64 = getattr(self, "avatar_b64", ""),
                joiner_admission = joiner_admission,
            )
            _p2p_pkg._session = sess
            _sess_ref[0] = sess              # C-2: expose to _handle_start

            def _on_game_start(payload):
                _ui_post(lambda: _handle_start(payload))

            def _on_players(players):
                _ui_post(lambda: _update_joined_ui(players))

            sess.on_player_list_changed = _on_players
            sess.on_game_start          = _on_game_start

            # Both callbacks run on the TRANSPORT thread. Every UI effect
            # goes through win.after -- Tkinter is not thread-safe, and a
            # lobby that authenticates correctly while corrupting the widget
            # tree is not an improvement.
            def _authenticated(conn_id):
                _ui_post(lambda m=_conn_method_ref[0]: status_lbl.config(
                    text=f"Authenticated ({m}) — waiting for host to start…",
                    fg=_ACCENT))
                _ui_post(_show_ready_btn)

            def _auth_failed(reason):
                _ui_post(lambda r=reason: _on_error(
                    "Host verification failed",
                    "The host did not authenticate against this room code.\n"
                    f"{r}\n\nPossible man-in-the-middle — connection aborted."))

            authenticator = _join_auth.JoinAuthenticator(
                transport=_transport,
                session=sess,
                joiner_admission=joiner_admission,
                nickname=self.nickname,
                avatar_b64=getattr(self, "avatar_b64", ""),
                on_authenticated=_authenticated,
                on_failed=_auth_failed,
            )
            _auth_ref[0] = authenticator

            _transport.reset_callbacks()
            _transport.on_message(authenticator.route)
            _transport.on_disconnect(lambda cid: sess.handle_disconnect(cid))


            def _do_connect():
                public_ip   = parsed.get("public_ip")
                public_port = parsed.get("public_port")
                relay_host  = parsed.get("relay_host")
                relay_port  = parsed.get("relay_port")

                conn_method = ["Direct connection"]  # updated if relay used

                try:
                    if addr_override:
                        # Manual override: direct connect (existing behaviour)
                        conn_id = _transport.connect(addr_override)

                    elif public_ip and public_port:
                        # Try direct TCP to the STUN-discovered address (3 s)
                        try:
                            conn_id = _transport.connect(
                                f"{public_ip}:{public_port}"
                            )
                        except ConnectionError as direct_exc:
                            log.info(
                                "Direct connect failed (%s); trying relay",
                                direct_exc,
                            )
                            _ui_post(lambda: status_lbl.config(
                                text="Direct failed — trying relay…",
                                fg=_DIM,
                            ))
                            if relay_host and relay_port:
                                try:
                                    # The relay gets the PUBLIC routing
                                    # token only. It used to be handed
                                    # strip_code(code) -- the entire invite
                                    # -- which under V2 is the admission
                                    # secret and the host key pin together,
                                    # i.e. everything needed to impersonate
                                    # the host or join uninvited.
                                    conn_id = _transport.connect_via_relay(
                                        relay_host,
                                        relay_port,
                                        _invite.public_room_id(parsed),
                                    )
                                    conn_method[0] = "Relay (via errantsaints.space)"
                                except ConnectionError as relay_exc:
                                    # Last resort: try LAN multicast
                                    host_addr = _transport.find_peer(
                                        discovery_token, timeout=5
                                    )
                                    if not host_addr:
                                        _ui_post(lambda e=relay_exc: _on_error(
                                            "Connection failed",
                                            f"Direct and relay both failed:\n{e}",
                                        ))
                                        return
                                    conn_id = _transport.connect(host_addr)
                            else:
                                # No relay in code — try LAN multicast as fallback
                                host_addr = _transport.find_peer(
                                    discovery_token, timeout=5
                                )
                                if not host_addr:
                                    _ui_post(lambda e=direct_exc: _on_error(
                                        "Connection failed",
                                        f"Direct connect failed and no relay available:\n{e}",
                                    ))
                                    return
                                conn_id = _transport.connect(host_addr)

                    else:
                        # No STUN address in code → fall back to LAN multicast
                        host_addr = _transport.find_peer(discovery_token, timeout=15)
                        if not host_addr:
                            _ui_post(lambda: _on_error(
                                "Host not found",
                                "Could not locate the host on the local network.\n\n"
                                "If the host is on a different network, ask them for "
                                "their public IP:port and enter it in the address "
                                "override field.",
                            ))
                            return
                        conn_id = _transport.connect(host_addr)

                    # The connection that actually survived routing. Every
                    # step below belongs to THIS conn_id and no other: a
                    # direct attempt that failed and a relay attempt that
                    # succeeded are different sockets, and admission state
                    # must never be inherited across them.
                    # Routing has settled. Only now does a handshake exist:
                    # begin() draws a fresh client_nonce and binds admission
                    # to THIS conn_id, so a challenge answered on a socket
                    # that failed cannot be completed on its replacement.
                    _conn_id_ref[0]     = conn_id
                    _conn_method_ref[0] = conn_method[0]
                    _auth_ref[0].begin(conn_id)

                    _ui_post(lambda m=conn_method[0]: status_lbl.config(
                        text=f"Connected ({m}) — authenticating host…",
                        fg=_DIM,
                    ))

                except Exception as exc:
                    _ui_post(lambda e=exc: _on_error("Connection failed", str(e)))


            def _on_error(title, msg):
                connect_btn.config(state="normal")
                status_lbl.config(text="Connection failed.", fg="#e33b6d")
                messagebox.showerror(title, msg, parent=win)

            def _show_ready_btn():
                if _ready_btn_ref[0] is not None:
                    return
                def _send_ready():
                    import json as _json
                    ready_msg = _json.loads(
                        _wire.pack("ready", {"ready": True}))
                    _transport.broadcast(ready_msg)
                    status_lbl.config(
                        text="Ready! Waiting for host to start…",
                        fg=_ACCENT)
                    _ready_btn_ref[0].config(state="disabled")
                btn = _btn(bar, "I'm Ready  ✓", _send_ready, accent=True)
                btn.pack(side="left", padx=(8, 0))
                _ready_btn_ref[0] = btn

            def _update_joined_ui(players):
                for w in join_player_frame.winfo_children():
                    w.destroy()
                for p in players:
                    sym = "✓" if p.ready else "○"
                    clr = _ACCENT if p.ready else _TEXT
                    tk.Label(join_player_frame,
                             text=f"  {p.nickname}  {sym}",
                             bg=_FELT, fg=clr,
                             font=("Segoe UI", 9), anchor="w").pack(
                        fill="x", padx=6, pady=1)

            def _handle_start(payload):
                seat_order = payload.get("seat_order", [])
                local_cid  = _conn_id_ref[0]
                local_seat = (seat_order.index(local_cid)
                              if local_cid in seat_order else 1)
                ts = payload.get("table_settings",
                                 {"sb": 10, "bb": 20, "stack": 1000})

                sess = _sess_ref[0]
                if sess is None:
                    _ui_post(lambda: _on_error(
                        "Connection error",
                        "Session was lost before the game started.",
                    ))
                    return

                # M-7 is no longer checked here, because it can no longer
                # be reached unverified. The old check ran at THIS point --
                # after player_info, after the roster, after Ready -- and
                # compared only the first 8 bytes of the host key with
                # startswith(). Both problems are gone: the exact 32-byte key
                # is authenticated by the admission handshake before any
                # identity is sent, and a session that has not completed it
                # never leaves pre-auth, so no game_start can arrive.
                #
                # Asserted rather than assumed: reaching a game start on an
                # unauthenticated session would mean the gate leaked.
                if not getattr(sess, "_host_authenticated", True):
                    _ui_post(lambda: _on_error(
                        "Host verification failed",
                        "The game started on a connection that never "
                        "authenticated against this room code.",
                    ))
                    return

                win.destroy()
                self._launch_mp_game(sess, is_host=False,
                                     local_seat=local_seat,
                                     table_settings=ts)

            # _ready_btn_ref / _conn_id_ref already declared above _do_connect
            threading.Thread(target=_do_connect, daemon=True).start()

        connect_btn = _btn(bar, "Connect  →", _connect, accent=True)
        connect_btn.pack(side="right")
        _btn(bar, "Cancel", win.destroy).pack(side="left")

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

    # ---- multiplayer game launch

    def _launch_mp_game(self, sess, is_host: bool, local_seat: int,
                        table_settings: dict) -> None:
        """Tear down onboarding and hand control to the multiplayer Holdem."""
        self._save()
        cb       = self.on_mp_start
        nickname = self.nickname
        avatar_b64 = getattr(self, "avatar_b64", "")
        self.frame.destroy()
        if cb:
            cb(sess, is_host, local_seat, table_settings, nickname, avatar_b64)

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
