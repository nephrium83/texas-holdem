"""Pins the table-wide deal policy at the session layer.

The deal policy is a property of the TABLE, not of a peer. It rides in the
game_start table_settings every peer already receives, so peers reach the
same policy without negotiating and without a new message type.

There is deliberately NO default. The previous arrangement -- a boolean
whose absence meant detection-only -- is how every shipped game came to run
without shuffle proofs while the Bayer-Groth implementation sat complete
and unreferenced. A table that does not say how it deals is refused.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crypto_gate import initialize_for_tests, NativeCryptoUnavailable
from tests import joiner_helpers as JH

try:
    initialize_for_tests()
    from holdem.p2p.timeout import FakeClock as _DependencyClock
    from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
    from holdem.p2p.session import (
        AUTHOR_MODE_COMPAT, AUTHOR_MODE_WIRE, AUTHOR_SEQ_START, Player,
        Session,
    )
except NativeCryptoUnavailable as exc:
    pytest.skip(f"libsodium unavailable: {exc}", allow_module_level=True)


KEY       = Session.DEAL_POLICY_SETTING
BG        = Session.DEAL_POLICY_BG
DETECTION = Session.DEAL_POLICY_DETECTION


def make_table(n=2, settings=None):
    """n started Sessions on one bus, following tests/test_session_inmemory.

    The bus is a compat transport, so both policies are legal here; wire
    mode is exercised separately below.
    """
    bus = InMemoryBus()
    sessions = {}
    for i in range(n):
        cid = f"peer{i}"
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid),
                    clock=_DependencyClock(),
                    master_secret=bytes(range(32)))
        s.local_conn_id = cid
        if not s.is_host:
            s._host_conn_id = "peer0"
        bus.register(cid, s)
        sessions[cid] = s
    host = sessions["peer0"]
    for i in range(n):
        host.players[f"peer{i}"] = Player(conn_id=f"peer{i}",
                                          peer_id=f"peer{i}",
                                          nickname=f"P{i}", avatar_b64="")
    host.start_game(dict(settings if settings is not None else {Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: BG}))
    bus.drain()
    return sessions, list(sessions)


class _WireTransport:
    """Verified-envelope transport double: puts a Session in wire mode."""

    delivers_verified_envelopes = True

    def __init__(self):
        self.sent = []

    def broadcast(self, msg):
        self.sent.append(msg)

    def broadcast_except(self, exclude, msg):
        self.sent.append(msg)

    def send(self, to, msg):
        self.sent.append(msg)


def _wire_joiner():
    s = Session(is_host=False, nickname="J", avatar_b64="",
                transport=_WireTransport(),
                joiner_admission=JH.joiner_admission(),
                clock=_DependencyClock(),
                master_secret=bytes(range(32)))
    JH.authenticate_host(s, "host")
    s.local_conn_id = "me"
    s._host_conn_id = "host"
    return s


# --------------------------------------------------- a policy is required

def test_a_table_with_no_deal_policy_is_refused():
    """The headline rule. An omitted policy used to mean detection-only,
    which is how the shipped product ran without proofs for its whole
    life. It is now a refusal, not a default."""
    with pytest.raises(ValueError, match="deal_policy"):
        make_table(2, settings={})


def test_a_table_with_other_settings_but_no_policy_is_refused():
    """Not special-cased on the empty dict: a fully-populated table that
    simply forgot the policy is refused on the same terms."""
    with pytest.raises(ValueError, match="deal_policy"):
        make_table(2, settings={"small_blind": 1, "big_blind": 2})


@pytest.mark.parametrize("value", [True, False, 1, 0, None, ["bayer-groth-v1"]])
def test_a_non_string_policy_is_refused_never_coerced(value):
    """The bool() coercion is gone, and this is what pins that.

    It used to be that bool("false") is True, bool("0") is True, and
    bool("no") is True -- so a stringly-typed config could mean the exact
    opposite of what it read. True and 1 are here alongside the falsey
    values on purpose: accepting a truthy non-string would be just as
    wrong, because the point is that the field names a protocol, not a
    switch.
    """
    with pytest.raises(ValueError, match="deal_policy"):
        make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: value})


@pytest.mark.parametrize("value", [
    "true", "false", "yes", "no", "0", "1", "",
    "bayer-groth",            # unversioned
    "BAYER-GROTH-V1",         # wrong case
    "bayer_groth_v1",         # underscores
    "detection-only",         # unversioned
])
def test_an_unrecognised_policy_string_is_refused(value):
    """Strict equality against a known set, not a prefix or fuzzy match."""
    with pytest.raises(ValueError, match="deal_policy"):
        make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: value})


def test_a_value_that_merely_stringifies_to_a_policy_is_refused():
    """Pins the isinstance(str) check specifically.

    Controls showed why this test is needed and what it must look like.
    Replacing `isinstance(value, str)` with `value = str(value)` fires
    nothing against ordinary inputs, because strict membership already
    refuses "True", "1", "['bayer-groth-v1']" and friends -- the coercion
    break is benign for anything JSON can carry. The isinstance check
    earns its place only against a value whose str() IS a valid policy, so
    that is what this asserts. Not reachable from the wire, where settings
    arrive as JSON; reachable from any Python caller of start_game.
    """
    class Sneaky:
        def __str__(self):
            return Session.DEAL_POLICY_BG

    assert Session.parse_deal_policy(
        {Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: Sneaky()}, AUTHOR_MODE_COMPAT) is None
    with pytest.raises(ValueError, match="deal_policy"):
        make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: Sneaky()})


# --------------------------------------------------- wire mode mandates BG

def test_wire_mode_refuses_detection_only():
    """The mandate. Detection-only is a legitimate, explicit mode for
    harnesses and benchmarks; on a transport carrying real envelopes it is
    refused, because "we will notice afterwards that someone cheated" is
    not a property this protocol will advertise as trustless."""
    assert Session.parse_deal_policy({Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: DETECTION}, AUTHOR_MODE_WIRE) is None


def test_wire_mode_accepts_bayer_groth():
    assert Session.parse_deal_policy({Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: BG}, AUTHOR_MODE_WIRE) == BG


def test_compat_mode_accepts_detection_only_explicitly():
    assert Session.parse_deal_policy(
        {Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: DETECTION}, AUTHOR_MODE_COMPAT) == DETECTION
    # ...but still not by omission.
    assert Session.parse_deal_policy({}, AUTHOR_MODE_COMPAT) is None


def test_a_wire_joiner_refuses_a_detection_only_table_before_playing():
    """Refusal lands BEFORE the peer accepts the table, which is the whole
    reason the check moved out of begin_hand. A peer that discovers the
    disagreement at deal time has already announced itself as playing.

    Asserted on terminal_record.previous_state, NOT on s.state. That is the
    only field that can tell the difference: terminate() overwrites state
    with "ENDED", so `s.state != "PLAYING"` holds whether the refusal ran
    before or after the transition, and a control that moved the refusal
    after it fired nothing.
    """
    s = _wire_joiner()
    s.handle_message("host", {"type": "game_start", "payload": {
        "seat_order": ["host", "me"], "table_settings": {Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: DETECTION}}})
    assert s.terminal_state == Session.POLICY_REFUSED
    assert s.deal_policy is None
    assert s.terminal_record is not None
    assert s.terminal_record.previous_state != "PLAYING", (
        "the peer entered PLAYING and was only then refused; refusal must "
        "precede accepting the table")
    assert s.terminal_record.previous_state == "LOBBY"


def test_a_refused_table_never_reaches_the_game_start_callback():
    """The observable consequence of the ordering, from the outside: a
    refused table must not notify anything that a game began. The Tk lobby
    tears down its dialog and launches a table in this callback."""
    s = _wire_joiner()
    fired = []
    s.on_game_start = fired.append
    s.handle_message("host", {"type": "game_start", "payload": {
        "seat_order": ["host", "me"], "table_settings": {Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: DETECTION}}})
    assert fired == [], "a refused table still announced a game start"


def test_a_refused_table_records_what_it_refused():
    """POLICY_REFUSED must be diagnosable. session_id is a digest now, so
    the record carries the policy explicitly or the cause is unrecoverable."""
    s = _wire_joiner()
    s.handle_message("host", {"type": "game_start", "payload": {
        "seat_order": ["host", "me"], "table_settings": {Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: DETECTION}}})
    assert DETECTION in s.terminal_reason


# --------------------------------------------------------- propagation

def test_policy_propagates_to_every_peer():
    sessions, _ = make_table(3, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: BG})
    for conn_id, s in sessions.items():
        assert s.deal_policy == BG, f"{conn_id} missed the table policy"
        assert s.prevention is True


def test_host_and_peers_agree_without_negotiating():
    """No peer announces a policy; they all read the same broadcast."""
    on = make_table(4, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: BG})[0]
    off = make_table(4, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: DETECTION})[0]
    assert len({s.deal_policy for s in on.values()}) == 1
    assert len({s.deal_policy for s in off.values()}) == 1
    assert next(iter(on.values())).prevention is True
    assert next(iter(off.values())).prevention is False


# ------------------------------------------------------- the chokepoint

def test_adopting_the_same_policy_twice_is_idempotent():
    """Retries and relay echoes must stay harmless."""
    sessions, _ = make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: BG})
    peer = sessions["peer1"]
    assert peer._adopt_deal_policy(BG) is True
    peer._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
    assert peer.deal_policy == BG


def test_changing_the_policy_is_refused_by_the_chokepoint():
    """None -> A adopts, A -> A is idempotent, A -> B is refused. Asserted
    on the writer itself, not on a handler that happens to call it."""
    sessions, _ = make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: BG})
    peer = sessions["peer1"]
    assert peer._adopt_deal_policy(DETECTION) is False
    assert peer.deal_policy == BG, "a refused adoption still wrote the field"


def test_a_second_game_start_cannot_change_the_policy():
    """Capabilities freeze once play begins."""
    sessions, _ = make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: BG})
    peer = sessions["peer1"]
    peer._on_game_start("peer0", {"payload": {
        "seat_order": ["peer0", "peer1"],
        "table_settings": {Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: DETECTION}}})
    assert peer.deal_policy == BG
    assert peer.prevention is True


def test_a_malformed_second_game_start_does_not_raise():
    """The freeze branch is a reject path reached from a message handler.
    Raising there would carry a hostile or stale game_start out of
    handle_message and onto the transport's dispatch thread."""
    sessions, _ = make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: BG})
    peer = sessions["peer1"]
    peer._on_game_start("peer0", {"payload": {
        "seat_order": ["peer0", "peer1"], "table_settings": {Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: 12345}}})
    assert peer.deal_policy == BG


