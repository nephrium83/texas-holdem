"""Windows ACL/security primitives for owner-only identity-file enforcement.

All ctypes calls happen inside _api(), which is loaded lazily so this module
can be imported on every platform without side-effects. Calling any native
helper on a non-Windows platform raises RuntimeError.

Public API
----------
get_token_user_sid_string() -> str
    Current token user's SID as 'S-R-A-S...' (cached per process).

classify_leaf_dacl(acl_ptr, user_sid_str, sd_control) -> str
    Returns 'conforming', 'trusted_only', or 'refused'.

get_handle_security(handle) -> (owner_str, acl_ptr, sd_control, sd_ptr)
    Calls GetSecurityInfo; sd_ptr must be freed with free_sd().

normalize_leaf_dacl(handle, user_sid_str, path)
    Sets D:P(A;;FA;;;user) via SetSecurityInfo on the held handle.

free_sd(sd_ptr)
    Releases a LocalAlloc'd security descriptor.

parse_acl(acl_bytes) -> list[(ace_type, mask, sid_str)]
    Pure-Python ACL parser; raises InsecureKeyStorage on malformed data.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Exception (re-exported; matches private_storage.InsecureKeyStorage)
# ---------------------------------------------------------------------------

class InsecureKeyStorage(ValueError):
    """Storage cannot enforce the private identity's security boundary."""


# ---------------------------------------------------------------------------
# Well-known trusted SID strings
# ---------------------------------------------------------------------------

_TRUSTED_SIDS: frozenset[str] = frozenset({
    "S-1-5-18",           # LocalSystem (SYSTEM)
    "S-1-5-32-544",       # BUILTIN\Administrators
    # TrustedInstaller (Windows Modules Installer)
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    "S-1-3-4",            # OWNER RIGHTS (trusted only because owner must be trusted)
})

# ACE types
_ACE_ALLOW = 0x00
_ACE_DENY = 0x01
_ACE_ALLOW_CALLBACK = 0x09
_ACE_DENY_CALLBACK = 0x0A

# ACE flags
_INHERIT_ONLY = 0x08  # AceFlags bit: ACE applies to child objects only

# DACL control flags
_SE_DACL_PROTECTED = 0x1000


# ---------------------------------------------------------------------------
# Pure-Python SID parser
# ---------------------------------------------------------------------------

def _sid_string(buf: bytes, offset: int = 0, end: int = -1) -> Optional[str]:
    """Parse SID at buf[offset:end] → canonical 'S-R-A-S...' or None."""
    if end < 0:
        end = len(buf)
    size = end - offset
    if size < 8:
        return None
    rev = buf[offset]
    count = buf[offset + 1]
    if rev != 1 or count > 15:
        return None
    if size < 8 + count * 4:
        return None
    authority = int.from_bytes(buf[offset + 2: offset + 8], "big")
    subs = [
        struct.unpack_from("<I", buf, offset + 8 + i * 4)[0]
        for i in range(count)
    ]
    base = f"S-1-{authority}"
    return (base + "-" + "-".join(str(s) for s in subs)) if subs else base


# ---------------------------------------------------------------------------
# Pure-Python ACL parser
# ---------------------------------------------------------------------------

