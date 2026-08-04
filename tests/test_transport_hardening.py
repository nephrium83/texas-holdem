"""Resource bounds on untrusted input, enforced before expensive work.

A peer is hostile until its bytes have been validated. These tests cover
the inputs that cost the receiver more than they cost the sender: deeply
nested JSON, oversized frames, and unterminated lines.

The nesting case is not theoretical. json.loads raises RecursionError --
NOT JSONDecodeError, and not a ValueError -- at roughly 20k nesting depth,
which fits in about 40 KB, far under the 1 MB frame cap. Neither
transport's error handling covered that type, so a 40 KB message became an
uncaught exception rather than a clean peer drop.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p import wire


def nested(depth: int) -> bytes:
    return ("[" * depth + "]" * depth).encode()


# ------------------------------------------------------- depth guard

def test_deeply_nested_json_is_rejected_as_valueerror():
    """The core fix: hostile nesting must fail closed, not raise an
    exception type the caller does not handle."""
    with pytest.raises(ValueError):
        wire.safe_loads(nested(20_000))


def test_nesting_rejected_below_the_recursion_limit():
    """Rejection must happen at our bound, not by hitting CPython's stack.
    Relying on RecursionError would make the behaviour depend on the
    interpreter's recursion limit and on how deep the call stack already is."""
    with pytest.raises(ValueError, match="nest"):
        wire.safe_loads(nested(wire.MAX_JSON_DEPTH + 1))


def test_recursion_error_never_escapes():
    """Belt and braces: whatever the depth, the caller sees ValueError."""
    for depth in (wire.MAX_JSON_DEPTH + 1, 5_000, 50_000):
        with pytest.raises(ValueError):
            wire.safe_loads(nested(depth))


def test_legal_nesting_still_parses():
    """The guard must not reject the protocol's own messages. A shuffle
    proof is the deepest real payload and nests only a few levels."""
    payload = {"a": [[{"b": [1, 2, {"c": ["00" * 32]}]}]]}
    assert wire.safe_loads(json.dumps(payload).encode()) == payload


def test_depth_bound_covers_objects_and_mixed_nesting():
    deep_obj = ('{"a":' * (wire.MAX_JSON_DEPTH + 1)
                + "1" + "}" * (wire.MAX_JSON_DEPTH + 1))
    with pytest.raises(ValueError, match="nest"):
        wire.safe_loads(deep_obj.encode())
    mixed = ("[{" + '"k":[' * (wire.MAX_JSON_DEPTH // 2 + 2))
    with pytest.raises(ValueError):
        wire.safe_loads(mixed.encode())


def test_depth_guard_runs_before_parsing():
    """The scan must reject without handing the blob to json.loads, so a
    hostile payload costs a linear scan and no allocation."""
    called = []
    original = json.loads

    def spy(*args, **kwargs):
        called.append(1)
        return original(*args, **kwargs)

    wire.json.loads = spy
    try:
        with pytest.raises(ValueError):
            wire.safe_loads(nested(wire.MAX_JSON_DEPTH + 1))
    finally:
        wire.json.loads = original
    assert not called, "json.loads was invoked on an over-nested payload"


# -------------------------------------------------------- size guard

def test_oversized_payload_is_rejected():
    with pytest.raises(ValueError, match="bytes"):
        wire.safe_loads(b"[" + b"0," * wire.MAX_JSON_BYTES + b"]")


def test_size_limit_is_not_larger_than_the_frame_limit():
    """A payload that passes the frame check must still face a JSON bound."""
    from holdem.p2p import transport
    assert wire.MAX_JSON_BYTES <= transport.MAX_MSG


# ------------------------------------------------- malformed inputs

@pytest.mark.parametrize("raw", [
    b"", b"   ", b"not json", b"{", b"[", b'{"a":}', b"\x00\x01\x02",
    b'{"a": 1e999999}',
])
def test_malformed_payloads_fail_closed(raw):
    with pytest.raises(ValueError):
        wire.safe_loads(raw)


def test_non_utf8_bytes_fail_closed():
    with pytest.raises(ValueError):
        wire.safe_loads(b'{"a": "\xff\xfe"}')
