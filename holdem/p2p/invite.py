"""
Room invite code format:
  BASE32( version[1] || peer_id_prefix[8] || rendezvous_key[8] || flags[1] )
  = 18 bytes → 29 BASE32 chars → displayed as groups of 4: XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XX
"""
import base64, os, secrets

VERSION = b'\x01'

def generate_room_code(peer_id: bytes | None = None, flags: int = 0) -> str:
    if peer_id is None:
        peer_id = secrets.token_bytes(32)
    prefix = peer_id[:8]
    rendezvous = secrets.token_bytes(8)
    raw = VERSION + prefix + rendezvous + bytes([flags & 0xFF])
    encoded = base64.b32encode(raw).decode().rstrip('=')
    return format_code(encoded)

def parse_room_code(code: str) -> dict:
    stripped = strip_code(code).upper()
    # pad to multiple of 8
    pad = (8 - len(stripped) % 8) % 8
    raw = base64.b32decode(stripped + '=' * pad)
    if len(raw) < 18:
        raise ValueError(f"Invalid room code (too short: {len(raw)} bytes)")
    return {
        "version": raw[0],
        "peer_id_prefix": raw[1:9].hex(),
        "rendezvous_key": raw[9:17].hex(),
        "flags": raw[17],
    }

def format_code(raw: str) -> str:
    raw = raw.replace('-', '')
    return '-'.join(raw[i:i+4] for i in range(0, len(raw), 4))

def strip_code(formatted: str) -> str:
    return formatted.replace('-', '').replace(' ', '')
