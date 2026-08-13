"""The joiner's authentication driver, headless.

Extracted from the Join Game dialog so the SHIPPED ordering is the ordering
under test. While this lived as closures inside a Tk callback it could only
be exercised by driving the GUI, which meant in practice it was not
exercised at all -- and the ordering it enforces is the entire security
property. ``tests/prod_peer.py`` proving the handshake proved a harness.

Ordering, and why each step is where it is
------------------------------------------
    parse invite -> arm(JoinerAdmission, Session) -> register callbacks
    -> connect -> hello -> challenge -> response -> accept
    -> mark_host_authenticated -> player_info -> Ready

Callbacks are registered BEFORE ``connect()``. The transport's reader
starts as part of establishing the connection, so registering afterwards
leaves frames from the far end arriving at either nothing or a Session that
has not been told to distrust them. That race is the thing this work
exists to remove, so it cannot be left to scheduling luck.

The hello is minted only once the FINAL connection exists. Routing may try
direct, then relay, then LAN discovery; those are different sockets. A
challenge issued to an attempt that failed must never be completable on the
attempt that replaced it, so admission state is bound to one conn_id and
traffic on any other is ignored.

This class touches no UI toolkit. It reports outcomes through plain
callables, which the dialog marshals onto the Tk thread -- messages arrive
on the transport's thread, and Tkinter is not thread-safe.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from holdem.p2p import admission as _adm
from holdem.p2p import identity as _identity
from holdem.p2p import wire as _wire

_log = logging.getLogger(__name__)


class JoinAuthenticator:
    """Drives the joiner half of admission for exactly one connection."""

    def __init__(self, transport, session, joiner_admission,
                 nickname: str = "", avatar_b64: str = "",
                 on_authenticated: Optional[Callable] = None,
                 on_failed: Optional[Callable] = None) -> None:
        self._transport = transport
        self._session = session
        self._adm = joiner_admission
        self._nickname = nickname
        self._avatar_b64 = avatar_b64
        self._on_authenticated = on_authenticated
        self._on_failed = on_failed
        #: The ONE connection being authenticated. None until routing settles.
        self.conn_id: Optional[str] = None
        self.player_info_sent = False

    # ------------------------------------------------------------------
    def route(self, conn_id: str, msg: dict) -> None:
        """Transport on_message hook. Handshake here, everything else down."""
        mtype = msg.get("type")
        if mtype in _adm.ADMISSION_TYPES:
            payload = msg.get("payload", msg)
            self._step(conn_id, mtype,
                       payload if isinstance(payload, dict) else {},
                       msg.get("pubkey", ""))
            return
        # Pre-auth traffic is refused by the Session's own gate rather than
        # here, so there is exactly one place that decides what a
        # pre-authenticated joiner may act on.
        self._session.handle_message(conn_id, msg)

    def begin(self, conn_id: str) -> None:
        """Bind to the connection routing produced, and send the hello.

        Called only with a connection that is actually established. Any
        earlier attempt's state is discarded by hello_payload(), which draws
        a fresh client_nonce and clears prior verification.
        """
        self.conn_id = conn_id
        self.player_info_sent = False
        self._transport.send(conn_id, {
            "type": "admission_hello", **self._adm.hello_payload()})

    # ------------------------------------------------------------------
    def _fail(self, reason: str) -> None:
        _log.warning("join: admission failed: %s", reason)
        try:
            if self.conn_id:
                self._transport.disconnect(self.conn_id)
        except Exception:                                # noqa: BLE001
            pass
        if self._on_failed is not None:
            self._on_failed(reason)

    def _step(self, conn_id: str, mtype: str, body: dict,
              author_hex: str) -> None:
        if self.conn_id is None or conn_id != self.conn_id:
            # Belongs to a socket this dialog is not using -- a failed
            # direct attempt cannot complete admission for the relay
            # connection that replaced it.
            return
        try:
            author = bytes.fromhex(author_hex or "")
            client_nonce = bytes.fromhex(body.get("client_nonce", "") or "")
            server_nonce = bytes.fromhex(body.get("server_nonce", "") or "")
        except ValueError:
            self._fail("malformed admission message")
            return

        if mtype == "admission_challenge":
            resp = self._adm.on_challenge(author, client_nonce, server_nonce)
            if resp is None:
                self._fail("the challenge was not signed by the key in the "
                           "room code")
                return
            # Deliberately still pre-authenticated. A valid challenge proves
            # the peer holds the invited host's key; it does NOT prove the
            # host accepted our capability proof. No host hop, no
            # player_info, no Ready until the accept validates the same
            # transcript.
            self._transport.send(conn_id,
                                 {"type": "admission_response", **resp})

        elif mtype == "admission_accept":
            if not self._adm.on_accept(author, client_nonce, server_nonce):
                self._fail("the acceptance did not match this handshake")
                return
            if not self._session.mark_host_authenticated(
                    conn_id, self._adm.host_pubkey):
                self._fail("the acceptance did not match the pinned host key")
                return
            self._send_player_info(conn_id)
            if self._on_authenticated is not None:
                self._on_authenticated(conn_id)

    def _send_player_info(self, conn_id: str) -> None:
        """Our identity, revealed only after the host is proven.

        Signed by this peer's own key, which is the key the host admitted on
        this connection -- the host enforces that they match, so an identity
        sent under a different key would be refused rather than seated.
        """
        info = _wire.pack("player_info", {
            "nickname": self._nickname, "avatar_b64": self._avatar_b64})
        self._transport.send(conn_id, json.loads(info))
        self.player_info_sent = True


def joiner_admission_from_invite(parsed: dict) -> _adm.JoinerAdmission:
    """Build the pin from a parsed V2 invite.

    One place converts invite fields into a capability, so a routing choice
    -- manual address, LAN discovery, STUN, relay -- cannot accidentally
    supply different authentication inputs. Routing decides where to dial;
    it never decides who is trusted.
    """
    return _adm.JoinerAdmission(
        admission_secret=bytes.fromhex(parsed["admission_secret"]),
        host_pubkey=bytes.fromhex(parsed["host_pubkey"]),
        joiner_pubkey=_identity.public_key_bytes(),
        discovery_token=bytes.fromhex(parsed["discovery_token"]),
    )
