"""Authenticated, encrypted setup for the optional direct-connection probe.

This is host-side engineering tooling, independent of poker admission and game
identity. Public artifacts contain ephemeral public keys or ciphertext only.
The host pins the guest ready keys from the exact authenticated GitHub run.
Every setup message is signed and bound to that run, source and package hash.
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path


MAX_JSON_BYTES = 256 * 1024
MAX_SDP_BYTES = 128 * 1024
CONTEXT_FIELDS = frozenset({
    "repository", "run_id", "run_attempt", "nonce", "source_commit",
    "package_sha256", "host_signing_public", "host_encryption_public",
})
READY_FIELDS = frozenset({
    "v", "purpose", "context", "guest_signing_public",
    "guest_encryption_public", "signature",
})
ENVELOPE_FIELDS = frozenset({
    "v", "purpose", "context", "kind", "sender_signing_public",
    "recipient_encryption_public", "ephemeral_public", "nonce", "ciphertext",
    "signature",
})
REPORT_FIELDS = frozenset({
    "v", "context", "role", "status", "failure_code", "sent_messages",
    "received_messages", "direct", "local_candidate_type",
    "remote_candidate_type", "protocol", "cleanup",
})
FAILURE_CODES = frozenset({
    "invalid_setup", "mailbox_unavailable", "setup_timeout", "connect_timeout",
    "exchange_failed", "not_direct", "cleanup_failed", "internal_error", "cancelled",
})


class SetupError(ValueError):
    """Setup failed without exposing descriptions, credentials or addresses."""


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SetupError("duplicate JSON field")
        result[key] = value
    return result


def _float(value):
    import math
    number = float(value)
    if not math.isfinite(number):
        raise SetupError("non-finite JSON number")
    return number


def load_json(raw: bytes) -> dict:
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_JSON_BYTES:
        raise SetupError("invalid JSON size")
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_float=_float,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               SetupError("non-finite JSON number")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SetupError("invalid JSON") from exc
    if type(value) is not dict:
        raise SetupError("JSON object required")
    return value


def read_json(path: Path) -> dict:
    with Path(path).open("rb") as source:
        return load_json(source.read(MAX_JSON_BYTES + 1))


def write_json(path: Path, value: dict) -> None:
    """Publish a complete new file atomically; never replace earlier evidence."""
    import os
    import secrets

    path = Path(path)
    raw = canonical(value) + b"\n"
    if len(raw) > MAX_JSON_BYTES or path.exists():
        raise SetupError("output must be a new bounded file")
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
        # Link creates the destination exclusively, unlike replace(). Both
        # files live in the same directory and the same filesystem.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fields(value: dict, expected: frozenset) -> None:
    if type(value) is not dict or set(value) != expected:
        raise SetupError("unexpected fields")


def _hex(value, length: int) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{%d}" % length, value) is None:
        raise SetupError("invalid hexadecimal value")
    return value


def validate_context(context: dict) -> dict:
    _fields(context, CONTEXT_FIELDS)
    if (type(context["repository"]) is not str or
            re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", context["repository"]) is None or
            len(context["repository"]) > 200):
        raise SetupError("invalid repository")
    for field in ("run_id", "run_attempt"):
        if (type(context[field]) is not str or
                re.fullmatch(r"[1-9][0-9]{0,19}", context[field]) is None):
            raise SetupError("invalid workflow identifier")
    for field in ("nonce", "package_sha256", "host_signing_public", "host_encryption_public"):
        _hex(context[field], 64)
    _hex(context["source_commit"], 40)
    return context


def _public(key) -> str:
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


class ProbeKeys:
    """Ephemeral proof keys stored only in an explicitly selected directory."""

    def __init__(self, signing, encryption):
        self.signing = signing
        self.encryption = encryption

    @property
    def signing_public(self) -> str:
        return _public(self.signing)

    @property
    def encryption_public(self) -> str:
        return _public(self.encryption)

    def public(self) -> dict:
        return {"signing_public": self.signing_public,
                "encryption_public": self.encryption_public}

    @classmethod
    def generate(cls, directory: Path):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        keys = cls(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())
        raw = lambda key: key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption()).hex()
        write_json(directory / "probe-keys.json", {
            "v": 1, "signing": raw(keys.signing), "encryption": raw(keys.encryption)})
        return keys

    @classmethod
    def load(cls, directory: Path):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        value = read_json(Path(directory) / "probe-keys.json")
        _fields(value, frozenset({"v", "signing", "encryption"}))
        if type(value["v"]) is not int or value["v"] != 1:
            raise SetupError("unsupported key version")
        return cls(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(_hex(value["signing"], 64))),
                   X25519PrivateKey.from_private_bytes(bytes.fromhex(_hex(value["encryption"], 64))))


def _sign(value: dict, keys: ProbeKeys) -> dict:
    return {**value, "signature": keys.signing.sign(canonical(value)).hex()}


def _verify(value: dict, signer: str) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    signature = bytes.fromhex(_hex(value["signature"], 128))
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(_hex(signer, 64))).verify(
            signature, canonical(unsigned))
    except (InvalidSignature, ValueError) as exc:
        raise SetupError("invalid setup signature") from exc


def make_ready(context: dict, keys: ProbeKeys) -> dict:
    validate_context(context)
    return _sign({"v": 1, "purpose": "poker-ice-ready-v1", "context": context,
                  "guest_signing_public": keys.signing_public,
                  "guest_encryption_public": keys.encryption_public}, keys)


def validate_ready(value: dict, context: dict) -> dict:
    validate_context(context)
    _fields(value, READY_FIELDS)
    if (type(value["v"]) is not int or value["v"] != 1 or
            value["purpose"] != "poker-ice-ready-v1" or value["context"] != context):
        raise SetupError("ready context mismatch")
    _hex(value["guest_signing_public"], 64)
    _hex(value["guest_encryption_public"], 64)
    _verify(value, value["guest_signing_public"])
    return value


def _derive_key(shared: bytes, context: dict) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=bytes.fromhex(context["nonce"]),
                info=b"poker-ice-setup-v1\x00" + canonical(context)).derive(shared)


def seal_description(description: dict, context: dict, keys: ProbeKeys,
                     recipient: str, *, now: int | None = None) -> dict:
    import os
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    validate_context(context)
    _description(description)
    _hex(recipient, 64)
    if description["type"] == "offer" and keys.signing_public != context["host_signing_public"]:
        raise SetupError("offer signer is not the pinned host")
    instant = int(time.time()) if now is None else now
    ephemeral = X25519PrivateKey.generate()
    metadata = {"v": 1, "purpose": "poker-ice-setup-v1", "context": context,
                "kind": description["type"], "sender_signing_public": keys.signing_public,
                "recipient_encryption_public": recipient,
                "ephemeral_public": _public(ephemeral), "nonce": os.urandom(12).hex()}
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(recipient)))
    plaintext = canonical({**description, "created_at": instant, "expires_at": instant + 600})
    encrypted = AESGCM(_derive_key(shared, context)).encrypt(
        bytes.fromhex(metadata["nonce"]), plaintext, canonical(metadata))
    return _sign({**metadata, "ciphertext": base64.b64encode(encrypted).decode("ascii")}, keys)


def validate_envelope(value: dict, context: dict, *, kind: str,
                      signer: str | None = None, recipient: str | None = None) -> dict:
    validate_context(context)
    _fields(value, ENVELOPE_FIELDS)
    if (kind not in ("offer", "answer") or type(value["v"]) is not int or value["v"] != 1 or
            value["purpose"] != "poker-ice-setup-v1" or value["kind"] != kind or
            value["context"] != context):
        raise SetupError("setup context mismatch")
    for name in ("sender_signing_public", "recipient_encryption_public", "ephemeral_public"):
        _hex(value[name], 64)
    _hex(value["nonce"], 24)
    if signer is not None and value["sender_signing_public"] != signer:
        raise SetupError("unexpected setup signer")
    if recipient is not None and value["recipient_encryption_public"] != recipient:
        raise SetupError("unexpected setup recipient")
    if kind == "offer" and value["sender_signing_public"] != context["host_signing_public"]:
        raise SetupError("unexpected host signer")
    if kind == "answer" and value["recipient_encryption_public"] != context["host_encryption_public"]:
        raise SetupError("unexpected host recipient")
    _ciphertext(value)
    _verify(value, value["sender_signing_public"])
    return value


def _ciphertext(value: dict) -> bytes:
    text = value["ciphertext"]
    if type(text) is not str or not 16 <= len(text) <= 200000:
        raise SetupError("invalid ciphertext size")
    try:
        encrypted = base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise SetupError("invalid ciphertext encoding") from exc
    if len(encrypted) < 16 or base64.b64encode(encrypted).decode("ascii") != text:
        raise SetupError("noncanonical ciphertext")
    return encrypted


def _description(value: dict) -> None:
    _fields(value, frozenset({"type", "sdp"}))
    if (value["type"] not in ("offer", "answer") or type(value["sdp"]) is not str or
            not value["sdp"].isascii() or
            not 1 <= len(value["sdp"]) <= MAX_SDP_BYTES):
        raise SetupError("invalid description")


def open_description(value: dict, context: dict, keys: ProbeKeys,
                     *, kind: str, signer: str, now: int | None = None) -> dict:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    validate_envelope(value, context, kind=kind, signer=signer, recipient=keys.encryption_public)
    metadata = {key: item for key, item in value.items() if key not in ("ciphertext", "signature")}
    try:
        shared = keys.encryption.exchange(
            X25519PublicKey.from_public_bytes(bytes.fromhex(value["ephemeral_public"])))
        raw = AESGCM(_derive_key(shared, context)).decrypt(
            bytes.fromhex(value["nonce"]), _ciphertext(value), canonical(metadata))
    except (InvalidTag, ValueError) as exc:
        raise SetupError("setup decryption failed") from exc
    inner = load_json(raw)
    _fields(inner, frozenset({"type", "sdp", "created_at", "expires_at"}))
    instant = int(time.time()) if now is None else now
    created, expires = inner["created_at"], inner["expires_at"]
    if (type(created) is not int or type(expires) is not int or
            not 0 < expires - created <= 600 or created > instant + 30 or expires < instant):
        raise SetupError("setup description expired or not yet valid")
    description = {"type": inner["type"], "sdp": inner["sdp"]}
    _description(description)
    if description["type"] != kind:
        raise SetupError("description role mismatch")
    return description


def validate_report(value: dict, context: dict) -> dict:
    validate_context(context)
    _fields(value, REPORT_FIELDS | ({"poker"} if type(value) is dict and "poker" in value else set()))
    if (type(value["v"]) is not int or value["v"] != 1 or value["context"] != context or
            value["role"] not in ("host", "guest") or
            value["status"] not in ("connected", "failed", "cancelled") or
            (value["failure_code"] is not None and
             (type(value["failure_code"]) is not str or value["failure_code"] not in FAILURE_CODES))):
        raise SetupError("invalid public report")
    if (value["status"] == "connected") != (value["failure_code"] is None):
        # Cancellation has a fixed failure code too, so a partial run cannot
        # accidentally appear as success merely because no exception was raised.
        raise SetupError("inconsistent report outcome")
    for name in ("sent_messages", "received_messages"):
        if type(value[name]) is not int or not 0 <= value[name] <= (10000 if "poker" in value else 20):
            raise SetupError("invalid report count")
    if type(value["direct"]) is not bool or value["protocol"] not in ("udp", None):
        raise SetupError("invalid report route")
    for name in ("local_candidate_type", "remote_candidate_type"):
        if value[name] not in ("host", "srflx", "prflx", None):
            raise SetupError("invalid candidate type")
    cleanup = value["cleanup"]
    _fields(cleanup, frozenset({"peer_closed", "owned_tasks_pending"}))
    if (type(cleanup["peer_closed"]) is not bool or type(cleanup["owned_tasks_pending"]) is not int or
            not 0 <= cleanup["owned_tasks_pending"] <= 20):
        raise SetupError("invalid cleanup report")
    if value["status"] == "connected" and not (
            value["direct"] and value["protocol"] == "udp" and
            value["local_candidate_type"] and value["remote_candidate_type"] and
            value["sent_messages"] >= 3 and value["received_messages"] >= 3 and
            cleanup["peer_closed"] and cleanup["owned_tasks_pending"] == 0):
        raise SetupError("incomplete successful proof")
    if "poker" in value:
        poker = value["poker"]
        _fields(poker, frozenset({"hands", "balances", "proofs_verified", "deal_policy", "matching_balances"}))
        if (type(poker["hands"]) is not int or not 1 <= poker["hands"] <= 10 or
                type(poker["proofs_verified"]) is not int or poker["proofs_verified"] != 2 or
                poker["deal_policy"] != "bayer-groth-v1" or poker["matching_balances"] is not True or
                type(poker["balances"]) is not list or len(poker["balances"]) != poker["hands"]):
            raise SetupError("invalid public poker receipt")
        for vector in poker["balances"]:
            if (type(vector) is not list or len(vector) != 2 or
                    any(type(chips) is not int or not 0 <= chips <= 20000 for chips in vector) or
                    sum(vector) != 20000):
                raise SetupError("invalid settled poker balances")
    return value
