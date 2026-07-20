"""In-memory transport for exercising multi-session flows in one process.

The real holdem.p2p.transport is a global module over asyncio sockets:
one process = one peer = one transport. To drive several Session instances
together in a unit test, each Session is constructed with its own
InMemoryTransport (via Session(transport=...)), all sharing one
InMemoryBus.

Delivery mirrors the real transport's semantics deliberately, so tests
exercise reality rather than a convenient fiction:
  * broadcast(msg) reaches every OTHER registered session, NOT the sender.
    (A component that needs to act on its own broadcast -- e.g. the
    mental-poker shuffle chain -- must self-deliver explicitly; the bus
    will not echo to the sender.)
  * send(to, msg) reaches exactly the one addressed session.

Delivery is queued, not immediate: broadcast()/send() enqueue, and the
test calls bus.drain() to run the exchange to quiescence. This prevents a
handler that emits further messages from recursing, and makes ordering
deterministic (FIFO), the same discipline used by the coordinator's own
test harnesses.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple


class InMemoryBus:
    """Shared delivery fabric for a set of in-process sessions."""

    def __init__(self):
        self._sessions: Dict[str, object] = {}
        self._queue: List[Tuple[str, Optional[str], dict]] = []

    def register(self, conn_id: str, session) -> None:
        self._sessions[conn_id] = session

    def unregister(self, conn_id: str) -> None:
        """Drop a session (simulates a disconnect)."""
        self._sessions.pop(conn_id, None)

    def enqueue(self, from_conn: str, to_conn: Optional[str], msg: dict) -> None:
        self._queue.append((from_conn, to_conn, msg))

    def drain(self, max_steps: int = 100000) -> int:
        """Deliver queued messages until the queue is empty. Returns the
        number of messages delivered. Raises if it exceeds max_steps
        (a runaway message loop)."""
        steps = 0
        while self._queue:
            if steps >= max_steps:
                raise RuntimeError(
                    "InMemoryBus.drain exceeded max_steps (message loop?)")
            from_conn, to_conn, msg = self._queue.pop(0)
            steps += 1
            if to_conn is not None:
                targets = [to_conn] if to_conn in self._sessions else []
            else:
                targets = [c for c in self._sessions if c != from_conn]
            for c in targets:
                sess = self._sessions.get(c)
                if sess is not None:
                    sess.handle_message(from_conn, dict(msg))
        return steps


class InMemoryTransport:
    """Per-session facade with the same broadcast()/send() surface the
    Session calls on the real transport module. Forwards to the shared bus,
    tagging the sender's conn_id."""

    def __init__(self, bus: InMemoryBus, conn_id: str):
        self._bus = bus
        self._conn_id = conn_id

    def broadcast(self, msg: dict) -> None:
        self._bus.enqueue(self._conn_id, None, msg)

    def send(self, to_conn: str, msg: dict) -> None:
        self._bus.enqueue(self._conn_id, to_conn, msg)


__all__ = ["InMemoryBus", "InMemoryTransport"]
