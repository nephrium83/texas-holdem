"""Validates the in-memory session harness: real Session instances, each
with an injected InMemoryTransport over a shared bus, actually exchange
messages. This is the safety net that later step-3 tests (deal wiring,
hostless betting) build on -- so it must prove the plumbing works before
anything is wired into session.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.session import Session
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport


def make_sessions(n):
    """n Sessions (peer0 is host) wired to one shared bus, with conn_ids
    assigned directly (bypassing the lobby handshake)."""
    bus = InMemoryBus()
    sessions = {}
    for i in range(n):
        cid = f"peer{i}"
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid))
        s.local_conn_id = cid
        if not s.is_host:
            s._host_conn_id = "peer0"
        bus.register(cid, s)
        sessions[cid] = s
    return bus, sessions


def _capture_chat(sessions):
    got = {c: [] for c in sessions}
    for c, s in sessions.items():
        s.on_chat = (lambda cc: (lambda nick, text: got[cc].append((nick, text))))(c)
    return got


def test_broadcast_reaches_other_sessions():
    """A host re-broadcast (triggered by a received chat) reaches every
    peer via the injected transport + bus."""
    bus, sessions = make_sessions(3)
    got = _capture_chat(sessions)
    chat = {"type": "chat", "payload": {"nickname": "P1", "text": "hi"}}
    bus.enqueue("peer1", "peer0", chat)          # peer1 -> host
    bus.drain()
    # host processed it, and re-broadcast reached both peers (incl. sender)
    assert ("P1", "hi") in got["peer0"]
    assert ("P1", "hi") in got["peer1"]
    assert ("P1", "hi") in got["peer2"]


def test_broadcast_excludes_sender():
    """The bus mirrors the real transport: a sender does not receive its own
    broadcast. The HOST broadcasts here -- peers receive but (being non-host)
    do not re-broadcast, so nothing loops back to confound the check."""
    bus, sessions = make_sessions(3)
    got = _capture_chat(sessions)
    sessions["peer0"]._transport.broadcast(
        {"type": "chat", "payload": {"nickname": "Host", "text": "yo"}})
    bus.drain()
    assert ("Host", "yo") not in got["peer0"]    # sender excluded
    assert ("Host", "yo") in got["peer1"]
    assert ("Host", "yo") in got["peer2"]


def test_direct_send_reaches_only_target():
    bus, sessions = make_sessions(3)
    got = _capture_chat(sessions)
    # peer2 sends a chat directly to peer1 only
    sessions["peer2"]._transport.send(
        "peer1", {"type": "chat", "payload": {"nickname": "P2", "text": "psst"}})
    bus.drain()
    assert ("P2", "psst") in got["peer1"]
    assert ("P2", "psst") not in got["peer0"]
    assert ("P2", "psst") not in got["peer2"]


def test_drain_returns_message_count_and_terminates():
    bus, sessions = make_sessions(2)
    _capture_chat(sessions)
    sessions["peer1"]._transport.broadcast(
        {"type": "chat", "payload": {"nickname": "P1", "text": "one"}})
    delivered = bus.drain()
    assert delivered >= 1                          # terminated, counted


def test_unregister_simulates_disconnect():
    bus, sessions = make_sessions(3)
    got = _capture_chat(sessions)
    bus.unregister("peer2")                        # peer2 drops
    sessions["peer1"]._transport.broadcast(
        {"type": "chat", "payload": {"nickname": "P1", "text": "gone?"}})
    bus.drain()
    assert ("P1", "gone?") in got["peer0"]
    assert ("P1", "gone?") not in got["peer2"]     # no longer receiving


def test_nested_drain_from_a_handler_is_refused():
    """A handler that calls drain() while one is running is refused loudly.

    The rule already existed -- BotDriver's docstring records that a nested
    drain reorders the shared queue and cost a ~20 % hand-desync rate -- but
    it lived only in prose, so every future callback author had to know it.
    Now the bus enforces its own invariant. This matters specifically for
    the on_game_start -> start_p2p_hand wiring, which runs INSIDE the drain
    that delivered game_start: it must enqueue and let the outer drain
    consume, never drain again itself.

    Asserted on the raised error, not on a game outcome: the underlying
    corruption is reordering, which is probabilistic and would make a
    flaky control.
    """
    bus, sessions = make_sessions(2)
    caught = []

    class Reentrant:
        def handle_message(self, from_conn, msg):
            try:
                bus.drain()                    # forbidden: already draining
            except RuntimeError as exc:
                caught.append(exc)

    bus.register("peer1", Reentrant())
    bus.enqueue("peer0", "peer1", {"type": "chat"})
    bus.drain()

    assert len(caught) == 1, "nested drain() was allowed"
    assert "re-entered" in str(caught[0])


def test_bus_is_usable_after_a_refused_nested_drain():
    """The guard releases on the way out: one bad handler must not wedge
    the bus for every later drain. A bare flag without try/finally would
    leave _draining set forever once a handler raised through it."""
    bus, sessions = make_sessions(2)
    got = _capture_chat(sessions)

    class Reentrant:
        def handle_message(self, from_conn, msg):
            try:
                bus.drain()
            except RuntimeError:
                pass

    bus.register("peer1", Reentrant())
    bus.enqueue("peer0", "peer1", {"type": "chat"})
    bus.drain()

    # A normal exchange still works afterwards.
    bus.register("peer1", sessions["peer1"])
    sessions["peer0"]._transport.broadcast(
        {"type": "chat", "payload": {"nickname": "Host", "text": "still here"}})
    bus.drain()
    assert ("Host", "still here") in got["peer1"]


if __name__ == "__main__":
    passed = total = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            total += 1
            try:
                fn()
                passed += 1
                print(f"  {name}: ok")
            except Exception as exc:
                print(f"  {name}: FAIL - {type(exc).__name__}: {exc}")
    print(f"{passed}/{total} passed")
