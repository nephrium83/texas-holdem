"""The peer-authored, host-relayed ingress pipeline: shape, not gameplay.

Behaviour is covered by test_author_identity, test_host_relay,
test_author_sequencing and test_three_peer_topology. What those cannot
catch is the trust boundary drifting back apart: authorization used to be
performed in three places that happened to agree, and a fourth copy would
have been just as green as the first three.

These tests assert the STRUCTURE the refactor established:

  normalize -> author/seat -> authorize ONCE -> sequence ONCE -> relay

plus the two facts that used to be implicit and are now declared.
"""
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.timeout import FakeClock as _DependencyClock
from holdem.p2p import inmemory_transport as _inmem
from holdem.p2p import tcp_transport as _tcp
from holdem.p2p import transport as _prod
from holdem.p2p.session import (
    AUTHOR_MODE_COMPAT, AUTHOR_MODE_WIRE, _HOSTLESS_PAYLOAD_TYPES, Player,
    Session,
)
from tests import joiner_helpers as JH

KEY_A = "aa" * 32
KEY_B = "bb" * 32


class _Undeclared:
    """A transport that says nothing about what it delivers."""

    def broadcast(self, msg):
        pass

    def send(self, to, msg):
        pass


class _Spy:
    """An explicitly declared compatibility transport."""

    delivers_verified_envelopes = False

    def __init__(self):
        self.sent = []

    def broadcast(self, msg):
        self.sent.append(msg)

    def broadcast_except(self, exclude, msg):
        self.sent.append(msg)

    def send(self, to, msg):
        self.sent.append(msg)


def _session(transport, mode=None):
    # Only a WIRE-mode joiner needs (and may only be built with) a pin; a
    # compat joiner given one would be gated on a handshake it never runs.
    wire = mode == AUTHOR_MODE_WIRE or (
        mode is None and getattr(transport, "delivers_verified_envelopes", None) is True)
    s = Session(is_host=False, nickname="n", avatar_b64="",
                transport=transport, author_mode=mode,
                joiner_admission=JH.joiner_admission() if wire else None,
                clock=_DependencyClock(),
                master_secret=bytes(range(32)))
    s.local_conn_id = "A"
    s._seat_order = ["A", "B", "C"]
    return s


# ── the representation boundary is declared, not guessed ──────────────────

def test_the_production_transport_declares_verified_envelopes():
    """Everything the real transport hands up has been through wire.unpack.

    Session reads this to choose AUTHOR_MODE_WIRE. If the declaration is
    ever dropped, production silently falls back to AUTHOR_MODE_COMPAT --
    which authorizes by delivering connection, i.e. trusts the relay host
    to speak for every seat it forwards. That is precisely the failure
    control N demonstrated, so it is pinned here rather than left to be
    noticed.
    """
    assert getattr(_prod, "delivers_verified_envelopes", False) is True, (
        "the production transport no longer declares verified envelopes; "
        "Session would drop to conn_id trust in production")
    assert Session(is_host=False, nickname="n", avatar_b64="",
                   transport=_prod, joiner_admission=JH.joiner_admission(),
                   clock=_DependencyClock(),
                   master_secret=bytes(range(32))).author_mode == AUTHOR_MODE_WIRE


@pytest.mark.parametrize("mod_or_cls, name", [
    (_inmem.InMemoryTransport, "InMemoryTransport"),
    (_tcp.SimpleTcpTransport, "SimpleTcpTransport"),
])
def test_unsigned_transports_declare_themselves_unverified(mod_or_cls, name):
    """The harness transports carry no signatures and must say so."""
    assert getattr(mod_or_cls, "delivers_verified_envelopes", None) is False, (
        f"{name} must declare delivers_verified_envelopes = False")


def test_an_undeclared_transport_is_refused_at_construction():
    """Silence is an error, never compatibility mode.

    This is the assertion that keeps the mode genuinely explicit. Defaulting
    an undeclared transport to compat would be the same implicit downgrade
    this refactor removed from _seat_author_ok, just relocated into
    capability detection: someone adds a transport, forgets one attribute,
    and the session concludes "no declaration -> compat -> trust the
    delivering connection". A missing declaration is not evidence that
    conn_id trust is safe; it is evidence that nobody has said.
    """
    with pytest.raises(TypeError, match="delivers_verified_envelopes"):
        _session(_Undeclared())


def test_a_non_bool_declaration_is_refused():
    """Truthiness is not a declaration -- "no" would read as wire mode."""
    class _Vague:
        delivers_verified_envelopes = "yes"

    with pytest.raises(TypeError, match="delivers_verified_envelopes"):
        _session(_Vague())