def test_a_fresh_session_does_not_inherit_a_previous_policy():
    a, _ = make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: DETECTION})
    b, _ = make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: BG})
    assert all(s.deal_policy == DETECTION for s in a.values())
    assert all(s.deal_policy == BG for s in b.values())


# ------------------------------------------------------- driver wiring

def test_driver_receives_the_table_policy():
    """The single mapping point from policy string to the deal layer's
    bool. This is the bridge that a downgrade would cut."""
    for policy, expected in ((DETECTION, False), (BG, True)):
        sessions, _ = make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: policy})
        for s in sessions.values():
            s.begin_hand(hand_no=1, button=0)
            assert s._deal_driver.deal.prevention is expected


def test_detection_only_hand_emits_no_proof():
    sessions, _ = make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: DETECTION})
    for s in sessions.values():
        s.begin_hand(hand_no=1, button=0)
    for s in sessions.values():
        for msg in s._deal_outbox:
            assert "proof" not in msg


def test_begin_hand_refuses_without_an_adopted_policy():
    """Belt to _on_game_start's braces: a hand that did not follow an
    accepted table has no policy to derive a deal context from."""
    bus = InMemoryBus()
    s = Session(is_host=True, nickname="P0", avatar_b64="",
                transport=InMemoryTransport(bus, "peer0"),
                clock=_DependencyClock(),
                master_secret=bytes(range(32)))
    s.local_conn_id = "peer0"
    s.configure_seats(["peer0", "peer1"])
    with pytest.raises(RuntimeError, match="no deal policy"):
        s.begin_hand(hand_no=1, button=0)


