"""Authenticated admission: the attacks, not the happy path.

Three DISTINCT guarantees live here, and they are named separately on
purpose. Calling all three "authentication" is how one of them gets removed
later on the grounds that another still exists:

HOST AUTHENTICITY
    The peer the joiner talks to holds the exact 32-byte Ed25519 key
    carried in the V2 invite. Not a prefix of it, and not "whoever answered
    the socket first".

CAPABILITY ADMISSION
    The joiner holds the private admission secret from the invite, proved
    by HMAC over a transcript rather than by transmitting it.

CONNECTION IDENTITY CONTINUITY
    The Ed25519 key that completed admission stays the sole author on that
    host-side connection. Admitting K1 and then accepting traffic authored
    by K2 would make the transcript's joiner binding decorative.

Explicitly NOT a guarantee here: Sybil resistance. Everyone invited holds
the same admission secret, so it proves "has the invitation", never "is
entitled to exactly one seat". A holder can mint any number of Ed25519
identities and pass this handshake once per identity. That is a policy
problem, and no test below should be read as covering it.

Also not a guarantee: confidentiality. This authenticates peers and a
capability; it does not encrypt the transport. A transparent MITM can still
relay, observe metadata, delay and suppress authenticated traffic. What it
cannot do is become the pinned host or alter a signed message.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p import admission as adm
from holdem.p2p import invite as inv
from holdem.p2p.session import (
    AUTHOR_MODE_COMPAT, AUTHOR_MODE_WIRE, Session,
)
from holdem.p2p.timeout import FakeClock

HOST_KEY   = bytes(range(32))
K1         = bytes([1]) * 32          # the joiner that authenticates
K2         = bytes([2]) * 32          # a different identity
SECRET     = bytes(range(16))
OTHER_SEC  = bytes([9]) * 16
TOKEN      = bytes([7]) * 8


class _Spy:
    """Verified-envelope transport double; records what was sent."""

    delivers_verified_envelopes = True

    def __init__(self):
        self.sent = []

    def broadcast(self, msg):
        self.sent.append(msg)

    def broadcast_except(self, exclude, msg):
        self.sent.append(msg)

    def send(self, to, msg):
        self.sent.append(msg)


def _host_adm(clock=None, ttl=adm.CHALLENGE_TTL_SECONDS):
    return adm.HostAdmission(admission_secret=SECRET, host_pubkey=HOST_KEY,
                             discovery_token=TOKEN, clock=clock, ttl=ttl)


def _joiner_adm(secret=SECRET, host=HOST_KEY, me=K1, token=TOKEN):
    return adm.JoinerAdmission(admission_secret=secret, host_pubkey=host,
                               joiner_pubkey=me, discovery_token=token)


def _mac_for(client_nonce, server_nonce, secret=SECRET, host=HOST_KEY,
             joiner=K1, token=TOKEN):
    return adm.compute_mac(secret, adm.transcript(
        token, host, joiner, client_nonce, server_nonce))


def _full_exchange(host_admission, conn_id="c1", joiner=K1, secret=SECRET):
    """hello -> challenge -> response. Returns (ok, client_n, server_n)."""
    cn = adm.new_nonce()
    ch = host_admission.on_hello(conn_id, joiner, cn)
    if ch is None:
        return False, cn, None
    sn = bytes.fromhex(ch["server_nonce"])
    ok = host_admission.on_response(
        conn_id, joiner, cn, sn,
        _mac_for(cn, sn, secret=secret, joiner=joiner))
    return ok, cn, sn


# ══════════════════════════════════════ CAPABILITY ADMISSION (host side)

def test_the_correct_secret_admits():
    """Baseline, so every rejection below cannot pass for the wrong reason."""
    h = _host_adm()
    ok, _, _ = _full_exchange(h)
    assert ok is True
    assert h.is_admitted("c1")
    assert h.admitted_key("c1") == K1


def test_a_wrong_admission_secret_is_refused():
    h = _host_adm()
    ok, _, _ = _full_exchange(h, secret=OTHER_SEC)
    assert ok is False
    assert not h.is_admitted("c1")


def test_a_connection_with_no_capability_at_all_is_never_admitted():
    """An arbitrary TCP peer that simply never handshakes."""
    h = _host_adm()
    assert not h.is_admitted("stranger")
    assert h.admitted_key("stranger") is None


def test_a_replayed_response_from_an_earlier_connection_fails():
    """The captured bytes are valid -- for a transcript that is finished."""
    h = _host_adm()
    cn = adm.new_nonce()
    sn = bytes.fromhex(h.on_hello("c1", K1, cn)["server_nonce"])
    mac = _mac_for(cn, sn)
    assert h.on_response("c1", K1, cn, sn, mac) is True

    h.forget("c1")                       # peer disconnects
    # Reconnect, new challenge, then replay the OLD response verbatim.
    cn2 = adm.new_nonce()
    sn2 = bytes.fromhex(h.on_hello("c1", K1, cn2)["server_nonce"])
    assert h.on_response("c1", K1, cn, sn, mac) is False
    assert not h.is_admitted("c1")
    assert sn2 != sn, "server nonce must be fresh per exchange"


def test_a_response_signed_by_a_different_joiner_key_fails():
    """K2 cannot answer the challenge K1 asked for."""
    h = _host_adm()
    cn = adm.new_nonce()
    sn = bytes.fromhex(h.on_hello("c1", K1, cn)["server_nonce"])
    assert h.on_response("c1", K2, cn, sn,
                         _mac_for(cn, sn, joiner=K2)) is False
    assert not h.is_admitted("c1")


def test_a_capability_holder_cannot_hijack_another_peers_handshake():
    """The attack the sender check actually exists for.

    The weak version of this test signs as K2 AND macs as K2, which fails
    on the MAC alone -- so it passes with the sender check removed, and a
    control proved exactly that.

    The real adversary is an INSIDER: someone who holds the invite (so can
    compute any MAC) and wants to answer a handshake a different peer
    started. It signs as K2 but macs over K1's transcript, which is the
    transcript the host will check against. Only the sender comparison
    stops it -- and without that stop the connection is admitted as K1
    while K2 is the peer actually holding it, which is precisely the
    identity confusion the binding exists to prevent.
    """
    h = _host_adm()
    cn = adm.new_nonce()
    sn = bytes.fromhex(h.on_hello("c1", K1, cn)["server_nonce"])

    hijack_mac = _mac_for(cn, sn, joiner=K1)     # valid for K1's transcript
    assert h.on_response("c1", K2, cn, sn, hijack_mac) is False, (
        "K2 answered a handshake K1 started, using a MAC over K1's "
        "transcript -- an invite holder can always compute that MAC")
    assert not h.is_admitted("c1")
    assert h.admitted_key("c1") is None, (
        "the connection was admitted under an identity that is not the "
        "peer holding it")


@pytest.mark.parametrize("field", ["client_nonce", "server_nonce"])
def test_an_altered_nonce_fails(field):
    h = _host_adm()
    cn = adm.new_nonce()
    sn = bytes.fromhex(h.on_hello("c1", K1, cn)["server_nonce"])
    mac = _mac_for(cn, sn)
    bad = adm.new_nonce()
    ok = h.on_response("c1", K1,
                       bad if field == "client_nonce" else cn,
                       bad if field == "server_nonce" else sn, mac)
    assert ok is False


def test_a_transcript_from_a_different_lobby_fails():
    """Context binding: same keys, same nonces, different discovery token."""
    h = _host_adm()
    cn = adm.new_nonce()
    sn = bytes.fromhex(h.on_hello("c1", K1, cn)["server_nonce"])
    other_token = bytes([8]) * 8
    assert h.on_response("c1", K1, cn, sn,
                         _mac_for(cn, sn, token=other_token)) is False


def test_an_expired_challenge_fails_deterministically():
    clk = FakeClock()
    h = _host_adm(clock=clk.monotonic, ttl=30.0)
    cn = adm.new_nonce()
    sn = bytes.fromhex(h.on_hello("c1", K1, cn)["server_nonce"])
    clk.advance(30.0)                    # exactly at the boundary
    assert h.on_response("c1", K1, cn, sn, _mac_for(cn, sn)) is False
    assert not h.is_admitted("c1")


def test_a_challenge_just_inside_the_ttl_still_works():
    """The inverse, so the expiry test cannot pass by always refusing."""
    clk = FakeClock()
    h = _host_adm(clock=clk.monotonic, ttl=30.0)
    cn = adm.new_nonce()
    sn = bytes.fromhex(h.on_hello("c1", K1, cn)["server_nonce"])
    clk.advance(29.0)
    assert h.on_response("c1", K1, cn, sn, _mac_for(cn, sn)) is True


def test_one_challenge_buys_exactly_one_attempt():
    """A wrong answer consumes it; no grinding against a live challenge."""
    h = _host_adm()
    cn = adm.new_nonce()
    sn = bytes.fromhex(h.on_hello("c1", K1, cn)["server_nonce"])
    assert h.on_response("c1", K1, cn, sn, b"\x00" * 32) is False
    assert h.on_response("c1", K1, cn, sn, _mac_for(cn, sn)) is False, (
        "the correct MAC was accepted against an already-consumed challenge")


def test_repeated_hello_cannot_grow_pending_state_without_bound():
    h = _host_adm()
    for _ in range(50):
        h.on_hello("c1", K1, adm.new_nonce())
    assert len(h._pending) == 1, (
        f"{len(h._pending)} live challenges on one connection; each would be "
        "independently answerable")


def test_disconnect_clears_pending_and_admitted_state():
    h = _host_adm()
    _full_exchange(h)
    h.on_hello("c2", K2, adm.new_nonce())
    h.forget("c1")
    h.forget("c2")
    assert not h.is_admitted("c1")
    assert h.admitted_key("c1") is None
    assert h._pending == {}


def test_reconnect_requires_a_fresh_challenge():
    h = _host_adm()
    ok, cn, sn = _full_exchange(h)
    assert ok
    h.forget("c1")
    assert not h.is_admitted("c1")
    ok2, cn2, sn2 = _full_exchange(h)
    assert ok2 and sn2 != sn


# ══════════════════════════════ CONNECTION IDENTITY CONTINUITY (host side)

def test_an_admitted_connection_cannot_re_handshake_as_another_key():
    """K1 is admitted on c1; K2 must not be able to take the connection.

    Otherwise the K1 binding sits next to a protocol-supported key-change
    hatch. Authenticating again is legitimate -- on a NEW connection, after
    a disconnect has cleared this one.
    """
    h = _host_adm()
    assert _full_exchange(h)[0] is True
    assert h.admitted_key("c1") == K1

    assert h.on_hello("c1", K2, adm.new_nonce()) is None, (
        "the host issued a challenge on an already-admitted connection")
    assert h.admitted_key("c1") == K1, "K2 replaced K1 on a live connection"


def test_the_same_key_may_authenticate_again_on_a_different_connection():
    """The inverse: immutability is per connection, not per key."""
    h = _host_adm()
    assert _full_exchange(h, conn_id="c1")[0] is True
    assert _full_exchange(h, conn_id="c2")[0] is True
    assert h.admitted_key("c1") == h.admitted_key("c2") == K1


# ═══════════════════════════════════════ HOST AUTHENTICITY (joiner side)

def test_a_challenge_from_the_pinned_host_produces_a_response():
    j = _joiner_adm()
    hello = j.hello_payload()
    cn = bytes.fromhex(hello["client_nonce"])
    resp = j.on_challenge(HOST_KEY, cn, adm.new_nonce())
    assert resp is not None and "mac" in resp
    assert j.verified_host is True


def test_a_challenge_from_the_wrong_host_key_yields_no_mac():
    """Correct secret, wrong host: the joiner must not even answer.

    The MAC is bound to the pinned host key and so would be useless to an
    impostor anyway, but there is no reason to hand one over, and stopping
    here is what keeps identity unsent.
    """
    j = _joiner_adm()
    cn = bytes.fromhex(j.hello_payload()["client_nonce"])
    assert j.on_challenge(K2, cn, adm.new_nonce()) is None
    assert j.verified_host is False


def test_a_key_sharing_the_old_eight_byte_prefix_is_still_refused():
    """The reason V2 pins 32 bytes instead of V1's 8.

    V1 verified the host with startswith() on a truncated hex prefix, i.e.
    a 64-bit target -- findable, and the exact thing an impostor would aim
    at. A test whose "wrong key" differs in byte 0 cannot tell a 32-byte
    comparison from an 8-byte one, and a control that truncated the check
    fired nothing until this existed.

    This key agrees with the pinned host for the whole of V1's window and
    disagrees immediately after.
    """
    near_miss = HOST_KEY[:8] + bytes([0xFF]) * 24
    assert near_miss[:8] == HOST_KEY[:8], "precondition: prefixes collide"
    assert near_miss != HOST_KEY

    j = _joiner_adm()
    cn = bytes.fromhex(j.hello_payload()["client_nonce"])
    assert j.on_challenge(near_miss, cn, adm.new_nonce()) is None, (
        "a key matching only the first 8 bytes was accepted as the host")
    assert j.verified_host is False


def test_a_near_miss_key_cannot_complete_the_accept_either():
    """Same collision, checked on the second host-authenticity gate."""
    near_miss = HOST_KEY[:8] + bytes([0xFF]) * 24
    j = _joiner_adm()
    cn = bytes.fromhex(j.hello_payload()["client_nonce"])
    sn = adm.new_nonce()
    assert j.on_challenge(HOST_KEY, cn, sn) is not None
    assert j.on_accept(near_miss, cn, sn) is False


def test_a_challenge_echoing_the_wrong_client_nonce_is_refused():
    j = _joiner_adm()
    j.hello_payload()
    assert j.on_challenge(HOST_KEY, adm.new_nonce(), adm.new_nonce()) is None
    assert j.verified_host is False


def test_a_challenge_before_any_hello_is_refused():
    j = _joiner_adm()
    assert j.on_challenge(HOST_KEY, adm.new_nonce(), adm.new_nonce()) is None


@pytest.mark.parametrize("bad_nonce", [b"", b"\x01" * 8, b"\x01" * 33])
def test_a_malformed_server_nonce_is_refused_before_any_transcript(bad_nonce):
    j = _joiner_adm()
    cn = bytes.fromhex(j.hello_payload()["client_nonce"])
    assert j.on_challenge(HOST_KEY, cn, bad_nonce) is None


def test_accept_completes_only_for_the_transcript_that_was_challenged():
    j = _joiner_adm()
    cn = bytes.fromhex(j.hello_payload()["client_nonce"])
    sn = adm.new_nonce()
    assert j.on_challenge(HOST_KEY, cn, sn) is not None

    assert j.on_accept(HOST_KEY, cn, adm.new_nonce()) is False, (
        "an accept for a different server nonce completed admission")
    assert j.on_accept(HOST_KEY, adm.new_nonce(), sn) is False
    assert j.on_accept(K2, cn, sn) is False, "accept from an unpinned key"
    assert j.on_accept(HOST_KEY, cn, sn) is True


def test_accept_without_a_prior_challenge_is_refused():
    """A random signed 'accept' from the real host completes nothing."""
    j = _joiner_adm()
    j.hello_payload()
    assert j.on_accept(HOST_KEY, adm.new_nonce(), adm.new_nonce()) is False


# ════════════════════════════════ THE SESSION PERIMETER (joiner pre-auth)

def _joiner_session():
    s = Session(is_host=False, nickname="J", avatar_b64="",
                transport=_Spy(), joiner_admission=_joiner_adm())
    s.local_conn_id = "me"
    return s


def _perimeter(s):
    """Everything a pre-auth message must not be able to move."""
    return {
        "players":        sorted(s.players),
        "join_order":     list(s._join_order),
        "peer_last_hash": dict(s._peer_last_hash),
        "host_conn_id":   s._host_conn_id,
        "local_conn_id":  s.local_conn_id,
        "state":          s.state,
        "seat_order":     list(s._seat_order),
    }


PRE_AUTH_FORGERIES = [
    ("player_ack",  {"payload": {"your_conn_id": "victim"}}),
    ("player_list", {"payload": {"players": [{"conn_id": "x",
                                              "nickname": "x"}]}}),
    ("game_start",  {"payload": {"table_settings": {},
                                 "seat_order": ["x", "y"]}}),
    ("player_info", {"payload": {"nickname": "x"}}),
    ("chat",        {"payload": {"nickname": "x", "text": "hi"}}),
    ("key_announce", {"payload": {"seat": 0, "hand": 1}}),
]


@pytest.mark.parametrize("mtype,extra", PRE_AUTH_FORGERIES,
                         ids=[m for m, _ in PRE_AUTH_FORGERIES])
def test_pre_auth_traffic_is_inert_and_moves_nothing(mtype, extra):
    """The perimeter, snapshotted.

    Asserted as a whole-state comparison rather than one handler at a time,
    because the gate's claim is that NOTHING gets through -- and a
    per-handler test only ever proves something about the handlers someone
    remembered to name. _peer_last_hash is in the snapshot deliberately:
    the gate sits ahead of the hash-chain bookkeeping precisely so an
    unadmitted peer cannot seed per-peer state.
    """
    s = _joiner_session()
    before = _perimeter(s)
    msg = {"type": mtype, "pubkey": K2.hex(), "ts": 1, "prev": "0" * 64,
           "sig": "ff" * 64, "hash": "ab" * 32}
    msg.update(extra)
    s.handle_message("impostor", msg)
    assert _perimeter(s) == before, (
        f"a pre-auth {mtype} moved session state before host authentication")


def test_a_forged_player_ack_cannot_set_the_host_hop():
    """The specific defect this replaced: first speaker became the host."""
    s = _joiner_session()
    s.handle_message("impostor", {
        "type": "player_ack", "pubkey": K2.hex(),
        "payload": {"your_conn_id": "victim"}})
    assert s._host_conn_id == "", "an unauthenticated peer became the host"
    assert s.local_conn_id == "me", "and it renamed us on the way past"


def test_only_mark_host_authenticated_opens_the_perimeter():
    """A verified CHALLENGE is not enough -- the accept must land.

    A valid challenge proves the peer holds the invited host's key. It does
    NOT prove the host accepted this joiner's capability proof, and until
    the accept validates the same transcript the joiner must stay closed:
    no player_info, no host hop, nothing but admission_* admitted.
    """
    s = _joiner_session()
    j = s._joiner_admission
    cn = bytes.fromhex(j.hello_payload()["client_nonce"])
    sn = adm.new_nonce()
    assert j.on_challenge(HOST_KEY, cn, sn) is not None, "challenge is valid"

    # Verified host key, no accept yet: still closed.
    assert s._host_authenticated is False
    before = _perimeter(s)
    s.handle_message("host", {"type": "player_ack", "pubkey": HOST_KEY.hex(),
                              "payload": {"your_conn_id": "seat-me"}})
    assert _perimeter(s) == before, (
        "a challenge alone opened the session before the accept arrived")

    # A forged accept must not open it either.
    assert j.on_accept(K2, cn, sn) is False
    assert s._host_authenticated is False

    # The real accept does.
    assert j.on_accept(HOST_KEY, cn, sn) is True
    s.mark_host_authenticated("host")
    assert s._host_authenticated is True
    assert s._host_conn_id == "host"


def test_after_authentication_player_ack_assigns_our_id():
    """The legitimate use, once the hop is authenticated."""
    s = _joiner_session()
    s.mark_host_authenticated("host")
    s.handle_message("host", {"type": "player_ack", "pubkey": HOST_KEY.hex(),
                              "payload": {"your_conn_id": "seat-me"}})
    assert s.local_conn_id == "seat-me"


def test_later_admission_traffic_cannot_repoint_the_host_hop():
    """Authentication is not re-openable by more handshake messages."""
    s = _joiner_session()
    s.mark_host_authenticated("host")
    s.handle_message("impostor", {
        "type": "admission_challenge", "pubkey": K2.hex(),
        "payload": {"client_nonce": "00" * 16, "server_nonce": "11" * 16}})
    assert s._host_conn_id == "host", "a later challenge repointed the host"
    assert s._host_authenticated is True


# ═══════════════════════════════ THE SESSION PERIMETER (host pre-admission)

def _host_session(host_admission):
    s = Session(is_host=True, nickname="H", avatar_b64="",
                transport=_Spy(), admission=host_admission)
    s.local_conn_id = "host"
    return s


def test_an_unadmitted_connection_never_appears_in_players():
    """The M-8 boundary: _on_player_info used to accept anyone who signed."""
    h = _host_adm()
    s = _host_session(h)
    before = _perimeter(s)
    s.handle_message("stranger", {
        "type": "player_info", "pubkey": K2.hex(), "ts": 1, "prev": "0" * 64,
        "sig": "ff" * 64, "hash": "ab" * 32,
        "payload": {"nickname": "mallory"}})
    assert "stranger" not in s.players
    assert _perimeter(s) == before


def test_an_admitted_connection_may_send_player_info():
    """The inverse, so the rejection above is not vacuous."""
    h = _host_adm()
    s = _host_session(h)
    assert _full_exchange(h, conn_id="c1", joiner=K1)[0] is True
    s.handle_message("c1", {
        "type": "player_info", "pubkey": K1.hex(), "ts": 1, "prev": "0" * 64,
        "sig": "ff" * 64, "hash": "ab" * 32,
        "payload": {"nickname": "alice"}})
    assert "c1" in s.players, "an admitted peer was refused"
    assert s.players["c1"].ed25519_pubkey_hex == K1.hex()


def test_admitted_as_k1_then_player_info_signed_by_k2_is_rejected():
    """Connection identity continuity, at the Session boundary.

    The client genuinely holds the capability -- it completed admission as
    K1 -- so this is not an outsider. It is an insider trying to seat a
    different signing identity than the one the transcript committed to.
    """
    h = _host_adm()
    s = _host_session(h)
    assert _full_exchange(h, conn_id="c1", joiner=K1)[0] is True
    before = _perimeter(s)
    s.handle_message("c1", {
        "type": "player_info", "pubkey": K2.hex(), "ts": 1, "prev": "0" * 64,
        "sig": "ff" * 64, "hash": "ab" * 32,
        "payload": {"nickname": "mallory"}})
    assert "c1" not in s.players
    assert _perimeter(s) == before


def test_k2_traffic_is_rejected_before_the_hash_chain_records_it():
    """Ordering matters: the gate is ahead of the chain bookkeeping.

    Otherwise a rejected message still writes _peer_last_hash for that
    connection -- state written on behalf of a peer that just failed the
    only check standing in front of it.
    """
    h = _host_adm()
    s = _host_session(h)
    assert _full_exchange(h, conn_id="c1", joiner=K1)[0] is True
    s.handle_message("c1", {
        "type": "chat", "pubkey": K2.hex(), "ts": 1, "prev": "0" * 64,
        "sig": "ff" * 64, "hash": "deadbeef" * 8,
        "payload": {"nickname": "x", "text": "x"}})
    assert "c1" not in s._peer_last_hash, (
        "a message that failed the identity check still seeded chain state")


def test_a_wire_mode_host_cannot_be_built_without_admission():
    """The insecure configuration is unreachable by omission."""
    with pytest.raises(ValueError, match="admission"):
        Session(is_host=True, nickname="H", avatar_b64="", transport=_Spy())


# ══════════════════════════════════════════════ INVITE / LEAKAGE INVARIANTS

def test_the_admission_secret_never_appears_in_a_repr():
    """Not in logs, tracebacks, or an incidental repr of a container."""
    h = _host_adm()
    j = _joiner_adm()
    for text in (repr(h), repr(j), repr([h, j])):
        assert SECRET.hex() not in text
        assert "redacted" in text


def test_the_public_room_id_carries_no_capability():
    """What the relay and the LAN are allowed to see."""
    code = inv.generate_room_code(host_pubkey=HOST_KEY)
    parsed = inv.parse_room_code(code)
    room = inv.public_room_id(parsed)
    assert room == parsed["discovery_token"]
    assert parsed["admission_secret"] not in room
    assert parsed["host_pubkey"] not in room
    # And the token is not simply a prefix of the secret.
    assert not parsed["admission_secret"].startswith(room)


def test_v2_with_trailing_bytes_is_rejected_not_truncated():
    """An authentication credential gets exact-length parsing.

    V1 accepted "at least this many bytes", which for a credential means an
    attacker can append anything and still be parsed.
    """
    import base64
    raw = base64.b32decode(
        inv.strip_code(inv.generate_room_code(host_pubkey=HOST_KEY))
        + "=" * ((8 - len(inv.strip_code(
            inv.generate_room_code(host_pubkey=HOST_KEY))) % 8) % 8))
    longer = raw + b"\x00" * 5
    b32 = base64.b32encode(longer).decode().rstrip("=")
    with pytest.raises(ValueError, match="expected"):
        inv.parse_room_code("-".join(b32[i:i + 4]
                                     for i in range(0, len(b32), 4)))


def test_stun_regeneration_keeps_the_same_lobby_authenticable():
    """A code copied five seconds ago must not become cryptographically dead.

    The host regenerates the displayed invite when STUN resolves. If either
    the token or the secret rotated, a guest holding the earlier code would
    fail admission against a lobby that looks identical on screen.
    """
    first = inv.parse_room_code(inv.generate_room_code(host_pubkey=HOST_KEY))
    second = inv.parse_room_code(inv.generate_room_code(
        host_pubkey=HOST_KEY,
        public_address=("5.6.7.8", 9999),
        discovery_token=first["discovery_token"],
        admission_secret=first["admission_secret"]))

    assert second["discovery_token"] == first["discovery_token"]
    assert second["admission_secret"] == first["admission_secret"]
    assert second["host_pubkey"] == first["host_pubkey"]

    # And a handshake built from the OLD copy still admits against the
    # live lobby's policy.
    live = adm.HostAdmission(
        admission_secret=bytes.fromhex(second["admission_secret"]),
        host_pubkey=HOST_KEY,
        discovery_token=bytes.fromhex(second["discovery_token"]))
    cn = adm.new_nonce()
    sn = bytes.fromhex(live.on_hello("c1", K1, cn)["server_nonce"])
    mac = adm.compute_mac(
        bytes.fromhex(first["admission_secret"]),
        adm.transcript(bytes.fromhex(first["discovery_token"]), HOST_KEY, K1,
                       cn, sn))
    assert live.on_response("c1", K1, cn, sn, mac) is True


# ═══════════════════════ THE SHIPPED HOST PATH (review finding 1)

def test_a_real_host_session_answers_a_hello_with_a_challenge():
    """The defect an independent review found that 1249 green tests did not.

    HostAdmission.on_hello/on_response/accept_payload had ZERO callers in
    holdem/. The only implementation lived in tests/prod_peer.py, so a real
    host received admission_hello and replied with nothing: no challenge,
    no admission, nobody could join a hosted game. The perimeter's claim
    held only vacuously, because no connection could ever be admitted.

    Every host-side test called HostAdmission directly or drove the
    harness. This one drives a real Session, which is what the application
    actually runs.
    """
    h = _host_adm()
    t = _Spy()
    s = Session(is_host=True, nickname="H", avatar_b64="",
                transport=t, admission=h)
    s.local_conn_id = "host"

    s.handle_message("c1", {
        "type": "admission_hello", "pubkey": K1.hex(), "ts": 1,
        "prev": "0" * 64, "sig": "ff" * 64, "hash": "ab" * 32,
        "payload": {"client_nonce": adm.new_nonce().hex()}})

    challenges = [m for m in t.sent if m.get("type") == "admission_challenge"]
    assert challenges, (
        "a real host Session answered a signed admission_hello with silence; "
        f"it sent {[m.get('type') for m in t.sent]}")
    assert "server_nonce" in challenges[0]


def test_a_real_host_session_completes_the_whole_handshake():
    """hello -> challenge -> response -> accept, entirely through Session."""
    h = _host_adm()
    t = _Spy()
    s = Session(is_host=True, nickname="H", avatar_b64="",
                transport=t, admission=h)
    s.local_conn_id = "host"

    cn = adm.new_nonce()
    s.handle_message("c1", {"type": "admission_hello", "pubkey": K1.hex(),
                            "payload": {"client_nonce": cn.hex()}})
    sn = bytes.fromhex(t.sent[-1]["server_nonce"])

    s.handle_message("c1", {"type": "admission_response", "pubkey": K1.hex(),
                            "payload": {"client_nonce": cn.hex(),
                                        "server_nonce": sn.hex(),
                                        "mac": _mac_for(cn, sn).hex()}})

    assert [m.get("type") for m in t.sent] == [
        "admission_challenge", "admission_accept"]
    assert h.is_admitted("c1"), "the Session did not admit a correct response"
    assert h.admitted_key("c1") == K1


def test_a_real_host_session_refuses_a_wrong_secret_and_sends_no_accept():
    h = _host_adm()
    t = _Spy()
    s = Session(is_host=True, nickname="H", avatar_b64="",
                transport=t, admission=h)
    s.local_conn_id = "host"

    cn = adm.new_nonce()
    s.handle_message("c1", {"type": "admission_hello", "pubkey": K1.hex(),
                            "payload": {"client_nonce": cn.hex()}})
    sn = bytes.fromhex(t.sent[-1]["server_nonce"])
    s.handle_message("c1", {"type": "admission_response", "pubkey": K1.hex(),
                            "payload": {"client_nonce": cn.hex(),
                                        "server_nonce": sn.hex(),
                                        "mac": _mac_for(
                                            cn, sn, secret=OTHER_SEC).hex()}})
    assert "admission_accept" not in [m.get("type") for m in t.sent]
    assert not h.is_admitted("c1")


@pytest.mark.parametrize("body", [
    {}, {"client_nonce": "zz"}, {"client_nonce": "00" * 4},
    {"client_nonce": None},
])
def test_malformed_handshake_input_is_dropped_not_raised(body):
    """The only surface an UNADMITTED peer can reach, so it must be inert."""
    h = _host_adm()
    t = _Spy()
    s = Session(is_host=True, nickname="H", avatar_b64="",
                transport=t, admission=h)
    s.local_conn_id = "host"
    s.handle_message("c1", {"type": "admission_hello", "pubkey": K1.hex(),
                            "payload": body})
    assert not h.is_admitted("c1")


# ═══════════════════════ HOST LOSS DOES NOT MIGRATE (review finding 2)

def test_wire_mode_refuses_to_elect_a_replacement_host():
    """Promotion is incompatible with pinning one exact host key.

    _elect_new_host set is_host = True on a Session built with
    admission=None, and _admission_ok short-circuits on a missing policy --
    so a promoted peer admitted anyone. The constructor invariant only ever
    covered construction.

    Migration is not repaired here, it is refused: the invite every joiner
    holds names ONE host key, so a promoted peer cannot be authenticated by
    it. Authenticated authority transfer is a separate protocol.
    """
    s = Session(is_host=False, nickname="J", avatar_b64="", transport=_Spy())
    s.local_conn_id = "me"
    s._host_conn_id = "hostconn"
    s._join_order = ["me", "other"]
    assert s.author_mode == AUTHOR_MODE_WIRE

    s.handle_disconnect("hostconn")

    assert s.is_host is False, "a wire-mode joiner promoted itself to host"
    assert s.terminal_state is not None, (
        "the session survived losing the only host its invite can name")
    # Asserted through handle_message rather than on _admission_ok: the
    # session is terminal, so the terminal check upstream is what refuses
    # traffic here, and probing the gate directly would report an open gate
    # that nothing can actually reach.
    s.handle_message("stranger", {
        "type": "player_info", "pubkey": K2.hex(),
        "payload": {"nickname": "mallory"}})
    assert "stranger" not in s.players, (
        "a terminated session still admitted a stranger to the roster")


def test_compat_mode_still_migrates():
    """The inverse: the old behaviour lives on where it is harmless."""
    class _Compat(_Spy):
        delivers_verified_envelopes = False

    s = Session(is_host=False, nickname="J", avatar_b64="",
                transport=_Compat())
    s.local_conn_id = "me"
    s._host_conn_id = "hostconn"
    s._join_order = ["me", "other"]
    assert s.author_mode == AUTHOR_MODE_COMPAT
    s.handle_disconnect("hostconn")
    assert s.is_host is True, "compat harnesses still rely on migration"


# ═══════════════════════ CHAIN AND FAIL-CLOSED (findings 4, 7)

def test_a_roster_naming_a_different_host_key_is_refused():
    """Closes invite key -> the host's own frozen seat key.

    The roster is host-authoritative, so without this a joiner's seat keys
    are whatever the host asserted -- including for the one identity the
    invite already pinned.
    """
    s = Session(is_host=False, nickname="J", avatar_b64="", transport=_Spy(),
                joiner_admission=_joiner_adm())
    s.local_conn_id = "me"
    s.mark_host_authenticated("host", HOST_KEY)

    near_miss = (HOST_KEY[:8] + bytes([0xFF]) * 24).hex()
    s.handle_message("host", {
        "type": "player_list", "pubkey": HOST_KEY.hex(),
        "payload": {"players": [
            {"conn_id": "host", "nickname": "H", "is_host": True,
             "ed25519_pubkey_hex": near_miss}]}})
    assert "host" not in s.players, (
        "a roster naming a host key that differs only after byte 8 was "
        "accepted; the chain would be an 8-byte claim")


def test_a_roster_naming_the_pinned_host_key_is_accepted():
    """The inverse, so the refusal above is not vacuous."""
    s = Session(is_host=False, nickname="J", avatar_b64="", transport=_Spy(),
                joiner_admission=_joiner_adm())
    s.local_conn_id = "me"
    s.mark_host_authenticated("host", HOST_KEY)
    s.handle_message("host", {
        "type": "player_list", "pubkey": HOST_KEY.hex(),
        "payload": {"players": [
            {"conn_id": "host", "nickname": "H", "is_host": True,
             "ed25519_pubkey_hex": HOST_KEY.hex()}]}})
    assert "host" in s.players


def test_an_admitted_wire_connection_rejects_a_message_with_no_author():
    """Missing pubkey is invalid, not exempt.

    The transport hands up unsigned relay-control frames; treating those as
    "nothing to compare" let them past the identity check and into the
    hash-chain bookkeeping, seeding per-peer state from unsigned bytes.
    """
    h = _host_adm()
    s = _host_session(h)
    assert _full_exchange(h, conn_id="c1", joiner=K1)[0] is True
    before = _perimeter(s)
    s.handle_message("c1", {"type": "chat", "hash": "cc" * 32,
                            "prev": "0" * 64,
                            "payload": {"nickname": "x", "text": "x"}})
    assert "c1" not in s._peer_last_hash
    assert _perimeter(s) == before
