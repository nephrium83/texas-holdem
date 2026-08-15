"""Pins the table-wide deal policy at the session layer.

The deal policy is a property of the TABLE, not of a peer. It rides in the
game_start table_settings every peer already receives, so peers reach the
same policy without negotiating and without a new message type.

There is deliberately NO default. The previous arrangement -- a boolean
whose absence meant detection-only -- is how every shipped game came to run
without shuffle proofs while the Bayer-Groth implementation sat complete
and unreferenced. A table that does not say how it deals is refused.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
    from holdem.p2p.session import (
        AUTHOR_MODE_COMPAT, AUTHOR_MODE_WIRE, Player, Session,
    )
except RuntimeError as exc:
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
                    transport=InMemoryTransport(bus, cid))
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
    host.start_game(dict(settings if settings is not None else {KEY: BG}))
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
                transport=_WireTransport())
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
        make_table(2, settings={KEY: value})


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
        make_table(2, settings={KEY: value})


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
        {KEY: Sneaky()}, AUTHOR_MODE_COMPAT) is None
    with pytest.raises(ValueError, match="deal_policy"):
        make_table(2, settings={KEY: Sneaky()})


# --------------------------------------------------- wire mode mandates BG

def test_wire_mode_refuses_detection_only():
    """The mandate. Detection-only is a legitimate, explicit mode for
    harnesses and benchmarks; on a transport carrying real envelopes it is
    refused, because "we will notice afterwards that someone cheated" is
    not a property this protocol will advertise as trustless."""
    assert Session.parse_deal_policy({KEY: DETECTION}, AUTHOR_MODE_WIRE) is None


def test_wire_mode_accepts_bayer_groth():
    assert Session.parse_deal_policy({KEY: BG}, AUTHOR_MODE_WIRE) == BG


def test_compat_mode_accepts_detection_only_explicitly():
    assert Session.parse_deal_policy(
        {KEY: DETECTION}, AUTHOR_MODE_COMPAT) == DETECTION
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
        "seat_order": ["host", "me"], "table_settings": {KEY: DETECTION}}})
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
        "seat_order": ["host", "me"], "table_settings": {KEY: DETECTION}}})
    assert fired == [], "a refused table still announced a game start"


def test_a_refused_table_records_what_it_refused():
    """POLICY_REFUSED must be diagnosable. session_id is a digest now, so
    the record carries the policy explicitly or the cause is unrecoverable."""
    s = _wire_joiner()
    s.handle_message("host", {"type": "game_start", "payload": {
        "seat_order": ["host", "me"], "table_settings": {KEY: DETECTION}}})
    assert DETECTION in s.terminal_reason


# --------------------------------------------------------- propagation

def test_policy_propagates_to_every_peer():
    sessions, _ = make_table(3, settings={KEY: BG})
    for conn_id, s in sessions.items():
        assert s.deal_policy == BG, f"{conn_id} missed the table policy"
        assert s.prevention is True


def test_host_and_peers_agree_without_negotiating():
    """No peer announces a policy; they all read the same broadcast."""
    on = make_table(4, settings={KEY: BG})[0]
    off = make_table(4, settings={KEY: DETECTION})[0]
    assert len({s.deal_policy for s in on.values()}) == 1
    assert len({s.deal_policy for s in off.values()}) == 1
    assert next(iter(on.values())).prevention is True
    assert next(iter(off.values())).prevention is False


# ------------------------------------------------------- the chokepoint

def test_adopting_the_same_policy_twice_is_idempotent():
    """Retries and relay echoes must stay harmless."""
    sessions, _ = make_table(2, settings={KEY: BG})
    peer = sessions["peer1"]
    assert peer._adopt_deal_policy(BG) is True
    assert peer.deal_policy == BG


def test_changing_the_policy_is_refused_by_the_chokepoint():
    """None -> A adopts, A -> A is idempotent, A -> B is refused. Asserted
    on the writer itself, not on a handler that happens to call it."""
    sessions, _ = make_table(2, settings={KEY: BG})
    peer = sessions["peer1"]
    assert peer._adopt_deal_policy(DETECTION) is False
    assert peer.deal_policy == BG, "a refused adoption still wrote the field"


def test_a_second_game_start_cannot_change_the_policy():
    """Capabilities freeze once play begins."""
    sessions, _ = make_table(2, settings={KEY: BG})
    peer = sessions["peer1"]
    peer._on_game_start("peer0", {"payload": {
        "seat_order": ["peer0", "peer1"],
        "table_settings": {KEY: DETECTION}}})
    assert peer.deal_policy == BG
    assert peer.prevention is True


def test_a_malformed_second_game_start_does_not_raise():
    """The freeze branch is a reject path reached from a message handler.
    Raising there would carry a hostile or stale game_start out of
    handle_message and onto the transport's dispatch thread."""
    sessions, _ = make_table(2, settings={KEY: BG})
    peer = sessions["peer1"]
    peer._on_game_start("peer0", {"payload": {
        "seat_order": ["peer0", "peer1"], "table_settings": {KEY: 12345}}})
    assert peer.deal_policy == BG


