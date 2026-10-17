"""PR #41 revision-4 specification model; no implemented v3 recovery claims.
Run from the repository root: python -B docs/research/models/recovery_context_v3.py
Checks canonical context fields, signed nonce agreement, replay predicates and
retention of the same setup binding. It does not exercise journal fsync,
transport, MentalDeal, or poker-hand recovery.
"""
from pathlib import Path
import hashlib
import json
import re
import struct

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

DOC = Path(__file__).resolve().parents[2] / "RECOVERY_SPEC.md"
SIGNERS = [Ed25519PrivateKey.from_private_bytes(bytes([i]) * 32) for i in (1, 2)]
KEYS = [key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) for key in SIGNERS]


def lp(value):
    return struct.pack(">I", len(value)) + value


def fields(table_id):
    assert len(table_id) == 32
    return [lp(b"poker.deal.context"), struct.pack(">I", 3), table_id,
            lp(b"bayer-groth-v1"), lp(b"poker.tda.2024.nlhe.v1"),
            struct.pack(">I", 2), b"".join(b"\x01" + lp(key) for key in KEYS),
            struct.pack(">I", 0), struct.pack(">Q", 30000),
            struct.pack(">Q", 5000), struct.pack(">Q", 5000)]


def proposal(host_nonce):
    f = fields(bytes(32))
    return hashlib.sha256(lp(b"poker.table.proposal.v1") + KEYS[0] + host_nonce
                          + b"".join(f[1:2] + f[3:])).digest()


def nonce_preimage(digest, seat, nonce):
    return (lp(b"poker.table.nonce.v1") + digest + struct.pack(">H", seat)
            + KEYS[seat] + nonce)


def identifier(digest, records):
    raw = lp(b"poker.table.session.v1") + digest + struct.pack(">I", len(KEYS))
    for seat, nonce, _signature in records:
        raw += struct.pack(">H", seat) + KEYS[seat] + nonce
    return hashlib.sha256(raw).digest()


def setup(nonces, host_nonce=b"h" * 32):
    digest = proposal(host_nonce)
    records = [(seat, nonce, SIGNERS[seat].sign(nonce_preimage(digest, seat, nonce)))
               for seat, nonce in enumerate(nonces)]
    return host_nonce, records, identifier(digest, records)


def validate_setup(transcript, local_binding, check_local=True):
    host_nonce, records, supplied = transcript
    digest = proposal(host_nonce)
    assert [record[0] for record in records] == list(range(len(KEYS)))
    for seat, nonce, signature in records:
        assert len(nonce) == 32
        SIGNERS[seat].public_key().verify(signature, nonce_preimage(digest, seat, nonce))
    assert identifier(digest, records) == supplied
    if check_local:
        local_seat, local_proposal, local_nonce = local_binding
        assert local_proposal == digest
        assert records[local_seat][1] == local_nonce
    return supplied


def context(table_id):
    return "poker.deal.v3:" + hashlib.sha256(b"".join(fields(table_id))).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def envelope(kind, ctx):
    payload = {"ctx": ctx, "hand": 1, "seat": 1}
    if kind == "hand_abandon":
        payload.update(subject=[0], reason=3, nonce="00" * 16)
    else:
        payload.update(author_seq=9, seq=0, action="fold", amount=0)
    message = {"v": 1, "type": kind, "payload": payload, "pubkey": KEYS[1].hex(),
               "ts": 1720000000000, "prev": "0" * 64}
    message["sig"] = SIGNERS[1].sign(canonical(message)).hex()
    message["hash"] = hashlib.sha256(canonical(message)).hexdigest()
    return message


def accepts(message, ctx):
    full = dict(message)
    assert full.pop("hash") == hashlib.sha256(canonical(full)).hexdigest()
    signature = bytes.fromhex(full.pop("sig"))
    SIGNERS[1].public_key().verify(signature, canonical(full))
    payload = full["payload"]
    return payload["ctx"] == ctx and payload["hand"] == 1 and payload["seat"] == 1


def rejected(call):
    try:
        call()
    except (AssertionError, ValueError, struct.error):
        return True
    return False


