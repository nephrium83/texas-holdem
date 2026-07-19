"""Pins holdem/p2p/keygen_pop.py -- Schnorr proof-of-possession.

The headline test constructs the real rogue-key attack and shows the PoP
blocks it. Plus standard sigma-protocol soundness: valid proof verifies,
every tamper/mismatch is rejected, context binding holds.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
    from holdem.p2p import keygen_pop as pop
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


# --------------------------------------------------------------- happy path

def test_valid_pop_verifies():
    x = R.random_scalar()
    X = R.mul_base(x)
    proof = pop.prove(x, ctx=b"session-1|seat-0")
    assert pop.verify(X, proof, ctx=b"session-1|seat-0") is True


def test_pop_is_64_bytes():
    assert len(pop.prove(R.random_scalar())) == pop.PROOF_BYTES == 64


def test_pops_are_randomised():
    x = R.random_scalar()
    X = R.mul_base(x)
    p1 = pop.prove(x, ctx=b"c")
    p2 = pop.prove(x, ctx=b"c")
    assert p1 != p2
    assert pop.verify(X, p1, ctx=b"c") and pop.verify(X, p2, ctx=b"c")


# --------------------------------------------------------------- THE attack

def test_rogue_key_attack_is_blocked():
    """Last announcer tries X_n = X_target - sum(others) to own the joint
    key. It has no known discrete log, so no valid PoP can be made for it."""
    # honest earlier seats
    xs = [R.random_scalar() for _ in range(3)]
    Xs = [R.mul_base(x) for x in xs]

    # attacker's desired joint key: one it fully controls
    x_star = R.random_scalar()
    X_target = R.mul_base(x_star)

    # rogue share = X_target - sum(others)
    acc = Xs[0]
    for X in Xs[1:]:
        acc = R.add(acc, X)
    X_rogue = R.sub(X_target, acc)

    # if accepted, the joint key would be exactly X_target (attacker-owned)
    joint = X_rogue
    for X in Xs:
        joint = R.add(joint, X)
    assert bytes(joint) == bytes(X_target)          # attack math works...

    # ...but the attacker cannot prove possession of X_rogue's discrete log.
    # Best they can do is prove for x_star (the log they DO know), whose
    # public point is X_target, not X_rogue -> verification fails.
    forged = pop.prove(x_star, ctx=b"s|seat-3")
    assert pop.verify(X_rogue, forged, ctx=b"s|seat-3") is False

    # and an honest verifier running verify_all flags exactly the rogue seat
    shares = Xs + [X_rogue]
    proofs = [pop.prove(x, ctx=f"s|seat-{i}".encode()) for i, x in enumerate(xs)]
    proofs.append(forged)
    bad = pop.verify_all(shares, proofs, ctx_for=lambda i: f"s|seat-{i}".encode())
    assert bad == [3]


# --------------------------------------------------------------- soundness

def test_wrong_share_rejected():
    x = R.random_scalar()
    proof = pop.prove(x, ctx=b"c")
    other_X = R.mul_base(R.random_scalar())
    assert pop.verify(other_X, proof, ctx=b"c") is False


def test_wrong_context_rejected():
    x = R.random_scalar()
    X = R.mul_base(x)
    proof = pop.prove(x, ctx=b"session-1|seat-0")
    assert pop.verify(X, proof, ctx=b"session-1|seat-1") is False
    assert pop.verify(X, proof, ctx=b"session-2|seat-0") is False


def test_tampered_commitment_rejected():
    x = R.random_scalar()
    X = R.mul_base(x)
    proof = bytearray(pop.prove(x, ctx=b"c"))
    tampered = bytes(R.add(R.point_from_bytes(bytes(proof[:32])), R.G)) + bytes(proof[32:])
    assert pop.verify(X, tampered, ctx=b"c") is False


def test_tampered_s_rejected():
    x = R.random_scalar()
    X = R.mul_base(x)
    proof = bytearray(pop.prove(x, ctx=b"c"))
    proof[40] ^= 0x01
    assert pop.verify(X, bytes(proof), ctx=b"c") is False


def test_wrong_length_rejected():
    X = R.mul_base(R.random_scalar())
    assert pop.verify(X, b"\x00" * 63, ctx=b"c") is False
    assert pop.verify(X, b"\x00" * 65, ctx=b"c") is False


def test_garbage_commitment_bytes_rejected():
    x = R.random_scalar()
    X = R.mul_base(x)
    proof = b"\xff" * 32 + bytes(pop.prove(x, ctx=b"c"))[32:]
    assert pop.verify(X, proof, ctx=b"c") is False


# --------------------------------------------------------------- ceremony

def test_verify_all_clean_ceremony():
    n = 4
    xs = [R.random_scalar() for _ in range(n)]
    Xs = [R.mul_base(x) for x in xs]
    ctx_for = lambda i: f"sess-A|seat-{i}".encode()
    proofs = [pop.prove(xs[i], ctx=ctx_for(i)) for i in range(n)]
    assert pop.verify_all(Xs, proofs, ctx_for) == []


def test_verify_all_flags_swapped_context():
    """A PoP made for the wrong seat index is caught by verify_all."""
    n = 3
    xs = [R.random_scalar() for _ in range(n)]
    Xs = [R.mul_base(x) for x in xs]
    ctx_for = lambda i: f"sess|seat-{i}".encode()
    proofs = [pop.prove(xs[i], ctx=ctx_for(i)) for i in range(n)]
    # seat 1 accidentally (or maliciously) proves under seat 0's context
    proofs[1] = pop.prove(xs[1], ctx=ctx_for(0))
    assert pop.verify_all(Xs, proofs, ctx_for) == [1]


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  {name}: ok")
            passed += 1
        except Exception as exc:
            print(f"  {name}: FAIL - {type(exc).__name__}: {exc}")
    print(f"{passed}/{len(fns)} passed")