def test_a_fresh_session_does_not_inherit_a_previous_policy():
    a, _ = make_table(2, settings={KEY: DETECTION})
    b, _ = make_table(2, settings={KEY: BG})
    assert all(s.deal_policy == DETECTION for s in a.values())
    assert all(s.deal_policy == BG for s in b.values())


# ------------------------------------------------------- driver wiring

def test_driver_receives_the_table_policy():
    """The single mapping point from policy string to the deal layer's
    bool. This is the bridge that a downgrade would cut."""
    for policy, expected in ((DETECTION, False), (BG, True)):
        sessions, _ = make_table(2, settings={KEY: policy})
        for s in sessions.values():
            s.begin_hand(hand_no=1, button=0)
            assert s._deal_driver.deal.prevention is expected


def test_detection_only_hand_emits_no_proof():
    sessions, _ = make_table(2, settings={KEY: DETECTION})
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
                transport=InMemoryTransport(bus, "peer0"))
    s.local_conn_id = "peer0"
    s.configure_seats(["peer0", "peer1"])
    with pytest.raises(RuntimeError, match="no deal policy"):
        s.begin_hand(hand_no=1, button=0)


# --------------------------------------------------- deal context binding

def test_the_deal_context_binds_the_policy():
    """"bayer-groth-v1" is a context commitment, not decorative metadata:
    two tables identical but for their policy must not share a deal
    domain."""
    bg, _ = make_table(2, settings={KEY: BG})
    det, _ = make_table(2, settings={KEY: DETECTION})
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
                    transport=InMemoryTransport(bus, "x"))
        s._deal_policy = BG
        s._seat_order = order
        return s._deal_context_bytes()

    assert ctx(["a|b", "c"]) != ctx(["a", "b|c"])


def test_the_deal_context_refuses_non_string_seat_ids():
    """Fail closed, matching the old '|'.join behaviour. Coercing with
    str() would trade a loud failure for an injectivity hole."""
    bus = InMemoryBus()
    s = Session(is_host=True, nickname="P", avatar_b64="",
                transport=InMemoryTransport(bus, "x"))
    s._deal_policy = BG
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
                    transport=InMemoryTransport(bus, cid))
        s.local_conn_id = cid
        s.configure_seats(list(order))
        assert s._adopt_deal_policy(policy)
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
    sessions, _ = make_table(2, settings={KEY: BG})
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
    sessions, _ = make_table(2, settings={KEY: BG})
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
                transport=InMemoryTransport(bus, "peer0"))
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
                transport=InMemoryTransport(bus, "peer0"))
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
                transport=InMemoryTransport(bus, "peer0"))
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
                transport=InMemoryTransport(bus, "x"))
    s._seat_order = ["a", "b"]
    assert s.deal_policy is None
    with pytest.raises(RuntimeError, match="before a policy is adopted"):
        s._deal_context_bytes()
