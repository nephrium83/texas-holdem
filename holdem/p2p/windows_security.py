"""Lazy native Windows identity storage; every child is handle-relative.

No library load, token query or filesystem operation occurs at import time.
"""
from __future__ import annotations

from collections import namedtuple
import os
import re
import struct
import time


Ace = namedtuple('Ace', 'type flags mask sid')
TRUSTED_SYSTEM = frozenset({
    'S-1-5-18', 'S-1-5-32-544',
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464',
})
OWNER_RIGHTS = 'S-1-3-4'
_API = {}
_DIR_ACCESS = 0x1200a0
_SYNC_REPARSE = 0x00200020


def _unsafe(reason):
    from holdem.p2p.identity_storage import InsecureKeyStorage
    return InsecureKeyStorage('insecure identity storage: ' + reason)


def sid_text(raw):
    if len(raw) < 8 or raw[0] != 1 or raw[1] > 15 or len(raw) != 8 + raw[1] * 4:
        raise _unsafe('malformed SID')
    result = f'S-1-{int.from_bytes(raw[2:8], "big")}'
    return result + ''.join(f'-{x}' for x in struct.unpack('<' + 'I' * raw[1], raw[8:]))


def parse_acl(raw):
    if len(raw) < 8 or raw[0] not in (2, 4):
        raise _unsafe('malformed ACL header')
    size, count = struct.unpack_from('<HH', raw, 2)
    if size < 8 or size > len(raw):
        raise _unsafe('malformed ACL bounds')
    position, aces = 8, []
    for _ in range(count):
        if position + 8 > size:
            raise _unsafe('truncated ACE')
        kind, flags, length, mask = struct.unpack_from('<BBHI', raw, position)
        if length < 8 or length % 4 or position + length > size:
            raise _unsafe('malformed ACE bounds')
        # Callback ACE application data follows its SID. Unknown ACEs are not
        # interpreted as ordinary SID ACEs and will fail closed.
        body = raw[position + 8:position + length]
        if kind not in (0, 1, 9, 10) or len(body) < 8:
            raise _unsafe('unsupported ACE structure')
        sid_length = 8 + body[1] * 4
        sid = sid_text(body[:sid_length])
        if kind in (0, 1) and sid_length != len(body):
            raise _unsafe('unexpected ordinary ACE payload')
        aces.append(Ace(kind, flags, mask, sid))
        position += length
    return tuple(aces)


def map_generic(mask):
    for bit, mapped in ((0x80000000, 0x120089), (0x40000000, 0x120116),
                        (0x20000000, 0x1200a0), (0x10000000, 0x1f01ff)):
        if mask & bit:
            mask = (mask & ~bit) | mapped
    return mask


def _allows(aces):
    for ace in aces:
        if ace.type not in (0, 1, 9, 10):
            raise _unsafe('unsupported ACE type')
        if ace.type in (0, 9) and not ace.flags & 8 and ace.mask:
            yield ace


def classify_directory(owner, aces, user, *, is_target):
    trusted = TRUSTED_SYSTEM | {user, OWNER_RIGHTS}
    if owner not in TRUSTED_SYSTEM | {user} or aces is None:
        return 'untrusted directory owner or NULL DACL'
    forbidden = 0x10000 | 0x40000 | 0x80000 | 0x40
    if is_target:
        forbidden |= 0x2 | 0x4 | 0x10 | 0x100
    for ace in _allows(aces):
        if ace.sid not in trusted:
            mask = map_generic(ace.mask)
            if mask & ~0x1f01ff or mask & forbidden:
                return 'other accounts can mutate the pinned namespace'
    return None


def classify_leaf(owner, control, aces, user):
    if owner != user or aces is None or not control & 0x1000:
        return 'identity requires token-user ownership and a protected private DACL'
    if any(ace.sid != user for ace in _allows(aces)):
        return 'identity DACL is not exclusively token-user'
    return 'conforming'


