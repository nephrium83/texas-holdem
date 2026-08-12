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

from holdem.p2p import inmemory_transport as _inmem
from holdem.p2p import tcp_transport as _tcp
from holdem.p2p import transport as _prod
from holdem.p2p.session import (
    AUTHOR_MODE_COMPAT, AUTHOR_MODE_WIRE, _HOSTLESS_PAYLOAD_TYPES, Player,
    Session,
)

KEY_A = "aa" * 32
KEY_B = "bb" * 32


class _Spy:
    """A transport that declares nothing, to exercise the default."""

    def __init__(self):
        self.sent = []

    def broadcast(self, msg):
        self.sent.append(msg)

    def broadcast_except(self, exclude, msg):
        self.sent.append(msg)

    def send(self, to, msg):
        self.sent.append(msg)


def _session(transport, mode=None):
    s = Session(is_host=False, nickname="n", avatar_b64="",
                transport=transport, author_mode=mode)
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
                   transport=_prod).author_mode == AUTHOR_MODE_WIRE


@pytest.mark.parametrize("mod_or_cls, name", [
    (_inmem.InMemoryTransport, "InMemoryTransport"),
    (_tcp.SimpleTcpTransport, "SimpleTcpTransport"),
])
def test_unsigned_transports_declare_themselves_unverified(mod_or_cls, name):
    """The harness transports carry no signatures and must say so."""
    assert getattr(mod_or_cls, "delivers_verified_envelopes", None) is False, (
        f"{name} must declare delivers_verified_envelopes = False")


def test_an_undeclared_transport_gets_the_compatibility_rule():
    """The default is compat, and that is a deliberate, bounded choice.

    A harness that forgot to declare would otherwise fail closed in a way
    that reads as a protocol bug. What protects production is the explicit
    declaration on the real transport, asserted above -- not this default.
    """
    assert _session(_Spy()).author_mode == AUTHOR_MODE_COMPAT


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
                "_on_session_end", "_on_timeout_proposal"]
    for name in handlers:
        src = _src(getattr(Session, name))
        assert "_seat_author_ok" not in src and "_author_owns_seat" not in src, (
            f"{name} re-authorizes; ingress already did")


def test_there_is_one_hostless_type_registry_of_exactly_eight():
    """Pinned as a literal, because a test parametrized over the registry
    cannot notice the registry shrinking -- it would simply run one case
    fewer and stay green."""
    assert _HOSTLESS_PAYLOAD_TYPES == frozenset({
        "key_announce", "deck_round", "deal_share", "audit_open",
        "bet_action", "hand_void", "session_end", "timeout_proposal",
    })


def test_the_dispatch_covers_every_registered_hostless_type():
    """No type may be registered and then never dispatched, or vice versa."""
    src = _src(Session.handle_message)
    for mtype in _HOSTLESS_PAYLOAD_TYPES:
        assert f'"{mtype}"' in src, (
            f"{mtype} is in the registry but never dispatched")


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
