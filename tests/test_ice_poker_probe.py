"""The same own-seat acceptance driver runs from source and in the EXE."""
import asyncio

import pytest

pytest.importorskip("aiortc")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from crypto_gate import require_crypto
from holdem.ice_poker_probe import _self_test, capability
from holdem.ice_signaling import ProbeKeys


def test_acceptance_invite_is_private_and_bound_to_pinned_context():
    keys = [ProbeKeys(Ed25519PrivateKey.generate(), X25519PrivateKey.generate()) for _ in range(3)]
    context = {"repository": "test/proof", "run_id": "1", "run_attempt": "1", "nonce": "a" * 64,
               "source_commit": "b" * 40, "package_sha256": "c" * 64,
               "host_signing_public": keys[0].signing_public,
               "host_encryption_public": keys[0].encryption_public}
    first = capability(context, keys[0], keys[1].encryption_public)
    assert first == capability(context, keys[1], keys[0].encryption_public)
    assert first != capability(context, keys[2], keys[0].encryption_public)
    assert first != capability({**context, "run_attempt": "2"}, keys[0], keys[1].encryption_public)
    assert len(first[0]) == 16 and len(first[1]) == 8
    assert first[0] != bytes.fromhex(context["nonce"])[:16]


def test_two_independent_own_seat_drivers_complete_ten_hands_and_cleanup():
    require_crypto()
    result = asyncio.run(_self_test())
    assert result == {"schema": "direct-poker-self-test-v1", "status": "passed", "hands": 10,
                      "matching_balances": True, "proofs_verified": 2, "deal_policy": "bayer-groth-v1",
                      "peer_closed": True, "owned_tasks_pending": 0, "callback_workers_alive": 0}