# --------------------------------------------------- deal context binding

def test_the_deal_context_binds_the_policy():
    """"bayer-groth-v1" is a context commitment, not decorative metadata:
    two tables identical but for their policy must not share a deal
    domain."""
    bg, _ = make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: BG})
    det, _ = make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: DETECTION})
    assert (bg["peer0"]._deal_session_id()
            != det["peer0"]._deal_session_id())


def test_the_deal_context_encoding_is_injective():
    """The old form was "poker|" + "|".join(order), which collides:
    ["a|b","c"] and ["a","b|c"] both encode to "poker|a|b|c", so two
    structurally different tables shared a DKG domain and a
    proof-of-possession minted at one verified at the other."""
    bus = InMemoryBus()

    def ctx(order):
        s = Session(is_host=True, nickname="P", avatar_b64="",
                    transport=InMemoryTransport(bus, "x"),
                    clock=_DependencyClock(),
                    master_secret=bytes(range(32)))
        s._deal_policy = BG
        s._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
        s._seat_order = order
        return s._deal_context_bytes()

    assert ctx(["a|b", "c"]) != ctx(["a", "b|c"])


def test_the_deal_context_refuses_non_string_seat_ids():
    """Fail closed, matching the old '|'.join behaviour. Coercing with
    str() would trade a loud failure for an injectivity hole."""
    bus = InMemoryBus()
    s = Session(is_host=True, nickname="P", avatar_b64="",
                transport=InMemoryTransport(bus, "x"),
                clock=_DependencyClock(),
                master_secret=bytes(range(32)))
    s._deal_policy = BG
    s._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
    s._seat_order = ["ok", 7]
    with pytest.raises(TypeError, match="seat id must be str"):
        s._deal_context_bytes()


# ------------------------------------------------------ end-to-end hand

def make_deal_table(n, policy):
    """n Sessions on one bus with the seat order configured directly.

    Follows tests/test_session_deal, which bypasses the lobby handshake.
    The policy is installed through _adopt_deal_policy rather than by
    writing _deal_policy: a setup path that bypasses the single writer
    would let these tests pass while the chokepoint was broken.
    """
    bus = InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid),
                    clock=_DependencyClock(),
                    master_secret=bytes(range(32)))
        s.local_conn_id = cid
        s.configure_seats(list(order))
        assert s._adopt_deal_policy(policy)
        s._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
        bus.register(cid, s)
        sessions[cid] = s
    return bus, sessions, order


@pytest.mark.parametrize("n", [2, 3])
def test_full_prevention_hand_over_real_sessions(n):
    """The integration proof.

    Convergence under prevention is only reachable if every round's proof
    was generated, serialized onto the wire, decoded, and verified by every
    peer -- a single failure aborts the hand instead.
    """
    bus, sessions, order = make_deal_table(n, BG)
    for cid in order:
        sessions[cid].begin_hand(hand_no=1, button=0)
    bus.drain()

    decks = [[ct.to_hex() for ct in sessions[c]._deal_driver.deal.deck]
             for c in order]
    assert all(deck == decks[0] for deck in decks)
    for cid in order:
        deal = sessions[cid]._deal_driver.deal
        assert deal.prevention is True
        assert deal.abort_reason is None
        assert deal.is_shuffle_complete()
        assert deal.hole_complete()


