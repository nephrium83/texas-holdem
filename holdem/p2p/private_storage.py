"""Serialized, complete publication of host-owned persistent key material.

The lock file is stable and contains no secrets. Never unlink it: replacing
its inode would let concurrent processes acquire different locks.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import platform
import stat
import sys
import tempfile
import time


class InsecureKeyStorage(ValueError):
    """Storage cannot enforce the private identity's security boundary."""


def _refuse(path: Path, reason: str) -> InsecureKeyStorage:
    return InsecureKeyStorage(
        f"insecure key storage at {path}: {reason}; use a supported, "
        "non-aliased local directory owned by this account")


def _lexical_target(target: Path) -> Path:
    # abspath/resolve would erase an alias followed by '..' before inspection.
    target = Path(target)
    if ".." in target.parts:
        raise _refuse(target, "parent traversal is not supported")
    return target if target.is_absolute() else Path.cwd() / target


def _posix_storage_kind(fd: int) -> int:
    """Query the opened object; never infer mount semantics from its pathname."""
    import ctypes
    if (sys.platform != "linux" or ctypes.sizeof(ctypes.c_void_p) != 8
            or platform.machine().lower() not in ("x86_64", "aarch64")):
        raise OSError(errno.ENOTSUP, "unsupported POSIX storage ABI")
    libc = ctypes.CDLL(None, use_errno=True)
    query = libc.fstatfs
    query.argtypes = (ctypes.c_int, ctypes.c_void_p)
    query.restype = ctypes.c_int
    # f_type is the first native word. libc writes the complete statfs struct;
    # use aligned storage larger than that struct on the supported Linux ABIs.
    result = (ctypes.c_long * 512)()
    if query(fd, ctypes.byref(result)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result[0] & 0xffffffff


def _require_posix_storage(fd: int, path: Path) -> None:
    try:
        kind = _posix_storage_kind(fd)
    except (OSError, AttributeError) as exc:
        raise _refuse(path, "filesystem permission semantics cannot be verified") from exc
    if (type(kind) is not int or kind not in
            (0xef53, 0x58465342, 0x9123683e, 0x01021994)):
        raise _refuse(path, "unsupported filesystem permission semantics")


def _require_directory(fd: int, path: Path, *, final: bool) -> None:
    _require_posix_storage(fd, path)
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
        raise _refuse(path, "directory is not owned by this account or root")
    if info.st_mode & 0o022:
        if final or not info.st_mode & stat.S_ISVTX:
            raise _refuse(path, "untrusted accounts can replace directory entries")


@contextmanager
def _private_namespace(directory: Path, *, create: bool):
    """Hold a checked no-follow directory chain through the caller's operation.

    This slice activates POSIX write/lock protection. Existing identity reads
    acquire their own lease when read_private is introduced in the next slice.
    """
    directory = _lexical_target(directory)
    if os.name == "nt":
        # Windows native namespace enforcement is a separate reviewed slice.
        if create:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        yield None
        return
    handles = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        current = Path(directory.anchor)
        fd = os.open(current, flags)
        handles.append(fd)
        _require_directory(fd, current, final=len(directory.parts) == 1)
        for index, part in enumerate(directory.parts[1:], 1):
            current /= part
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise _refuse(current, "symlink or non-directory component") from exc
                raise
            handles.append(child)
            fd = child
            _require_directory(fd, current, final=index == len(directory.parts) - 1)
        yield fd
    finally:
        for handle in reversed(handles):
            os.close(handle)


def ensure_private_dir(directory: Path) -> None:
    with _private_namespace(directory, create=True):
        pass


def canonical_target(target: Path) -> Path:
    """Preserve the supplied POSIX chain; refuse aliases instead of resolving."""
    if os.name == "nt":
        for part in target.parts[1:] if target.anchor else target.parts:
            if part not in (".", "..") and (part.endswith((".", " ")) or ":" in part):
                raise ValueError(f"ambiguous Windows key storage path: {target}")
        return target.resolve()
    target = _lexical_target(target)
    current = Path(target.anchor)
    for part in target.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise _refuse(current, "symlink component")
    return target


@contextmanager
def locked(target: Path):
    """Serialize readers/creators/upgrades across threads and processes."""
    target = canonical_target(target)
    with _private_namespace(target.parent, create=True) as parent_fd:
        with _locked(target, parent_fd):
            yield


@contextmanager
def _locked(target: Path, parent_fd):
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
    name = target.with_name(target.name + ".lock")
    options = {}
    if os.name != "nt":
        flags |= os.O_NOFOLLOW | os.O_NONBLOCK
        name = name.name
        options['dir_fd'] = parent_fd
    try:
        fd = os.open(name, flags, 0o600, **options)
    except OSError as exc:
        if os.name != "nt" and exc.errno in (errno.ELOOP, errno.EISDIR):
            raise _refuse(target, "unsafe lock object") from exc
        raise
    acquired = False
    try:
        if os.name != "nt":
            _require_posix_storage(fd, target)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise _refuse(target, "unsafe lock ownership, type, permissions or links")
        deadline = time.monotonic() + 15
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for key storage lock at {target}") from exc
                time.sleep(.01)
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write(target: Path, data: bytes) -> None:
    """Publish within a protected namespace while the caller holds locked()."""
    target = canonical_target(target)
    with _private_namespace(target.parent, create=True):
        _atomic_write(target, data)


def _atomic_write(target: Path, data: bytes) -> None:
    """Publish complete synced bytes while the caller holds locked(target).

    mkstemp creates POSIX files with mode0600. Windows ACL enforcement is
    separate; chmod is not an owner-only ACL on that platform.
    """
    fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        try:
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("key storage write made no progress")
                remaining = remaining[written:]
            if os.fstat(fd).st_size != len(data):
                raise OSError("incomplete key storage write")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, target)
        if os.name != "nt":
            directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                try:
                    os.fsync(directory)
                except OSError as exc:
                    if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                        raise
            finally:
                os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