def parse_acl(acl_buf: bytes) -> list[tuple[int, int, str]]:
    """Parse raw ACL bytes → list of (ace_type, mask, sid_string).

    Only returns effective (non-INHERIT_ONLY, non-zero-mask) ACEs.
    Raises InsecureKeyStorage on any malformed data.
    """
    if len(acl_buf) < 8:
        raise InsecureKeyStorage("ACL header too short")
    revision = acl_buf[0]
    if revision not in (2, 4):
        raise InsecureKeyStorage(f"unsupported ACL revision {revision}")
    acl_size = struct.unpack_from("<H", acl_buf, 2)[0]
    ace_count = struct.unpack_from("<H", acl_buf, 4)[0]
    if acl_size > len(acl_buf):
        raise InsecureKeyStorage("ACL AclSize exceeds buffer")

    result: list[tuple[int, int, str]] = []
    pos = 8  # skip 8-byte ACL header
    for _ in range(ace_count):
        if pos + 8 > acl_size:
            raise InsecureKeyStorage(f"ACE at offset {pos} extends past ACL boundary")
        ace_type = acl_buf[pos]
        ace_flags = acl_buf[pos + 1]
        ace_size = struct.unpack_from("<H", acl_buf, pos + 2)[0]
        if ace_size < 8 or ace_size % 4 != 0 or pos + ace_size > acl_size:
            raise InsecureKeyStorage(f"malformed ACE at offset {pos}: size={ace_size}")
        mask = struct.unpack_from("<I", acl_buf, pos + 4)[0]
        if not (ace_flags & _INHERIT_ONLY) and mask:
            sid_str = _sid_string(acl_buf, pos + 8, pos + ace_size)
            if sid_str is None:
                raise InsecureKeyStorage(f"malformed SID in ACE at offset {pos}")
            result.append((ace_type, mask, sid_str))
        pos += ace_size
    return result


# ---------------------------------------------------------------------------
# DACL classification
# ---------------------------------------------------------------------------

def classify_leaf_dacl(acl_ptr: int, user_sid_str: str, sd_control: int) -> str:
    """Classify file DACL → 'conforming', 'trusted_only', or 'refused'.

    conforming   — DACL is SE_DACL_PROTECTED and every effective allow ACE
                   names only the token user.
    trusted_only — Every effective allow ACE names the user or a TRUSTED_SID;
                   can be normalized to conforming via SetSecurityInfo.
    refused      — NULL DACL, unsupported ACE type, or any effective allow ACE
                   for a SID that is neither the user nor a trusted principal.
    """
    if not acl_ptr:
        return "refused"  # NULL DACL grants full access to everyone

    try:
        import ctypes
        header = (ctypes.c_byte * 4).from_address(acl_ptr)
        acl_size = struct.unpack_from("<H", bytes(header), 2)[0]
        acl_buf = bytes((ctypes.c_byte * acl_size).from_address(acl_ptr))
        aces = parse_acl(acl_buf)
    except InsecureKeyStorage:
        return "refused"
    except (OSError, ValueError):
        return "refused"

    has_non_user_trusted = False

    for ace_type, _mask, sid_str in aces:
        if ace_type in (_ACE_DENY, _ACE_DENY_CALLBACK):
            continue  # deny ACEs cannot grant access; safe to ignore
        if ace_type not in (_ACE_ALLOW, _ACE_ALLOW_CALLBACK):
            return "refused"  # unsupported ACE type
        if sid_str == user_sid_str:
            continue
        if sid_str in _TRUSTED_SIDS:
            has_non_user_trusted = True
            continue
        return "refused"  # untrusted principal has an effective allow ACE

    if has_non_user_trusted:
        return "trusted_only"
    if sd_control & _SE_DACL_PROTECTED:
        return "conforming"
    # Unprotected, user-only: still conforming (no untrusted principal can read)
    return "conforming"


# ---------------------------------------------------------------------------
# Lazy Windows API loader
# ---------------------------------------------------------------------------

_API: dict = {}


