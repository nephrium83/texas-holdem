"""Run/package/key binding and confidentiality of engineering setup artifacts."""
import copy
import json
from pathlib import Path

import pytest

from holdem.ice_signaling import (
    ProbeKeys, SetupError, canonical, load_json, make_ready, open_description,
    read_json, seal_description, validate_context, validate_envelope,
    validate_ready, validate_report, write_json,
)


@pytest.fixture
def setup(tmp_path):
    pytest.importorskip("cryptography")
    host = ProbeKeys.generate(tmp_path / "host")
    guest = ProbeKeys.generate(tmp_path / "guest")
    context = {"repository": "nephrium83/texas-holdem", "run_id": "123456",
               "run_attempt": "1", "nonce": "01" * 32, "source_commit": "02" * 20,
               "package_sha256": "03" * 32, "host_signing_public": host.signing_public,
               "host_encryption_public": host.encryption_public}
    return host, guest, context


def test_bidirectional_setup_and_ready_do_not_expose_sdp(setup, tmp_path):
    host, guest, context = setup
    ready = make_ready(context, guest)
    assert validate_ready(ready, context) == ready
    description = {"type": "offer", "sdp": "v=0\na=ice-pwd:PRIVATE-PASSWORD\n192.0.2.10"}
    offer = seal_description(description, context, host, guest.encryption_public, now=1000)
    assert "PRIVATE-PASSWORD" not in canonical(offer).decode()
    assert "192.0.2.10" not in canonical(offer).decode()
    assert open_description(offer, context, guest, kind="offer", signer=host.signing_public,
                            now=1001) == description
    answer_description = {"type": "answer", "sdp": "v=0\na=ice-pwd:OTHER-PRIVATE"}
    answer = seal_description(answer_description, context, guest, host.encryption_public, now=1000)
    assert open_description(answer, context, host, kind="answer", signer=guest.signing_public,
                            now=1001) == answer_description
    assert ProbeKeys.load(tmp_path / "host").public() == host.public()
    with pytest.raises(SetupError):
        ProbeKeys.generate(tmp_path / "host")


@pytest.mark.parametrize("field", ["repository", "run_id", "run_attempt", "nonce", "source_commit",
                                  "package_sha256", "host_signing_public", "host_encryption_public"])
def test_context_swaps_fail_before_applying_description(setup, field):
    host, guest, context = setup
    offer = seal_description({"type": "offer", "sdp": "v=0"}, context, host, guest.encryption_public, now=1000)
    changed = dict(context)
    changed[field] = {"repository": "attacker/repo", "run_id": "9876", "run_attempt": "2"}.get(
        field, "ab" * (20 if field == "source_commit" else 32))
    with pytest.raises(SetupError):
        open_description(offer, changed, guest, kind="offer", signer=host.signing_public, now=1000)


@pytest.mark.parametrize("field,value", [
    ("kind", "answer"), ("sender_signing_public", "aa" * 32),
    ("recipient_encryption_public", "bb" * 32), ("ephemeral_public", "cc" * 32),
    ("nonce", "dd" * 12), ("signature", "00" * 64), ("purpose", "other-purpose"),
    ("ciphertext", "AAAA" * 20), ("v", True),
])
def test_tampering_fails(setup, field, value):
    host, guest, context = setup
    offer = seal_description({"type": "offer", "sdp": "v=0"}, context, host, guest.encryption_public, now=1000)
    offer[field] = value
    with pytest.raises(SetupError):
        open_description(offer, context, guest, kind="offer", signer=host.signing_public, now=1000)


def test_pin_recipient_role_and_expiry_rejected(setup, tmp_path):
    host, guest, context = setup
    other = ProbeKeys.generate(tmp_path / "other")
    offer = seal_description({"type": "offer", "sdp": "v=0"}, context, host, guest.encryption_public, now=1000)
    for keys, kind, signer, now in [
        (guest, "offer", other.signing_public, 1000),
        (other, "offer", host.signing_public, 1000),
        (guest, "answer", host.signing_public, 1000),
        (guest, "offer", host.signing_public, 1601),
        (guest, "offer", host.signing_public, 969),
    ]:
        with pytest.raises(SetupError):
            open_description(offer, context, keys, kind=kind, signer=signer, now=now)
    with pytest.raises(SetupError):
        seal_description({"type": "offer", "sdp": "v=0"}, context, other, guest.encryption_public)


