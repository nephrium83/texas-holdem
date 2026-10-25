"""Cold startup, publication, and byte compatibility of native crypto.

Every lifecycle check gets a fresh interpreter. The pytest reporter and
other crypto suites may already have initialized the parent interpreter.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

REPO = Path(__file__).resolve().parents[1]


def _run(script, tmp_path):
    env = dict(os.environ, PYTHONPATH=str(REPO),
               HOLDEM_CONFIG_DIR=str(tmp_path / "config"))
    result = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(script)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


_FAKE_BACKEND = """
class FakeBackend:
    def version(self):
        return 'test-backend'
    def __getattr__(self, name):
        return lambda *args: bytes(32)
"""


def test_valid_native_operations_require_startup_even_on_zero_fast_paths(tmp_path):
    _run('''
        import ctypes
        from pathlib import Path
        def forbidden(*args, **kwargs):
            raise AssertionError('operation tried to discover native crypto')
        ctypes.CDLL = forbidden
        Path.exists = forbidden
        from holdem.p2p import ristretto as R
        assert not R.is_initialized()
        zero = R.Scalar(bytes(32))
        one = R.Scalar(bytes([1]) + bytes(31))
        calls = [
            R.require_initialized, R.libsodium_version, R.random_scalar, R.G.is_valid,
            lambda: R.scalar_reduce(bytes(64)),
            lambda: R.scalar_add(one, one), lambda: R.scalar_sub(one, one),
            lambda: R.scalar_mul(one, one), lambda: R.scalar_negate(one),
            lambda: R.scalar_invert(one), lambda: R.mul(one, R.G),
            lambda: R.mul_base(one), lambda: R.add(R.G, R.G),
            lambda: R.sub(R.G, R.G), lambda: R.hash_to_group(bytes(64)),
            lambda: R.point_from_bytes(bytes(R.G)),
            lambda: R.mul_safe(zero, R.G),
            lambda: R.mul_safe(one, R.IDENTITY),
            lambda: R.mul_base_safe(zero),
            lambda: R.multiscalar_mul([], []),
        ]
        for call in calls:
            try:
                call()
            except R.CryptoNotInitialized as exc:
                assert 'host' in str(exc).lower()
            else:
                raise AssertionError('operation succeeded without host startup')
        assert not R.is_initialized()
    ''', tmp_path)


@pytest.mark.parametrize("entrypoint", ["begin_hand", "start_p2p_hand"])
def test_forgotten_startup_cannot_load_secrets_or_partially_start_a_hand(
        entrypoint, tmp_path):
    _run(f'''
        import copy
        from holdem.p2p import ristretto as R
        from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
        from holdem.p2p.session import Session
        from holdem.p2p.timeout import FakeClock
        assert not R.is_initialized()
        calls = []
        def secret():
            calls.append('secret')
            return bytes(range(32))
        class RecordingTransport(InMemoryTransport):
            def broadcast(self, message):
                calls.append('broadcast')
                super().broadcast(message)
            def send(self, target, message):
                calls.append('send')
                super().send(target, message)
        bus = InMemoryBus()
        transport = RecordingTransport(bus, 'peer')
        session = Session(is_host=False, nickname='Peer', avatar_b64='',
                          transport=transport, clock=FakeClock(),
                          secret_provider=secret)
        session.local_conn_id = 'peer'
        session.configure_seats(['peer', 'remote'])
        session._adopt_deal_policy(Session.DEAL_POLICY_BG)
        assert session.seat_order == ['peer', 'remote']
        assert session.replica is None and not R.is_initialized()
        assert calls == [], 'lobby configuration accessed crypto or the secret'

        def bind():
            calls.append('seat binding')
        session._bind_seat_keys = bind
        prefixes = ('_deal', '_table', '_hand', '_session', '_seat', '_master',
                    '_msg', '_final', '_p2p')
        def snapshot():
            values = {{name: value for name, value in vars(session).items()
                       if name.startswith(prefixes)}}
            identities = {{name: id(value) for name, value in values.items()
                           if isinstance(value, (list, dict, set))}}
            return copy.deepcopy(values), identities
        before = snapshot()
        try:
            if {entrypoint!r} == 'begin_hand':
                session.begin_hand(1, button=0)
            else:
                session.start_p2p_hand(hand_no=1, names=['Peer', 'Remote'],
                                       stacks=[1000, 1000], sb=5, bb=10)
        except R.CryptoNotInitialized:
            pass
        else:
            raise AssertionError('hand started without host initialization')
        assert calls == [], f'failed hand performed side effects: {{calls}}'
        assert snapshot() == before, 'failed startup partially changed hand state'
        assert session.replica is None
        assert session._deal_driver is None
        assert bus.pending == 0
    ''', tmp_path)


def test_embedding_host_can_supply_crypto_without_loading_the_desktop_adapter(tmp_path):
    _run(_FAKE_BACKEND + '''
import ctypes, sys
from holdem.p2p import ristretto as R
ctypes.CDLL = lambda *args: (_ for _ in ()).throw(AssertionError('CDLL'))
backend = FakeBackend()
R.initialize(backend)
R.initialize(backend)
assert R.libsodium_version() == 'test-backend'
assert R.random_scalar() == bytes(32)
assert 'holdem.native_crypto' not in sys.modules
try:
    R.initialize(FakeBackend())
except RuntimeError as exc:
    assert 'replacement' in str(exc)
else:
    raise AssertionError('running backend was replaced')
''', tmp_path)


def test_invalid_backend_publishes_nothing_and_a_valid_retry_works(tmp_path):
    _run(_FAKE_BACKEND + '''
from holdem.p2p import ristretto as R
try:
    R.initialize(object())
except TypeError as exc:
    assert 'missing operations' in str(exc)
else:
    raise AssertionError('incomplete backend accepted')
assert not R.is_initialized()
R.initialize(FakeBackend())
assert R.is_initialized()
''', tmp_path)


def test_concurrent_host_startup_publishes_one_complete_backend(tmp_path):
    _run(_FAKE_BACKEND + '''
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from holdem import native_crypto as N
from holdem.p2p import ristretto as R
entered, release = Event(), Event()
barrier = Barrier(16)
backend = FakeBackend()
calls = []
def load():
    calls.append('load')
    assert not R.is_initialized()
    entered.set()
    assert release.wait(5)
    return backend
N._load_backend = load
def start():
    barrier.wait(timeout=5)
    return N.initialize()
with ThreadPoolExecutor(max_workers=16) as pool:
    futures = [pool.submit(start) for _ in range(16)]
    assert entered.wait(5)
    assert not R.is_initialized(), 'backend escaped the startup boundary'
    release.set()
    results = [future.result(timeout=5) for future in futures]
assert calls == ['load']
assert all(result is backend for result in results)
assert N.initialize() is backend
assert R.libsodium_version() == 'test-backend'
''', tmp_path)


@pytest.mark.parametrize("error", ["NativeCryptoUnavailable", "NativeCryptoInitializationError"])
def test_failed_host_startup_does_not_publish_and_explicit_retry_works(error, tmp_path):
    _run(_FAKE_BACKEND + f'''
from holdem import native_crypto as N
from holdem.p2p import ristretto as R
calls = []
def load():
    calls.append('load')
    if len(calls) == 1:
        raise N.{error}('controlled startup failure')
    return FakeBackend()
N._load_backend = load
try:
    N.initialize()
except N.{error}:
    pass
else:
    raise AssertionError('startup failure was swallowed')
assert not R.is_initialized()
assert N._backend is None
backend = N.initialize()
assert N.initialize() is backend
assert calls == ['load', 'load']
''', tmp_path)


def test_concurrent_failed_attempts_never_publish_a_partial_backend(tmp_path):
    _run(_FAKE_BACKEND + '''
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from holdem import native_crypto as N
from holdem.p2p import ristretto as R
barrier = Barrier(8)
attempts = []
def load():
    attempts.append('load')
    assert not R.is_initialized()
    assert N._backend is None
    raise N.NativeCryptoInitializationError('controlled concurrent failure')
N._load_backend = load
def start():
    barrier.wait(timeout=5)
    try:
        N.initialize()
    except N.NativeCryptoInitializationError as exc:
        return str(exc)
    raise AssertionError('failed startup returned')
with ThreadPoolExecutor(max_workers=8) as pool:
    results = list(pool.map(lambda _: start(), range(8)))
assert results == ['controlled concurrent failure'] * 8
assert len(attempts) == 8, 'each explicit failed call must remain retryable'
assert N._backend is None and not R.is_initialized()
N._load_backend = FakeBackend
assert N.initialize() is N.initialize()
''', tmp_path)


def test_missing_and_unsupported_libraries_have_a_typed_diagnosis(tmp_path):
    _run('''
        import ctypes
        from pathlib import Path
        from holdem import native_crypto as N
        from holdem.p2p import ristretto as R
        N._candidate_paths = lambda: iter((Path('absent'), Path('unsupported')))
        calls = []
        def load(path):
            calls.append(path)
            if path == 'absent':
                raise OSError('controlled missing library')
            return object()
        ctypes.CDLL = load
        try:
            N.initialize()
        except N.NativeCryptoUnavailable as exc:
            message = str(exc)
            assert 'HOLDEM_LIBSODIUM' in message
            assert 'controlled missing library' in message
            assert 'unsupported' in message and 'missing required API' in message
        else:
            raise AssertionError('missing native runtime was accepted')
        assert calls == ['absent', 'unsupported']
        assert not R.is_initialized()
        assert N._backend is None
    ''', tmp_path)


def test_explicit_missing_library_cannot_fall_back_to_developer_runtime(tmp_path):
    _run('''
        import ctypes
        from pathlib import Path
        from holdem import native_crypto as N
        from holdem.p2p import ristretto as R
        def forbidden(*args):
            raise AssertionError('explicit selection attempted native discovery')
        N._candidate_paths = forbidden
        N._load_backend = forbidden
        ctypes.CDLL = forbidden
        try:
            N.initialize(library_path=Path('missing-packaged.dll'))
        except N.NativeCryptoUnavailable:
            pass
        else:
            raise AssertionError('missing packaged library used a fallback')
        assert N._backend is None and not R.is_initialized()
    ''', tmp_path)


def test_explicit_library_loads_only_the_selected_absolute_file(tmp_path):
    _run('''
        import ctypes
        from pathlib import Path
        from holdem import native_crypto as N
        from holdem.p2p import ristretto as R
        selected = Path('packaged.dll')
        selected.write_bytes(b'controlled fixture')
        calls = []
        def load(path):
            calls.append(path)
            return object()
        ctypes.CDLL = load
        N._candidate_paths = lambda: (_ for _ in ()).throw(AssertionError('discovery'))
        try:
            N.initialize(library_path=selected)
        except N.NativeCryptoUnavailable as exc:
            assert 'missing required API' in str(exc)
        else:
            raise AssertionError('incompatible selected runtime accepted')
        assert calls == [str(selected.resolve())]
        assert N._backend is None and not R.is_initialized()
    ''', tmp_path)


def test_explicit_runtime_is_idempotent_and_cannot_replace_another_path(tmp_path):
    _run(_FAKE_BACKEND + '''
from pathlib import Path
from holdem import native_crypto as N
selected = Path('selected.dll').resolve()
other = Path('other.dll').resolve()
selected.write_bytes(b'selected')
other.write_bytes(b'other')
loads = []
def load(path):
    loads.append(path)
    return selected, object()
N._selected_library = load
N._LibsodiumBackend = lambda library: FakeBackend()
backend = N.initialize(library_path=selected)
assert N.initialize() is backend
assert N.initialize(library_path=selected) is backend
try:
    N.initialize(library_path=other)
except N.NativeCryptoInitializationError as exc:
    assert 'replacement is forbidden' in str(exc)
else:
    raise AssertionError('explicit runtime replaced an initialized backend')
assert loads == [selected]
assert N.initialize() is backend
''', tmp_path)


@pytest.mark.parametrize("mode", ["sodium-init", "abi-size", "version", "signature"])
def test_present_but_broken_native_runtime_is_never_optional(mode, tmp_path):
    _run(f'''
        from holdem import native_crypto as N
        from holdem.p2p import ristretto as R
        mode = {mode!r}
        class Function:
            def __init__(self, result):
                self.result = result
            def __call__(self, *args):
                return self.result
        class Library:
            pass
        library = Library()
        for name in N._REQUIRED:
            result = N._SIZES.get(name, 0)
            if name == 'sodium_version_string':
                result = b'controlled-version'
            setattr(library, name, Function(result))
        if mode == 'sodium-init':
            library.sodium_init.result = -1
        elif mode == 'abi-size':
            library.crypto_core_ristretto255_bytes.result = 31
        elif mode == 'version':
            library.sodium_version_string.result = None
        elif mode == 'signature':
            library.crypto_scalarmult_ristretto255 = object()
        N._load_libsodium = lambda: library
        try:
            N.initialize()
        except N.NativeCryptoInitializationError:
            pass
        else:
            raise AssertionError('broken native runtime was accepted')
        assert N._backend is None
        assert not R.is_initialized()
    ''', tmp_path)


@pytest.mark.parametrize("error", ["NativeCryptoUnavailable", "NativeCryptoInitializationError"])
def test_sidecar_stops_before_sessions_and_listeners_when_startup_fails(
        error, monkeypatch):
    import asyncio
    from holdem import native_crypto, sidecar_launcher

    def failed():
        raise getattr(native_crypto, error)("controlled startup failure")

    def forbidden(*args, **kwargs):
        pytest.fail("sidecar created a session or listener after failed startup")

    monkeypatch.setattr(native_crypto, "initialize", failed)
    monkeypatch.setattr(sidecar_launcher, "_make_sessions", forbidden)
    monkeypatch.setattr(sidecar_launcher, "ClientServer", forbidden)
    with pytest.raises(getattr(native_crypto, error), match="controlled startup"):
        asyncio.run(sidecar_launcher.run(
            seats=2, sb=5, bb=10, stack=1000, structure="No-Limit",
            port=0, seed=0, nickname="test"))


@pytest.mark.parametrize("error", ["NativeCryptoUnavailable", "NativeCryptoInitializationError"])
def test_multiplayer_create_reports_startup_failure_before_keys_or_sockets(
        error, monkeypatch):
    from types import SimpleNamespace
    import secrets
    from holdem import native_crypto, onboarding

    errors = []
    owner = SimpleNamespace(root=object())

    def failed():
        raise getattr(native_crypto, error)("controlled startup failure")

    def forbidden(*args, **kwargs):
        pytest.fail("multiplayer created keys, a session, or a socket after failure")

    monkeypatch.setattr(native_crypto, "initialize", failed)
    monkeypatch.setattr(onboarding.messagebox, "showerror",
                        lambda *args, **kwargs: errors.append((args, kwargs)))
    monkeypatch.setattr(onboarding._identity, "load_or_create", forbidden)
    monkeypatch.setattr(onboarding._transport, "start_host", forbidden)
    monkeypatch.setattr(onboarding._session_mod, "Session", forbidden)
    monkeypatch.setattr(secrets, "token_bytes", forbidden)
    onboarding.OnboardingFlow._create_game_dialog(owner)
    assert errors == [(("Multiplayer unavailable", "controlled startup failure"),
                       {"parent": owner.root})]


def test_multiplayer_join_initializes_before_keys_sessions_or_worker_start():
    import ast
    import inspect
    from holdem import onboarding

    tree = ast.parse(textwrap.dedent(inspect.getsource(
        onboarding.OnboardingFlow._join_game_dialog)))
    connect = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "_connect")
    calls = [node for node in ast.walk(connect) if isinstance(node, ast.Call)]
    startup = next(node for node in calls
                   if ast.unparse(node.func) == "_native_crypto.initialize")
    side_effects = {"_identity.load_or_create", "_session_mod.Session",
                    "_transport.connect", "threading.Thread"}
    after = [node for node in calls if ast.unparse(node.func) in side_effects]
    assert after, "the Join check stopped observing its connection path"
    assert all(startup.lineno < node.lineno for node in after)
    guarded = next(node for node in ast.walk(connect)
                   if isinstance(node, ast.Try) and startup in list(ast.walk(node)))
    handler = guarded.handlers[0]
    assert "NativeCryptoUnavailable" in ast.unparse(handler.type)
    assert "NativeCryptoInitializationError" in ast.unparse(handler.type)
    assert isinstance(handler.body[-1], ast.Return)


def test_native_startup_preserves_baseline_group_and_card_encodings():
    import hashlib
    from crypto_gate import require_crypto
    require_crypto()
    from holdem.p2p import bg_hadamard, bg_shuffle, elgamal, ristretto as R

    one = R.Scalar(bytes([1]) + bytes(31))
    assert R.G.hex() == 'e2f2ae0a6abc4e71a884a961c500515f58e30b6aa582dd8db6a65945e08d2d76'
    assert R.G == R.mul_base(one)
    assert bg_hadamard._NEG_ONE.hex() == 'ecd3f55c1a631258d69cf7a2def9de1400000000000000000000000000000010'
    assert bg_hadamard._NEG_ONE == R.scalar_negate(one)
    assert bg_shuffle._GENERATOR.hex() == 'e25b915920c6e0a4f782375f856b03ad63ac3da1db0a7bdbc9ae7a4495b2f63a'
    assert bg_shuffle._GENERATOR == R.hash_to_group(
        hashlib.sha512(b'poker.bg.shuffle.mask-generator.v1').digest())
    points = elgamal.deck_points()
    assert hashlib.sha256(b''.join(points)).hexdigest() == '6fc0cd9741f43bc4ab4c3ab61065ee8fb74f8ee51dd23e082558fe38e0cd2458'
    assert points == [R.hash_to_group(elgamal.card_label_bytes(card))
                      for card in elgamal.CARDS]
