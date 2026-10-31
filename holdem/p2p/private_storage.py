"""Serialized, complete publication of host-owned persistent key material.

The lock file is stable and contains no secrets. Never unlink it: replacing
its inode would let concurrent processes acquire different locks.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import errno
import os
from pathlib import Path
import platform
import stat
import sys
import tempfile
import time
from typing import NamedTuple


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


class PrivateRead(NamedTuple):
    data: bytes
    links: int


# ---------------------------------------------------------------------------
# Windows leaf-file security (windows-strict-leaf-read slice)
# ---------------------------------------------------------------------------

def _nt_open_with_write_dac(path: Path):
    """Open path with WRITE_DAC access using CreateFileW; return raw handle.

    Returns (handle, valid) where valid=False means ACCESS_DENIED and the
    caller must CloseHandle even on failure (handle is INVALID_HANDLE_VALUE).
    Raises OSError for errors other than ACCESS_DENIED.
    """
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CreateFileW = k32.CreateFileW
    CreateFileW.restype = wt.HANDLE
    CreateFileW.argtypes = [
        wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
        wt.DWORD, wt.DWORD, wt.HANDLE,
    ]
    WRITE_DAC = 0x00040000
    READ_CONTROL = 0x00020000  # SetSecurityInfo reads existing SD internally
    WRITE_OWNER = 0x00080000   # PROTECTED_DACL_SECURITY_INFORMATION requires this
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    INVALID_HANDLE_VALUE = ctypes.cast(ctypes.c_void_p(-1), wt.HANDLE).value

    handle = CreateFileW(
        str(path), WRITE_DAC | READ_CONTROL | WRITE_OWNER,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, None,
    )
    if handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        if err == 5:  # ERROR_ACCESS_DENIED
            return handle, False
        raise OSError(None, f"CreateFileW failed (error {err})", str(path), err)
    return handle, True


def _nt_close(handle) -> None:
    import ctypes
    import ctypes.wintypes as wt
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.CloseHandle.restype = wt.BOOL
    k32.CloseHandle(handle)


def _require_private_existing_windows(fd: int, path: Path, *, can_repair: bool) -> None:
    """Verify Windows identity file owner and DACL; normalize TRUSTED-ONLY DACLs.

    CONFORMING  : SE_DACL_PROTECTED, every effective allow ACE names the token
                  user → accepted, no repair needed.
    TRUSTED-ONLY: every effective allow ACE names the user or a trusted OS
                  principal (SYSTEM / Administrators / TrustedInstaller /
                  OWNER RIGHTS), e.g. the stock inherited %APPDATA% DACL →
                  normalized to D:P(A;;FA;;;user) via WRITE_DAC reopen.
    REFUSED     : NULL DACL, unsupported ACE type, or any effective allow ACE
                  for an untrusted SID → InsecureKeyStorage.
    """
    import msvcrt
    from holdem.p2p.windows_security import (
        get_token_user_sid_string, get_handle_security,
        classify_leaf_dacl, normalize_leaf_dacl, free_sd,
    )

    handle = msvcrt.get_osfhandle(fd)
    user_sid = get_token_user_sid_string()

    owner_str, acl_ptr, sd_control, sd_ptr = get_handle_security(handle)
    try:
        if owner_str != user_sid:
            raise _refuse(
                path,
                f"file is not owned by the current user "
                f"(owner {owner_str}); change ownership or copy to a "
                f"user-owned location"
            )
        classification = classify_leaf_dacl(acl_ptr, user_sid, sd_control)
    finally:
        free_sd(sd_ptr)

    if classification == "conforming":
        return

    if classification == "refused":
        raise _refuse(
            path,
            "file DACL grants access to untrusted principals or has an "
            "unsupported structure; remedy: "
            f'icacls "{path}" /inheritance:r /grant:r *{user_sid}:F'
        )

    # TRUSTED-ONLY: attempt normalization via a WRITE_DAC reopen.
    if not can_repair:
        raise _refuse(path, "inherited DACL cannot be normalized on this handle")

    repair_handle, have_write_dac = _nt_open_with_write_dac(path.resolve())
    if not have_write_dac:
        raise _refuse(
            path,
            "file has an inherited DACL and WRITE_DAC access was denied; "
            f'remedy: icacls "{path}" /inheritance:r /grant:r *{user_sid}:F'
        )
    try:
        normalize_leaf_dacl(repair_handle, user_sid, path)
    finally:
        _nt_close(repair_handle)

    # Re-read from the original handle to confirm normalization succeeded.
    owner2, acl2, ctrl2, sd2 = get_handle_security(handle)
    try:
        if classify_leaf_dacl(acl2, user_sid, ctrl2) != "conforming":
            raise _refuse(path, "DACL normalization did not achieve conforming state")
    finally:
        free_sd(sd2)


def _require_private_temp_windows(fd: int, path: Path) -> None:
    """Verify owner and normalize DACL of a just-created Windows temp key file.

    mkstemp inherits the parent directory's DACL (typically TRUSTED-ONLY on a
    standard %APPDATA% path). Normalize to D:P(A;;FA;;;user) before writing
    any key bytes so that the completed publication is owner-only from the
    moment of the atomic rename.
    """
    import msvcrt
    from holdem.p2p.windows_security import (
        get_token_user_sid_string, get_handle_security,
        classify_leaf_dacl, normalize_leaf_dacl, free_sd,
    )

    handle = msvcrt.get_osfhandle(fd)
    user_sid = get_token_user_sid_string()

    owner_str, acl_ptr, sd_control, sd_ptr = get_handle_security(handle)
    try:
        if owner_str != user_sid:
            raise _refuse(path, "temporary key file is not owned by the current user")
        classification = classify_leaf_dacl(acl_ptr, user_sid, sd_control)
        if classification == "refused":
            raise _refuse(
                path, "temporary key file DACL grants access to untrusted principals"
            )
    finally:
        free_sd(sd_ptr)

    if classification == "conforming":
        return

    # TRUSTED-ONLY: normalize via a WRITE_DAC reopen (temp file was just created).
    repair_handle, have_write_dac = _nt_open_with_write_dac(path)
    if not have_write_dac:
        raise _refuse(path, "temporary key file: WRITE_DAC access denied during normalization")
    try:
        normalize_leaf_dacl(repair_handle, user_sid, path)
    finally:
        _nt_close(repair_handle)

    owner2, acl2, ctrl2, sd2 = get_handle_security(handle)
    try:
        if classify_leaf_dacl(acl2, user_sid, ctrl2) != "conforming":
            raise _refuse(path, "temporary key file DACL normalization failed")
    finally:
        free_sd(sd2)


# ---------------------------------------------------------------------------
# Cross-platform file verification
# ---------------------------------------------------------------------------

def _require_private_existing(fd: int, path: Path, *, can_repair: bool) -> None:
    """Verify and, when permitted, restrict the exact opened object."""
    if os.name == "nt":
        _require_private_existing_windows(fd, path, can_repair=can_repair)
        return
    try:
        _require_posix_storage(fd, path)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _refuse(path, 'not a regular file')
        if info.st_uid != os.geteuid():
            raise _refuse(path, 'file is not owned by this account')
        if info.st_mode & 0o077:
            if not can_repair:
                raise _refuse(path, 'file permissions cannot be repaired')
            os.fchmod(fd, stat.S_IMODE(info.st_mode) & 0o700)
            repaired = os.fstat(fd)
            if (not stat.S_ISREG(repaired.st_mode) or repaired.st_uid != os.geteuid()
                    or repaired.st_mode & 0o077):
                raise _refuse(path, 'file permissions could not be restricted')
    except OSError as exc:
        raise _refuse(path, 'file security could not be verified or repaired') from exc


def read_private(target: Path) -> PrivateRead | None:
    """Return bytes and link count from one checked fd, without reopening."""
    target = canonical_target(target)
    with ExitStack() as stack:
        try:
            parent_fd = stack.enter_context(_private_namespace(target.parent, create=False))
        except FileNotFoundError:
            return None
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)
        options = {}
        name = target
        if os.name != 'nt':
            flags |= os.O_NONBLOCK | os.O_NOFOLLOW
            options['dir_fd'] = parent_fd
            name = target.name
        try:
            fd = os.open(name, flags, **options)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if os.name != 'nt' and exc.errno in (errno.ELOOP, errno.ENXIO):
                raise _refuse(target, 'redirected or non-regular identity') from exc
            raise
        try:
            _require_private_existing(fd, target, can_repair=True)
            links = os.fstat(fd).st_nlink
            chunks = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return PrivateRead(b''.join(chunks), links)
        finally:
            os.close(fd)


def canonical_target(target: Path, *, allow_aliases: bool = False) -> Path:
    """Preserve the supplied POSIX chain; refuse aliases instead of resolving."""
    if os.name == "nt":
        for part in target.parts[1:] if target.anchor else target.parts:
            if part not in (".", "..") and (part.endswith((".", " ")) or ":" in part):
                raise ValueError(f"ambiguous Windows key storage path: {target}")
        return target.resolve()
    if allow_aliases:
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
    with _private_namespace(target.parent, create=True) as parent_fd:
        _atomic_write(target, data, parent_fd)


def _require_private_temp(fd: int, path: Path) -> None:
    if os.name == "nt":
        _require_private_temp_windows(fd, path)
        return
    try:
        _require_posix_storage(fd, path)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise _refuse(path, 'temporary key file is not owner-only')
    except OSError as exc:
        raise _refuse(path, 'temporary file security could not be verified') from exc


def _atomic_write(target: Path, data: bytes, parent_fd) -> None:
    """Publish complete synced bytes while the caller holds locked(target).

    mkstemp creates POSIX files with mode 0600. Windows temp-file DACL
    normalization runs inside _require_private_temp before the first key byte.
    """
    fd, name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        try:
            _require_private_temp(fd, temporary)
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("key storage write made no progress")
                remaining = remaining[written:]
            if os.fstat(fd).st_size != len(data):
                raise OSError("incomplete key storage write")
            os.fsync(fd)
            _require_private_temp(fd, temporary)
        finally:
            os.close(fd)
        if os.name == 'nt':
            os.replace(temporary, target)
        else:
            os.replace(temporary.name, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
    finally:
        try:
            if os.name == 'nt':
                temporary.unlink()
            else:
                os.unlink(temporary.name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
