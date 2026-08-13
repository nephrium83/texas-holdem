"""What socket graph does the production transport actually build?

docs/AUDIT-M8-IDENTITY.md finding D: only the host calls ``start_host()``
(onboarding.py:600); joiners only ``connect()`` to the host's address
(onboarding.py:916-979). The host re-broadcasts chat and nothing else --
``_on_chat`` is the single ``is_host`` re-broadcast in session.py.

Meanwhile the hostless mental-poker deal is peer-symmetric. Every seat's
FIRST action is to broadcast ``key_announce`` (mental_deal.py:290-308),
and every seat needs every other seat's announcement to form the joint
key. At three seats that requires B's message to reach C.

At two peers a star and a mesh are the same graph, which is why nothing
has caught this. These tests use three.

Production transport only. ``holdem.p2p.transport`` keeps ``_loop``,
``_writers`` and ``_server`` at module scope, so one peer per process --
hence tests/prod_peer.py and three subprocesses rather than a fixture.

Deliberately NOT done here: connecting B to C by hand. The question is
what topology the application creates, and a test that builds a better
one measures nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PEER = str(Path(__file__).parent / "prod_peer.py")
BOOT_TIMEOUT = 20.0
DELIVER_WAIT = 5.0
# A three-seat mental deal over three real processes, including the
# Bayer-Groth shuffle proofs, is seconds of genuine crypto -- not a
# delivery wait. Generous on purpose; the assertions are on state, not time.
DEAL_WAIT = 120.0

HAND_ARGS = {
    "hand_no":   1,
    "names":     ["A", "B", "C"],
    "stacks":    [1000, 1000, 1000],
    "sb":        10,
    "bb":        20,
    "structure": "No-Limit",
    "button":    0,
}


class Peer:
    """One prod_peer subprocess with a stdout collector."""

    def __init__(self, role: str, label: str, invite: str = "") -> None:
        self.label = label
        argv = [sys.executable, PEER, "--role", role, "--label", label]
        if invite:
            argv += ["--invite", invite]
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        self.events: list = []
        self._lock = threading.Lock()
        self.stderr: list = []
        threading.Thread(target=self._pump, daemon=True,
                         name=f"peer-{label}").start()
        threading.Thread(target=self._pump_err, daemon=True,
                         name=f"peer-err-{label}").start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self._lock:
                self.events.append(evt)

    def _pump_err(self) -> None:
        for line in self.proc.stderr:
            with self._lock:
                self.stderr.append(line.rstrip())

    def send(self, cmd: dict) -> None:
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()

    def wait_for(self, pred, timeout=BOOT_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                snap = list(self.events)
            for e in snap:
                if pred(e):
                    return e
            time.sleep(0.05)
        return None

    def all_of(self, mtype: str) -> list:
        with self._lock:
            return [e for e in self.events if e.get("type") == mtype]

    def close(self) -> None:
        try:
            self.send({"op": "quit"})
            self.proc.wait(timeout=5)
        except Exception:                              # noqa: BLE001
            try:
                self.proc.terminate()
            except Exception:                          # noqa: BLE001
                pass


@pytest.fixture
def three_peers():
    """Host A, joiners B and C -- wired exactly as onboarding.py wires them."""
    a = Peer("host", "A")
    b = c = None
    try:
        ready = a.wait_for(lambda e: e.get("type") == "ready")
        assert ready is not None, f"host never became ready; stderr={a.stderr}"
        addr = ready["addr"]
        assert addr, "host reported no listen address"
        invite = ready.get("invite")
        assert invite, "host did not publish a V2 invite"
        # Joiners are started WITH the invite, as a human would paste it
        # before joining. The pin has to exist before the socket does: a
        # Session built after connect() would spend the gap accepting
        # whatever the far end said, which is the window this closes.
        b = Peer("joiner", "B", invite=invite)
        c = Peer("joiner", "C", invite=invite)
        # Joiners dial the HOST and begin the handshake. They no longer send
        # player_info on connect: identity is revealed only after the host
        # proves it holds the pinned key.
        for p in (b, c):
            p.wait_for(lambda e: e.get("type") == "ready")
            p.send({"op": "connect", "addr": addr, "invite": invite})
            assert p.wait_for(
                lambda e: e.get("type") == "ack" and e.get("op") == "connect"
            ) is not None, f"{p.label} could not reach the host; stderr={p.stderr}"
        # Let the host finish accepting both, and wait for admission to
        # complete rather than sleeping and hoping.
        a.wait_for(lambda e: e.get("type") == "connected")
        for p in (b, c):
            got = p.wait_for(lambda e: e.get("type") == "admission",
                             timeout=BOOT_TIMEOUT)
            assert got is not None and got.get("admitted"), (
                f"{p.label} was not admitted; stderr={p.stderr[-8:]}")
        yield a, b, c
    finally:
        for p in (c, b, a):
            if p is not None:
                p.close()


def _graph(peer: Peer) -> list:
    before = len(peer.all_of("graph"))
    peer.send({"op": "graph"})
    got = peer.wait_for(
        lambda e: e.get("type") == "graph"
        and len(peer.all_of("graph")) > before, timeout=10.0)
    assert got is not None, f"{peer.label} never reported its graph"
    return got["peers"]


def test_production_topology_is_a_star(three_peers):
    """Record the graph the application actually builds.

    Not an assertion that a star is wrong -- that is the next test. This
    one pins what exists, so a future change to full mesh fails here
    loudly rather than silently altering the trust model.
    """
    a, b, c = three_peers
    ga, gb, gc = _graph(a), _graph(b), _graph(c)
    assert len(ga) == 2, f"host should hold two connections, got {ga}"
    assert len(gb) == 1, f"joiner B should hold one connection, got {gb}"
    assert len(gc) == 1, f"joiner C should hold one connection, got {gc}"
    # B and C know only the host, and the host's ids are per-socket, so
    # the two joiners share no connection.
    assert set(gb).isdisjoint(set(gc)) or gb == gc, (
        f"unexpected id overlap: B={gb} C={gc}")


def _status(peer, timeout=DELIVER_WAIT):
    """Ask for a fresh status and return THAT one.

    wait_for scans the event history from the beginning and returns the
    first match, so a predicate of "type == status" hands back the oldest
    status the peer ever sent. Polling with it reports the same stale
    snapshot forever -- which looked exactly like a Session stuck in LOBBY
    and sent this investigation after a product bug that did not exist.
    Wait for the count to grow, then read the newest entry.
    """
    before = len(peer.all_of("status"))
    peer.send({"op": "status"})
    got = peer.wait_for(lambda e: e.get("type") == "status"
                        and len(peer.all_of("status")) > before,
                        timeout=timeout)
    assert got is not None, f"{peer.label} never reported status"
    return peer.all_of("status")[-1]


def _await(peer, pred, what, timeout=20.0):
    """Poll status until pred holds, so a stall names the state it stalled in."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _status(peer)
        if pred(last):
            return last
        time.sleep(0.25)
    raise AssertionError(
        f"{peer.label} never reached {what}; last status={last} "
        f"stderr={peer.stderr[-8:]}")


