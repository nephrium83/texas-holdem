"""Pins holdem/p2p/bg_wire.py -- the Bayer-Groth shuffle proof wire codec.

The codec sits between a verifying peer and an untrusted sender, so the
properties that matter are: a real proof survives a JSON round trip and
still verifies, and every malformed input raises ValueError rather than
producing a partly-built proof or leaking a different exception type.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import bg_shuffle, bg_wire, elgamal as eg
    from holdem.p2p import ristretto as R
    from holdem.p2p.pedersen import CommitmentKey
    from holdem.p2p.shuffle_mp import random_permutation
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


M, N = 4, 13


@pytest.fixture(scope="module")
def statement():
    """One real 52-card proof plus everything needed to verify it.

    Module-scoped: proving costs ~130 ms and every test here reuses the
    same proof.
    """
    ck = CommitmentKey.generate(N, seed=b"bg-wire-test")
    pk = R.mul_base(R.scalar_reduce(hashlib.sha512(b"pk").digest()))
    in_deck = eg.make_trivial_deck()
    perm = random_permutation(len(in_deck))
    scalars = [R.random_scalar() for _ in range(len(in_deck))]
    out_deck = [eg.reencrypt(pk, in_deck[src], scalars[i])
                for i, src in enumerate(perm)]
    ctx = b"bg-wire-test-context"
    proof = bg_shuffle.prove(pk, ck, in_deck, out_deck, perm, scalars,
                             M, N, ctx)
    return {"ck": ck, "pk": pk, "in_deck": in_deck, "out_deck": out_deck,
            "ctx": ctx, "proof": proof}


# ------------------------------------------------------------- round trip

def test_round_trip_is_exact(statement):
    encoded = bg_wire.encode(statement["proof"])
    assert bg_wire.decode(encoded, M, N) == statement["proof"]


def test_round_trip_survives_json(statement):
    """The codec's whole point: it must pass through real JSON, which is
    what the signed wire envelope actually carries."""
    blob = json.dumps(bg_wire.encode(statement["proof"]))
    assert bg_wire.decode(json.loads(blob), M, N) == statement["proof"]


def test_decoded_proof_still_verifies(statement):
    decoded = bg_wire.decode(
        json.loads(json.dumps(bg_wire.encode(statement["proof"]))), M, N)
    assert bg_shuffle.verify(
        statement["pk"], statement["ck"], statement["in_deck"],
        statement["out_deck"], M, N, statement["ctx"], decoded)


def test_identity_component_round_trips(statement):
    """ZeroProof.c_D[m+1] is the identity by construction, and a Pedersen
    commitment to zero with zero randomness is the identity too. The
    all-zero Ristretto encoding must therefore survive decoding -- a
    decoder that rejected it would reject every valid proof."""
    c_D = statement["proof"].product.hadamard.zero.c_D
    identities = [i for i, p in enumerate(c_D)
                  if bytes(p) == bytes(R.IDENTITY)]
    assert identities == [M + 1], "expected exactly c_D[m+1] to be identity"
    decoded = bg_wire.decode(bg_wire.encode(statement["proof"]), M, N)
    assert bytes(decoded.product.hadamard.zero.c_D[M + 1]) == \
        bytes(R.IDENTITY)


def test_encoding_is_json_safe(statement):
    """Every leaf must be a str or a list -- no bytes, no tuples that only
    survive because json happens to coerce them."""
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert isinstance(key, str)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        else:
            assert isinstance(node, str), f"non-string leaf: {type(node)}"

    walk(bg_wire.encode(statement["proof"]))


# ------------------------------------------------------------- rejections

def test_decode_rejects_non_object():
    for bad in ([], "proof", 7, None):
        with pytest.raises(ValueError):
            bg_wire.decode(bad, M, N)


def test_decode_rejects_missing_field(statement):
    for field in ("a_commits", "b_commits", "product", "multi"):
        encoded = bg_wire.encode(statement["proof"])
        del encoded[field]
        with pytest.raises(ValueError):
            bg_wire.decode(encoded, M, N)


def test_decode_rejects_missing_nested_field(statement):
    encoded = bg_wire.encode(statement["proof"])
    del encoded["product"]["hadamard"]["zero"]["r_tilde"]
    with pytest.raises(ValueError):
        bg_wire.decode(encoded, M, N)


def test_decode_rejects_bad_hex(statement):
    encoded = bg_wire.encode(statement["proof"])
    encoded["a_commits"][0] = "not-hex-at-all"
    with pytest.raises(ValueError):
        bg_wire.decode(encoded, M, N)


def test_decode_rejects_wrong_length_point(statement):
    encoded = bg_wire.encode(statement["proof"])
    encoded["a_commits"][0] = "00" * 31
    with pytest.raises(ValueError):
        bg_wire.decode(encoded, M, N)


def test_decode_rejects_wrong_type_leaf(statement):
    encoded = bg_wire.encode(statement["proof"])
    encoded["multi"]["r_blinded"] = 12345
    with pytest.raises(ValueError):
        bg_wire.decode(encoded, M, N)


def test_decode_rejects_array_where_object_expected(statement):
    encoded = bg_wire.encode(statement["proof"])
    encoded["product"] = []
    with pytest.raises(ValueError):
        bg_wire.decode(encoded, M, N)


def test_decode_rejects_object_where_array_expected(statement):
    encoded = bg_wire.encode(statement["proof"])
    encoded["a_commits"] = {"0": "00" * 32}
    with pytest.raises(ValueError):
        bg_wire.decode(encoded, M, N)


def test_decode_rejects_malformed_ciphertext(statement):
    encoded = bg_wire.encode(statement["proof"])
    encoded["multi"]["vector_e_k"][0] = ["00" * 32]      # one element, not two
    with pytest.raises(ValueError):
        bg_wire.decode(encoded, M, N)


@pytest.mark.parametrize("field,parent", [
    ("a_commits", None),
    ("b_commits", None),
    ("commit_b_k", "multi"),
    ("vector_e_k", "multi"),
    ("a_blinded", "multi"),
])
def test_decode_enforces_declared_dimensions(statement, field, parent):
    """Supplying (m, n) must pin every list length, so a structurally wrong
    proof is rejected at the codec rather than deeper in verification."""
    encoded = bg_wire.encode(statement["proof"])
    target = encoded if parent is None else encoded[parent]
    target[field] = target[field][:-1]
    with pytest.raises(ValueError):
        bg_wire.decode(encoded, M, N)


def test_decode_without_dimensions_accepts_self_consistent(statement):
    """Dimensions are optional; omitting them still round-trips."""
    encoded = bg_wire.encode(statement["proof"])
    assert bg_wire.decode(encoded) == statement["proof"]


def test_decode_rejects_degenerate_dimensions(statement):
    encoded = bg_wire.encode(statement["proof"])
    with pytest.raises(ValueError):
        bg_wire.decode(encoded, 1, N)


def test_decode_rejects_oversized_list(statement):
    """Allocation defence against a hostile peer when no dimensions are
    supplied to pin the shape."""
    encoded = bg_wire.encode(statement["proof"])
    encoded["a_commits"] = ["00" * 32] * 5000
    with pytest.raises(ValueError):
        bg_wire.decode(encoded)
