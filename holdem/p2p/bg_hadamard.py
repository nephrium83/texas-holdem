"""Bayer-Groth Hadamard product argument (paper section 5.1, Theorem 9).

Proves that a committed vector is the entry-wise (Hadamard) product of a
sequence of committed vectors:

    b = a_1 * a_2 * ... * a_m          (entry-wise, all vectors in Z_q^n)

given commitments c_A = (c_A1..c_Am) to the a_i and c_b to b.

This is the adapter that puts bg_zero.py to work: it does no group
algebra of its own beyond building derived commitments, and reduces the
whole statement to a single zero argument.

How the reduction works
-----------------------
The prover commits to the PARTIAL products

    b_1 = a_1,  b_2 = a_1*a_2,  ...,  b_m = a_1*...*a_m

and proves b_{i+1} = a_{i+1} * b_i for i = 1..m-1. The ends are pinned by
construction rather than by a check: c_B1 IS c_A1 and c_Bm IS c_b, so a
chain that verifies necessarily starts at a_1 and ends at the claimed b.
Only the interior commitments c_B2..c_B(m-1) travel in the proof.

After a challenge x the m-1 separate equations are batched into one:

    sum_{i=1..m-1} x^i b_{i+1} = sum_{i=1..m-1} a_{i+1} (x^i b_i)

Writing d_i = x^i b_i and d = sum_{i=1..m-1} x^i b_{i+1}, both of which
the verifier can form homomorphically as c_Di = x^i * c_Bi and
c_D = sum_{i=1..m-1} x^i * c_B(i+1), this becomes

    d = sum_{i=1..m-1} a_{i+1} d_i

A second challenge y defines the bilinear map of section 5.1,
u * v = sum_j u_j v_j y^j, and the statement is finally expressed as a
zero relation, which bg_zero discharges:

    0 = sum_{i=1..m-1} a_{i+1} * d_i  -  1 * d

The subtraction is what makes the sequence lengths work out. See below.

THE MAPPING INTO THE ZERO ARGUMENT
----------------------------------
The zero argument proves 0 = sum_{i=1..M} A_i * B_{i-1} for its own
sequence length M. Here M = m, NOT m-1, because the -1 term occupies the
last slot of the A family:

    A family (its 1..M):  [a_2, ..., a_m, -1]     <- m entries
    B family (its 0..M-1): [d_1, ..., d_{m-1}, d] <- m entries

Expanding gives a_2*d_1 + ... + a_m*d_{m-1} + (-1)*d, which is the
target relation. Getting M = m-1 here is the natural mistake and it
produces a proof of a different, weaker statement.

Commitments and openings handed down:

    A:  [c_A2..c_Am, c_-1]     openings [a_2..a_m, -1],  [r_2..r_m, 0]
    B:  [c_D1..c_D(m-1), c_D]  openings [d_1..d_{m-1}, d],
                                        [t_1..t_{m-1}, t]

    where  d_i = x^i b_i,  t_i = x^i s_i
           d   = sum_{i=1..m-1} x^i b_{i+1}
           t   = sum_{i=1..m-1} x^i s_{i+1}
           c_-1 = comck(-1; 0)

Every one of those commitments is DERIVED -- c_Di is x^i * c_Bi, formed
by scalar multiplication, never by calling commit(). bg_zero.prove takes
commitments alongside their openings precisely so this caller can exist.

Dimensions
----------
m >= 2. Unlike bg_zero, which is sound and simulatable at m = 1, the
Hadamard argument degenerates there: the statement collapses to b = a_1,
there are no partial products, and the i = 1..m-1 reduction is empty. The
bound belongs here and is not pushed down into the primitive. n >= 1.

CHALLENGE ORDERING IS A SOUNDNESS REQUIREMENT
---------------------------------------------
Theorem 9's extraction depends on c_B being fixed BEFORE the prover sees
the challenges: only then are d_1..d_{m-1}, d determined before y defines
the bilinear map, which is what makes "the relation holds for random y"
imply the relation holds at all. So the order is: commit c_B, then derive
BOTH x and y from a transcript containing c_A, c_b and c_B. Deriving y
from anything that does not include c_B breaks the argument while leaving
every completeness test passing.

x and y are drawn from one transcript hash with distinct suffixes, so
neither can be ground independently of the other.

The zero argument's context binds c_A, c_b, c_B, x and y in addition to
the caller's own context, so an inner proof cannot be lifted out of this
invocation and replayed in another.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Sequence

from holdem.p2p import ristretto as R
from holdem.p2p import bg_zero
from holdem.p2p.bg_zero import BilinearMap, ZeroProof
from holdem.p2p.ristretto import Point, Scalar
from holdem.p2p.pedersen import CommitmentKey, commit


_DOMAIN = b"poker.bg.hadamard.v1"
_VERSION = 1

_ZERO = Scalar(b"\x00" * 32)
_ONE = Scalar(b"\x01" + b"\x00" * 31)
_NEG_ONE = R.scalar_negate(_ONE)


@dataclass(frozen=True)
class HadamardProof:
    """Interior partial-product commitments plus the underlying zero proof.

    ``c_B_interior`` holds c_B2..c_B(m-1) only -- m-2 points, empty when
    m == 2. c_B1 and c_Bm are not transmitted: they ARE c_A1 and c_b, and
    the verifier rebuilds them from the statement rather than checking a
    prover-supplied copy, so there is nothing to disagree about.
    """
    c_B_interior: List[Point]
    zero: ZeroProof


# --------------------------------------------------------------------------
# transcript
# --------------------------------------------------------------------------

def _field(h: "hashlib._Hash", data: bytes) -> None:
    h.update(len(data).to_bytes(4, "big"))
    h.update(data)


def _point_list(h: "hashlib._Hash", points: Sequence[Point]) -> None:
    h.update(len(points).to_bytes(4, "big"))
    for p in points:
        _field(h, bytes(p))


def _transcript(ck: CommitmentKey, n: int, m: int, context: bytes,
                c_A: Sequence[Point], c_b: Point,
                c_B: Sequence[Point]) -> bytes:
    """The bytes both challenges are drawn from.

    c_B is included, which is the whole point: Theorem 9 needs the
    partial-product commitments fixed before x and y exist.
    """
    h = hashlib.sha512()
    _field(h, _DOMAIN)
    h.update(_VERSION.to_bytes(4, "big"))
    _field(h, bytes(ck.H))
    _point_list(h, ck.Gs)
    h.update(n.to_bytes(4, "big"))
    h.update(m.to_bytes(4, "big"))
    _field(h, context)
    _point_list(h, c_A)
    _field(h, bytes(c_b))
    _point_list(h, c_B)
    return h.digest()


def _challenges(transcript: bytes) -> tuple:
    """Derive (x, y) from one transcript with distinct suffixes.

    Two draws from the same hash rather than two independent hashes: a
    prover grinding for a favourable y necessarily regrinds x as well,
    since both move together with any change to the transcript.
    """
    x = R.scalar_reduce(hashlib.sha512(transcript + b"\x01x").digest())
    y = R.scalar_reduce(hashlib.sha512(transcript + b"\x02y").digest())
    if R.is_zero_scalar(x) or R.is_zero_scalar(y):
        raise ValueError("Fiat-Shamir challenge reduced to zero")
    return x, y


def _zero_context(context: bytes, c_A: Sequence[Point], c_b: Point,
                  c_B: Sequence[Point], x: Scalar, y: Scalar) -> bytes:
    """Context handed down to the zero argument.

    Binds this invocation: the statement, the partial-product
    commitments, and both challenges, on top of whatever the caller
    supplied. Without it a zero proof produced here would be a free-
    floating object that could be presented against another Hadamard
    statement that happened to reduce to the same relation.
    """
    h = hashlib.sha512()
    _field(h, b"poker.bg.hadamard.zero-context.v1")
    _field(h, context)
    _point_list(h, c_A)
    _field(h, bytes(c_b))
    _point_list(h, c_B)
    _field(h, bytes(x))
    _field(h, bytes(y))
    return h.digest()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _powers(x: Scalar, count: int) -> List[Scalar]:
    """[x^0, x^1, ..., x^{count-1}]."""
    out = [_ONE]
    for _ in range(count - 1):
        out.append(R.scalar_mul(out[-1], x))
    return out


def _derived_commitments(ck: CommitmentKey, c_B: Sequence[Point], m: int,
                         n: int, xs: Sequence[Scalar]) -> tuple:
    """c_D1..c_D(m-1), c_D and c_-1, all formed homomorphically.

    c_B is 0-indexed here: c_B[0] is the paper's c_B1.
    """
    c_D_list = [R.mul_safe(xs[i], c_B[i - 1]) for i in range(1, m)]
    c_D = R.multiscalar_mul(list(xs[1:m]), [c_B[i] for i in range(1, m)])
    c_neg1 = commit(ck, [_NEG_ONE] * n, _ZERO)
    return c_D_list, c_D, c_neg1


# --------------------------------------------------------------------------
# prove / verify
# --------------------------------------------------------------------------

def prove(ck: CommitmentKey, c_A: Sequence[Point],
          a: Sequence[Sequence[Scalar]], r: Sequence[Scalar],
          c_b: Point, b: Sequence[Scalar], s: Scalar,
          context: bytes) -> HadamardProof:
    """Prove b = a_1 * ... * a_m entry-wise.

    ``c_A``/``a``/``r`` are the m commitments and their openings;
    ``c_b``/``b``/``s`` is the claimed product and its opening.
    ``context`` is mandatory and binds this proof to its place in the
    surrounding protocol.
    """
    m = len(a)
    if m < 2:
        raise ValueError("Hadamard product argument requires m >= 2")
    n = len(a[0])
    if n < 1:
        raise ValueError("Hadamard product argument requires n >= 1")
    if n > ck.n:
        raise ValueError(f"vector wider than commitment key: {n} > {ck.n}")
    if not (len(c_A) == len(r) == m):
        raise ValueError("c_A, a and r must all have length m")
    for vec in a:
        if len(vec) != n:
            raise ValueError("every a_i must have length n")
    if len(b) != n:
        raise ValueError("b must have length n")

    for i in range(m):
        if commit(ck, a[i], r[i]) != c_A[i]:
            raise ValueError(f"c_A[{i}] does not open to (a, r)")
    if commit(ck, b, s) != c_b:
        raise ValueError("c_b does not open to (b, s)")

    # Partial products b_1..b_m, with b_m required to equal the claim.
    partials: List[List[Scalar]] = [list(a[0])]
    for i in range(1, m):
        prev = partials[-1]
        partials.append([R.scalar_mul(prev[j], a[i][j]) for j in range(n)])
    for j in range(n):
        if bytes(partials[-1][j]) != bytes(b[j]):
            raise ValueError("witness does not satisfy b == product of a_i")

    # c_B1 = c_A1 and c_Bm = c_b pin the ends; only the interior is fresh.
    s_list: List[Scalar] = [r[0]]
    interior: List[Point] = []
    for i in range(1, m - 1):
        s_i = R.random_scalar()
        s_list.append(s_i)
        interior.append(commit(ck, partials[i], s_i))
    s_list.append(s)
    c_B: List[Point] = [c_A[0], *interior, c_b]

    # Challenges come only after c_B exists. See the module docstring.
    x, y = _challenges(_transcript(ck, n, m, context, c_A, c_b, c_B))
    xs = _powers(x, m)
    bmap = BilinearMap.from_challenge(y, n)

    c_D_list, c_D, c_neg1 = _derived_commitments(ck, c_B, m, n, xs)

    # Openings of the derived commitments: d_i = x^i b_i, t_i = x^i s_i.
    d_list = [[R.scalar_mul(xs[i], partials[i - 1][j]) for j in range(n)]
              for i in range(1, m)]
    t_list = [R.scalar_mul(xs[i], s_list[i - 1]) for i in range(1, m)]

    d_vec = [_ZERO] * n
    for i in range(1, m):
        for j in range(n):
            d_vec[j] = R.scalar_add(
                d_vec[j], R.scalar_mul(xs[i], partials[i][j]))
    t_val = _ZERO
    for i in range(1, m):
        t_val = R.scalar_add(t_val, R.scalar_mul(xs[i], s_list[i]))

    # M = m: the -1 term takes the last A slot. See the module docstring.
    zero_proof = bg_zero.prove(
        ck,
        [*c_A[1:], c_neg1],
        [*[list(v) for v in a[1:]], [_NEG_ONE] * n],
        [*r[1:], _ZERO],
        [*c_D_list, c_D],
        [*d_list, d_vec],
        [*t_list, t_val],
        bmap,
        _zero_context(context, c_A, c_b, c_B, x, y),
    )
    return HadamardProof(c_B_interior=interior, zero=zero_proof)


def verify(ck: CommitmentKey, c_A: Sequence[Point], c_b: Point, n: int,
           context: bytes, proof: HadamardProof) -> bool:
    """Verify a Hadamard product proof. Returns False; never raises."""
    m = len(c_A)
    if m < 2 or n < 1 or n > ck.n:
        return False
    if len(proof.c_B_interior) != m - 2:
        return False

    # Rebuilt from the statement, not taken from the prover.
    c_B: List[Point] = [c_A[0], *proof.c_B_interior, c_b]

    try:
        x, y = _challenges(_transcript(ck, n, m, context, c_A, c_b, c_B))
        bmap = BilinearMap.from_challenge(y, n)
    except ValueError:
        return False

    xs = _powers(x, m)
    c_D_list, c_D, c_neg1 = _derived_commitments(ck, c_B, m, n, xs)

    return bg_zero.verify(
        ck,
        [*c_A[1:], c_neg1],
        [*c_D_list, c_D],
        bmap,
        _zero_context(context, c_A, c_b, c_B, x, y),
        proof.zero,
    )


__all__ = ["HadamardProof", "prove", "verify"]
