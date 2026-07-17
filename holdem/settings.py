"""Settings taxonomy and persistence.

Every option the game exposes has exactly one scope, and the scope
decides who may change it and when:

- CLIENT      this machine only: look, feel, pace, local conveniences.
              Persisted to a per-user config file and applied at launch.
- TABLE_RULE  the contract every seat plays under: stakes, structure,
              timing, and which extras are allowed. Single-player, these
              are yours to set. At a live multiplayer table they are
              fixed by the join code (the rules hash below) and change
              only by unanimous, signed amendment.
- SEAT        per-seat lifecycle actions (sit out, straddle arm, top-up).
              Not settings at all: they are protocol messages, rendered
              as buttons, never persisted.

The multiplayer design in docs/MULTIPLAYER.md builds directly on this
module: a table's join code embeds `rules_hash(...)` so every client can
verify it is playing the same game.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

CLIENT = "client"
TABLE_RULE = "table_rule"
SEAT = "seat"

# Every option, one scope each. `kind` is bool / choice / int / action.
# `live` marks table rules that may be amended between hands mid-session
# (single-player; multiplayer requires unanimous consent). `note` flags
# options with multiplayer consequences.
SPEC = {
    # ---- client -----------------------------------------------------
    "theme":        dict(scope=CLIENT, kind="choice", default="Cyberpunk",
                         choices=["Cyberpunk", "Classic Felt"],
                         label="Theme"),
    "speed":        dict(scope=CLIENT, kind="choice", default="Normal",
                         choices=["Slow", "Normal", "Fast", "Instant"],
                         label="Game speed",
                         note="animation pace only; table pace is the clock"),
    "reveal":       dict(scope=CLIENT, kind="choice",
                         default="Realistic (muck losers)",
                         choices=["Winner only", "Realistic (muck losers)",
                                  "Everyone"],
                         label="Show cards at",
                         note="display-only vs AI; under mental poker a "
                              "mucked card stays encrypted unless the table "
                              "consents to open it"),
    "hints":        dict(scope=CLIENT, kind="bool", default=True,
                         label="Coaching hints",
                         note="training aid: hidden at human tables unless "
                              "the table rule allows it"),
    "odds":         dict(scope=CLIENT, kind="bool", default=True,
                         label="Live equity readout",
                         note="training aid: hidden at human tables unless "
                              "the table rule allows it"),
    "auto_deal":    dict(scope=CLIENT, kind="bool", default=False,
                         label="Auto-deal next hand"),
    "clock_on":     dict(scope=CLIENT, kind="bool", default=True,
                         label="Action clock (25s + time bank)",
                         note="single-player convenience; at a live table "
                              "the clock is the liveness rule and is not "
                              "optional"),
    "observe":     dict(scope=CLIENT, kind="bool", default=False,
                         label="Observe mode (AI plays your seat)",
                         note="single-player only; at a human table this "
                              "is botting"),
    "ai_topup":     dict(scope=CLIENT, kind="bool", default=True,
                         label="AI auto top-up (cash)",
                         note="single-player only: opponents rebuy to the "
                              "buy-in between hands"),
    "ai_mixed":     dict(scope=CLIENT, kind="bool", default=True,
                         label="Mixed AI skill levels",
                         note="single-player only"),
    "ai_level":     dict(scope=CLIENT, kind="int", default=2,
                         label="AI skill (1-3)", lo=1, hi=3,
                         note="single-player only"),
    "fullscreen":   dict(scope=CLIENT, kind="bool", default=True,
                         label="Fullscreen (F11 to toggle)",
                         note="window launches maximised; persists across "
                              "sessions; F11 toggles between maximised and "
                              "normal windowed mode"),

    # ---- audio (CLIENT) --------------------------------------------
    "sounds_enabled": dict(scope=CLIENT, kind="bool", default=True,
                           label="Sound effects"),
    "sound_volume":   dict(scope=CLIENT, kind="int",  default=70,
                           label="Sound volume (0-100)", lo=0, hi=100),

    # ---- ui polish (CLIENT) -----------------------------------------
    "four_color_deck": dict(scope=CLIENT, kind="bool", default=False,
                            label="Four-color deck"),
    "felt_color":      dict(scope=CLIENT, kind="str",  default="#35654d",
                            maxlen=16, label="Table felt color"),
    "bet_buttons":     dict(scope=CLIENT, kind="str",  default="0.5,1,2,3",
                            maxlen=64,
                            label="Bet-size shortcuts (fractions, comma-sep)"),

    # ---- bankroll / XP / progression (CLIENT) ----------------------
    "bankroll":              dict(scope=CLIENT, kind="int", default=10000,
                                  label="Bankroll", lo=0, hi=100_000_000),
    "xp":                    dict(scope=CLIENT, kind="int", default=0,
                                  label="XP", lo=0, hi=100_000_000),
    "player_level":          dict(scope=CLIENT, kind="int", default=1,
                                  label="Player level", lo=1, hi=50),
    "hands_played_total":    dict(scope=CLIENT, kind="int", default=0,
                                  label="Hands played (all-time)", lo=0,
                                  hi=100_000_000),
    "last_daily_bonus_date": dict(scope=CLIENT, kind="str", default="",
                                  maxlen=10, label="Last daily bonus date"),

    # ---- onboarding / player identity (CLIENT, local machine) -------
    "nickname":     dict(scope=CLIENT, kind="str", default="",
                         maxlen=20, label="Nickname"),
    "avatar_idx":   dict(scope=CLIENT, kind="int", default=0,
                         label="Avatar (built-in index)", lo=0, hi=15),
    "avatar_path":  dict(scope=CLIENT, kind="str", default="",
                         maxlen=512, label="Avatar (custom image path)"),
    "avatar_b64":   dict(scope=CLIENT, kind="str", default="",
                         maxlen=12000, label="Avatar (base64 PNG thumbnail)"),
    "last_room_code": dict(scope=CLIENT, kind="str", default="",
                           maxlen=64, label="Last used room code (Join pre-fill)"),

    # ---- table rules ------------------------------------------------
    "mode":         dict(scope=TABLE_RULE, kind="choice", default="Cash",
                         choices=["Cash", "Tournament"], label="Game"),
    "structure":    dict(scope=TABLE_RULE, kind="choice", default="No-Limit",
                         choices=["No-Limit", "Pot-Limit", "Fixed-Limit"],
                         label="Betting"),
    "sb":           dict(scope=TABLE_RULE, kind="int", default=10,
                         label="Small blind", lo=1, hi=5000),
    "bb":           dict(scope=TABLE_RULE, kind="int", default=20,
                         label="Big blind", lo=2, hi=10000),
    "stack":        dict(scope=TABLE_RULE, kind="int", default=1000,
                         label="Starting stack", lo=20, hi=100000),
    "players":      dict(scope=TABLE_RULE, kind="int", default=6,
                         label="Players", lo=2, hi=9),
    "bb_ante":      dict(scope=TABLE_RULE, kind="bool", default=True,
                         label="Big blind ante (tournament)"),
    "level_minutes": dict(scope=TABLE_RULE, kind="int", default=8,
                          label="Level minutes", lo=3, hi=30),
    "rit":          dict(scope=TABLE_RULE, kind="choice", default="Ask",
                         choices=["Ask", "Always", "Never"],
                         label="Run it twice", live=True),
    "straddles":    dict(scope=TABLE_RULE, kind="bool", default=False,
                         label="Allow straddles (cash, big-bet)", live=True),
    "rabbit":       dict(scope=TABLE_RULE, kind="bool", default=True,
                         label="Rabbit hunting", live=True,
                         note="under mental poker this costs the table a "
                              "cooperative decryption round"),
    "training_aids": dict(scope=TABLE_RULE, kind="bool", default=True,
                          label="Allow training aids", live=True,
                          note="gates hints and live equity; defaults off "
                               "at human tables"),
    "buyin_min_bb": dict(scope=TABLE_RULE, kind="int", default=40,
                         label="Min buy-in (BB)", lo=10, hi=500),
    "buyin_max_bb": dict(scope=TABLE_RULE, kind="int", default=100,
                         label="Max buy-in (BB)", lo=20, hi=1000),
    "clock_base":   dict(scope=TABLE_RULE, kind="int", default=25,
                         label="Action clock (s)", lo=5, hi=120),
    "bank_start":   dict(scope=TABLE_RULE, kind="int", default=60,
                         label="Time bank (s)", lo=0, hi=600),
    "bank_topup":   dict(scope=TABLE_RULE, kind="int", default=10,
                         label="Bank top-up per orbit (s)", lo=0, hi=60),
    "bank_cap":     dict(scope=TABLE_RULE, kind="int", default=120,
                         label="Time bank cap (s)", lo=0, hi=900),

    # ---- seat actions (protocol messages, never persisted) ----------
    "sit_out":      dict(scope=SEAT, kind="action", label="Sit out / I'm back"),
    "straddle_arm": dict(scope=SEAT, kind="action", label="Straddle toggle"),
    "top_up":       dict(scope=SEAT, kind="action", label="Add chips"),
}

TABLE_RULE_KEYS = tuple(k for k, s in SPEC.items() if s["scope"] == TABLE_RULE)
CLIENT_KEYS = tuple(k for k, s in SPEC.items() if s["scope"] == CLIENT)


def defaults(scope):
    return {k: s["default"] for k, s in SPEC.items()
            if s["scope"] == scope and "default" in s}


def _valid(key, value):
    s = SPEC.get(key)
    if s is None:
        return None
    kind = s["kind"]
    if kind == "str":
        if not isinstance(value, str):
            return None
        return value[:s.get("maxlen", 512)]
    if kind == "bool":
        return bool(value) if isinstance(value, bool) else None
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return min(s.get("hi", value), max(s.get("lo", value), value))
    if kind == "choice":
        return value if value in s["choices"] else None
    return None


# ---------------------------------------------------------------- config

_cache: dict | None = None   # M-14: in-memory cache; invalidated on save()


def config_dir() -> Path:
    env = os.environ.get("HOLDEM_CONFIG_DIR")
    if env:
        return Path(env)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "holdem"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "holdem"


def config_path() -> Path:
    return config_dir() / "settings.json"


def load() -> dict:
    """Return {'client': {...}, 'last_table': {...}}, always complete.

    Unknown keys are dropped, invalid values fall back to defaults, and a
    missing or corrupt file yields pure defaults. Never raises.
    Result is cached in memory until save() invalidates it (M-14).
    """
    global _cache
    if _cache is not None:
        return _cache
    client = defaults(CLIENT)
    table = defaults(TABLE_RULE)
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    for bucket, store in (("client", client), ("last_table", table)):
        src = raw.get(bucket)
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            ok = _valid(k, v)
            if ok is not None and k in store:
                store[k] = ok
    _cache = {"client": client, "last_table": table}
    return _cache


def get(key: str):
    """Read a single CLIENT key from the persisted file (or its default)."""
    spec = SPEC.get(key)
    if spec is None or spec["scope"] != CLIENT:
        raise KeyError(key)
    return load()["client"].get(key, spec["default"])


def set(key: str, value) -> bool:
    """Write a single CLIENT key atomically. Returns True on success."""
    spec = SPEC.get(key)
    if spec is None or spec["scope"] != CLIENT:
        raise KeyError(key)
    stored = load()
    # Mutate a copy so the cache stays valid if save() fails
    import copy
    stored = copy.deepcopy(stored)
    stored["client"][key] = value
    return save(stored["client"], stored["last_table"])


def save(client: dict, last_table: dict) -> bool:
    """Atomically persist both buckets. Best-effort: never raises.
    Invalidates the in-memory cache on success (M-14)."""
    global _cache
    out = {
        "client": {k: v for k, v in client.items()
                   if _valid(k, v) is not None and SPEC[k]["scope"] == CLIENT},
        "last_table": {k: v for k, v in last_table.items()
                       if _valid(k, v) is not None
                       and SPEC[k]["scope"] == TABLE_RULE},
    }
    try:
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "settings.json.tmp"
        tmp.write_text(json.dumps(out, indent=2, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, config_path())
        _cache = None   # invalidate cache after successful write
        return True
    except Exception:
        return False


# ----------------------------------------------------------- table rules

def table_rules(**values) -> dict:
    """A complete, validated rules dict: SPEC defaults overlaid with the
    given values. This is the table contract."""
    rules = defaults(TABLE_RULE)
    for k, v in values.items():
        ok = _valid(k, v)
        if ok is not None and k in rules:
            rules[k] = ok
    return rules


def rules_hash(rules: dict) -> str:
    """Ten hex chars identifying the contract. Canonical (sorted keys,
    compact separators) so every client derives the same value -- this is
    what a multiplayer join code embeds."""
    canon = json.dumps({k: rules[k] for k in sorted(TABLE_RULE_KEYS)},
                       separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(canon.encode('utf-8')).hexdigest()[:10]
