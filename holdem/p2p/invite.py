"""
Room invite code format (Sprint 3A -- 29-byte payload):

  BASE32(
    peer_id_prefix[8]  ||   # first 8 bytes of host Ed25519 pubkey
    rendezvous_key[8]  ||   # random LAN multicast / DHT token
    public_ip[4]       ||   # STUN-discovered IPv4 (0.0.0.0 if unknown)
    public_port[2]     ||   # STUN-discovered port  (0 if unknown)
    relay_host[4]      ||   # fallback relay IPv4   (0.0.0.0 if none)
    relay_port[2]      ||   # fallback relay port   (0 if none)
    flags[1]                # reserved for future use
  )
  = 29 bytes -> 47 BASE32 chars -> displayed as groups of 4 separated by '-'

The peer_id_prefix lets joiners verify they connected to the right host
before the DKG handshake begins (M-7).  The rendezvous_key is the LAN
multicast lookup token.  The public_ip/port pair is the STUN-discovered
external address for direct internet connections; relay_host/port is the
fallback proxy address.

When public_ip is 0.0.0.0 the joiner falls back to LAN multicast.
When relay_host is 0.0.0.0 no relay is available.
"""
from __future__ import annotations

import base64
import secrets
import socket
import struct

from holdem.p2p import identity as _identity

# 4-byte "all zeros" sentinel meaning "not set"
_NULL_IP   = b'\x00\x00\x00\x00'
_NULL_PORT = b'\x00\x00'

PAYLOAD_LEN = 29


def generate_room_code(
    peer_id=None,
    flags=0,
    public_address=None,
    relay_address=None,
    rendezvous_key=None,
):
    """Generate a Base32 room invite code.

    Parameters
    ----------
    peer_id:
        Raw bytes of the host's Ed25519 public key (first 8 bytes are
        used).  Defaults to the local peer's public key.
    flags:
        1-byte flags field (reserved; pass 0).
    public_address:
        (ip_str, port) from STUN, embedded so joiners can try a direct
        connection.  Pass None if STUN failed.
    relay_address:
        (ip_str, port) of the fallback relay server.  Pass None if no
        relay is available.
    rendezvous_key:
        Hex string (16 chars = 8 bytes) of the rendezvous key to reuse.
        Pass None to generate a fresh random key (normal case for a new
        game).  Pass an existing key when regenerating the code after
        STUN resolves so that LAN multicast stays consistent.

    Returns
    -------
    str
        Formatted room code (Base32, groups of 4, separated by hyphens).
    """
    # --- peer_id_prefix (8 bytes) ---
    if peer_id is None:
        prefix = bytes.fromhex(_identity.peer_id())
    else:
        prefix = bytes(peer_id)[:8]

    # --- rendezvous_key (8 bytes) ---
    if rendezvous_key is not None:
        rk = bytes.fromhex(rendezvous_key)[:8]
    else:
        rk = secrets.token_bytes(8)

    # --- public_ip (4 bytes) + public_port (2 bytes) ---
    if public_address:
        pub_ip_bytes   = socket.inet_aton(public_address[0])
        pub_port_bytes = struct.pack(">H", public_address[1])
    else:
        pub_ip_bytes   = _NULL_IP
        pub_port_bytes = _NULL_PORT

    # --- relay_host (4 bytes) + relay_port (2 bytes) ---
    if relay_address:
        relay_ip_bytes   = socket.inet_aton(relay_address[0])
        relay_port_bytes = struct.pack(">H", relay_address[1])
    else:
        relay_ip_bytes   = _NULL_IP
        relay_port_bytes = _NULL_PORT

    # --- flags (1 byte) ---
    flags_byte = bytes([flags & 0xFF])

    raw = (prefix + rk
           + pub_ip_bytes   + pub_port_bytes
           + relay_ip_bytes + relay_port_bytes
           + flags_byte)
    assert len(raw) == PAYLOAD_LEN, "payload length %d != %d" % (len(raw), PAYLOAD_LEN)

    encoded = base64.b32encode(raw).decode().rstrip("=")
    return format_code(encoded)


def parse_room_code(code):
    """Decode a room invite code.

    Returns a dict with keys:
      peer_id_prefix  -- hex string (16 chars)
      rendezvous_key  -- hex string (16 chars)
      public_ip       -- IPv4 string, or None if not set
      public_port     -- int, or None if not set
      relay_host      -- IPv4 string, or None if not set
      relay_port      -- int, or None if not set
      flags           -- int

    Raises ValueError on malformed or unsupported codes.
    """
    stripped = strip_code(code).upper()
    pad = (8 - len(stripped) % 8) % 8
    try:
        raw = base64.b32decode(stripped + "=" * pad)
    except Exception as exc:
        raise ValueError("Room code is not valid Base32: %s" % exc) from exc

    if len(raw) < PAYLOAD_LEN:
        raise ValueError(
            "Invalid room code (decoded to %d bytes, expected %d). "
            "This may be an older-format code -- ask the host to regenerate it."
            % (len(raw), PAYLOAD_LEN)
        )

    peer_id_prefix = raw[0:8].hex()
    rendezvous_key = raw[8:16].hex()

    # public address
    pub_ip_raw  = raw[16:20]
    pub_port_n  = struct.unpack(">H", raw[20:22])[0]
    pub_ip_int  = struct.unpack(">I", pub_ip_raw)[0]
    public_ip   = socket.inet_ntoa(pub_ip_raw) if pub_ip_int != 0 else None
    public_port = pub_port_n                   if pub_ip_int != 0 else None

    # relay address
    relay_ip_raw  = raw[22:26]
    relay_port_n  = struct.unpack(">H", raw[26:28])[0]
    relay_ip_int  = struct.unpack(">I", relay_ip_raw)[0]
    relay_host    = socket.inet_ntoa(relay_ip_raw) if relay_ip_int != 0 else None
    relay_port    = relay_port_n                   if relay_ip_int != 0 else None

    flags = raw[28]

    return {
        "peer_id_prefix": peer_id_prefix,
        "rendezvous_key": rendezvous_key,
        "public_ip":      public_ip,
        "public_port":    public_port,
        "relay_host":     relay_host,
        "relay_port":     relay_port,
        "flags":          flags,
    }


def format_code(raw):
    """Insert hyphens every 4 characters for readability."""
    raw = raw.replace("-", "")
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


def strip_code(formatted):
    """Remove hyphens and spaces from a formatted room code."""
    return formatted.replace("-", "").replace(" ", "")