def test_prevention_hand_puts_proofs_on_the_wire():
    """Guards the test above: if no proof were ever emitted, convergence
    would still hold and the assertion would prove nothing."""
    seen = []
    bus, sessions, order = make_deal_table(2, BG)
    original = bus.enqueue

    def spy(src, dst, msg):
        if msg.get("type") == "deck_round" or (
                isinstance(msg.get("payload"), dict)
                and msg["payload"].get("round") is not None):
            body = msg.get("payload", msg)
            seen.append("proof" in body)
        return original(src, dst, msg)

    bus.enqueue = spy
    for cid in order:
        sessions[cid].begin_hand(hand_no=1, button=0)
    bus.drain()
    assert seen, "no deck_round crossed the bus"
    assert all(seen), "a deck_round crossed the bus without a proof"


def test_detection_only_hand_still_converges():
    """The explicit compat mode must be unaffected by any of the above."""
    bus, sessions, order = make_deal_table(3, DETECTION)
    for cid in order:
        sessions[cid].begin_hand(hand_no=1, button=0)
    bus.drain()
    decks = [[ct.to_hex() for ct in sessions[c]._deal_driver.deal.deck]
             for c in order]
    assert all(deck == decks[0] for deck in decks)
    for cid in order:
        deal = sessions[cid]._deal_driver.deal
        assert deal.prevention is False
        assert deal.abort_reason is None
        assert deal.hole_complete()


# ------------------------------------------- terminal atomicity & forensics

def test_the_terminal_record_carries_the_deal_policy():
    """Pins the field, not a string that happens to mention it.

    session_id is a digest now, so a POLICY_REFUSED record is unreadable
    without this. Review deleted the field and the entire suite stayed
    green: the only test that looked at it asserted on terminal_reason,
    which interpolates the value incidentally.
    """
    sessions, _ = make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: BG})
    peer = sessions["peer1"]
    peer.terminate(Session.HOST_LOST, "host dropped")
    assert peer.terminal_record is not None
    assert peer.terminal_record.deal_policy == BG


def test_a_malformed_seat_order_cannot_split_the_terminal_transition():
    """Terminal state is absorbing AND atomic: flags, record, teardown.

    terminate() used to build the record by recomputing the deal context,
    which raises on a non-str seat id -- and _on_game_start adopts whatever
    seat_order a host sends without validating its shape. So a host sending
    ["host", 7, "me"] could set terminal_state and then have the record
    construction raise out of the message handler, leaving a session that
    was terminal but had never produced a record, never invalidated its
    pending work, and never notified its callbacks.
    """
    sessions, _ = make_table(2, settings={Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE, KEY: BG})
    peer = sessions["peer1"]
    notified = []
    peer.on_session_terminated = notified.append

    peer._seat_order = ["peer0", 7, "peer1"]        # a host said so
    peer.terminate(Session.HOST_LOST, "host dropped")   # must not raise

    assert peer.terminal_state == Session.HOST_LOST
    assert peer.terminal_record is not None, \
        "terminal flags were set but no record was produced"
    assert peer.terminal_record.deal_policy == BG
    assert notified, "termination completed without notifying callbacks"


def test_a_session_terminated_from_the_lobby_records_no_deal_context():
    """None is the honest answer when no hand ever existed -- and building
    one would mean inventing a context for a table that never started."""
    bus = InMemoryBus()
    s = Session(is_host=True, nickname="P0", avatar_b64="",
                transport=InMemoryTransport(bus, "peer0"),
                clock=_DependencyClock(),
                master_secret=bytes(range(32)))
    s.local_conn_id = "peer0"
    s.terminate(Session.LOCAL_SHUTDOWN, "user quit")
    assert s.terminal_record is not None
    assert s.terminal_record.session_id is None
    assert s.terminal_record.deal_policy is None


# ------------------------------------------------ hand-start transactionality

def test_a_refused_deal_leaves_no_bettable_table():
    """No live gameplay state before every precondition holds.

    _begin_p2p_hand used to construct and START the replica -- posting
    blinds, opening betting -- and only then call begin_hand, which this
    mandate gave new refusal paths. A refusal there left a table that
    accepted bets (send_bet_action gates on _replica alone) and could never
    settle, because settling needs a deal that was refused. There is no
    timeout to rescue it: check_deadlines has no production caller.
    """
    bus = InMemoryBus()
    order = ["peer0", "peer1"]
    s = Session(is_host=True, nickname="P0", avatar_b64="",
                transport=InMemoryTransport(bus, "peer0"),
                clock=_DependencyClock(),
                master_secret=bytes(range(32)))
    s.local_conn_id = "peer0"
    s.configure_seats(order)                     # no policy adopted

    with pytest.raises(RuntimeError, match="no deal policy"):
        s.start_p2p_hand(hand_no=1, names=["P0", "P1"], stacks=[500, 500],
                         sb=5, bb=10, button=0)

    assert s.replica is None, "a refused hand left a live replica"
    assert s._deal_driver is None
    assert s.send_bet_action("call", 0) == "rejected", \
        "a refused hand left a table that accepts bets"


def test_the_chokepoint_refuses_a_policy_it_does_not_recognise():
    """A single writer that accepts anything is only half a chokepoint: it
    centralises WHEN the field changes and leaves WHAT it may hold to the
    caller. _deal_first_hand takes a caller-supplied policy."""
    bus = InMemoryBus()
    s = Session(is_host=True, nickname="P0", avatar_b64="",
                transport=InMemoryTransport(bus, "peer0"),
                clock=_DependencyClock(),
                master_secret=bytes(range(32)))
    with pytest.raises(ValueError, match="not a deal policy"):
        s._adopt_deal_policy("banana")
    assert s.deal_policy is None


