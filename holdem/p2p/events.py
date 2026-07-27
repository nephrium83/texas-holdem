"""Structured JSONL event logging for the P2P sidecar.

Every state transition worth observing is emitted as one JSON object per line
to stdout, flushed immediately.  Warnings, errors, and stack traces go to
stderr.  Tests inject a ListSink to assert on events without reading stdout.

Schema version: 1

Event set
---------
sidecar_started    -- session constructed
peer_connected     -- a player (local or remote) joined the session
hand_started       -- a new hand has been initialized
action_received    -- a remote bet-action message arrived and passed validation
action_applied     -- apply_action returned "applied" (local or remote)
timeout_proposed   -- this peer broadcast a timeout_proposal
timeout_applied    -- a timeout was accepted and acted on
hand_voided        -- the current hand is being voided
peer_unavailable   -- a peer was marked unavailable (deal timeout)
digest_changed     -- the replica state digest moved to a new value
sidecar_stopping   -- the session has finished (winner decided or ended)

Do not log on every socket read — that produces archaeology, not diagnostics.
"""
from __future__ import annotations

import json
import sys
from typing import Protocol, runtime_checkable

SCHEMA_VERSION = 1


@runtime_checkable
class EventSink(Protocol):
    """Anything that accepts a dict and does something with it."""
    def emit(self, event: dict) -> None: ...


class StdoutSink:
    """Writes one JSON object per line to stdout, flushed immediately.

    Uses sys.stdout.write rather than print() to avoid appending an extra
    newline on some platforms and to keep the stream unambiguous when the
    host process captures stdout line-by-line.
    """
    def emit(self, event: dict) -> None:
        sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
        sys.stdout.flush()


class NullSink:
    """Discards all events.  Default when logging is not configured."""
    def emit(self, event: dict) -> None:
        pass


class ListSink:
    """Accumulates events in a list.  Use in tests to assert on the
    event stream without reading stdout."""
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(dict(event))

    def of_type(self, event_name: str) -> list[dict]:
        """Return every event whose ``event`` field matches *event_name*."""
        return [e for e in self.events if e.get("event") == event_name]

    def last(self, event_name: str) -> dict | None:
        """Return the most recent event of a given type, or None."""
        matches = self.of_type(event_name)
        return matches[-1] if matches else None
