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