def main():
    # Deterministic model fixtures, not production defaults. Deliberately reuse
    # the host's proposal nonce to prove the honest peer's nonce is sufficient.
    previous = setup([b"a" * 32, b"b" * 32])
    fresh = setup([b"c" * 32, b"d" * 32])
    current_binding = (1, proposal(fresh[0]), b"d" * 32)
    current_id = validate_setup(fresh, current_binding)
    old_ctx, new_ctx = context(previous[2]), context(current_id)
    assert old_ctx != new_ctx

    for kind in ("bet_action", "hand_abandon"):
        old = envelope(kind, old_ctx)
        assert accepts(old, old_ctx)
        assert not accepts(old, new_ctx)

    # A fresh outer host signature does not make old contributions fresh.
    replay_payload = previous[0] + b"".join(nonce + sig for _, nonce, sig in previous[1]) + previous[2]
    fresh_host_signature = SIGNERS[0].sign(replay_payload)
    SIGNERS[0].public_key().verify(fresh_host_signature, replay_payload)
    assert rejected(lambda: validate_setup(previous, current_binding))
    assert validate_setup(previous, current_binding, check_local=False) == previous[2]

    # Model the exact SESSION_NONCE payload and restart with its recovered data.
    record = struct.pack(">IH", 1, current_binding[0]) + current_binding[1] + current_binding[2]
    assert len(record) == 70
    version, seat = struct.unpack(">IH", record[:6])
    assert version == 1
    recovered_binding = seat, record[6:38], record[38:70]
    recovered_id = validate_setup(fresh, recovered_binding)
    assert recovered_id == current_id and context(recovered_id) == new_ctx
    assert rejected(lambda: validate_setup(previous, recovered_binding))
    assert rejected(lambda: validate_setup(fresh, (seat, record[6:38], b"x" * 32)))

    # A pending peer has only SESSION_NONCE, not id/ctx. Model a fetched
    # complete transcript restored by the host from its journalled game_start.
    host_record = json.dumps({
        "host_nonce": fresh[0].hex(),
        "records": [[seat, nonce.hex(), sig.hex()] for seat, nonce, sig in fresh[1]],
        "identifier": fresh[2].hex(),
    })
    decoded = json.loads(host_record)
    fetched = (bytes.fromhex(decoded["host_nonce"]),
               [(seat, bytes.fromhex(nonce), bytes.fromhex(sig))
                for seat, nonce, sig in decoded["records"]],
               bytes.fromhex(decoded["identifier"]))
    fetched_id = validate_setup(fetched, recovered_binding)
    assert fetched_id == current_id and context(fetched_id) == new_ctx
    assert rejected(lambda: validate_setup(previous, recovered_binding))

    # Check the markdown's encoding-only fixture widths and offsets independently.
    vector = fields(bytes(range(32)))
    expected = []
    offset = 0
    for number, raw in enumerate(vector):
        expected.append((number, offset, len(raw)))
        offset += len(raw)
    assert offset == 208
    doc = DOC.read_text(encoding="utf-8")
    table = doc.split("The zero-based field offsets and widths are:", 1)[1].split("Total pre-image", 1)[0]
    actual = [tuple(map(int, match)) for match in re.findall(r"\| (\d+) \| (\d+) \| (\d+)", table)]
    assert actual == expected, (actual, expected)

    # Negative domain control: omit field 2, restoring the reviewed defect.
    first = fields(previous[2]); second = fields(current_id)
    assert b"".join(first[:2] + first[3:]) == b"".join(second[:2] + second[3:])

    print("SPEC MODEL ONLY; no v3 runtime recovery or journal-I/O claims")
    print("208-octet layout and all documented offsets: PASS")
    print("Signed fresh per-seat contributions produce distinct contexts: PASS")
    print("Prior signed bet_action and hand_abandon rejected: PASS")
    print("Freshly signed malicious-host reuse rejected by local nonce: PASS")
    print("70-octet SESSION_NONCE recovery retains identifier/context: PASS")
    print("Pending setup fetch derives context without prior id/ctx: PASS")
    print("Wrong recovery nonce rejected: PASS")
    print("Deliberate breaks (omit session field; skip local nonce check): DETECTED")


if __name__ == "__main__":
    main()
