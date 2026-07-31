"""Pins rejection of degenerate key shares in the mental-poker ceremony.

X = identity means x = 0, a discrete log everyone knows, so a Schnorr
proof-of-possession for it is trivially forgeable: pick k and send
(k*G, k), because s*G == R + c*identity reduces to s*G == R. A seat
sending one contributes nothing to the joint key, and a table where every
seat did would agree on an identity joint key -- under which ElGamal
degenerates to C1 = M + r*identity = M and the whole deck is public.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import elgamal as eg
    from holdem.p2p import keygen_pop
    from holdem.p2p import ristretto as R
    from holdem.p2p.mental_deal import MentalDeal, Phase
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


CTX = b"poker.dkg.v1|session|1|0"


def forged_identity_pop():
    """A PoP that passes the Schnorr equation for X = identity.

    s*G == R + c*identity == R, so choosing R = k*G and s = k satisfies it
    without knowing anything. This is the proof the ceremony must refuse.
    """
    k = R.random_scalar()
    return bytes(R.mul_base(k)) + bytes(k)


def test_the_forgery_actually_satisfies_the_schnorr_equation():
    """Guards the test itself: if this stopped being forgeable the rejection
    below would pass for the wrong reason."""
    proof = forged_identity_pop()
    commitment = R.point_from_bytes(proof[:32])
    s = R.Scalar(proof[32:])
    c = keygen_pop._challenge(R.IDENTITY, commitment, CTX)
    lhs = R.mul_base_safe(s)
    rhs = R.add(commitment, R.mul_safe(c, R.IDENTITY))
    assert bytes(lhs) == bytes(rhs)


def test_identity_share_is_rejected():
    assert keygen_pop.verify(R.IDENTITY, forged_identity_pop(), CTX) is False


def test_honest_share_still_verifies():
    x = R.random_scalar()
    assert keygen_pop.verify(R.mul_base(x), keygen_pop.prove(x, CTX), CTX)


def test_verify_all_names_the_identity_seat():
    xs = [R.random_scalar(), R.random_scalar()]
    shares = [R.mul_base(xs[0]), R.IDENTITY]
    proofs = [keygen_pop.prove(xs[0], b"s|0"), forged_identity_pop()]
    assert keygen_pop.verify_all(shares, proofs, lambda i: f"s|{i}".encode()) \
        == [1]


def test_ceremony_aborts_and_blames_the_offending_seat():
    """End to end: an identity announcement must fail the hand closed with
    attribution, not merely be ignored."""
    deal = MentalDeal(session_id="session", hand_no=1, seat=0,
                      seats_in=[0, 1], button=0, master_secret=b"m")
    deal.start()
    deal.handle({"type": "key_announce", "seat": 1,
                 "X_hex": bytes(R.IDENTITY).hex(),
                 "pop_hex": forged_identity_pop().hex()})
    assert deal.phase == Phase.ABORTED
    assert deal.bad_seat == 1
    assert "proof-of-possession" in deal.abort_reason


def test_identity_joint_key_is_refused():
    """Defence in depth: even if a degenerate share reached the sum, the
    ceremony must not hand an identity key to the shuffle. Otherwise the
    first re-encryption raises an uncaught ValueError mid-protocol."""
    deal = MentalDeal(session_id="session", hand_no=1, seat=0,
                      seats_in=[0, 1], button=0, master_secret=b"m")
    deal.start()
    # Force the degenerate sum directly: seat 1's share is the negation of
    # seat 0's, so the shares cancel. No PoP could produce this, which is
    # exactly why the check is defence in depth rather than reachable.
    deal._pubkeys[1] = R.sub(R.IDENTITY, deal._pubkeys[0])
    deal._finish_keygen()
    assert deal.phase == Phase.ABORTED
    assert "degenerate" in deal.abort_reason


def test_elgamal_under_identity_key_would_have_leaked():
    """Why the checks above matter: with an identity public key the
    ciphertext's second component IS the plaintext."""
    with pytest.raises(ValueError):
        eg.encrypt(R.IDENTITY, eg._CARD_POINTS[0], R.random_scalar())
