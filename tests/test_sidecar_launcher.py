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
from holdem.sidecar_launcher import (
    BotDriver, _deal_first_hand, _make_sessions, _wrap_with_drain,
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
