#!/usr/bin/env python3
"""
Two-process test worker.

Runs one holdem.p2p.session.Session over a SimpleTcpTransport,
driven by newline-delimited JSON commands on stdin, emitting
newline-delimited JSON events on stdout.

Usage
-----
# Host (binds, waits for one peer):
  python tests/peer_worker.py --role host [--port PORT]

# Guest (connects to host):
  python tests/peer_worker.py --role guest --peer-port PORT

Stdout protocol (one JSON object per line)
------------------------------------------
  {"type": "ready",     "port": N}            -- host only: listening on port N
  {"type": "connected", "peer": "<id>"}       -- peer accepted / connect complete
  {"type": "snapshot",  "snap": {...}}        -- emitted on every on_state_changed
  {"type": "state_event", ...}               -- from the session's event sink
  {"type": "ack",       "op": "<op>"}        -- command received and executed

Stdin protocol (one JSON object per line)
-----------------------------------------
  {"op": "start_hand", "args": {hand_no, names, stacks, sb, bb, structure, button}}
  {"op": "action", "action": "fold|call|check|raise", "amount": 0}
  {"op": "next_hand"}
  {"op": "quit"}
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time

# Ensure the repo root is importable when the script is run directly.
import os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))

from holdem.p2p.tcp_transport import SimpleTcpTransport
from holdem.p2p.session import Session
from holdem.p2p.events import EventSink
from holdem import client_view


# ── stdout helpers ────────────────────────────────────────────────────────────

def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


# ── command-path tracing ─────────────────────────────────────────────────────
#
# PR #22's watchdog caught the two-process stall and reported:
#
#   HOST   alive=True start_hand_ack=False phases_seen=[] stderr_bytes=0
#   GUEST  alive=True start_hand_ack=True  phases_seen=['dealing']
#
# The host never acknowledged a start_hand its own stdin was handed, while
# the guest processed the identical command. That narrows the failure to
# the host's command path but does not say WHERE on it.
#
# These traces are emitted as ordinary events, so the existing collector
# and the watchdog's event tail pick them up with no new plumbing. Each
# carries seconds since process start. The stages split the five
# possibilities that are otherwise indistinguishable:
#
#   no  stdin_open                      -> reader thread never started
#   stdin_open, no stdin_line           -> reader blocked despite input
#                                          (or bytes never arrived)
#   stdin_line, no cmd_queued           -> read but not parsed/queued
#   cmd_queued, no cmd_dequeued         -> queued, consumer never got to it
#   cmd_dispatch, no ack                -> dispatch began and blocked
#
# msg_dequeued without a matching msg_done additionally identifies the
# single consumer being stuck inside session.handle_message, which is the
# leading hypothesis: the guest is told to start first, so its deal
# messages can fill the host's queue ahead of the host's own start_hand.

_T0 = time.monotonic()


def _trace(stage: str, **fields) -> None:
    _emit({"type": "trace", "stage": stage,
           "t": round(time.monotonic() - _T0, 3), **fields})


# ── event sink that writes to stdout ─────────────────────────────────────────

class WorkerEventSink(EventSink):
    def emit(self, event: dict) -> None:
        _emit(event)


# ── main ─────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--role",      required=True, choices=["host", "guest"])
    p.add_argument("--port",      type=int, default=0,
                   help="port for host to listen on (0=auto)")
    p.add_argument("--peer-port", type=int, default=0, dest="peer_port",
                   help="port to connect to (guest only)")
    p.add_argument("--conn-id",   default=None, dest="conn_id",
                   help="local conn_id (defaults to role name)")
    p.add_argument("--seats",     type=int, default=2)
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    conn_id = args.conn_id or args.role
    is_host = (args.role == "host")

    # Build seat_order: host always index 0, guest index 1.
    seat_order = ["host", "guest"] if args.seats == 2 else \
                 [f"seat{i}" for i in range(args.seats)]

    # Queue serialises all session interactions on one thread.
    # ("msg", peer_id, msg_dict)  -- from TCP reader
    # ("cmd", cmd_dict)           -- from stdin
    evt_queue: queue.Queue = queue.Queue()

    # ── transport ──────────────────────────────────────────────────────
    transport = SimpleTcpTransport(conn_id)

    # ── session ────────────────────────────────────────────────────────
    session = Session(
        is_host=is_host,
        nickname=conn_id.capitalize(),
        avatar_b64="",
        transport=transport,
        sink=WorkerEventSink(),
    )
    session.local_conn_id = conn_id
    session.configure_seats(seat_order)
    session._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)

    # Emit a snapshot on every state change.
    def _on_state_changed() -> None:
        try:
            snap = client_view.snapshot(session)
            _emit({"type": "snapshot", "snap": snap})
        except Exception as exc:
            _emit({"type": "error", "msg": str(exc)})

    session.on_state_changed = _on_state_changed

    # Deliver TCP messages through the queue so session methods are
    # only called from the main thread (avoids concurrency in session.py).
    def _on_tcp_message(peer_id: str, msg: dict) -> None:
        evt_queue.put(("msg", peer_id, msg))

    # Monkey-patch transport to queue messages instead of calling directly.
    _orig_start_reader = transport._start_reader

    def _queued_reader(peer_id: str, sock, leftover: bytes = b"") -> None:
        """Queueing stand-in for SimpleTcpTransport._start_reader.

        ``leftover`` is whatever the handshake's recv() pulled in past the
        hello's newline. It MUST be consumed here: this override shadows
        the production reader entirely, so a copy that ignores it
        reintroduces the dropped-frame defect inside the worker while the
        real transport is fixed.
        """
        def _read() -> None:
            import json as _json
            f = sock.makefile("rb")
            carry = leftover
            try:
                while True:
                    if b"\n" in carry:
                        line, _, carry = carry.partition(b"\n")
                        raw = line
                    else:
                        chunk = f.readline()
                        if not chunk:
                            break
                        raw = carry + chunk
                        carry = b""
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        msg = _json.loads(raw)
                    except _json.JSONDecodeError:
                        continue
                    evt_queue.put(("msg", peer_id, msg))
            except Exception:
                pass

        threading.Thread(target=_read, daemon=True,
                         name=f"reader-{peer_id}").start()

    transport._start_reader = _queued_reader

    # ── connect ────────────────────────────────────────────────────────
    if is_host:
        port = transport.listen(args.port)
        _emit({"type": "ready", "port": port})
    else:
        if not args.peer_port:
            sys.exit("--peer-port required for guest role")
        transport.connect("127.0.0.1", args.peer_port)

    # Wait for handshake (connection established by _handshake → queued_reader)
    if not transport.wait_connected(timeout=15.0):
        sys.exit("timed out waiting for peer connection")

    # Determine peer conn_id from transport's peer map.
    with transport._writers_lock:
        peer_ids = list(transport._peers.keys())
    peer_id = peer_ids[0] if peer_ids else "unknown"
    _emit({"type": "connected", "peer": peer_id})

    # ── stdin reader thread ────────────────────────────────────────────
    def _stdin_reader() -> None:
        _trace("stdin_open")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            _trace("stdin_line", n=len(line))
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                _trace("stdin_undecodable")
                continue
            evt_queue.put(("cmd", cmd))
            _trace("cmd_queued", op=cmd.get("op"), depth=evt_queue.qsize())
        _trace("stdin_eof")

    threading.Thread(target=_stdin_reader, daemon=True,
                     name="stdin-reader").start()

    # ── main loop ──────────────────────────────────────────────────────
    _stalled = False
    while True:
        try:
            item = evt_queue.get(timeout=60)
        except queue.Empty:
            _emit({"type": "error", "msg": "idle timeout"})
            break

        if item[0] == "msg":
            _, peer, msg = item
            # msg_dequeued without msg_done means the single consumer is
            # stuck inside handle_message and every queued command behind
            # it -- including start_hand -- is starved.
            _trace("msg_dequeued", mtype=msg.get("type"),
                   depth=evt_queue.qsize())
            # Defect injection for tests only: stall the single consumer
            # inside message handling, which is what starves a queued
            # command. Never set in normal runs.
            _stall = _os.environ.get("PEER_WORKER_STALL_MS")
            if _stall and not _stalled:
                _stalled = True
                time.sleep(int(_stall) / 1000.0)
            try:
                session.handle_message(peer, msg)
            except Exception as exc:
                _emit({"type": "error", "msg": f"handle_message: {exc}"})
            _trace("msg_done", mtype=msg.get("type"))

        elif item[0] == "cmd":
            _, cmd = item
            op = cmd.get("op")
            _trace("cmd_dispatch", op=op, depth=evt_queue.qsize())
            try:
                if op == "start_hand":
                    session.start_p2p_hand(**cmd["args"])
                    _emit({"type": "ack", "op": "start_hand"})
                elif op == "action":
                    session.send_bet_action(
                        cmd["action"], cmd.get("amount", 0))
                    _emit({"type": "ack", "op": "action"})
                elif op == "next_hand":
                    session.next_p2p_hand()
                    _emit({"type": "ack", "op": "next_hand"})
                elif op == "quit":
                    _emit({"type": "ack", "op": "quit"})
                    break
                else:
                    _emit({"type": "error", "msg": f"unknown op: {op}"})
            except Exception as exc:
                _emit({"type": "error", "msg": f"{op}: {exc}"})

    transport.close()


if __name__ == "__main__":
    main()
