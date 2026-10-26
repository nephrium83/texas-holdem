"""Bounded setup-only WebSocket connection to a host-configured service.

An invite selects a trusted service identifier, never an arbitrary URL. The
service sees public routing and encrypted setup; gameplay uses DirectPeer.
"""
from __future__ import annotations

import asyncio
import ipaddress
import ssl
import sys
import time
from urllib.parse import urlsplit


class IntroductionError(ConnectionError):
    pass


def _active_trust_context(source):
    """Exclude expired local anchors that shadow valid server cross-signs.

    Windows' CA store can retain an expired cross-signed intermediate. OpenSSL
    prefers that cached copy over the valid chain supplied by the server.
    Keep only currently valid certificates already trusted by the default
    context; do not add trust, change hostname checks or relax verification.
    """
    from datetime import datetime, timezone
    import warnings
    from cryptography import x509
    from cryptography.utils import CryptographyDeprecationWarning

    if source.verify_mode != ssl.CERT_REQUIRED or not source.check_hostname:
        raise ValueError("Introduction TLS requires certificate and hostname verification")
    now = datetime.now(timezone.utc)
    active = []
    for raw in source.get_ca_certs(binary_form=True):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", CryptographyDeprecationWarning)
                certificate = x509.load_der_x509_certificate(raw)
            before = (certificate.not_valid_before_utc if hasattr(certificate, "not_valid_before_utc")
                      else certificate.not_valid_before.replace(tzinfo=timezone.utc))
            after = (certificate.not_valid_after_utc if hasattr(certificate, "not_valid_after_utc")
                     else certificate.not_valid_after.replace(tzinfo=timezone.utc))
            if before <= now <= after:
                active.append(ssl.DER_cert_to_PEM_cert(raw))
        except (ValueError, CryptographyDeprecationWarning):
            continue  # Malformed local certificates cannot establish trust.
    if not active:
        raise IntroductionError("No valid trusted certificate authorities are available")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_flags = source.verify_flags
    context.minimum_version = source.minimum_version
    context.maximum_version = source.maximum_version
    context.options = source.options
    context.load_verify_locations(cadata="".join(active))
    return context


def secure_context():
    source = ssl.create_default_context()
    return _active_trust_context(source) if sys.platform == "win32" else source


def validate_endpoint(endpoint: str, *, allow_loopback=False) -> str:
    if type(endpoint) is not str or len(endpoint) > 2048:
        raise ValueError("The introduction service is not configured")
    try:
        parsed = urlsplit(endpoint)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment or
                parsed.query or parsed.path != "/v1" or parsed.port == 0):
            raise ValueError()
        if parsed.scheme == "wss":
            return endpoint
        if parsed.scheme == "ws" and allow_loopback and ipaddress.ip_address(parsed.hostname).is_loopback:
            return endpoint
    except (ValueError, TypeError):
        pass
    raise ValueError("The introduction service requires a trusted secure address")


class IntroductionConnection:
    def __init__(self, websocket, service, challenge):
        self.socket, self.service, self.challenge = websocket, service, challenge
        self._send_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def connect(cls, endpoint, service, *, allow_loopback=False):
        from websockets.asyncio.client import connect
        from .ice_setup import load_json
        endpoint = validate_endpoint(endpoint, allow_loopback=allow_loopback)
        options = dict(compression=None, max_size=256 * 1024, max_queue=4,
                       write_limit=32768, open_timeout=10, close_timeout=2,
                       ping_interval=20, ping_timeout=20, proxy=None)
        if endpoint.startswith("wss:"):
            options["ssl"] = secure_context()
        websocket = await connect(endpoint, **options)
        try:
            raw = await asyncio.wait_for(websocket.recv(), 10)
            challenge = load_json(raw.encode("utf-8") if type(raw) is str else raw)
            now = int(time.time())
            if (set(challenge) != {"type", "service", "challenge", "expires_at"} or
                    challenge["type"] != "challenge" or challenge["service"] != service or
                    type(challenge["challenge"]) is not str or len(challenge["challenge"]) != 64 or
                    any(c not in "0123456789abcdef" for c in challenge["challenge"]) or
                    # The service owns its actual 30-second timeout. Accept
                    # the same bounded 30-second future clock skew used by
                    # signed setup records, rather than requiring equal clocks.
                    type(challenge["expires_at"]) is not int or not now < challenge["expires_at"] <= now + 60):
                raise IntroductionError("The introduction service returned an invalid challenge")
            return cls(websocket, service, challenge["challenge"])
        except BaseException:
            await websocket.close()
            raise

    async def send(self, message):
        from .ice_setup import canonical, load_json
        raw = canonical(message)
        load_json(raw)  # apply the shared depth/member/size rules before sending
        async with self._send_lock:
            if self._closed:
                raise IntroductionError("The introduction connection closed")
            await asyncio.wait_for(self.socket.send(raw.decode("ascii")), 5)

    async def receive(self, timeout=35):
        from .ice_setup import load_json
        if self._closed:
            raise IntroductionError("The introduction connection closed")
        raw = await asyncio.wait_for(self.socket.recv(), timeout)
        message = load_json(raw.encode("utf-8") if type(raw) is str else raw)
        if message.get("type") == "error":
            raise IntroductionError("The introduction service could not complete this request")
        return message

    async def aclose(self):
        self._closed = True
        await self.socket.close()
