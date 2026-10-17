"""Ristretto255 operations with an explicitly supplied backend.

Importing this module does not discover, load or initialize a native library.
An embedding host calls ``initialize(backend)`` before arithmetic. The desktop
host supplies libsodium through ``holdem.native_crypto.initialize()``.
Public points, scalars and deterministic constants retain their byte encoding.
"""
from __future__ import annotations

from threading import Lock
from typing import Protocol

POINT_BYTES = 32
SCALAR_BYTES = 32
HASH_BYTES = 64
_WIDE_BYTES = 64


class CryptoNotInitialized(RuntimeError):
    """A host attempted native arithmetic before installing its backend."""


class Backend(Protocol):
    """Stateless byte operations supplied by the embedding host.

    Backends validate canonical points and preserve libsodium's error and
    zero-scalar behavior. They are trusted capabilities, like the host signer.
    """
    def version(self) -> str: ...
    def point_is_valid(self, point: bytes) -> bool: ...
    def random_scalar(self) -> bytes: ...
    def scalar_reduce(self, wide: bytes) -> bytes: ...
    def scalar_add(self, a: bytes, b: bytes) -> bytes: ...
    def scalar_sub(self, a: bytes, b: bytes) -> bytes: ...
    def scalar_mul(self, a: bytes, b: bytes) -> bytes: ...
    def scalar_negate(self, a: bytes) -> bytes: ...
    def scalar_invert(self, a: bytes) -> bytes: ...
    def mul(self, scalar: bytes, point: bytes) -> bytes: ...
    def mul_base(self, scalar: bytes) -> bytes: ...
    def add(self, a: bytes, b: bytes) -> bytes: ...
    def sub(self, a: bytes, b: bytes) -> bytes: ...
    def hash_to_group(self, data: bytes) -> bytes: ...


_METHODS = (
    'version', 'point_is_valid', 'random_scalar', 'scalar_reduce',
    'scalar_add', 'scalar_sub', 'scalar_mul', 'scalar_negate', 'scalar_invert',
    'mul', 'mul_base', 'add', 'sub', 'hash_to_group',
)
_backend: Backend | None = None
_backend_lock = Lock()


def initialize(backend: Backend) -> None:
    """Install a fully initialized backend once; the same object is idempotent.

    The host prepares the capability before this atomic publication. Replacing
    it during a running hand is forbidden. Failed validation publishes nothing.
    """
    global _backend
    with _backend_lock:
        if _backend is backend and backend is not None:
            return
        if _backend is not None:
            raise RuntimeError('Ristretto backend is already initialized; replacement is forbidden')
        missing = [name for name in _METHODS if not callable(getattr(backend, name, None))]
        if missing:
            raise TypeError('Ristretto backend is missing operations: ' + ', '.join(missing))
        _backend = backend


def is_initialized() -> bool:
    return _backend is not None


def _require_backend() -> Backend:
    backend = _backend
    if backend is None:
        raise CryptoNotInitialized(
            'Ristretto backend is not initialized. The host must explicitly '
            'initialize its crypto backend before starting a mental-poker hand.')
    return backend


def require_initialized() -> None:
    """Refuse a hand before state changes if its host omitted crypto startup."""
    _require_backend()


def libsodium_version() -> str:
    """Compatibility accessor for the installed backend's version."""
    return _require_backend().version()


class Point(bytes):
    """A Ristretto255 group element (32 canonical bytes)."""
    __slots__ = ()

    def __new__(cls, data: bytes):
        if len(data) != POINT_BYTES:
            raise ValueError(f'point must be {POINT_BYTES} bytes, got {len(data)}')
        return super().__new__(cls, data)

    def is_valid(self) -> bool:
        return _require_backend().point_is_valid(bytes(self))

    def __repr__(self) -> str:
        return f'Point({self.hex()[:16]}...)'


class Scalar(bytes):
    """A scalar in the Ristretto255 group-order field (little-endian)."""
    __slots__ = ()

    def __new__(cls, data: bytes):
        if len(data) != SCALAR_BYTES:
            raise ValueError(f'scalar must be {SCALAR_BYTES} bytes, got {len(data)}')
        return super().__new__(cls, data)

    def __repr__(self) -> str:
        return f'Scalar({self.hex()[:16]}...)'


