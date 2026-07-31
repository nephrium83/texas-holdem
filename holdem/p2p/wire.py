"""
Signed wire format for multiplayer actions.

Every game event travels as a signed envelope conforming to the Phase 1
spec in docs/MULTIPLAYER.md.  The envelope is JSON with these fields:

  v        -- envelope version (int, always 1)
  type     -- action type string (e.g. "player_info", "game_start", "action")
  payload  -- action-specific dict
  pubkey   -- sender Ed25519 public key, 64-char hex
  ts       -- Unix timestamp in milliseconds
  prev     -- SHA-256 hex of previous chain entry (or "0"*64 for genesis)
  sig      -- Ed25519 signature over canonical pre-image, 128-char hex
  hash     -- SHA-256 of the full envelope (for chain linkage)

Canonical pre-image for signing: the envelope dict WITHOUT the "sig" and
"hash" keys, sorted keys, compact separators, UTF-8.
"""
from __future__ import annotations

import hashlib
import json
import time

from holdem.p2p import identity


# Bounds applied to untrusted bytes BEFORE they are parsed. A frame that
# passes the transport's length check is still attacker-controlled, and
# json.loads is where a small message can cost the receiver far more than
# it cost the sender.
MAX_JSON_BYTES = 1 << 19        # 512 KiB; the largest real message is a
                                # ~9 KB shuffle proof, so this is generous
MAX_JSON_DEPTH = 64             # the protocol's deepest payload nests ~6

# Envelope version this build speaks, and the set it will accept. The field
# was previously written as a literal and only checked for presence, so a
# peer announcing any version at all was accepted and its payload
# interpreted under THIS version's rules -- which is exactly the situation
# a version field exists to prevent. Widen SUPPORTED_VERSIONS only when a
# build can genuinely honour the older semantics.
PROTOCOL_VERSION = 1
SUPPORTED_VERSIONS = frozenset({1})


def _check_depth(raw: bytes) -> None:
    """Reject over-nested JSON with a linear scan and no allocation.

    json.loads raises RecursionError on deep nesting -- not JSONDecodeError,
    and not a ValueError -- so it escapes every handler that expects a
    malformed frame. Depth 20k fits in about 40 KB, well inside the frame
    cap, which turned a small hostile message into an uncaught exception.

    Bounding depth ourselves also makes the behaviour independent of
    sys.getrecursionlimit() and of how deep the call stack already is when
    the frame arrives -- otherwise the same payload could be accepted on
    one code path and crash on another.

    Bytes inside string literals are skipped, so a legitimate value like
    "[[[[" cannot trip the bound.
    """
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:          # backslash
                escaped = True
            elif byte == 0x22:          # closing quote
                in_string = False
            continue
        if byte == 0x22:                # opening quote
            in_string = True
        elif byte in (0x5B, 0x7B):      # [ or {
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError(
                    f"JSON nested deeper than {MAX_JSON_DEPTH} levels")
        elif byte in (0x5D, 0x7D):      # ] or }
            depth -= 1


def _reject_constant(name: str):
    """Refuse the NaN / Infinity / -Infinity literals json accepts."""
    raise ValueError(f"JSON contains the non-finite literal {name}")


def _finite_float(text: str) -> float:
    """Parse a JSON number, refusing anything that is not finite.

    ``float("1e999999")`` is ``inf`` with no error, so an overflowing
    exponent is the quiet way to smuggle a non-finite value past a parser.
    """
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"JSON contains the non-finite number {text!r}")
    return value


