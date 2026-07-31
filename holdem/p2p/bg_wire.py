"""Wire codec for the Bayer-Groth shuffle proof (prevention path).

``bg_shuffle.ShuffleProof`` is a four-level nest of frozen dataclasses
(ShuffleProof -> ProductProof -> HadamardProof -> ZeroProof, plus SVPProof
and MultiExponentiationProof). This module is the single place that turns
that nest into JSON-safe data and back, so the proof modules themselves
stay free of transport concerns and there is exactly one file to audit for
round-trip correctness.

Representation
--------------
The existing hostless wire format (see mental_deal.py's message list and
``elgamal.Ciphertext.to_hex``) is flat JSON with 32-byte values as hex
strings and ciphertexts as ``[c0_hex, c1_hex]`` pairs. This codec follows
that convention exactly rather than inventing a packed binary blob: a
52-card proof costs roughly 6 KB of hex against a 3 KB canonical payload,
which is the same 2x the ``deck`` field of the very same message already
pays. Field names mirror the dataclass attributes so a reviewer can diff
the codec against the struct by eye.

Fail-closed
-----------
Every decode path raises ``ValueError`` on anything malformed -- bad hex,
wrong length, missing key, wrong JSON type, non-canonical group element,
or (when dimensions are supplied) a list whose length does not match the
shape ``(m, n)`` implies. Callers treat that exactly like a verification
failure. Decoding NEVER returns a partially-populated proof.

Note on the identity element: several proof components are legitimately
the identity point -- ``ZeroProof.c_D[m+1]`` is the identity by
construction, and a commitment to zero with zero randomness is too. The
all-zero Ristretto255 encoding is canonical and ``point_from_bytes``
accepts it, so proof points are parsed with full validation and no
identity special-case. ``test_identity_component_round_trips`` pins that.
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

from holdem.p2p import ristretto as R
from holdem.p2p.bg_hadamard import HadamardProof
from holdem.p2p.bg_product import ProductProof
from holdem.p2p.bg_shuffle import MultiExponentiationProof, ShuffleProof
from holdem.p2p.bg_svp import SVPProof
from holdem.p2p.bg_zero import ZeroProof
from holdem.p2p.elgamal import Ciphertext
from holdem.p2p.ristretto import Point, Scalar


# Upper bound on any decoded list when explicit dimensions are not given.
# A 52-card proof's longest list is 2m entries; this is pure allocation
# defence against a hostile peer, not a protocol constraint.
_MAX_LIST = 4096


# ------------------------------------------------------------------ encode

def _pt(point: Point) -> str:
    return bytes(point).hex()


def _pts(points: Sequence[Point]) -> List[str]:
    return [_pt(p) for p in points]


def _sc(scalar: Scalar) -> str:
    return bytes(scalar).hex()


def _scs(scalars: Sequence[Scalar]) -> List[str]:
    return [_sc(s) for s in scalars]


def _cts(ciphers: Sequence[Ciphertext]) -> List[List[str]]:
    return [list(c.to_hex()) for c in ciphers]


def _encode_zero(proof: ZeroProof) -> dict:
    return {
        "c_A0": _pt(proof.c_A0),
        "c_Bm": _pt(proof.c_Bm),
        "c_D": _pts(proof.c_D),
        "a_tilde": _scs(proof.a_tilde),
        "b_tilde": _scs(proof.b_tilde),
        "r_tilde": _sc(proof.r_tilde),
        "s_tilde": _sc(proof.s_tilde),
        "t_tilde": _sc(proof.t_tilde),
    }


def _encode_hadamard(proof: HadamardProof) -> dict:
    return {
        "c_B_interior": _pts(proof.c_B_interior),
        "zero": _encode_zero(proof.zero),
    }


def _encode_svp(proof: SVPProof) -> dict:
    return {
        "c_d": _pt(proof.c_d),
        "c_delta": _pt(proof.c_delta),
        "c_Delta": _pt(proof.c_Delta),
        "a_tilde": _scs(proof.a_tilde),
        "b_tilde": _scs(proof.b_tilde),
        "r_tilde": _sc(proof.r_tilde),
        "s_tilde": _sc(proof.s_tilde),
    }


def _encode_product(proof: ProductProof) -> dict:
    return {
        "c_b": _pt(proof.c_b),
        "hadamard": _encode_hadamard(proof.hadamard),
        "svp": _encode_svp(proof.svp),
    }


def _encode_multi(proof: MultiExponentiationProof) -> dict:
    return {
        "a_0_commit": _pt(proof.a_0_commit),
        "commit_b_k": _pts(proof.commit_b_k),
        "vector_e_k": _cts(proof.vector_e_k),
        "r_blinded": _sc(proof.r_blinded),
        "b_blinded": _sc(proof.b_blinded),
        "s_blinded": _sc(proof.s_blinded),
        "tau_blinded": _sc(proof.tau_blinded),
        "a_blinded": _scs(proof.a_blinded),
    }


def encode(proof: ShuffleProof) -> dict:
    """Encode a shuffle proof as JSON-safe nested dicts of hex strings."""
    return {
        "a_commits": _pts(proof.a_commits),
        "b_commits": _pts(proof.b_commits),
        "product": _encode_product(proof.product),
        "multi": _encode_multi(proof.multi),
    }


# ------------------------------------------------------------------ decode

def _obj(data: Any, field: str) -> dict:
    value = data[field]
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _seq(data: Any, field: str, expect: Optional[int]) -> list:
    value = data[field]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array")
    if expect is not None and len(value) != expect:
        raise ValueError(
            f"{field} has {len(value)} entries, expected {expect}")
    if len(value) > _MAX_LIST:
        raise ValueError(f"{field} exceeds the maximum decoded length")
    return list(value)


def _d_pt(value: Any) -> Point:
    if not isinstance(value, str):
        raise ValueError("point must be a hex string")
    return R.point_from_bytes(bytes.fromhex(value))


def _d_pts(data: Any, field: str, expect: Optional[int] = None) -> List[Point]:
    return [_d_pt(v) for v in _seq(data, field, expect)]


def _d_sc(value: Any) -> Scalar:
    if not isinstance(value, str):
        raise ValueError("scalar must be a hex string")
    return Scalar(bytes.fromhex(value))


def _d_scs(data: Any, field: str, expect: Optional[int] = None) -> List[Scalar]:
    return [_d_sc(v) for v in _seq(data, field, expect)]


def _d_ct(value: Any) -> Ciphertext:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("ciphertext must be a [c0_hex, c1_hex] pair")
    if not all(isinstance(part, str) for part in value):
        raise ValueError("ciphertext components must be hex strings")
    return Ciphertext.from_hex(value)


def _d_cts(data: Any, field: str,
           expect: Optional[int] = None) -> List[Ciphertext]:
    return [_d_ct(v) for v in _seq(data, field, expect)]


def _decode_zero(data: dict, m: Optional[int], n: Optional[int]) -> ZeroProof:
    return ZeroProof(
        c_A0=_d_pt(data["c_A0"]),
        c_Bm=_d_pt(data["c_Bm"]),
        c_D=_d_pts(data, "c_D", None if m is None else 2 * m + 1),
        a_tilde=_d_scs(data, "a_tilde", n),
        b_tilde=_d_scs(data, "b_tilde", n),
        r_tilde=_d_sc(data["r_tilde"]),
        s_tilde=_d_sc(data["s_tilde"]),
        t_tilde=_d_sc(data["t_tilde"]),
    )


def _decode_hadamard(data: dict, m: Optional[int],
                     n: Optional[int]) -> HadamardProof:
    return HadamardProof(
        c_B_interior=_d_pts(data, "c_B_interior",
                            None if m is None else m - 2),
        zero=_decode_zero(_obj(data, "zero"), m, n),
    )


def _decode_svp(data: dict, n: Optional[int]) -> SVPProof:
    return SVPProof(
        c_d=_d_pt(data["c_d"]),
        c_delta=_d_pt(data["c_delta"]),
        c_Delta=_d_pt(data["c_Delta"]),
        a_tilde=_d_scs(data, "a_tilde", n),
        b_tilde=_d_scs(data, "b_tilde", n),
        r_tilde=_d_sc(data["r_tilde"]),
        s_tilde=_d_sc(data["s_tilde"]),
    )


def _decode_product(data: dict, m: Optional[int],
                    n: Optional[int]) -> ProductProof:
    return ProductProof(
        c_b=_d_pt(data["c_b"]),
        hadamard=_decode_hadamard(_obj(data, "hadamard"), m, n),
        svp=_decode_svp(_obj(data, "svp"), n),
    )


def _decode_multi(data: dict, m: Optional[int],
                  n: Optional[int]) -> MultiExponentiationProof:
    width = None if m is None else 2 * m
    return MultiExponentiationProof(
        a_0_commit=_d_pt(data["a_0_commit"]),
        commit_b_k=_d_pts(data, "commit_b_k", width),
        vector_e_k=_d_cts(data, "vector_e_k", width),
        r_blinded=_d_sc(data["r_blinded"]),
        b_blinded=_d_sc(data["b_blinded"]),
        s_blinded=_d_sc(data["s_blinded"]),
        tau_blinded=_d_sc(data["tau_blinded"]),
        a_blinded=_d_scs(data, "a_blinded", n),
    )


def decode(data: Any, m: Optional[int] = None,
           n: Optional[int] = None) -> ShuffleProof:
    """Decode a shuffle proof, raising ``ValueError`` on anything malformed.

    Supplying ``m`` and ``n`` pins every list to the exact length those
    dimensions imply, so a structurally wrong proof is rejected here
    rather than deeper inside verification. Omitting them accepts any
    self-consistent shape up to a fixed allocation cap.
    """
    if m is not None and n is not None and (m < 2 or n < 2):
        raise ValueError("shuffle dimensions require m,n >= 2")
    try:
        if not isinstance(data, dict):
            raise ValueError("shuffle proof must be an object")
        return ShuffleProof(
            a_commits=_d_pts(data, "a_commits", m),
            b_commits=_d_pts(data, "b_commits", m),
            product=_decode_product(_obj(data, "product"), m, n),
            multi=_decode_multi(_obj(data, "multi"), m, n),
        )
    except ValueError:
        raise
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError(f"malformed shuffle proof: {exc}") from exc


__all__ = ["encode", "decode"]
