"""Ristretto255 group operations, backed by libsodium via ctypes.

This is the cryptographic foundation for the mental-poker shuffle
(docs/MULTIPLAYER.md Phase 2). It presents the small, safe interface the
protocol pseudocode assumes -- ``mul``, ``add``, ``sub``,
``random_scalar``, ``hash_to_group``, and scalar-field arithmetic -- over
libsodium's audited Ristretto255 primitives.

Why Ristretto255: it is a prime-order group built on Curve25519, so it
has no cofactor subtleties, every 32-byte value is either a single
canonical point or invalid (no ambiguous encodings), and libsodium
rejects non-canonical / low-order encodings for us. That property is a
security requirement here -- a shuffle proof that accepted a malformed
point could let a cheater smuggle a card that is not in the deck.

Points and scalars are represented as distinct subclasses of ``bytes``
so the type checker and ``isinstance`` catch the classic footgun of
passing a scalar where a point is expected. Both are always exactly 32
bytes.

The libsodium shared library is located at import time; see
``_load_libsodium`` for the search order. If it cannot be found or the
Ristretto API is not present, importing this module raises RuntimeError
with an actionable message rather than failing later at call time.
"""
from __future__ import annotations

import ctypes as _C
import os as _os
import sys as _sys
from pathlib import Path as _Path
from typing import Iterable as _Iterable


# --------------------------------------------------------------------------
# Library loading
# --------------------------------------------------------------------------

_LIB_BASENAMES = {
    "win32": ["libsodium.dll", "sodium.dll"],
    "darwin": ["libsodium.dylib", "libsodium.23.dylib"],
    "linux": ["libsodium.so", "libsodium.so.23", "libsodium.so.26"],
}


def _candidate_paths() -> _Iterable[_Path]:
    """Yield plausible libsodium locations, most-specific first.

    Search order:
      1. HOLDEM_LIBSODIUM env var (explicit override -- a full file path)
      2. a ``native/`` directory beside the repo (packaged/shipped layout)
      3. the poker-native build output on this dev machine
      4. any of the platform base names on the default loader path
    """
    plat = "win32" if _sys.platform.startswith("win") else \
           "darwin" if _sys.platform == "darwin" else "linux"
    names = _LIB_BASENAMES[plat]

    override = _os.environ.get("HOLDEM_LIBSODIUM")
    if override:
        yield _Path(override)

    repo_root = _Path(__file__).resolve().parents[2]
    native_dir = repo_root / "native"
    for n in names:
        yield native_dir / n

    # dev build output (see poker-native/_build_sodium.bat)
    home = _Path(_os.path.expanduser("~"))
    dev = home / "poker-native" / "libsodium" / "bin" / "x64" / "Release" / "v145" / "dynamic"
    for n in names:
        yield dev / n

    # bare name -- let the OS loader resolve it from PATH / ldconfig
    for n in names:
        yield _Path(n)


def _load_libsodium() -> _C.CDLL:
    tried = []
    for cand in _candidate_paths():
        try:
            if cand.is_absolute() and not cand.exists():
                tried.append(f"{cand} (not found)")
                continue
            lib = _C.CDLL(str(cand))
            # smoke-check that this build actually has the ristretto API
            if not hasattr(lib, "crypto_scalarmult_ristretto255_base"):
                tried.append(f"{cand} (no ristretto255 API)")
                continue
            return lib
        except OSError as exc:
            tried.append(f"{cand} ({exc})")

    raise RuntimeError(
        "libsodium with Ristretto255 support could not be loaded. Set the "
        "HOLDEM_LIBSODIUM environment variable to a libsodium shared library "
        "built with the ristretto255 API, or place one in the repo's native/ "
        "directory. Tried:\n  " + "\n  ".join(tried)
    )


_lib = _load_libsodium()

if _lib.sodium_init() < 0:              # 0 = ok, 1 = already initialised
    raise RuntimeError("sodium_init() failed")


# --------------------------------------------------------------------------
# ctypes signatures
# --------------------------------------------------------------------------