def classify_lock(owner, aces, user):
    if owner not in TRUSTED_SYSTEM | {user} or aces is None:
        return 'untrusted lock owner or NULL DACL'
    if any(ace.sid not in TRUSTED_SYSTEM | {user, OWNER_RIGHTS} for ace in _allows(aces)):
        return 'untrusted lock access'
    return None


def file_sddl(user):
    return f'O:{user}D:P(A;;FA;;;{user})'


def dir_sddl(user):
    return f'O:{user}D:P(A;OICI;FA;;;{user})'


def _api():
    if _API:
        return _API
    import ctypes as c
    from ctypes import wintypes as w
    k, a, n = (c.WinDLL('kernel32', use_last_error=True),
               c.WinDLL('advapi32', use_last_error=True), c.WinDLL('ntdll'))
    class Unicode(c.Structure):
        _fields_ = [('Length', w.USHORT), ('MaximumLength', w.USHORT), ('Buffer', w.LPWSTR)]
    class Attributes(c.Structure):
        _fields_ = [('Length', w.ULONG), ('RootDirectory', w.HANDLE),
                    ('ObjectName', c.POINTER(Unicode)), ('Attributes', w.ULONG),
                    ('SecurityDescriptor', c.c_void_p), ('SecurityQualityOfService', c.c_void_p)]
    class StatusUnion(c.Union):
        _fields_ = [('Status', c.c_long), ('Pointer', c.c_void_p)]
    class Status(c.Structure):
        _fields_ = [('u', StatusUnion), ('Information', c.c_size_t)]
    class Standard(c.Structure):
        _fields_ = [('AllocationSize', c.c_longlong), ('EndOfFile', c.c_longlong),
                    ('NumberOfLinks', w.DWORD), ('DeletePending', w.BOOLEAN), ('Directory', w.BOOLEAN)]
    class Rename(c.Structure):
        _fields_ = [('ReplaceIfExists', w.BOOL), ('RootDirectory', w.HANDLE),
                    ('FileNameLength', w.DWORD), ('FileName', w.WCHAR * 1)]
    table = dict(c=c, w=w, Unicode=Unicode, Attributes=Attributes, Status=Status,
                 Standard=Standard, Rename=Rename)
    signatures = [
        (n, 'NtCreateFile', c.c_long, [c.POINTER(w.HANDLE), w.DWORD, c.POINTER(Attributes),
          c.POINTER(Status), c.c_void_p, w.ULONG, w.ULONG, w.ULONG, w.ULONG, c.c_void_p, w.ULONG]),
        (n, 'RtlNtStatusToDosError', w.ULONG, [c.c_long]),
        (n, 'NtQueryVolumeInformationFile', c.c_long,
         [w.HANDLE, c.POINTER(Status), c.c_void_p, w.ULONG, c.c_int]),
        (n, 'NtSetInformationFile', c.c_long,
         [w.HANDLE, c.POINTER(Status), c.c_void_p, w.ULONG, c.c_int]),
        (k, 'CloseHandle', w.BOOL, [w.HANDLE]),
        (k, 'GetCurrentProcess', w.HANDLE, []),
        (k, 'QueryDosDeviceW', w.DWORD, [w.LPCWSTR, w.LPWSTR, w.DWORD]),
        (k, 'GetFinalPathNameByHandleW', w.DWORD, [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]),
        (k, 'GetFileInformationByHandleEx', w.BOOL, [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]),
        (k, 'GetVolumeInformationByHandleW', w.BOOL, [w.HANDLE, w.LPWSTR, w.DWORD,
         c.POINTER(w.DWORD), c.POINTER(w.DWORD), c.POINTER(w.DWORD), w.LPWSTR, w.DWORD]),
        (k, 'SetFileInformationByHandle', w.BOOL, [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]),
        (k, 'ReadFile', w.BOOL, [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]),
        (k, 'LocalFree', c.c_void_p, [c.c_void_p]),
        (a, 'OpenProcessToken', w.BOOL, [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]),
        (a, 'GetTokenInformation', w.BOOL, [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]),
        (a, 'IsValidSid', w.BOOL, [c.c_void_p]),
        (a, 'GetLengthSid', w.DWORD, [c.c_void_p]),
        (a, 'GetSecurityInfo', w.DWORD, [w.HANDLE, c.c_int, w.DWORD,
          c.POINTER(c.c_void_p), c.POINTER(c.c_void_p), c.POINTER(c.c_void_p),
          c.POINTER(c.c_void_p), c.POINTER(c.c_void_p)]),
        (a, 'GetSecurityDescriptorControl', w.BOOL, [c.c_void_p, c.POINTER(w.USHORT), c.POINTER(w.DWORD)]),
        (a, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', w.BOOL,
         [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(w.DWORD)]),
    ]
    for dll, name, result, args in signatures:
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, result
        table[name] = fn
    _API.update(table)
    return _API


