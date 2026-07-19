"""Shadow-deck cut-and-choose shuffle proof (adopted-plan step 3).

A non-interactive zero-knowledge argument that a shuffled deck C' is a
genuine permutation-and-re-encryption of an input deck C -- WITHOUT
revealing the permutation or the re-encryption randomness. This is the
*prevention* layer: the post-hand audit (deck_audit.py) catches a cheat
after the fact, but a shuffler must attach this proof to its output for
its shuffle to be accepted at all.

Construction (Sako-Kilian shadow-deck cut-and-choose)
-----------------------------------------------------
Real shuffle, kept secret:  C'[i] = reencrypt(C[pi[i]], rho[i]).

The prover generates k independent *shadow* shuffles of the SAME input:
    D_j[i] = reencrypt(C[sigma_j[i]], r_j[i])          j = 1..k
and commits to them by hashing. A Fiat-Shamir challenge derives k bits
b_1..b_k from the complete transcript. For each shadow j:

  b_j = 0 : open the shadow itself -- reveal (sigma_j, r_j); the verifier
            recomputes D_j from C and checks it matches.
  b_j = 1 : open the BRIDGE from the shadow to the real output -- reveal
            phi_j[i] = sigma_j^{-1}[pi[i]]  and
            delta_j[i] = rho[i] - r_j[phi_j[i]];
            the verifier checks C'[i] == reencrypt(D_j[phi_j[i]], delta_j[i]).

Why it is sound (the whole argument, in a paragraph)
----------------------------------------------------
Answering BOTH sides for any single shadow j composes into a valid
(permutation, randomizer) witness for C -> C': side 0 proves D_j is a
clean shuffle of C, side 1 proves C' is a clean shuffle of D_j, and the
composition pi = sigma_j . phi_j is a permutation with the summed
randomizers. So if C -> C' is NOT a valid shuffle, for every shadow the
prover can satisfy at most one of the two sides; a transcript that
verifies therefore requires the challenge bit to land on the satisfiable
side for all k shadows -- probability 2^-k per Fiat-Shamir hash query.

Zero-knowledge: a b=0 opening reveals a fresh uniform (sigma_j, r_j)
independent of the secret pi; a b=1 opening reveals phi_j = sigma_j^{-1}.pi,
which -- because sigma_j is uniform and independent of pi -- is itself a
uniform permutation revealing nothing about pi, plus delta_j masked by
the fresh r_j. Each opening is trivially simulatable.

k is a FULL security parameter, not statistical
-----------------------------------------------
Under Fiat-Shamir the prover grinds offline: resample one shadow, rehash,
retry, at ~one hash + O(N) point-mults per attempt. So a false statement
is accepted with probability ~ q * 2^-k for q hash queries. k = 128 (see
DEFAULT_K). Do NOT lower it to an interactive-style 40.

No commitment key exists anywhere in this construction, so the Scytl /
Swiss Post trapdoor-commitment bug class is structurally impossible here.

Assumptions: DDH (already relied on by the ElGamal layer) + random oracle.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Sequence

from holdem.p2p import ristretto as R
from holdem.p2p.ristretto import Scalar
from holdem.p2p.elgamal import Ciphertext, reencrypt
from holdem.p2p.shuffle_mp import shuffle_deck, inverse_permutation


DEFAULT_K = 128
_DOMAIN = b"poker.shuffleproof.shadow.v1"


@dataclass(frozen=True)
class ShadowOpening:
    """The opening for one shadow, selected by its challenge bit.

    Exactly one of the two forms is populated:
      bit 0 -> (perm, scalars)  = (sigma_j, r_j), the shadow's own witness
      bit 1 -> (perm, scalars)  = (phi_j, delta_j), the bridge to C'
    Same field shapes either way (a permutation + one scalar per position),
    so the wire format is uniform and the bit alone says how to check it.
    """
    bit: int
    perm: List[int]
    scalars: List[Scalar]


@dataclass(frozen=True)
class ShuffleProof:
    """A shadow-deck cut-and-choose proof for one shuffle C -> C'."""
    shadows: List[List[Ciphertext]]     # the k committed shadow decks
    openings: List[ShadowOpening]       # one opening per shadow


def _hash_deck(h: "hashlib._Hash", deck: Sequence[Ciphertext]) -> None:
    for ct in deck:
        h.update(bytes(ct.c0))
        h.update(bytes(ct.c1))