@pytest.fixture
def three_sessions(three_peers):
    """The full production onboarding sequence, then a started game.

    Everything the in-memory tests replace with a bus happens here for
    real: signed player_info, the host binding ed25519_pubkey_hex from the
    VERIFIED envelope, player_ack telling each joiner the conn_id the host
    filed it under, and game_start distributing seat order.
    """
    a, b, c = three_peers
    _await(a, lambda s: len(s["players"]) == 3, "a three-player roster")

    a.send({"op": "start_game", "settings": {}})
    assert a.wait_for(lambda e: e.get("type") == "ack"
                      and e.get("op") == "start_game") is not None, \
        f"host could not start the game; stderr={a.stderr[-8:]}"

    for p in (a, b, c):
        _await(p, lambda s: s["state"] == "PLAYING" and len(s["seat_order"]) == 3,
               "PLAYING with a three-seat order")
    for p in (b, c):
        _await(p, lambda s: s["local_conn_id"] in s["seat_order"],
               "knowing its own seat (player_ack)")
    return a, b, c


def test_production_onboarding_binds_every_seat_to_a_key(three_sessions):
    """Seat authority must be real before the relay can mean anything.

    _maybe_relay forwards only what _seat_author_ok accepts, and that check
    is against keys bound from VERIFIED envelopes -- the ed25519_pubkey_hex
    the host took from each joiner's signed player_info, not from anything a
    joiner asserted about itself. If binding does not happen on the
    production path the relay forwards nothing, and every later assertion
    here would be measuring an empty room.

    Checked after start_hand because _bind_seat_keys runs inside
    start_p2p_hand, not at game start: binding is frozen once, immediately
    before the first hand needs it.
    """
    a, b, c = three_sessions
    for p in (a, b, c):
        p.send({"op": "start_hand", "args": dict(HAND_ARGS)})
    for p in (a, b, c):
        assert p.wait_for(lambda e: e.get("type") == "ack"
                          and e.get("op") == "start_hand",
                          timeout=DELIVER_WAIT) is not None, \
            f"{p.label} did not accept start_hand; stderr={p.stderr[-8:]}"

    for p in (a, b, c):
        st = _status(p)
        assert len(st["seat_keys"]) == 3, (
            f"{p.label} bound {len(st['seat_keys'])} seat keys, expected 3: "
            f"{st['seat_keys']}")
        assert st["local_seat"] is not None, (
            f"{p.label} cannot resolve its own seat: {st}")
    # The three peers must agree on WHICH key holds each seat, or the relay
    # would forward messages some recipients reject.
    keysets = [_status(p)["seat_keys"] for p in (a, b, c)]
    assert keysets[0] == keysets[1] == keysets[2], (
        f"peers disagree on seat bindings: {keysets}")


