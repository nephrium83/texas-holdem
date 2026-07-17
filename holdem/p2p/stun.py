"""
Async STUN client — RFC 5389 Binding Request.

Usage::

    from holdem.p2p.stun import get_public_address, STUNError

    try:
        ip, port = await get_public_address(local_port=12345)
    except STUNError as exc:
        print(f"STUN failed: {exc}")

The function sends a single UDP Binding Request to ``stun.l.google.com:19302``
and parses the XOR-MAPPED-ADDRESS attribute from the response.  Raises
``STUNError`` on any failure (DNS, timeout, unexpected response, etc.).
"""
from __future__ import annotations

import asyncio
import secrets
import socket
import struct

STUN_HOST = "stun.l.google.com"
STUN_PORT = 19302
MAGIC_COOKIE = 0x2112A442

# Binding Request / Binding Success Response message types
_BINDING_REQUEST  = 0x0001
_BINDING_RESPONSE = 0x0101

# XOR-MAPPED-ADDRESS attribute type
_XOR_MAPPED_ADDRESS = 0x0020

# IPv4 address family
_FAMILY_IPV4 = 0x01


class STUNError(Exception):
    """Raised when the STUN exchange fails for any reason."""


def _build_request() -> tuple[bytes, bytes]:
    """Return ``(request_bytes, transaction_id)`` for a Binding Request.

    Header layout (RFC 5389 §6):
      0-1  : message type  = 0x0001
      2-3  : message length = 0x0000  (no attributes)
      4-7  : magic cookie  = 0x2112A442
      8-19 : transaction ID (12 random bytes)
    """
    transaction_id = secrets.token_bytes(12)
    header = struct.pack(">HHI", _BINDING_REQUEST, 0x0000, MAGIC_COOKIE)
    return header + transaction_id, transaction_id


def _parse_xor_mapped_address(data: bytes) -> tuple[str, int] | None:
    """Parse a raw XOR-MAPPED-ADDRESS attribute body.

    ``data`` is the attribute value (after the 4-byte type+length header).
    Returns ``(ip, port)`` or ``None`` if the attribute cannot be decoded.
    """
    if len(data) < 8:
        return None
    family = data[1]
    if family != _FAMILY_IPV4:
        return None
    # Port: XOR with high 16 bits of magic cookie
    (xor_port,) = struct.unpack(">H", data[2:4])
    port = xor_port ^ (MAGIC_COOKIE >> 16)
    # IP: XOR with full 32-bit magic cookie
    (xor_ip,) = struct.unpack(">I", data[4:8])
    ip_int = xor_ip ^ MAGIC_COOKIE
    ip = socket.inet_ntoa(struct.pack(">I", ip_int))
    return ip, port


def _parse_response(data: bytes) -> tuple[str, int]:
    """Parse a STUN response and return ``(public_ip, public_port)``.

    Raises ``STUNError`` if the response is malformed or lacks a
    XOR-MAPPED-ADDRESS attribute.
    """
    if len(data) < 20:
        raise STUNError(f"STUN response too short ({len(data)} bytes)")

    msg_type, msg_len, magic = struct.unpack(">HHI", data[:8])

    if magic != MAGIC_COOKIE:
        raise STUNError("STUN magic cookie mismatch in response")
    if msg_type != _BINDING_RESPONSE:
        raise STUNError(
            f"Unexpected STUN response type: {msg_type:#06x} "
            f"(expected {_BINDING_RESPONSE:#06x})"
        )

    # Walk attributes starting at byte 20
    offset = 20
    end = min(20 + msg_len, len(data))
    while offset + 4 <= end:
        attr_type, attr_len = struct.unpack(">HH", data[offset : offset + 4])
        offset += 4
        attr_data = data[offset : offset + attr_len]
        # Advance past attribute value, padded to 4-byte boundary
        offset += attr_len
        if attr_len % 4:
            offset += 4 - (attr_len % 4)

        if attr_type == _XOR_MAPPED_ADDRESS:
            result = _parse_xor_mapped_address(attr_data)
            if result is not None:
                return result

    raise STUNError("No XOR-MAPPED-ADDRESS attribute found in STUN response")


async def get_public_address(local_port: int = 0) -> tuple[str, int]:
    """Return ``(public_ip, public_port)`` via a STUN Binding Request.

    Parameters
    ----------
    local_port:
        The local UDP port to bind (0 lets the OS pick).  When called after
        ``transport.start_host()``, pass the TCP listen port so the STUN
        response reflects the same NAT mapping the TCP server uses — this
        is only a hint; most NATs will use a different port for UDP anyway.

    Raises
    ------
    STUNError
        On DNS failure, timeout (3 s), or an unexpected STUN response.
    """
    loop = asyncio.get_event_loop()

    # Resolve STUN server to an IP address
    try:
        infos = await loop.getaddrinfo(
            STUN_HOST, STUN_PORT,
            family=socket.AF_INET,
            type=socket.SOCK_DGRAM,
        )
    except OSError as exc:
        raise STUNError(f"DNS resolution for {STUN_HOST} failed: {exc}") from exc

    if not infos:
        raise STUNError(f"No IPv4 address found for {STUN_HOST}")

    stun_addr = infos[0][4]  # (ip, port)

    request, _txid = _build_request()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        if local_port:
            try:
                sock.bind(("", local_port))
            except OSError:
                # Port already in use by the TCP server — bind to any port
                pass

        await loop.sock_sendto(sock, request, stun_addr)

        try:
            response = await asyncio.wait_for(
                loop.sock_recv(sock, 1024),
                timeout=3.0,
            )
        except asyncio.TimeoutError:
            raise STUNError("STUN request timed out (3 s)")

        return _parse_response(response)

    finally:
        sock.close()