def _checked_bool(ok):
    if not ok:
        c = _api()['c']
        code = c.get_last_error()
        raise c.WinError(code)


def _free_sd(value):
    api = _api()
    if api['LocalFree'](value):
        raise OSError('LocalFree failed for security descriptor')


class Handle:
    def __init__(self, value):
        if not value:
            raise _unsafe('native open returned NULL handle')
        self.value = value

    def close(self):
        if self.value is not None:
            value, self.value = self.value, None
            _checked_bool(_api()['CloseHandle'](value))


def token_user_sid():
    api = _api()
    c, w = api['c'], api['w']
    value = w.HANDLE()
    _checked_bool(api['OpenProcessToken'](api['GetCurrentProcess'](), 8, c.byref(value)))
    handle = Handle(value.value)
    try:
        length = w.DWORD()
        ok = api['GetTokenInformation'](handle.value, 1, None, 0, c.byref(length))
        code = c.get_last_error()
        if ok or code != 122:
            raise _unsafe('invalid token size probe')
        buffer = c.create_string_buffer(length.value)
        _checked_bool(api['GetTokenInformation'](handle.value, 1, buffer, length, c.byref(length)))
        sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]
        if not api['IsValidSid'](sid):
            raise _unsafe('invalid token SID')
        return c.string_at(sid, api['GetLengthSid'](sid))
    finally:
        handle.close()


def _open(parent, name, *, access, share, disposition, options, sddl=None, root=False):
    if not root and (not name or name in ('.', '..') or any(x in name for x in '\\/:\0')):
        raise _unsafe('child native name must be one component')
    if root and (parent is not None or len(name) != 7 or name[:4] != '\\??\\'
                 or not name[4].isascii() or not name[4].isalpha() or name[5:] != ':\\'):
        raise _unsafe('invalid absolute root anchor')
    api = _api()
    c, w = api['c'], api['w']
    encoded = name.encode('utf-16-le')
    if len(encoded) > 65532:
        raise _unsafe('native name is too long')
    buffer = c.create_unicode_buffer(name)
    u = api['Unicode'](len(encoded), len(encoded) + 2, c.cast(buffer, w.LPWSTR))
    sd = c.c_void_p()
    opened = None
    primary = None
    try:
        if sddl:
            _checked_bool(api['ConvertStringSecurityDescriptorToSecurityDescriptorW'](sddl, 1, c.byref(sd), None))
        attributes = api['Attributes'](c.sizeof(api['Attributes']),
            None if parent is None else parent.value, c.pointer(u), 0x40, sd, None)
        status, handle = api['Status'](), w.HANDLE()
        deadline = time.monotonic() + 2
        while True:
            result = api['NtCreateFile'](c.byref(handle), access, c.byref(attributes),
                c.byref(status), None, 0x80, share, disposition, options, None, 0)
            if result >= 0:
                opened = Handle(handle.value)
                return opened, status.Information == 2
            unsigned = result & 0xffffffff
            code = api['RtlNtStatusToDosError'](result)
            error = c.WinError(code)
            if unsigned in (0xc0000034, 0xc000003a):
                raise FileNotFoundError(name) from error
            if unsigned == 0xc0000035:
                raise FileExistsError(name) from error
            if unsigned in (0xc0000043, 0xc0000056) and time.monotonic() < deadline:
                time.sleep(.01)
                continue
            raise _unsafe(f'native open refused {name} (Windows error {code})') from error
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if sd.value:
            try:
                _free_sd(sd)
            except BaseException as exc:
                from holdem.p2p.identity_storage import _diagnose
                _diagnose(name, 'security descriptor release failed', exc)
                if opened is not None:
                    try:
                        opened.close()
                    except BaseException as close_error:
                        _diagnose(name, 'open result close failed', close_error)
                if primary is None:
                    raise


