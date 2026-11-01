"""Cryptographic input validation: finding N1 (small-order Ed25519 keys and
nonces), audit #66 (non-canonical scalars) and audit #50 (a "never raises"
verifier that raised).
"""
import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    importlib.import_module("holdem.p2p.elgamal")       # libsodium guard
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}", allow_module_level=True)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from holdem.p2p import dleq, keygen_pop, ristretto as R, signing, wire

L = 2**252 + 27742317777372353535851937790883648493   # Ristretto255 group order
SMALL_ORDER = [bytes.fromhex(h) for h in (
    "0000000000000000000000000000000000000000000000000000000000000000",
    "0100000000000000000000000000000000000000000000000000000000000000",
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
)]
IDENTITY = SMALL_ORDER[1]


def _keypair():
    sk = Ed25519PrivateKey.generate()
    return sk, sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


# ------------------------------------------------------------------ N1

def test_identity_key_universal_forgery_is_refused():
    """Before: R=identity, S=0 'verified' under the identity public key for
    every single message. Now: never."""
    keyless = IDENTITY + bytes(32)
    assert not any(signing.verify(IDENTITY, f"m{i}".encode(), keyless)
                   for i in range(500))


@pytest.mark.parametrize("key", SMALL_ORDER)
@pytest.mark.parametrize("sign_bit", [False, True])
def test_every_small_order_public_key_is_refused(key, sign_bit):
    if sign_bit:
        key = key[:31] + bytes([key[31] | 0x80])
    for nonce in SMALL_ORDER:
        sig = nonce + bytes(32)
        assert not any(signing.verify(key, f"m{i}".encode(), sig) for i in range(64))


def test_small_order_nonce_is_refused_even_under_an_honest_key():
    _, pk = _keypair()
    for nonce in SMALL_ORDER:
        assert not signing.verify(pk, b"msg", nonce + bytes(32))


def test_non_canonical_public_key_is_refused():
    p = 2**255 - 19
    for y in (p + 2, p + 10, 2**255 - 1):
        assert not signing.verify(y.to_bytes(32, "little"), b"m", bytes(64))


def test_honest_signatures_still_verify():
    for _ in range(20):
        sk, pk = _keypair()
        sig = sk.sign(b"hello")
        assert signing.verify(pk, b"hello", sig)
        assert signing.verify(bytearray(pk), b"hello", bytearray(sig))
        assert not signing.verify(pk, b"hellO", sig)


@pytest.mark.parametrize("pk,sig", [(b"", b""), (bytes(31), bytes(64)),
                                     (bytes(32), bytes(63)), ("ab" * 16, bytes(64)),
                                     (None, None)])
def test_malformed_inputs_are_false_not_exceptions(pk, sig):
    assert signing.verify(pk, b"m", sig) is False


class _Clock:
    def time(self):
        return 1_800_000_000.0


def test_keyless_envelope_is_refused_end_to_end():
    """A complete, hash-consistent envelope 'signed' by the identity key with
    the keyless signature. The old verify let wire.unpack accept it whole."""
    msg = {"v": 1, "type": "chat", "payload": {"text": "hi"},
           "pubkey": IDENTITY.hex(), "ts": 1_800_000_000_000, "prev": "0" * 64}
    msg["sig"] = (IDENTITY + bytes(32)).hex()
    full = json.dumps(msg, sort_keys=True, separators=(",", ":")).encode()
    msg["hash"] = hashlib.sha256(full).hexdigest()
    with pytest.raises(ValueError, match="Invalid signature"):
        wire.unpack(json.dumps(msg).encode(), clock=_Clock())


def test_ice_signaling_uses_the_hardened_verify():
    from holdem import ice_signaling
    value = {"v": 1, "purpose": "x"}
    value["signature"] = (IDENTITY + bytes(32)).hex()
    with pytest.raises(ice_signaling.SetupError):
        ice_signaling._verify(value, IDENTITY.hex())


# ------------------------------------------------------------------ #66

@pytest.mark.parametrize("value", [L, L + 1, L + 12345, 2**256 - 1])
def test_non_canonical_scalars_are_refused(value):
    raw = value.to_bytes(32, "little")
    with pytest.raises(ValueError, match="non-canonical"):
        R.Scalar(raw)
    with pytest.raises(ValueError, match="non-canonical"):
        R.scalar_from_bytes(raw)


def test_canonical_edge_scalars_are_accepted():
    for value in (0, 1, L - 1):
        assert int.from_bytes(R.Scalar(value.to_bytes(32, "little")), "little") == value


def _malleate(scalar_bytes):
    """The same field element, encoded non-canonically (s + L)."""
    return (int.from_bytes(scalar_bytes, "little") + L).to_bytes(32, "little")


def test_dleq_proof_cannot_be_malleated_with_a_non_canonical_response():
    x = R.random_scalar()
    X = R.mul_base(x)
    C0 = R.mul_base(R.random_scalar())
    D = R.mul(x, C0)
    proof = dleq.prove(x, C0)
    assert dleq.verify(X, D, C0, proof)
    assert not dleq.verify(X, D, C0, proof[:32] + _malleate(proof[32:]))
    assert not dleq.verify(X, D, C0, _malleate(proof[:32]) + proof[32:])


def test_proof_of_possession_cannot_be_malleated():
    x = R.random_scalar()
    X = R.mul_base(x)
    proof = keygen_pop.prove(x, b"ctx")
    assert keygen_pop.verify(X, proof, b"ctx")
    assert not keygen_pop.verify(X, proof[:32] + _malleate(proof[32:]), b"ctx")


# ------------------------------------------------------------------ #50

class _Junk:
    pass


@pytest.mark.parametrize("proof", [None, _Junk(), 42, "proof"])
def test_hadamard_verify_rejects_malformed_proofs_without_raising(proof):
    from holdem.p2p import bg_hadamard
    from holdem.p2p.pedersen import CommitmentKey
    ck = CommitmentKey.generate(4)
    c_A = [R.mul_base(R.random_scalar()) for _ in range(3)]
    assert bg_hadamard.verify(ck, c_A, c_A[0], 4, b"ctx", proof) is False


@pytest.mark.parametrize("c_A", [None, [None, None, None], 7])
def test_hadamard_verify_rejects_malformed_statements_without_raising(c_A):
    from holdem.p2p import bg_hadamard
    from holdem.p2p.pedersen import CommitmentKey
    ck = CommitmentKey.generate(4)
    assert bg_hadamard.verify(ck, c_A, R.IDENTITY, 4, b"ctx", _Junk()) is False
