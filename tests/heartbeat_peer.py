"""One peer that runs ONLY ``holdem.p2p.transport``, for the pause tests.

Why a separate harness
----------------------
The heartbeat is a transport property, and the question the process tests
ask -- does a peer whose machine stops running notice its partner is
gone? -- cannot be asked in one process. A suspended process has no event
loop, so there is no way to fake it from inside the peer that must detect
it; and it cannot be asked through ``tests/prod_peer.py`` either, because
that peer stands up a whole Session and would answer "something in the
pipeline reported a loss" rather than "the transport dropped a silent
socket".

So this peer imports the transport and nothing else from the P2P stack.
It holds no Session, no admission policy, and no game state. Every frame
it reports is one that reached an ``on_message`` callback, which is
exactly the boundary the heartbeat must stay below.

Each process gets its own HOLDEM_CONFIG_DIR, set before the first holdem
import because ``identity`` loads or generates this machine's Ed25519 key
at import time -- two peers on one machine sharing one key would make
every signature mutually verifiable for the wrong reason.

Protocol, newline-JSON on stdin/stdout. Every line out carries ``t``,
seconds since this process started, so a test can report WHEN something
happened and not merely that it did.

  in   {"op": "send", "msg": {...}}   -- broadcast a signed frame
       {"op": "peers"}                -- who is still connected
       {"op": "quit"}
  out  {"type": "ready",        "addr": "host:port", "pid": N,
                                "heartbeat": 5.0, "silence": 20.0}
       {"type": "dialed",       "conn_id": "...", "addr": "..."}
       {"type": "connected",    "conn_id": "...", "addr": "..."}
       {"type": "disconnected", "conn_id": "..."}
       {"type": "recv",         "conn_id": "...", "mtype": "chat"}
       {"type": "peers",        "peers": [...]}
       {"type": "ack",          "op": "..."}
       {"type": "error",        "msg": "..."}

``--no-heartbeat`` turns the whole liveness feature off (no frames out,
no timeout in). It is the deliberate-break control: with both peers
started that way, a paused peer is never dropped, which is what makes the
other tests evidence for the heartbeat rather than for anything else that
might close a socket.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time

_T0 = time.monotonic()
_LOCK = threading.Lock()


def _emit(obj: dict) -> None:
    """One JSON line on stdout, stamped and flushed."""
    line = dict(obj, t=round(time.monotonic() - _T0, 3))
    with _LOCK:
        sys.stdout.write(json.dumps(line, separators=(",", ":"),
                                    default=repr) + "\n")
        sys.stdout.flush()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", required=True, choices=["host", "joiner"])
    ap.add_argument("--addr", default="",
                    help="joiner: the host:port to dial")
    ap.add_argument("--config-dir", default="",
                    help="HOLDEM_CONFIG_DIR for this process (default: a "
                         "fresh temp dir)")
    ap.add_argument("--no-heartbeat", action="store_true",
                    help="control: run with no heartbeat and no timeout")
    args = ap.parse_args()

    # Both of these must happen before the holdem import below: the config
    # directory because identity reads it at import time, the path because
    # this harness is launched as a file, not as part of the package.
    os.environ["HOLDEM_CONFIG_DIR"] = (
        args.config_dir or tempfile.mkdtemp(prefix="holdem-heartbeat-"))
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".."))

    from holdem.p2p import transport                    # noqa: E402

    if args.no_heartbeat:
        transport.HEARTBEAT_INTERVAL = None
        transport.SILENCE_TIMEOUT = None

    transport.reset_callbacks()
    transport.on_connect(lambda cid, addr: _emit(
        {"type": "connected", "conn_id": cid, "addr": addr}))
    transport.on_disconnect(lambda cid: _emit(
        {"type": "disconnected", "conn_id": cid}))
    transport.on_message(lambda cid, msg: _emit(
        {"type": "recv", "conn_id": cid, "mtype": msg.get("type")}))

    ready = {"type": "ready", "addr": "", "pid": os.getpid(),
             "heartbeat": transport.HEARTBEAT_INTERVAL,
             "silence": transport.SILENCE_TIMEOUT}
    if args.role == "host":
        ready["addr"] = transport.start_host(0)
    _emit(ready)

    if args.role == "joiner" and args.addr:
        try:
            cid = transport.connect(args.addr)
            _emit({"type": "dialed", "conn_id": cid, "addr": args.addr})
        except Exception as exc:                        # noqa: BLE001
            _emit({"type": "error", "msg": f"connect: {exc!r}"})

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
            if op == "send":
                transport.broadcast(cmd["msg"])
                _emit({"type": "ack", "op": "send"})
            elif op == "peers":
                _emit({"type": "peers", "peers": sorted(transport.peer_ids())})
            elif op == "quit":
                _emit({"type": "ack", "op": "quit"})
                break
            else:
                _emit({"type": "error", "msg": f"unknown op {op!r}"})
        except Exception as exc:                        # noqa: BLE001
            _emit({"type": "error", "op": op, "msg": repr(exc)})

    try:
        transport.stop(timeout=3.0)
    except Exception:                                   # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
