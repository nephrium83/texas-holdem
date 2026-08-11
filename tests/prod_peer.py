"""Minimal peer over the PRODUCTION transport, for topology experiments.

tests/peer_worker.py drives SimpleTcpTransport, which is the sidecar/test
transport. This one drives ``holdem.p2p.transport`` -- the module the
application actually uses via onboarding.py -- because the question it
exists to answer is what socket graph the real code builds.

That module keeps its state at module scope (``_loop``, ``_writers``,
``_server``), so exactly one peer can live in a process. Three peers
therefore means three processes, which is why this file exists at all
rather than a fixture.

Protocol, newline-JSON on stdin/stdout, deliberately smaller than
peer_worker's:

  in   {"op": "connect", "addr": "host:port"}
       {"op": "graph"}                      -- report my conn_ids
       {"op": "broadcast", "msg": {...}}
       {"op": "quit"}
  out  {"type": "ready", "addr": "..."}     -- host only
       {"type": "connected", "conn_id": "..."}
       {"type": "graph", "peers": [...]}
       {"type": "recv", "from": "...", "mtype": "..."}
       {"type": "ack", "op": "..."}

No Session, no MentalDeal. This measures delivery, so anything above the
transport would only add ways for the answer to be wrong.
"""
from __future__ import annotations

import argparse
import json
import os as _os
import sys
import threading
import time

sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))

from holdem.p2p import transport                       # noqa: E402

_LOCK = threading.Lock()


def _emit(obj: dict) -> None:
    with _LOCK:
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True, choices=["host", "joiner"])
    ap.add_argument("--label", required=True)
    args = ap.parse_args()

    def _on_msg(conn_id: str, msg: dict) -> None:
        payload = msg.get("payload", msg)
        _emit({"type": "recv", "from": conn_id,
               "mtype": msg.get("type"),
               "seat": payload.get("seat") if isinstance(payload, dict) else None})

    def _on_conn(conn_id: str, address: str) -> None:
        _emit({"type": "connected", "conn_id": conn_id, "addr": address})

    transport.on_message(_on_msg)
    transport.on_connect(_on_conn)

    if args.role == "host":
        addr = transport.start_host(0)
        _emit({"type": "ready", "addr": addr})
    else:
        _emit({"type": "ready", "addr": ""})

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
                _emit({"type": "connected", "conn_id": cid,
                       "addr": cmd["addr"], "outbound": True})
                _emit({"type": "ack", "op": "connect"})
            elif op == "graph":
                with transport._writers_lock:
                    peers = sorted(transport._writers.keys())
                _emit({"type": "graph", "peers": peers})
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
