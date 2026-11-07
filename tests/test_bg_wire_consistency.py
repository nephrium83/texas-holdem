"""bg_wire.decode enforces a self-consistent shape even WITHOUT explicit
dimensions (audit #133, handed over from Codex). Every dimension-derived
list is mutated by one-element insertion and removal, and each mutant is
decoded with full, partial and no dimensions."""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crypto_gate import initialize_for_tests, NativeCryptoUnavailable

try:
    initialize_for_tests()
    from holdem.p2p import bg_shuffle, bg_wire, elgamal as eg
    from holdem.p2p import ristretto as R
    from holdem.p2p.pedersen import CommitmentKey
    from holdem.p2p.shuffle_mp import random_permutation
except NativeCryptoUnavailable as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}", allow_module_level=True)

M, N = 4, 13

# Every list whose length the shape (m, n) implies.
LISTS = [
    ("a_commits",), ("b_commits",),
    ("product", "hadamard", "c_B_interior"),
    ("product", "hadamard", "zero", "c_D"),
    ("product", "hadamard", "zero", "a_tilde"),
    ("product", "hadamard", "zero", "b_tilde"),
    ("product", "svp", "a_tilde"),
    ("product", "svp", "b_tilde"),
    ("multi", "commit_b_k"),
    ("multi", "vector_e_k"),
    ("multi", "a_blinded"),
]
DIMENSIONS = [("full", (M, N)), ("none", (None, None)),
              ("m only", (M, None)), ("n only", (None, N))]


@pytest.fixture(scope="module")
def encoded():
    ck = CommitmentKey.generate(N, seed=b"bg-wire-consistency")
    pk = R.mul_base(R.scalar_reduce(hashlib.sha512(b"pk").digest()))
    in_deck = eg.make_trivial_deck()
    perm = random_permutation(len(in_deck))
    scalars = [R.random_scalar() for _ in range(len(in_deck))]
    out_deck = [eg.reencrypt(pk, in_deck[src], scalars[i]) for i, src in enumerate(perm)]
    proof = bg_shuffle.prove(pk, ck, in_deck, out_deck, perm, scalars, M, N, b"ctx")
    # Through real JSON, as it travels.
    return json.loads(json.dumps(bg_wire.encode(proof))), proof


def _target(data, path):
    node = data
    for key in path[:-1]:
        node = node[key]
    return node, path[-1]


@pytest.mark.parametrize("label,dims", DIMENSIONS)
def test_an_honest_proof_decodes_with_any_dimension_hint(encoded, label, dims):
    data, proof = encoded
    assert bg_wire.decode(copy.deepcopy(data), *dims) == proof


@pytest.mark.parametrize("path", LISTS, ids=lambda p: ".".join(p))
@pytest.mark.parametrize("change", ["insert", "remove"])
@pytest.mark.parametrize("label,dims", DIMENSIONS)
def test_a_list_out_of_step_with_the_shape_is_refused(encoded, path, change, label, dims):
    data, _ = encoded
    mutant = copy.deepcopy(data)
    node, key = _target(mutant, path)
    if change == "insert":
        node[key].append(copy.deepcopy(node[key][0]))
    else:
        node[key].pop()
    with pytest.raises(ValueError):
        bg_wire.decode(mutant, *dims)


@pytest.mark.parametrize("path", [("a_commits",), ("product", "svp", "a_tilde")],
                         ids=lambda p: ".".join(p))
def test_inferred_dimensions_below_the_shuffle_minimum_are_refused(encoded, path):
    data, _ = encoded
    mutant = copy.deepcopy(data)
    node, key = _target(mutant, path)
    node[key] = node[key][:1]
    with pytest.raises(ValueError, match=">= 2"):
        bg_wire.decode(mutant)
