"""Explicit Internet invitations; LAN V2 codes retain their existing codec.

Only a configured service identifier is carried. Invitations cannot introduce
an arbitrary network destination. The private capability is serialized only
when the host deliberately calls encode_invite(), never by object repr.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import re
import time


VERSION = 3
PROFILE = "direct-udp-v1"
PREFIX = "poker3:"
MAX_CODE_CHARS = 1024
MAX_LIFETIME = 1800
_FIELDS = frozenset({"v", "profile", "service", "host_public", "discovery_token",
                     "generation", "expires_at", "admission_secret"})


class InviteError(ValueError):
    """An unsupported, expired, or malformed invite; never includes its text."""


def _hex(value, length):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{%d}" % length, value) is None:
        raise InviteError("Invalid Internet invite field")


def _validate(value):
    if type(value) is not dict or set(value) != _FIELDS:
        raise InviteError("Invalid Internet invite fields")
    if type(value["v"]) is not int or value["v"] != VERSION or value["profile"] != PROFILE:
        raise InviteError("Unsupported Internet invite version or profile")
    if type(value["service"]) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", value["service"]) is None:
        raise InviteError("Invalid Internet introduction service identifier")
    for name, size in (("host_public", 64), ("discovery_token", 16),
                       ("generation", 32), ("admission_secret", 32)):
        _hex(value[name], size)
    if type(value["expires_at"]) is not int or not 0 < value["expires_at"] < 2**63:
        raise InviteError("Invalid Internet invite expiry")


@dataclass(frozen=True)
class InternetInvite:
    service: str
    host_public: str
    discovery_token: str
    generation: str
    expires_at: int
    admission_secret: str = field(repr=False)
    v: int = field(default=VERSION, init=False)
    profile: str = field(default=PROFILE, init=False)

    def __post_init__(self):
        _validate(_record(self))


def _record(invite):
    return {"v": invite.v, "profile": invite.profile, "service": invite.service,
            "host_public": invite.host_public, "discovery_token": invite.discovery_token,
            "generation": invite.generation, "expires_at": invite.expires_at,
            "admission_secret": invite.admission_secret}


def encode_invite(invite: InternetInvite) -> str:
    if type(invite) is not InternetInvite:
        raise InviteError("An InternetInvite is required")
    value = _record(invite)
    _validate(value)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    result = PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if len(result) > MAX_CODE_CHARS:
        raise InviteError("Internet invite is too large")
    return result


def parse_invite(code: str, *, services: dict, now=None) -> InternetInvite:
    if type(code) is not str or not 1 <= len(code) <= MAX_CODE_CHARS:
        raise InviteError("Invalid Internet invite size")
    if type(services) is not dict:
        raise InviteError("Trusted introduction services must be configured")
    text = code.strip()
    if not text.startswith(PREFIX):
        raise InviteError("Unsupported Internet invite version or profile")
    encoded = text[len(PREFIX):]
    if re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None:
        raise InviteError("Invalid Internet invite encoding")
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != encoded:
            raise InviteError("Noncanonical Internet invite encoding")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise InviteError("Duplicate Internet invite field")
                result[key] = value
            return result

        value = json.loads(raw.decode("ascii"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(InviteError("Invalid Internet invite value")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InviteError("Invalid Internet invite encoding") from None
    _validate(value)
    if value["service"] not in services:
        raise InviteError("This introduction service is not configured")
    instant = int(time.time()) if now is None else now
    if type(instant) is not int:
        raise InviteError("Invalid current time")
    if not instant < value["expires_at"] <= instant + MAX_LIFETIME:
        raise InviteError("Internet invite is expired or has an invalid lifetime")
    return InternetInvite(**{key: item for key, item in value.items() if key not in ("v", "profile")})