def test_the_deal_context_refuses_a_session_with_no_adopted_policy():
    """The encoding has no representation for an invalid lifecycle state.

    An earlier version used `self._deal_policy or ""`, which folded None
    and "" onto the same pre-image -- an injectivity hole in the one
    function whose entire purpose is injectivity. Refusing instead of
    substituting is what makes the collision unrepresentable rather than
    merely unlikely, and it is why forensic callers use
    _recorded_session_id() rather than routing through here.
    """
    bus = InMemoryBus()
    s = Session(is_host=True, nickname="P", avatar_b64="",
                transport=InMemoryTransport(bus, "x"),
                clock=_DependencyClock(),
                master_secret=bytes(range(32)))
    s._seat_order = ["a", "b"]
    assert s.deal_policy is None
    with pytest.raises(RuntimeError, match="before a policy is adopted"):
        s._deal_context_bytes()


def test_a_hand_that_fails_after_the_replica_exists_rolls_it_back():
    """Pins the ROLLBACK, which the hoist test cannot reach.

    _begin_p2p_hand now checks policy preconditions before constructing
    anything -- so test_a_refused_deal_leaves_no_bettable_table exercises
    the hoist and never touches the rollback. Review deleted the entire
    try/except and the whole suite stayed green.

    begin_hand still has refusal paths that NEED the replica to evaluate
    (is the local seat dealt into this hand?). This drives one of them: a
    local conn_id absent from the seat order passes every precondition,
    builds and starts the replica -- posting blinds -- and only then
    raises. Without the rollback that leaves a bettable table with no deal
    driver, which can never settle.
    """
    bus = InMemoryBus()
    order = ["peer0", "peer1"]
    s = Session(is_host=True, nickname="P0", avatar_b64="",
                transport=InMemoryTransport(bus, "peer0"),
                clock=_DependencyClock(),
                master_secret=bytes(range(32)))
    s.local_conn_id = "peer0"
    s.configure_seats(order)
    assert s._adopt_deal_policy(DETECTION)
    s._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
    s.local_conn_id = "ghost"          # seated nowhere

    with pytest.raises(RuntimeError, match="local seat not in seat order"):
        s.start_p2p_hand(hand_no=1, names=["P0", "P1"], stacks=[500, 500],
                         sb=5, bb=10, button=0)

    assert s.replica is None, \
        "a hand that failed after the replica existed left it live"
    assert s._deal_driver is None
    assert s.send_bet_action("call", 0) == "rejected"


def test_reseating_clears_the_cached_deal_context():
    """The cache must not outlive the state it describes.

    _recorded_session_id() returns a cached digest so terminate() never has
    to recompute. configure_seats changes the seat order the digest commits
    to, so the cache has to go with it -- otherwise a terminal record can
    name a context this session was never running under.
    """
    bus = InMemoryBus()
    s = Session(is_host=True, nickname="P0", avatar_b64="",
                transport=InMemoryTransport(bus, "peer0"),
                clock=_DependencyClock(),
                master_secret=bytes(range(32)))
    s.local_conn_id = "peer0"
    s.configure_seats(["peer0", "peer1"])
    assert s._adopt_deal_policy(BG)
    s._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
    first = s._deal_session_id()
    assert s._recorded_session_id() == first

    s.configure_seats(["peer0", "peer1", "peer2"])
    assert s._recorded_session_id() != first, \
        "the cached deal context survived a seat-order change"


# ===========================================================================
# Prevention over the VERIFIED-ENVELOPE path, with one identity per peer
# (audit #181)
#
# Everything above runs on InMemoryBus, which hands flat dicts straight
# across. A Session on it selects AUTHOR_MODE_COMPAT, where seat authority
# falls back to the delivering conn_id -- correct there, because the bus
# really is authoritative about who sent what, but it means those tests
# contain no author identities to get wrong. The wire-mode tests above go
# the other way: a lone Session on _WireTransport, a double that collects
# messages and never delivers one, so no hand can run.
#
# So "full prevention integration" was proven on a path with no signatures
# and no distinct keys, which is what #181 says. These run a real
# Bayer-Groth hand the production way instead: every message packed with
# THAT peer's own Ed25519 key, verified on arrival, seats frozen onto those
# keys, and each peer holding its own master secret.
# ===========================================================================


