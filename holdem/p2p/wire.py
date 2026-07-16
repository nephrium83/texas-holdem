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


def pack(action_type: str, payload: dict, prev_hash: str = "0" * 64) -> bytes:
    """Build a signed, hash-chained action envelope.

    Returns UTF-8 JSON bytes ready to send over the wire.
    """
    msg: dict = {
        "v":       1,
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
    Raises ValueError if the signature is invalid or required fields are
    missing.
    """
    msg = json.loads(raw)
    for field in ("v", "type", "payload", "pubkey", "ts", "prev", "sig", "hash"):
        if field not in msg:
            raise ValueError("Missing field: %s" % field)

    sig  = bytes.fromhex(msg.pop("sig"))
    h    = msg.pop("hash")

    canonical = json.dumps(msg, sort_keys=True, separators=(",", ":")).encode()
    pubkey = bytes.fromhex(msg["pubkey"])

    if not identity.verify(pubkey, canonical, sig):
        raise ValueError("Invalid signature in envelope from pubkey %s" % msg["pubkey"][:16])

    msg["sig"]  = sig.hex()
    msg["hash"] = h
    return msg
