"""Whole hands over the real signed transport: settle, carry, next hand,
side pots, and a seated peer lost mid-hand.

Every other multi-hand test runs on the in-memory bus, which replaces the
signed envelopes, the admission handshake, seat-key binding and the host
relay with a dict passed between objects. The three-peer topology tests
cross that gap but stop at betting. These play on: each peer is a
tests/prod_peer.py process with its own identity, admitted through the
shipped handshake, dealing Bayer-Groth hands over holdem.p2p.transport.

The harness only chooses each action and asks every peer what it sees.
Nothing is compared through a shortcut: hand results, stacks and digests
are each peer's own report of its own replica.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import crypto_gate                                     # noqa: E402
from test_three_peer_topology import Peer, _status     # noqa: E402

SB, BB = 10, 20


@pytest.fixture(autouse=True)
def _needs_crypto():
    crypto_gate.require_crypto()


#: When the running test's own pytest timeout fires, less a margin.
_deadline = [float("inf")]


@pytest.fixture(autouse=True)
def _stall_deadline(request):
    """Cut every wait short of the test's own timeout (see _left).

    A stall must fail in _until or _ask, naming what it waited for. A bare
    pytest timeout says nothing, and on Windows it ends the whole run.
    """
    marker = request.node.get_closest_marker("timeout")
    if marker is not None:
        _deadline[0] = time.monotonic() + marker.args[0] - 2.0
    yield
    _deadline[0] = float("inf")


def _left(timeout):
    """timeout, or less if the test's deadline comes first."""
    return max(0.0, min(timeout, _deadline[0] - time.monotonic()))


def _until(peer, pred, what, timeout=10.0):
    """Poll status until pred holds; a stall names the state it stalled in.

    Each wait is one step -- a deal, an action, a settlement -- which takes
    about a second here. It ends before the test's own timeout would, so
    the failure says what it was waiting for. A status request answers in
    milliseconds; its half-second floor stays inside the deadline's margin.
    """
    deadline = time.monotonic() + _left(timeout)
    last = None
    while time.monotonic() < deadline:
        last = _status(peer, timeout=max(0.5, _left(10.0)))
        if pred(last):
            return last
        time.sleep(0.02)
    raise AssertionError(f"{peer.label} never reached {what}; last status="
                         f"{last} stderr={peer.stderr[-8:]}")


def _ask(peer, op, **fields):
    """Send one op and return ITS ack (not the first one ever seen)."""
    def acks():
        return [e for e in peer.all_of("ack") if e.get("op") == op]

    def errors():
        return [e for e in peer.all_of("error") if e.get("op") == op]

    n_ack, n_err = len(acks()), len(errors())
    peer.send({"op": op, **fields})
    got = peer.wait_for(lambda e: len(acks()) > n_ack
                        or len(errors()) > n_err, timeout=_left(30.0))
    assert got is not None, f"{peer.label} never answered {op}"
    assert len(errors()) == n_err, f"{peer.label} {op}: {errors()[-1]}"
    return acks()[-1]


@pytest.fixture
def table(tmp_path):
    """make(stacks) -> peers indexed by seat, the first hand being dealt.

    The host is seat 0 and the joiners follow in the order they were
    admitted. Each peer gets its own config directory, so its own key.
    """
    made = []

    def make(stacks):
        n = len(stacks)
        host = Peer("host", "A", tmp_path / "A")
        made.append(host)
        ready = host.wait_for(lambda e: e.get("type") == "ready",
                              timeout=_left(20.0))
        assert ready and ready.get("addr") and ready.get("invite"), \
            f"host never became ready; stderr={host.stderr[-8:]}"
        for label in "BC"[:n - 1]:
            p = Peer("joiner", label, tmp_path / label,
                     invite=ready["invite"])
            made.append(p)
            assert p.wait_for(lambda e: e.get("type") == "ready",
                              timeout=_left(20.0)), f"{label} never ready"
            _ask(p, "connect", addr=ready["addr"])
            got = p.wait_for(lambda e: e.get("type") == "admission",
                             timeout=_left(30.0))
            assert got and got.get("admitted"), \
                f"{label} was not admitted: {got}; stderr={p.stderr[-8:]}"
        _until(host, lambda s: len(s["players"]) == n, "a full roster")
        args = {"hand_no": 1, "names": [f"S{i}" for i in range(n)],
                "stacks": list(stacks), "sb": SB, "bb": BB,
                "structure": "No-Limit", "button": 0}
        for p in made:
            _ask(p, "arm_hand", args=args)        # dealt on game_start
        _ask(host, "start_game",
             settings={"deal_policy": "bayer-groth-v1"})
        by_seat = {}
        for p in made:
            st = _until(p, lambda s: s["state"] == "PLAYING"
                        and len(s["seat_order"]) == n
                        and s["local_conn_id"] in s["seat_order"]
                        and len(s["seat_keys"]) == n,
                        "PLAYING with its own seat and every seat's key")
            by_seat[st["seat_order"].index(st["local_conn_id"])] = p
            # One key per seat. Peers sharing a config directory share a
            # key, and every play test here still passed that way.
            keys = st["seat_keys"]
            assert len(set(keys.values())) == n, keys
        return [by_seat[i] for i in range(n)]

    yield make
    for p in reversed(made):
        p.close()


