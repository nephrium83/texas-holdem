"""One PRODUCTION peer: real Session, real transport, real onboarding flow.

This drives ``holdem.p2p.transport`` -- the module the application actually
uses via onboarding.py -- rather than SimpleTcpTransport, and it now hosts a
real ``holdem.p2p.session.Session`` rather than measuring raw delivery.

Why a real Session, and why three processes
-------------------------------------------
The in-memory star-bus tests prove the relay LOGIC. They cannot prove that
production onboarding, the roster, seat-key binding, the signed transport,
Session and the relay compose correctly, because they replace four of those
six with a bus. The ``_hostless_body`` defect fixed in #30 lived exactly in
that gap: correct on the flat in-memory form, wrong on the enveloped wire
form, and invisible to every test that never built an envelope.

``holdem.p2p.transport`` keeps ``_loop``, ``_writers`` and ``_server`` at
module scope, so exactly one peer can live in a process. Three peers is
therefore three processes.

Each process must also have its OWN ``HOLDEM_CONFIG_DIR``: the signing key
is loaded from it at import, so peers sharing one directory share one
identity and every seat binds to the same key -- not the deployment shape.
The test harness (test_three_peer_topology.Peer) gives each peer its own.

The onboarding sequence reproduced here is the one in onboarding.py, in
order, with nothing shortcut:

  host    start_host() -> Session(is_host=True) -> local_conn_id from
          identity.peer_id() -> add_local_player() -> on_message(handle_message)
  joiner  Session(is_host=False, joiner_admission=pin from the invite)
          -> on_message(JoinAuthenticator.route) -> connect()
          -> JoinAuthenticator.begin(): the SHIPPED joiner half of admission,
          which sends a SIGNED player_info only once the host is proven
  host    _on_player_info binds ed25519_pubkey_hex from the VERIFIED envelope,
          replies player_ack (which is how a joiner learns the conn_id the
          host filed it under -- production assigns random UUIDs, so a joiner
          cannot know its own id any other way), broadcasts player_list
  host    start_game() -> game_start carries seat_order to every peer
  all     start_p2p_hand() -> the hostless deal begins (start_hand, or from
          on_game_start once armed, as the sidecar starts it)
  all     act / next drive betting and continuous play hand after hand

Protocol, newline-JSON on stdin/stdout:

  in   {"op": "connect", "addr": "host:port"}
       {"op": "start_game"}                  -- host only
       {"op": "start_hand", "args": {...}}
       {"op": "arm_hand", "args": {...}}     -- start_hand on game_start
       {"op": "act", "action": "fold"|"call"|"raise", "amount": N}
       {"op": "next"}                        -- next_p2p_hand()
       {"op": "graph"}                       -- my conn_ids
       {"op": "status"}                      -- session/deal state
       {"op": "broadcast", "msg": {...}}     -- raw, for topology probes
       {"op": "quit"}
  out  {"type": "ready",     "addr": "...", "peer_id": "..."}
       {"type": "connected", "conn_id": "..."}
       {"type": "admission", "conn_id": "...", "admitted": bool}  -- joiner
       {"type": "recv",      "from": "...", "mtype": "...", "seat": N,
                             "hand": N, "author_seq": N}
       {"type": "graph",     "peers": [...]}
       {"type": "status",    ...}
       {"type": "ack",       "op": "...", "verdict": "..."}  -- act/next
       {"type": "error",     "msg": "..."}
"""
from __future__ import annotations

import argparse
import json
import os as _os
import sys
import threading
import time

sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))

from holdem.p2p import admission as _adm                # noqa: E402
from holdem.p2p import identity as _identity            # noqa: E402
from holdem.p2p import invite as _invite                # noqa: E402
from holdem.p2p import join_auth as _join_auth          # noqa: E402
from holdem.p2p import transport                        # noqa: E402
from holdem.p2p.session import Session                  # noqa: E402

_LOCK = threading.Lock()


def _emit(obj: dict) -> None:
    with _LOCK:
        sys.stdout.write(json.dumps(obj, separators=(",", ":"),
                                    default=repr) + "\n")
        sys.stdout.flush()


def _status(sess: Session, host_admission=None) -> dict:
    """Everything a three-process assertion might need to see.

    Read as the session owner: the dispatch consumer mutates the replica
    while this runs on the stdin thread, and a digest taken halfway through
    an action would report a state no peer was ever in.
    """
    with sess._owner:
        return _status_owned(sess, host_admission)


