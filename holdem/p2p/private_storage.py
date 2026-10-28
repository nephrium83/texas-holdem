"""Serialized, complete publication of host-owned persistent key material.

The lock file is stable and contains no secrets. Never unlink it: replacing
its inode would let concurrent processes acquire different locks.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import tempfile
import time


def canonical_target(target: Path) -> Path:
    """One storage/lock name per symlink target; refuse ambiguous Windows names."""
    if os.name == "nt":
        for part in target.parts[1:] if target.anchor else target.parts:
            if part not in (".", "..") and (part.endswith((".", " ")) or ":" in part):
                raise ValueError(f"ambiguous Windows key storage path: {target}")
    return target.resolve()


@contextmanager
def locked(target: Path):
    """Serialize readers/creators/upgrades across threads and processes."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target.with_name(target.name + ".lock"),
                 os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
    acquired = False
    try:
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
