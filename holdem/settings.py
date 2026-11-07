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
import contextlib
import logging
import json
import os
import tempfile
import threading
import time
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

# Retained for older callers/tests which clear it; disk reads are fresh.
_cache: dict | None = None
_log = logging.getLogger(__name__)
_writer_lock = threading.RLock()


class _Bucket(dict):
    def __init__(self, values):
        super().__init__(values)
        self.baseline = dict(values)


@contextlib.contextmanager
def _json_lock(path: Path):
    """Serialize local threads and processes around a fresh read/merge/write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _writer_lock, open(path.with_name(path.name + ".lock"), "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"0")
                lock.flush()
            deadline = time.monotonic() + 5
            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("settings/notes writer lock timed out")
                    time.sleep(0.01)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def _read_json_mapping(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return raw


def _write_json(path: Path, value: dict) -> None:
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(value, target, indent=2, sort_keys=True, ensure_ascii=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _settings_from_raw(raw: dict) -> dict:
    result = {"client": defaults(CLIENT), "last_table": defaults(TABLE_RULE)}
    for name, values in result.items():
        source = raw.get(name, {})
        if not isinstance(source, dict):
            raise ValueError(f"settings bucket {name} must be an object")
        for key, value in source.items():
            if key in values:
                normalized = _valid(key, value)
                if normalized is not None:
                    values[key] = normalized
    return result


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
    """Fresh complete settings; missing files default, other failures log.

    Failed reads are never cached or made into a writable replacement. A
    later save re-reads under the writer lock and refuses corrupt/unreadable
    input, preserving its bytes for explicit recovery.
    """
    try:
        stored = _settings_from_raw(_read_json_mapping(config_path()))
        stored["last_table"] = table_rules(**stored["last_table"])
    except (OSError, ValueError, TypeError) as exc:
        _log.warning("settings read failed; original preserved: %s", exc)
        stored = {"client": defaults(CLIENT), "last_table": defaults(TABLE_RULE)}
    return {name: _Bucket(values) for name, values in stored.items()}


def get(key: str):
    """Read a single CLIENT key from the persisted file (or its default)."""
    spec = SPEC.get(key)
    if spec is None or spec["scope"] != CLIENT:
        raise KeyError(key)
    return load()["client"][key]


def set(key: str, value) -> bool:
    """Write only the requested CLIENT key in a locked fresh transaction."""
    spec = SPEC.get(key)
    if spec is None or spec["scope"] != CLIENT:
        raise KeyError(key)
    if _valid(key, value) is None:
        return False
    return save({key: value}, {})


def _changes(bucket: dict, scope: str) -> dict:
    baseline = getattr(bucket, "baseline", {})
    changes = {}
    for key, value in bucket.items():
        if key not in SPEC or SPEC[key]["scope"] != scope:
            continue
        normalized = _valid(key, value)
        if normalized is not None and normalized != baseline.get(key):
            changes[key] = normalized
    return changes


def save(client: dict, last_table: dict) -> bool:
    """Persist normalized changes with interprocess serialization.

    Buckets returned by load retain their baseline, so unrelated values in
    stale snapshots never overwrite newer disk values. Plain dicts are
    explicit patches: supplied keys are intentional, omitted keys preserved.
    Failure returns False and logs; no unreadable original is overwritten.
    """
    global _cache
    try:
        patches = {"client": _changes(client, CLIENT),
                   "last_table": _changes(last_table, TABLE_RULE)}
        path = config_path()
        with _json_lock(path):
            current = _settings_from_raw(_read_json_mapping(path))
            for name, changes in patches.items():
                current[name].update(changes)
            current["last_table"] = table_rules(**current["last_table"])
            _write_json(path, current)
        _cache = None
        for bucket, scope in ((client, CLIENT), (last_table, TABLE_RULE)):
            if isinstance(bucket, _Bucket):
                bucket.baseline = {key: _valid(key, value) for key, value in bucket.items()
                                   if key in SPEC and SPEC[key]["scope"] == scope}
        return True
    except (OSError, ValueError, TypeError) as exc:
        _log.warning("settings save failed; original preserved: %s", exc)
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
    if rules["sb"] >= rules["bb"]:
        raise ValueError("small blind must be less than big blind")
    if rules["stack"] < rules["bb"]:
        raise ValueError("starting stack must cover the big blind")
    if rules["buyin_min_bb"] > rules["buyin_max_bb"]:
        raise ValueError("minimum buy-in must not exceed maximum buy-in")
    if rules["bank_start"] > rules["bank_cap"]:
        raise ValueError("initial time bank must not exceed its cap")
    return rules


def rules_hash(rules: dict) -> str:
    """Ten hex chars identifying the contract. Canonical (sorted keys,
    compact separators) so every client derives the same value -- this is
    what a multiplayer join code embeds."""
    rules = table_rules(**rules)
    canon = json.dumps({k: rules[k] for k in sorted(TABLE_RULE_KEYS)},
                       separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(canon.encode('utf-8')).hexdigest()[:10]
