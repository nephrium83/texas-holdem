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


class Peer:
    """One prod_peer subprocess with a stdout collector."""

    def __init__(self, role: str, label: str) -> None:
        self.label = label
        self.proc = subprocess.Popen(
            [sys.executable, PEER, "--role", role, "--label", label],
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
    b = Peer("joiner", "B")
    c = Peer("joiner", "C")
    try:
        ready = a.wait_for(lambda e: e.get("type") == "ready")
        assert ready is not None, f"host never became ready; stderr={a.stderr}"
        addr = ready["addr"]
        assert addr, "host reported no listen address"
        # Joiners dial the HOST, which is the only thing onboarding does.
        for p in (b, c):
            p.wait_for(lambda e: e.get("type") == "ready")
            p.send({"op": "connect", "addr": addr})
            assert p.wait_for(
                lambda e: e.get("type") == "ack" and e.get("op") == "connect"
            ) is not None, f"{p.label} could not reach the host; stderr={p.stderr}"
        # Let the host finish accepting both.
        a.wait_for(lambda e: e.get("type") == "connected")
        time.sleep(0.5)
        yield a, b, c
    finally:
        for p in (c, b, a):
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


@pytest.mark.xfail(strict=True, reason=(
    "CONFIRMED DEFECT, awaiting the mesh-vs-relay architecture decision. "
    "onboarding.py builds a star; the hostless deal assumes a mesh; "
    "nothing relays deal messages. Measured: host=2 connections, each "
    "joiner=1, B<->C absent, B's key_announce reaches the host and not C. "
    "strict=True so this fails loudly the moment the topology is fixed "
    "and the xfail becomes a lie."))
def test_joiner_broadcast_does_not_reach_the_other_joiner(three_peers):
    """The finding, demonstrated on the first message a deal needs.

    Every seat's first act is to broadcast ``key_announce``, and every
    seat needs every other seat's copy to derive the joint key. Seat B
    broadcasts one. Under the topology onboarding builds, B's writers
    contain only the host, so C never sees it, and nothing in session.py
    relays a deal message -- ``_on_chat`` is the only ``is_host``
    re-broadcast.

    Required recipients : {A, C}
    Actual recipients   : {A}
    First missing message: key_announce, seat 1 -> seat 2

    A three-seat hand cannot leave KEYGEN. This test FAILS on the
    current production topology, which is the point.
    """
    a, b, c = three_peers
    announce = {"type": "key_announce", "seat": 1,
                "X_hex": "00" * 32, "pop": {"c": "00" * 32, "s": "00" * 32},
                "hand": 1}
    b.send({"op": "broadcast", "msg": announce})
    assert b.wait_for(lambda e: e.get("type") == "ack"
                      and e.get("op") == "broadcast") is not None

    got_a = a.wait_for(lambda e: e.get("type") == "recv"
                       and e.get("mtype") == "key_announce",
                       timeout=DELIVER_WAIT)
    got_c = c.wait_for(lambda e: e.get("type") == "recv"
                       and e.get("mtype") == "key_announce",
                       timeout=DELIVER_WAIT)

    assert got_a is not None, (
        "host did not receive the joiner's key_announce -- the star edge "
        "itself is broken, which is a different bug")
    assert got_c is not None, (
        "CONFIRMED: joiner C never received joiner B's key_announce. "
        f"B's peers={_graph(b)} C's peers={_graph(c)} host's peers={_graph(a)}. "
        "Required recipients {host, C}, actual {host}. A three-seat "
        "MentalDeal cannot leave KEYGEN on this topology.")