def _b(x: bytes):
    """bytes -> ctypes ubyte buffer (a fresh copy; safe as an input arg)."""
    return (_C.c_ubyte * len(x)).from_buffer_copy(x)


def _out(n: int = 32):
    return (_C.c_ubyte * n)()


for _fn, _res in [
    ("crypto_core_ristretto255_bytes", _C.c_size_t),
    ("crypto_core_ristretto255_scalarbytes", _C.c_size_t),
    ("crypto_core_ristretto255_hashbytes", _C.c_size_t),
    ("crypto_core_ristretto255_nonreducedscalarbytes", _C.c_size_t),
    ("crypto_scalarmult_ristretto255", _C.c_int),
    ("crypto_scalarmult_ristretto255_base", _C.c_int),
    ("crypto_core_ristretto255_add", _C.c_int),
    ("crypto_core_ristretto255_sub", _C.c_int),
    ("crypto_core_ristretto255_from_hash", _C.c_int),
    ("crypto_core_ristretto255_is_valid_point", _C.c_int),
    ("crypto_core_ristretto255_scalar_invert", _C.c_int),
    ("sodium_version_string", _C.c_char_p),
]:
    getattr(_lib, _fn).restype = _res

POINT_BYTES: int = _lib.crypto_core_ristretto255_bytes()          # 32
SCALAR_BYTES: int = _lib.crypto_core_ristretto255_scalarbytes()   # 32
HASH_BYTES: int = _lib.crypto_core_ristretto255_hashbytes()       # 64
_WIDE_BYTES: int = _lib.crypto_core_ristretto255_nonreducedscalarbytes()  # 64

assert POINT_BYTES == 32 and SCALAR_BYTES == 32 and HASH_BYTES == 64


def libsodium_version() -> str:
    return _lib.sodium_version_string().decode()


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------

class Point(bytes):
    """A Ristretto255 group element (32 canonical bytes)."""

    __slots__ = ()

    def __new__(cls, data: bytes):
        if len(data) != POINT_BYTES:
            raise ValueError(f"point must be {POINT_BYTES} bytes, got {len(data)}")
        return super().__new__(cls, data)

    def is_valid(self) -> bool:
        """True iff this is a canonical, non-low-order encoding."""
        return _lib.crypto_core_ristretto255_is_valid_point(_b(self)) == 1

    def __repr__(self) -> str:
        return f"Point({self.hex()[:16]}...)"


class Scalar(bytes):
    """A scalar in the Ristretto255 group-order field (32 bytes, little-endian)."""

    __slots__ = ()

    def __new__(cls, data: bytes):
        if len(data) != SCALAR_BYTES:
            raise ValueError(f"scalar must be {SCALAR_BYTES} bytes, got {len(data)}")
        return super().__new__(cls, data)

    def __repr__(self) -> str:
        return f"Scalar({self.hex()[:16]}...)"


# --------------------------------------------------------------------------
# Scalar field arithmetic
# --------------------------------------------------------------------------

def random_scalar() -> Scalar:
    """A uniformly random non-zero scalar."""
    out = _out(SCALAR_BYTES)
    _lib.crypto_core_ristretto255_scalar_random(out)
    return Scalar(bytes(out))


def scalar_reduce(wide: bytes) -> Scalar:
    """Reduce 64 arbitrary bytes into the scalar field (for Fiat-Shamir)."""
    if len(wide) != _WIDE_BYTES:
        raise ValueError(f"scalar_reduce expects {_WIDE_BYTES} bytes")
    out = _out(SCALAR_BYTES)
    _lib.crypto_core_ristretto255_scalar_reduce(out, _b(wide))
    return Scalar(bytes(out))


def scalar_add(a: Scalar, b: Scalar) -> Scalar:
    out = _out(SCALAR_BYTES)
    _lib.crypto_core_ristretto255_scalar_add(out, _b(a), _b(b))
    return Scalar(bytes(out))


def scalar_sub(a: Scalar, b: Scalar) -> Scalar:
    out = _out(SCALAR_BYTES)
    _lib.crypto_core_ristretto255_scalar_sub(out, _b(a), _b(b))
    return Scalar(bytes(out))


