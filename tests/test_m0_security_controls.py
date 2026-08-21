"""Deliberate-break controls for the M0 security fixes (issue #37, D1-D4).

A security test that passes proves nothing on its own: it may be asserting
a property the code never had a way to violate, or reading an observable
the fix does not actually move. The control answers the only question that
matters -- does this test depend on the fix? -- by restoring the pre-fix
behaviour in place and showing the observable flip back.

The breaks are staged with monkeypatch rather than by hand-editing
holdem/p2p/session.py, so they are committed evidence that re-runs on
every CI job instead of a paragraph in a pull request saying someone once
saw them fire. Each pre-fix function below is the code as it stood on
``main`` at 07f61a7, reduced only where noted.

Read with tests/test_m0_security.py: every control here names the invariant
test it is the control FOR.
"""
import sys
from pathlib import Path

from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import crypto_gate
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import AUTHOR_MODE_WIRE, Player, Session

# The controls stage the SAME scenarios the invariant tests do. Sharing the
# builders rather than re-writing them is deliberate: a control that set up
# a subtly different table would be measuring a different thing.
from test_m0_security import compat_host, seated, wire_session


# ------------------------------------------------------ pre-fix behaviour

def _prefix_bind_seat_keys(self):
    """_bind_seat_keys as it stood on main: freeze whatever resolved.

    Byte-for-byte the shipped logic minus the two guards M0 added -- the
    incomplete-map refusal and the wire-mode zero-key refusal. Everything
    else, including the idempotent early return that makes the map
    one-way, is unchanged, because the one-way-ness is what turns a
    partial freeze from a nuisance into a permanent one.
    """
    if self._seat_keys:
        return
    bound = {}
    with self._lock:
        for seat, cid in enumerate(self._seat_order):
            player = self.players.get(cid)
            key = getattr(player, "ed25519_pubkey_hex", "") if player else ""
            if key:
                bound[seat] = key
    if not bound:
        return                          # "compat: no envelopes, no keys"
    self._seat_keys = bound


def _prefix_on_player_info(self, conn_id, msg):
    """_on_player_info as it stood on main: host check, then the roster.

    Reduced to the roster write and the join-order append. The player_ack
    reply and the roster re-broadcast are omitted so the control breaks
    the lifecycle perimeter and nothing else -- what D2 is about is which
    states may mutate the roster, not what the host says afterwards.
    """
    if not self.is_host:
        return
    payload = msg.get("payload", {})
    with self._lock:
        self.players[conn_id] = Player(
            conn_id            = conn_id,
            peer_id            = msg.get("pubkey", "")[:16],
            nickname           = payload.get("nickname", "Player"),
            avatar_b64         = payload.get("avatar_b64", ""),
            x25519_pubkey_hex  = payload.get("x25519_pubkey_hex", ""),
            ed25519_pubkey_hex = self._adopt_signing_key(
                conn_id, msg.get("pubkey", "")),
            is_host            = False,
        )
        if conn_id not in self._join_order:
            self._join_order.append(conn_id)


def _prefix_assert_deal_preconditions(self):
    """The mandate as it stood on main: keyed to author_mode alone.

    The adopted policy did not participate, so a compat table that had
    settled on Bayer-Groth could deal without prevention.
    """
    if self.terminal_state is not None:
        raise RuntimeError("cannot begin a hand: session terminated")
    if self._deal_policy is None:
        raise RuntimeError("cannot begin hand: no deal policy has been adopted")
    if self.author_mode == AUTHOR_MODE_WIRE and not self.prevention:
        raise RuntimeError("cannot begin hand: wire mode requires prevention")


# ------------------------------------------------------------------- D1

