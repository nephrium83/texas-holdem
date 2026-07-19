"""Pins holdem/p2p/shuffle_proof.py -- shadow-deck cut-and-choose proof.

Completeness: an honest shuffle proves and verifies. Soundness (the
point): a cheating shuffler whose output is NOT a clean permutation of
the input cannot produce a proof that verifies -- tested by tampering the
output and by tampering individual openings. Plus binding: wrong context,
wrong decks, wrong k all fail.

Most tests use a small k for speed; test_default_k_128 exercises the real
k=128 once end-to-end.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
    from holdem.p2p import elgamal as eg
    from holdem.p2p import shuffle_mp as sh
    from holdem.p2p import shuffle_proof as sp
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


K = 24                       # small but > enough to make accidental pass ~2^-24


def _pk(n_seats=3):
    xs = [R.random_scalar() for _ in range(n_seats)]
    return eg.joint_public_key([R.mul_base(x) for x in xs])


def _shuffled(pk):
    """A trivial-start deck and one honest shuffle of it, with witness."""
    in_deck = eg.make_trivial_deck()
    out_deck, wit = sh.shuffle_deck(pk, in_deck)
    return in_deck, out_deck, wit


# --------------------------------------------------------------- completeness

def test_honest_proof_verifies():
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars,
                     ctx=b"hand-1", k=K)
    assert sp.verify(pk, in_deck, out_deck, proof, ctx=b"hand-1", k=K) is True


def test_proof_has_k_shadows_and_openings():
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars, k=K)
    assert len(proof.shadows) == K
    assert len(proof.openings) == K
    # openings carry both bit values across k shadows (overwhelmingly likely)
    bits = {op.bit for op in proof.openings}
    assert bits == {0, 1}


def test_proofs_are_randomised():
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    p1 = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars, k=K)
    p2 = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars, k=K)
    # different shadow randomness -> different committed shadows
    assert bytes(p1.shadows[0][0].c0) != bytes(p2.shadows[0][0].c0)
    assert sp.verify(pk, in_deck, out_deck, p1, k=K)
    assert sp.verify(pk, in_deck, out_deck, p2, k=K)


@pytest.mark.parametrize("k", [1, 2, 8, 128])
def test_various_k(k):
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars, k=k)
    assert sp.verify(pk, in_deck, out_deck, proof, k=k) is True


def test_default_k_128():
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars)   # DEFAULT_K
    assert sp.verify(pk, in_deck, out_deck, proof) is True
    assert len(proof.shadows) == sp.DEFAULT_K == 128


# --------------------------------------------------------------- soundness

def test_corrupt_output_cannot_be_proven():
    """A shuffler that duplicates a card in its output: the (perm,scalars)
    witness no longer explains out_deck, so the bridge openings fail and
    the proof does not verify."""
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    corrupt = list(out_deck)
    corrupt[10] = corrupt[3]                     # duplicate
    # prover still has only the witness for the ORIGINAL out_deck
    proof = sp.prove(pk, in_deck, corrupt, wit.perm, wit.scalars, k=K)
    assert sp.verify(pk, in_deck, corrupt, proof, k=K) is False


def test_substituted_output_cannot_be_proven():
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    corrupt = list(out_deck)
    corrupt[0] = eg.encrypt(pk, R.mul_base(R.random_scalar()))   # foreign card
    proof = sp.prove(pk, in_deck, corrupt, wit.perm, wit.scalars, k=K)
    assert sp.verify(pk, in_deck, corrupt, proof, k=K) is False


def test_verify_rejects_output_tampered_after_proof():
    """Honest proof, then the output deck is swapped under the verifier."""
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars, k=K)
    tampered = list(out_deck)
    tampered[5] = eg.reencrypt(pk, tampered[5])    # re-randomise one position
    assert sp.verify(pk, in_deck, tampered, proof, k=K) is False


def test_tampered_shadow_rejected():
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars, k=K)
    shadows = [list(D) for D in proof.shadows]
    shadows[0][0] = eg.reencrypt(pk, shadows[0][0])
    bad = sp.ShuffleProof(shadows=shadows, openings=proof.openings)
    # altering a shadow changes the challenge, desyncing every opened bit
    assert sp.verify(pk, in_deck, out_deck, bad, k=K) is False


def test_tampered_opening_scalar_rejected():
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars, k=K)
    ops = list(proof.openings)
    victim = ops[0]
    sc = list(victim.scalars)
    sc[0] = R.scalar_add(sc[0], R.random_scalar())
    ops[0] = sp.ShadowOpening(bit=victim.bit, perm=victim.perm, scalars=sc)
    bad = sp.ShuffleProof(shadows=proof.shadows, openings=ops)
    assert sp.verify(pk, in_deck, out_deck, bad, k=K) is False


def test_flipped_opening_bit_rejected():
    """Claiming the opposite bit (without the data to back it) fails: the
    recomputed challenge bit won't match the claimed one."""
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars, k=K)
    ops = list(proof.openings)
    v = ops[0]
    ops[0] = sp.ShadowOpening(bit=1 - v.bit, perm=v.perm, scalars=v.scalars)
    bad = sp.ShuffleProof(shadows=proof.shadows, openings=ops)
    assert sp.verify(pk, in_deck, out_deck, bad, k=K) is False


