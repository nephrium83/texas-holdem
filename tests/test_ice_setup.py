"""Direct introductions preserve pins, expiry, signatures, and AEAD context."""
import base64
import hmac
import json
import time

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from holdem import ice_setup as S
from holdem.p2p.identity import Identity


def _identity():
    return Identity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())


@pytest.fixture
def records():
    now = int(time.time())
    host, guest = _identity(), _identity()
    descriptor = S.sign_record({"v": 1, "purpose": S.HOST_PURPOSE, "service": "test",
                               "host_public": host.public_key_bytes().hex(), "discovery_token": "a" * 16,
                               "generation": "b" * 32, "setup_public": host.x25519_public_key_bytes().hex(),
                               "challenge": "c" * 64, "created_at": now, "expires_at": now + 600}, host)
    ready = {"v": 1, "purpose": S.GUEST_PURPOSE, "service": "test",
             "host_public": descriptor["host_public"], "discovery_token": descriptor["discovery_token"],
             "generation": descriptor["generation"], "guest_public": guest.public_key_bytes().hex(),
             "setup_public": guest.x25519_public_key_bytes().hex(), "guest_nonce": "d" * 64,
             "challenge": "e" * 64, "created_at": now, "expires_at": now + 60}
    secret = bytes(range(16))
    ready["capability_proof"] = S.capability_proof(secret, ready, descriptor)
    ready = S.sign_record(ready, guest)
    return host, guest, descriptor, ready, secret, now


def test_host_guest_records_and_complete_setup_round_trip(records):
    host, guest, descriptor, ready, _, now = records
    assert S.verify_host_record(descriptor, service="test", now=now) is descriptor
    assert S.verify_guest_record(ready, service="test", now=now) is ready
    context = S.setup_context(descriptor, ready, now=now)
    for sender, receiver, kind in ((host, guest, "offer"), (guest, host, "answer")):
        description = {"type": kind, "sdp": "v=0\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"}
        envelope = S.seal_description(description, context, sender, receiver.x25519_public_key_bytes().hex())
        assert "sdp" not in json.dumps(envelope)
        assert S.validate_envelope(envelope, context, kind=kind, now=now) is envelope
        assert S.open_description(envelope, context, receiver, kind=kind,
                                  signer=sender.public_key_bytes().hex(), now=now) == description


def test_capability_proof_binds_host_record_generation_and_guest_keys(records):
    host, guest, descriptor, ready, secret, now = records
    proof = S.capability_proof(secret, ready, descriptor)
    assert hmac.compare_digest(proof, ready["capability_proof"])
    assert proof == S.capability_proof(secret, {**ready, "signature": "0" * 128, "capability_proof": "0" * 64}, descriptor)
    assert proof != S.capability_proof(bytes(reversed(secret)), ready, descriptor)
    for field in ("guest_public", "setup_public", "guest_nonce", "generation", "challenge"):
        altered = {**ready, field: "f" * len(ready[field])}
        assert proof != S.capability_proof(secret, altered, descriptor)
    rotated_host = S.sign_record({**descriptor, "challenge": "f" * 64}, host)
    assert proof != S.capability_proof(secret, ready, rotated_host)
    assert secret.hex() not in json.dumps(ready)


@pytest.mark.parametrize("field,value", [
    ("v", True), ("purpose", "poker-direct-guest-v1"), ("service", "other"),
    ("host_public", "0" * 16), ("discovery_token", "a" * 18),
    ("generation", "x" * 32), ("setup_public", "A" * 64),
    ("challenge", "a" * 63), ("created_at", True), ("expires_at", None),
    ("signature", "0" * 128), ("extra", "field"),
])
def test_malformed_host_record_fails_closed(records, field, value):
    _, _, descriptor, _, _, now = records
    altered = {**descriptor, field: value}
    with pytest.raises(S.SetupError):
        S.verify_host_record(altered, service="test", now=now)


@pytest.mark.parametrize("field,value", [
    ("v", False), ("purpose", "poker-direct-room-v1"), ("service", "other"),
    ("guest_public", "0" * 16), ("setup_public", None), ("guest_nonce", "0" * 62),
    ("capability_proof", "0" * 63), ("signature", "0" * 128), ("extra", 1),
])
def test_malformed_guest_record_fails_closed(records, field, value):
    _, _, _, ready, _, now = records
    with pytest.raises(S.SetupError):
        S.verify_guest_record({**ready, field: value}, service="test", now=now)


