"""Integration test for the sidecar launcher's session/bot wiring: the
same real hostless-hand machinery test_client_server.py exercises (real
Sessions on an InMemoryBus, real mental-poker dealing), but through
sidecar_launcher's own session/bot construction rather than a bespoke
harness. The no-leak invariant itself is already exhaustively covered
by test_client_view.py and the crypto layer's own tests; this file's
job is the launcher's new glue -- session/bot construction, the
drain-wrapping, and BotDriver's react logic -- not re-deriving crypto
guarantees already proven elsewhere.
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem import client_view
from holdem.p2p.inmemory_transport import InMemoryBus
from holdem.sidecar_launcher import (
    BotDriver, _deal_first_hand, _make_sessions, _make_start_table,
    _wire_hand_start, _wrap_with_drain,
)

import importlib
try:
    importlib.import_module("holdem.p2p.elgamal")
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)

HUMAN_SEAT = 0


def _build_table(seats=4, seed=1234):
    bus, sessions, order = _make_sessions(seats)
    human_conn_id = order[HUMAN_SEAT]
    human_session = sessions[human_conn_id]
    bot_sessions = {cid: s for cid, s in sessions.items() if cid != human_conn_id}
    _wrap_with_drain(human_session, bus)
    BotDriver(bot_sessions, random.Random(seed))
    return bus, sessions, order, human_session


def test_make_sessions_reserves_seat_zero_for_the_human():
    bus, sessions, order = _make_sessions(4)
    assert len(sessions) == 4
    assert sessions[order[0]].local_seat == 0


def test_deal_deals_and_bots_act_up_to_the_humans_turn():
    """4-max, button=0 (the human): preflop order is UTG (seat 3) then
    the button, so exactly one bot acts automatically before it becomes
    the human's turn -- proving BotDriver reacted to the deal completing
    without any external polling."""
    bus, sessions, order, human_session = _build_table()
    _deal_first_hand(sessions, order, bus, sb=5, bb=10, stack=500,
                     structure="No-Limit")
    snapshot = client_view.snapshot(human_session)
    assert snapshot["phase"] == "betting"
    assert "legal" in snapshot["you"]


def test_folding_lets_the_rest_of_the_hand_resolve_automatically():
    """Every remaining actor is a bot once the human folds, so the
    wrapped send_bet_action's drain() should carry the hand all the way
    to settled or voided with no further external driving."""
    bus, sessions, order, human_session = _build_table()
    _deal_first_hand(sessions, order, bus, sb=5, bb=10, stack=500,
                     structure="No-Limit")
    human_session.send_bet_action("fold")
    snapshot = client_view.snapshot(human_session)
    assert snapshot["turn"]["state"] in ("hand_complete", "voided")


def test_next_hand_advances_after_bots_have_already_auto_advanced():
    """Bots auto-advance next_p2p_hand() as soon as their own snapshot
    reports hand_complete/voided, mirroring a human who clicks Next Hand
    immediately. The human calling next_p2p_hand() afterward must still
    cleanly start hand 2, per next_p2p_hand's own guarantee that
    hand-scoped message buffering absorbs call-order skew."""
    bus, sessions, order, human_session = _build_table()
    _deal_first_hand(sessions, order, bus, sb=5, bb=10, stack=500,
                     structure="No-Limit")
    human_session.send_bet_action("fold")
    verdict = human_session.next_p2p_hand()
    assert verdict == "started"
    snapshot = client_view.snapshot(human_session)
    assert snapshot["hand_num"] == 2


def test_bot_driver_is_a_no_op_for_a_session_with_nothing_to_do():
    """A bot session that isn't mid-turn and hasn't settled must not
    raise or act when its hook fires -- BotDriver's react() is called on
    every state change, not only the ones it cares about."""
    bus, sessions, order = _make_sessions(3)
    bot_sessions = {cid: s for cid, s in sessions.items() if cid != order[0]}
    driver = BotDriver(bot_sessions, random.Random(1))
    for session in bot_sessions.values():
        driver._react(session)  # no hand started yet: must not raise


# ------------------------------------------------------- start_table verdicts

_SETTINGS = {"sb": 5, "bb": 10, "stack": 500, "structure": "No-Limit",
             "deal_policy": "bayer-groth-v1"}


def test_start_table_starts_the_table_and_drains():
    bus, sessions, order = _make_sessions(2)
    _wire_hand_start(sessions, order)
    start_table = _make_start_table(sessions, order, bus, _SETTINGS)

    assert start_table() == "started"
    assert sessions[order[0]].state == "PLAYING"
    assert sessions[order[0]].replica is not None
    assert bus._queue == [], "start_table left messages undelivered"


def test_start_table_reports_refused_when_nothing_started():
    """A refusal that happens BEFORE the table commits leaves the session in
    the lobby, and the client can act on that."""
    bus, sessions, order = _make_sessions(2)
    _wire_hand_start(sessions, order)
    start_table = _make_start_table(
        sessions, order, bus, dict(_SETTINGS, deal_policy="nonsense-v9"))

    assert start_table() == "refused"
    assert sessions[order[0]].state == "LOBBY"
    assert sessions[order[0]].deal_policy is None


def test_a_first_hand_failure_is_not_reported_as_a_refused_table():
    """The two failures are very different states to recover from.

    start_game validates the policy, THEN broadcasts game_start and commits
    PLAYING, and only then runs on_game_start -- which deals the whole first
    hand. One except clause around all of it reported "refused" for a table
    that was already irreversibly live, telling the client the table never
    began when it had. Session state is the discriminator.
    """
    bus, sessions, order = _make_sessions(2)
    _wire_hand_start(sessions, order)

    def explode(_payload):
        raise RuntimeError("the deal fell over")

    sessions[order[0]].on_game_start = explode
    start_table = _make_start_table(sessions, order, bus, _SETTINGS)

    assert start_table() == "hand_failed"
    assert sessions[order[0]].state == "PLAYING",         "the table did commit; only the hand failed"


def test_every_peer_receives_game_start_even_when_the_hand_fails():
    """Asserted on PEER STATE, not on an empty queue.

    An empty queue is also what a half-delivered broadcast looks like: the
    message is popped, the first recipient raises, and the rest never see
    it -- strictly worse than being left queued, because it can never be
    redelivered. The original version of this test asserted
    `bus._queue == []` and passed in exactly that case.

    Three seats, and the failure is installed on EVERY seat, which is the
    realistic shape: _wire_hand_start puts the same callback on all of
    them, so a broken deal breaks all of them.
    """
    bus, sessions, order = _make_sessions(3)
    _wire_hand_start(sessions, order)
    for cid in order:
        sessions[cid].on_game_start = lambda _p: (_ for _ in ()).throw(
            RuntimeError("the deal fell over"))
    start_table = _make_start_table(sessions, order, bus, _SETTINGS)

    start_table()

    for cid in order:
        assert sessions[cid].state == "PLAYING", (
            f"{cid} never received game_start: a raising handler stopped "
            f"the broadcast reaching the peers behind it")


def test_a_failure_shared_by_every_seat_still_returns_a_verdict():
    """The realistic failure, and the one that used to escape entirely.

    bus.drain() sat in a finally. A finally that raises DISCARDS the
    pending return, so when every seat's handler failed -- which is what
    happens, since they all share one callback -- the verdict never
    returned and the RuntimeError escaped start_table, unwound
    client_view.apply_command, and dropped the client's socket.
    """
    bus, sessions, order = _make_sessions(3)
    _wire_hand_start(sessions, order)
    for cid in order:
        sessions[cid].on_game_start = lambda _p: (_ for _ in ()).throw(
            RuntimeError("the deal fell over"))
    start_table = _make_start_table(sessions, order, bus, _SETTINGS)

    assert start_table() == "hand_failed"


def test_delivery_continues_past_a_failing_message():
    """The drain must reach quiescence, not stop at the first failure.

    bus.drain() re-raises the first handler error, which unwinds its own
    delivery loop and abandons everything still queued. A single
    try/except around one drain() call therefore leaves the remainder
    undelivered -- the same defect as leaving game_start queued, which is
    what containing the drain was meant to prevent. Measured at 4 seats
    with one broken seat: 3 messages abandoned, delivered late by whatever
    command happened to drain next.
    """
    bus, sessions, order = _make_sessions(4)
    _wire_hand_start(sessions, order)
    sessions[order[1]].on_game_start = lambda _p: (_ for _ in ()).throw(
        RuntimeError("seat 1 is broken"))
    start_table = _make_start_table(sessions, order, bus, _SETTINGS)

    verdict = start_table()

    assert bus.pending == 0, (
        f"{bus.pending} message(s) abandoned after a handler failed; "
        f"they would be delivered late by an unrelated later command")
    assert verdict == "hand_failed"


def test_a_seat_that_cannot_deal_downgrades_the_verdict():
    """Pins the started -> hand_failed downgrade specifically.

    The table IS live and its first hand cannot complete -- a seat is
    missing from the deal, and check_deadlines has no production caller,
    so nothing will ever void it. Reporting "started" would tell the
    client a hand is underway that never will be.
    """
    bus, sessions, order = _make_sessions(3)
    _wire_hand_start(sessions, order)
    sessions[order[2]].on_game_start = lambda _p: (_ for _ in ()).throw(
        RuntimeError("seat 2 is broken"))
    start_table = _make_start_table(sessions, order, bus, _SETTINGS)

    assert start_table() == "hand_failed", \
        "a seat that could not deal was reported as a clean start"


def test_a_healthy_table_is_not_branded_hand_failed_by_a_transient_error():
    """The verdict measures the hand, not the absence of exceptions.

    A first version set hand_failed whenever ANY drain attempt had raised,
    so one hiccup in a bot's state hook branded a table that dealt
    perfectly -- every seat with a driver and three verified proofs -- as
    failed. section 4 defines hand_failed as "its first hand did not
    complete", and it had.
    """
    bus, sessions, order = _make_sessions(3)
    _wire_hand_start(sessions, order)

    fired = {"n": 0}
    victim = sessions[order[1]]
    previous = victim.on_state_changed

    def flaky():
        # Chain first, then raise ONCE the seat is demonstrably healthy.
        # Raising earlier is not "incidental": on_state_changed fires from
        # inside begin_hand, so an early throw aborts that seat's own deal
        # and the verdict is then correct to report hand_failed. My first
        # version did exactly that and the test failed for the right reason.
        if previous is not None:
            previous()
        if not fired["n"] and victim.replica is not None                 and victim.proofs_verified > 0:
            fired["n"] = 1
            raise RuntimeError("one transient hiccup")

    victim.on_state_changed = flaky
    start_table = _make_start_table(sessions, order, bus, _SETTINGS)

    verdict = start_table()

    assert fired["n"] == 1, "the transient error never fired"
    assert all(s.replica is not None for s in sessions.values())
    assert all(s.proofs_verified > 0 for s in sessions.values()),         "premise broken: the table did not actually deal"
    assert bus.pending == 0
    assert verdict == "started", (
        "a table where every seat dealt was reported as hand_failed "
        "because one incidental hook raised")


def test_a_runaway_message_loop_is_not_retried():
    """The step limit is a conclusion, not a per-message failure.

    Retrying it repeats the whole exchange: 64 attempts at the default
    100k-step limit is 6.4 million deliveries while the client blocks on a
    socket. DrainLoopError exists to separate "this handler failed" from
    "this exchange will never end".
    """
    from holdem.p2p.inmemory_transport import DrainLoopError
    from holdem.sidecar_launcher import _drain_to_quiescence

    calls = {"n": 0}
    bus = InMemoryBus()

    def explode(**_kw):
        calls["n"] += 1
        raise DrainLoopError("message loop")

    bus.drain = explode
    bus._queue.append(("a", "b", {"type": "chat"}))

    assert _drain_to_quiescence(bus) is False
    assert calls["n"] == 1, \
        f"the runaway-loop guard was retried {calls['n']} times"


def test_an_undeliverable_action_is_not_reported_as_applied():
    """Routing the action paths through the shared drain helper made them
    SWALLOW delivery failures: the bool was discarded and the client was
    told its bet applied while peers may never have seen it. That also
    silenced the logging at client_server._handle_command, because the
    error stopped arriving there at all."""
    bus, sessions, order = _make_sessions(2)
    human = sessions[order[0]]
    _wrap_with_drain(human, bus)

    class Exploding:
        def handle_message(self, from_conn, msg):
            raise RuntimeError("peer is broken")

    bus.register(order[1], Exploding())
    human._transport.broadcast({"type": "chat", "payload": {}})

    with pytest.raises(RuntimeError, match="could not be delivered"):
        human.send_bet_action("call", 0)