def test_control_partial_freeze_returns_when_the_guard_is_removed(monkeypatch):
    """CONTROL FOR test_d1_partial_seat_key_map_is_never_frozen.

    With the guard gone the poison state is back: two of three seats
    frozen, the third refused, and the refusal is permanent -- a later
    roster carrying the missing key cannot repair it, because the map is
    one-way. The named test's ``pytest.raises(RuntimeError)`` cannot hold
    against this behaviour, which is what makes it load-bearing.
    """
    monkeypatch.setattr(Session, "_bind_seat_keys", _prefix_bind_seat_keys)
    s = seated(wire_session(), {"a": "AA", "b": "BB", "c": ""})

    s._bind_seat_keys()                     # no refusal: this is the defect

    assert s._seat_keys == {0: "AA", 1: "BB"}
    assert s._author_owns_seat("c", None, 2) is False

    s.players["c"].ed25519_pubkey_hex = "CC"
    s._bind_seat_keys()
    assert s._seat_keys == {0: "AA", 1: "BB"}, (
        "the freeze is one-way, so seat 2 is stranded for the session")


def test_control_the_disconnect_window_strands_a_seat_again(monkeypatch):
    """CONTROL FOR test_d1_disconnect_before_freeze_cannot_strand_a_seat.

    The reachable form, with no attacker: handle_disconnect has already
    popped the dropped peer from ``players`` by the time start_p2p_hand
    binds, so its seat resolves to no key.
    """
    monkeypatch.setattr(Session, "_bind_seat_keys", _prefix_bind_seat_keys)
    s = seated(wire_session(), {"a": "AA", "b": "BB", "c": "CC"})
    s.players.pop("c")

    s._bind_seat_keys()

    assert s._seat_keys == {0: "AA", 1: "BB"}
    assert s._author_owns_seat("c", "CC", 2) is False, (
        "the returning peer's real key no longer authorizes its own seat")


def test_control_wire_zero_key_starts_a_dead_table(monkeypatch):
    """CONTROL FOR test_d1_wire_mode_rejects_a_wholly_unresolved_seat_order.

    The review finding this control exists for: the pre-fix guard read
    ``if not bound: return`` under a comment about compat, but never
    checked the mode. Authorization still fails closed afterwards -- so
    this looks safe -- yet it fails at the wrong MOMENT. The hand starts
    and then every seat is refused at message time: a dead table wearing
    the costume of a live one.
    """
    monkeypatch.setattr(Session, "_bind_seat_keys", _prefix_bind_seat_keys)
    s = seated(wire_session(), {"a": "", "b": "", "c": ""})

    s._bind_seat_keys()                     # returns quietly; nothing raised

    assert s._seat_keys == {}
    assert s._author_owns_seat("a", "AA", 0) is False
    assert s._author_owns_seat("b", "BB", 1) is False, (
        "every seat refused after the hand has already begun")


def test_control_the_wire_guard_does_not_fire_on_an_empty_seat_order():
    """NEGATIVE CONTROL: 'no seats yet' is not 'seats that will not resolve'.

    Against the REAL implementation, because what is being checked is that
    the fix did not overreach -- a guard that refused an empty seat order
    would break every wire session before it had a table.
    """
    s = wire_session()
    s._seat_order = []

    s._bind_seat_keys()

    assert s._seat_keys == {}


# ------------------------------------------------------------------- D2

def test_control_player_info_mutates_a_playing_roster_again(monkeypatch):
    """CONTROL FOR test_d2_player_info_is_refused_outside_lobby.

    Measured on main as ``state: PLAYING | intruder admitted: True |
    join_order: ['intruder']``. It gains no seat -- _seat_order and
    _seat_keys are frozen before the first hand -- but the roster is not a
    scratchpad, and _on_player_ack and _on_game_start already carry this
    perimeter.
    """
    monkeypatch.setattr(Session, "_on_player_info", _prefix_on_player_info)
    s = compat_host()
    seated(s, {"host": "HH", "p1": "11"})
    s.state = "PLAYING"

    s._on_player_info("intruder", {"pubkey": "ZZ",
                                   "payload": {"nickname": "mallory"}})

    assert "intruder" in s.players
    assert "intruder" in s._join_order


def test_control_a_terminated_session_accepts_a_roster_write_again(monkeypatch):
    """CONTROL FOR test_d2_terminated_session_refuses_player_info.

    Terminality is the second half of the perimeter and it is not implied
    by the state check: terminate() sets state to ENDED, so a guard keyed
    on PLAYING alone lapses exactly when the session dies.
    """
    monkeypatch.setattr(Session, "_on_player_info", _prefix_on_player_info)
    s = compat_host()
    s.state = "LOBBY"
    s.terminate("TEST", "done")

    s._on_player_info("joiner", {"pubkey": "JJ", "payload": {}})

    assert "joiner" in s.players


