"""
P2P layer tests — Sprint 3A: STUN + relay invite codes.

Covers:
- STUN binding-request byte format (RFC 5389 header, no network required)
- STUN XOR-MAPPED-ADDRESS decoding math
- Invite code encode / decode round-trips with public_address and relay_address
- Null-address sentinel behaviour (0.0.0.0 maps to None)
- rendezvous_key stability when regenerating a code after STUN resolves
- Old-format (< 29 byte) codes produce a clear ValueError
"""
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.invite import (
    PAYLOAD_LEN,
    LegacyInviteError,
    generate_room_code,
    parse_room_code,
    public_room_id,
    strip_code,
)
from holdem.p2p.stun import MAGIC_COOKIE, _build_request, _parse_response, STUNError


# ---------------------------------------------------------------------------
# STUN request format (RFC 5389 §6)
# ---------------------------------------------------------------------------

def test_stun_request_format():
    """STUN binding request must be 20 bytes with the correct RFC 5389 header."""
    request, transaction_id = _build_request()

    assert len(request) == 20, "STUN header is always 20 bytes"

    # Bytes 0-1: message type = Binding Request (0x0001)
    (msg_type,) = struct.unpack(">H", request[0:2])
    assert msg_type == 0x0001, f"Expected Binding Request 0x0001, got {msg_type:#06x}"

    # Bytes 2-3: message length = 0 (no attributes in a bare binding request)
    (msg_len,) = struct.unpack(">H", request[2:4])
    assert msg_len == 0x0000, "Bare binding request carries no attributes"

    # Bytes 4-7: magic cookie = 0x2112A442
    (magic,) = struct.unpack(">I", request[4:8])
    assert magic == MAGIC_COOKIE == 0x2112A442, "Magic cookie mismatch"

    # Bytes 8-19: 12-byte transaction ID, must match what was returned
    assert len(transaction_id) == 12
    assert request[8:20] == transaction_id, "Transaction ID bytes must appear verbatim"


def test_stun_xor_mapped_address_decode():
    """XOR-MAPPED-ADDRESS decoding must correctly reverse the XOR mask."""
    # Craft a minimal synthetic STUN Binding Success Response that contains a
    # single XOR-MAPPED-ADDRESS attribute for 93.184.216.34:4321.
    ip_str  = "93.184.216.34"
    port    = 4321

    import socket
    ip_int  = struct.unpack(">I", socket.inet_aton(ip_str))[0]
    xor_port = port    ^ (MAGIC_COOKIE >> 16)
    xor_ip   = ip_int  ^ MAGIC_COOKIE

    # Build attribute: type=0x0020, len=8, reserved, family=0x01, XOR-port, XOR-IP
    attr_body = struct.pack(">BBHI", 0x00, 0x01, xor_port, xor_ip)  # 8 bytes
    attr = struct.pack(">HH", 0x0020, 8) + attr_body                 # 4-byte header

    # Build a minimal response header (20 bytes): type=0x0101, len=len(attr)
    txid = b'\x00' * 12
    header = struct.pack(">HHI", 0x0101, len(attr), MAGIC_COOKIE) + txid
    response = header + attr

    decoded_ip, decoded_port = _parse_response(response)
    assert decoded_ip   == ip_str, f"IP mismatch: {decoded_ip} != {ip_str}"
    assert decoded_port == port,   f"Port mismatch: {decoded_port} != {port}"


def test_stun_error_on_wrong_magic():
    """A response with the wrong magic cookie must raise STUNError."""
    txid     = b'\x00' * 12
    response = struct.pack(">HHI", 0x0101, 0, 0xDEADBEEF) + txid  # bad magic
    with pytest.raises(STUNError, match="magic cookie"):
        _parse_response(response)


def test_stun_error_on_short_response():
    """A response shorter than 20 bytes must raise STUNError."""
    with pytest.raises(STUNError, match="too short"):
        _parse_response(b'\x00' * 10)


# ---------------------------------------------------------------------------
# Invite code encode / decode — V2 (70-byte) format
# ---------------------------------------------------------------------------

_HOST_KEY = bytes(range(32))


def test_invite_payload_length():
    """generate_room_code must produce exactly PAYLOAD_LEN (70) raw bytes."""
    code = generate_room_code(
        host_pubkey=_HOST_KEY,
        public_address=("203.0.113.42", 54321),
        relay_address=("192.168.1.10", 7878),
        flags=0,
    )
    import base64
    stripped = strip_code(code).upper()
    raw = base64.b32decode(stripped + "=" * ((8 - len(stripped) % 8) % 8))
    assert len(raw) == PAYLOAD_LEN, f"Expected {PAYLOAD_LEN} bytes, got {len(raw)}"


