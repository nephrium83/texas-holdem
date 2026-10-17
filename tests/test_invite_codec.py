"""Wire fixtures and numeric-address compatibility for the socket-free codec."""
from __future__ import annotations

import base64
import os
from pathlib import Path
import random
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from holdem.p2p import invite


FIELDS = {
    "host_pubkey": bytes(range(32)),
    "discovery_token": "2021222324252627",
    "admission_secret": "303132333435363738393a3b3c3d3e3f",
}
# Captured from the pre-refactor codec, rather than built with its offsets.
GOLDEN_RAW = bytes.fromhex(
    "02000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    "2021222324252627303132333435363738393a3b3c3d3e3f"
    "cb00712ad431c0a8010a1ec6a5")
GOLDEN_CODE = (
    "AIAA-CAQD-AQCQ-MBYI-BEFA-WDAN-BYHR-AEIS-CMKB-KFQX-DAMR-UGY4-DUPB-6IBB-"
    "EIRS-IJJG-E4YD-CMRT-GQ2T-MNZY-HE5D-WPB5-HY74-WADR-FLKD-DQFI-AEFB-5RVF")


def _raw(code):
    return base64.b32decode(invite.strip_code(code))


def _code(raw):
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def test_v2_wire_bytes_match_the_pre_refactor_fixture():
    code = invite.generate_room_code(
        **FIELDS, flags=0xA5, public_address=("203.0.113.42", 54321),
        relay_address=("192.168.1.10", 7878))
    assert code == GOLDEN_CODE
    assert _raw(code) == GOLDEN_RAW
    assert len(GOLDEN_RAW) == 70


@pytest.mark.parametrize("code", [GOLDEN_CODE, GOLDEN_CODE.lower(),
                                 GOLDEN_CODE.replace("-", " ")])
def test_the_fixed_wire_fixture_decodes_every_field(code):
    assert invite.parse_room_code(code) == {
        "version": 2,
        "host_pubkey": FIELDS["host_pubkey"].hex(),
        "discovery_token": FIELDS["discovery_token"],
        "admission_secret": FIELDS["admission_secret"],
        "public_ip": "203.0.113.42", "public_port": 54321,
        "relay_host": "192.168.1.10", "relay_port": 7878, "flags": 0xA5,
    }


@pytest.mark.parametrize("address, canonical", [
    ("1", "0.0.0.1"), ("256", "0.0.1.0"),
    ("127.1", "127.0.0.1"), ("127.0.1", "127.0.0.1"),
    ("127.0.0.1", "127.0.0.1"),
    ("1.16777215", "1.255.255.255"),
    ("1.2.65535", "1.2.255.255"), ("1.2.3.255", "1.2.3.255"),
    ("4294967294", "255.255.255.254"),
    ("0x7f000001", "127.0.0.1"), ("0X7F000001", "127.0.0.1"),
    ("017700000001", "127.0.0.1"), ("0177.1", "127.0.0.1"),
    ("0x7f.0.01.1", "127.0.1.1"), ("010", "0.0.0.8"),
    ("1.2.3.4\tignored", "1.2.3.4"), ("1.2.3.4\nignored", "1.2.3.4"),
    ("1.2.3.4\r\v\f ", "1.2.3.4"), ("1 .2", "0.0.0.1"),
    ("1 \uff11", "0.0.0.1"),
    pytest.param("0x" + "0" * 5000 + "1", "0.0.0.1", id="long-zero-prefix"),
])
@pytest.mark.parametrize("field, parsed_field", [
    ("public_address", "public_ip"), ("relay_address", "relay_host")])
def test_legacy_numeric_forms_keep_their_address_bytes(
        field, parsed_field, address, canonical):
    code = invite.generate_room_code(**FIELDS, **{field: (address, 65535)})
    assert invite.parse_room_code(code)[parsed_field] == canonical


