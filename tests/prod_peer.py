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

The onboarding sequence reproduced here is the one in onboarding.py, in
order, with nothing shortcut:

  host    start_host() -> Session(is_host=True) -> local_conn_id from
          identity.peer_id() -> add_local_player() -> on_message(handle_message)
  joiner  Session(is_host=False) -> connect() -> send a SIGNED player_info
  host    _on_player_info binds ed25519_pubkey_hex from the VERIFIED envelope,
          replies player_ack (which is how a joiner learns the conn_id the
          host filed it under -- production assigns random UUIDs, so a joiner
          cannot know its own id any other way), broadcasts player_list
  host    start_game() -> game_start carries seat_order to every peer
  all     start_p2p_hand() -> the hostless deal begins

Protocol, newline-JSON on stdin/stdout:

  in   {"op": "connect", "addr": "host:port"}
       {"op": "start_game"}                  -- host only
       {"op": "start_hand", "args": {...}}
       {"op": "graph"}                       -- my conn_ids
       {"op": "status"}                      -- session/deal state
       {"op": "broadcast", "msg": {...}}     -- raw, for topology probes
       {"op": "quit"}
  out  {"type": "ready",     "addr": "...", "peer_id": "..."}
       {"type": "connected", "conn_id": "..."}
       {"type": "recv",      "from": "...", "mtype": "...", "seat": N,
                             "author_seq": N}
       {"type": "graph",     "peers": [...]}
       {"type": "status",    ...}
       {"type": "ack",       "op": "..."}
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

from holdem.p2p import identity as _identity            # noqa: E402
from holdem.p2p import transport                        # noqa: E402
from holdem.p2p import wire as _wire                    # noqa: E402
from holdem.p2p.session import Session                  # noqa: E402

_LOCK = threading.Lock()


def _emit(obj: dict) -> None:
    with _LOCK:
        sys.stdout.write(json.dumps(obj, separators=(",", ":"),
                                    default=repr) + "\n")
        sys.stdout.flush()


def _status(sess: Session) -> dict:
    """Everything a three-process assertion might need to see."""
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
        "seat_keys":     {str(k): v[:16] for k, v in
                          getattr(sess, "_seat_keys", {}).items()},
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
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True, choices=["host", "joiner"])
    ap.add_argument("--label", required=True)
    args = ap.parse_args()

    is_host = (args.role == "host")
    sess = Session(is_host=is_host, nickname=args.label, avatar_b64="")

    def _on_msg(conn_id: str, msg: dict) -> None:
        # Report BEFORE handing to the Session, so a message that makes the
        # Session throw is still visible to the test as having arrived.
        payload = msg.get("payload", msg)
        body = payload if isinstance(payload, dict) else {}
        _emit({"type": "recv", "from": conn_id, "mtype": msg.get("type"),
               "seat": body.get("seat", body.get("seat_from")),
               "author_seq": body.get("author_seq")})
        try:
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
        _emit({"type": "ready", "addr": addr, "peer_id": _identity.peer_id()})
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
                cid = transport.connect(cmd["addr"])
                # Exactly what onboarding.py sends: a SIGNED player_info.
                # The host binds this envelope's pubkey to the seat, so an
                # unsigned shortcut here would not exercise binding at all.
                info = _wire.pack("player_info", {
                    "nickname": args.label, "avatar_b64": ""})
                transport.send(cid, json.loads(info))
                _emit({"type": "connected", "conn_id": cid,
                       "addr": cmd["addr"], "outbound": True})
                _emit({"type": "ack", "op": "connect"})
            elif op == "start_game":
                sess.start_game(cmd.get("settings", {}))
                _emit({"type": "ack", "op": "start_game"})
            elif op == "start_hand":
                sess.start_p2p_hand(**cmd["args"])
                _emit({"type": "ack", "op": "start_hand"})
            elif op == "graph":
                with transport._writers_lock:
                    peers = sorted(transport._writers.keys())
                _emit({"type": "graph", "peers": peers})
            elif op == "status":
                _emit(_status(sess))
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