def test_invite_encode_decode_with_all_fields():
    """Full round-trip, including the whole host key rather than a prefix."""
    pub_addr  = ("1.2.3.4",       12345)
    relay     = ("192.168.1.10",  7878)

    code   = generate_room_code(host_pubkey=_HOST_KEY, public_address=pub_addr,
                                relay_address=relay, flags=0x42)
    parsed = parse_room_code(code)

    assert parsed["version"]     == 2
    assert parsed["host_pubkey"] == _HOST_KEY.hex(), (
        "V2 must carry the EXACT 32-byte key; a prefix is a 64-bit identity "
        "target and cannot be pinned against")
    assert parsed["public_ip"]   == "1.2.3.4"
    assert parsed["public_port"] == 12345
    assert parsed["relay_host"]  == "192.168.1.10"
    assert parsed["relay_port"]  == 7878
    assert parsed["flags"]       == 0x42
    assert len(parsed["discovery_token"])  == 16     # 8 bytes
    assert len(parsed["admission_secret"]) == 32     # 16 bytes


def test_the_discovery_token_and_admission_secret_are_different_values():
    """They are not two names for one field.

    The whole point of V2 is that one of these is multicast in cleartext
    and the other is a capability. If a refactor ever collapsed them, every
    other test here would still pass.
    """
    parsed = parse_room_code(generate_room_code(host_pubkey=_HOST_KEY))
    assert parsed["discovery_token"] != parsed["admission_secret"][:16]


def test_invite_null_addresses_return_none():
    """When no public_address or relay_address is given, parsed fields are None."""
    parsed = parse_room_code(generate_room_code(host_pubkey=_HOST_KEY, flags=0))

    assert parsed["public_ip"]   is None
    assert parsed["public_port"] is None
    assert parsed["relay_host"]  is None
    assert parsed["relay_port"]  is None


def test_invite_routing_and_capability_survive_stun_update():
    """Regenerating after STUN must preserve BOTH the token and the secret.

    The token so LAN multicast keeps resolving; the secret because anyone
    who already copied the code would otherwise be locked out by a refresh
    they never saw.
    """
    parsed1 = parse_room_code(generate_room_code(host_pubkey=_HOST_KEY))
    token, secret = parsed1["discovery_token"], parsed1["admission_secret"]

    parsed2 = parse_room_code(generate_room_code(
        host_pubkey=_HOST_KEY,
        public_address=("5.6.7.8", 9999),
        relay_address=("192.168.1.10", 7878),
        discovery_token=token,
        admission_secret=secret,
    ))

    assert parsed2["discovery_token"]  == token,  "routing token must not change"
    assert parsed2["admission_secret"] == secret, "capability must not change"
    assert parsed2["public_ip"]        == "5.6.7.8"
    assert parsed2["relay_host"]       == "192.168.1.10"


def test_public_room_id_is_the_token_and_not_the_secret():
    """What the relay is allowed to see."""
    code   = generate_room_code(host_pubkey=_HOST_KEY)
    parsed = parse_room_code(code)
    room   = public_room_id(parsed)

    assert room == parsed["discovery_token"]
    assert parsed["admission_secret"] not in room
    assert parsed["host_pubkey"] not in room
    assert public_room_id(code) == room, "must accept a raw code too"


def test_invite_hyphen_formatting():
    """Room code must be formatted as groups of 4 chars separated by hyphens."""
    groups = generate_room_code(host_pubkey=_HOST_KEY).split("-")
    for g in groups[:-1]:
        assert len(g) == 4, f"Expected 4-char group, got '{g}'"
    assert 1 <= len(groups[-1]) <= 4


def test_a_v1_room_code_is_refused_and_says_why():
    """No silent downgrade to unauthenticated joining.

    A V1 code has no admission secret to prove and only a truncated host
    key to pin, so accepting one means joining unauthenticated. It gets its
    own exception type so no caller can handle it as "malformed" and fall
    back.
    """
    import base64
    v1_raw = bytes(29)
    b32 = base64.b32encode(v1_raw).decode().rstrip("=")
    v1_code = "-".join(b32[i:i+4] for i in range(0, len(b32), 4))

    with pytest.raises(LegacyInviteError, match="regenerate"):
        parse_room_code(v1_code)


def test_a_v1_code_whose_first_byte_is_two_is_still_refused():
    """Version detection must not be a first-byte sniff.

    A V1 payload has no version byte -- byte 0 is public-key material and
    can legitimately be 0x02. Sniffing the version before the length would
    read such a code as V2 and parse a "host key" out of unrelated bytes.
    """
    import base64
    v1_raw = bytes([0x02]) + bytes(28)
    b32 = base64.b32encode(v1_raw).decode().rstrip("=")
    code = "-".join(b32[i:i+4] for i in range(0, len(b32), 4))

    with pytest.raises(LegacyInviteError):
        parse_room_code(code)


def test_invite_rejects_short_code():
    """Anything that is neither V1 nor V2 is malformed."""
    import base64
    short_raw = b'' + bytes(17)          # 18 bytes
    short_b32 = base64.b32encode(short_raw).decode().rstrip("=")
    short_code = "-".join(short_b32[i:i+4] for i in range(0, len(short_b32), 4))

    with pytest.raises(ValueError, match="expected"):
        parse_room_code(short_code)


def test_generate_refuses_a_truncated_host_key():
    """V1's 8-byte prefix must not be passable as a V2 key."""
    with pytest.raises(ValueError, match="32 bytes"):
        generate_room_code(host_pubkey=b"" * 8)
