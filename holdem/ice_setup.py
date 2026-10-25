"""Bounded authenticated setup records for automatic direct introductions.

This host adapter has no game authority and never accepts an invitation secret
from the service. Signatures and AEAD use the same established cryptography
primitives as the engineering probe, with separate product domains/context.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import time

from .p2p.signing import verify as verify_signature


MAX_JSON_BYTES = 256 * 1024
MAX_SDP_BYTES = 128 * 1024
MAX_DEPTH = 8
MAX_MEMBERS = 256
HOST_PURPOSE = "poker-direct-room-v1"
GUEST_PURPOSE = "poker-direct-guest-v1"
SETUP_PURPOSE = "poker-direct-setup-v1"
CAPABILITY_DOMAIN = b"poker-direct-capability-v1\x00"
HOST_FIELDS = frozenset({"v", "purpose", "service", "host_public", "discovery_token",
                         "generation", "setup_public", "challenge", "created_at", "expires_at", "signature"})
GUEST_FIELDS = frozenset({"v", "purpose", "service", "host_public", "discovery_token",
                          "generation", "guest_public", "setup_public", "guest_nonce", "challenge",
                          "created_at", "expires_at", "capability_proof", "signature"})
CONTEXT_FIELDS = frozenset({"v", "service", "host_public", "host_setup", "discovery_token", "generation",
                            "guest_public", "guest_setup", "guest_nonce", "expires_at"})
ENVELOPE_FIELDS = frozenset({"v", "purpose", "context", "kind", "sender_signing_public",
                             "recipient_encryption_public", "ephemeral_public", "nonce", "ciphertext", "signature"})


class SetupError(ValueError):
    """Fixed, safe setup diagnosis without private records or descriptions."""


def _tree(value):
    members = 0
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise SetupError("setup JSON is too deep")
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise SetupError("setup JSON keys must be strings")
            members += len(item)
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            members += len(item)
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is float:
            if not math.isfinite(item):
                raise SetupError("non-finite setup JSON number")
        elif type(item) not in (str, int, bool, type(None)):
            raise SetupError("unsupported setup JSON value")
        if members > MAX_MEMBERS:
            raise SetupError("too many setup JSON members")


def canonical(value: dict) -> bytes:
    if type(value) is not dict:
        raise SetupError("setup JSON object required")
    _tree(value)
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    except (ValueError, TypeError, RecursionError) as exc:
        raise SetupError("invalid setup JSON") from None
    if len(raw) > MAX_JSON_BYTES:
        raise SetupError("invalid setup JSON size")
    return raw


def load_json(raw: bytes) -> dict:
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_JSON_BYTES:
        raise SetupError("invalid setup JSON size")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SetupError("duplicate setup JSON field")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(SetupError("non-finite setup JSON number")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SetupError("invalid setup JSON") from None
    if type(value) is not dict:
        raise SetupError("setup JSON object required")
    _tree(value)
    return value


def _fields(value, expected):
    if type(value) is not dict or set(value) != expected:
        raise SetupError("unexpected setup fields")
    canonical(value)


def _hex(value, size):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{%d}" % size, value) is None:
        raise SetupError("invalid setup hexadecimal field")


def _service(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", value) is None:
        raise SetupError("invalid setup service identifier")


def _now(now):
    instant = int(time.time()) if now is None else now
    if type(instant) is not int:
        raise SetupError("invalid setup time")
    return instant


def _lifetime(record, ttl, now):
    created, expires = record["created_at"], record["expires_at"]
    instant = _now(now)
    if (type(created) is not int or type(expires) is not int or
            not 0 < created < expires < 2**63 or not expires - created <= ttl or
            created > instant + 30 or expires <= instant):
        raise SetupError("setup record expired or not yet valid")


def sign_record(record: dict, identity) -> dict:
    if type(record) is not dict:
        raise SetupError("setup record required")
    unsigned = {key: value for key, value in record.items() if key != "signature"}
    signature = identity.sign(canonical(unsigned))
    if type(signature) is not bytes or len(signature) != 64:
        raise SetupError("invalid setup signer")
    result = {**unsigned, "signature": signature.hex()}
    canonical(result)
    return result


def _verify(record, public):
    _hex(public, 64)
    _hex(record["signature"], 128)
    unsigned = {key: value for key, value in record.items() if key != "signature"}
    if not verify_signature(bytes.fromhex(public), canonical(unsigned), bytes.fromhex(record["signature"])):
        raise SetupError("invalid setup signature")


def _route(record, service):
    if type(record["v"]) is not int or record["v"] != 1:
        raise SetupError("unsupported setup record version")
    _service(service)
    if record["service"] != service:
        raise SetupError("setup service mismatch")
    for name, size in (("host_public", 64), ("discovery_token", 16), ("generation", 32),
                       ("setup_public", 64), ("challenge", 64)):
        _hex(record[name], size)


def verify_host_record(record, *, service, now=None):
    _fields(record, HOST_FIELDS)
    _route(record, service)
    if record["purpose"] != HOST_PURPOSE:
        raise SetupError("invalid host setup purpose")
    _lifetime(record, 1800, now)
    _verify(record, record["host_public"])
    return record


def verify_guest_record(record, *, service, now=None):
    _fields(record, GUEST_FIELDS)
    _route(record, service)
    if record["purpose"] != GUEST_PURPOSE:
        raise SetupError("invalid guest setup purpose")
    for name in ("guest_public", "guest_nonce", "capability_proof"):
        _hex(record[name], 64)
    if record["guest_public"] == record["host_public"]:
        raise SetupError("guest must have a different identity")
    _lifetime(record, 90, now)
    _verify(record, record["guest_public"])
    return record


def capability_proof(secret, guest_record, host_record):
    if type(secret) is not bytes or len(secret) != 16:
        raise SetupError("invalid private invitation capability")
    if type(guest_record) is not dict or type(host_record) is not dict:
        raise SetupError("setup records required")
    unsigned = {key: value for key, value in guest_record.items() if key not in ("signature", "capability_proof")}
    transcript = (CAPABILITY_DOMAIN + canonical(unsigned) + b"\x00" +
                  hashlib.sha256(canonical(host_record)).digest())
    return hmac.new(secret, transcript, hashlib.sha256).hexdigest()


def setup_context(host_record, guest_record, *, now=None):
    if type(host_record) is not dict or "service" not in host_record:
        raise SetupError("host setup record required")
    service = host_record["service"]
    verify_host_record(host_record, service=service, now=now)
    verify_guest_record(guest_record, service=service, now=now)
    for field in ("host_public", "discovery_token", "generation"):
        if host_record[field] != guest_record[field]:
            raise SetupError("setup room generation mismatch")
    if guest_record["expires_at"] > host_record["expires_at"]:
        raise SetupError("guest setup outlives its room")
    return {"v": 1, "service": service, "host_public": host_record["host_public"],
            "host_setup": host_record["setup_public"], "discovery_token": host_record["discovery_token"],
            "generation": host_record["generation"], "guest_public": guest_record["guest_public"],
            "guest_setup": guest_record["setup_public"], "guest_nonce": guest_record["guest_nonce"],
            "expires_at": guest_record["expires_at"]}


def validate_context(context, *, now=None):
    _fields(context, CONTEXT_FIELDS)
    if type(context["v"]) is not int or context["v"] != 1:
        raise SetupError("unsupported setup context version")
    _service(context["service"])
    for name, size in (("host_public", 64), ("host_setup", 64), ("discovery_token", 16),
                       ("generation", 32), ("guest_public", 64), ("guest_setup", 64), ("guest_nonce", 64)):
        _hex(context[name], size)
    if context["host_public"] == context["guest_public"]:
        raise SetupError("setup identities must differ")
    expires = context["expires_at"]
    if type(expires) is not int or not _now(now) < expires <= _now(now) + 120:
        raise SetupError("setup context expired or has invalid lifetime")
    return context


def _description(value):
    _fields(value, frozenset({"type", "sdp"}))
    if (value["type"] not in ("offer", "answer") or type(value["sdp"]) is not str or
            not value["sdp"].isascii() or not 1 <= len(value["sdp"]) <= MAX_SDP_BYTES):
        raise SetupError("invalid bounded setup description")


def _public_x(key):
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _derive_key(shared, context):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=bytes.fromhex(context["guest_nonce"]),
                info=SETUP_PURPOSE.encode("ascii") + b"\x00" + canonical(context)).derive(shared)


def seal_description(description, context, identity, recipient: str):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    validate_context(context)
    _description(description)
    _hex(recipient, 64)
    sender = identity.public_key_bytes().hex()
    expected_sender = context["host_public"] if description["type"] == "offer" else context["guest_public"]
    expected_recipient = context["guest_setup"] if description["type"] == "offer" else context["host_setup"]
    if sender != expected_sender or recipient != expected_recipient:
        raise SetupError("setup sender or recipient differs from its pinned context")
    ephemeral = X25519PrivateKey.generate()
    metadata = {"v": 1, "purpose": SETUP_PURPOSE, "context": context, "kind": description["type"],
                "sender_signing_public": sender, "recipient_encryption_public": recipient,
                "ephemeral_public": _public_x(ephemeral), "nonce": os.urandom(12).hex()}
    try:
        shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(recipient)))
        encrypted = AESGCM(_derive_key(shared, context)).encrypt(
            bytes.fromhex(metadata["nonce"]), canonical(description), canonical(metadata))
    except ValueError:
        raise SetupError("setup encryption failed") from None
    return sign_record({**metadata, "ciphertext": base64.b64encode(encrypted).decode("ascii")}, identity)


def _ciphertext(value):
    text = value["ciphertext"]
    if type(text) is not str or not 16 <= len(text) <= 200000:
        raise SetupError("invalid setup ciphertext size")
    try:
        raw = base64.b64decode(text, validate=True)
    except ValueError:
        raise SetupError("invalid setup ciphertext") from None
    if len(raw) < 16 or base64.b64encode(raw).decode("ascii") != text:
        raise SetupError("noncanonical setup ciphertext")
    return raw


def validate_envelope(value, context, *, kind, signer=None, recipient=None, now=None):
    validate_context(context, now=now)
    _fields(value, ENVELOPE_FIELDS)
    if (kind not in ("offer", "answer") or type(value["v"]) is not int or value["v"] != 1 or
            value["purpose"] != SETUP_PURPOSE or value["context"] != context or value["kind"] != kind):
        raise SetupError("setup envelope context mismatch")
    expected_sender = context["host_public"] if kind == "offer" else context["guest_public"]
    expected_recipient = context["guest_setup"] if kind == "offer" else context["host_setup"]
    for name in ("sender_signing_public", "recipient_encryption_public", "ephemeral_public"):
        _hex(value[name], 64)
    _hex(value["nonce"], 24)
    if (value["sender_signing_public"] != expected_sender or
            value["recipient_encryption_public"] != expected_recipient or
            (signer is not None and signer != expected_sender) or
            (recipient is not None and recipient != expected_recipient)):
        raise SetupError("setup envelope identity mismatch")
    _ciphertext(value)
    _verify(value, expected_sender)
    return value


validate_setup_envelope = validate_envelope


def open_description(envelope, context, identity, *, kind, signer: str, now=None):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    validate_envelope(envelope, context, kind=kind, signer=signer,
                      recipient=identity.x25519_public_key_bytes().hex(), now=now)
    metadata = {key: item for key, item in envelope.items() if key not in ("signature", "ciphertext")}
    try:
        shared = identity.x25519_private_key().exchange(
            X25519PublicKey.from_public_bytes(bytes.fromhex(envelope["ephemeral_public"])))
        raw = AESGCM(_derive_key(shared, context)).decrypt(
            bytes.fromhex(envelope["nonce"]), _ciphertext(envelope), canonical(metadata))
    except (ValueError, InvalidTag):
        raise SetupError("setup decryption failed") from None
    description = load_json(raw)
    _description(description)
    if description["type"] != kind:
        raise SetupError("setup description role mismatch")
    return description