def safe_loads(raw: bytes):
    """json.loads for untrusted bytes: bounded, and ValueError on anything.

    Every failure mode a hostile peer can reach -- oversized, over-nested,
    malformed, non-UTF-8, or a number that overflows -- surfaces as
    ValueError, which is the one type every caller already treats as
    "drop this peer".
    """
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError(
            f"JSON payload of {len(raw)} bytes exceeds {MAX_JSON_BYTES}")
    _check_depth(raw)
    try:
        # Non-finite numbers are rejected during parsing rather than after:
        # it covers nested values for free, and it costs nothing on the
        # honest path. Python's json accepts NaN/Infinity literals AND
        # silently overflows a long exponent like 1e999999 to inf. Either
        # would survive into a state digest, where json.dumps re-emits
        # "Infinity" -- not valid JSON for any other parser, so two peers
        # could disagree on a hash they both consider well-formed.
        value = json.loads(raw, parse_constant=_reject_constant,
                           parse_float=_finite_float)
    except RecursionError as exc:       # defence in depth behind _check_depth
        raise ValueError("JSON nesting exhausted the parser") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"JSON payload is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    return value


def pack(action_type: str, payload: dict, prev_hash: str = "0" * 64) -> bytes:
    """Build a signed, hash-chained action envelope.

    Returns UTF-8 JSON bytes ready to send over the wire.
    """
    msg: dict = {
        "v":       PROTOCOL_VERSION,
        "type":    action_type,
        "payload": payload,
        "pubkey":  identity.public_key_bytes().hex(),
        "ts":      int(time.time() * 1000),
        "prev":    prev_hash,
    }
    # Sign the canonical pre-image (no "sig", no "hash")
    canonical = json.dumps(msg, sort_keys=True, separators=(",", ":")).encode()
    msg["sig"] = identity.sign(canonical).hex()
    # Hash the full signed envelope for chain linkage
    full = json.dumps(msg, sort_keys=True, separators=(",", ":")).encode()
    msg["hash"] = hashlib.sha256(full).hexdigest()
    return json.dumps(msg).encode()


def unpack(raw: bytes) -> dict:
    """Parse and verify a signed envelope.

    Returns the verified message dict (with "sig" and "hash" restored).
    Raises ValueError if the signature is invalid, required fields are
    missing, the timestamp is outside the ±30-second window (H-6), or the
    declared hash does not match the recomputed one (H-7).
    """
    msg = safe_loads(raw)
    if not isinstance(msg, dict):
        raise ValueError("envelope must be a JSON object")
    for field in ("v", "type", "payload", "pubkey", "ts", "prev", "sig", "hash"):
        if field not in msg:
            raise ValueError("Missing field: %s" % field)

    # Reject an incompatible version before interpreting anything else: the
    # remaining checks all assume this version's envelope semantics.
    # isinstance excludes bool deliberately -- True == 1 in Python, so a
    # bool would otherwise pass as version 1.
    version = msg["v"]
    if isinstance(version, bool) or not isinstance(version, int) \
            or version not in SUPPORTED_VERSIONS:
        raise ValueError(
            f"unsupported protocol version {version!r} "
            f"(this build speaks {sorted(SUPPORTED_VERSIONS)})")

    sig  = bytes.fromhex(msg.pop("sig"))
    h    = msg.pop("hash")

    canonical = json.dumps(msg, sort_keys=True, separators=(",", ":")).encode()
    pubkey = bytes.fromhex(msg["pubkey"])

    if not identity.verify(pubkey, canonical, sig):
        raise ValueError("Invalid signature in envelope from pubkey %s" % msg["pubkey"][:16])

    # H-6: replay protection — reject envelopes outside a ±30-second window
    skew_ms = abs(time.time() * 1000 - msg["ts"])
    if skew_ms > 30_000:
        raise ValueError(
            "Envelope timestamp out of window (skew %.0f ms)" % skew_ms
        )

    # H-7: verify the declared hash matches the recomputed one
    msg["sig"] = sig.hex()
    full = json.dumps(msg, sort_keys=True, separators=(",", ":")).encode()
    expected_hash = hashlib.sha256(full).hexdigest()
    if h != expected_hash:
        raise ValueError(
            "Hash mismatch: declared %s, computed %s" % (h[:16], expected_hash[:16])
        )

    msg["hash"] = h
    return msg
