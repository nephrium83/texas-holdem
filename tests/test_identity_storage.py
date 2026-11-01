"""Persistent keys must survive malformed files, interrupted writes and races."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from holdem.p2p import device_secret, identity
from holdem.p2p import identity_storage as storage


def _store(path, data):
    with storage.creating(path) as publisher:
        publisher.publish(data)


@pytest.mark.parametrize("stored", [b"{", b"{}", b"[]", b"null", b"\xff",
    b'{"private_key_b64":null}', b'{"private_key_b64":"garbage"}',
    b'{"x25519_private_key_b64":"AA=="}'])
def test_existing_invalid_identity_is_preserved(tmp_path, stored):
    path = tmp_path / "identity.json"
    _store(path, stored)
    with pytest.raises(ValueError, match="identity"):
        identity.load_or_create(path)
    assert path.read_bytes() == stored


def test_unreadable_identity_is_not_treated_as_absent(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    identity.load_or_create(path)
    stored = path.read_bytes()
    from holdem.p2p import windows_security as win
    backend = win if os.name == 'nt' else storage
    name = 'read_all' if os.name == 'nt' else '_read_all'
    calls = []
    def denied(*args):
        calls.append(True)
        raise PermissionError("injected unreadable identity")
    monkeypatch.setattr(backend, name, denied)
    with pytest.raises(ValueError, match="identity"):
        identity.load_or_create(path)
    assert path.read_bytes() == stored
    assert calls == [True]


def test_complete_identity_load_needs_no_writable_lock(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    first = identity.load_or_create(path)
    original = os.open
    def denied(target, *args, **kwargs):
        if str(target).endswith(".lock"):
            raise PermissionError("read-only storage")
        return original(target, *args, **kwargs)
    monkeypatch.setattr(os, "open", denied)
    second = identity.load_or_create(path)
    assert second.public_key_bytes() == first.public_key_bytes()
    assert second.x25519_public_key_bytes() == first.x25519_public_key_bytes()


@pytest.mark.skipif(os.name != "nt", reason="Windows pathname normalization")
@pytest.mark.parametrize("suffix", [".", " ", ":stream"])
def test_ambiguous_windows_storage_names_fail_before_publication(tmp_path, suffix):
    for loader, name in ((identity.load_or_create, "identity.json"),
                         (device_secret.load_or_create, "device_secret")):
        path = tmp_path / (name + suffix)
        with pytest.raises(ValueError, match="ambiguous"):
            loader(path)
        assert not (tmp_path / name).exists()


def test_legacy_hard_links_require_explicit_copy_before_upgrade(tmp_path):
    path, alias = tmp_path / "identity.json", tmp_path / "alias.json"
    _legacy(path)
    before = path.read_bytes()
    os.link(path, alias)
    for target in (path, alias):
        with pytest.raises(ValueError, match="hard links"):
            identity.load_or_create(target)
    assert path.read_bytes() == alias.read_bytes() == before and path.samefile(alias)


def test_symlink_legacy_upgrade_preserves_shared_identity(tmp_path):
    path, alias = tmp_path / "identity.json", tmp_path / "alias.json"
    _legacy(path)
    try:
        alias.symlink_to(path)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account lacks symlink creation privilege")
        raise
    before = path.read_bytes()
    with pytest.raises(storage.InsecureKeyStorage):
        identity.load_or_create(alias)
    assert alias.is_symlink() and alias.samefile(path)
    assert path.read_bytes() == before
    identity.load_or_create(path)


# The device secret now publishes through identity_storage (finding 210).
# These hooks target os.write/os.fsync/os.link, which only the POSIX
# publish path uses; Windows publishes via the Win32 API, where they would
# never fire. Windows publication is covered by the identity tests and
# the multi-process convergence test below, which run on both.
_POSIX_PUBLISH = pytest.mark.skipif(
    os.name == "nt", reason="hooks the POSIX publish path (os.write/fsync/link)")


@_POSIX_PUBLISH
def test_short_device_secret_writes_are_completed(tmp_path, monkeypatch):
    path = tmp_path / "device_secret"
    write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: write(fd, data[:3]))
    secret = device_secret.load_or_create(path)
    assert len(secret) == 32 and path.read_bytes() == secret


@_POSIX_PUBLISH
def test_zero_progress_secret_write_fails_without_publication(tmp_path, monkeypatch):
    path = tmp_path / "device_secret"
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(OSError):
        device_secret.load_or_create(path)
    assert not path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


@_POSIX_PUBLISH
def test_failed_secret_sync_removes_temporary_key(tmp_path, monkeypatch):
    path = tmp_path / "device_secret"
    def failed(*args):
        raise OSError("injected sync failure")
    monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises(OSError, match="injected"):
        device_secret.load_or_create(path)
    assert not path.exists() and list(tmp_path.glob("*.tmp")) == []


@_POSIX_PUBLISH
def test_no_hard_links_never_exposes_partial_secret(tmp_path, monkeypatch):
    path = tmp_path / "device_secret"
    seen = []
    write = os.write
    def unavailable(*args):
        raise OSError("hard links unavailable")
    def observing(fd, data):
        seen.append(path.stat().st_size if path.exists() else None)
        return write(fd, data)
    monkeypatch.setattr(os, "link", unavailable)
    monkeypatch.setattr(os, "write", observing)
    result = device_secret.load_or_create(path)
    assert seen and all(size in (None, 32) for size in seen)
    assert path.read_bytes() == result


def _legacy(path):
    _store(path, json.dumps({"private_key_b64": base64.b64encode(bytes(range(32))).decode(),
                            "nickname": "preserve me"}).encode())


@pytest.mark.parametrize("legacy", [False, True])
def test_simultaneous_explicit_path_loads_converge_on_both_keys(tmp_path, legacy):
    import threading
    path = tmp_path / "identity.json"
    if legacy:
        _legacy(path)
    barrier = threading.Barrier(4)
    def load(_):
        barrier.wait(timeout=5)
        loaded = identity.load_or_create(path)
        return loaded.public_key_bytes(), loaded.x25519_public_key_bytes()
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(load, range(4)))
    assert len(set(values)) == 1
    persisted = identity.load_or_create(path)
    assert values[0] == (persisted.public_key_bytes(), persisted.x25519_public_key_bytes())
    if legacy:
        assert json.loads(path.read_text())["nickname"] == "preserve me"


def test_interrupted_legacy_upgrade_preserves_old_identity(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    _legacy(path)
    stored = path.read_bytes()
    def failed(*args):
        raise OSError("injected durability failure")
    monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises(OSError, match="injected"):
        identity.load_or_create(path)
    assert path.read_bytes() == stored
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_identity_publication_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    _legacy(path)
    previous = path.read_bytes()
    def failed(*args):
        raise OSError("injected publication failure")
    monkeypatch.setattr(storage, "_commit_rename", failed)
    with pytest.raises(OSError, match="injected"):
        identity.load_or_create(path)
    assert path.read_bytes() == previous and list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("kind", ["identity", "legacy", "secret-no-links"])
def test_independent_processes_adopt_one_persisted_identity(tmp_path, kind):
    target = tmp_path / ("device_secret" if kind == "secret-no-links" else "identity.json")
    if kind == "legacy":
        _legacy(target)
    gate = tmp_path / "go"
    script = '''
import hashlib, os, pathlib, sys, time
from holdem.p2p import device_secret, identity
kind, target, gate, ready = sys.argv[1:]
pathlib.Path(ready).touch()
deadline = time.monotonic() + 10
while not pathlib.Path(gate).exists():
    if time.monotonic() >= deadline: raise TimeoutError("start gate")
    time.sleep(.01)
if kind == "secret-no-links":
    def unavailable(*args): raise OSError("no hard links")
    os.link = unavailable
    print(hashlib.sha256(device_secret.load_or_create(pathlib.Path(target))).hexdigest())
else:
    value = identity.load_or_create(target)
    print(value.public_key_bytes().hex(), value.x25519_public_key_bytes().hex())
'''
    processes = []
    try:
        for i in range(3):
            processes.append(subprocess.Popen(
                [sys.executable, "-B", "-c", script, kind, str(target), str(gate), str(tmp_path / f"ready-{i}")],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8"))
        deadline = time.monotonic() + 10
        while len(list(tmp_path.glob("ready-*"))) != 3:
            assert all(p.poll() is None for p in processes), "a child failed before readiness"
            if time.monotonic() >= deadline:
                raise AssertionError("children did not reach start gate")
            time.sleep(.01)
        gate.touch()
        outputs = []
        for process in processes:
            out, err = process.communicate(timeout=20)
            assert process.returncode == 0, err
            outputs.append(out.strip())
        assert outputs[0] and len(set(outputs)) == 1
        if kind == "secret-no-links":
            import hashlib
            assert outputs[0] == hashlib.sha256(target.read_bytes()).hexdigest()
        else:
            persisted = identity.load_or_create(target)
            assert outputs[0] == persisted.public_key_bytes().hex() + " " + persisted.x25519_public_key_bytes().hex()
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)


@pytest.mark.parametrize('suffix', ['../identity.json', 'alias/../identity.json'])
def test_parent_traversal_refuses_before_side_effects(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr(os, 'mkdir', lambda *a, **k: pytest.fail('mkdir before refusal'))
    with pytest.raises(storage.InsecureKeyStorage):
        identity.load_or_create(str(tmp_path) + '/' + suffix)
    assert list(tmp_path.iterdir()) == []


def test_storage_import_is_inert(tmp_path):
    code = '''
import ctypes, os, sys, _thread
def fail(*args, **kwargs): raise AssertionError('native operation during import')
ctypes.CDLL = fail
if hasattr(ctypes, 'WinDLL'): ctypes.WinDLL = fail
os.urandom = fail
_thread.start_new_thread = fail
from holdem.p2p import identity_storage, windows_security
assert not windows_security._API
'''
    result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True,
                            text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_no_canonicalization_on_real_create_and_reload(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('canonicalization erases the supplied chain')
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'resolve', forbidden)
        patch.setattr(os.path, 'realpath', forbidden)
        patch.setattr(os.path, 'abspath', forbidden)
        first = identity.load_or_create(tmp_path / 'private' / 'identity.json')
        second = identity.load_or_create(tmp_path / 'private' / 'identity.json')
    assert first.public_key_bytes() == second.public_key_bytes()


def test_read_failure_never_generates_keys(tmp_path, monkeypatch):
    path = tmp_path / 'identity.json'
    identity.load_or_create(path)
    before = path.read_bytes()
    from holdem.p2p import windows_security as win
    def failed(*args):
        raise OSError('read fault')
    monkeypatch.setattr(win if os.name == 'nt' else storage,
                        'read_all' if os.name == 'nt' else '_read_all', failed)
    monkeypatch.setattr(identity.Ed25519PrivateKey, 'generate',
                        lambda: pytest.fail('generation after read error'))
    with pytest.raises(ValueError, match='cannot read identity'):
        identity.load_or_create(path)
    assert path.read_bytes() == before


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='native Linux policy')
@pytest.mark.parametrize('mode', [0o644, 0o640, 0o660, 0o7000])
def test_posix_insecure_existing_modes_refuse_without_mutation(tmp_path, mode):
    path = tmp_path / 'identity.json'
    identity.load_or_create(path)
    path.chmod(mode)
    before = path.stat()
    with pytest.raises(storage.InsecureKeyStorage):
        identity.load_or_create(path)
    assert path.stat().st_mode == before.st_mode


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='native Linux policy')
@pytest.mark.parametrize('kind', [None, True, 'ext4', -1, 0x2fc12fc1, 0x794c7630, 0x6969, 0x65735546])
def test_posix_unverified_storage_refuses_before_mutation(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(storage, '_filesystem_type', lambda fd: kind)
    monkeypatch.setattr(os, 'mkdir', lambda *a, **k: pytest.fail('mkdir on unverified storage'))
    monkeypatch.setattr(os, 'read', lambda *a: pytest.fail('read on unverified storage'))
    with pytest.raises(storage.InsecureKeyStorage):
        identity.load_or_create(tmp_path / 'private' / 'identity.json')
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='native Linux policy')
@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'directory'])
def test_posix_nonregular_leaf_refused_without_opening(tmp_path, monkeypatch, kind):
    path = tmp_path / 'identity.json'
    if kind == 'symlink':
        path.symlink_to(tmp_path / 'absent')
    elif kind == 'fifo':
        os.mkfifo(path)
    else:
        path.mkdir()
    original = os.open
    def observe(name, *args, **kwargs):
        assert name != 'identity.json', 'nonregular leaf was opened'
        return original(name, *args, **kwargs)
    monkeypatch.setattr(os, 'open', observe)
    with pytest.raises(storage.InsecureKeyStorage):
        identity.load_or_create(path)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='native Linux policy')
def test_posix_alias_and_writable_parent_refused(tmp_path):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    path = private / 'identity.json'
    identity.load_or_create(path)
    before = path.read_bytes()
    alias = tmp_path / 'alias'
    alias.symlink_to(private, target_is_directory=True)
    with pytest.raises(storage.InsecureKeyStorage):
        identity.load_or_create(alias / path.name)
    private.chmod(0o777)
    with pytest.raises(storage.InsecureKeyStorage):
        identity.load_or_create(path)
    assert path.read_bytes() == before


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='native Linux policy')
def test_posix_publication_stays_in_pinned_directory(tmp_path):
    parent = tmp_path / 'private'
    target = parent / 'identity.json'
    with storage.creating(target) as publisher:
        moved = tmp_path / 'moved'
        parent.rename(moved)
        parent.mkdir(mode=0o700)
        publisher.publish(b'generated-private-fixture')
    assert (moved / target.name).read_bytes() == b'generated-private-fixture'
    assert list(parent.iterdir()) == []
    assert list(moved.glob('*.tmp')) == []


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='native Linux policy')
def test_posix_real_directory_fsync_and_opath_control(tmp_path, monkeypatch):
    import errno
    import stat
    original = os.fsync
    synced = []
    def sync(fd):
        original(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(fd)
    monkeypatch.setattr(os, 'fsync', sync)
    identity.load_or_create(tmp_path / 'identity.json')
    assert len(synced) == 1
    fd = os.open(tmp_path, os.O_PATH | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError) as caught:
            original(fd)
        assert caught.value.errno == errno.EBADF
    finally:
        os.close(fd)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='native Linux policy')
def test_posix_postcommit_fsync_failure_keeps_same_identity(tmp_path, monkeypatch):
    import errno
    import stat
    original = os.fsync
    reached = []
    def sync(fd):
        original(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            reached.append(True)
            raise OSError(errno.EIO, 'postcommit injected after real directory sync')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', sync)
        first = identity.load_or_create(tmp_path / 'identity.json')
    assert reached == [True]
    second = identity.load_or_create(tmp_path / 'identity.json')
    assert first.public_key_bytes() == second.public_key_bytes()


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='native Linux resource ownership')
@pytest.mark.parametrize('release_first', [False, True])
@pytest.mark.parametrize('locked', [False, True])
def test_posix_successful_read_cleanup_failure_never_replaces(tmp_path, monkeypatch, release_first, locked):
    import stat
    path = tmp_path / 'identity.json'
    first = identity.load_or_create(path)
    stored = path.read_bytes()
    original, retained, calls = os.close, [], []
    def close(fd):
        if not calls and stat.S_ISREG(os.fstat(fd).st_mode):
            calls.append(fd)
            if release_first:
                original(fd)
            else:
                retained.append(fd)
            raise OSError('injected release failure')
        return original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(os, 'close', close)
        patch.setattr(identity.Ed25519PrivateKey, 'generate', lambda: pytest.fail('replacement after cleanup'))
        with pytest.raises((OSError, ValueError)):
            if locked:
                with storage.creating(path) as publisher:
                    assert publisher.read_existing() == stored
            else:
                identity.load_or_create(path)
    for fd in retained:
        original(fd)  # exact harness-owned retained descriptor only
    assert len(calls) == 1 and path.read_bytes() == stored
    assert identity.load_or_create(path).public_key_bytes() == first.public_key_bytes()


@pytest.mark.skipif(os.name != 'nt', reason='native Windows handles')
def test_windows_root_exception_and_child_call_names(tmp_path, monkeypatch):
    from holdem.p2p import windows_security as win
    original, calls = win._open, []
    def observe(parent, name, **kwargs):
        calls.append((parent is None, name))
        return original(parent, name, **kwargs)
    monkeypatch.setattr(win, '_open', observe)
    identity.load_or_create(tmp_path / 'identity.json')
    roots = [(root, name) for root, name in calls if root]
    # Unlocked missing read and locked creation each do one rooted walk.
    assert len(roots) == 2
    assert all(name == '\\??\\' + tmp_path.drive + '\\' for _, name in roots)
    assert all(name not in ('.', '..') and not any(c in name for c in '\\/:\0')
               for root, name in calls if not root)


@pytest.mark.skipif(os.name != 'nt', reason='native Windows ACL')
def test_windows_created_owner_is_token_user_not_default_owner(tmp_path):
    from holdem.p2p import windows_security as win
    path = tmp_path / 'private' / 'identity.json'
    identity.load_or_create(path)
    with storage._namespace(path, create=False) as (directory, resources):
        handle, _ = win.open_child(directory, path.name, access=0x120089, share=1,
                                   disposition=1, options=win._SYNC_REPARSE | 0x40)
        resources.own(handle, lambda h: h.close(), 'test close')
        owner, control, aces = win.inspect(handle)
        user = win.sid_text(win.token_user_sid())
        assert win.classify_leaf(owner, control, aces, user) == 'conforming'


@pytest.mark.skipif(os.name != 'nt', reason='native Windows ACL')
def test_windows_inherited_identity_refused_without_read_or_repair(tmp_path, monkeypatch):
    from holdem.p2p import windows_security as win
    path = tmp_path / 'identity.json'
    path.write_bytes(b'generated-inherited-fixture')
    before = path.read_bytes()
    monkeypatch.setattr(win, 'read_all', lambda *a: pytest.fail('read before ACL refusal'))
    with pytest.raises(storage.InsecureKeyStorage):
        identity.load_or_create(path)
    assert path.read_bytes() == before


@pytest.mark.skipif(os.name != 'nt', reason='native Windows junction')
def test_windows_junction_refused(tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    alias = tmp_path / 'alias'
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(alias), str(private)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    try:
        with pytest.raises(storage.InsecureKeyStorage):
            identity.load_or_create(alias / 'identity.json')
        assert list(private.iterdir()) == []
    finally:
        alias.rmdir()


@pytest.mark.skipif(os.name != 'nt', reason='native Windows resource ownership')
@pytest.mark.parametrize('release_first', [False, True])
@pytest.mark.parametrize('locked', [False, True])
def test_windows_successful_read_close_failure_never_replaces(tmp_path, monkeypatch, release_first, locked):
    from holdem.p2p import windows_security as win
    path = tmp_path / 'identity.json'
    first = identity.load_or_create(path)
    stored = path.read_bytes()
    original, retained, calls, leaf = win.Handle.close, [], [], []
    read = win.read_all
    def observe(handle):
        result = read(handle)
        leaf.append(handle.value)
        return result
    def close(handle):
        if handle.value is not None and not calls and handle.value in leaf:
            calls.append(True)
            if release_first:
                original(handle)
            else:
                # Remove ownership from the failed wrapper; the test harness
                # alone retains the exact raw handle for safe later cleanup.
                retained.append(win.Handle(handle.value))
                handle.value = None
            raise OSError('injected release failure')
        return original(handle)
    with monkeypatch.context() as patch:
        patch.setattr(win.Handle, 'close', close)
        patch.setattr(win, 'read_all', observe)
        patch.setattr(identity.Ed25519PrivateKey, 'generate', lambda: pytest.fail('replacement after cleanup'))
        with pytest.raises((ValueError, OSError)):
            if locked:
                with storage.creating(path) as publisher:
                    publisher.read_existing()
            else:
                identity.load_or_create(path)
    for handle in retained:
        original(handle)
    assert calls == [True] and path.read_bytes() == stored
    assert identity.load_or_create(path).public_key_bytes() == first.public_key_bytes()


def test_missing_file_error_from_opened_read_is_not_absence(tmp_path, monkeypatch):
    from holdem.p2p import windows_security as win
    path = tmp_path / 'identity.json'
    identity.load_or_create(path)
    stored = path.read_bytes()
    def failed(*args):
        raise FileNotFoundError('injected after the object was opened')
    monkeypatch.setattr(win if os.name == 'nt' else storage,
                        'read_all' if os.name == 'nt' else '_read_all', failed)
    monkeypatch.setattr(identity.Ed25519PrivateKey, 'generate', lambda: pytest.fail('replacement after I/O error'))
    with pytest.raises(ValueError, match='cannot read identity'):
        identity.load_or_create(path)
    assert path.read_bytes() == stored


def _set_generated_windows_dacl(handle, sddl):
    """Test-only mutation of generated fixture descriptors, never user keys."""
    import ctypes as c
    from ctypes import wintypes as w
    from holdem.p2p import windows_security as win
    api = win._api()
    adv = c.WinDLL('advapi32', use_last_error=True)
    get_dacl = adv.GetSecurityDescriptorDacl
    get_dacl.argtypes = [c.c_void_p, c.POINTER(w.BOOL), c.POINTER(c.c_void_p), c.POINTER(w.BOOL)]
    get_dacl.restype = w.BOOL
    set_info = adv.SetSecurityInfo
    set_info.argtypes = [w.HANDLE, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p]
    set_info.restype = w.DWORD
    sd, acl, present, defaulted = c.c_void_p(), c.c_void_p(), w.BOOL(), w.BOOL()
    win._checked_bool(api['ConvertStringSecurityDescriptorToSecurityDescriptorW'](sddl, 1, c.byref(sd), None))
    try:
        win._checked_bool(get_dacl(sd, c.byref(present), c.byref(acl), c.byref(defaulted)))
        assert present.value and acl.value
        code = set_info(handle.value, 1, 0x80000004, None, None, acl, None)
        assert code == 0, f'generated fixture SetSecurityInfo error {code}'
    finally:
        win._free_sd(sd)


@pytest.mark.skipif(os.name != 'nt' or os.environ.get('HOLDEM_STORAGE_NATIVE_ACTOR') != '1',
                    reason='opt-in disposable Windows VM: creates/removes a temporary local actor account')
def test_windows_retained_foreign_write_dac_is_refused_before_read(tmp_path, monkeypatch):
    """An actual second user retains WRITE_DAC after its grant disappears."""
    import ctypes as c
    from ctypes import wintypes as w
    import secrets
    from holdem.p2p import windows_security as win
    class User(c.Structure):
        _fields_ = [('name', w.LPWSTR), ('password', w.LPWSTR), ('password_age', w.DWORD),
                    ('privilege', w.DWORD), ('home', w.LPWSTR), ('comment', w.LPWSTR),
                    ('flags', w.DWORD), ('script', w.LPWSTR)]
    net = c.WinDLL('netapi32')
    net.NetUserAdd.argtypes = [w.LPCWSTR, w.DWORD, c.c_void_p, c.POINTER(w.DWORD)]
    net.NetUserAdd.restype = w.DWORD
    net.NetUserDel.argtypes, net.NetUserDel.restype = [w.LPCWSTR, w.LPCWSTR], w.DWORD
    adv = c.WinDLL('advapi32', use_last_error=True)
    adv.LogonUserW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(w.HANDLE)]
    adv.LogonUserW.restype = w.BOOL
    adv.ImpersonateLoggedOnUser.argtypes, adv.ImpersonateLoggedOnUser.restype = [w.HANDLE], w.BOOL
    adv.RevertToSelf.argtypes, adv.RevertToSelf.restype = [], w.BOOL
    name, password = 'HS' + secrets.token_hex(6), secrets.token_urlsafe(24) + 'Aa1!'
    user = User(name, password, 0, 1, None, 'Disposable storage regression actor', 0x10001, None)
    parameter = w.DWORD()
    code = net.NetUserAdd(None, 1, c.byref(user), c.byref(parameter))
    assert code == 0, f'disposable-VM actor creation failed: {code}, parameter {parameter.value}'
    token, retained, impersonating = w.HANDLE(), None, False
    path = tmp_path / 'private' / 'identity.json'
    try:
        win._checked_bool(adv.LogonUserW(name, '.', password, 2, 0, c.byref(token)))
        identity.load_or_create(path)
        before = path.read_bytes()
        owner = win.sid_text(win.token_user_sid())
        api, needed = win._api(), w.DWORD()
        assert not api['GetTokenInformation'](token, 1, None, 0, c.byref(needed))
        assert c.get_last_error() == 122
        actor_info = c.create_string_buffer(needed.value)
        win._checked_bool(api['GetTokenInformation'](token, 1, actor_info, needed, c.byref(needed)))
        actor_sid = c.cast(actor_info, c.POINTER(c.c_void_p))[0]
        assert win.sid_text(c.string_at(actor_sid, api['GetLengthSid'](actor_sid))) != owner
        with storage._namespace(path, create=False) as (directory, resources):
            fixture, _ = win.open_child(directory, path.name, access=0x60080, share=7,
                disposition=1, options=0x200040)
            resources.own(fixture, lambda h: h.close(), 'fixture descriptor close')
            _set_generated_windows_dacl(fixture, f'D:P(A;;FA;;;{owner})(A;;0x00060080;;;WD)')
            win._checked_bool(adv.ImpersonateLoggedOnUser(token))
            impersonating = True
            try:
                retained, _ = win.open_child(directory, path.name, access=0x60080, share=7,
                                             disposition=1, options=0x200040)
            finally:
                win._checked_bool(adv.RevertToSelf())
                impersonating = False
            _set_generated_windows_dacl(fixture, f'D:P(A;;FA;;;{owner})(A;;FA;;;SY)')
            descriptor = win.inspect(fixture)
            monkeypatch.setattr(win, 'read_all', lambda *a: pytest.fail('read during strict refusal'))
            with pytest.raises(storage.InsecureKeyStorage):
                identity.load_or_create(path)
            assert win.inspect(fixture) == descriptor and path.read_bytes() == before
            win._checked_bool(adv.ImpersonateLoggedOnUser(token))
            impersonating = True
            try:
                _set_generated_windows_dacl(retained, f'D:P(A;;FA;;;{owner})(A;;FR;;;WD)')
            finally:
                win._checked_bool(adv.RevertToSelf())
                impersonating = False
            assert win.inspect(fixture) != descriptor, 'retained foreign WRITE_DAC must remain effective'
    finally:
        if impersonating:
            win._checked_bool(adv.RevertToSelf())
        if retained is not None:
            retained.close()
        if token.value:
            win.Handle(token.value).close()
        code = net.NetUserDel(None, name)
        assert code == 0, f'disposable actor cleanup failed: {code}'


@pytest.mark.skipif(os.name != 'nt', reason='native Windows DOS device namespace')
def test_windows_substituted_drive_root_refused(tmp_path):
    import ctypes as c
    from holdem.p2p import windows_security as win
    api, drive = win._api(), None
    for letter in 'ZYXWVUTSRQP':
        target = c.create_unicode_buffer(32768)
        if not api['QueryDosDeviceW'](letter + ':', target, len(target)):
            code = c.get_last_error()
            if code in (2, 3):
                drive = letter + ':'
                break
    assert drive is not None, 'no unused disposable drive letter'
    private, junction = tmp_path / 'private', tmp_path / 'junction'
    private.mkdir()
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(private)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    mapped = False
    try:
        result = subprocess.run(['subst.exe', drive, str(junction)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        mapped = True
        with pytest.raises(storage.InsecureKeyStorage, match='substituted'):
            identity.load_or_create(drive + '\\identity.json')
        assert list(private.iterdir()) == []
    finally:
        if mapped:
            result = subprocess.run(['subst.exe', drive, '/d'], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
        junction.rmdir()


@pytest.mark.skipif(os.name != 'nt', reason='native NTFS short-name aliases')
def test_windows_short_leaf_alias_refused_before_second_lock(tmp_path, monkeypatch):
    import ctypes as c
    from ctypes import wintypes as w
    from holdem.p2p import windows_security as win
    path = tmp_path / 'Long Identity Storage Fixture.json'
    _legacy(path)
    kernel = c.WinDLL('kernel32', use_last_error=True)
    short_path = kernel.GetShortPathNameW
    short_path.argtypes, short_path.restype = [w.LPCWSTR, w.LPWSTR, w.DWORD], w.DWORD
    buffer = c.create_unicode_buffer(32768)
    count = short_path(str(path), buffer, len(buffer))
    assert 0 < count < len(buffer), c.get_last_error()
    alias = path.with_name(buffer.value.rsplit('\\', 1)[-1])
    if alias.name.casefold() == path.name.casefold():
        pytest.skip('test volume has no generated 8.3 alias; requires separate enabled native target')
    assert alias.samefile(path)
    stored = path.read_bytes()
    monkeypatch.setattr(win, 'lock', lambda *a: pytest.fail('alternate lock was acquired'))
    monkeypatch.setattr(win, 'read_all', lambda *a: pytest.fail('alias was read before refusal'))
    with pytest.raises(storage.InsecureKeyStorage, match='short-name'):
        identity.load_or_create(alias)
    assert path.read_bytes() == stored and not alias.with_name(alias.name + '.lock').exists()
