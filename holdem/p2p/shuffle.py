"""
Verifiable shuffle for multiplayer Texas Hold'em.

Protocol
--------
Phase 1 – Commit
    Each player i generates 32 random seed bytes and a 16-byte nonce.
    commit_i = SHA-256(seed_i || nonce_i)
    Host sends  {"type": "shuffle_start",          "payload": {"commit_hex": ..., "x25519_pubkey_hex": ...}}
    Peers reply {"type": "shuffle_commit",          "payload": {"commit_hex": ..., "x25519_pubkey_hex": ...}}

Phase 2 – Reveal
    Host broadcasts {"type": "shuffle_commit_collect", "payload": {"commits": {conn_id: hex, ...}}}
    All nodes respond with {"type": "shuffle_reveal", "payload": {"seed_hex": ..., "nonce_hex": ...}}
    Host verifies SHA-256(seed || nonce) == commit for every peer.

Phase 3 – Derive & deal
    master_seed = SHA-256(seed_A || seed_B || ... concatenated by *sorted* conn_id)
    Deterministic Fisher-Yates over 52 cards via HMAC-SHA256 counter RNG.
    Host broadcasts {"type": "shuffle_reveal_collect", "payload": {"reveals": {conn_id: {seed,nonce}, ...}}}
    Host sends per-recipient {"type": "shuffle_deal", "payload": {"seat": N, "encrypted_hex": "..."}}
    Each peer decrypts their hole cards with their local X25519 private key.

Security properties
-------------------
* Commit-binding:  no party can change their seed after broadcasting a commit.
* Liveness:        one dishonest peer that refuses to reveal causes the hand to abort.
* Verifiability:   given the reveal_collect broadcast anyone can independently
                   reproduce the master seed and the deck order.
* Card privacy:    hole cards are encrypted with ephemeral X25519 + AES-256-GCM;
                   only the intended recipient can decrypt them.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import os
import struct
from dataclasses import dataclass, field
from typing import Dict, List


# ---------------------------------------------------------------------------
# Commit / reveal helpers
# ---------------------------------------------------------------------------

def make_seed() -> bytes:
    """Return 32 cryptographically-random seed bytes."""
    return os.urandom(32)


def make_nonce() -> bytes:
    """Return 16 cryptographically-random nonce bytes."""
    return os.urandom(16)


def compute_commit(seed: bytes, nonce: bytes) -> bytes:
    """Return SHA-256(seed || nonce) — the binding commitment."""
    return hashlib.sha256(seed + nonce).digest()


def verify_commit(seed: bytes, nonce: bytes, commit: bytes) -> bool:
    """Constant-time check: SHA-256(seed || nonce) == commit."""
    expected = hashlib.sha256(seed + nonce).digest()
    return _hmac.compare_digest(expected, commit)


# ---------------------------------------------------------------------------
# Master seed derivation
# ---------------------------------------------------------------------------

def derive_master_seed(seeds_by_conn_id: Dict[str, bytes]) -> bytes:
    """Combine all verified seeds into one master seed.

    Seeds are concatenated in ascending ``conn_id`` lexicographic order then
    hashed with SHA-256.  All parties independently compute the same value
    once the reveal_collect broadcast arrives.
    """
    if not seeds_by_conn_id:
        raise ValueError("No seeds provided")
    combined = b"".join(seeds_by_conn_id[k] for k in sorted(seeds_by_conn_id))
    return hashlib.sha256(combined).digest()


# ---------------------------------------------------------------------------
# Deterministic Fisher-Yates shuffle
# ---------------------------------------------------------------------------

class _HMACCounterRNG:
    """Deterministic PRNG built from HMAC-SHA256 in counter mode.

    Provides unbiased integers via rejection sampling; outputs are
    reproducible from the same key on any platform.
    """

    def __init__(self, key: bytes) -> None:
        self._key = key
        self._counter = 0
        self._buf = b""

    def _refill(self) -> None:
        ctr_bytes = struct.pack(">Q", self._counter)
        self._buf += _hmac.new(self._key, ctr_bytes, hashlib.sha256).digest()
        self._counter += 1

    def randbelow(self, n: int) -> int:
        """Return a uniform random integer in ``[0, n)``."""
        if n <= 1:
            return 0
        bits = (n - 1).bit_length()
        byte_count = (bits + 7) // 8
        mask = (1 << bits) - 1
        while True:
            while len(self._buf) < byte_count:
                self._refill()
            raw = int.from_bytes(self._buf[:byte_count], "big") & mask
            self._buf = self._buf[byte_count:]
            if raw < n:
                return raw


def deterministic_shuffle(master_seed: bytes) -> List[int]:
    """Return a shuffled list of card indices 0..51 (Fisher-Yates).

    Card index ``i`` corresponds to ``FULL_DECK[i]`` in engine.py order,
    i.e. ``Card(v = i//4 + 2, s = i % 4)``.

    Index 0 in the returned list is the *first* card dealt.
    """
    rng = _HMACCounterRNG(master_seed)
    deck = list(range(52))
    for i in range(51, 0, -1):
        j = rng.randbelow(i + 1)
        deck[i], deck[j] = deck[j], deck[i]
    return deck


# ---------------------------------------------------------------------------
# X25519 + AES-256-GCM hole-card encryption
# ---------------------------------------------------------------------------

def _derive_aes_key(shared_secret: bytes) -> bytes:
    """HKDF-SHA256(shared_secret) → 32-byte AES key."""
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.backends import default_backend
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"holdem-hole-cards",
        backend=default_backend(),
    ).derive(shared_secret)


def encrypt_hole_cards(
    cards_str: List[str],
    recipient_x25519_pubkey_bytes: bytes,
) -> bytes:
    """Encrypt hole cards for one recipient using X25519 + AES-256-GCM.

    Returns ``ephemeral_pubkey (32 B) || iv (12 B) || ciphertext+tag``.
    The ephemeral keypair is freshly generated for every call.
    """
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey,
    )
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    eph_priv = X25519PrivateKey.generate()
    eph_pub_bytes = eph_priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    recip_pub = X25519PublicKey.from_public_bytes(recipient_x25519_pubkey_bytes)
    shared = eph_priv.exchange(recip_pub)

    aes_key = _derive_aes_key(shared)
    plaintext = ",".join(cards_str).encode()
    iv = os.urandom(12)
    ct_and_tag = AESGCM(aes_key).encrypt(iv, plaintext, b"holdem-deal")
    return eph_pub_bytes + iv + ct_and_tag


def decrypt_hole_cards(
    ciphertext_blob: bytes,
    local_x25519_private_key,
) -> List[str]:
    """Decrypt hole cards using the local X25519 private key.

    Returns a list of card strings, e.g. ``['Ac', '2h']``.
    Raises ``ValueError`` on authentication failure or malformed input.
    """
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(ciphertext_blob) < 32 + 12 + 16:
        raise ValueError(
            f"Ciphertext blob too short: {len(ciphertext_blob)} bytes"
        )
    eph_pub_bytes = ciphertext_blob[:32]
    iv = ciphertext_blob[32:44]
    ct_and_tag = ciphertext_blob[44:]

    eph_pub = X25519PublicKey.from_public_bytes(eph_pub_bytes)
    shared = local_x25519_private_key.exchange(eph_pub)
    aes_key = _derive_aes_key(shared)
    try:
        plaintext = AESGCM(aes_key).decrypt(iv, ct_and_tag, b"holdem-deal")
    except Exception as exc:
        raise ValueError(f"Hole-card decryption failed: {exc}") from exc
    return plaintext.decode().split(",")


# ---------------------------------------------------------------------------
# ShuffleRound — per-hand protocol state machine
# ---------------------------------------------------------------------------

@dataclass
class ShuffleRound:
    """State for one commit-reveal shuffle round across all seats.

    ``local_conn_id`` is this node's own identifier.
    ``all_conn_ids`` lists every seat (including local), in the order they
    appear in the session's _seat_order.

    Typical host workflow::

        sr = ShuffleRound(local_conn_id="host-id",
                          all_conn_ids=["host-id", "peer-a", "peer-b"])
        commit = sr.local_commit()   # generate & store local seed
        # ...broadcast commit to peers, wait for their shuffle_commits...
        sr.record_commit("peer-a", commit_bytes_a)
        sr.record_commit("peer-b", commit_bytes_b)
        assert sr.all_commits_received()
        # ...broadcast commit_collect, wait for reveals...
        sr.record_reveal("peer-a", seed_a, nonce_a)   # raises on mismatch
        sr.record_reveal("peer-b", seed_b, nonce_b)
        assert sr.all_reveals_received()
        deck = sr.shuffled_deck()   # List[int] length 52
    """

    local_conn_id: str
    all_conn_ids: List[str]

    _seed:    bytes = field(default_factory=make_seed, init=False, repr=False)
    _nonce:   bytes = field(default_factory=make_nonce, init=False, repr=False)
    _commits: Dict[str, bytes] = field(default_factory=dict, init=False)
    _seeds:   Dict[str, bytes] = field(default_factory=dict, init=False)
    _nonces:  Dict[str, bytes] = field(default_factory=dict, init=False)

    # X25519 pubkeys collected from peers (conn_id → 32-byte pubkey)
    x25519_pubkeys: Dict[str, bytes] = field(default_factory=dict, init=False)

    def local_commit(self) -> bytes:
        """Generate and store the local commit; return the 32-byte value."""
        commit = compute_commit(self._seed, self._nonce)
        self._commits[self.local_conn_id] = commit
        self._seeds[self.local_conn_id] = self._seed
        self._nonces[self.local_conn_id] = self._nonce   # H-1: retain nonce
        return commit

    @property
    def local_seed_hex(self) -> str:
        return self._seed.hex()

    @property
    def local_nonce_hex(self) -> str:
        return self._nonce.hex()

    def record_commit(self, conn_id: str, commit: bytes) -> None:
        """Record a commitment received from another peer."""
        self._commits[conn_id] = commit

    def record_x25519_pubkey(self, conn_id: str, pubkey_bytes: bytes) -> None:
        """Record the X25519 encryption pubkey for a peer."""
        self.x25519_pubkeys[conn_id] = pubkey_bytes

    def all_commits_received(self) -> bool:
        """True when every seat has sent a commitment."""
        return all(c in self._commits for c in self.all_conn_ids)

    def record_reveal(self, conn_id: str, seed: bytes, nonce: bytes) -> None:
        """Verify and record a peer's reveal.

        Raises ``ValueError`` if the seed+nonce doesn't match the stored commit.
        """
        commit = self._commits.get(conn_id)
        if commit is None:
            raise ValueError(
                f"record_reveal: no prior commit from {conn_id!r}"
            )
        if not verify_commit(seed, nonce, commit):
            raise ValueError(
                f"Commit mismatch for {conn_id!r}: "
                "SHA256(seed||nonce) ≠ committed value — possible cheating"
            )
        self._seeds[conn_id] = seed
        self._nonces[conn_id] = nonce   # H-1: retain nonce for verifiable broadcast

    def all_reveals_received(self) -> bool:
        """True when every seat's seed has been verified and stored."""
        return all(c in self._seeds for c in self.all_conn_ids)

    def master_seed(self) -> bytes:
        """Derive the master seed from all verified peer seeds."""
        if not self.all_reveals_received():
            raise RuntimeError("Not all reveals received yet")
        return derive_master_seed(self._seeds)

    def shuffled_deck(self) -> List[int]:
        """Return the deterministically shuffled deck as 52 card indices."""
        return deterministic_shuffle(self.master_seed())

    def reveals_snapshot(self) -> Dict[str, Dict[str, str]]:
        """Serialisable snapshot of all reveals for broadcast/audit."""
        return {
            cid: {
                "seed_hex":  self._seeds[cid].hex(),
                "nonce_hex": self._nonces[cid].hex(),   # H-1: real nonce, so
                                                        # SHA256(seed||nonce)==commit
            }
            for cid in self.all_conn_ids
            if cid in self._seeds and cid in self._nonces
        }
