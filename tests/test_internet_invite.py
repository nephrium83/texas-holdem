"""Internet invite routing is explicit, bounded, and private by default."""
import base64
from dataclasses import replace
import json

import pytest

from holdem.internet_invite import InternetInvite, InviteError, encode_invite, parse_invite
from holdem.p2p import invite as lan_invite


NOW = 1800000000
SERVICES = {"test": "ws://127.0.0.1:12345"}


@pytest.fixture
def invite():
    return InternetInvite(service="test", host_public="a" * 64, discovery_token="b" * 16,
                          generation="c" * 32, expires_at=NOW + 600, admission_secret="d" * 32)


def _alter(code, **changes):
    encoded = code.split(":", 1)[1]
    value = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    value.update(changes)
    return "poker3:" + base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def test_explicit_internet_invite_roundtrips_and_redacts_capability(invite):
    code = encode_invite(invite)
    assert code.startswith("poker3:") and len(code) <= 1024
    assert parse_invite(code, services=SERVICES, now=NOW) == invite
    assert parse_invite("\n" + code + "\n", services=SERVICES, now=NOW) == invite
    assert invite.admission_secret not in repr(invite)
    assert invite.admission_secret not in str(invite)
    assert "ws://" not in code


@pytest.mark.parametrize("changes", [
    {"v": 2}, {"v": True}, {"profile": "relay"}, {"profile": None},
    {"service": "https://evil.example"}, {"service": "other"},
    {"host_public": "a" * 16}, {"host_public": "A" * 64},
    {"admission_secret": "d" * 31}, {"discovery_token": "b" * 17},
    {"generation": "c" * 31}, {"expires_at": True}, {"expires_at": NOW},
    {"expires_at": NOW - 1}, {"expires_at": NOW + 1801}, {"extra": "field"},
])
def test_unsupported_tampered_or_expired_invites_fail(invite, changes):
    with pytest.raises(InviteError):
        parse_invite(_alter(encode_invite(invite), **changes), services=SERVICES, now=NOW)


def test_invites_cannot_choose_unconfigured_service_or_network_address(invite):
    with pytest.raises(InviteError, match="not configured"):
        parse_invite(encode_invite(invite), services={}, now=NOW)
    with pytest.raises(InviteError):
        replace(invite, service="wss://192.168.1.1")


@pytest.mark.parametrize("code", ["", "poker4:AAAA", "poker3:", "poker3:AAAA=", "poker3:!!!!", "x" * 1025, None])
def test_malformed_encoding_is_bounded_and_safe(code):
    with pytest.raises(InviteError):
        parse_invite(code, services=SERVICES, now=NOW)


def test_duplicate_json_field_is_refused(invite):
    code = encode_invite(invite)
    encoded = code.split(":", 1)[1]
    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    duplicate = b'{"v":3,' + raw[1:]
    code = "poker3:" + base64.urlsafe_b64encode(duplicate).decode().rstrip("=")
    with pytest.raises(InviteError):
        parse_invite(code, services=SERVICES, now=NOW)


def test_lan_v2_invite_remains_separate(invite):
    lan = lan_invite.generate_room_code(bytes.fromhex(invite.host_public), public_address=("192.168.1.22", 7823))
    assert lan_invite.parse_room_code(lan)["version"] == 2
    with pytest.raises(InviteError):
        parse_invite(lan, services=SERVICES, now=NOW)
    with pytest.raises(ValueError):
        lan_invite.parse_room_code(encode_invite(invite))