class SignedBus(InMemoryBus):
    """InMemoryBus that really signs and verifies, one signer per peer.

    Packing happens at enqueue, where the sender is known, and the
    inherited drain then delivers an already-verified envelope -- so the
    queueing, fan-out and drain-loop protection are the ones every other
    suite uses, and only the envelope handling is new.

    It does what transport.py does at that boundary, including folding flat
    hostless fields into payload through the PRODUCTION helper rather than
    a copy of it, so the envelope shape cannot drift from the real one.
    """

    def __init__(self, clock, tamper=None):
        super().__init__()
        self._clock = clock
        self._signers = {}
        #: Applied to each outgoing message BEFORE it is signed, so the
        #: envelope that arrives is VALIDLY signed by its real author and
        #: carries bad content. That is the threat worth testing: a peer
        #: that signs a wrong proof. Forging a signature is not -- unpack
        #: rejects it, which the signing layer's own suites cover.
        self._tamper = tamper
        #: (from, to, envelope) for every delivery, so a test can replay
        #: real traffic rather than a hand-built imitation of it.
        self.log = []

    def register_signer(self, conn_id, signer):
        self._signers[conn_id] = signer

    def envelope(self, from_conn, msg):
        from holdem.p2p import wire as _wire
        from holdem.p2p.transport import _wire_payload
        if "sig" in msg:
            return msg              # pre-packed, as production passes through
        if self._tamper is not None:
            msg = self._tamper(from_conn, msg) or msg
        raw = _wire.pack(msg.get("type", ""), _wire_payload(msg),
                         signer=self._signers[from_conn], clock=self._clock)
        # unpack is the verification step: a bad signature raises here, so
        # nothing unverified can reach a Session through this bus.
        return _wire.unpack(raw, clock=self._clock)

    def enqueue(self, from_conn, to_conn, msg):
        env = self.envelope(from_conn, msg)
        self.log.append((from_conn, to_conn, env))
        super().enqueue(from_conn, to_conn, env)

    def enqueue_except(self, from_conn, exclude_conn, msg):
        env = self.envelope(from_conn, msg)
        self.log.append((from_conn, exclude_conn, env))
        super().enqueue_except(from_conn, exclude_conn, env)


class SignedTransport(InMemoryTransport):
    """The production mode, declared. Session refuses to guess."""

    delivers_verified_envelopes = True


def signed_table(tmp_path, n=2, policy=None, tamper=None):
    """n wire-mode Sessions, each with its OWN identity and master secret.

    Joiners are admitted by completing the real handshake and authenticate
    the host against the key their invite pins -- no back door, because the
    point is that the composed path works, and a hole carved through
    admission would be a hole in the thing under test.
    """
    from holdem.p2p import admission as _adm
    from holdem.p2p import identity as _identity
    from tests import admission_helpers as AH

    clock = _DependencyClock()
    bus = SignedBus(clock, tamper=tamper)
    order = [f"peer{i}" for i in range(n)]

    # One identity file per peer: distinct Ed25519 keys, the production way.
    signers = {cid: _identity.load_or_create(tmp_path / f"{cid}.json")
               for cid in order}
    keys = {cid: signers[cid].public_key_bytes() for cid in order}
    assert len({bytes(k) for k in keys.values()}) == n, \
        "peers did not get distinct signing keys"

    host_key = keys["peer0"]
    sessions = {}
    for i, cid in enumerate(order):
        if i == 0:
            # No clock override: HostAdmission wants a CALLABLE and uses it
            # for challenge expiry only. The handshake below completes
            # immediately, so its default monotonic source is correct and
            # passing the FakeClock object would just raise.
            guard = dict(admission=_adm.HostAdmission(
                admission_secret=JH.TEST_ADMISSION_SECRET,
                host_pubkey=host_key))
        else:
            guard = dict(joiner_admission=JH.joiner_admission(
                host_pubkey=host_key, joiner_pubkey=keys[cid]))
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=SignedTransport(bus, cid),
                    clock=clock,
                    # Distinct per peer: a shared secret cannot show a
                    # composition defect in anything derived from it.
                    master_secret=bytes([i + 1]) * 32,
                    **guard)
        s.local_conn_id = cid
        bus.register(cid, s)
        bus.register_signer(cid, signers[cid])
        sessions[cid] = s

    host = sessions["peer0"]
    for cid in order[1:]:
        AH.admit(host._admission, cid, keys[cid],
                 JH.TEST_ADMISSION_SECRET, host_key)
        JH.authenticate_host(sessions[cid], "peer0", host_key)
        sessions[cid]._host_conn_id = "peer0"

    # The roster the host is authoritative for, carrying the key each peer
    # actually signs with -- what _bind_seat_keys freezes seats onto.
    for i, cid in enumerate(order):
        host.players[cid] = Player(conn_id=cid, peer_id=cid,
                                   nickname=f"P{i}", avatar_b64="",
                                   is_host=(i == 0),
                                   ed25519_pubkey_hex=keys[cid].hex())
    # The joiners learn those keys the way production joiners do: from the
    # host's roster broadcast. Without it their rosters carry no keys, and
    # _bind_seat_keys refuses the hand outright in wire mode rather than
    # starting one in which every seat is refused at message time.
    with host._owner:
        host._broadcast_player_list()
    bus.drain()

    host.start_game({
        Session.CONTINUATION_POLICY_SETTING: Session.CONTINUATION_END_TABLE,
        KEY: policy if policy is not None else BG,
    })
    bus.drain()
    for cid in order[1:]:
        roster = sessions[cid].players
        assert roster.get(cid) is not None and roster[cid].ed25519_pubkey_hex,             f"{cid} never received a roster carrying signing keys"
    return bus, sessions, order, keys


def test_the_signed_table_really_is_the_production_arrangement(tmp_path):
    """Guards everything below it.

    If this table quietly ran in compat mode, or left seat keys unbound,
    the hand below would prove no more than the existing one -- which is
    exactly how #181 happened.
    """
    bus, sessions, order, keys = signed_table(tmp_path, 3)
    for cid in order:
        s = sessions[cid]
        assert s.author_mode == AUTHOR_MODE_WIRE, f"{cid} is not in wire mode"
        assert s._deal_policy == BG
    host = sessions["peer0"]
    assert len({p.ed25519_pubkey_hex for p in host.players.values()}) == 3, \
        "the roster does not carry three distinct keys"


