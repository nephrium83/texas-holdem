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
    generate_room_code,
    parse_room_code,
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
# Invite code encode / decode — new 29-byte format
# ---------------------------------------------------------------------------

def test_invite_payload_length():
    """generate_room_code must produce exactly PAYLOAD_LEN (29) raw bytes."""
    code = generate_room_code(
        peer_id=b'\x01\x02\x03\x04\x05\x06\x07\x08',
        public_address=("203.0.113.42", 54321),
        relay_address=("192.168.1.10", 7878),
        flags=0,
    )
    import base64
    raw = base64.b32decode(strip_code(code).upper() + "=")
    assert len(raw) == PAYLOAD_LEN, f"Expected {PAYLOAD_LEN} bytes, got {len(raw)}"


def test_invite_encode_decode_with_all_fields():
    """Full round-trip: public_address and relay_address survive encode/decode."""
    peer_id   = b'\xde\xad\xbe\xef\xca\xfe\xba\xbe'
    pub_addr  = ("1.2.3.4",       12345)
    relay     = ("192.168.1.10",  7878)

    code   = generate_room_code(peer_id=peer_id, public_address=pub_addr,
                                relay_address=relay, flags=0x42)
    parsed = parse_room_code(code)

    assert parsed["peer_id_prefix"] == peer_id.hex()
    assert parsed["public_ip"]      == "1.2.3.4"
    assert parsed["public_port"]    == 12345
    assert parsed["relay_host"]     == "192.168.1.10"
    assert parsed["relay_port"]     == 7878
    assert parsed["flags"]          == 0x42
    # rendezvous_key must be 8 bytes encoded as 16 hex chars
    assert len(parsed["rendezvous_key"]) == 16


def test_invite_null_addresses_return_none():
    """When no public_address or relay_address is given, parsed fields are None."""
    code   = generate_room_code(peer_id=bytes(8), flags=0)
    parsed = parse_room_code(code)

    assert parsed["public_ip"]   is None
    assert parsed["public_port"] is None
    assert parsed["relay_host"]  is None
    assert parsed["relay_port"]  is None


def test_invite_rendezvous_key_stable_on_stun_update():
    """Regenerating a code after STUN resolves must preserve the rendezvous_key."""
    # Step 1: initial code without STUN (null public address)
    code1   = generate_room_code(peer_id=bytes(8))
    parsed1 = parse_room_code(code1)
    rk      = parsed1["rendezvous_key"]

    # Step 2: regenerate with STUN result, reusing same rendezvous_key
    code2   = generate_room_code(
        peer_id=bytes(8),
        public_address=("5.6.7.8", 9999),
        relay_address=("192.168.1.10", 7878),
        rendezvous_key=rk,
    )
    parsed2 = parse_room_code(code2)

    assert parsed2["rendezvous_key"] == rk,        "rendezvous_key must not change"
    assert parsed2["public_ip"]      == "5.6.7.8", "STUN address must be updated"
    assert parsed2["relay_host"]     == "192.168.1.10"


def test_invite_hyphen_formatting():
    """Room code must be formatted as groups of 4 chars separated by hyphens."""
    code = generate_room_code(peer_id=bytes(8))
    groups = code.split("-")
    # All groups except possibly the last must be exactly 4 chars
    for g in groups[:-1]:
        assert len(g) == 4, f"Expected 4-char group, got '{g}'"
    # Last group may be 1-4 chars
    assert 1 <= len(groups[-1]) <= 4


def test_invite_rejects_short_code():
    """Codes with fewer than 29 decoded bytes must raise ValueError."""
    import base64
    # Build an 18-byte payload (old Sprint 2 format)
    short_raw = b'\x01' + bytes(17)          # 18 bytes
    short_b32 = base64.b32encode(short_raw).decode().rstrip("=")
    # Format it like a real code
    short_code = "-".join(short_b32[i:i+4] for i in range(0, len(short_b32), 4))

    with pytest.raises(ValueError, match="too short|older-format"):
        parse_room_code(short_code)