@pytest.mark.parametrize("address", ["0", "00", "0x0", "0.0", "0.0.0.0"])
def test_every_numeric_zero_suppresses_its_nonzero_port(address):
    parsed = invite.parse_room_code(invite.generate_room_code(
        **FIELDS, public_address=(address, 1234), relay_address=(address, 5678)))
    assert parsed["public_ip"] is parsed["public_port"] is None
    assert parsed["relay_host"] is parsed["relay_port"] is None


@pytest.mark.parametrize("address", [
    "", "  ", "\t", "\n", " 1.2.3.4", "\t1.2.3.4", "1.2.3.4x", "localhost", "::1",
    ".1", "1.", "1..2", "1.2.3.4.5", "1.2.3. 4", "+1", "-1", "1_0",
    "0x", "0X", "0xg", "08", "09", "0o10", "0b10", "00x1",
    "4294967296", "0x100000000", "040000000000", "256.1", "1.16777216",
    "1.2.65536", "1.2.3.256", "1.2.3.08", "1.2.3.4\x1c", "1.2.3.4\x85",
    "1.2.3.4\xa0", "1.2.3.4\u2000", "\uff11\uff12\uff17.0.0.1", "\u0661.2.3.4",
])
@pytest.mark.parametrize("field", ["public_address", "relay_address"])
def test_malformed_numeric_addresses_still_raise_oserror(field, address):
    with pytest.raises(OSError, match="illegal IP address string"):
        invite.generate_room_code(**FIELDS, **{field: (address, 1234)})


@pytest.mark.parametrize("address, error", [
    (None, TypeError), (1, TypeError), (b"127.0.0.1", TypeError),
    (bytearray(b"127.0.0.1"), TypeError), ("1.2.3.4\0ignored", ValueError),
    ("1 \0", ValueError), ("1 \ud800", UnicodeEncodeError),
])
def test_address_type_and_string_conversion_errors_are_preserved(address, error):
    with pytest.raises(error):
        invite.generate_room_code(**FIELDS, public_address=(address, 1234))


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_all_ones_keeps_the_windows_exact_spelling_exception(platform, monkeypatch):
    monkeypatch.setattr(invite.sys, "platform", platform)
    for address in ["255.255.255.255", "4294967295", "0xffffffff",
                    "037777777777", "255.16777215", "255.255.65535",
                    "0377.0377.0377.0377", "255.255.255.255 "]:
        if platform == "win32" and address != "255.255.255.255":
            with pytest.raises(OSError):
                invite.generate_room_code(**FIELDS, public_address=(address, 1))
        else:
            parsed = invite.parse_room_code(invite.generate_room_code(
                **FIELDS, public_address=(address, 1)))
            assert parsed["public_ip"] == "255.255.255.255"


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_a_single_space_retains_the_windows_zero_exception(platform, monkeypatch):
    monkeypatch.setattr(invite.sys, "platform", platform)
    if platform == "win32":
        parsed = invite.parse_room_code(invite.generate_room_code(
            **FIELDS, public_address=(" ", 1234)))
        assert parsed["public_ip"] is parsed["public_port"] is None
    else:
        with pytest.raises(OSError):
            invite.generate_room_code(**FIELDS, public_address=(" ", 1234))


def test_a_numeric_corpus_matches_the_local_socket_codec():
    """Differential coverage includes valid layouts and malformed mixtures."""
    import socket

    rng = random.Random(20261017)
    tokens = ["0", "1", "255", "256", "65535", "65536", "16777215",
              "16777216", "4294967294", "4294967295", "4294967296",
              "00", "010", "08", "0x", "0xff", "0X7F", "0377", "0400",
              "", "+1", "-1", "1_0", "\uff11"]
    addresses = {".".join(rng.choices(tokens, k=rng.randint(1, 5)))
                 + rng.choice(["", "", " ", "\tignored", "x", "\xa0"])
                 for _ in range(2000)}
    addresses.update(".".join(str(rng.randrange(256)) for _ in range(4))
                     for _ in range(200))
    for address in sorted(addresses):
        try:
            expected = socket.inet_aton(address)
        except OSError:
            with pytest.raises(OSError):
                invite.generate_room_code(**FIELDS, public_address=(address, 1))
        else:
            raw = _raw(invite.generate_room_code(
                **FIELDS, public_address=(address, 1), relay_address=(address, 2)))
            assert raw[57:61] == expected, repr(address)
            assert raw[63:67] == expected, repr(address)