def _api() -> dict:
    """Load Windows security APIs once; cache for the process lifetime."""
    if _API:
        return _API
    if sys.platform != "win32":
        raise RuntimeError("windows_security._api() called on a non-Windows platform")

    import ctypes
    import ctypes.wintypes as wt

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # GetSecurityInfo
    fn = adv.GetSecurityInfo
    fn.restype = wt.DWORD
    fn.argtypes = [
        wt.HANDLE, ctypes.c_uint, wt.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _API["GetSecurityInfo"] = fn

    # SetSecurityInfo
    fn = adv.SetSecurityInfo
    fn.restype = wt.DWORD
    fn.argtypes = [
        wt.HANDLE, ctypes.c_uint, wt.DWORD,
        ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    _API["SetSecurityInfo"] = fn

    # LocalFree
    fn = k32.LocalFree
    fn.restype = wt.HANDLE
    fn.argtypes = [ctypes.c_void_p]
    _API["LocalFree"] = fn

    # GetSecurityDescriptorControl
    fn = adv.GetSecurityDescriptorControl
    fn.restype = wt.BOOL
    fn.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wt.WORD),
        ctypes.POINTER(wt.DWORD),
    ]
    _API["GetSDControl"] = fn

    # GetSecurityDescriptorDacl
    fn = adv.GetSecurityDescriptorDacl
    fn.restype = wt.BOOL
    fn.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wt.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wt.BOOL),
    ]
    _API["GetSDDacl"] = fn

    # OpenProcessToken
    fn = adv.OpenProcessToken
    fn.restype = wt.BOOL
    fn.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    _API["OpenProcessToken"] = fn

    # GetTokenInformation
    fn = adv.GetTokenInformation
    fn.restype = wt.BOOL
    fn.argtypes = [
        wt.HANDLE, ctypes.c_uint, ctypes.c_void_p,
        wt.DWORD, ctypes.POINTER(wt.DWORD),
    ]
    _API["GetTokenInformation"] = fn

    # ConvertSidToStringSidW
    fn = adv.ConvertSidToStringSidW
    fn.restype = wt.BOOL
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.LPWSTR)]
    _API["ConvertSidToStringSidW"] = fn

    # ConvertStringSecurityDescriptorToSecurityDescriptorW
    fn = adv.ConvertStringSecurityDescriptorToSecurityDescriptorW
    fn.restype = wt.BOOL
    fn.argtypes = [
        wt.LPCWSTR, wt.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wt.ULONG),
    ]
    _API["ConvertStringSD"] = fn

    # CloseHandle (kernel32)
    fn = k32.CloseHandle
    fn.restype = wt.BOOL
    fn.argtypes = [wt.HANDLE]
    _API["CloseHandle"] = fn

    # GetCurrentProcess (pseudo-handle, never closed)
    fn = k32.GetCurrentProcess
    fn.restype = wt.HANDLE
    fn.argtypes = []
    _API["GetCurrentProcess"] = fn

    return _API


# ---------------------------------------------------------------------------
# Token user SID (cached per process)
# ---------------------------------------------------------------------------

_CACHED_USER_SID: Optional[str] = None


