"""Persistent keys must survive malformed files, interrupted writes and races."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import pytest

from holdem.p2p import device_secret, identity
from holdem.p2p import private_storage as storage


@unittest.skipUnless(sys.platform == 'linux', 'native Linux namespace policy')
class TestPrivateNamespace(unittest.TestCase):
    """Generated fixtures only; no real identity or host configuration."""

    def setUp(self):
        import tempfile
        self.directory = tempfile.TemporaryDirectory(prefix='storage-namespace-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_supported_binary_write_and_stable_lock(self):
        target = self.root / 'private' / 'identity.json'
        data = bytes(range(256))
        with storage.locked(target):
            storage.atomic_write(target, data)
        lock = target.with_name(target.name + '.lock')
        inode = lock.stat().st_ino
        with storage.locked(target):
            self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(list(target.parent.glob('*.tmp')), [])

    def test_unverifiable_storage_refuses_before_mutation(self):
        target = self.root / 'missing' / 'identity.json'
        for kind in (None, True, 'tmpfs', -1, 2**64, 0x65735546,
                     0x6969, 0xff534d42, 0x794c7630):
            with self.subTest(kind=kind), patch.object(
                    storage, '_posix_storage_kind', return_value=kind), patch.object(
                    os, 'mkdir', wraps=os.mkdir) as mkdir, patch.object(
                    storage.tempfile, 'mkstemp', wraps=storage.tempfile.mkstemp) as temp:
                with self.assertRaises(storage.InsecureKeyStorage):
                    with storage.locked(target):
                        self.fail('unsafe lock was acquired')
                with self.assertRaises(storage.InsecureKeyStorage):
                    storage.atomic_write(target, b'generated')
                mkdir.assert_not_called()
                temp.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_capability_query_failure_preserves_prior_bytes(self):
        target = self.root / 'identity.json'
        target.write_bytes(b'prior')
        with patch.object(storage, '_posix_storage_kind', side_effect=OSError('query failed')):
            with self.assertRaises(storage.InsecureKeyStorage) as caught:
                storage.atomic_write(target, b'new')
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(target.read_bytes(), b'prior')
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_every_component_is_checked_and_descriptors_close(self):
        directory = self.root / 'private'
        directory.mkdir()
        seen, opened, closed = [], [], []
        real_query, real_open, real_close = storage._posix_storage_kind, os.open, os.close
        def query(fd):
            seen.append(os.fstat(fd).st_ino)
            if len(seen) == 3:
                return None
            return real_query(fd)
        def open_fd(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd
        def close_fd(fd):
            closed.append(fd)
            return real_close(fd)
        with patch.object(storage, '_posix_storage_kind', query), patch.object(
                os, 'open', open_fd), patch.object(os, 'close', close_fd):
            with self.assertRaises(storage.InsecureKeyStorage):
                storage.ensure_private_dir(directory)
        self.assertEqual(len(seen), 3)
        self.assertCountEqual(opened, closed)

    def test_unsafe_ancestor_refuses_before_lock_or_temp(self):
        ancestor = self.root / 'writable'
        parent = ancestor / 'private'
        parent.mkdir(parents=True, mode=0o700)
        ancestor.chmod(0o777)
        target = parent / 'identity.json'
        target.write_bytes(b'prior')
        for operation in ('lock', 'write'):
            with self.subTest(operation=operation), self.assertRaises(storage.InsecureKeyStorage):
                if operation == 'lock':
                    with storage.locked(target):
                        self.fail('unsafe lock was acquired')
                else:
                    storage.atomic_write(target, b'new')
        self.assertEqual(target.read_bytes(), b'prior')
        self.assertEqual([p.name for p in parent.iterdir()], ['identity.json'])

    def test_sticky_ancestor_works_but_sticky_final_parent_refuses(self):
        shared = self.root / 'shared'
        shared.mkdir(mode=0o1777)
        shared.chmod(0o1777)
        target = shared / 'owned' / 'identity.json'
        with storage.locked(target):
            storage.atomic_write(target, b'generated')
        self.assertEqual(target.read_bytes(), b'generated')
        with self.assertRaises(storage.InsecureKeyStorage):
            with storage.locked(shared / 'unsafe.json'):
                self.fail('shared final storage was accepted')
        self.assertFalse((shared / 'unsafe.json.lock').exists())

    def test_missing_components_are_private_existing_ones_unchanged(self):
        self.root.chmod(0o755)
        before = self.root.stat()
        old = os.umask(0)
        try:
            storage.ensure_private_dir(self.root / 'one' / 'two')
        finally:
            os.umask(old)
        for component in (self.root / 'one', self.root / 'one' / 'two'):
            self.assertEqual(component.stat().st_mode & 0o777, 0o700)
        after = self.root.stat()
        self.assertEqual((before.st_mode, before.st_uid), (after.st_mode, after.st_uid))

    def test_original_aliases_and_parent_traversal_refuse(self):
        real = self.root / 'real'
        real.mkdir()
        target = real / 'identity.json'
        target.write_bytes(b'prior')
        alias = self.root / 'alias'
        alias.symlink_to(real, target_is_directory=True)
        dangling = self.root / 'dangling'
        dangling.symlink_to(self.root / 'absent')
        leaf = self.root / 'leaf'
        leaf.symlink_to(target)
        for path in (alias / 'identity.json', alias / '..' / 'identity.json',
                     dangling, leaf):
            with self.subTest(path=path), self.assertRaises(storage.InsecureKeyStorage):
                storage.canonical_target(path)
        with self.assertRaises(storage.InsecureKeyStorage):
            storage.atomic_write(alias / 'identity.json', b'new')
        self.assertEqual(target.read_bytes(), b'prior')

    def test_unsafe_lock_objects_refuse_without_mutation(self):
        target = self.root / 'identity.json'
        lock = self.root / 'identity.json.lock'
        for kind in ('fifo', 'directory', 'symlink', 'loose', 'hardlink'):
            with self.subTest(kind=kind):
                if kind == 'fifo':
                    os.mkfifo(lock)
                elif kind == 'directory':
                    lock.mkdir()
                elif kind == 'symlink':
                    lock.symlink_to(self.root / 'absent')
                else:
                    lock.write_bytes(b'preserve')
                    lock.chmod(0o666 if kind == 'loose' else 0o600)
                    if kind == 'hardlink':
                        os.link(lock, self.root / 'second-link')
                before = lock.lstat()
                with self.assertRaises(storage.InsecureKeyStorage):
                    with storage.locked(target):
                        self.fail('unsafe lock was acquired')
                after = lock.lstat()
                self.assertEqual((before.st_ino, before.st_mode), (after.st_ino, after.st_mode))
                if kind == 'directory':
                    lock.rmdir()
                else:
                    lock.unlink()
                (self.root / 'second-link').unlink(missing_ok=True)


@pytest.mark.parametrize("stored", [b"{", b"{}", b"[]", b"null", b"\xff",
    b'{"private_key_b64":null}', b'{"private_key_b64":"garbage"}',
    b'{"x25519_private_key_b64":"AA=="}'])
def test_existing_invalid_identity_is_preserved(tmp_path, stored):
    path = tmp_path / "identity.json"
    path.write_bytes(stored)
    with pytest.raises(ValueError, match="identity"):
        identity.load_or_create(path)
    assert path.read_bytes() == stored


def test_unreadable_identity_is_not_treated_as_absent(tmp_path, monkeypatch):
    from pathlib import Path
    path = tmp_path / "identity.json"
    identity.load_or_create(path)
    stored = path.read_bytes()
    original = Path.read_text
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("injected unreadable identity")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(ValueError, match="identity"):
        identity.load_or_create(path)
    assert path.read_bytes() == stored


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
    if os.name != "nt":
        from holdem.p2p.private_storage import InsecureKeyStorage
        before = path.read_bytes()
        with pytest.raises(InsecureKeyStorage, match="symlink"):
            identity.load_or_create(alias)
        assert path.read_bytes() == before and alias.is_symlink()
        return
    first, second = identity.load_or_create(alias), identity.load_or_create(path)
    assert alias.is_symlink() and alias.samefile(path)
    assert first.public_key_bytes() == second.public_key_bytes()
    assert first.x25519_public_key_bytes() == second.x25519_public_key_bytes()


def test_short_device_secret_writes_are_completed(tmp_path, monkeypatch):
    path = tmp_path / "device_secret"
    write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: write(fd, data[:3]))
    secret = device_secret.load_or_create(path)
    assert len(secret) == 32 and path.read_bytes() == secret


def test_zero_progress_secret_write_fails_without_publication(tmp_path, monkeypatch):
    path = tmp_path / "device_secret"
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(OSError):
        device_secret.load_or_create(path)
    assert not path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_secret_sync_removes_temporary_key(tmp_path, monkeypatch):
    path = tmp_path / "device_secret"
    def failed(*args):
        raise OSError("injected sync failure")
    monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises(OSError, match="injected"):
        device_secret.load_or_create(path)
    assert not path.exists() and list(tmp_path.glob("*.tmp")) == []


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
    path.write_text(json.dumps({"private_key_b64": base64.b64encode(bytes(range(32))).decode(),
                                "nickname": "preserve me"}), encoding="utf-8")


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
    monkeypatch.setattr(os, "replace", failed)
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