def random_scalar() -> Scalar:
    return Scalar(_require_backend().random_scalar())


def scalar_reduce(wide: bytes) -> Scalar:
    if len(wide) != _WIDE_BYTES:
        raise ValueError(f'scalar_reduce expects {_WIDE_BYTES} bytes')
    return Scalar(_require_backend().scalar_reduce(wide))


def scalar_add(a: Scalar, b: Scalar) -> Scalar:
    return Scalar(_require_backend().scalar_add(a, b))


def scalar_sub(a: Scalar, b: Scalar) -> Scalar:
    return Scalar(_require_backend().scalar_sub(a, b))


def scalar_mul(a: Scalar, b: Scalar) -> Scalar:
    return Scalar(_require_backend().scalar_mul(a, b))


def scalar_negate(a: Scalar) -> Scalar:
    return Scalar(_require_backend().scalar_negate(a))


def scalar_invert(a: Scalar) -> Scalar:
    return Scalar(_require_backend().scalar_invert(a))


def mul(k: Scalar, p: Point) -> Point:
    return Point(_require_backend().mul(k, p))


def mul_base(k: Scalar) -> Point:
    return Point(_require_backend().mul_base(k))


def add(a: Point, b: Point) -> Point:
    return Point(_require_backend().add(a, b))


def sub(a: Point, b: Point) -> Point:
    return Point(_require_backend().sub(a, b))


def hash_to_group(data: bytes) -> Point:
    if len(data) != HASH_BYTES:
        raise ValueError(f'hash_to_group expects {HASH_BYTES} bytes, got {len(data)}')
    return Point(_require_backend().hash_to_group(data))


def point_from_bytes(data: bytes) -> Point:
    point = Point(data)
    if not point.is_valid():
        raise ValueError('non-canonical or low-order Ristretto255 point')
    return point


def scalar_from_bytes(data: bytes) -> Scalar:
    return Scalar(data)


# Canonical encodings, independently checked against their native derivations.
G = Point(bytes.fromhex('e2f2ae0a6abc4e71a884a961c500515f58e30b6aa582dd8db6a65945e08d2d76'))
IDENTITY = Point(b'\x00' * POINT_BYTES)
_ZERO_SCALAR = Scalar(b'\x00' * SCALAR_BYTES)


def is_zero_scalar(s: Scalar) -> bool:
    return bytes(s) == bytes(_ZERO_SCALAR)


def mul_safe(k: Scalar, p: Point) -> Point:
    _require_backend()
    if is_zero_scalar(k) or bytes(p) == bytes(IDENTITY):
        return IDENTITY
    return mul(k, p)


def mul_base_safe(k: Scalar) -> Point:
    _require_backend()
    return IDENTITY if is_zero_scalar(k) else mul_base(k)


def multiscalar_mul(scalars, points) -> Point:
    _require_backend()
    scalars, points = list(scalars), list(points)
    if len(scalars) != len(points):
        raise ValueError('multiscalar_mul: scalars and points differ in length')
    acc = IDENTITY
    for scalar, point in zip(scalars, points):
        acc = add(acc, mul_safe(scalar, point))
    return acc


__all__ = [
    'Backend', 'CryptoNotInitialized', 'initialize', 'is_initialized',
    'require_initialized',
    'Point', 'Scalar', 'G', 'IDENTITY', 'POINT_BYTES', 'SCALAR_BYTES', 'HASH_BYTES',
    'libsodium_version', 'random_scalar', 'scalar_reduce', 'is_zero_scalar',
    'scalar_add', 'scalar_sub', 'scalar_mul', 'scalar_negate', 'scalar_invert',
    'mul', 'mul_base', 'mul_safe', 'mul_base_safe', 'multiscalar_mul',
    'add', 'sub', 'hash_to_group', 'point_from_bytes', 'scalar_from_bytes',
]