def get_token_user_sid_string() -> str:
    """Return the current token user's SID as a canonical 'S-R-A-S...' string."""
    global _CACHED_USER_SID
    if _CACHED_USER_SID is not None:
        return _CACHED_USER_SID

    import ctypes
    import ctypes.wintypes as wt

    api = _api()
    TOKEN_QUERY = 0x0008
    TokenUser = 1

    token = wt.HANDLE()
    if not api["OpenProcessToken"](
        api["GetCurrentProcess"](), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wt.DWORD(0)
        api["GetTokenInformation"](token, TokenUser, None, 0, ctypes.byref(needed))
        if ctypes.get_last_error() != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise ctypes.WinError(ctypes.get_last_error())
        buf = (ctypes.c_byte * needed.value)()
        if not api["GetTokenInformation"](
            token, TokenUser, buf, needed, ctypes.byref(needed)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER: { SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes } }
        # Sid is the first pointer-sized field in the buffer
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        str_ptr = wt.LPWSTR()
        if not api["ConvertSidToStringSidW"](sid_ptr, ctypes.byref(str_ptr)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            result = str_ptr.value
        finally:
            api["LocalFree"](str_ptr)
        _CACHED_USER_SID = result
        return result
    finally:
        api["CloseHandle"](token)


# ---------------------------------------------------------------------------
# Security info for a Win32 handle
# ---------------------------------------------------------------------------

def get_handle_security(handle: int) -> tuple[str, int, int, int]:
    """Return (owner_sid_str, acl_ptr, sd_control, sd_ptr).

    The caller must release sd_ptr with free_sd() when done.
    acl_ptr and sd_control are 0 if no DACL is present.
    """
    import ctypes
    import ctypes.wintypes as wt

    api = _api()
    SE_FILE_OBJECT = 1
    OWNER_SI = 0x00000001
    DACL_SI = 0x00000004

    p_owner = ctypes.c_void_p()
    p_dacl = ctypes.c_void_p()
    p_sd = ctypes.c_void_p()

    err = api["GetSecurityInfo"](
        handle, SE_FILE_OBJECT, OWNER_SI | DACL_SI,
        ctypes.byref(p_owner), None,
        ctypes.byref(p_dacl), None,
        ctypes.byref(p_sd),
    )
    if err != 0:
        raise OSError(None, f"GetSecurityInfo failed (error {err})", None, err)

    # Convert owner SID to string
    str_ptr = wt.LPWSTR()
    if not api["ConvertSidToStringSidW"](p_owner, ctypes.byref(str_ptr)):
        err2 = ctypes.get_last_error()
        api["LocalFree"](p_sd)
        raise ctypes.WinError(err2)
    try:
        owner_str = str_ptr.value
    finally:
        api["LocalFree"](str_ptr)

    # Get DACL control flags
    control = wt.WORD(0)
    revision = wt.DWORD(0)
    api["GetSDControl"](p_sd, ctypes.byref(control), ctypes.byref(revision))

    return owner_str, p_dacl.value or 0, control.value, p_sd.value or 0


def free_sd(sd_ptr: int) -> None:
    """Release a security descriptor obtained from GetSecurityInfo."""
    if sd_ptr:
        _api()["LocalFree"](sd_ptr)


# ---------------------------------------------------------------------------
# DACL normalization
# ---------------------------------------------------------------------------

def normalize_leaf_dacl(handle: int, user_sid_str: str, path: Path) -> None:
    """Set D:P(A;;FA;;;{user}) on the file identified by handle.

    Raises InsecureKeyStorage if the operation fails.
    """
    import ctypes
    import ctypes.wintypes as wt

    api = _api()
    sddl = f"D:P(A;;FA;;;{user_sid_str})"
    p_sd = ctypes.c_void_p()
    sd_size = wt.ULONG(0)
    if not api["ConvertStringSD"](sddl, 1, ctypes.byref(p_sd), ctypes.byref(sd_size)):
        raise InsecureKeyStorage(
            f"cannot build owner-only security descriptor for {path}: "
            f"{ctypes.WinError(ctypes.get_last_error())}"
        )
    try:
        dacl_present = wt.BOOL(False)
        p_dacl = ctypes.c_void_p()
        dacl_defaulted = wt.BOOL(False)
        if not api["GetSDDacl"](
            p_sd, ctypes.byref(dacl_present),
            ctypes.byref(p_dacl), ctypes.byref(dacl_defaulted),
        ):
            raise InsecureKeyStorage(
                f"cannot extract DACL from new security descriptor for {path}: "
                f"{ctypes.WinError(ctypes.get_last_error())}"
            )
        SE_FILE_OBJECT = 1
        DACL_SI = 0x00000004
        # PROTECTED_DACL_SECURITY_INFORMATION prevents Windows from re-inheriting
        # parent directory ACEs after we set the DACL. Without it the file reverts
        # to trusted-only immediately. Requires WRITE_OWNER on the handle (the
        # caller must open with WRITE_DAC | READ_CONTROL | WRITE_OWNER).
        PROTECTED_DACL_SI = 0x80000000
        err = api["SetSecurityInfo"](
            handle, SE_FILE_OBJECT,
            DACL_SI | PROTECTED_DACL_SI,
            None, None, p_dacl, None,
        )
        if err != 0:
            raise InsecureKeyStorage(
                f"SetSecurityInfo failed normalizing DACL for {path} (error {err})"
            )
    finally:
        api["LocalFree"](p_sd)


__all__ = [
    "InsecureKeyStorage",
    "parse_acl",
    "classify_leaf_dacl",
    "get_token_user_sid_string",
    "get_handle_security",
    "free_sd",
    "normalize_leaf_dacl",
]