def open_root(drive):
    api = _api()
    target = api['c'].create_unicode_buffer(32768)
    count = api['QueryDosDeviceW'](drive, target, len(target))
    _checked_bool(count)
    # A drive-letter root is a trusted volume anchor, not permission to skip
    # filesystem components hidden by SUBST or another DOS path alias.
    if not re.fullmatch(r'\\Device\\HarddiskVolume[0-9]+', target.value, re.IGNORECASE):
        raise _unsafe('drive must map directly to a physical volume, not a substituted path')
    handle = _open(None, '\\??\\' + drive + '\\', access=_DIR_ACCESS, share=3,
                   disposition=1, options=_SYNC_REPARSE | 1, root=True)[0]
    try:
        if _final_name(handle).casefold() != (target.value + '\\').casefold():
            raise _unsafe('drive root does not match its verified physical-volume anchor')
        return handle
    except BaseException:
        handle.close()
        raise


def open_child(parent, name, **kwargs):
    handle, created = _open(parent, name, **kwargs)
    try:
        # Query the opened object, never resolve/reopen the supplied pathname.
        # Refusing alternate 8.3 names keeps leaf+'.lock' unique for upgrades.
        if _final_name(handle).rsplit('\\', 1)[-1].casefold() != name.casefold():
            raise _unsafe('short-name or alternate component aliases are unsupported')
        return handle, created
    except BaseException:
        handle.close()
        raise


def _final_name(handle):
    api = _api()
    buffer = api['c'].create_unicode_buffer(32768)
    count = api['GetFinalPathNameByHandleW'](handle.value, buffer, len(buffer), 2)
    _checked_bool(count)
    if count >= len(buffer):
        raise _unsafe('opened object name cannot be verified')
    return buffer.value


def attribute_tag(handle):
    api = _api()
    values = (api['w'].DWORD * 2)()
    _checked_bool(api['GetFileInformationByHandleEx'](handle.value, 9, values, api['c'].sizeof(values)))
    return values[0], values[1]


def standard_info(handle):
    api = _api()
    info = api['Standard']()
    _checked_bool(api['GetFileInformationByHandleEx'](handle.value, 1, api['c'].byref(info), api['c'].sizeof(info)))
    return info.EndOfFile, info.NumberOfLinks, bool(info.Directory)


def inspect(handle):
    api = _api()
    c, w = api['c'], api['w']
    owner, acl, sd = c.c_void_p(), c.c_void_p(), c.c_void_p()
    code = api['GetSecurityInfo'](handle.value, 1, 5, c.byref(owner), None, c.byref(acl), None, c.byref(sd))
    if code:
        raise c.WinError(code)
    try:
        if not owner.value or not api['IsValidSid'](owner):
            raise _unsafe('invalid owner SID')
        owner_text = sid_text(c.string_at(owner, api['GetLengthSid'](owner)))
        control, revision = w.USHORT(), w.DWORD()
        _checked_bool(api['GetSecurityDescriptorControl'](sd, c.byref(control), c.byref(revision)))
        aces = None
        if acl.value:
            header = c.string_at(acl, 8)
            length = struct.unpack_from('<H', header, 2)[0]
            if length < 8:
                raise _unsafe('invalid native ACL size')
            aces = parse_acl(c.string_at(acl, length))
        return owner_text, control.value, aces
    finally:
        _free_sd(sd)