def test_a_joiners_deal_message_reaches_the_other_joiner_via_the_host(
        three_sessions):
    """B -> A -> C, on the first message a three-seat deal needs.

    This replaces an xfail that recorded the opposite. Under the star that
    onboarding builds, B's writers contain only the host, so without the
    relay C never sees B's key_announce and the deal cannot leave KEYGEN.

    The assertion is on the AUTHOR, not on arrival: C is connected only to
    the host, so any key_announce C receives claiming seat 1 must have been
    forwarded, and the host is a courier rather than the author of it.
    """
    a, b, c = three_sessions
    b_seat = _status(b)["local_seat"]

    for p in (a, b, c):
        p.send({"op": "start_hand", "args": dict(HAND_ARGS)})
    for p in (a, b, c):
        assert p.wait_for(lambda e: e.get("type") == "ack"
                          and e.get("op") == "start_hand",
                          timeout=DELIVER_WAIT) is not None, \
            f"{p.label} did not accept start_hand; stderr={p.stderr[-8:]}"

    relayed = c.wait_for(
        lambda e: (e.get("type") == "recv"
                   and e.get("mtype") == "key_announce"
                   and e.get("seat") == b_seat),
        timeout=DEAL_WAIT)
    assert relayed is not None, (
        f"joiner C never received seat {b_seat}'s key_announce. "
        f"C's peers={_graph(c)} (host only), so this message can only "
        f"arrive by relay. Received by C: "
        f"{[(e.get('mtype'), e.get('seat')) for e in c.all_of('recv')]}")


def test_three_real_sessions_deal_hole_cards_and_reach_betting(three_sessions):
    """The end-to-end claim, asserted at the strongest point it reaches.

    KEYGEN is the phase the star could not leave, because it is the one that
    needs every seat's announcement at every other seat. But stopping the
    assertion there would under-claim what actually happens: three real
    production processes complete the whole three-seat mental deal over a
    relayed star -- key ceremony, shuffle chain, selective deal -- and arrive
    at betting with hole cards recovered.

    Asserted on hole_complete and the replica phase rather than on the deal
    phase alone, because "past KEYGEN" is satisfied by a hand that limped
    one step and stalled.
    """
    a, b, c = three_sessions
    for p in (a, b, c):
        p.send({"op": "start_hand", "args": dict(HAND_ARGS)})

    for p in (a, b, c):
        _await(p, lambda s: s["hole_complete"] or s["hand_voided"],
               "hole cards recovered", timeout=DEAL_WAIT)

    for p in (a, b, c):
        st = _status(p)
        assert not st["hand_voided"], (
            f"{p.label} voided instead of advancing: {st['void_reason']}")
        assert st["deal_phase"] not in (None, "KEYGEN"), (
            f"{p.label} is still in {st['deal_phase']}")
        assert st["hole_complete"], (
            f"{p.label} did not recover its hole cards: {st}")
        assert st["replica_phase"] == "betting", (
            f"{p.label} reached {st['replica_phase']!r}, expected betting")


def test_the_host_does_not_echo_a_relayed_envelope_back_to_its_author(
        three_sessions):
    """B must never receive its own authored envelope back from the host.

    This needs its own assertion because the hand SURVIVES the failure. With
    a relay that echoes (broadcast instead of broadcast_except), B's own
    messages come back to it, the sequence bookkeeping recognises each as
    the same (author_seq, fingerprint) already applied, drops it, and the
    deal completes to betting exactly as before. Measured on this harness:

        baseline    echoed_back_to_B = []
        echo break  echoed_back_to_B = [deal_share, deck_round, key_announce]
        both        hole_complete on all three peers, replica phase betting

    So every other end-to-end assertion here is blind to it -- replay
    protection is doing its job and hiding the waste. What is left is
    doubled relay traffic and a peer re-processing its own emissions, which
    only a trace of who received what can see.
    """
    a, b, c = three_sessions
    b_seat = _status(b)["local_seat"]
    for p in (a, b, c):
        p.send({"op": "start_hand", "args": dict(HAND_ARGS)})
    _await(c, lambda s: s["hole_complete"] or s["hand_voided"],
           "hole cards recovered", timeout=DEAL_WAIT)

    echoed = sorted({e.get("mtype") for e in b.all_of("recv")
                     if e.get("seat") == b_seat})
    assert echoed == [], (
        f"the host echoed seat {b_seat}'s own envelopes back to it: {echoed}. "
        "The deal still completes because replay protection drops them, so "
        "nothing else in this file would notice.")


def test_every_seats_deal_traffic_reaches_the_far_joiner(three_sessions):
    """C is wired only to the host, so everything from B arrived by relay.

    Broader than the key_announce case: if the relay forwarded only the
    first message type and dropped the rest, the deal would stall later
    rather than never start, and a test that watched one type would still
    be green.
    """
    a, b, c = three_sessions
    b_seat = _status(b)["local_seat"]
    for p in (a, b, c):
        p.send({"op": "start_hand", "args": dict(HAND_ARGS)})
    _await(c, lambda s: s["hole_complete"] or s["hand_voided"],
           "hole cards recovered", timeout=DEAL_WAIT)

    from_b = {e.get("mtype") for e in c.all_of("recv")
              if e.get("seat") == b_seat}
    for mtype in ("key_announce", "deck_round", "deal_share"):
        assert mtype in from_b, (
            f"C never received a relayed {mtype} authored by seat {b_seat}; "
            f"got {sorted(from_b)}")