def _take_action(seated, k, choose):
    """Play action k of the current hand; False once the hand has ended.

    choose(legal, k) picks it from the actor's own legal menu. The actor is
    asked only once the reference peer has reached sequence k and the actor
    itself agrees it is to act there, so nothing is sent out of turn.
    """
    st = _until(seated[0], lambda s: s["settled"] or s["hand_voided"]
                or s["terminal"] is not None
                or (s["replica_phase"] == "betting" and s["seq"] == k),
                f"action {k} or the end of the hand")
    if st["settled"] or st["hand_voided"] or st["terminal"] is not None:
        return False
    actor = seated[st["actor"]]
    mine = _until(actor, lambda s: s["seq"] == k and s["legal"] is not None,
                  f"its turn at action {k}")
    action, amount = choose(mine["legal"], k)
    ack = _ask(actor, "act", action=action, amount=amount)
    assert ack["verdict"] == "applied", (k, action, amount, ack)
    return True


def _play_hand(seated, choose):
    """Drive the current hand until it ends; returns every peer's status."""
    k = 0
    while _take_action(seated, k, choose):
        k += 1
    return [_until(p, lambda s: s["settled"] or s["hand_voided"]
                   or s["terminal"] is not None, "the end of the hand")
            for p in seated]


def _assert_agreed(statuses, total):
    """Every peer settled the same hand the same way, conserving chips."""
    for st in statuses:
        assert st["settled"] and not st["hand_voided"], st
        assert st["terminal"] is None, st["terminal_reason"]
    ref = statuses[0]
    for st in statuses[1:]:
        assert st["hand_no"] == ref["hand_no"]
        assert st["digest"] == ref["digest"]
        assert st["result"] == ref["result"]
        assert st["stacks"] == ref["stacks"]
    assert sum(ref["stacks"]) == total
    return ref


def _agreed_by_every_seat(seated, hand):
    """Each peer admitted hand hand's hand_settled from every other seat,
    over the signed wire, and found it matched its own settlement.

    Read from each peer's own status, which counts a report only once
    ingress has checked who signed it and the handler has compared it.
    Counting arrivals instead still passed with the comparison removed.
    Every seat in these tests is dealt every hand, and the next hand waits
    for all of their reports, so Next is pressed only after this.
    """
    want = list(range(len(seated)))
    for p in seated:
        _until(p, lambda s: s["hand_no"] == hand and s["agreed"] == want,
               f"every seat's matching report on hand {hand}")


def _next_hand(seated):
    """Every peer calls next_p2p_hand, as each human presses Next Hand."""
    return {_ask(p, "next")["verdict"] for p in seated}


# Choosers: (legal, k) -> (action, raise-to amount). "call" with nothing
# to call is a check.
def _checkdown(legal, k):
    return ("call", 0)


def _fold_at_once(legal, k):
    return ("fold", 0)


def _raise_called(legal, k):
    return ("raise", legal["min_to"]) if k == 0 else ("call", 0)


def _all_in_folded_to(legal, k):
    return ("raise", legal["max_to"]) if k == 0 else ("fold", 0)


def _reraise_then_fold(legal, k):
    return ("raise", legal["min_to"]) if k < 2 else ("fold", 0)


def _flop_bet_folded_to(legal, k):
    # limp, check, then the first flop bet takes it down
    return {2: ("raise", legal["min_to"]), 3: ("fold", 0)}.get(k, ("call", 0))


def _all_in(legal, k):
    return ("raise", legal["max_to"]) if legal["can_raise"] else ("call", 0)


TEN_HANDS = [_checkdown, _fold_at_once, _raise_called, _all_in_folded_to,
             _reraise_then_fold, _flop_bet_folded_to, _checkdown,
             _all_in_folded_to, _raise_called, _checkdown]