def test_ready_keys_context_and_extra_fields_are_authenticated(setup):
    host, guest, context = setup
    original = make_ready(context, guest)
    for name, value in [("guest_signing_public", host.signing_public),
                        ("guest_encryption_public", host.encryption_public),
                        ("context", {**context, "run_attempt": "2"}),
                        ("sdp", "private")]:
        changed = copy.deepcopy(original)
        changed[name] = value
        with pytest.raises(SetupError):
            validate_ready(changed, context)


@pytest.mark.parametrize("raw", [b'{"v":1,"v":2}', b'{"v":NaN}', b'{"v":1e9999}', b'[]', b'"text"',
                                b'{"a":' + b'[' * 10000, b' ' * (256 * 1024 + 1)],
                         ids=["duplicate", "nan", "overflow", "array", "string", "deep", "oversize"])
def test_hostile_json_is_bounded_and_strict(raw):
    with pytest.raises(SetupError):
        load_json(raw)


def test_atomic_new_files_and_bounded_reader(tmp_path):
    path = tmp_path / "evidence.json"
    write_json(path, {"v": 1})
    assert read_json(path) == {"v": 1}
    with pytest.raises(SetupError):
        write_json(path, {"v": 2})
    assert read_json(path) == {"v": 1}
    assert not list(tmp_path.glob("*.tmp"))
    path.write_bytes(b" " * (256 * 1024 + 1))
    with pytest.raises(SetupError):
        read_json(path)


def test_large_ascii_sdp_and_non_ascii_bound(setup):
    host, guest, context = setup
    description = {"type": "offer", "sdp": "a" * (128 * 1024)}
    envelope = seal_description(description, context, host, guest.encryption_public)
    assert len(canonical(envelope)) < 256 * 1024
    assert open_description(envelope, context, guest, kind="offer", signer=host.signing_public) == description
    for sdp in ("a" * (128 * 1024 + 1), "\u2603", ""):
        with pytest.raises(SetupError):
            seal_description({"type": "offer", "sdp": sdp}, context, host, guest.encryption_public)


def _report(context):
    return {"v": 1, "context": context, "role": "guest", "status": "connected",
            "failure_code": None, "sent_messages": 4, "received_messages": 5,
            "direct": True, "local_candidate_type": "host", "remote_candidate_type": "srflx",
            "protocol": "udp", "cleanup": {"peer_closed": True, "owned_tasks_pending": 0}}


def test_safe_report_rejects_private_fields_and_false_success(setup):
    _, _, context = setup
    original = _report(context)
    assert validate_report(original, context) == original
    for name, value in [("sdp", "private"), ("protocol", "tcp"), ("direct", False),
                        ("sent_messages", 0), ("received_messages", True),
                        ("remote_candidate_type", "relay"), ("failure_code", []),
                        ("cleanup", {"peer_closed": True, "owned_tasks_pending": 1})]:
        changed = copy.deepcopy(original)
        changed[name] = value
        with pytest.raises(SetupError):
            validate_report(changed, context)
    failed = {**original, "status": "failed", "failure_code": "connect_timeout"}
    assert validate_report(failed, context) == failed


def test_public_poker_receipt_requires_exact_settled_public_balances(setup):
    _, _, context = setup
    poker = {"hands": 10, "balances": [[10000, 10000]] * 10, "proofs_verified": 2,
             "deal_policy": "bayer-groth-v1", "matching_balances": True}
    original = {**_report(context), "sent_messages": 300, "received_messages": 300, "poker": poker}
    assert validate_report(original, context) == original
    for key, value in [("hands", True), ("hands", 11), ("proofs_verified", 0), ("proofs_verified", 1),
                       ("deal_policy", "commit-reveal"), ("matching_balances", False),
                       ("balances", [[10000, 10000]] * 9), ("balances", [[10000, 9999]] * 10),
                       ("balances", [[True, 19999]] * 10), ("hole", ["AS", "AH"])]:
        changed = copy.deepcopy(original)
        changed["poker"][key] = value
        with pytest.raises(SetupError):
            validate_report(changed, context)
