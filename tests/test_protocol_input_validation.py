"""Protocol input validation: audit findings #59, #62, #63, #78, #82.
(#79, STUN, is covered next to the other STUN tests in test_p2p.py.)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_gate import initialize_for_tests, NativeCryptoUnavailable
from tests import joiner_helpers as JH

try:
    initialize_for_tests()
    from holdem.p2p import elgamal as eg
    from holdem.p2p import invite as inv
    from holdem.p2p import ristretto as R
    from holdem.p2p.mental_deal import Phase
    from holdem.p2p.session import Player, Session
    from holdem.p2p.timeout import FakeClock
    import test_join_auth as J
    import test_mental_deal_prevention as MP
except NativeCryptoUnavailable as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}", allow_module_level=True)


# ------------------------------------------------- #59 admission terminal

def _challenge_and_response(t, host, auth):
    auth.begin("conn-1")
    cn = bytes.fromhex(t.sent[-1][1]["client_nonce"])
    msg, _ = J._challenge_from(host, "conn-1", J._identity.public_key_bytes(), cn)
    auth.route("conn-1", msg)
    resp = t.sent[-1][1]
    assert host.on_response("conn-1", J._identity.public_key_bytes(),
                            bytes.fromhex(resp["client_nonce"]),
                            bytes.fromhex(resp["server_nonce"]),
                            bytes.fromhex(resp["mac"]))
    return {"type": "admission_accept", "pubkey": J.HOST_KEY.hex(),
            "payload": host.accept_payload("conn-1")}


def test_a_valid_accept_queued_behind_a_failure_cannot_authenticate():
    """The UI was told the join failed; a valid accept arriving next must
    not then authenticate the very connection that failed."""
    parsed, host, t, ja, sess, auth, events = J._wire_up()
    valid_accept = _challenge_and_response(t, host, auth)
    auth.route("conn-1", {"type": "admission_accept", "pubkey": "zz-not-hex",
                          "payload": {}})                  # malformed -> fail
    auth.route("conn-1", valid_accept)                     # queued behind it
    assert events["failed"] and len(events["failed"]) == 1
    assert events["auth"] == []
    assert "player_info" not in t.types()


def test_a_bad_message_after_success_does_not_also_report_failure():
    parsed, host, t, ja, sess, auth, events = J._wire_up()
    valid_accept = _challenge_and_response(t, host, auth)
    auth.route("conn-1", valid_accept)
    assert events["auth"] == ["conn-1"]
    auth.route("conn-1", {"type": "admission_accept", "pubkey": "zz",
                          "payload": {}})
    assert events["failed"] == []
    assert t.disconnected == []


def test_exactly_one_failure_callback_per_attempt_and_begin_starts_a_new_one():
    parsed, host, t, ja, sess, auth, events = J._wire_up()
    auth.begin("conn-1")
    for _ in range(3):
        auth.route("conn-1", {"type": "admission_challenge", "pubkey": "zz",
                              "payload": {}})
    assert len(events["failed"]) == 1
    assert t.disconnected == ["conn-1"]
    auth.begin("conn-2")                                   # explicit new attempt
    assert t.sent[-1] == ("conn-2", t.sent[-1][1])
    assert t.sent[-1][1]["type"] == "admission_hello"
    auth.route("conn-2", {"type": "admission_challenge", "pubkey": "zz",
                          "payload": {}})
    assert len(events["failed"]) == 2


# --------------------------------------------- #62 non-card plaintext

def test_a_non_card_plaintext_voids_instead_of_stalling():
    """Detection-only: the last shuffler swaps the ciphertext at seat 0's
    first hole position for an encryption of a point that is no card. Every
    decryption share is valid, so seat 0 used to wait for that hole forever;
    now it aborts at once, blaming no seat (the shuffler is unknown until
    the witnesses are opened)."""
    seats = [0, 1]
    target = {}

    def tamper(msg, deals):
        if msg.get("type") == "deck_round" and msg.get("round") == len(seats):
            d0 = deals[0]                    # same map MentalDeal uses
            pos = MP.md.dmap.hole_positions(d0.button, d0.seats_in)[0][0]
            junk = R.mul_base(R.random_scalar())
            assert eg.point_to_card(junk) is None
            msg = dict(msg)
            deck = list(msg["deck"])
            deck[pos] = eg.encrypt(deals[0].joint_pk, junk).to_hex()
            msg["deck"] = deck
            target["pos"] = pos
        return msg

    deals, _ = MP.run(seats, prevention=False, tamper=tamper)
    assert "pos" in target, "the tamper hook never fired"
    d = deals[0]
    assert d.phase == Phase.ABORTED, "seat 0 stalled instead of aborting"
    assert "not a card" in d.abort_reason
    assert d.bad_seat is None


def test_an_honest_deal_is_unaffected():
    deals, _ = MP.run([0, 1], prevention=False)
    for s in (0, 1):
        assert deals[s].phase == Phase.DEAL
        assert deals[s].hole_complete()


# ------------------------------------------------- #63 invite inputs

KEY = bytes(range(32))


def test_reused_token_and_secret_round_trip_exactly():
    code = inv.generate_room_code(KEY)
    p = inv.parse_room_code(code)
    again = inv.parse_room_code(inv.generate_room_code(
        KEY, discovery_token=p["discovery_token"],
        admission_secret=p["admission_secret"]))
    assert again["discovery_token"] == p["discovery_token"]
    assert again["admission_secret"] == p["admission_secret"]


@pytest.mark.parametrize("field,size", [("discovery_token", inv.DISCOVERY_TOKEN_LEN),
                                        ("admission_secret", inv.ADMISSION_SECRET_LEN)])
@pytest.mark.parametrize("make", [
    lambda n: "ab" * (n + 1),            # longer: used to be silently truncated
    lambda n: "ab" * (n - 1),            # shorter: used to be an AssertionError
    lambda n: "zz" * n,                  # not hex
    lambda n: "a" * (2 * n - 1),         # odd length
    lambda n: bytes(n),                  # not a string
])
def test_reused_values_must_be_exact(field, size, make):
    with pytest.raises(ValueError):
        inv.generate_room_code(KEY, **{field: make(size)})


def test_distinct_tokens_can_no_longer_collide_by_truncation():
    base = "11" * inv.DISCOVERY_TOKEN_LEN
    inv.generate_room_code(KEY, discovery_token=base)
    with pytest.raises(ValueError):
        inv.generate_room_code(KEY, discovery_token=base + "22")


@pytest.mark.parametrize("flags", [256, -1, True, 1.0])
def test_flags_out_of_byte_range_are_refused(flags):
    with pytest.raises(ValueError):
        inv.generate_room_code(KEY, flags=flags)


@pytest.mark.parametrize("port", [65536, -1, "80", True])
def test_ports_out_of_range_are_value_errors(port):
    with pytest.raises(ValueError):
        inv.generate_room_code(KEY, public_address=("1.2.3.4", port))


# ------------------------------------------------- #78 bet_action frames

class _Replica:
    def __init__(self):
        self.calls = []
        self.next_seq = 0
        self.phase = "betting"
        self.actor = None

    def apply_action(self, seq, seat, action, amount):
        self.calls.append((seq, seat, action, amount))
        return "rejected"

    def state_digest(self):
        return ""


def _bet_session():
    s = Session(is_host=False, nickname="P", avatar_b64="",
                transport=J.FakeTransport(), clock=FakeClock(),
                joiner_admission=JH.joiner_admission(),
                master_secret=bytes([7]) * 32)
    s._replica = _Replica()
    s._hand_msg_ok = lambda conn_id, msg: True
    return s


@pytest.mark.parametrize("field,bad", [
    ("seq", True), ("seq", "3"), ("seq", 2.5), ("seq", -1), ("seq", None),
    ("amount", True), ("amount", "25"), ("amount", 25.7), ("amount", -5),
    ("action", 5), ("seat", True), ("seat", "1"),
])
def test_malformed_bet_frames_are_refused_not_coerced(field, bad):
    s = _bet_session()
    msg = {"type": "bet_action", "seq": 3, "seat": 1, "action": "raise",
           "amount": 40}
    msg[field] = bad
    s._on_bet_action("A", msg)
    assert s._replica.calls == [], f"{field}={bad!r} reached the replica"


def test_a_well_formed_bet_frame_still_reaches_the_replica():
    s = _bet_session()
    s._on_bet_action("A", {"type": "bet_action", "seq": 3, "seat": 1,
                           "action": "raise", "amount": 40})
    assert s._replica.calls == [(3, 1, "raise", 40)]


# --------------------------------------------------------- #82 chat

ALICE, BOB = "aa" * 32, "bb" * 32


def _chat_host():
    t = J.FakeTransport()
    # Compat flavour: a verified-envelope HOST must be given an admission
    # policy, which is beside the point of a direct handler test.
    t.delivers_verified_envelopes = False
    sent = t.sent                     # (conn_id, msg); broadcasts use None

    s = Session(is_host=True, nickname="H", avatar_b64="", transport=t,
                clock=FakeClock(), master_secret=bytes([7]) * 32)
    s.players["ca"] = Player(conn_id="ca", peer_id="", nickname="Alice",
                             avatar_b64="", ed25519_pubkey_hex=ALICE)
    s.players["cb"] = Player(conn_id="cb", peer_id="", nickname="Bob",
                             avatar_b64="", ed25519_pubkey_hex=BOB)
    got = []
    s.on_chat = lambda nick, text: got.append((nick, text))
    return s, got, sent


def test_chat_shows_the_signing_author_not_the_claimed_nickname():
    s, got, sent = _chat_host()
    s._on_chat("cb", {"type": "chat", "pubkey": BOB,
                      "payload": {"nickname": "Alice", "text": "send me chips"}})
    assert got == [("Bob", "send me chips")]


def test_chat_from_an_unknown_signing_key_is_dropped_and_not_relayed():
    s, got, sent = _chat_host()
    s._on_chat("cb", {"type": "chat", "pubkey": "cc" * 32,
                      "payload": {"nickname": "Alice", "text": "hi"}})
    assert got == [] and sent == []


def test_unsigned_chat_at_the_host_uses_the_sending_connection():
    s, got, sent = _chat_host()
    s._on_chat("cb", {"type": "chat", "payload": {"nickname": "Alice", "text": "x"}})
    assert got == [("Bob", "x")]
