"""Real TLS handshakes cover expired cached cross-signs and rejection paths."""
from datetime import datetime, timedelta, timezone
import socket
import ssl
import threading
import asyncio
import json

import pytest

pytest.importorskip("cryptography")
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from holdem.introduction_client import _active_trust_context


def certificate(name, key, issuer, issuer_key, *, ca, expired=False):
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer or subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=5))
        .not_valid_after(now + timedelta(days=-1 if expired else 30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=ca, crl_sign=ca,
            encipher_only=False, decipher_only=False), critical=True))
    if not ca:
        builder = (builder.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False))
    return builder.sign(issuer_key, hashes.SHA256())


def pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def handshake(tmp_path, context, leaf, intermediate, key, *, hostname="localhost"):
    cert_path, key_path = tmp_path / "server.pem", tmp_path / "server.key"
    cert_path.write_text(pem(leaf) + pem(intermediate), encoding="ascii")
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        def server():
            connection, _ = listener.accept()
            with connection:
                try:
                    with server_context.wrap_socket(connection, server_side=True):
                        pass
                except OSError:
                    pass  # The client deliberately rejects three cases.
        worker = threading.Thread(target=server)
        worker.start()
        try:
            with socket.create_connection(listener.getsockname(), timeout=5) as connection:
                with context.wrap_socket(connection, server_hostname=hostname):
                    pass
        finally:
            worker.join(6)
            assert not worker.is_alive()


@pytest.mark.parametrize("case", ["valid", "expired-leaf", "wrong-host", "untrusted-root"])
def test_valid_server_cross_sign_survives_expired_cache_without_weakening_tls(tmp_path, case):
    keys = [ec.generate_private_key(ec.SECP256R1()) for _ in range(4)]
    root_a = certificate("Root A", keys[0], None, keys[0], ca=True)
    root_b = certificate("Root B", keys[1], None, keys[1], ca=True)
    expired = certificate("Shared CA", keys[2], root_a.subject, keys[0], ca=True, expired=True)
    valid = certificate("Shared CA", keys[2], root_b.subject, keys[1], ca=True)
    leaf = certificate("localhost", keys[3], valid.subject, keys[2], ca=False, expired=case == "expired-leaf")
    trusted = pem(root_a) + pem(expired) + ("" if case == "untrusted-root" else pem(root_b))
    source = ssl.create_default_context(cadata=trusted)
    if case == "valid":
        with pytest.raises(ssl.SSLCertVerificationError) as error:
            handshake(tmp_path, source, leaf, valid, keys[3])
        assert error.value.verify_code == 10  # Actual expired cached chain.
    context = _active_trust_context(source)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.verify_flags == source.verify_flags
    assert context.minimum_version == source.minimum_version
    if case == "valid":
        handshake(tmp_path, context, leaf, valid, keys[3])
    else:
        with pytest.raises(ssl.SSLCertVerificationError):
            handshake(tmp_path, context, leaf, valid, keys[3],
                      hostname="wrong.invalid" if case == "wrong-host" else "localhost")


@pytest.mark.parametrize("offset,accepted", [(2, True), (31, True), (60, True), (61, False), (0, False), (-1, False)])
def test_challenge_allows_bounded_clock_skew_but_rejects_expired_or_excessive_times(monkeypatch, offset, accepted):
    websocket_client = pytest.importorskip("websockets.asyncio.client")
    from holdem import introduction_client as client
    class Socket:
        closed = False
        async def recv(self):
            return json.dumps({"type": "challenge", "service": "test", "challenge": "a" * 64,
                               "expires_at": 1000000 + offset})
        async def close(self):
            self.closed = True
    socket = Socket()
    async def connect(*args, **kwargs):
        return socket
    monkeypatch.setattr(websocket_client, "connect", connect)
    monkeypatch.setattr(client.time, "time", lambda: 1000000)
    async def run():
        if accepted:
            connection = await client.IntroductionConnection.connect("ws://127.0.0.1:1234/v1", "test", allow_loopback=True)
            await connection.aclose()
        else:
            with pytest.raises(client.IntroductionError):
                await client.IntroductionConnection.connect("ws://127.0.0.1:1234/v1", "test", allow_loopback=True)
        assert socket.closed
    asyncio.run(run())
