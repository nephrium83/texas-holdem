"""Seat authorization is by protocol AUTHOR, not by transport hop.

docs/AUDIT-M8-IDENTITY.md found that every seat-scoped check authorized on
``conn_id`` while the Ed25519 signature was verified and then used for
nothing. docs/TOPOLOGY_DECISION.md then showed why pinning a key to a
connection is the wrong repair: under the authenticated host relay, one
host connection legitimately carries messages authored by every other
seat.

So the model separates two things the code conflated:

    transport hop   = conn_id             -- who handed me these bytes
    protocol author = signing key -> seat -- who said it

These tests pin the second. The relay itself is NOT implemented yet; the
point of landing this first is that the relay is only safe once seat
authority no longer depends on which connection delivered a message.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p.session import Player, Session
    from holdem.p2p.events import EventSink
except RuntimeError as exc:                          # pragma: no cover
    pytest.skip(f"libsodium unavailable: {exc}", allow_module_level=True)


KEY_A = "aa" * 32          # host, seat 0
KEY_B = "bb" * 32          # seat 1
KEY_C = "cc" * 32          # seat 2
KEY_X = "ee" * 32          # a stranger's perfectly valid key


class _Spy:
    #: These suites hand the Session enveloped messages carrying a verified
    #: "pubkey" and bind seat keys, which is the production arrangement, so
    #: this double declares the production mode. Declared explicitly because
    #: Session refuses to guess: an undeclared transport raises rather than
    #: quietly selecting conn_id authorization.
    delivers_verified_envelopes = True

    def __init__(self):
        self.sent = []

    def broadcast(self, msg):
        self.sent.append(msg)

    def send(self, to, msg):
        self.sent.append(msg)

    def attach(self, s):
        pass

    def set_on_disconnect(self, cb):
        pass


class _Sink(EventSink):
    def emit(self, event):
        pass


def _table(local="A"):
    """Three seats, keys bound, as a peer would hold them mid-session."""
    s = Session(is_host=False, nickname="n", avatar_b64="",
                transport=_Spy(), sink=_Sink())
    s.local_conn_id = local
    s._host_conn_id = "A"
    s._seat_order = ["A", "B", "C"]
    for cid, key in (("A", KEY_A), ("B", KEY_B), ("C", KEY_C)):
        s.players[cid] = Player(conn_id=cid, peer_id=key[:16], nickname=cid,
                                avatar_b64="", ed25519_pubkey_hex=key)
    s._bind_seat_keys()
    assert s._seat_keys == {0: KEY_A, 1: KEY_B, 2: KEY_C}
    return s


def _env(author_key: str, seat: int, mtype="bet_action"):
    """An envelope as wire.unpack would hand it up: signature already
    verified against the pubkey the envelope names."""
    return {"type": mtype, "pubkey": author_key, "seat": seat,
            "hand": 1, "payload": {"seat": seat, "hand": 1}}


# ------------------------------------------------------ the relay property

def test_relayed_envelope_authenticates_by_author_not_by_hop():
    """THE relay-compatibility property.

    C receives a message B authored, delivered over C's connection to the
    HOST. The hop is the host; the author is B. Authorization must follow
    the author.

    A rule keyed on conn_id rejects this, which is exactly why the relay
    could not have been built on top of the previous model.
    """
    c = _table(local="C")
    relayed = _env(KEY_B, seat=1)
    assert c._seat_author_ok("A", relayed, 1) is True, (
        "a B-authored envelope relayed via the host's connection was "
        "refused -- seat authority is still following the hop")


def test_direct_delivery_still_authenticates():
    """The same message arriving directly from B must also pass, so the
    two delivery paths are indistinguishable to the verifier."""
    c = _table(local="C")
    assert c._seat_author_ok("B", _env(KEY_B, seat=1), 1) is True


# ------------------------------------------------------------- forgeries

def test_stranger_key_claiming_a_seat_is_refused():
    """A freshly generated, perfectly valid key is still not this seat.

    wire.unpack is self-certifying -- it verifies the signature against
    the key the envelope names -- so any attacker can produce a valid
    envelope. Validity alone must not confer a seat.
    """
    c = _table(local="C")
    assert c._seat_author_ok("A", _env(KEY_X, seat=1), 1) is False


def test_a_real_seat_cannot_speak_for_another_seat():
    """B's genuine key, claiming C's seat."""
    c = _table(local="A")
    assert c._seat_author_ok("B", _env(KEY_B, seat=2), 2) is False


def test_the_hop_cannot_launder_authority():
    """The host relaying does not let the HOST author for another seat."""
    c = _table(local="C")
    assert c._seat_author_ok("A", _env(KEY_A, seat=1), 1) is False, (
        "the delivering host authored for seat 1 and was accepted")


def test_out_of_range_seats_are_refused():
    c = _table(local="A")
    for seat in (-1, 3, 99):
        assert c._seat_author_ok("B", _env(KEY_B, seat=seat), seat) is False


# ------------------------------------------------------ binding immutability

def test_binding_is_frozen_once_established():
    """A seat cannot change signing key after binding.

    The roster is host-authoritative and mutable; the binding is not. A
    later roster edit -- from a buggy host, a hostile one, or a
    reconnection -- must not move a seat onto a different key.
    """
    s = _table(local="A")
    s.players["B"].ed25519_pubkey_hex = KEY_X       # roster rewritten
    s._bind_seat_keys()                             # attempt to rebind
    assert s._seat_keys[1] == KEY_B, "binding was mutable after establishment"
    assert s._seat_author_ok("B", _env(KEY_X, seat=1), 1) is False
    assert s._seat_author_ok("B", _env(KEY_B, seat=1), 1) is True


def test_seat_with_no_bound_key_is_refused_not_trusted():
    """An unresolvable seat fails closed -- by refusing the BINDING.

    This test used to assert that _bind_seat_keys froze the partial map
    and that seat 2 was then refused. The refusal was real, so the test
    passed, and the assertion looked like a security property. It was
    half of one. Because the map is authoritative and one-way, the
    frozen partial state also made seat 2 permanently unauthorizable for
    the rest of the session -- reachable with no attacker at all, by a
    peer dropping between start_game and start_p2p_hand (D1, issue #37).

    The invariant is unchanged: an unresolved seat is never trusted. The
    mechanism is stronger: we refuse to freeze at all, so the session
    fails before dealing rather than continuing with a dead seat.
    """
    s = Session(is_host=False, nickname="n", avatar_b64="",
                transport=_Spy(), sink=_Sink())
    s.local_conn_id = "A"
    s._seat_order = ["A", "B", "C"]
    s.players["A"] = Player(conn_id="A", peer_id="", nickname="A",
                            avatar_b64="", ed25519_pubkey_hex=KEY_A)
    s.players["B"] = Player(conn_id="B", peer_id="", nickname="B",
                            avatar_b64="", ed25519_pubkey_hex=KEY_B)
    # C joined without a key ever reaching us.
    s.players["C"] = Player(conn_id="C", peer_id="", nickname="C",
                            avatar_b64="")
    with pytest.raises(RuntimeError, match="incomplete"):
        s._bind_seat_keys()

    # Nothing was frozen, so a later complete attempt can still succeed.
    assert s._seat_keys == {}
    # And the original property still holds: with no bindings, wire mode
    # refuses the seat rather than falling back to the delivering conn.
    assert s._seat_author_ok("C", _env(KEY_C, seat=2), 2) is False


def test_self_delivery_is_exempt():
    """The local driver feeds its own emissions back with no envelope."""
    s = _table(local="B")
    assert s._seat_author_ok("B", {"type": "bet_action", "seat": 1}, 1) is True


# ------------------------------------------------- end to end through routing

def test_the_envelope_author_survives_payload_unwrapping():
    """Regression for a defect this work introduced and nearly shipped.

    handle_message unwraps hostless types through _hostless_body, which
    rebuilt the message from ``payload`` alone. The signing key lives on
    the ENVELOPE, so the unwrap dropped it and every remote hostless
    message became unauthorizable -- the protocol would have failed shut
    in production while the in-memory suite stayed green, because that
    harness carries no envelopes and takes the conn_id fallback.

    A payload-supplied pubkey must never win, either: only the envelope's
    key was verified by wire.unpack.
    """
    s = _table(local="A")
    body = s._hostless_body({"type": "bet_action", "pubkey": KEY_B,
                             "payload": {"seat": 1, "pubkey": KEY_X}})
    assert body["pubkey"] == KEY_B, "payload pubkey overrode the envelope's"

    unsigned = s._hostless_body({"type": "bet_action",
                                 "payload": {"seat": 1, "pubkey": KEY_X}})
    assert "pubkey" not in unsigned, "a payload forged an author"


def test_wrong_author_bet_action_is_dropped_before_the_replica():
    """Not just the helper: the ROUTED message must be refused.

    Delivered from the host's connection so the self-delivery exemption
    cannot mask the check.
    """
    s = _table(local="C")
    s._hand_no = 1
    reached = []

    class _Replica:
        def next_seq(self):
            return 0

        def apply_action(self, *a, **k):
            reached.append(a)
            return "applied"

    s._replica = _Replica()
    s.handle_message("A", {"type": "bet_action", "pubkey": KEY_X, "hand": 1,
                           "payload": {"seat": 1, "hand": 1, "action": "call",
                                       "amount": 0, "seq": 0}})
    assert not reached, "a stranger-authored bet_action reached the replica"


# --------------------------------------------------------- player_list gate

def test_non_host_player_list_is_inert():
    """Roster writes are hop-level authority: host connection only.

    Ungated, any peer could overwrite ed25519_pubkey_hex in the roster and
    so choose the key a victim would bind to a seat -- defeating author
    authentication before it started.
    """
    s = _table(local="C")
    hostile = {"type": "player_list", "pubkey": KEY_X, "payload": {"players": [
        {"conn_id": "B", "nickname": "pwned", "ed25519_pubkey_hex": KEY_X,
         "is_host": True},
        {"conn_id": "Z", "nickname": "ghost", "ed25519_pubkey_hex": KEY_X},
    ]}}
    s.handle_message("B", hostile)                      # B is not the host
    assert s.players["B"].ed25519_pubkey_hex == KEY_B, "roster was rewritten"
    assert s.players["B"].nickname == "B"
    assert "Z" not in s.players, "a ghost player was injected"


def test_host_player_list_still_applies():
    """The gate must not break the legitimate path."""
    s = _table(local="C")
    s.handle_message("A", {"type": "player_list", "pubkey": KEY_A,
                           "payload": {"players": [
                               {"conn_id": "B", "nickname": "Bee",
                                "ed25519_pubkey_hex": KEY_B}]}})
    assert s.players["B"].nickname == "Bee"