@pytest.mark.parametrize("n", [2, 3])
def test_full_prevention_hand_over_verified_envelopes(tmp_path, n):
    """The integration proof #181 asked for, on the production path.

    Convergence under prevention is only reachable if every round's proof
    was generated, signed, verified, decoded and checked by every peer. The
    difference from the compat version above is what carries the authority:
    here each peer signs with its OWN key and the seats are frozen onto
    those keys, so a message authorized by the delivering connection rather
    than by its author would be refused rather than silently accepted.
    """
    bus, sessions, order, keys = signed_table(tmp_path, n)
    # start_p2p_hand, not begin_hand. Every production caller uses it
    # (ice_room, lan_session, sidecar_launcher) and only it binds seat keys;
    # begin_hand says in its own comment that it exists for "tests and
    # harnesses". The compat integration this replaces uses begin_hand,
    # where an unbound map is legitimate -- a second reason it could not
    # see a composition defect in the keyed path.
    for cid in order:
        sessions[cid].start_p2p_hand(
            hand_no=1, names=[f"P{i}" for i in range(n)],
            stacks=[500] * n, sb=5, bb=10, button=0)
    bus.drain()

    decks = [[ct.to_hex() for ct in sessions[c]._deal_driver.deal.deck]
             for c in order]
    assert all(deck == decks[0] for deck in decks), "decks diverged"
    for cid in order:
        s = sessions[cid]
        deal = s._deal_driver.deal
        assert deal.prevention is True
        assert deal.abort_reason is None, f"{cid} aborted: {deal.abort_reason}"
        assert deal.is_shuffle_complete(), f"{cid} shuffle incomplete"
        assert deal.hole_complete(), f"{cid} holes incomplete"
        # Proof that the authorization above was author-based: seats are
        # bound, and bound to the keys the peers actually sign with.
        assert s._seat_keys, f"{cid} bound no seat keys"
        bound = {bytes.fromhex(v) if isinstance(v, str) else bytes(v)
                 for v in s._seat_keys.values()}
        assert bound == {bytes(keys[c]) for c in order}, (
            f"{cid} bound seats onto {bound!r}")


def _deal_hand(bus, sessions, order, n):
    for cid in order:
        sessions[cid].start_p2p_hand(
            hand_no=1, names=[f"P{i}" for i in range(n)],
            stacks=[500] * n, sb=5, bb=10, button=0)
    bus.drain()


def _forgery_fixture(tmp_path):
    """A dealt 3-seat signed table plus the pieces a forgery needs.

    Returns (bus, sessions, order, keys, target, replica, seat, owner,
    impostor) where ``seat`` is the real current actor, ``owner`` the peer
    whose key that seat is frozen onto, and ``impostor`` a third peer --
    never the target, whose own messages take the self-delivery exemption.
    """
    n = 3
    bus, sessions, order, keys = signed_table(tmp_path, n)
    _deal_hand(bus, sessions, order, n)
    target = sessions["peer0"]
    replica = target._replica
    seat = replica.actor
    assert seat is not None, "no actor to impersonate"
    owner = order[seat]
    impostor = next(cid for cid in order
                    if cid not in (owner, target.local_conn_id))
    return bus, sessions, order, keys, target, replica, seat, owner, impostor


def _call_envelope(bus, target, signer_cid, seat, replica):
    """A legitimate call for ``seat``, signed by ``signer_cid``.

    Every field is the correct one for the real actor -- sequence number,
    hand, action, and the next unused number in that seat's author stream
    -- so the ONLY thing a refusal can be about is who signed it.
    """
    seen = target._author_seq_seen.get((1, seat), {})
    return bus.envelope(signer_cid, {
        "type": "bet_action", "hand": 1, "seat": seat,
        "seq": replica.next_seq, "action": "call", "amount": 0,
        "author_seq": max(seen, default=AUTHOR_SEQ_START - 1) + 1})


def test_an_action_from_a_peer_that_does_not_own_the_seat_is_refused(tmp_path):
    """Seat authorship, reached deliberately.

    Delivered over the IMPOSTOR'S OWN connection and signed with the
    impostor's own key, so the admission binding between a connection and
    the key it was admitted with is satisfied -- and what is left to refuse
    the message is _author_owns_seat, which is the subject.

    A first version sent the forgery over the seat owner's connection
    instead. It passed, and it passed with _author_owns_seat removed, because
    the admission binding refused it first: "signed by <impostor> but that
    connection was admitted as <owner>". A control above the one under
    test, answering first -- the shape audit #125, #166, #190 and #172 all
    turned out to have.

    The positive control at the end is what makes the refusal mean
    something: the identical decision, signed by the key the seat is bound
    to, must be applied.
    """
    (bus, sessions, order, keys, target, replica,
     seat, owner, impostor) = _forgery_fixture(tmp_path)

    before = replica.state_digest()
    forged = _call_envelope(bus, target, impostor, seat, replica)
    assert forged["pubkey"] == keys[impostor].hex(), \
        "the bus did not sign with the impostor's key"
    target.handle_message(impostor, forged)
    assert replica.state_digest() == before, (
        f"{impostor} acted for seat {seat}, which is bound to {owner}")

    honest = _call_envelope(bus, target, owner, seat, replica)
    assert honest["pubkey"] == keys[owner].hex()
    target.handle_message(owner, honest)
    assert replica.state_digest() != before, (
        "the correctly signed control action was refused too, so the "
        "refusal above says nothing about authorship")


