"""Persistent per-peer opponent notes stored in ~/.texas_holdem_notes.json.

Each entry is keyed by peer_id (hex connection-id string) and holds:
  peer_id    : str  -- the key (also stored inside the dict for convenience)
  nickname   : str  -- last-seen display name, updated on each session
  color      : str  -- label color: red/orange/yellow/green/blue/purple/none
  note       : str  -- free-text note about this player
  last_seen  : str  -- ISO-8601 timestamp of the last update

API (write-through on every change; load on first access):
  get(peer_id)                  -> dict
  set_color(peer_id, color, nickname=None)
  set_note(peer_id, text,  nickname=None)
  update_nickname(peer_id, nickname)
  all()                         -> list[dict]  sorted by last_seen desc
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

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


def _load() -> dict[str, dict]:
    global _data
    if _data is not None:
        return _data
    try:
        raw = json.loads(_NOTES_FILE.read_text(encoding="utf-8"))
        _data = raw if isinstance(raw, dict) else {}
    except Exception:
        _data = {}
    return _data


def _save() -> None:
    try:
        _NOTES_FILE.write_text(
            json.dumps(_data, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
    except Exception:
        pass


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


def set_color(peer_id: str, color: str, nickname: str | None = None) -> None:
    """Persist a color label for peer_id.  color must be in _VALID_COLORS."""
    if color not in _VALID_COLORS:
        return
    d = _load()
    entry = d.setdefault(peer_id, _defaults())
    entry["color"] = color
    if nickname:
        entry["nickname"] = nickname
    entry["last_seen"] = _now()
    _save()


def set_note(peer_id: str, text: str, nickname: str | None = None) -> None:
    """Persist a free-text note for peer_id."""
    d = _load()
    entry = d.setdefault(peer_id, _defaults())
    entry["note"] = text
    if nickname:
        entry["nickname"] = nickname
    entry["last_seen"] = _now()
    _save()


def update_nickname(peer_id: str, nickname: str) -> None:
    """Update the last-seen nickname for peer_id without touching other fields."""
    d = _load()
    entry = d.setdefault(peer_id, _defaults())
    entry["nickname"] = nickname
    entry["last_seen"] = _now()
    _save()


def all() -> list[dict]:
    """Return all note entries as a list of dicts sorted by last_seen descending."""
    d = _load()
    result = []
    for pid, entry in d.items():
        item = {**_defaults(), **entry, "peer_id": pid}
        result.append(item)
    result.sort(key=lambda x: x.get("last_seen", ""), reverse=True)
    return result
