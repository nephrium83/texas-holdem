"""Bayer-Groth zero argument (paper section 5.2, Theorem 10).

Proves that a sequence of committed vectors satisfies a bilinear relation
summing to zero:

    0 = sum_{i=1..m} a_i * b_{i-1}

for a public bilinear map * : Z_q^n x Z_q^n -> Z_q, given commitments to
a_1..a_m and to b_0..b_{m-1}. Note the index ranges differ between the two
families -- that asymmetry is the main source of implementation error here.

This is the primitive underneath the Hadamard product argument (5.1), which
reduces "this committed matrix has these row products" to a zero relation.
It is NOT the single-value product argument in bg_svp.py (section 5.3),
which proves prod(a_i) = b for a single committed vector.

Protocol (restated additively)
------------------------------
The prover picks blinding vectors a_0, b_m at random, so both families run
0..m, and computes the diagonal coefficients

    d_k = sum over i,j in [0,m] with j = (m - k) + i  of  a_i * b_j

for k = 0..2m. Observe that d_{m+1} is exactly sum_{i=1..m} a_i * b_{i-1},
the quantity claimed to be zero, so the prover fixes both d_{m+1} = 0 and
t_{m+1} = 0, making that commitment the identity element -- something the
verifier can check without an opening.

After the challenge x the prover sends the folded openings

    a~ = sum_{i=0..m} x^i a_i          r~ = sum_{i=0..m} x^i r_i
    b~ = sum_{j=0..m} x^{m-j} b_j      s~ = sum_{j=0..m} x^{m-j} s_j
                                       t~ = sum_{k=0..2m} x^k t_k

and the verifier accepts if

    sum_{i=0..m} x^i * c_A[i]      == comck(a~; r~)
    sum_{j=0..m} x^{m-j} * c_B[j]  == comck(b~; s~)
    sum_{k=0..2m} x^k * c_D[k]     == comck(a~ * b~; t~)
    c_D[m+1]                       == identity

THE B FOLDING HAS NO SEPARATE BLINDER TERM. c_Bm is already the j = m term
of the second sum, at weight x^0 = 1; writing it as "c_Bm + sum_{j=0..m}"
counts it twice. The A folding may be written c_A0 + sum_{i=1..m} x^i c_A[i]
only because that sum genuinely starts at i = 1. The asymmetry is real and
copying one shape onto the other silently breaks the argument.

Soundness intuition: a~ * b~ expands to sum_k d_k x^k. The verifier fixes
the x^{m+1} coefficient to zero via the identity check, so by
Schwartz-Zippel over a degree-2m polynomial a prover has negligible chance
over x unless the relation genuinely holds.

Dimensions
----------
m >= 1 and n >= 1. bg_svp requires n >= 2 because section 5.3 forces
delta_1 = d_1 and delta_n = 0 at both ends, which collide at n = 1 and
destroy the blinding of a~_1. Section 5.2 has NO analogous collapse: its
only forced quantity is t_{m+1}, which never needs to be free because
d_{m+1} is publicly zero, and its blinders a_0, b_m are unconstrained
random vectors. At m = 1 the simulator of Theorem 10 still has {t_0, t_1}
free and a~ = a_0 + x*a_1, b~ = x*b_0 + b_1 are both uniform, so perfect
SHVZK holds. m = 0 is rejected: with no a_i the statement is vacuous.

Scope
-----
This is a standalone primitive. It knows nothing about the outer product
argument: it TAKES a BilinearMap rather than deriving the challenge y that
defines one, and it treats ``context`` as opaque bytes. Deriving y and
packing the outer transcript belong to the future section 5.1 adapter.

``prove`` takes commitments and their openings rather than committing its
own inputs, because section 5.1 hands this argument DERIVED commitments
(c_Di = x^i * c_Bi and so on) whose openings it computes itself. An API
that insisted on building its own commitments could not serve that caller.

Fiat-Shamir (the Scytl/SwissPost pitfall)
-----------------------------------------
The challenge hashes the complete transcript: domain tag, protocol version,
the full commitment key (every generator, not a seed reference), both
dimensions, the bilinear map's actual coefficients, the caller's context,
every statement commitment, and every initial prover message. Nothing the
verifier checks is outside the hash.

The map coefficients matter most. A proof valid under one map must not
verify under another with the same commitments -- omitting them is the 2019
Swiss Post incomplete-transcript failure, and it passes every completeness
test.

``n`` is bound because Pedersen commitments are zero-padded: a commitment
to (a, b) is byte-identical to one to (a, b, 0, 0), so the vector width is
not recoverable from the group elements and must be stated.

Every variable-length field is length-prefixed so that no two distinct
transcripts can share a preimage.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Sequence, Tuple

from holdem.p2p import ristretto as R
from holdem.p2p.ristretto import Point, Scalar
from holdem.p2p.bg_challenge import nonzero_challenge
from holdem.p2p.bg_witness import require_witness
from holdem.p2p.pedersen import CommitmentKey, commit


_DOMAIN = b"poker.bg.zero.v1"
_VERSION = 1

_ZERO = Scalar(b"\x00" * 32)
_ONE = Scalar(b"\x01" + b"\x00" * 31)     # scalars are little-endian


# --------------------------------------------------------------------------
# bilinear map
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BilinearMap:
    """The public map * : Z_q^n x Z_q^n -> Z_q, as an explicit coefficient
    vector: u * v = sum_j coefficients[j] * u_j * v_j.

    Section 5.1 instantiates this as sum_{j=1..n} u_j v_j y^j for a verifier
    challenge y -- note the exponent starts at 1, so 0-based index j carries
    y^{j+1}. Starting at y^0 would leave the first coordinate unweighted.

    Use ``from_challenge`` in production. The general constructor exists for
    tests: section 5.2 alone needs only bilinearity, but section 5.1's
    soundness rests specifically on the coefficients being powers of a
    randomly chosen y (its extraction is a Vandermonde argument over exactly
    that structure). A caller free to choose arbitrary coefficients could
    obtain a perfectly valid zero proof of nothing -- a partly-zero
    coefficient vector makes the relation trivially satisfiable -- so zero
    coefficients are rejected here regardless of how the map was built.
    """
    coefficients: Tuple[Scalar, ...]

    def __post_init__(self) -> None:
        if not self.coefficients:
            raise ValueError("bilinear map needs at least one coefficient")
        for c in self.coefficients:
            if R.is_zero_scalar(c):
                raise ValueError("bilinear map coefficients must be nonzero")

    @property
    def n(self) -> int:
        return len(self.coefficients)

    @staticmethod
    def from_challenge(y: Scalar, n: int) -> "BilinearMap":
        """The section 5.1 map: coefficients y^1 .. y^n."""
        if n < 1:
            raise ValueError("bilinear map needs n >= 1")
        if R.is_zero_scalar(y):
            raise ValueError("bilinear map challenge y must be nonzero")
        coeffs: List[Scalar] = []
        acc = y
        for _ in range(n):
            coeffs.append(acc)
            acc = R.scalar_mul(acc, y)
        return BilinearMap(tuple(coeffs))

    def evaluate(self, u: Sequence[Scalar], v: Sequence[Scalar]) -> Scalar:
        if len(u) != self.n or len(v) != self.n:
            raise ValueError("bilinear map applied to wrong-width vectors")
        acc = _ZERO
        for j in range(self.n):
            acc = R.scalar_add(acc, R.scalar_mul(self.coefficients[j],
                                                 R.scalar_mul(u[j], v[j])))
        return acc

    def to_bytes(self) -> bytes:
        """Canonical encoding for the transcript: the values themselves, not
        a label -- two different maps must never hash alike."""
        return b"".join(bytes(c) for c in self.coefficients)


# --------------------------------------------------------------------------
# proof object
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ZeroProof:
    """Non-interactive zero-argument proof.

    c_D has 2m+1 entries; c_D[m+1] is the identity by construction.
    """
    c_A0: Point
    c_Bm: Point
    c_D: List[Point]
    a_tilde: List[Scalar]
    b_tilde: List[Scalar]
    r_tilde: Scalar
    s_tilde: Scalar
    t_tilde: Scalar


# --------------------------------------------------------------------------
# transcript
# --------------------------------------------------------------------------

def _field(h: "hashlib._Hash", data: bytes) -> None:
    """Length-prefixed transcript field."""
    h.update(len(data).to_bytes(4, "big"))
    h.update(data)


def _point_list(h: "hashlib._Hash", points: Sequence[Point]) -> None:
    h.update(len(points).to_bytes(4, "big"))
    for p in points:
        _field(h, bytes(p))


def _challenge(ck: CommitmentKey, n: int, m: int, bmap: BilinearMap,
               context: bytes, c_A: Sequence[Point], c_B: Sequence[Point],
               c_A0: Point, c_Bm: Point, c_D: Sequence[Point]) -> Scalar:
    """Fiat-Shamir challenge over the complete transcript."""
    h = hashlib.sha512()
    _field(h, _DOMAIN)
    h.update(_VERSION.to_bytes(4, "big"))
    _field(h, bytes(ck.H))
    _point_list(h, ck.Gs)
    h.update(n.to_bytes(4, "big"))
    h.update(m.to_bytes(4, "big"))
    _field(h, bmap.to_bytes())
    _field(h, context)
    _point_list(h, c_A)
    _point_list(h, c_B)
    _field(h, bytes(c_A0))
    _field(h, bytes(c_Bm))
    _point_list(h, c_D)
    return nonzero_challenge(h)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _powers(x: Scalar, count: int) -> List[Scalar]:
    """[x^0, x^1, ..., x^{count-1}]."""
    out = [_ONE]
    for _ in range(count - 1):
        out.append(R.scalar_mul(out[-1], x))
    return out


def _fold_vectors(vectors: Sequence[Sequence[Scalar]],
                  weights: Sequence[Scalar], n: int) -> List[Scalar]:
    """sum_i weights[i] * vectors[i], componentwise."""
    out = [_ZERO] * n
    for vec, w in zip(vectors, weights):
        for j in range(n):
            out[j] = R.scalar_add(out[j], R.scalar_mul(w, vec[j]))
    return out


def _fold_scalars(values: Sequence[Scalar],
                  weights: Sequence[Scalar]) -> Scalar:
    acc = _ZERO
    for v, w in zip(values, weights):
        acc = R.scalar_add(acc, R.scalar_mul(w, v))
    return acc


def _diagonals(A: Sequence[Sequence[Scalar]], B: Sequence[Sequence[Scalar]],
               m: int, bmap: BilinearMap) -> List[Scalar]:
    """d_k for k = 0..2m, over pairs with j = (m - k) + i."""
    out: List[Scalar] = []
    for k in range(2 * m + 1):
        acc = _ZERO
        for i in range(m + 1):
            j = (m - k) + i
            if 0 <= j <= m:
                acc = R.scalar_add(acc, bmap.evaluate(A[i], B[j]))
        out.append(acc)
    return out


# --------------------------------------------------------------------------
# prove / verify
# --------------------------------------------------------------------------

def prove(ck: CommitmentKey,
          c_A: Sequence[Point], a: Sequence[Sequence[Scalar]],
          r: Sequence[Scalar],
          c_B: Sequence[Point], b: Sequence[Sequence[Scalar]],
          s: Sequence[Scalar],
          bmap: BilinearMap, context: bytes) -> ZeroProof:
    """Prove 0 = sum_{i=1..m} a_i * b_{i-1}.

    ``c_A``/``a``/``r`` are the m commitments to a_1..a_m and their
    openings; ``c_B``/``b``/``s`` are the m commitments to b_0..b_{m-1} and
    theirs. Commitments are passed in rather than recomputed so that
    callers holding derived commitments (section 5.1) can use this.

    ``context`` binds the proof to its place in the surrounding protocol --
    session, hand, shuffle, outer challenges. It is mandatory and has no
    default: an optional replay binding is one that gets forgotten.
    """
    m = len(a)
    if m < 1:
        raise ValueError("zero argument requires m >= 1")
    n = bmap.n
    if n < 1:
        raise ValueError("zero argument requires n >= 1")
    if n > ck.n:
        raise ValueError(f"vector wider than commitment key: {n} > {ck.n}")
    if not (len(c_A) == len(r) == m):
        raise ValueError("c_A, a and r must all have length m")
    if not (len(c_B) == len(b) == len(s) == m):
        raise ValueError("c_B, b and s must all have length m")
    for vec in (*a, *b):
        if len(vec) != n:
            raise ValueError("every witness vector must have length n")

    # Verify the caller's commitments really open to the claimed witness,
    # so a mismatch surfaces here rather than as an unexplained verify()
    # failure later.
    for idx in range(m):
        if commit(ck, a[idx], r[idx]) != c_A[idx]:
            raise ValueError(f"c_A[{idx}] does not open to (a, r)")
        if commit(ck, b[idx], s[idx]) != c_B[idx]:
            raise ValueError(f"c_B[{idx}] does not open to (b, s)")

    # Blinders: a_0 and b_m. Both families then run 0..m.
    a0 = [R.random_scalar() for _ in range(n)]
    bm = [R.random_scalar() for _ in range(n)]
    r0 = R.random_scalar()
    sm = R.random_scalar()

    A: List[Sequence[Scalar]] = [a0, *a]           # a_0 .. a_m
    B: List[Sequence[Scalar]] = [*b, bm]           # b_0 .. b_m
    rA: List[Scalar] = [r0, *r]
    sB: List[Scalar] = [*s, sm]

    d = _diagonals(A, B, m, bmap)
    require_witness(R.is_zero_scalar(d[m + 1]),
                    "witness does not satisfy sum a_i * b_{i-1} == 0")

    c_A0 = commit(ck, a0, r0)
    c_Bm = commit(ck, bm, sm)

    t = [R.random_scalar() for _ in range(2 * m + 1)]
    t[m + 1] = _ZERO                                # forced, with d_{m+1}
    c_D = [commit(ck, [d[k]], t[k]) for k in range(2 * m + 1)]

    x = _challenge(ck, n, m, bmap, context, c_A, c_B, c_A0, c_Bm, c_D)

    xs = _powers(x, 2 * m + 1)
    a_weights = xs[: m + 1]                          # x^i,     i = 0..m
    b_weights = [xs[m - j] for j in range(m + 1)]    # x^{m-j}, j = 0..m

    return ZeroProof(
        c_A0=c_A0,
        c_Bm=c_Bm,
        c_D=c_D,
        a_tilde=_fold_vectors(A, a_weights, n),
        b_tilde=_fold_vectors(B, b_weights, n),
        r_tilde=_fold_scalars(rA, a_weights),
        s_tilde=_fold_scalars(sB, b_weights),
        t_tilde=_fold_scalars(t, xs),
    )


def verify(ck: CommitmentKey,
           c_A: Sequence[Point], c_B: Sequence[Point],
           bmap: BilinearMap, context: bytes, proof: ZeroProof) -> bool:
    """Verify a zero-argument proof. Returns False; never raises."""
    m = len(c_A)
    n = bmap.n
    if m < 1 or n < 1 or n > ck.n:
        return False
    if len(c_B) != m:
        return False
    if len(proof.c_D) != 2 * m + 1:
        return False
    if len(proof.a_tilde) != n or len(proof.b_tilde) != n:
        return False

    # The only element required to be the identity. Checked as a point
    # equality, NOT by recomputing a commitment from a prover-supplied
    # randomness -- accepting anything that merely opens to zero would let
    # the prover carry a nonzero d_{m+1} through under blinding.
    if proof.c_D[m + 1] != R.IDENTITY:
        return False

    try:
        x = _challenge(ck, n, m, bmap, context, c_A, c_B,
                       proof.c_A0, proof.c_Bm, proof.c_D)
    except ValueError:
        return False

    return check_equations(ck, c_A, c_B, bmap, proof, x)


def check_equations(ck: CommitmentKey, c_A: Sequence[Point],
                    c_B: Sequence[Point], bmap: BilinearMap,
                    proof: ZeroProof, x: Scalar) -> bool:
    """The three verification equations, for an externally supplied x.

    Split out from ``verify`` because the SHVZK simulator of Theorem 10 is
    stated for the INTERACTIVE argument, where x is handed to it. Under
    Fiat-Shamir x is a hash of the prover's own messages, so a simulator
    cannot both choose x freely and produce commitments hashing to it --
    that gap is closed by programming the random oracle, not by the code.
    Exposing the equations lets the simulator be tested against exactly
    what it is claimed to satisfy.
    """
    m = len(c_A)
    xs = _powers(x, 2 * m + 1)
    a_weights = xs[: m + 1]
    b_weights = [xs[m - j] for j in range(m + 1)]

    # (1)  sum_{i=0..m} x^i * c_A[i] == comck(a~; r~)
    if R.multiscalar_mul(a_weights, [proof.c_A0, *c_A]) != \
            commit(ck, proof.a_tilde, proof.r_tilde):
        return False

    # (2)  sum_{j=0..m} x^{m-j} * c_B[j] == comck(b~; s~)
    #      c_Bm is the j = m term here, at weight x^0 -- not a separate
    #      addend. See the module docstring.
    if R.multiscalar_mul(b_weights, [*c_B, proof.c_Bm]) != \
            commit(ck, proof.b_tilde, proof.s_tilde):
        return False

    # (3)  sum_{k=0..2m} x^k * c_D[k] == comck(a~ * b~; t~)
    try:
        paired = bmap.evaluate(proof.a_tilde, proof.b_tilde)
    except ValueError:
        return False
    if R.multiscalar_mul(xs, proof.c_D) != \
            commit(ck, [paired], proof.t_tilde):
        return False

    return True


__all__ = ["BilinearMap", "ZeroProof", "prove", "verify",
           "check_equations"]
