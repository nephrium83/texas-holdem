"""Strict, object-bound storage for host identities.

Native libraries and storage are touched only when a host explicitly loads an
identity. Device secrets retain their separate compatibility contract.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import logging
import os
from pathlib import Path
import platform
import secrets
import stat
import sys
import time


class InsecureKeyStorage(ValueError):
    """The supplied storage cannot establish the identity security boundary."""


class StorageCleanupError(OSError):
    """A nonpublishing operation could not release all owned resources."""


def _refuse(path, reason):
    return InsecureKeyStorage(
        f"insecure identity storage at {path}: {reason}; use a physical private "
        "directory on supported local storage owned by this account")


def _strict_path(supplied):
    value = os.fspath(supplied)
    if not isinstance(value, str) or not value or '\0' in value:
        raise _refuse(supplied, 'empty or invalid path')
    if os.name == 'nt':
        import ntpath
        raw = value.replace('/', '\\')
        if '..' in raw.split('\\'):
            raise _refuse(value, 'parent traversal is not supported')
        drive, tail = ntpath.splitdrive(raw)
        if not drive and not tail.startswith('\\'):
            raw = os.getcwd() + '\\' + raw
            drive, tail = ntpath.splitdrive(raw)
        if (len(drive) != 2 or not drive[0].isascii() or not drive[0].isalpha()
                or drive[1] != ':' or not tail.startswith('\\')):
            raise _refuse(value, 'only drive-letter absolute paths are supported')
        parts = [p for p in tail.split('\\') if p and p != '.']
        if any(p.endswith(('.', ' ')) or ':' in p for p in parts):
            raise ValueError(f'ambiguous Windows key storage path: {value}')
        if not parts:
            raise _refuse(value, 'identity filename is required')
        return Path(drive.upper() + '\\', *parts)
    if not sys.platform.startswith('linux'):
        raise _refuse(value, 'POSIX permission semantics are supported only on Linux')
    if '..' in value.split('/') or value.startswith('//'):
        raise _refuse(value, 'parent traversal or double-root path is not supported')
    if not value.startswith('/'):
        value = os.getcwd() + '/' + value
    path = Path(value)
    if len(path.parts) < 2:
        raise _refuse(value, 'identity filename is required')
    return path


def _diagnose(path, operation, error, *, committed=False):
    # Diagnostics must never change the committed result, even with a broken
    # application log handler or warnings configured as errors.
    try:
        logging.getLogger(__name__).warning(
            'Identity storage cleanup: path=%s operation=%s committed=%s error=%s',
            path, operation, committed, type(error).__name__)
    except BaseException:
        pass


class _Resources:
    def __init__(self, path):
        self.path = path
        self.items = []
        self.committed = False

    def own(self, resource, release, label):
        self.items.append((resource, release, label))
        return resource

    def release(self, resource):
        for index in range(len(self.items) - 1, -1, -1):
            item, close, _ = self.items[index]
            if item is resource or item == resource:
                self.items.pop(index)  # one attempt, including close failures
                close(item)
                return
        raise RuntimeError('resource has no owner')

    def finish(self, primary=None):
        first = None
        while self.items:
            item, close, label = self.items.pop()
            try:
                close(item)
            except BaseException as exc:
                _diagnose(self.path, label, exc, committed=self.committed)
                if first is None:
                    first = exc
        if first is not None and primary is None and not self.committed:
            raise StorageCleanupError(f'cannot release identity storage resources at {self.path}; '
                          'no identity was returned; release retained resources before retrying') from first


def _filesystem_type(fd):
    import ctypes
    if (ctypes.sizeof(ctypes.c_void_p) != 8
            or platform.machine().lower() not in ('x86_64', 'aarch64')):
        raise OSError(errno.ENOTSUP, 'unsupported Linux statfs ABI')
    libc = ctypes.CDLL(None, use_errno=True)
    query = libc.fstatfs
    query.argtypes = (ctypes.c_int, ctypes.c_void_p)
    query.restype = ctypes.c_int
    buffer = (ctypes.c_long * 64)()
    if query(fd, ctypes.byref(buffer)) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return buffer[0] & 0xffffffff


def _verify_directory(fd, path, *, final):
    info = os.fstat(fd)
    try:
        kind = _filesystem_type(fd)
    except (OSError, AttributeError) as exc:
        raise _refuse(path, 'filesystem permission semantics cannot be verified') from exc
    if type(kind) is not int or kind not in (0xef53, 0x58465342, 0x9123683e,
                                           0xf2f52010, 0x01021994):
        raise _refuse(path, 'filesystem permission semantics are unsupported')
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
        raise _refuse(path, 'directory type or owner is untrusted')
    if info.st_mode & 0o022 and (final or not info.st_mode & stat.S_ISVTX):
        raise _refuse(path, 'other accounts can replace directory entries')
    return info


def _verify_leaf(fd, directory, path, *, before=None, single=False):
    info, parent = os.fstat(fd), os.fstat(directory)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o7077 or info.st_dev != parent.st_dev
            or (single and info.st_nlink != 1)
            or (before is not None and (info.st_dev, info.st_ino) !=
                (before.st_dev, before.st_ino))):
        raise _refuse(path, 'file type, owner, modes, links or object binding is unsafe')
    return info


def _prestat(directory, name, path):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise _refuse(path, 'identity or lock is not a regular non-aliased file')
    return info


def _read_all(fd):
    chunks = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b''.join(chunks)
        chunks.append(chunk)


def _walk_posix(path, resources, *, create):
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = Path('/')
    fd = resources.own(os.open('/', flags), os.close, 'directory close')
    _verify_directory(fd, current, final=len(path.parent.parts) == 1)
    for index, part in enumerate(path.parent.parts[1:], 1):
        current /= part
        created = False
        try:
            child = os.open(part, flags, dir_fd=fd)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            try:
                child = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                raise _refuse(current, 'created or competing directory cannot be pinned safely') from exc
        except OSError as exc:
            raise _refuse(current, 'directory cannot be pinned without following aliases') from exc
        resources.own(child, os.close, 'directory close')
        info = _verify_directory(child, current, final=index == len(path.parent.parts) - 1)
        if created and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise _refuse(current, 'created directory is not private')
        resources.release(fd)
        fd = child
    return fd


def _read_posix(path, directory, resources):
    before = _prestat(directory, path.name, path)
    if before is None:
        raise FileNotFoundError(path)
    if (before.st_uid != os.geteuid() or before.st_mode & 0o7077
            or before.st_dev != os.fstat(directory).st_dev):
        raise _refuse(path, 'existing identity owner, modes or device is unsafe')
    fd = resources.own(os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW |
                             os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory),
                       os.close, 'leaf close')
    info = _verify_leaf(fd, directory, path, before=before)
    try:
        data = _read_all(fd)
        resources.release(fd)
    except FileNotFoundError as exc:
        # Only the name lookup above is an absence signal. Never turn an I/O
        # or release failure into permission to generate replacement keys.
        raise OSError('opened identity read or release failed') from exc
    return data, info.st_nlink


def _commit_rename(temp, directory, leaf):
    if os.name == 'nt':
        from holdem.p2p import windows_security as win
        win.rename_to(temp, directory, leaf)
    else:
        os.replace(temp, leaf, src_dir_fd=directory, dst_dir_fd=directory)


@contextmanager
def _namespace(path, *, create):
    resources = _Resources(path)
    primary = None
    try:
        if os.name == 'nt':
            from holdem.p2p import windows_security as win
            directory = win.walk(path, resources, create=create)
        else:
            directory = _walk_posix(path, resources, create=create)
        yield directory, resources
    except BaseException as exc:
        primary = exc
        raise
    finally:
        resources.finish(primary)


def _read(path, directory, resources):
    if os.name == 'nt':
        from holdem.p2p import windows_security as win
        return win.read_existing(directory, path.name, resources)
    return _read_posix(path, directory, resources)


def load_identity_bytes(path):
    path = _strict_path(path)
    with _namespace(path, create=False) as (directory, resources):
        data, _ = _read(path, directory, resources)
        return data


def _lock_posix(path, directory, resources):
    import fcntl
    name = path.name + '.lock'
    before = _prestat(directory, name, path)
    fd = resources.own(os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
                             os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=directory),
                       os.close, 'lock close')
    _verify_leaf(fd, directory, path, before=before, single=True)
    deadline = time.monotonic() + 15
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f'timed out waiting for key storage lock at {path}') from exc
            time.sleep(.01)
    resources.own(('flock', fd), lambda item: fcntl.flock(item[1], fcntl.LOCK_UN), 'lock unlock')


class Publisher:
    def __init__(self, path, directory, resources):
        self.path, self.directory, self.resources = path, directory, resources
        self.leaf_nlink = None

    def read_existing(self):
        try:
            data, self.leaf_nlink = _read(self.path, self.directory, self.resources)
            return data
        except FileNotFoundError:
            self.leaf_nlink = None
            return None

    def publish(self, data):
        if self.resources.committed:
            raise RuntimeError('publisher already committed')
        if os.name == 'nt':
            from holdem.p2p import windows_security as win
            win.publish(self.path, self.directory, self.resources, data)
            return
        self._publish_posix(data)

    def _publish_posix(self, data):
        # O_PATH pins D but cannot be fsynced. Acquire and verify the durability
        # descriptor before creating a temporary or committing any replacement.
        directory = self.resources.own(os.open('.', os.O_RDONLY | os.O_DIRECTORY |
                                              os.O_NOFOLLOW | os.O_CLOEXEC,
                                              dir_fd=self.directory),
                                      os.close, 'durability directory close')
        info = _verify_directory(directory, self.path.parent, final=True)
        pinned = os.fstat(self.directory)
        if (info.st_dev, info.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise _refuse(self.path, 'durability directory binding changed')
        name = None
        for _ in range(100):
            candidate = f'{self.path.name}.{secrets.token_hex(8)}.tmp'
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.directory)
                name = candidate
                break
            except FileExistsError:
                continue
        if name is None:
            raise FileExistsError('cannot allocate identity temporary')
        self.resources.own(fd, os.close, f'temporary close {name}; deletion may await release')
        try:
            _verify_leaf(fd, self.directory, self.path, single=True)
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError('key storage write made no progress')
                remaining = remaining[written:]
            if os.fstat(fd).st_size != len(data):
                raise OSError('incomplete key storage write')
            os.fsync(fd)
            _verify_leaf(fd, self.directory, self.path, single=True)
            _commit_rename(name, self.directory, self.path.name)
            self.resources.committed = True
        finally:
            if not self.resources.committed:
                try:
                    os.unlink(name, dir_fd=self.directory)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    _diagnose(self.path.parent / name,
                              'owned temporary deletion failed; residual remains; protection unverified', exc)
        try:
            os.fsync(directory)
        except OSError as exc:
            _diagnose(self.path, 'postcommit directory fsync', exc, committed=True)


@contextmanager
def creating(path):
    path = _strict_path(path)
    with _namespace(path, create=True) as (directory, resources):
        if os.name == 'nt':
            from holdem.p2p import windows_security as win
            win.lock(directory, path.name, resources)
        else:
            _lock_posix(path, directory, resources)
        yield Publisher(path, directory, resources)