# Timeouts are about 3x each test's runtime over three runs on a 16-thread
# dev machine (17.7-18.0 s, 2.8 s, 3.6 s and 5.7 s, table set-up and
# teardown included), inside CI's 60 s per test. The runs are bound by
# message round trips, not CPU: pinned to two CPUs they took the same time.
# Every wait ends two seconds short of them (_stall_deadline), so a slow
# or stalled run fails naming the step it was on.
@pytest.mark.timeout(55)
def test_two_seats_play_ten_hands_and_agree_on_every_one(table):
    """Fold, call, raise and all-in over ten consecutive hands.

    After every hand both peers report the same hand result, stacks and
    digest, the stacks still hold every chip, the next hand is dealt from
    exactly those stacks, and the heads-up button changes hands each time.
    """
    seated = table([1000, 1000])
    buttons, settled = [], None
    for hand, choose in enumerate(TEN_HANDS, start=1):
        for p in seated:
            st = _until(p, lambda s: s["hand_no"] == hand
                        and (s["hole_complete"] or s["hand_voided"]),
                        f"hand {hand} dealt")
            assert not st["hand_voided"], st["void_reason"]
            # Chips carried: the hand was dealt from the last settlement.
            if settled is not None:
                assert st["last_settled_stacks"] == settled
        start = st["last_settled_stacks"]
        ref = _assert_agreed(_play_hand(seated, choose), total=2000)
        _agreed_by_every_seat(seated, hand)
        if choose is _all_in_folded_to:
            # Heads-up the button acts first. It shoved its whole stack, so
            # everything but the big blind it was folded to comes back.
            btn = ref["button"]
            assert ref["result"]["refund"] == [btn, start[btn] - BB], ref
        settled = ref["stacks"]
        buttons.append(ref["button"])
        if hand < len(TEN_HANDS):
            assert _next_hand(seated) == {"started"}
    assert all(a != b for a, b in zip(buttons, buttons[1:])), buttons
    assert settled != [1000, 1000]                    # chips actually moved


@pytest.mark.timeout(10)
def test_three_unequal_stacks_all_in_build_side_pots(table):
    """[100, 300, 500] all in. The engine moves the button to seat 1 on a
    first hand, so seat 1 opens to 300, seat 2 re-shoves to 500 and seat 0
    calls all-in for 100. Seat 2's top 200 is uncalled and refunded:

      main  100 x 3          = 300, every seat eligible
      side  (300 - 100) x 2  = 400, seats 1 and 2

    The cards are real, so who wins is not known; the pots are, and every
    peer must pay them out identically.
    """
    seated = table([100, 300, 500])
    ref = _assert_agreed(_play_hand(seated, _all_in), total=900)
    _agreed_by_every_seat(seated, 1)
    pots = ref["result"]["pots"]
    assert len(pots) >= 2
    assert [(p["amount"], p["eligible"]) for p in pots] == \
        [(300, [0, 1, 2]), (400, [1, 2])]
    assert ref["result"]["refund"] == [2, 200]


@pytest.mark.parametrize("n", [
    pytest.param(2, marks=pytest.mark.timeout(12), id="2-seats"),
    pytest.param(3, marks=pytest.mark.timeout(18), id="3-seats"),
])
def test_a_joiner_killed_mid_hand_ends_the_table_for_every_survivor(
        table, n):
    """Hand 1 settles, hand 2 has chips in the pot, then the last joiner's
    process dies. Only the host's socket sees it go; at three seats the
    other joiner learns from the host's signed peer_lost notice. The
    leaver folds: every survivor ends PEER_LOST having settled its own
    copy of hand 2, with the leaver's chips in the pot forfeit
    (docs/CASUAL_P2P_RULES.md).

    The leaver had called the big blind of 20. Heads-up the other seat
    had 20 in too and takes the 40. At three seats seat 0 had the small
    blind of 10 and seat 1 the big: 10 from each seat makes 30, shared by
    seats 0 and 1, and the other 10 from seats 1 and 2 is seat 1's alone.
    """
    seated = table([1000] * n)
    hand1 = _assert_agreed(_play_hand(seated, _raise_called),
                           total=1000 * n)["stacks"]
    _agreed_by_every_seat(seated, 1)
    assert _next_hand(seated) == {"started"}
    for p in seated:
        _until(p, lambda s: s["hand_no"] == 2 and s["hole_complete"],
               "hand 2 dealt")
    assert _take_action(seated, 0, _checkdown)
    in_pot = [_until(p, lambda s: s["seq"] == 1, "action 0 applied")
              for p in seated]
    put_in = [a - b for a, b in zip(hand1, in_pot[0]["stacks"])]
    won = {2: [20, -20], 3: [5, 15, -20]}[n]
    assert put_in == {2: [20, 20], 3: [10, 20, 20]}[n], in_pot[0]
    forfeit = [a + b for a, b in zip(hand1, won)]

    victim, survivors = seated[-1], seated[:-1]
    victim.proc.kill()
    victim.proc.wait(timeout=10)

    for p in survivors:
        # The host's socket closes at once on loopback, and its notice to
        # the other joiner is one message; without either, a survivor
        # waits forever.
        st = _until(p, lambda s: s["terminal"] is not None,
                    "a terminal state", timeout=5.0)
        assert st["terminal"] == "PEER_LOST", st["terminal_reason"]
        assert st["terminal_reason"].startswith(
            f"seat {n - 1} ({victim.label}) disconnected"), \
            st["terminal_reason"]
        assert st["terminal_reason"].endswith("; it forfeits hand 2"), \
            st["terminal_reason"]
        assert st["last_settled_stacks"] == forfeit