def _status_owned(sess: Session, host_admission) -> dict:
    driver = getattr(sess, "_deal_driver", None)
    deal = getattr(driver, "deal", None)
    replica = getattr(sess, "_replica", None)
    try:
        local_seat = sess.local_seat
    except Exception:                                  # noqa: BLE001
        local_seat = None
    return {
        "type":          "status",
        "state":         sess.state,
        "local_conn_id": sess.local_conn_id,
        "host_conn_id":  getattr(sess, "_host_conn_id", None),
        "local_seat":    local_seat,
        "seat_order":    list(getattr(sess, "_seat_order", [])),
        # FULL keys, not prefixes. These feed the continuity assertion, and
        # comparing 8 bytes there would re-create the 64-bit target V2 was
        # built to eliminate -- in the very test that claims the chain holds.
        "seat_keys":     {str(k): v for k, v in
                          getattr(sess, "_seat_keys", {}).items()},
        # Host only: which Ed25519 key each connection was ADMITTED under.
        # Reported so a test can join the admission layer to the seat
        # freeze rather than trusting the handoff between them.
        "admitted_keys": ({cid: key.hex()
                           for cid, key in host_admission._admitted.items()}
                          if host_admission is not None else {}),
        "hand_no":       getattr(sess, "_hand_no", None),
        "players":       sorted(getattr(sess, "players", {})),
        # .value where the phase is an enum, so assertions compare against a
        # plain string rather than a repr that changes with the class name.
        "deal_phase":    getattr(getattr(deal, "phase", None), "value",
                                 getattr(deal, "phase", None)),
        "keys_in":       len(getattr(deal, "joint_shares", None) or
                             getattr(deal, "_key_shares", None) or []),
        "hole_complete": bool(deal.hole_complete()) if deal is not None
                         and hasattr(deal, "hole_complete") else False,
        "replica_phase": getattr(replica, "phase", None),
        "hand_voided":   bool(getattr(sess, "hand_voided", False)),
        "void_reason":   getattr(sess, "void_reason", None),
        # Betting and settlement, for driving and comparing whole hands.
        # legal is this seat's own menu, present only while it is to act.
        "seq":           getattr(replica, "next_seq", None),
        "actor":         replica.actor if replica is not None else None,
        "button":        replica.button if replica is not None else None,
        "stacks":        replica.stacks if replica is not None else None,
        "digest":        (replica.state_digest() if replica is not None
                          else None),
        "legal":         (replica.engine.legal(local_seat)
                          if replica is not None
                          and replica.phase == "betting"
                          and replica.actor == local_seat else None),
        "settled":       sess.hand_result is not None,
        "result":        sess.hand_result,
        # Seats whose matching hand_settled this peer has admitted for the
        # current hand, itself included: counted only after ingress checked
        # the signature and seat and the digest compared equal. The next
        # hand waits for every dealt seat.
        "agreed":        (sorted(getattr(sess, "_ended_agreed", ()))
                          if getattr(sess, "_ended_hand", None)
                          == getattr(sess, "_hand_no", None) else []),
        "last_settled_stacks": sess.last_settled_stacks,
        "terminal":      sess.terminal_state,
        "terminal_reason": sess.terminal_reason,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True, choices=["host", "joiner"])
    ap.add_argument("--label", required=True)
    ap.add_argument("--invite", default="",
                    help="joiner: the V2 room code to pin the host with")
    args = ap.parse_args()

    is_host = (args.role == "host")

    # The host mints a real V2 invite and stands its admission policy on the
    # secret inside it, exactly as onboarding does. The invite is emitted on
    # stdout so the test can hand it to the joiners -- the harness standing in
    # for a human pasting a code, not a shortcut around the capability: the
    # joiners still have to prove possession of it.
    host_admission = None
    invite_code = ""
    if is_host:
        invite_code = _invite.generate_room_code(
            host_pubkey=_identity.public_key_bytes())
        _parsed = _invite.parse_room_code(invite_code)
        host_admission = _adm.HostAdmission(
            admission_secret=bytes.fromhex(_parsed["admission_secret"]),
            host_pubkey=_identity.public_key_bytes(),
            discovery_token=bytes.fromhex(_parsed["discovery_token"]))

    # The joiner's pin is built up front from the invite, by the same
    # function onboarding uses, so the Session can be constructed already
    # refusing non-handshake traffic. Building it after connect() would
    # leave a window in which a hostile endpoint could speak first and be
    # believed.
    joiner_admission = None
    if not is_host and args.invite:
        joiner_admission = _join_auth.joiner_admission_from_invite(
            _invite.parse_room_code(args.invite))

    sess = Session(is_host=is_host, nickname=args.label, avatar_b64="",
                   admission=host_admission,
                   joiner_admission=joiner_admission)

    # Neither half of the handshake is this harness's. The host half is
    # Session's; it used to be reimplemented here, which is how the shipped
    # host path came to have no handshake at all while these tests stayed
    # green -- the harness answered on production's behalf. The joiner half
    # is the shipped JoinAuthenticator, wired as onboarding wires it: route()
    # is the message hook, begin() runs once connect() has a socket, and it
    # sends the signed player_info only after the host is proven.
    authenticator = None
    if joiner_admission is not None:
        authenticator = _join_auth.JoinAuthenticator(
            transport=transport, session=sess,
            joiner_admission=joiner_admission, nickname=args.label,
            on_authenticated=lambda cid: _emit(
                {"type": "admission", "conn_id": cid, "admitted": True}),
            on_failed=lambda reason: _emit(
                {"type": "admission", "conn_id": authenticator.conn_id,
                 "admitted": False, "reason": reason}))

    def _on_msg(conn_id: str, msg: dict) -> None:
        # Report BEFORE handing to the Session, so a message that makes the
        # Session throw is still visible to the test as having arrived.
        payload = msg.get("payload", msg)
        body = payload if isinstance(payload, dict) else {}
        _emit({"type": "recv", "from": conn_id, "mtype": msg.get("type"),
               "seat": body.get("seat", body.get("seat_from")),
               "hand": body.get("hand"),
               "author_seq": body.get("author_seq")})
        try:
            if authenticator is not None:
                authenticator.route(conn_id, msg)
            else:
                sess.handle_message(conn_id, msg)
        except Exception as exc:                       # noqa: BLE001
            _emit({"type": "error", "msg": f"handle_message: {exc!r}"})

    def _on_conn(conn_id: str, address: str) -> None:
        _emit({"type": "connected", "conn_id": conn_id, "addr": address})

    transport.reset_callbacks()
    transport.on_message(_on_msg)
    transport.on_connect(_on_conn)
    transport.on_disconnect(lambda cid: sess.handle_disconnect(cid))

    if is_host:
        # Listener first, exactly as onboarding.py orders it: add_local_player
        # broadcasts a roster update, and doing it before the loop exists
        # logs "transport is not running" for a send that had no recipients
        # anyway. Harmless, but a spurious warning in a harness whose job is
        # to surface real ones.
        addr = transport.start_host(0)
        # H-12: the host files itself under a stable id derived from its own
        # key, not from a connection -- it has no inbound connection to be
        # named by. Joiners get UUIDs and learn theirs via player_ack.
        sess.local_conn_id = _identity.peer_id()
        sess.add_local_player(sess.local_conn_id)
        _emit({"type": "ready", "addr": addr,
               "peer_id": _identity.peer_id(),
               "invite": invite_code})
    else:
        _emit({"type": "ready", "addr": "", "peer_id": _identity.peer_id()})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        op = cmd.get("op")
        try:
            if op == "connect":
                if authenticator is None:
                    raise RuntimeError("a joiner needs --invite to connect")
                cid = transport.connect(cmd["addr"])
                # player_info is NOT sent here. begin() sends only the
                # hello; identity goes out once admission_accept verifies
                # against the pinned host key, never to whoever happened to
                # answer the socket.
                _emit({"type": "connected", "conn_id": cid,
                       "addr": cmd["addr"], "outbound": True})
                authenticator.begin(cid)
                _emit({"type": "ack", "op": "connect"})
            elif op == "start_game":
                sess.start_game(cmd.get("settings", {}))
                _emit({"type": "ack", "op": "start_game"})
            elif op == "start_hand":
                sess.start_p2p_hand(**cmd["args"])
                _emit({"type": "ack", "op": "start_hand"})
            elif op == "arm_hand":
                # Deal the first hand from on_game_start, as the sidecar
                # does (_wire_hand_start): game_start is handled before any
                # later frame from the host, so seat keys are bound before
                # the first deal message can arrive. A first hand started
                # from stdin races it, and a hostless message that beats
                # the binding is refused rather than buffered.
                def _deal(payload, _args=dict(cmd["args"])):
                    sess.start_p2p_hand(**_args)
                sess.on_game_start = _deal
                _emit({"type": "ack", "op": "arm_hand"})
            elif op == "act":
                verdict = sess.send_bet_action(cmd["action"],
                                               int(cmd.get("amount", 0)))
                _emit({"type": "ack", "op": "act", "verdict": verdict})
            elif op == "next":
                _emit({"type": "ack", "op": "next",
                       "verdict": sess.next_p2p_hand()})
            elif op == "graph":
                with transport._writers_lock:
                    peers = sorted(transport._writers.keys())
                _emit({"type": "graph", "peers": peers})
            elif op == "status":
                _emit(_status(sess, host_admission))
            elif op == "broadcast":
                transport.broadcast(cmd["msg"])
                _emit({"type": "ack", "op": "broadcast"})
            elif op == "quit":
                _emit({"type": "ack", "op": "quit"})
                break
            else:
                _emit({"type": "error", "msg": f"unknown op {op}"})
        except Exception as exc:                       # noqa: BLE001
            _emit({"type": "error", "op": op, "msg": repr(exc)})

    try:
        transport.stop(timeout=3.0)
    except Exception:                                  # noqa: BLE001
        pass
    time.sleep(0.1)


if __name__ == "__main__":
    main()
