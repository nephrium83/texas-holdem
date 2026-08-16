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

import logging
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)


class DrainLoopError(RuntimeError):
    """The queue never emptied: a runaway message loop.

    Distinct from a handler raising, because the two want opposite
    responses. A failed handler is per-message and worth retrying past --
    the rest of the queue is still deliverable. A step-limit breach is a
    conclusion about the whole exchange, already reached by counting, and
    retrying it just repeats the work: 64 retries of a 100k-step limit is
    6.4 million deliveries on a socket the client is waiting on.
    """


class InMemoryBus:
    """Shared delivery fabric for a set of in-process sessions."""

    def __init__(self):
        self._sessions: Dict[str, object] = {}
        self._queue: List[Tuple[str, Optional[str], dict]] = []
        self._draining = False

    @property
    def pending(self) -> int:
        """Messages still queued. Lets a caller that survives handler
        failures tell whether delivery actually finished."""
        return len(self._queue)

    @property
    def is_draining(self) -> bool:
        """Whether a drain is in progress.

        For a caller that may enqueue from outside the normal command path
        and wants to deliver its own work WITHOUT becoming a second pump.
        If a drain is already running it will consume anything newly
        enqueued -- same queue -- so the right move is to skip, not to
        nest. drain() refuses re-entry outright, so this is how a caller
        asks rather than finds out by exception.
        """
        return self._draining

    def register(self, conn_id: str, session) -> None:
        self._sessions[conn_id] = session

    def unregister(self, conn_id: str) -> None:
        """Drop a session (simulates a disconnect)."""
        self._sessions.pop(conn_id, None)

    def enqueue(self, from_conn: str, to_conn: Optional[str], msg: dict) -> None:
        self._queue.append((from_conn, to_conn, msg))

    def enqueue_except(self, from_conn: str, exclude_conn: str,
                       msg: dict) -> None:
        """Deliver to every session but the sender and one exclusion.

        The relay seam: a host forwards a joiner's envelope onward without
        echoing it back to the joiner that sent it.
        """
        for c in list(self._sessions):
            if c != from_conn and c != exclude_conn:
                self._queue.append((from_conn, c, msg))

    def drain(self, max_steps: int = 100000) -> int:
        """Deliver queued messages until the queue is empty. Returns the
        number of messages delivered. Raises if it exceeds max_steps
        (a runaway message loop), or if re-entered from inside a handler.

        Not re-entrant, and enforced rather than documented. A handler that
        calls drain() while one is already running consumes the SHARED queue
        from underneath the outer loop: every message enqueued so far is
        delivered before the outer loop resumes, so ordering stops being
        FIFO. Nothing is lost and nothing recurses forever, which is exactly
        what made this expensive -- it surfaced as a ~20 % hand-desync rate
        rather than as a crash. The rule for callbacks is: enqueue, and let
        the active drain consume. The guard turns a probabilistic desync
        into a deterministic, immediate failure at the offending call.
        """
        if self._draining:
            raise RuntimeError(
                "InMemoryBus.drain() re-entered from inside a message "
                "handler. Handlers must enqueue and let the already-active "
                "drain consume; a nested drain reorders the shared queue.")
        self._draining = True
        try:
            steps = 0
            while self._queue:
                if steps >= max_steps:
                    raise DrainLoopError(
                        "InMemoryBus.drain exceeded max_steps (message loop?)")
                from_conn, to_conn, msg = self._queue.pop(0)
                steps += 1
                if to_conn is not None:
                    targets = [to_conn] if to_conn in self._sessions else []
                else:
                    targets = [c for c in self._sessions if c != from_conn]
                # Per-target isolation: one raising handler must not stop
                # the other targets from receiving a broadcast that has
                # ALREADY been dequeued. Without this, a game_start whose
                # first recipient throws is popped and only partially
                # delivered -- strictly worse than never being sent, since
                # it can never be redelivered, and the remaining peers sit
                # in LOBBY forever. Every target is attempted; the first
                # exception is re-raised afterwards so nothing is silently
                # swallowed.
                first_error = None
                for c in targets:
                    sess = self._sessions.get(c)
                    if sess is None:
                        continue
                    try:
                        sess.handle_message(from_conn, dict(msg))
                    except Exception as exc:       # noqa: BLE001 - re-raised
                        if first_error is None:
                            first_error = exc
                        else:
                            # Only the first propagates. Log the rest rather
                            # than dropping them: a second failing peer is a
                            # separate fact, and losing it silently is how a
                            # multi-peer failure reads as a single-peer one.
                            _log.exception(
                                "InMemoryBus: additional handler failure "
                                "delivering %s to %s", msg.get("type"), c,
                                exc_info=exc)
                if first_error is not None:
                    raise first_error
            return steps
        finally:
            self._draining = False


class InMemoryTransport:
    """Per-session facade with the same broadcast()/send() surface the
    Session calls on the real transport module. Forwards to the shared bus,
    tagging the sender's conn_id."""

    #: Flat dicts, no envelopes, no signatures. Session reads this to choose
    #: AUTHOR_MODE_COMPAT, where seat authority falls back to the delivering
    #: conn_id -- correct here, because there is no author to check and the
    #: bus is authoritative about who sent what.
    delivers_verified_envelopes = False

    def __init__(self, bus: InMemoryBus, conn_id: str):
        self._bus = bus
        self._conn_id = conn_id

    def broadcast(self, msg: dict) -> None:
        self._bus.enqueue(self._conn_id, None, msg)

    def broadcast_except(self, exclude_conn_id: str, msg: dict) -> None:
        """Broadcast to everyone but one peer -- the relay seam."""
        self._bus.enqueue_except(self._conn_id, exclude_conn_id, msg)

    def send(self, to_conn: str, msg: dict) -> None:
        self._bus.enqueue(self._conn_id, to_conn, msg)


__all__ = ["InMemoryBus", "InMemoryTransport"]