@pytest.mark.parametrize("length", [0, 1, 28, 30, 69, 71])
def test_malformed_payload_lengths_are_refused(length):
    with pytest.raises(ValueError, match="expected 70"):
        invite.parse_room_code(_code(bytes(length)))


@pytest.mark.parametrize("first_byte", [0, 2, 255])
def test_legacy_length_wins_over_the_first_byte(first_byte):
    with pytest.raises(invite.LegacyInviteError, match="regenerate"):
        invite.parse_room_code(_code(bytes([first_byte]) + bytes(28)))


@pytest.mark.parametrize("version", [0, 1, 3, 255])
def test_unknown_v2_sized_versions_are_refused(version):
    with pytest.raises(ValueError, match="Unsupported room code version"):
        invite.parse_room_code(_code(bytes([version]) + GOLDEN_RAW[1:]))


@pytest.mark.parametrize("code", ["!", "0" * 112, "1" * 112, "A" * 110,
                                 "A\t" * 56, "\u00e9" * 112])
def test_malformed_base32_is_refused(code):
    with pytest.raises(ValueError, match="not valid Base32"):
        invite.parse_room_code(code)


# Import and exercise both directions while neither the Python socket
# facade nor its native module can be loaded, in a fresh interpreter.
SOCKET_FREE_PROBE = r"""
import importlib, sys
sys.modules['socket'] = None
sys.modules['_socket'] = None
codec = importlib.import_module(sys.argv[1])
code = codec.generate_room_code(
    bytes(range(32)), flags=0xa5,
    discovery_token='2021222324252627',
    admission_secret='303132333435363738393a3b3c3d3e3f',
    public_address=('0xcb.0.0161.42', 54321),
    relay_address=('0300.0xa8.266', 7878))
assert code == sys.argv[2], 'wire bytes changed'
parsed = codec.parse_room_code(code)
assert parsed['public_ip'] == '203.0.113.42'
assert parsed['relay_host'] == '192.168.1.10'
"""


def _fresh_codec(module, tmp_path):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), str(REPO)]))
    return subprocess.run(
        [sys.executable, "-B", "-c", SOCKET_FREE_PROBE, module, GOLDEN_CODE],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)


def test_import_and_codec_operations_need_no_socket_module(tmp_path):
    proc = _fresh_codec("holdem.p2p.invite", tmp_path)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("mutation, failure", [
    ("import socket\n", "import of socket halted"),
    ("original = generate_room_code\n"
     "def generate_room_code(*args, **kwargs):\n"
     "    import socket\n"
     "    return original(*args, **kwargs)\n", "import of socket halted"),
    ("original = generate_room_code\n"
     "def generate_room_code(*args, **kwargs):\n"
     "    kwargs['flags'] += 1\n"
     "    return original(*args, **kwargs)\n", "wire bytes changed"),
], ids=["socket-import", "lazy-socket-import", "changed-wire-byte"])
def test_control_socket_or_wire_regressions_fail_the_fresh_probe(
        mutation, failure, tmp_path):
    source = Path(invite.__file__).read_text(encoding="utf-8")
    (tmp_path / "broken_invite.py").write_text(source + "\n" + mutation,
                                              encoding="utf-8")
    proc = _fresh_codec("broken_invite", tmp_path)
    assert proc.returncode != 0
    assert failure in proc.stderr