def test_an_envelope_signed_by_another_peer_is_refused_on_that_connection(
        tmp_path):
    """The control that masked the one above, pinned in its own right.

    Admission binds a connection to the key it was admitted with, so an
    envelope arriving over one peer's connection carrying another peer's
    signature is refused before any seat rule is consulted. Worth a test of
    its own precisely because it is strong enough to hide the seat rule's
    absence.
    """
    (bus, sessions, order, keys, target, replica,
     seat, owner, impostor) = _forgery_fixture(tmp_path)

    before = replica.state_digest()
    # Correct in every respect for the seat, signed by the impostor, but
    # delivered over the OWNER'S connection: signature and connection
    # disagree.
    crossed = _call_envelope(bus, target, impostor, seat, replica)
    target.handle_message(owner, crossed)
    assert replica.state_digest() == before, (
        f"an envelope signed by {impostor} was accepted on {owner}'s "
        f"connection")

    honest = _call_envelope(bus, target, owner, seat, replica)
    target.handle_message(owner, honest)
    assert replica.state_digest() != before, (
        "the matching control was refused too, so the refusal above says "
        "nothing about the connection binding")


def test_a_replayed_signed_deal_message_is_not_relayed_again(tmp_path):
    """Replay of REAL traffic, observed where the replica cannot hide it.

    Every envelope the hand produced is recorded, so this re-delivers one
    byte-for-byte: valid signature, valid author, a seat that really owns
    it -- everything except novelty.

    Asserted on the HOST'S RELAY, deliberately. A first version checked
    only that the deal state had not moved, and that passed with
    _sequence_ok removed: the deal state machine ignores a round it has
    already consumed, so it dedupes replays on its own and the gate's
    absence was invisible. This is the same reason audit #144's mutant was
    reported unreachable. _admit_hostless relays only AFTER the sequencing
    gate, so what the gate uniquely protects is the host declining to
    amplify a replay -- and that is observable on the bus.
    """
    n = 3
    bus, sessions, order, keys = signed_table(tmp_path, n)
    _deal_hand(bus, sessions, order, n)
    host = sessions["peer0"]
    assert host.is_host

    replayable = [(src, env) for src, _dst, env in bus.log
                  if env.get("type") == "deck_round" and src != "peer0"]
    assert replayable, "the hand produced no deck_round to replay"
    src, env = replayable[-1]

    deal = host._deal_driver.deal
    before_deal = ([ct.to_hex() for ct in deal.deck], deal.abort_reason,
                   deal.is_shuffle_complete())
    before_sent = len(bus.log)

    host.handle_message(src, dict(env))
    bus.drain()

    assert len(bus.log) == before_sent, (
        f"the host re-broadcast a replayed deal message "
        f"({len(bus.log) - before_sent} new envelopes)")
    after_deal = ([ct.to_hex() for ct in deal.deck], deal.abort_reason,
                  deal.is_shuffle_complete())
    assert after_deal == before_deal, "a replayed deal message moved the deal"


@pytest.mark.parametrize("how", ["dropped", "corrupted"])
def test_a_bad_shuffle_proof_aborts_the_hand(tmp_path, how):
    """A peer that signs a bad proof must abort the hand, not be believed.

    The tamper runs before signing, so what arrives is validly signed by
    its real author -- the prevention check is the only thing standing
    between a wrong shuffle and a dealt hand.
    """
    seen = []

    def tamper(from_conn, msg):
        if msg.get("type") != "deck_round" or "proof" not in msg:
            return msg
        if seen:
            return msg                      # only the first one
        seen.append(from_conn)
        bad = dict(msg)
        if how == "dropped":
            bad.pop("proof")
        else:
            raw = bad["proof"]
            # Flip one hex digit somewhere inside the encoded proof, which
            # leaves its shape intact -- so this exercises verification
            # rather than the decoder's length checks.
            blob = json.dumps(raw)
            cut = len(blob) // 2
            for i in range(cut, len(blob)):
                if blob[i] in "0123456789abcdef":
                    blob = blob[:i] + ("f" if blob[i] != "f" else "0") + blob[i + 1:]
                    break
            bad["proof"] = json.loads(blob)
        return bad

    n = 2
    bus, sessions, order, keys = signed_table(tmp_path, n, tamper=tamper)
    _deal_hand(bus, sessions, order, n)
    assert seen, "no deck_round carrying a proof crossed the bus"

    aborted = [cid for cid in order
               if sessions[cid]._deal_driver is not None
               and sessions[cid]._deal_driver.deal.abort_reason]
    assert aborted, (
        "a bad shuffle proof was accepted: "
        + repr({cid: (sessions[cid]._deal_driver.deal.abort_reason,
                      sessions[cid]._deal_driver.deal.is_shuffle_complete())
                for cid in order
                if sessions[cid]._deal_driver is not None}))
    reason = sessions[aborted[0]]._deal_driver.deal.abort_reason
    assert "proof" in reason, f"aborted for an unrelated reason: {reason}"
    for cid in order:
        driver = sessions[cid]._deal_driver
        if driver is not None and driver.deal.abort_reason:
            assert not driver.deal.hole_complete(), \
                f"{cid} dealt holes from an unproven shuffle"