def volume_facts(handle):
    api = _api()
    c, w = api['c'], api['w']
    device = (w.DWORD * 2)()
    io = api['Status']()
    status = api['NtQueryVolumeInformationFile'](handle.value, c.byref(io), device, c.sizeof(device), 4)
    if status < 0:
        raise c.WinError(api['RtlNtStatusToDosError'](status))
    fs, flags = c.create_unicode_buffer(64), w.DWORD()
    _checked_bool(api['GetVolumeInformationByHandleW'](handle.value, None, 0,
                    None, None, c.byref(flags), fs, len(fs)))
    return device[0], device[1], fs.value.upper(), flags.value


def _verify_volume(handle):
    device, characteristics, name, flags = volume_facts(handle)
    if device != 7 or characteristics & 0x11 or name not in ('NTFS', 'REFS') or not flags & 8:
        raise _unsafe('volume must be a fixed local NTFS/ReFS volume with persistent ACLs')


def _verify_directory(handle, user, *, final, created=False):
    attributes, tag = attribute_tag(handle)
    if not attributes & 0x10 or attributes & 0x400 or tag:
        raise _unsafe('directory is a reparse point or not a directory')
    owner, control, aces = inspect(handle)
    reason = classify_directory(owner, aces, user, is_target=final)
    if reason:
        raise _unsafe(reason)
    if created and classify_leaf(owner, control, aces, user) != 'conforming':
        raise _unsafe('created directory is not protected token-user owned')


def walk(path, resources, *, create):
    user = sid_text(token_user_sid())
    handle = resources.own(open_root(path.drive), lambda h: h.close(), 'root close')
    _verify_volume(handle)
    _verify_directory(handle, user, final=len(path.parent.parts) == 1)
    for index, name in enumerate(path.parent.parts[1:], 1):
        try:
            child, created = open_child(handle, name, access=_DIR_ACCESS, share=3,
                disposition=1, options=_SYNC_REPARSE | 1)
        except FileNotFoundError:
            if not create:
                raise
            try:
                child, created = open_child(handle, name, access=_DIR_ACCESS, share=3,
                    disposition=2, options=_SYNC_REPARSE | 1, sddl=dir_sddl(user))
            except FileExistsError:
                child, created = open_child(handle, name, access=_DIR_ACCESS, share=3,
                    disposition=1, options=_SYNC_REPARSE | 1)
        resources.own(child, lambda h: h.close(), 'directory close')
        _verify_directory(child, user, final=index == len(path.parent.parts) - 1, created=created)
        handle = child
    _verify_volume(handle)
    return handle


def _verify_file(handle, user, *, lock=False, single=False):
    attributes, tag = attribute_tag(handle)
    size, links, directory = standard_info(handle)
    if directory or attributes & (0x10 | 0x400) or tag or (single and links != 1):
        raise _unsafe('file type, reparse tag or link count is unsafe')
    owner, control, aces = inspect(handle)
    reason = classify_lock(owner, aces, user) if lock else classify_leaf(owner, control, aces, user)
    if reason not in (None, 'conforming'):
        raise _unsafe(reason)
    return size, links


def read_all(handle):
    api = _api()
    c, w = api['c'], api['w']
    chunks = []
    while True:
        buffer, length = c.create_string_buffer(65536), w.DWORD()
        _checked_bool(api['ReadFile'](handle.value, buffer, len(buffer), c.byref(length), None))
        if not length.value:
            return b''.join(chunks)
        chunks.append(buffer.raw[:length.value])


def read_existing(directory, leaf, resources):
    handle, _ = open_child(directory, leaf, access=0x120089, share=1,
                           disposition=1, options=_SYNC_REPARSE | 0x40)
    resources.own(handle, lambda h: h.close(), 'leaf close')
    _, links = _verify_file(handle, sid_text(token_user_sid()))
    try:
        data = read_all(handle)
        resources.release(handle)
    except FileNotFoundError as exc:
        raise OSError('opened identity read or release failed') from exc
    return data, links


