"""Explicit desktop startup for the libsodium Ristretto backend.

Importing this host adapter is inert. ``initialize()`` owns native discovery,
ABI checks and sodium initialization, then atomically supplies the capability
to the core. A failed attempt publishes nothing and can be retried.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from threading import Lock

from holdem.p2p import ristretto as R


class NativeCryptoUnavailable(RuntimeError):
    """No loadable library with the required Ristretto API was found."""


class NativeCryptoInitializationError(RuntimeError):
    """A present native runtime failed initialization or its ABI contract."""


_LIB_BASENAMES = {
    'win32': ('libsodium.dll', 'sodium.dll'),
    'darwin': ('libsodium.dylib', 'libsodium.23.dylib'),
    'linux': ('libsodium.so', 'libsodium.so.23', 'libsodium.so.26'),
}
_SIZES = {
    'crypto_core_ristretto255_bytes': 32,
    'crypto_core_ristretto255_scalarbytes': 32,
    'crypto_core_ristretto255_hashbytes': 64,
    'crypto_core_ristretto255_nonreducedscalarbytes': 64,
}
_REQUIRED = (
    'sodium_init', 'sodium_version_string', *_SIZES,
    'crypto_core_ristretto255_scalar_random',
    'crypto_core_ristretto255_scalar_reduce',
    'crypto_core_ristretto255_scalar_add',
    'crypto_core_ristretto255_scalar_sub',
    'crypto_core_ristretto255_scalar_mul',
    'crypto_core_ristretto255_scalar_negate',
    'crypto_core_ristretto255_scalar_invert',
    'crypto_scalarmult_ristretto255', 'crypto_scalarmult_ristretto255_base',
    'crypto_core_ristretto255_add', 'crypto_core_ristretto255_sub',
    'crypto_core_ristretto255_from_hash', 'crypto_core_ristretto255_is_valid_point',
)


def _candidate_paths():
    platform = ('win32' if sys.platform.startswith('win') else
                'darwin' if sys.platform == 'darwin' else 'linux')
    names = _LIB_BASENAMES[platform]
    override = os.environ.get('HOLDEM_LIBSODIUM')
    if override:
        yield Path(override)
    native = Path(__file__).resolve().parents[1] / 'native'
    for name in names:
        yield native / name
    dev = (Path(os.path.expanduser('~')) / 'poker-native' / 'libsodium' /
           'bin' / 'x64' / 'Release' / 'v145' / 'dynamic')
    for name in names:
        yield dev / name
    for name in names:
        yield Path(name)


def _load_libsodium():
    import ctypes

    tried = []
    for candidate in _candidate_paths():
        try:
            if candidate.is_absolute() and not candidate.exists():
                tried.append(f'{candidate} (not found)')
                continue
            library = ctypes.CDLL(str(candidate))
        except OSError as exc:
            tried.append(f'{candidate} ({exc})')
            continue
        missing = [name for name in _REQUIRED if not hasattr(library, name)]
        if missing:
            tried.append(f'{candidate} (missing required API: {", ".join(missing)})')
            continue
        return library
    raise NativeCryptoUnavailable(
        'libsodium with Ristretto255 support could not be loaded. Set '
        'HOLDEM_LIBSODIUM to a compatible shared library or place one in '
        "the repo's native/ directory. Tried:\n  " + '\n  '.join(tried))


class _LibsodiumBackend:
    def __init__(self, library):
        import ctypes

        self._ctypes = ctypes
        self._library = library
        pointer = ctypes.POINTER(ctypes.c_ubyte)
        try:
            for name in _SIZES:
                function = getattr(library, name)
                function.argtypes, function.restype = [], ctypes.c_size_t
            library.sodium_init.argtypes, library.sodium_init.restype = [], ctypes.c_int
            library.sodium_version_string.argtypes = []
            library.sodium_version_string.restype = ctypes.c_char_p
            signatures = {
                'crypto_core_ristretto255_scalar_random': (1, None),
                'crypto_core_ristretto255_scalar_reduce': (2, None),
                'crypto_core_ristretto255_scalar_add': (3, None),
                'crypto_core_ristretto255_scalar_sub': (3, None),
                'crypto_core_ristretto255_scalar_mul': (3, None),
                'crypto_core_ristretto255_scalar_negate': (2, None),
                'crypto_core_ristretto255_scalar_invert': (2, ctypes.c_int),
                'crypto_scalarmult_ristretto255': (3, ctypes.c_int),
                'crypto_scalarmult_ristretto255_base': (2, ctypes.c_int),
                'crypto_core_ristretto255_add': (3, ctypes.c_int),
                'crypto_core_ristretto255_sub': (3, ctypes.c_int),
                'crypto_core_ristretto255_from_hash': (2, ctypes.c_int),
                'crypto_core_ristretto255_is_valid_point': (1, ctypes.c_int),
            }
            for name, (arity, result_type) in signatures.items():
                function = getattr(library, name)
                function.argtypes, function.restype = [pointer] * arity, result_type
            if library.sodium_init() < 0:
                raise NativeCryptoInitializationError('sodium_init() failed')
            for name, expected in _SIZES.items():
                actual = getattr(library, name)()
                if actual != expected:
                    raise NativeCryptoInitializationError(
                        f'libsodium ABI mismatch: {name} returned {actual}, expected {expected}')
            # Version decoding is validated before any capability is published.
            self._version = library.sodium_version_string().decode()
        except NativeCryptoInitializationError:
            raise
        except Exception as exc:
            raise NativeCryptoInitializationError(
                f'Could not configure the libsodium Ristretto backend: {exc}') from exc

    def version(self):
        return self._version

    def _input(self, value, length=32):
        if len(value) != length:
            raise ValueError(f'native input must be {length} bytes, got {len(value)}')
        return (self._ctypes.c_ubyte * length).from_buffer_copy(value)

    def _output(self, name, *values, wide=False, error=None):
        output = (self._ctypes.c_ubyte * 32)()
        inputs = [self._input(value, 64 if wide else 32) for value in values]
        result = getattr(self._library, name)(output, *inputs)
        if error is not None and result != 0:
            raise ValueError(error)
        return bytes(output)

    def point_is_valid(self, point):
        return self._library.crypto_core_ristretto255_is_valid_point(self._input(point)) == 1

    def random_scalar(self):
        return self._output('crypto_core_ristretto255_scalar_random')

    def scalar_reduce(self, wide):
        return self._output('crypto_core_ristretto255_scalar_reduce', wide, wide=True)

    def scalar_add(self, a, b):
        return self._output('crypto_core_ristretto255_scalar_add', a, b)

    def scalar_sub(self, a, b):
        return self._output('crypto_core_ristretto255_scalar_sub', a, b)

    def scalar_mul(self, a, b):
        return self._output('crypto_core_ristretto255_scalar_mul', a, b)

    def scalar_negate(self, a):
        return self._output('crypto_core_ristretto255_scalar_negate', a)

    def scalar_invert(self, a):
        return self._output('crypto_core_ristretto255_scalar_invert', a,
                            error='scalar has no inverse (is it zero?)')

    def mul(self, scalar, point):
        return self._output('crypto_scalarmult_ristretto255', scalar, point,
                            error='scalarmult failed (identity or invalid point)')

    def mul_base(self, scalar):
        return self._output('crypto_scalarmult_ristretto255_base', scalar,
                            error='base scalarmult failed (zero scalar?)')

    def add(self, a, b):
        return self._output('crypto_core_ristretto255_add', a, b,
                            error='point add failed (invalid input point)')

    def sub(self, a, b):
        return self._output('crypto_core_ristretto255_sub', a, b,
                            error='point sub failed (invalid input point)')

    def hash_to_group(self, data):
        return self._output('crypto_core_ristretto255_from_hash', data, wide=True,
                            error='hash-to-group failed')


def _load_backend():
    return _LibsodiumBackend(_load_libsodium())


_backend = None
_backend_path = None
_initialization_lock = Lock()


def _selected_library(path):
    """Load only a caller-selected library, without discovery or fallback."""
    import ctypes

    try:
        selected = Path(path).resolve(strict=True)
        if not selected.is_file():
            raise OSError('selected native library is not a regular file')
        library = ctypes.CDLL(str(selected))
    except (OSError, TypeError, ValueError) as exc:
        raise NativeCryptoUnavailable(
            f'The explicitly selected libsodium library could not be loaded: {exc}') from exc
    missing = [name for name in _REQUIRED if not hasattr(library, name)]
    if missing:
        raise NativeCryptoUnavailable(
            'The explicitly selected libsodium library is missing required API: '
            + ', '.join(missing))
    return selected, library


def initialize(*, library_path: str | os.PathLike | None = None) -> R.Backend:
    """Initialize desktop crypto once, before sessions, sockets or workers.

    Calls after success return the same capability. A load, initialization,
    ABI or publication failure leaves this adapter uninitialized; a later
    explicit call retries. A different core backend is never replaced.
    ``library_path`` selects exactly one file and disables discovery/fallback.
    A later explicit selection must match the already loaded native library.
    """
    global _backend, _backend_path
    with _initialization_lock:
        if _backend is not None:
            if library_path is not None:
                try:
                    selected = Path(library_path).resolve(strict=True)
                except (OSError, TypeError, ValueError) as exc:
                    raise NativeCryptoUnavailable(
                        f'The explicitly selected libsodium library could not be loaded: {exc}') from exc
                if selected != _backend_path:
                    raise NativeCryptoInitializationError(
                        'A different native crypto library is already initialized; replacement is forbidden')
            return _backend
        if R.is_initialized():
            raise NativeCryptoInitializationError(
                'The core already has a different crypto backend; replacement is forbidden')
        if library_path is None:
            backend = _load_backend()
            name = getattr(getattr(backend, '_library', None), '_name', None)
            selected = Path(name).resolve() if name and Path(name).is_absolute() else None
        else:
            selected, library = _selected_library(library_path)
            backend = _LibsodiumBackend(library)
        R.initialize(backend)
        _backend = backend
        _backend_path = selected
        return backend


__all__ = ['initialize', 'NativeCryptoUnavailable', 'NativeCryptoInitializationError']
