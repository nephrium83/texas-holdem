"""Bayer-Groth verifiable shuffle argument.

This is the prevention-path construction built from the paper's shuffle
argument: a matrix-elements product argument plus a multi-exponentiation
argument.  It proves that ``out_deck[i]`` is a re-encryption of
``in_deck[perm[i]]`` without revealing ``perm`` or the re-encryption
scalars.

The existing ``shuffle_proof.py`` remains the v1 cut-and-choose path.  This
module is deliberately standalone until its cost is measured in the full
mental-poker startup flow.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import List, Sequence

from holdem.p2p import bg_product, ristretto as R
from holdem.p2p.bg_product import ProductProof
from holdem.p2p.elgamal import Ciphertext, encrypt, reencrypt
from holdem.p2p.pedersen import CommitmentKey, commit
from holdem.p2p.ristretto import Point, Scalar


_DOMAIN = b"poker.bg.shuffle.v1"
_MULTI_DOMAIN = b"poker.bg.shuffle.multi-exponentiation.v1"
_GENERATOR = R.hash_to_group(
    hashlib.sha512(b"poker.bg.shuffle.mask-generator.v1").digest())
_ZERO = Scalar(b"\x00" * 32)
_ONE = Scalar(b"\x01" + b"\x00" * 31)


@dataclass(frozen=True)
class MultiExponentiationProof:
    """Proof that committed weights produce the public ciphertext product."""

    a_0_commit: Point
    commit_b_k: List[Point]
    vector_e_k: List[Ciphertext]
    r_blinded: Scalar
    b_blinded: Scalar
    s_blinded: Scalar
    tau_blinded: Scalar
    a_blinded: List[Scalar]


@dataclass(frozen=True)
class ShuffleProof:
    """A Bayer-Groth proof for one encrypted shuffle round."""

    a_commits: List[Point]
    b_commits: List[Point]
    product: ProductProof
    multi: MultiExponentiationProof


def _field(h: "hashlib._Hash", data: bytes) -> None:
    h.update(len(data).to_bytes(4, "big"))
    h.update(data)


def _point_list(h: "hashlib._Hash", points: Sequence[Point]) -> None:
    h.update(len(points).to_bytes(4, "big"))
    for point in points:
        _field(h, bytes(point))


def _cipher(h: "hashlib._Hash", cipher: Ciphertext) -> None:
    _field(h, bytes(cipher.c0))
    _field(h, bytes(cipher.c1))


def _cipher_list(h: "hashlib._Hash", ciphers: Sequence[Ciphertext]) -> None:
    h.update(len(ciphers).to_bytes(4, "big"))
    for cipher in ciphers:
        _cipher(h, cipher)


def _flatten_ciphers(chunks: Sequence[Sequence[Ciphertext]]) -> List[Ciphertext]:
    return [cipher for chunk in chunks for cipher in chunk]


def _cipher_add(left: Ciphertext, right: Ciphertext) -> Ciphertext:
    return Ciphertext(R.add(left.c0, right.c0), R.add(left.c1, right.c1))


def _cipher_scale(scalar: Scalar, cipher: Ciphertext) -> Ciphertext:
    return Ciphertext(R.mul_safe(scalar, cipher.c0),
                      R.mul_safe(scalar, cipher.c1))


def _cipher_sum(ciphers: Sequence[Ciphertext]) -> Ciphertext:
    result = Ciphertext(R.IDENTITY, R.IDENTITY)
    for cipher in ciphers:
        result = _cipher_add(result, cipher)
    return result


def _dot_cipher(values: Sequence[Scalar], ciphers: Sequence[Ciphertext]) -> Ciphertext:
    if len(values) != len(ciphers):
        raise ValueError("dot product lengths differ")
    return _cipher_sum([_cipher_scale(value, cipher)
                        for value, cipher in zip(values, ciphers)])


def _dot_scalar(left: Sequence[Scalar], right: Sequence[Scalar]) -> Scalar:
    if len(left) != len(right):
        raise ValueError("scalar dot-product lengths differ")
    result = _ZERO
    for a, b in zip(left, right):
        result = R.scalar_add(result, R.scalar_mul(a, b))
    return result


def _powers(x: Scalar, count: int) -> List[Scalar]:
    result: List[Scalar] = []
    current = _ONE
    for _ in range(count):
        current = R.scalar_mul(current, x)
        result.append(current)
    return result


def _scalar_int(value: int) -> Scalar:
    return Scalar(value.to_bytes(32, "little"))


def _commit_scalars(ck: CommitmentKey, values: Sequence[Scalar],
                    blinders: Sequence[Scalar]) -> List[Point]:
    if len(values) != len(blinders):
        raise ValueError("values and blinders differ in length")
    return [commit(ck, [value], blinder)
            for value, blinder in zip(values, blinders)]


def _statement_context(context: bytes, ck: CommitmentKey, pk: Point,
                       in_deck: Sequence[Ciphertext],
                       out_deck: Sequence[Ciphertext], m: int, n: int) -> bytes:
    h = hashlib.sha512()
    _field(h, _DOMAIN)
    _field(h, context)
    _field(h, bytes(pk))
    _field(h, bytes(ck.H))
    _point_list(h, ck.Gs)
    h.update(m.to_bytes(4, "big"))
    h.update(n.to_bytes(4, "big"))
    _cipher_list(h, in_deck)
    _cipher_list(h, out_deck)
    return h.digest()


def _challenge_x(statement: bytes, a_commits: Sequence[Point]) -> Scalar:
    h = hashlib.sha512()
    _field(h, statement)
    _field(h, b"permutation-commitments")
    _point_list(h, a_commits)
    x = R.scalar_reduce(h.digest())
    if R.is_zero_scalar(x):
        raise ValueError("shuffle challenge x reduced to zero")
    return x


def _challenge_yz(statement: bytes, a_commits: Sequence[Point],
                  b_commits: Sequence[Point]) -> tuple[Scalar, Scalar]:
    h = hashlib.sha512()
    _field(h, statement)
    _field(h, b"product-challenges")
    _point_list(h, a_commits)
    _point_list(h, b_commits)
    digest = h.digest()
    y = R.scalar_reduce(hashlib.sha512(digest + b"y").digest())
    z = R.scalar_reduce(hashlib.sha512(digest + b"z").digest())
    if R.is_zero_scalar(y) or R.is_zero_scalar(z):
        raise ValueError("shuffle challenge reduced to zero")
    return y, z


def _multi_challenge(statement: bytes, a_commits: Sequence[Point],
                     b_commits: Sequence[Point], product: Ciphertext,
                     a_0_commit: Point, commit_b_k: Sequence[Point],
                     vector_e_k: Sequence[Ciphertext]) -> Scalar:
    h = hashlib.sha512()
    _field(h, _MULTI_DOMAIN)
    _field(h, statement)
    _point_list(h, a_commits)
    _point_list(h, b_commits)
    _cipher(h, product)
    _field(h, bytes(a_0_commit))
    _point_list(h, commit_b_k)
    _cipher_list(h, vector_e_k)
    challenge = R.scalar_reduce(h.digest())
    if R.is_zero_scalar(challenge):
        raise ValueError("multi-exponentiation challenge reduced to zero")
    return challenge


def _validate(pk: Point, ck: CommitmentKey, in_deck: Sequence[Ciphertext],
              out_deck: Sequence[Ciphertext], m: int, n: int) -> None:
    if m < 2 or n < 2:
        raise ValueError("shuffle dimensions require m,n >= 2")
    if m * n != len(in_deck) or len(out_deck) != len(in_deck):
        raise ValueError("shuffle deck length does not match m*n")
    if n > ck.n:
        raise ValueError("shuffle row width exceeds commitment key")
    if not isinstance(pk, Point):
        raise ValueError("invalid public key")


def _chunks(values: Sequence, n: int) -> List[list]:
    return [list(values[i:i + n]) for i in range(0, len(values), n)]


def _multi_prove(pk: Point, ck: CommitmentKey, statement: bytes,
                 a_commits: Sequence[Point], b_commits: Sequence[Point],
                 out_chunks: Sequence[Sequence[Ciphertext]],
                 matrix_a: Sequence[Sequence[Scalar]],
                 matrix_blinders: Sequence[Scalar], rho: Scalar,
                 product: Ciphertext, m: int, n: int
                 ) -> MultiExponentiationProof:
    """Prove ``product = Enc(0; -rho) + sum_ij matrix_a[i][j] * out[i][j]``.

    The paper calls this sub-argument's exponent matrix A, which is why the
    fields are named a_0/a_blinded. The shuffle instantiates it with the
    **b** matrix (the x^perm powers) and ``b_commits`` as the exponent
    commitments -- ``a_commits`` enters only the challenge hash. Passing
    the index matrix here instead would prove a true statement about the
    wrong exponents and leave the decks unlinked.

    ``product`` is the public value the verifier recomputes from the INPUT
    deck. It is a statement input, never read back out of the proof.
    """
    length = 2 * m
    a_0 = [R.random_scalar() for _ in range(n)]
    r_0 = R.random_scalar()
    b_values = [R.random_scalar() for _ in range(length)]
    s_values = [R.random_scalar() for _ in range(length)]
    tau_values = [R.random_scalar() for _ in range(length)]
    b_values[m] = _ZERO
    s_values[m] = _ZERO
    tau_values[m] = rho

    a_0_commit = commit(ck, a_0, r_0)
    commit_b_k = _commit_scalars(ck, b_values, s_values)

    diagonals = [Ciphertext(R.IDENTITY, R.IDENTITY) for _ in range(2 * m - 1)]
    center = m - 1
    for distance in range(1, m):
        additional = _dot_cipher(a_0, out_chunks[distance - 1])
        left = _cipher_sum([
            _dot_cipher(matrix_a[i - distance], out_chunks[i])
            for i in range(distance, m)
        ])
        right = _cipher_sum([
            _dot_cipher(matrix_a[i], out_chunks[i - distance])
            for i in range(distance, m)
        ])
        diagonals[center - distance] = _cipher_add(left, additional)
        diagonals[center + distance] = right

    diagonals[center] = _cipher_sum([
        _dot_cipher(matrix_a[i], out_chunks[i]) for i in range(m)
    ])
    diagonals.insert(0, _dot_cipher(a_0, out_chunks[-1]))

    vector_e_k = [
        _cipher_add(
            encrypt(pk, R.mul_safe(b_values[k], _GENERATOR), tau_values[k]),
            diagonals[k])
        for k in range(length)
    ]
    challenge = _multi_challenge(
        statement, a_commits, b_commits, product, a_0_commit,
        commit_b_k, vector_e_k)
    challenge_powers = [_ONE, *_powers(challenge, length - 1)]
    x_array = challenge_powers[1:m + 1]
    a_blinded = []
    for j in range(n):
        value = a_0[j]
        for i in range(m):
            value = R.scalar_add(value, R.scalar_mul(x_array[i], matrix_a[i][j]))
        a_blinded.append(value)

    return MultiExponentiationProof(
        a_0_commit=a_0_commit,
        commit_b_k=commit_b_k,
        vector_e_k=vector_e_k,
        r_blinded=R.scalar_add(r_0, _dot_scalar(matrix_blinders, x_array)),
        b_blinded=_dot_scalar(b_values, challenge_powers),
        s_blinded=_dot_scalar(s_values, challenge_powers),
        tau_blinded=_dot_scalar(tau_values, challenge_powers),
        a_blinded=a_blinded,
    )


def _multi_verify(pk: Point, ck: CommitmentKey, statement: bytes,
                  a_commits: Sequence[Point], b_commits: Sequence[Point],
                  out_chunks: Sequence[Sequence[Ciphertext]],
                  product: Ciphertext, proof: MultiExponentiationProof,
                  m: int, n: int) -> bool:
    if len(a_commits) != m or len(b_commits) != m:
        return False
    if len(proof.commit_b_k) != 2 * m or len(proof.vector_e_k) != 2 * m:
        return False
    if len(proof.a_blinded) != n:
        return False
    zero_commit = commit(ck, [_ZERO], _ZERO)
    if proof.commit_b_k[m] != zero_commit or proof.vector_e_k[m] != product:
        return False
    try:
        challenge = _multi_challenge(
            statement, a_commits, b_commits, product, proof.a_0_commit,
            proof.commit_b_k, proof.vector_e_k)
    except ValueError:
        return False
    challenge_powers = [_ONE, *_powers(challenge, 2 * m - 1)]
    x_array = challenge_powers[1:m + 1]
    # The exponent commitments are b_commits: the shuffle instantiates this
    # sub-argument with the x^perm matrix, not the permutation indices.
    c_b_x = R.multiscalar_mul(list(x_array), list(b_commits))
    if R.add(c_b_x, proof.a_0_commit) != commit(
            ck, proof.a_blinded, proof.r_blinded):
        return False
    c_b_k = R.multiscalar_mul(challenge_powers, proof.commit_b_k)
    if c_b_k != commit(ck, [proof.b_blinded], proof.s_blinded):
        return False
    sum_e = _cipher_sum([
        _cipher_scale(challenge_powers[k], proof.vector_e_k[k])
        for k in range(2 * m)
    ])
    masking = encrypt(pk, R.mul_safe(proof.b_blinded, _GENERATOR),
                      proof.tau_blinded)
    rhs = _cipher_sum([
        _dot_cipher(
            [R.scalar_mul(challenge_powers[m - 1 - i], value)
             for value in proof.a_blinded],
            out_chunks[i])
        for i in range(m)
    ])
    expected = _cipher_add(masking, rhs)
    return sum_e == expected


def prove(pk: Point, ck: CommitmentKey, in_deck: Sequence[Ciphertext],
          out_deck: Sequence[Ciphertext], perm: Sequence[int],
          scalars: Sequence[Scalar], m: int, n: int,
          context: bytes) -> ShuffleProof:
    """Prove ``out[i] = reencrypt(pk, in[perm[i]], scalars[i])``."""
    _validate(pk, ck, in_deck, out_deck, m, n)
    if len(perm) != len(in_deck) or sorted(perm) != list(range(len(in_deck))):
        raise ValueError("perm must be a permutation of the deck positions")
    if len(scalars) != len(in_deck):
        raise ValueError("scalars must match the deck length")
    for i, source in enumerate(perm):
        expected = reencrypt(pk, in_deck[source], scalars[i])
        if expected != out_deck[i]:
            raise ValueError("output deck does not match the shuffle witness")
    return _prove_unchecked(pk, ck, in_deck, out_deck, perm, scalars,
                            m, n, context)


def _prove_unchecked(pk: Point, ck: CommitmentKey,
                     in_deck: Sequence[Ciphertext],
                     out_deck: Sequence[Ciphertext], perm: Sequence[int],
                     scalars: Sequence[Scalar], m: int, n: int,
                     context: bytes) -> ShuffleProof:
    """The prover algorithm with NO witness self-check.

    ``prove`` validates its witness before delegating here, but soundness
    must never rest on the prover checking itself -- a real attacker just
    deletes that check. This seam exists so the test suite can BE that
    attacker: run the honest algorithm over a deck that is not a shuffle
    of the input and confirm the verifier still rejects.

    Not part of the public API; do not call it outside tests.
    """
    statement = _statement_context(context, ck, pk, in_deck, out_deck, m, n)
    indices = [_scalar_int(i + 1) for i in range(m * n)]
    a_flat = [indices[source] for source in perm]
    a_chunks = _chunks(a_flat, n)
    a_blinders = [R.random_scalar() for _ in range(m)]
    a_commits = [commit(ck, row, blind)
                 for row, blind in zip(a_chunks, a_blinders)]
    x = _challenge_x(statement, a_commits)
    x_powers = _powers(x, m * n)
    b_flat = [x_powers[source] for source in perm]
    b_chunks = _chunks(b_flat, n)
    b_blinders = [R.random_scalar() for _ in range(m)]
    b_commits = [commit(ck, row, blind)
                 for row, blind in zip(b_chunks, b_blinders)]
    y, z = _challenge_yz(statement, a_commits, b_commits)

    d_flat = [R.scalar_sub(
        R.scalar_add(R.scalar_mul(y, a), b_value), z)
        for a, b_value in zip(a_flat, b_flat)]
    t = [R.scalar_add(R.scalar_mul(y, r), s)
         for r, s in zip(a_blinders, b_blinders)]
    d_chunks = _chunks(d_flat, n)
    d_commits = [commit(ck, row, blind)
                 for row, blind in zip(d_chunks, t)]
    claimed = _ONE
    for index in range(m * n):
        claimed = R.scalar_mul(
            claimed,
            R.scalar_sub(R.scalar_add(R.scalar_mul(y, _scalar_int(index + 1)),
                                      x_powers[index]), z))

    product_context = hashlib.sha512(
        _DOMAIN + b"|product|" + statement + bytes(x) + bytes(y) + bytes(z)
    ).digest()
    product = bg_product.prove(ck, d_commits, d_chunks, t, claimed,
                                product_context)

    out_chunks = _chunks(out_deck, n)
    rho = _ZERO
    for scalar, weight in zip(scalars, b_flat):
        rho = R.scalar_sub(rho, R.scalar_mul(scalar, weight))
    # The public statement value: sum_j x^{j+1} * in_deck[j]. The honest
    # relation prod_i out[i]^{b_i} = Enc(0; -rho) + this holds exactly
    # because b_i = x^{perm(i)+1} re-indexes the sum over the input deck.
    multi = _multi_prove(
        pk, ck, statement, a_commits, b_commits, out_chunks,
        b_chunks, b_blinders, rho, _dot_cipher(x_powers, in_deck), m, n)
    return ShuffleProof(a_commits=a_commits, b_commits=b_commits,
                        product=product, multi=multi)


def verify(pk: Point, ck: CommitmentKey, in_deck: Sequence[Ciphertext],
           out_deck: Sequence[Ciphertext], m: int, n: int, context: bytes,
           proof: ShuffleProof) -> bool:
    """Verify a Bayer-Groth shuffle proof. Returns False; never raises."""
    try:
        _validate(pk, ck, in_deck, out_deck, m, n)
        statement = _statement_context(context, ck, pk, in_deck, out_deck, m, n)
        x = _challenge_x(statement, proof.a_commits)
        y, z = _challenge_yz(statement, proof.a_commits, proof.b_commits)
        x_powers = _powers(x, m * n)
        claimed = _ONE
        for index in range(m * n):
            claimed = R.scalar_mul(
                claimed,
                R.scalar_sub(R.scalar_add(R.scalar_mul(y, _scalar_int(index + 1)),
                                          x_powers[index]), z))
        d_commits = []
        for a_commit, b_commit in zip(proof.a_commits, proof.b_commits):
            d_commit = R.add(R.mul_safe(y, a_commit), b_commit)
            d_commits.append(d_commit)
        # Subtracting z from every committed coordinate is a public derived
        # commitment, using the commitment to [-z, ..., -z].
        neg_z = [R.scalar_negate(z)] * n
        neg_z_commit = commit(ck, neg_z, _ZERO)
        d_commits = [R.add(value, neg_z_commit) for value in d_commits]
        product_context = hashlib.sha512(
            _DOMAIN + b"|product|" + statement + bytes(x) + bytes(y) + bytes(z)
        ).digest()
        if not bg_product.verify(ck, d_commits, n, claimed,
                                 product_context, proof.product):
            return False
        # The verifier recomputes the multi-exponentiation statement from
        # the PUBLIC input deck. This is the only step that binds out_deck
        # to in_deck; taking the value from the proof instead would make
        # the check vacuous and accept any forged deck.
        expected = _dot_cipher(x_powers, in_deck)
        return _multi_verify(
            pk, ck, statement, proof.a_commits, proof.b_commits,
            _chunks(out_deck, n), expected, proof.multi, m, n)
    except (AttributeError, IndexError, TypeError, ValueError):
        return False


__all__ = ["MultiExponentiationProof", "ShuffleProof", "prove", "verify"]