def adopt(handle, flags):
    import msvcrt
    fd = msvcrt.open_osfhandle(handle.value, flags)
    handle.value = None  # ownership moved only after successful adoption
    return fd


def lock(directory, leaf, resources):
    import msvcrt
    handle, _ = open_child(directory, leaf + '.lock', access=0x12019f, share=3,
        disposition=3, options=_SYNC_REPARSE | 0x40, sddl=file_sddl(sid_text(token_user_sid())))
    resources.own(handle, lambda h: h.close(), 'raw lock close')
    _verify_file(handle, sid_text(token_user_sid()), lock=True, single=True)
    fd = adopt(handle, os.O_RDWR | os.O_BINARY)
    resources.release(handle)
    resources.own(fd, os.close, 'lock close')
    deadline = time.monotonic() + 15
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            break
        except OSError as exc:
            if exc.errno not in (13, 11, 36):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(f'timed out waiting for key storage lock at {leaf}') from exc
            time.sleep(.01)
    def unlock(value):
        os.lseek(value[1], 0, os.SEEK_SET)
        msvcrt.locking(value[1], msvcrt.LK_UNLCK, 1)
    resources.own(('locking', fd), unlock, 'lock unlock')


def rename_to(source, directory, leaf):
    if not leaf or leaf in ('.', '..') or any(x in leaf for x in '\\/:\0'):
        raise _unsafe('rename leaf must be one component')
    api = _api()
    c = api['c']
    encoded = leaf.encode('utf-16-le')
    offset = api['Rename'].FileName.offset
    length = max(c.sizeof(api['Rename']), offset + len(encoded) + 2)
    buffer = c.create_string_buffer(length)
    info = c.cast(buffer, c.POINTER(api['Rename'])).contents
    info.ReplaceIfExists, info.RootDirectory, info.FileNameLength = True, directory.value, len(encoded)
    c.memmove(c.addressof(buffer) + offset, encoded, len(encoded))
    deadline = time.monotonic() + 2
    while True:
        io = api['Status']()
        status = api['NtSetInformationFile'](source, c.byref(io), buffer, length, 10)
        if status >= 0:
            return
        code = api['RtlNtStatusToDosError'](status)
        if code not in (5, 32) or time.monotonic() >= deadline:
            raise c.WinError(code)
        time.sleep(.01)


def delete_by_handle(handle):
    api = _api()
    value = api['w'].BOOL(True)
    _checked_bool(api['SetFileInformationByHandle'](handle.value, 4, api['c'].byref(value), api['c'].sizeof(value)))


def publish(path, directory, resources, data):
    import msvcrt
    import secrets
    from holdem.p2p.identity_storage import _commit_rename, _diagnose
    user = sid_text(token_user_sid())
    for _ in range(100):
        name = f'{path.name}.{secrets.token_hex(8)}.tmp'
        try:
            handle, _ = open_child(directory, name, access=0x13019f, share=0,
                disposition=2, options=_SYNC_REPARSE | 0x40, sddl=file_sddl(user))
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError('cannot allocate identity temporary')
    resources.own(handle, lambda h: h.close(), 'raw temporary close')
    fd = None
    try:
        _verify_file(handle, user, single=True)
        fd = adopt(handle, os.O_RDWR | os.O_BINARY)
        resources.release(handle)
        resources.own(fd, os.close, f'temporary close {name}; deletion may await release')
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError('key storage write made no progress')
            view = view[count:]
        raw = msvcrt.get_osfhandle(fd)
        borrowed = Handle(raw)
        if standard_info(borrowed)[0] != len(data):
            raise OSError('incomplete key storage write')
        os.fsync(fd)
        _verify_file(borrowed, user, single=True)
        _commit_rename(raw, directory, path.name)
        resources.committed = True
    finally:
        if not resources.committed:
            try:
                borrowed = handle if fd is None else Handle(msvcrt.get_osfhandle(fd))
                delete_by_handle(borrowed)
            except BaseException as exc:
                _diagnose(path.parent / name,
                          'owned temporary deletion failed; residual remains; protection unverified', exc)
