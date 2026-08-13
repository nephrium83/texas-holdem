"""Authenticated admission: prove possession of the invite, pin the host.

The problem this solves
-----------------------
Before this, any process that could open a TCP connection and sign an
envelope became a ``Player`` in the host's roster: ``_on_player_info``
created one from any correctly-signed connection, and "correctly signed"
only means the sender owns *some* Ed25519 key, which anyone can generate.
Possession of the room code was never actually demonstrated to the host.

Meanwhile the joiner's protection against connecting to the wrong host was
a ``startswith()`` on the first 8 bytes of the host key, evaluated at game
start -- after the joiner had already sent its identity, appeared in the
roster, and possibly readied up.

What this provides, and what it does not
----------------------------------------
Provides: mutual authentication of the two Ed25519 identities, and proof
that the joiner holds the admission secret from the invite, completed
BEFORE any lobby state can be mutated.

Does NOT provide: any defence against someone who legitimately holds the
room code. The admission secret is shared by everyone invited, so it proves
"has the invitation", not "is entitled to exactly one seat". A holder can
generate any number of Ed25519 identities and complete this handshake once
per identity. That is a policy problem -- host approval, or one-time
per-seat capabilities -- and it is deliberately not addressed here. Calling
this Sybil resistance would be false.

The secret never crosses the network. It is only ever an HMAC key over a
transcript of values both sides already know.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from dataclasses import dataclass
from hashlib import sha256
from typing import Dict, Optional

# Logs record DECISIONS and identities, never the admission secret and
# never a MAC. Public keys and conn_ids are fine -- they are already on the
# wire; the secret is the one value that never appears anywhere but memory.
_log = logging.getLogger(__name__)

# Domain separation, per cryptographic STATEMENT rather than per protocol.
# There is one MAC in this handshake and it says exactly one thing: "the
# holder of the admission secret asserts this response, for this lobby,
# between these two keys, over these two nonces". If a second MACed
# statement is ever added it gets its own label -- a shared generic
# "admission transcript" is what makes reflection and cross-statement
# substitution possible, because two different claims would then be
# indistinguishable byte strings under the same key.
DOMAIN_RESPONSE = b"poker.admission.v1.response"

# Bumped with the transcript LAYOUT, not with the product. A MAC computed
# under an older layout must not verify under a newer one.
TRANSCRIPT_VERSION = 1

NONCE_LEN  = 16
SECRET_LEN = 16
MAC_LEN    = 32
TOKEN_LEN  = 8

#: How long a pending challenge stays answerable. Long enough for a human
#: on a slow link, short enough that a captured challenge is not a standing
#: invitation. Bounded lifetime is also what stops pending state from
#: accumulating for connections that never answer.
CHALLENGE_TTL_SECONDS = 30.0

#: Message types that make up the handshake. A host accepts ONLY these from
#: a connection it has not yet admitted; see Session.handle_message.
ADMISSION_TYPES = frozenset({
    "admission_hello", "admission_challenge",
    "admission_response", "admission_accept",
})


def new_nonce() -> bytes:
    return secrets.token_bytes(NONCE_LEN)


def transcript(discovery_token: bytes, host_pubkey: bytes,
               joiner_pubkey: bytes, client_nonce: bytes,
               server_nonce: bytes) -> bytes:
    """The bytes both sides MAC.

    Every field is fixed width, and the only variable-length element -- the
    domain label -- is length-prefixed, so no two distinct tuples can encode
    to the same byte string. Without that, concatenation is ambiguous: two
    different (nonce, key) splits could produce identical input and a MAC
    over one would validate the other.

    What each field is doing:

    * the LABEL names the statement being made, so a MAC over this can
      never be mistaken for a MAC over some future, different assertion.
    * ``TRANSCRIPT_VERSION`` pins the layout, so a MAC produced under an
      older field order cannot verify under a newer one.
    * ``discovery_token`` binds the MAC to one LOBBY. Strictly redundant
      today, because the admission secret is freshly generated per invite
      and therefore already lobby-unique -- but that is a property of how
      the secret happens to be minted, not of this transcript. If a secret
      is ever reused, derived, or rotated across lobbies, this is the field
      that keeps a response from one table from being valid at another.
      Context binding is a byte-cheap hedge against a future refactor.
    * ``client_nonce`` and ``server_nonce`` make the transcript fresh in
      BOTH directions, so neither side can be made to accept a value it did
      not contribute entropy to.
    * ``host_pubkey`` binds the MAC to one specific host. A response
      captured from a handshake with host A is not a valid response to host
      B, even though both hold the same admission secret -- which matters
      because everyone invited holds it.
    * ``joiner_pubkey`` binds it to one specific joiner, so a captured
      response cannot be replayed under a different identity.
    """
    for name, value, size in (
        ("discovery_token", discovery_token, TOKEN_LEN),
        ("host_pubkey", host_pubkey, 32),
        ("joiner_pubkey", joiner_pubkey, 32),
        ("client_nonce", client_nonce, NONCE_LEN),
        ("server_nonce", server_nonce, NONCE_LEN),
    ):
        if not isinstance(value, (bytes, bytearray)) or len(value) != size:
            raise ValueError(
                f"{name} must be exactly {size} bytes, got "
                f"{len(value) if isinstance(value, (bytes, bytearray)) else type(value)}")
    return (bytes([len(DOMAIN_RESPONSE)]) + DOMAIN_RESPONSE
            + bytes([TRANSCRIPT_VERSION])
            + bytes(discovery_token)
            + bytes(host_pubkey) + bytes(joiner_pubkey)
            + bytes(client_nonce) + bytes(server_nonce))


def compute_mac(secret: bytes, tr: bytes) -> bytes:
    return hmac.new(bytes(secret), tr, sha256).digest()


def verify_mac(secret: bytes, tr: bytes, mac) -> bool:
    """Constant-time compare. Never ``==`` on a MAC."""
    if not isinstance(mac, (bytes, bytearray)) or len(mac) != MAC_LEN:
        return False
    return hmac.compare_digest(compute_mac(secret, tr), bytes(mac))


@dataclass
class _Pending:
    client_nonce: bytes
    server_nonce: bytes
    joiner_pubkey: bytes     # the key that sent the hello; the response must match
    issued_at: float


class HostAdmission:
    """Host side: issue challenges, verify responses, track who is admitted.

    State is CONNECTION-SCOPED and one-use. A challenge is consumed by the
    first response to it, whether that response is accepted or rejected, so
    an attacker cannot grind attempts against one live challenge. Disconnect
    clears both pending and admitted state, so a reconnecting peer must do
    the whole exchange again and a response captured from an earlier
    connection is useless.
    """

    def __init__(self, admission_secret: bytes, host_pubkey: bytes,
                 discovery_token: bytes = b"\x00" * TOKEN_LEN,
                 clock=None, ttl: float = CHALLENGE_TTL_SECONDS) -> None:
        secret = bytes(admission_secret)
        if len(secret) != SECRET_LEN:
            raise ValueError(f"admission_secret must be {SECRET_LEN} bytes")
        if len(bytes(host_pubkey)) != 32:
            raise ValueError("host_pubkey must be 32 bytes")
        if len(bytes(discovery_token)) != TOKEN_LEN:
            raise ValueError(f"discovery_token must be {TOKEN_LEN} bytes")
        self._secret = secret
        self._host_pubkey = bytes(host_pubkey)
        self._token = bytes(discovery_token)
        self._pending: Dict[str, _Pending] = {}
        self._admitted: Dict[str, bytes] = {}     # conn_id -> joiner pubkey
        # Nonces of the exchange that admitted each connection, kept so the
        # accept can echo the transcript the joiner will check against.
        self._admitted_nonces: Dict[str, tuple] = {}
        self._ttl = ttl
        if clock is None:
            import time as _time
            clock = _time.monotonic
        self._now = clock

    def __repr__(self) -> str:
        # Explicit, so the secret cannot reach a log line, a traceback, or a
        # debugger transcript through an incidental repr of a container that
        # happens to hold this object.
        return (f"<HostAdmission host={self._host_pubkey.hex()[:16]} "
                f"admitted={len(self._admitted)} pending={len(self._pending)} "
                f"secret=<redacted>>")

    # -- queries -------------------------------------------------------
    def is_admitted(self, conn_id: str) -> bool:
        return conn_id in self._admitted

    def admitted_key(self, conn_id: str) -> Optional[bytes]:
        return self._admitted.get(conn_id)

    # -- lifecycle -----------------------------------------------------
    def forget(self, conn_id: str) -> None:
        """Drop everything about a connection. Called on disconnect."""
        self._pending.pop(conn_id, None)
        self._admitted.pop(conn_id, None)
        self._admitted_nonces.pop(conn_id, None)

    def _expire(self) -> None:
        now = self._now()
        for cid in [c for c, p in self._pending.items()
                    if now - p.issued_at >= self._ttl]:
            del self._pending[cid]

    # -- handshake -----------------------------------------------------
    def on_hello(self, conn_id: str, joiner_pubkey: bytes,
                 client_nonce: bytes) -> Optional[dict]:
        """Answer an admission_hello with a challenge payload, or None.

        A second hello on the same connection REPLACES any pending
        challenge rather than adding one. Otherwise a peer could hold open
        an unbounded number of live challenges on one socket, and each
        would remain independently answerable.

        An ALREADY-ADMITTED connection is refused outright. Connection
        identity is immutable once established: admitting K1 and then
        letting K2 re-handshake on the same socket would be a
        protocol-supported key-change hatch sitting next to the binding
        that exists to prevent exactly that. Authenticating again is
        legitimate -- on a new connection, with a fresh transcript, after a
        disconnect has cleared this one.
        """
        if conn_id in self._admitted:
            _log.warning(
                "admission: refusing a second hello on %s -- already "
                "admitted as %s; reconnect to authenticate again",
                conn_id, self._admitted[conn_id].hex()[:16])
            return None
        self._expire()
        if (not isinstance(client_nonce, (bytes, bytearray))
                or len(client_nonce) != NONCE_LEN):
            return None
        if len(bytes(joiner_pubkey)) != 32:
            return None
        server_nonce = new_nonce()
        self._pending[conn_id] = _Pending(
            client_nonce=bytes(client_nonce), server_nonce=server_nonce,
            joiner_pubkey=bytes(joiner_pubkey), issued_at=self._now())
        return {"client_nonce": bytes(client_nonce).hex(),
                "server_nonce": server_nonce.hex()}

    def on_response(self, conn_id: str, joiner_pubkey: bytes,
                    client_nonce: bytes, server_nonce: bytes,
                    mac: bytes) -> bool:
        """Verify a response. True admits the connection.

        The pending challenge is consumed either way -- accepted or not --
        so one challenge buys exactly one attempt.
        """
        self._expire()
        pending = self._pending.pop(conn_id, None)
        if pending is None:
            return False
        # The response must come from the SAME key that asked. Without this
        # an attacker could let a legitimate joiner start a handshake and
        # then answer it under its own identity, inheriting the seat.
        if bytes(joiner_pubkey) != pending.joiner_pubkey:
            return False
        if (bytes(client_nonce) != pending.client_nonce
                or bytes(server_nonce) != pending.server_nonce):
            return False
        tr = transcript(self._token, self._host_pubkey,
                        pending.joiner_pubkey,
                        pending.client_nonce, pending.server_nonce)
        if not verify_mac(self._secret, tr, mac):
            return False
        self._admitted[conn_id] = pending.joiner_pubkey
        self._admitted_nonces[conn_id] = (pending.client_nonce,
                                          pending.server_nonce)
        return True

    def accept_payload(self, conn_id: str) -> Optional[dict]:
        """The admission_accept body for a just-admitted connection.

        Echoes the nonces of the exchange that admitted it, so the joiner
        can confirm the accept belongs to ITS handshake rather than to some
        other one the host completed.
        """
        nonces = self._admitted_nonces.get(conn_id)
        if nonces is None:
            return None
        return {"client_nonce": nonces[0].hex(),
                "server_nonce": nonces[1].hex()}


class JoinerAdmission:
    """Joiner side: pin the host, prove the capability, verify the accept.

    The pin is the EXACT 32-byte key from the invite. Nothing about the
    connection -- which address answered, which conn_id the transport
    assigned, who spoke first -- contributes to deciding that a peer is the
    host. Under a relay or a hostile LAN, all of those are attacker-chosen.
    """

    def __init__(self, admission_secret: bytes, host_pubkey: bytes,
                 joiner_pubkey: bytes,
                 discovery_token: bytes = b"\x00" * TOKEN_LEN) -> None:
        self._secret = bytes(admission_secret)
        self._host_pubkey = bytes(host_pubkey)
        self._joiner_pubkey = bytes(joiner_pubkey)
        self._token = bytes(discovery_token)
        if len(self._secret) != SECRET_LEN:
            raise ValueError(f"admission_secret must be {SECRET_LEN} bytes")
        if len(self._host_pubkey) != 32 or len(self._joiner_pubkey) != 32:
            raise ValueError("pubkeys must be 32 bytes")
        if len(self._token) != TOKEN_LEN:
            raise ValueError(f"discovery_token must be {TOKEN_LEN} bytes")
        self._client_nonce: Optional[bytes] = None
        self._server_nonce: Optional[bytes] = None
        self.verified_host = False

    def __repr__(self) -> str:
        return (f"<JoinerAdmission host={self._host_pubkey.hex()[:16]} "
                f"verified={self.verified_host} secret=<redacted>>")

    @property
    def host_pubkey(self) -> bytes:
        return self._host_pubkey

    def hello_payload(self) -> dict:
        self._client_nonce = new_nonce()
        self._server_nonce = None
        self.verified_host = False
        return {"client_nonce": self._client_nonce.hex()}

    def on_challenge(self, author_pubkey: bytes, client_nonce: bytes,
                     server_nonce: bytes) -> Optional[dict]:
        """Verify the challenge came from the pinned host; build a response.

        Returns None -- and stays unverified -- if the author is not the
        pinned key or the echoed client_nonce is not the one we sent. The
        author check happens BEFORE any response is produced, so a peer that
        is not the host never receives a MAC at all. (Even if it did, the
        MAC is bound to the pinned host key and would be useless to it, but
        there is no reason to hand it over.)
        """
        if self._client_nonce is None:
            return None
        if bytes(author_pubkey) != self._host_pubkey:
            return None
        if bytes(client_nonce) != self._client_nonce:
            return None
        if (not isinstance(server_nonce, (bytes, bytearray))
                or len(server_nonce) != NONCE_LEN):
            return None
        self._server_nonce = bytes(server_nonce)
        self.verified_host = True
        tr = transcript(self._token, self._host_pubkey, self._joiner_pubkey,
                        self._client_nonce, self._server_nonce)
        return {"client_nonce": self._client_nonce.hex(),
                "server_nonce": self._server_nonce.hex(),
                "mac": compute_mac(self._secret, tr).hex()}

    def on_accept(self, author_pubkey: bytes, client_nonce: bytes,
                  server_nonce: bytes) -> bool:
        """Final check before the joiner reveals anything about itself."""
        if not self.verified_host:
            return False
        if bytes(author_pubkey) != self._host_pubkey:
            return False
        return (bytes(client_nonce) == self._client_nonce
                and bytes(server_nonce) == self._server_nonce)
