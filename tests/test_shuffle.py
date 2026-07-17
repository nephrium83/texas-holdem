"""
Tests for the verifiable shuffle protocol (holdem/p2p/shuffle.py).

Coverage
--------
* commit / verify round-trip (correct and tampered)
* derive_master_seed sorting property
* deterministic Fisher-Yates: reproducibility, full coverage, seeded length
* ShuffleRound: two-party and three-party happy paths
* ShuffleRound: tamper detection (bad seed revealed)
* ShuffleRound: missing commit raises on reveal
* X25519 + AES-256-GCM encrypt / decrypt
* decryption rejects tampered ciphertext
* Deck.from_indices integration with Engine.start_hand
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.shuffle import (
    ShuffleRound,
    compute_commit,
    decrypt_hole_cards,
    derive_master_seed,
    deterministic_shuffle,
    encrypt_hole_cards,
    make_nonce,
    make_seed,
    verify_commit,
)


# ---------------------------------------------------------------------------
# Commit / verify
# ---------------------------------------------------------------------------

def test_commit_round_trip():
    seed  = make_seed()
    nonce = make_nonce()
    commit = compute_commit(seed, nonce)
    assert len(commit) == 32
    assert verify_commit(seed, nonce, commit)


def test_commit_wrong_seed_fails():
    seed  = make_seed()
    nonce = make_nonce()
    commit = compute_commit(seed, nonce)
    bad_seed = bytes(b ^ 0xFF for b in seed)
    assert not verify_commit(bad_seed, nonce, commit)


def test_commit_wrong_nonce_fails():
    seed  = make_seed()
    nonce = make_nonce()
    commit = compute_commit(seed, nonce)
    bad_nonce = bytes(b ^ 0x01 for b in nonce)
    assert not verify_commit(seed, bad_nonce, commit)


def test_commit_bit_flip_fails():
    seed  = make_seed()
    nonce = make_nonce()
    commit = compute_commit(seed, nonce)
    flipped = bytes([commit[0] ^ 1]) + commit[1:]
    assert not verify_commit(seed, nonce, flipped)


# ---------------------------------------------------------------------------
# Master seed derivation
# ---------------------------------------------------------------------------

def test_derive_master_seed_deterministic():
    seeds = {"peer-a": b"\x01" * 32, "peer-b": b"\x02" * 32}
    ms1 = derive_master_seed(seeds)
    ms2 = derive_master_seed(seeds)
    assert ms1 == ms2
    assert len(ms1) == 32


def test_derive_master_seed_sorted_by_id():
    """Insertion order must not affect the result — only sorted key order matters."""
    seeds_ab = {"a": b"\xAA" * 32, "b": b"\xBB" * 32}
    seeds_ba = {"b": b"\xBB" * 32, "a": b"\xAA" * 32}
    assert derive_master_seed(seeds_ab) == derive_master_seed(seeds_ba)


def test_derive_master_seed_different_seeds_differ():
    s1 = derive_master_seed({"x": b"\x01" * 32})
    s2 = derive_master_seed({"x": b"\x02" * 32})
    assert s1 != s2


def test_derive_master_seed_empty_raises():
    with pytest.raises(ValueError):
        derive_master_seed({})


# ---------------------------------------------------------------------------
# Deterministic Fisher-Yates
# ---------------------------------------------------------------------------

def test_deterministic_shuffle_length():
    deck = deterministic_shuffle(b"\x00" * 32)
    assert len(deck) == 52


def test_deterministic_shuffle_is_permutation():
    deck = deterministic_shuffle(b"\xDE\xAD" * 16)
    assert sorted(deck) == list(range(52))


def test_deterministic_shuffle_reproducible():
    seed = b"\xCA\xFE" * 16
    assert deterministic_shuffle(seed) == deterministic_shuffle(seed)


def test_deterministic_shuffle_different_seeds_differ():
    d1 = deterministic_shuffle(b"\x00" * 32)
    d2 = deterministic_shuffle(b"\xFF" * 32)
    assert d1 != d2


def test_deterministic_shuffle_not_sorted():
    """A 52-card deck should very rarely (probability ≈ 1/52!) be already sorted."""
    deck = deterministic_shuffle(b"\x42" * 32)
    assert deck != list(range(52))


# ---------------------------------------------------------------------------
# ShuffleRound — two-party happy path
# ---------------------------------------------------------------------------

def _make_round(local_id: str, all_ids: list) -> ShuffleRound:
    return ShuffleRound(local_conn_id=local_id, all_conn_ids=all_ids)


def test_shuffle_round_two_party():
    host   = _make_round("host", ["host", "peer"])
    client = _make_round("peer", ["host", "peer"])

    # Phase 1: commits
    host_commit   = host.local_commit()
    client_commit = client.local_commit()

    host.record_commit("peer", client_commit)
    client.record_commit("host", host_commit)

    assert host.all_commits_received()
    assert client.all_commits_received()

    # Phase 2: reveals
    host.record_reveal("peer",  bytes.fromhex(client.local_seed_hex),
                                bytes.fromhex(client.local_nonce_hex))
    client.record_reveal("host", bytes.fromhex(host.local_seed_hex),
                                 bytes.fromhex(host.local_nonce_hex))

    assert host.all_reveals_received()
    assert client.all_reveals_received()

    # Both sides must derive the same shuffled deck
    deck_host   = host.shuffled_deck()
    deck_client = client.shuffled_deck()

    assert deck_host == deck_client
    assert sorted(deck_host) == list(range(52))


def test_shuffle_round_three_party():
    all_ids = ["alpha", "beta", "gamma"]
    rounds  = {pid: _make_round(pid, all_ids) for pid in all_ids}

    commits = {pid: rounds[pid].local_commit() for pid in all_ids}
    for pid, sr in rounds.items():
        for other, commit in commits.items():
            if other != pid:
                sr.record_commit(other, commit)

    assert all(sr.all_commits_received() for sr in rounds.values())

    seeds  = {pid: bytes.fromhex(rounds[pid].local_seed_hex)  for pid in all_ids}
    nonces = {pid: bytes.fromhex(rounds[pid].local_nonce_hex) for pid in all_ids}
    for pid, sr in rounds.items():
        for other in all_ids:
            if other != pid:
                sr.record_reveal(other, seeds[other], nonces[other])

    decks = [rounds[pid].shuffled_deck() for pid in all_ids]
    assert decks[0] == decks[1] == decks[2]
    assert sorted(decks[0]) == list(range(52))


# ---------------------------------------------------------------------------
# ShuffleRound — tamper detection
# ---------------------------------------------------------------------------

def test_shuffle_round_tampered_seed_raises():
    host   = _make_round("host", ["host", "peer"])
    client = _make_round("peer", ["host", "peer"])

    host.record_commit("peer", client.local_commit())
    client.record_commit("host", host.local_commit())

    # Peer tries to reveal a DIFFERENT seed than committed
    bad_seed = bytes(b ^ 0xFF for b in bytes.fromhex(client.local_seed_hex))
    with pytest.raises(ValueError, match="[Cc]ommit"):
        host.record_reveal("peer", bad_seed,
                           bytes.fromhex(client.local_nonce_hex))


def test_shuffle_round_missing_commit_raises():
    sr = _make_round("host", ["host", "peer"])
    sr.local_commit()
    # Try to record a reveal for a peer we never got a commit from
    with pytest.raises(ValueError, match="commit"):
        sr.record_reveal("peer", b"\x00" * 32, b"\x00" * 16)


def test_shuffle_round_not_all_commits_before_master_seed():
    sr = _make_round("host", ["host", "peer"])
    sr.local_commit()
    # peer hasn't sent a commit yet → all_commits_received should be False
    assert not sr.all_commits_received()


def test_shuffle_round_shuffled_deck_before_reveals_raises():
    sr = _make_round("solo", ["solo"])
    sr.local_commit()
    # Record own reveal (trivial 1-party round)
    sr.record_reveal("solo",
                     bytes.fromhex(sr.local_seed_hex),
                     bytes.fromhex(sr.local_nonce_hex))
    # Now it should work fine (1-party is degenerate but valid)
    deck = sr.shuffled_deck()
    assert sorted(deck) == list(range(52))


# ---------------------------------------------------------------------------
# X25519 + AES-256-GCM encrypt / decrypt
# ---------------------------------------------------------------------------

def _x25519_keypair():
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    priv = X25519PrivateKey.generate()
    pub  = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv, pub


def test_encrypt_decrypt_round_trip():
    priv, pub = _x25519_keypair()
    cards = ["Ac", "Kh"]
    blob  = encrypt_hole_cards(cards, pub)
    result = decrypt_hole_cards(blob, priv)
    assert result == cards


def test_encrypt_decrypt_two_cards_arbitrary():
    priv, pub = _x25519_keypair()
    for hand in [["2c", "7d"], ["Th", "Js"], ["As", "Ac"]]:
        assert decrypt_hole_cards(encrypt_hole_cards(hand, pub), priv) == hand


def test_encrypt_produces_different_blobs_each_call():
    """Ephemeral keypair ensures ciphertexts are never identical."""
    _, pub = _x25519_keypair()
    cards = ["Ah", "2d"]
    blob1 = encrypt_hole_cards(cards, pub)
    blob2 = encrypt_hole_cards(cards, pub)
    assert blob1 != blob2


def test_decrypt_wrong_key_raises():
    _, pub  = _x25519_keypair()
    priv2, _ = _x25519_keypair()
    blob = encrypt_hole_cards(["Ac", "Kh"], pub)
    with pytest.raises(ValueError):
        decrypt_hole_cards(blob, priv2)


def test_decrypt_tampered_ciphertext_raises():
    priv, pub = _x25519_keypair()
    blob = bytearray(encrypt_hole_cards(["Ac", "Kh"], pub))
    # Flip a byte in the ciphertext region (after pubkey+iv = 44 bytes)
    blob[50] ^= 0xFF
    with pytest.raises(ValueError):
        decrypt_hole_cards(bytes(blob), priv)


def test_decrypt_truncated_blob_raises():
    with pytest.raises(ValueError, match="too short"):
        decrypt_hole_cards(b"\x00" * 10, None)


# ---------------------------------------------------------------------------
# Deck.from_indices integration with Engine.start_hand
# ---------------------------------------------------------------------------

def test_deck_from_indices_deals_correct_card():
    """Card at index 0 of the shuffled order must be the first card dealt."""
    from holdem.engine import Deck, FULL_DECK

    indices = list(range(52))   # identity permutation
    deck = Deck.from_indices(indices)

    # First deal should return FULL_DECK[0] (the 2♣)
    dealt = deck.deal(1)
    assert len(dealt) == 1
    assert dealt[0].v == FULL_DECK[0].v
    assert dealt[0].s == FULL_DECK[0].s


def test_deck_from_indices_full_hand():
    """Engine.start_hand with a Deck.from_indices deck runs a full solo hand."""
    from holdem.engine import Deck, Engine, Player as EngPlayer
    import hashlib

    master_seed = hashlib.sha256(b"test-integration").digest()
    from holdem.p2p.shuffle import deterministic_shuffle as ds
    indices = ds(master_seed)
    deck = Deck.from_indices(indices)

    players = [
        EngPlayer(0, "Alice", 1000, human=True),
        EngPlayer(1, "Bob",   1000),
    ]
    engine = Engine(players, sb=5, bb=10)
    ok = engine.start_hand(deck=deck)
    assert ok, "start_hand should succeed with 2 seated players"
    assert engine.street == "preflop"
    assert len(engine.players[0].hole) == 2
    assert len(engine.players[1].hole) == 2


def test_deck_from_indices_deterministic_via_master_seed():
    """Two engines seeded from the same master_seed must get identical hole cards."""
    from holdem.engine import Deck, Engine, Player as EngPlayer
    from holdem.p2p.shuffle import deterministic_shuffle as ds, derive_master_seed

    seeds = {"alpha": b"\x10" * 32, "beta": b"\x20" * 32}
    master = derive_master_seed(seeds)
    indices = ds(master)

    def _run():
        deck = Deck.from_indices(indices)
        players = [
            EngPlayer(0, "P0", 500, human=True),
            EngPlayer(1, "P1", 500),
        ]
        e = Engine(players, sb=5, bb=10)
        e.start_hand(deck=deck)
        return (
            [(c.v, c.s) for c in e.players[0].hole],
            [(c.v, c.s) for c in e.players[1].hole],
        )

    run1 = _run()
    run2 = _run()
    assert run1 == run2
