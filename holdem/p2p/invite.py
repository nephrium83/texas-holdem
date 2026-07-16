"""
Room invite code format:
  BASE32( version[1] || peer_id_prefix[8] || rendezvous_key[8] || flags[1] )
  = 18 bytes -> 29 BASE32 chars -> displayed as groups of 4:
  XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XX

The peer_id_prefix is the first 8 bytes of the host Ed25519 public key
(via identity.peer_id()) so joiners can verify they connected to the right
host before the DKG handshake begins.  The rendezvous_key is random and
serves as the LAN multicast / DHT lookup token.
"""
import base64
import secrets

from holdem.p2p import identity as _identity

VERSION = b'\x01'


def generate_room_code(peer_id=None, flags=0):
    if peer_id is None:
        prefix = bytes.fromhex(_identity.peer_id())
    else:
        prefix = peer_id[:8]
    rendezvous = secrets.token_bytes(8)
    raw = VERSION + prefix + rendezvous + bytes([flags & 0xFF])
    encoded = base64.b32encode(raw).decode().rstrip("=")
    return format_code(encoded)


def parse_room_code(code):
    stripped = strip_code(code).upper()
    pad = (8 - len(stripped) % 8) % 8
    raw = base64.b32decode(stripped + "=" * pad)
    if len(raw) < 18:
        raise ValueError("Invalid room code (too short: %d bytes)" % len(raw))
    return {
        "version": raw[0],
        "peer_id_prefix": raw[1:9].hex(),
        "rendezvous_key": raw[9:17].hex(),
        "flags": raw[17],
    }


def format_code(raw):
    raw = raw.replace("-", "")
    return "-".join(raw[i:i+4] for i in range(0, len(raw), 4))


def strip_code(formatted):
    return formatted.replace("-", "").replace(" ", "")
