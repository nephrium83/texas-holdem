"""
Room invite code, V2 (70-byte payload).

  BASE32(
    version[1]           ||   # 0x02
    host_pubkey[32]      ||   # EXACT host Ed25519 public key
    discovery_token[8]   ||   # PUBLIC routing token (LAN multicast, relay room)
    admission_secret[16] ||   # PRIVATE capability -- never leaves this code
    public_ip[4]         ||   # STUN-discovered IPv4 (0.0.0.0 if unknown)
    public_port[2]       ||   # STUN-discovered port  (0 if unknown)
    relay_host[4]        ||   # fallback relay IPv4   (0.0.0.0 if none)
    relay_port[2]        ||   # fallback relay port   (0 if none)
    flags[1]
  )
  = 70 bytes -> 112 BASE32 chars, displayed in groups of 4.

Three fields, three very different trust levels
-----------------------------------------------
The V1 code conflated them, and the names actively misled:

``discovery_token``
    PUBLIC. It is multicast on the LAN in cleartext every two seconds and
    used as the relay's room identifier, so it is visible to anyone on the
    network segment and to the relay operator. It routes; it authorizes
    nothing. This is V1's ``rendezvous_key``, renamed because "key" invited
    exactly the mistake of treating it as a secret.

``admission_secret``
    PRIVATE. The capability that proves the bearer was invited. It is never
    multicast, never sent to the relay, and never crosses the wire in any
    form -- it is only ever used as an HMAC key over a transcript of public
    nonces (see holdem/p2p/admission.py). Possession is demonstrated, not
    transmitted.

``host_pubkey``
    The EXACT 32-byte Ed25519 public key of the host. V1 carried only the
    first 8 bytes, and the joiner's check was a startswith() on a truncated
    hex prefix, run at game start -- long after the joiner had already sent
    its identity and joined the roster. 8 bytes is a 64-bit target, which
    is not a comfortable margin for an identity pin, and the check happened
    far too late to protect anything.

Why V1 is refused rather than supported
---------------------------------------
A V1 code cannot express any of the above: it has no admission secret to
prove and no full key to pin, so accepting one means joining
unauthenticated against a truncated identity. Invites are ephemeral --
generated per game, shared by copy/paste, dead when the table ends -- so
the cost of refusing is that a host presses "regenerate" once, and the
benefit is that there is no downgrade path an attacker can steer a joiner
onto. Version detection is by decoded LENGTH first, because a V1 payload
has no version byte and its first byte is part of a public key, which can
legitimately be 0x02.
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

VERSION            = 2
PAYLOAD_LEN        = 70      # V2
PAYLOAD_LEN_V1     = 29      # legacy, refused
HOST_PUBKEY_LEN    = 32
DISCOVERY_TOKEN_LEN = 8
ADMISSION_SECRET_LEN = 16

_OFF_HOST      = 1
_OFF_DISCOVERY = _OFF_HOST + HOST_PUBKEY_LEN            # 33
_OFF_SECRET    = _OFF_DISCOVERY + DISCOVERY_TOKEN_LEN   # 41
_OFF_PUB_IP    = _OFF_SECRET + ADMISSION_SECRET_LEN     # 57
_OFF_PUB_PORT  = _OFF_PUB_IP + 4                        # 61
_OFF_RELAY_IP  = _OFF_PUB_PORT + 2                      # 63
_OFF_RELAY_PORT = _OFF_RELAY_IP + 4                     # 67
_OFF_FLAGS     = _OFF_RELAY_PORT + 2                    # 69


class LegacyInviteError(ValueError):
    """A V1 room code was presented.

    Its own type so callers can tell "regenerate this" apart from "this is
    gibberish", and so no caller can accidentally handle it by falling back
    to an unauthenticated join.
    """


def generate_room_code(
    host_pubkey=None,
    flags=0,
    public_address=None,
    relay_address=None,
    discovery_token=None,
    admission_secret=None,
):
    """Generate a V2 Base32 room invite code.

    Parameters
    ----------
    host_pubkey:
        Raw 32 bytes of the host's Ed25519 public key. Defaults to the
        local peer's. Unlike V1 this is the WHOLE key, not a prefix.
    flags:
        1-byte flags field (reserved; pass 0).
    public_address, relay_address:
        (ip_str, port) pairs, or None.
    discovery_token:
        Hex string (16 chars = 8 bytes) to reuse, so the code can be
        regenerated when STUN resolves without breaking LAN multicast.
        None generates a fresh one.
    admission_secret:
        Hex string (32 chars = 16 bytes) to reuse, for the same reason.
        None generates a fresh one. Regenerating a code for the SAME game
        must pass both, or joiners holding the earlier code lose admission.
    """
    if host_pubkey is None:
        key = _identity.public_key_bytes()
    else:
        key = bytes(host_pubkey)
    if len(key) != HOST_PUBKEY_LEN:
        raise ValueError(
            f"host_pubkey must be exactly {HOST_PUBKEY_LEN} bytes, got "
            f"{len(key)} -- V2 pins the whole key, not a prefix")

    if discovery_token is not None:
        token = bytes.fromhex(discovery_token)[:DISCOVERY_TOKEN_LEN]
    else:
        token = secrets.token_bytes(DISCOVERY_TOKEN_LEN)

    if admission_secret is not None:
        secret = bytes.fromhex(admission_secret)[:ADMISSION_SECRET_LEN]
    else:
        secret = secrets.token_bytes(ADMISSION_SECRET_LEN)
    if len(secret) != ADMISSION_SECRET_LEN:
        raise ValueError(
            f"admission_secret must be {ADMISSION_SECRET_LEN} bytes")

    if public_address:
        pub_ip_bytes   = socket.inet_aton(public_address[0])
        pub_port_bytes = struct.pack(">H", public_address[1])
    else:
        pub_ip_bytes, pub_port_bytes = _NULL_IP, _NULL_PORT

    if relay_address:
        relay_ip_bytes   = socket.inet_aton(relay_address[0])
        relay_port_bytes = struct.pack(">H", relay_address[1])
    else:
        relay_ip_bytes, relay_port_bytes = _NULL_IP, _NULL_PORT

    raw = (bytes([VERSION]) + key + token + secret
           + pub_ip_bytes   + pub_port_bytes
           + relay_ip_bytes + relay_port_bytes
           + bytes([flags & 0xFF]))
    assert len(raw) == PAYLOAD_LEN, f"payload {len(raw)} != {PAYLOAD_LEN}"

    return format_code(base64.b32encode(raw).decode().rstrip("="))


def parse_room_code(code):
    """Decode a V2 room invite code.

    Returns a dict with keys:
      version           -- int (always 2)
      host_pubkey       -- hex string (64 chars), the EXACT key to pin
      discovery_token   -- hex string (16 chars), PUBLIC
      admission_secret  -- hex string (32 chars), PRIVATE. Never transmit.
      public_ip/public_port/relay_host/relay_port -- or None
      flags             -- int

    Raises LegacyInviteError for a V1 code, ValueError for anything else.
    """
    stripped = strip_code(code).upper()
    pad = (8 - len(stripped) % 8) % 8
    try:
        raw = base64.b32decode(stripped + "=" * pad)
    except Exception as exc:
        raise ValueError("Room code is not valid Base32: %s" % exc) from exc

    # Length first: a V1 payload has no version byte, and its leading byte
    # is public-key material that can legitimately equal 0x02. Sniffing the
    # version before the length would misread some V1 codes as V2 and then
    # read a "host key" out of unrelated bytes.
    if len(raw) == PAYLOAD_LEN_V1:
        raise LegacyInviteError(
            "This is a legacy (V1) unauthenticated invite. It carries no "
            "admission secret and only a truncated host key, so it cannot "
            "be used to join securely. Ask the host to regenerate the room "
            "code.")
    if len(raw) != PAYLOAD_LEN:
        raise ValueError(
            "Invalid room code (decoded to %d bytes, expected %d)."
            % (len(raw), PAYLOAD_LEN))
    if raw[0] != VERSION:
        raise ValueError(
            "Unsupported room code version %d (this build speaks %d)."
            % (raw[0], VERSION))

    pub_ip_raw    = raw[_OFF_PUB_IP:_OFF_PUB_IP + 4]
    pub_port_n    = struct.unpack(">H", raw[_OFF_PUB_PORT:_OFF_PUB_PORT + 2])[0]
    pub_ip_int    = struct.unpack(">I", pub_ip_raw)[0]
    relay_ip_raw  = raw[_OFF_RELAY_IP:_OFF_RELAY_IP + 4]
    relay_port_n  = struct.unpack(">H", raw[_OFF_RELAY_PORT:_OFF_RELAY_PORT + 2])[0]
    relay_ip_int  = struct.unpack(">I", relay_ip_raw)[0]

    return {
        "version":          raw[0],
        "host_pubkey":      raw[_OFF_HOST:_OFF_DISCOVERY].hex(),
        "discovery_token":  raw[_OFF_DISCOVERY:_OFF_SECRET].hex(),
        "admission_secret": raw[_OFF_SECRET:_OFF_PUB_IP].hex(),
        "public_ip":        socket.inet_ntoa(pub_ip_raw) if pub_ip_int else None,
        "public_port":      pub_port_n                   if pub_ip_int else None,
        "relay_host":       socket.inet_ntoa(relay_ip_raw) if relay_ip_int else None,
        "relay_port":       relay_port_n                   if relay_ip_int else None,
        "flags":            raw[_OFF_FLAGS],
    }


def public_room_id(parsed_or_code) -> str:
    """The identifier safe to hand to the relay service and the LAN.

    Exists so no call site has to decide for itself which field is safe to
    publish. The V1 code did not offer that choice and the join path sent
    the WHOLE room code as the relay's room name, which under V2 would hand
    the relay operator the admission secret and the host key pin together.
    """
    parsed = (parse_room_code(parsed_or_code)
              if isinstance(parsed_or_code, str) else parsed_or_code)
    return parsed["discovery_token"]


def format_code(raw):
    """Insert hyphens every 4 characters for readability."""
    raw = raw.replace("-", "")
    return "-".join(raw[i : i + 4] for i in range(0, len(raw), 4))


def strip_code(formatted):
    """Remove hyphens and spaces from a formatted room code."""
    return formatted.replace("-", "").replace(" ", "")