def _challenge_bits(
    ctx: bytes,
    in_deck: Sequence[Ciphertext],
    out_deck: Sequence[Ciphertext],
    shadows: Sequence[Sequence[Ciphertext]],
    k: int,
) -> List[int]:
    """k Fiat-Shamir challenge bits over the COMPLETE transcript.

    ``ctx`` binds the proof to its session context (session/hand/seat ids,
    joint pubkey) -- the caller supplies it; nothing the verifier checks
    is outside this hash.
    """
    h = hashlib.sha512()
    h.update(_DOMAIN)
    h.update(len(ctx).to_bytes(4, "big"))
    h.update(ctx)
    h.update(k.to_bytes(4, "big"))
    h.update(len(in_deck).to_bytes(4, "big"))
    _hash_deck(h, in_deck)
    _hash_deck(h, out_deck)
    for D in shadows:
        _hash_deck(h, D)
    seed = h.digest()

    # Expand to k bits with a counter-mode hash of the transcript digest.
    bits: List[int] = []
    counter = 0
    while len(bits) < k:
        block = hashlib.sha512(seed + counter.to_bytes(4, "big")).digest()
        for byte in block:
            for j in range(8):
                bits.append((byte >> j) & 1)
                if len(bits) == k:
                    break
            if len(bits) == k:
                break
        counter += 1
    return bits


def prove(
    pk: R.Point,
    in_deck: Sequence[Ciphertext],
    out_deck: Sequence[Ciphertext],
    perm: Sequence[int],
    scalars: Sequence[Scalar],
    ctx: bytes = b"",
    k: int = DEFAULT_K,
) -> ShuffleProof:
    """Prove out_deck = reencrypt-permute(in_deck) under (perm, scalars).

    ``(perm, scalars)`` is the real shuffle's witness (a ShuffleWitness's
    fields): out_deck[i] = reencrypt(in_deck[perm[i]], scalars[i]).
    ``ctx`` binds the proof to session context and MUST match at verify.
    """
    n = len(in_deck)
    if len(out_deck) != n:
        raise ValueError("in_deck and out_deck differ in length")
    if len(perm) != n or sorted(perm) != list(range(n)):
        raise ValueError("perm must be a permutation of range(len(in_deck))")
    if len(scalars) != n:
        raise ValueError("scalars must have one entry per position")

    # 1. generate k independent shadow shuffles of in_deck, retaining witnesses
    shadows: List[List[Ciphertext]] = []
    shadow_perms: List[List[int]] = []
    shadow_scalars: List[List[Scalar]] = []
    for _ in range(k):
        D, wit = shuffle_deck(pk, in_deck)
        shadows.append(D)
        shadow_perms.append(wit.perm)
        shadow_scalars.append(wit.scalars)

    # 2. Fiat-Shamir challenge over the full transcript
    bits = _challenge_bits(ctx, in_deck, out_deck, shadows, k)

    # 3. open each shadow per its bit
    openings: List[ShadowOpening] = []
    for j in range(k):
        sig = shadow_perms[j]
        r = shadow_scalars[j]
        if bits[j] == 0:
            openings.append(ShadowOpening(bit=0, perm=list(sig), scalars=list(r)))
        else:
            sig_inv = inverse_permutation(sig)
            # phi[i] = sigma^{-1}[pi[i]] ; delta[i] = rho[i] - r[phi[i]]
            phi = [sig_inv[perm[i]] for i in range(n)]
            delta = [R.scalar_sub(scalars[i], r[phi[i]]) for i in range(n)]
            openings.append(ShadowOpening(bit=1, perm=phi, scalars=delta))

    return ShuffleProof(shadows=shadows, openings=openings)


def verify(
    pk: R.Point,
    in_deck: Sequence[Ciphertext],
    out_deck: Sequence[Ciphertext],
    proof: ShuffleProof,
    ctx: bytes = b"",
    k: int = DEFAULT_K,
) -> bool:
    """Verify a shadow-deck cut-and-choose shuffle proof."""
    n = len(in_deck)
    if len(out_deck) != n:
        return False
    if len(proof.shadows) != k or len(proof.openings) != k:
        return False
    for D in proof.shadows:
        if len(D) != n:
            return False

    # recompute the challenge from the prover's committed shadows
    bits = _challenge_bits(ctx, in_deck, out_deck, proof.shadows, k)

    for j in range(k):
        op = proof.openings[j]
        D = proof.shadows[j]
        if op.bit != bits[j]:
            return False                    # opened the wrong side
        if len(op.perm) != n or len(op.scalars) != n:
            return False
        if sorted(op.perm) != list(range(n)):
            return False                    # not a permutation

        if bits[j] == 0:
            # D must be exactly reencrypt(in_deck[sigma[i]], r[i])
            for i in range(n):
                expect = reencrypt(pk, in_deck[op.perm[i]], op.scalars[i])
                if bytes(expect.c0) != bytes(D[i].c0) or \
                        bytes(expect.c1) != bytes(D[i].c1):
                    return False
        else:
            # out_deck[i] must be reencrypt(D[phi[i]], delta[i])
            for i in range(n):
                expect = reencrypt(pk, D[op.perm[i]], op.scalars[i])
                if bytes(expect.c0) != bytes(out_deck[i].c0) or \
                        bytes(expect.c1) != bytes(out_deck[i].c1):
                    return False

    return True


__all__ = ["DEFAULT_K", "ShadowOpening", "ShuffleProof", "prove", "verify"]
