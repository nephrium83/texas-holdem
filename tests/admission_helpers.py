"""Complete a real admission handshake, for tests about something else.

Deliberately NOT a bypass. There is no ``mark_admitted`` on HostAdmission
and there should not be: a back door that only tests use is still a back
door, and it would be the one piece of the capability check with no
adversarial coverage. This drives the ordinary public API -- hello,
challenge, response -- exactly as the wire path does, minus the wire.

Suites that exercise relay or seat identity need an admitted connection as
a precondition; they should get one by satisfying the gate, not by
disabling it.
"""
from __future__ import annotations

from holdem.p2p import admission as _adm


def admit(host_admission, conn_id: str, joiner_pubkey: bytes,
          admission_secret: bytes, host_pubkey: bytes,
          discovery_token: bytes = b"\x00" * _adm.TOKEN_LEN) -> None:
    """Run the full exchange so ``conn_id`` becomes admitted. Raises if not."""
    client_nonce = _adm.new_nonce()
    challenge = host_admission.on_hello(conn_id, joiner_pubkey, client_nonce)
    if challenge is None:
        raise AssertionError(f"host refused the hello from {conn_id}")
    server_nonce = bytes.fromhex(challenge["server_nonce"])
    tr = _adm.transcript(bytes(discovery_token), bytes(host_pubkey),
                         bytes(joiner_pubkey), client_nonce, server_nonce)
    mac = _adm.compute_mac(bytes(admission_secret), tr)
    if not host_admission.on_response(conn_id, joiner_pubkey,
                                      client_nonce, server_nonce, mac):
        raise AssertionError(f"host refused a correct response from {conn_id}")