def test_an_undeclared_transport_is_usable_with_an_explicit_mode():
    """The escape hatch is stating the mode, not omitting the declaration."""
    s = _session(_Undeclared(), mode=AUTHOR_MODE_COMPAT)
    assert s.author_mode == AUTHOR_MODE_COMPAT


def test_an_unknown_mode_is_refused_at_construction():
    with pytest.raises(ValueError):
        _session(_Spy(), mode="whatever")


# ── the fallback is now a mode, and wire mode fails closed ────────────────

def test_wire_mode_refuses_a_remote_seat_when_no_keys_are_bound():
    """Production must not stand the delivering connection in for an author.

    With no bindings there is nothing to authorize against. The old code
    silently answered "is the conn_id the seat's owner?", which is the
    transport hop -- and under the star relay the hop is the HOST for every
    joiner-authored message.
    """
    s = _session(_Spy(), mode=AUTHOR_MODE_WIRE)
    assert s._seat_keys == {}, "precondition: nothing bound"
    assert s._author_owns_seat("B", KEY_B, 1) is False
    assert s._seat_author_ok("B", {"pubkey": KEY_B, "seat": 1}, 1) is False


def test_compat_mode_keeps_the_conn_id_rule():
    """The inverse, so the fail-closed test cannot pass by refusing always."""
    s = _session(_Spy(), mode=AUTHOR_MODE_COMPAT)
    assert s._author_owns_seat("B", None, 1) is True
    assert s._author_owns_seat("C", None, 1) is False, (
        "compat still binds a seat to its own connection")


def test_a_bound_key_outranks_the_mode_either_way():
    """Once bindings exist, both modes authorize by author and only by author."""
    for mode in (AUTHOR_MODE_WIRE, AUTHOR_MODE_COMPAT):
        s = _session(_Spy(), mode=mode)
        s.players["B"] = Player(conn_id="B", peer_id="", nickname="B",
                                avatar_b64="", ed25519_pubkey_hex=KEY_B)
        s._seat_keys = {1: KEY_B}
        assert s._author_owns_seat("A-the-host", KEY_B, 1) is True, (
            f"{mode}: a relayed message must authorize by author, not hop")
        assert s._author_owns_seat("B", KEY_A, 1) is False, (
            f"{mode}: the wrong key must be refused even from the right hop")


# ── structure: one gate, not three that agree ─────────────────────────────

def _src(fn):
    return inspect.getsource(fn)


def test_the_relay_does_not_authorize_independently():
    """_relay_if_host consumes an already-authorized context.

    A second lookup here would be a second copy of the rule, free to drift
    from the first. It also could not be reached with the same inputs: the
    relay sees the delivering conn_id, which is the wrong question.
    """
    src = _src(Session._relay_if_host)
    for forbidden in ("_seat_author_ok", "_author_owns_seat", "_seat_keys"):
        assert forbidden not in src, (
            f"_relay_if_host performs its own authorization ({forbidden}); "
            "it must consume the context _admit_hostless already authorized")


def test_the_sequence_gate_does_not_authorize_independently():
    """_sequence_ok must not re-decide authorship either."""
    src = _src(Session._sequence_ok)
    for forbidden in ("_seat_author_ok", "_author_owns_seat", "_seat_keys"):
        assert forbidden not in src, (
            f"_sequence_ok decides authorship ({forbidden}); authorship is "
            "settled once, by _admit_hostless, before sequencing")


def test_the_ingress_gate_is_the_only_authorizer_on_the_hostless_path():
    """Exactly one function on the ingress path calls the rule."""
    callers = [name for name, fn in vars(Session).items()
               if inspect.isfunction(fn) and "_author_owns_seat(" in _src(fn)
               and name not in ("_author_owns_seat",)]
    assert sorted(callers) == ["_admit_hostless", "_seat_author_ok"], (
        f"authorization is reached from {sorted(callers)}; it should be "
        "_admit_hostless (the ingress gate) plus _seat_author_ok (the "
        "compatibility shim the adversarial suites call directly)")


def test_the_typed_handlers_do_not_repeat_authorization():
    """Handlers receive an already-admitted body."""
    handlers = ["_on_deal_message", "_on_bet_action", "_on_hand_void",
                "_on_session_end", "_on_timeout_proposal", "_on_peer_lost",
                "_on_hand_settled"]
    for name in handlers:
        src = _src(getattr(Session, name))
        assert "_seat_author_ok" not in src and "_author_owns_seat" not in src, (
            f"{name} re-authorizes; ingress already did")