def scalar_mul(a: Scalar, b: Scalar) -> Scalar:
    out = _out(SCALAR_BYTES)
    _lib.crypto_core_ristretto255_scalar_mul(out, _b(a), _b(b))
    return Scalar(bytes(out))


def scalar_negate(a: Scalar) -> Scalar:
    out = _out(SCALAR_BYTES)
    _lib.crypto_core_ristretto255_scalar_negate(out, _b(a))
    return Scalar(bytes(out))


def scalar_invert(a: Scalar) -> Scalar:
    out = _out(SCALAR_BYTES)
    rc = _lib.crypto_core_ristretto255_scalar_invert(out, _b(a))
    if rc != 0:
        raise ValueError("scalar has no inverse (is it zero?)")
    return Scalar(bytes(out))


# --------------------------------------------------------------------------
# Group operations
# --------------------------------------------------------------------------

def mul(k: Scalar, p: Point) -> Point:
    """k . p. Raises ValueError if p is the identity or invalid."""
    out = _out(POINT_BYTES)
    rc = _lib.crypto_scalarmult_ristretto255(out, _b(k), _b(p))
    if rc != 0:
        raise ValueError("scalarmult failed (identity or invalid point)")
    return Point(bytes(out))


def mul_base(k: Scalar) -> Point:
    """k . G, where G is the Ristretto255 base point."""
    out = _out(POINT_BYTES)
    rc = _lib.crypto_scalarmult_ristretto255_base(out, _b(k))
    if rc != 0:
        raise ValueError("base scalarmult failed (zero scalar?)")
    return Point(bytes(out))


def add(a: Point, b: Point) -> Point:
    out = _out(POINT_BYTES)
    rc = _lib.crypto_core_ristretto255_add(out, _b(a), _b(b))
    if rc != 0:
        raise ValueError("point add failed (invalid input point)")
    return Point(bytes(out))


def sub(a: Point, b: Point) -> Point:
    out = _out(POINT_BYTES)
    rc = _lib.crypto_core_ristretto255_sub(out, _b(a), _b(b))
    if rc != 0:
        raise ValueError("point sub failed (invalid input point)")
    return Point(bytes(out))


def hash_to_group(data: bytes) -> Point:
    """Map arbitrary bytes to a group element (RFC 9380 hash-to-ristretto255).

    ``data`` must be exactly 64 bytes of uniform input (e.g. a SHA-512
    digest). The result is a valid point with no known discrete-log
    relation to G -- this is how cards are encoded as points.
    """
    if len(data) != HASH_BYTES:
        raise ValueError(f"hash_to_group expects {HASH_BYTES} bytes, got {len(data)}")
    out = _out(POINT_BYTES)
    _lib.crypto_core_ristretto255_from_hash(out, _b(data))
    return Point(bytes(out))


def point_from_bytes(data: bytes) -> Point:
    """Parse 32 bytes off the wire into a validated Point.

    Rejects non-canonical and low-order encodings -- use this for anything
    that arrives from another peer.
    """
    p = Point(data)
    if not p.is_valid():
        raise ValueError("non-canonical or low-order Ristretto255 point")
    return p


def scalar_from_bytes(data: bytes) -> Scalar:
    return Scalar(data)


# The Ristretto255 base point G (= 1*G). Exposed as a constant because the
# DLEQ and shuffle proofs hash it into their Fiat-Shamir challenges.
G: Point = mul_base(Scalar((1).to_bytes(SCALAR_BYTES, "little")))


__all__ = [
    "Point", "Scalar", "G",
    "POINT_BYTES", "SCALAR_BYTES", "HASH_BYTES",
    "libsodium_version",
    "random_scalar", "scalar_reduce",
    "scalar_add", "scalar_sub", "scalar_mul", "scalar_negate", "scalar_invert",
    "mul", "mul_base", "add", "sub", "hash_to_group",
    "point_from_bytes", "scalar_from_bytes",
]