def test_non_permutation_opening_rejected():
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars, k=K)
    ops = list(proof.openings)
    v = ops[0]
    ops[0] = sp.ShadowOpening(bit=v.bit, perm=[0] * len(v.perm), scalars=v.scalars)
    bad = sp.ShuffleProof(shadows=proof.shadows, openings=ops)
    assert sp.verify(pk, in_deck, out_deck, bad, k=K) is False


# --------------------------------------------------------------- binding

def test_wrong_context_rejected():
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars,
                     ctx=b"hand-1", k=K)
    assert sp.verify(pk, in_deck, out_deck, proof, ctx=b"hand-2", k=K) is False


def test_wrong_k_rejected():
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars, k=K)
    assert sp.verify(pk, in_deck, out_deck, proof, k=K + 1) is False


def test_wrong_input_deck_rejected():
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars, k=K)
    other_in = eg.make_trivial_deck()
    other_in[0] = eg.reencrypt(pk, other_in[0])
    assert sp.verify(pk, other_in, out_deck, proof, k=K) is False


# --------------------------------------------------------------- witness guard

def test_prove_rejects_bad_perm():
    pk = _pk()
    in_deck, out_deck, wit = _shuffled(pk)
    with pytest.raises(ValueError):
        sp.prove(pk, in_deck, out_deck, [0] * len(in_deck), wit.scalars, k=K)


def test_end_to_end_with_audit_consistency():
    """The proof accepts exactly what a subsequent audit would find clean:
    prove+verify a real shuffle, then confirm the same deck decrypts to 52
    cards (i.e. proof acceptance and audit acceptance agree on honest)."""
    from collections import Counter
    xs = [R.random_scalar() for _ in range(3)]
    Xs = [R.mul_base(x) for x in xs]
    pk = eg.joint_public_key(Xs)
    in_deck = eg.make_trivial_deck()
    out_deck, wit = sh.shuffle_deck(pk, in_deck)

    proof = sp.prove(pk, in_deck, out_deck, wit.perm, wit.scalars, k=K)
    assert sp.verify(pk, in_deck, out_deck, proof, k=K)

    cards = []
    for ct in out_deck:
        shares = [eg.partial_decrypt(ct, x) for x in xs]
        cards.append(eg.point_to_card(eg.combine(ct, shares)))
    assert Counter(cards) == Counter(eg.CARDS)


if __name__ == "__main__":
    passed = total = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        marks = getattr(fn, "pytestmark", [])
        params = None
        for m in marks:
            if m.name == "parametrize":
                params = m.args[1]
        cases = params if params else [None]
        for c in cases:
            total += 1
            try:
                fn(c) if params else fn()
                passed += 1
                print(f"  {name}{'['+str(c)+']' if params else ''}: ok")
            except Exception as exc:
                print(f"  {name}{'['+str(c)+']' if params else ''}: FAIL - {exc}")
    print(f"{passed}/{total} passed")