def test_there_is_one_explicit_hostless_type_registry():
    """Pinned as a literal, because a test parametrized over the registry
    cannot notice the registry shrinking -- it would simply run one case
    fewer and stay green."""
    assert _HOSTLESS_PAYLOAD_TYPES == frozenset({
        "key_announce", "deck_round", "deal_share", "audit_open",
        "bet_action", "hand_void", "session_end", "timeout_proposal",
        "peer_lost", "hand_settled",
        "casual_clock_fault", "casual_drop_observed", "casual_drop",
        "casual_drop_ack", "casual_drop_dispute",
    })


def test_the_dispatch_covers_every_registered_hostless_type():
    """Every registered type must REACH a handler -- observed at runtime.

    Two earlier versions of this test passed for the wrong reason, both
    times because they read the dispatch instead of exercising it:

      1. The first asserted only that the type's NAME appeared somewhere in
         the dispatch source. A comment satisfies that.
      2. The second walked the AST and required the matching branch to
         contain a call. ANY call satisfied that, so rewriting the
         bet_action branch to call print() left the test green while the
         handler stopped being invoked at all.

    No reading of the source can settle this, because what the test claims
    is that a message of each registered type arrives somewhere that
    handles it. So each destination gets a recorder and each type gets a
    message. A branch reduced to ``pass``, pointed at the wrong handler, or
    calling something other than its handler records nothing and fails.
    """
    from tests.test_session_hand import make_table

    bus, sessions, order = make_table(2)
    s = sessions["peer0"]
    fired = []

    def _recorder(name):
        def handler(*a, **k):
            fired.append(name)
        return handler

    # Every destination handle_message / _deliver_hand_message can route a
    # hostless type to. hasattr first: a renamed handler must fail here
    # rather than silently install a recorder nothing ever calls.
    for attr in ("_on_deal_message", "_on_bet_action", "_on_hand_void",
                 "_on_hand_settled", "_on_session_end",
                 "_on_timeout_proposal", "_on_peer_lost"):
        assert hasattr(s, attr), f"Session has no {attr}"
        setattr(s, attr, _recorder(attr))

    class _Continuation:
        """The casual_drop_* family dispatches through this collaborator."""

        departed = frozenset()
        pending = None
        carry = None

        def receive_observation(self, body):
            fired.append("receive_observation")

        def receive_drop(self, body):
            fired.append("receive_drop")

        def receive_ack(self, body):
            fired.append("receive_ack")

        def receive_dispute(self, body):
            fired.append("receive_dispute")

    s._casual_continuation = _Continuation()

    from holdem.p2p import casual_runtime
    original_clock_fault = casual_runtime.clock_fault
    casual_runtime.clock_fault = lambda session, body: fired.append(
        "clock_fault")
    try:
        undispatched = []
        for mtype in sorted(_HOSTLESS_PAYLOAD_TYPES):
            fired.clear()
            # Seatless and handless on purpose. In compat mode a body with
            # no seat clears _admit_hostless without a bound seat key, and
            # no "hand" keeps it out of the future-hand buffer -- so what
            # the message has to survive is the dispatch, which is the
            # subject, and not the authorization gate, which has its own
            # tests in test_author_identity.
            s.handle_message("peer1", {"type": mtype, "payload": {}})
            if not fired:
                undispatched.append(mtype)
        assert not undispatched, (
            f"registered but never reached a handler: {undispatched}")
    finally:
        casual_runtime.clock_fault = original_clock_fault


# ── narrow test-helper audit ──────────────────────────────────────────────

def test_the_topology_status_helper_returns_the_latest_state():
    """Scoped audit of a helper this refactor's area depends on.

    Twice now a helper in this code's tests reported something other than
    what it claimed to observe: _status polled with wait_for, which scans
    history from the beginning and returns the FIRST match, so every poll
    handed back the oldest snapshot. A host that had started its game
    normally looked permanently stuck in LOBBY.

    Not a sweep of every utility in the repository -- just proof that this
    one returns current state, on a stub whose history is unambiguous.
    """
    from tests import test_three_peer_topology as topo

    class _FakePeer:
        label = "A"
        stderr: list = []

        def __init__(self):
            self.events = [{"type": "status", "state": "LOBBY"}]

        def all_of(self, t):
            return [e for e in self.events if e.get("type") == t]

        def send(self, cmd):
            if cmd.get("op") == "status":
                self.events.append({"type": "status", "state": "PLAYING"})

        def wait_for(self, pred, timeout=None):
            for e in self.events:            # first match, as the real one does
                if pred(e):
                    return e
            return None

    got = topo._status(_FakePeer())
    assert got["state"] == "PLAYING", (
        "the helper returned a stale snapshot; it must report the state it "
        f"was asked for, got {got}")
