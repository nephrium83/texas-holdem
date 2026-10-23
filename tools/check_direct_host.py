"""Bounded direct-host diagnostic; real router changes require an explicit flag.

The normal desktop host/controller and authenticated TCP listener are used.
No invite is printed; optional private coordination stays in the new run folder.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--simulate-router", action="store_true")
    mode.add_argument("--allow-router-mapping", action="store_true",
                      help="Create/renew/remove this game's temporary TCP router mapping")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 300:
        parser.error("--seconds must be between 1 and 300")
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    # Set before importing host services; the test never uses a player's identity.
    os.environ["HOLDEM_CONFIG_DIR"] = str(run_dir / "private-config")
    sys.path.insert(0, str(ROOT))
    from holdem.lan_session import LanTable
    from holdem.p2p import invite, transport

    class SimulatedLease:
        def __init__(self, _ip, port, **_kwargs):
            self.external_address = ("127.0.0.1", port)

        def close(self):
            pass

    if args.simulate_router:
        async def no_stun():
            return None
        transport._resolve_stun = no_stun
    table = LanTable("Connection check", {}, mapping_factory=(
        SimulatedLease if args.simulate_router else None))
    report = dict(ok=False, simulated=args.simulate_router,
                  requested_seconds=args.seconds, error="")
    started = time.monotonic()
    try:
        table.host(internet=True)
        state = table.lobby_state()
        parsed = invite.parse_room_code(state["invite"])
        assert state["network"] == "internet" and state["state"] == "lobby"
        assert parsed["flags"] & invite.FLAG_DIRECT_INTERNET
        report["advertised_address"] = [parsed["public_ip"], parsed["public_port"]]
        # Private file, excluded from public reports/artifacts. No CLI argument
        # or stdout value ever contains the admission capability.
        (run_dir / "private-invite.txt").write_text(state["invite"], encoding="utf-8")
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            if table.lobby_state()["state"] == "closed":
                raise RuntimeError("The direct connection closed during its lease check")
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        report["ok"] = True
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = str(exc) or type(exc).__name__
    finally:
        table.close()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["transport_stopped"] = transport.event_loop() is None
        report["peer_sockets_closed"] = not transport.peer_ids()
        report["ok"] = (report["ok"] and report["transport_stopped"]
                        and report["peer_sockets_closed"])
        (run_dir / "report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