@pytest.mark.parametrize("kind,delta", [("host", 1801), ("guest", 91)])
def test_even_valid_signatures_cannot_extend_record_lifetime(records, kind, delta):
    host, guest, descriptor, ready, _, now = records
    identity, record, verifier = (host, descriptor, S.verify_host_record) if kind == "host" else (guest, ready, S.verify_guest_record)
    altered = S.sign_record({**record, "expires_at": now + delta}, identity)
    with pytest.raises(S.SetupError, match="expired or not yet valid"):
        verifier(altered, service="test", now=now)
    with pytest.raises(S.SetupError):
        verifier(record, service="test", now=record["expires_at"])
    future = S.sign_record({**record, "created_at": now + 31, "expires_at": now + 61}, identity)
    with pytest.raises(S.SetupError):
        verifier(future, service="test", now=now)


def test_cross_room_or_overlong_guest_records_cannot_form_context(records):
    host, guest, descriptor, ready, _, now = records
    for field in ("host_public", "discovery_token", "generation"):
        altered = S.sign_record({**ready, field: "f" * len(ready[field])}, guest)
        with pytest.raises(S.SetupError):
            S.setup_context(descriptor, altered, now=now)
    short_host = S.sign_record({**descriptor, "expires_at": now + 10}, host)
    with pytest.raises(S.SetupError, match="outlives"):
        S.setup_context(short_host, ready, now=now)


def test_ciphertext_cannot_be_replayed_into_another_attempt_or_generation(records):
    host, guest, descriptor, ready, _, now = records
    context = S.setup_context(descriptor, ready, now=now)
    envelope = S.seal_description({"type": "offer", "sdp": "private candidates"}, context, host, context["guest_setup"])
    for field, length in (("generation", 32), ("guest_nonce", 64), ("discovery_token", 16)):
        replay_context = {**context, field: "f" * length}
        with pytest.raises(S.SetupError, match="context mismatch"):
            S.open_description(envelope, replay_context, guest, kind="offer", signer=descriptor["host_public"], now=now)
    with pytest.raises(S.SetupError):
        S.open_description(envelope, context, guest, kind="offer", signer="f" * 64, now=now)
    with pytest.raises(S.SetupError):
        S.open_description(envelope, context, guest, kind="answer", signer=descriptor["host_public"], now=now)
    with pytest.raises(S.SetupError):
        S.open_description(envelope, context, _identity(), kind="offer", signer=descriptor["host_public"], now=now)
    with pytest.raises(S.SetupError):
        S.open_description(envelope, context, guest, kind="offer", signer=descriptor["host_public"], now=ready["expires_at"])


def test_valid_signature_does_not_hide_aead_tampering(records):
    host, guest, descriptor, ready, _, now = records
    context = S.setup_context(descriptor, ready, now=now)
    envelope = S.seal_description({"type": "offer", "sdp": "private candidates"}, context, host, context["guest_setup"])
    encrypted = bytearray(base64.b64decode(envelope["ciphertext"]))
    encrypted[0] ^= 1
    altered = S.sign_record({**envelope, "ciphertext": base64.b64encode(encrypted).decode()}, host)
    with pytest.raises(S.SetupError, match="decryption failed"):
        S.open_description(altered, context, guest, kind="offer", signer=descriptor["host_public"], now=now)


def test_only_context_pinned_identities_can_seal_setup(records):
    host, guest, descriptor, ready, _, now = records
    context = S.setup_context(descriptor, ready, now=now)
    with pytest.raises(S.SetupError):
        S.seal_description({"type": "offer", "sdp": "v=0"}, context, guest, context["guest_setup"])
    with pytest.raises(S.SetupError):
        S.seal_description({"type": "offer", "sdp": "v=0"}, context, host, context["host_setup"])
    with pytest.raises(S.SetupError):
        S.seal_description({"type": "offer", "sdp": "v=0"}, {**context, "guest_setup": "0" * 64}, host, "0" * 64)


@pytest.mark.parametrize("raw", [
    b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":1e999}',
    b'[]', b'{}' + b' ' * S.MAX_JSON_BYTES, b'{"x":' + b'[' * 10 + b'0' + b']' * 10 + b'}',
    json.dumps({str(i): i for i in range(257)}).encode(), b'\xff',
], ids=["duplicate", "nan", "infinity", "overflow", "array", "oversize", "depth", "members", "invalid-utf8"])
def test_network_json_rejects_oversize_duplicate_deep_and_nonfinite_input(raw):
    with pytest.raises(S.SetupError):
        S.load_json(raw)


def test_serialization_has_the_same_depth_member_and_finite_bounds():
    value = {"x": {"y": [1, True, None, 1.5]}}
    assert S.load_json(S.canonical(value)) == value
    for invalid in ({"x": float("nan")}, {i: i for i in range(10)}, {"x": object()}, {"sdp": "x" * S.MAX_JSON_BYTES}):
        with pytest.raises(S.SetupError):
            S.canonical(invalid)