def test_control_lobby_admission_survives_the_real_perimeter():
    """NEGATIVE CONTROL: the fix must not break the legitimate path.

    Against the REAL handler. D2's accepted constraint is that lobby
    admission and roster behaviour keep working; a perimeter that refused
    a joiner in LOBBY would be a worse defect than the one it closed.
    """
    s = compat_host()
    s.state = "LOBBY"

    s._on_player_info("joiner", {"pubkey": "JJ",
                                 "payload": {"nickname": "legit"}})

    assert s.players["joiner"].nickname == "legit"
    assert "joiner" in s._join_order


# ------------------------------------------------------------------- D4

def _compat_session(cid="peer0"):
    bus = InMemoryBus()
    s = Session(is_host=False, nickname="P", avatar_b64="",
                transport=InMemoryTransport(bus, cid),
                master_secret=b"\x02" * 32)
    s.local_conn_id = cid
    return s


def test_control_a_default_makes_prevention_omissible_again():
    """CONTROL FOR test_d4_driver_prevention_cannot_be_omitted.

    The residual real risk behind D4: the insecure state was reachable by
    OMISSION. Restoring the default -- here as a subclass, since the break
    is the signature rather than the body -- and the construction site that
    forgot the argument deals detection-only while the table believes it is
    running Bayer-Groth. Nothing downstream says so.

    Crypto-gated for the same reason as the invariant test: importing the
    driver pulls in ristretto.
    """
    crypto_gate.require_crypto()
    from holdem.p2p.mental_deal_driver import MentalDealDriver

    class DriverWithOldDefault(MentalDealDriver):
        def __init__(self, *args, prevention=False, **kwargs):
            super().__init__(*args, prevention=prevention, **kwargs)

    driver = DriverWithOldDefault(
        session_id="s", hand_no=1, local_seat=0, seats_in=[0, 1],
        button=0, master_secret=b"m", send=lambda m: None)

    assert driver.deal.prevention is False, (
        "constructed without stating a prevention mode, and it dealt "
        "detection-only silently")


def test_control_the_author_mode_mandate_lets_a_bg_compat_table_through(
        monkeypatch):
    """CONTROL FOR test_d4_bg_policy_forces_prevention_regardless_of_author_mode.

    With the mandate keyed to author_mode alone, a compat table that
    settled on Bayer-Groth deals without prevention and nothing refuses
    it. Today `prevention` is derived from the policy, so the two cannot
    drift on their own -- forcing them apart is how both the test and this
    control prove the guard is load-bearing rather than decorative.
    """
    monkeypatch.setattr(Session, "_assert_deal_preconditions",
                        _prefix_assert_deal_preconditions)
    s = _compat_session()
    s._adopt_deal_policy(Session.DEAL_POLICY_BG)

    with mock.patch.object(type(s), "prevention",
                           property(lambda self: False)):
        s._assert_deal_preconditions()      # the defect: no refusal at all


def test_control_a_detection_only_compat_table_is_still_legal():
    """NEGATIVE CONTROL: against the REAL guard.

    Do not force deliberately detection-only harnesses onto Bayer-Groth.
    The policy-keyed mandate must refuse a peer that disagrees with its
    table, not a table that legitimately declared detection-only.
    """
    s = _compat_session()
    s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)

    assert s.prevention is False
    s._assert_deal_preconditions()


def test_control_the_real_guard_still_refuses_the_same_table():
    """The other half of the pair above: same scenario, real guard, refused.

    Stated as its own test so the control and the invariant are not read
    from one run -- if the monkeypatch above ever silently failed to
    apply, this is what distinguishes 'the break was staged' from 'the
    break did nothing'.
    """
    s = _compat_session()
    s._adopt_deal_policy(Session.DEAL_POLICY_BG)

    with mock.patch.object(type(s), "prevention",
                           property(lambda self: False)):
        with pytest.raises(RuntimeError, match="every participating peer path"):
            s._assert_deal_preconditions()
