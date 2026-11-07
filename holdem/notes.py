"""Persistent per-peer opponent notes stored in ~/.texas_holdem_notes.json.

Each entry is keyed by peer_id (hex connection-id string) and holds:
  peer_id    : str  -- the key (also stored inside the dict for convenience)
  nickname   : str  -- last-seen display name, updated on each session
  color      : str  -- label color: red/orange/yellow/green/blue/purple/none
  note       : str  -- free-text note about this player
  last_seen  : str  -- ISO-8601 timestamp of the last update

API (fresh reads and locked atomic writes; mutations return success):
  get(peer_id)                  -> dict
  set_color(peer_id, color, nickname=None)
  set_note(peer_id, text,  nickname=None)
  update_nickname(peer_id, nickname)
  all()                         -> list[dict]  sorted by last_seen desc
"""
from __future__ import annotations

import datetime
import logging
from pathlib import Path

from holdem.settings import _json_lock, _read_json_mapping, _write_json

_log = logging.getLogger(__name__)

_NOTES_FILE: Path = Path.home() / ".texas_holdem_notes.json"

_VALID_COLORS: frozenset[str] = frozenset(
    {"red", "orange", "yellow", "green", "blue", "purple", "none"}
)

# In-memory cache; None = not loaded yet.
_data: dict[str, dict] | None = None


# ------------------------------------------------------------------ helpers

def _defaults() -> dict:
    return {"nickname": "", "color": "none", "note": "", "last_seen": ""}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _read_disk() -> dict:
    raw = _read_json_mapping(_NOTES_FILE)
    if any(not isinstance(entry, dict) for entry in raw.values()):
        raise ValueError("notes entries must be objects")
    return raw


def _load() -> dict[str, dict]:
    try:
        return _read_disk()
    except (OSError, ValueError) as exc:
        _log.warning("notes read failed; original preserved: %s", exc)
        return {}


def _update(peer_id: str, **changes) -> bool:
    global _data
    try:
        with _json_lock(_NOTES_FILE):
            current = _read_disk()
            entry = {**_defaults(), **current.get(peer_id, {}), **changes}
            entry["last_seen"] = _now()
            current[peer_id] = entry
            _write_json(_NOTES_FILE, current)
        _data = current
        return True
    except (OSError, ValueError, TypeError) as exc:
        _log.warning("notes save failed; original preserved: %s", exc)
        return False


# ------------------------------------------------------------------ public API

def get(peer_id: str) -> dict:
    """Return the note entry for peer_id, or defaults if not found.

    The returned dict always contains: peer_id, nickname, color, note, last_seen.
    """
    d = _load()
    entry = d.get(peer_id, {})
    result = {**_defaults(), **entry}
    result["peer_id"] = peer_id
    return result


def set_color(peer_id: str, color: str, nickname: str | None = None) -> bool:
    """Persist a color label; report a failed write instead of pretending."""
    if color not in _VALID_COLORS:
        return False
    return _update(peer_id, color=color, **({"nickname": nickname} if nickname else {}))


def set_note(peer_id: str, text: str, nickname: str | None = None) -> bool:
    """Persist a free-text note in a fresh locked read/merge/write."""
    return _update(peer_id, note=text, **({"nickname": nickname} if nickname else {}))


def update_nickname(peer_id: str, nickname: str) -> bool:
    return _update(peer_id, nickname=nickname)


def all() -> list[dict]:
    """Return all note entries as a list of dicts sorted by last_seen descending."""
    d = _load()
    result = []
    for pid, entry in d.items():
        item = {**_defaults(), **entry, "peer_id": pid}
        result.append(item)
    result.sort(key=lambda x: x.get("last_seen", ""), reverse=True)
    return result
