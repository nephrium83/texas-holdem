"""Headless sidecar launcher — Python backend served to a Godot client.

Starts a Session and a ClientServer, prints the bound port on stdout,
then blocks until SIGTERM / SIGINT.

Usage::

    python -m holdem.sidecar_launcher [options]

Options:
    --port PORT        TCP port for the ClientServer (0 = auto-assign, default)
    --seats N          Number of seats at the table (2-9, default 2)
    --small-blind SB   Small blind amount in chips (default 25)
    --big-blind BB     Big blind amount in chips (default 50)
    --stack CHIPS      Starting stack per seat (default 1000)
    --nickname NAME    Display name for the local seat (default "Player")

On startup the launcher prints exactly one line to stdout::

    SIDECAR_PORT:<n>

then accepts incoming Godot client connections until terminated.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

_log = logging.getLogger(__name__)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m holdem.sidecar_launcher",
        description="Texas Hold'em headless sidecar for a Godot client.",
    )
    p.add_argument("--port",        type=int, default=0,        metavar="PORT")
    p.add_argument("--seats",       type=int, default=2,        metavar="N")
    p.add_argument("--small-blind", type=int, default=25,       metavar="SB",
                   dest="small_blind")
    p.add_argument("--big-blind",   type=int, default=50,       metavar="BB",
                   dest="big_blind")
    p.add_argument("--stack",       type=int, default=1000,     metavar="CHIPS")
    p.add_argument("--nickname",    type=str, default="Player", metavar="NAME")
    args = p.parse_args(argv)

    # Validate — fail with a clear message before touching any game code.
    if args.seats < 2:
        p.error("--seats: minimum 2")
    if args.seats > 9:
        p.error("--seats: maximum 9 (standard table)")
    if args.small_blind <= 0:
        p.error("--small-blind: must be positive")
    if args.big_blind <= args.small_blind:
        p.error("--big-blind: must exceed small blind")
    if args.stack < args.big_blind:
        p.error("--stack: must cover at least one big blind")
    if not (0 <= args.port <= 65535):
        p.error("--port: must be 0–65535")

    return args


async def _run(argv=None):
    args = _parse_args(argv)

    # Build a solo session backed by an in-memory transport (no real network).
    # In a full multiplayer session each peer runs its own sidecar process;
    # here a single process hosts a solo practice seat.
    from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
    from holdem.p2p.session import Session
    from holdem.client_server import ClientServer

    bus = InMemoryBus()
    local_id = "local"
    transport = InMemoryTransport(bus, local_id)

    session = Session(
        is_host=True,
        nickname=args.nickname,
        avatar_b64="",
        transport=transport,
    )
    session.local_conn_id = local_id
    bus.register(local_id, session)
    session.add_local_player(local_id)

    # Check whether the requested port is already in use before starting the
    # ClientServer, so the error message stays comprehensible.
    if args.port != 0:
        import socket as _socket
        try:
            probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", args.port))
            probe.close()
        except OSError:
            print(
                f"error: port {args.port} is already in use",
                file=sys.stderr,
            )
            sys.exit(1)


    server = ClientServer(session, host="127.0.0.1", port=args.port)
    await server.start()

    # Announce the bound port so the parent process (or test harness) can
    # connect without a race on port discovery.
    print(f"SIDECAR_PORT:{server.port}", flush=True)
    _log.info("sidecar ready on port %d", server.port)

    # Block until cancelled (SIGTERM/SIGINT on Unix; KeyboardInterrupt on
    # Windows, which asyncio converts to CancelledError on the main task).
    stop = asyncio.get_running_loop().create_future()

    def _shutdown():
        if not stop.done():
            stop.set_result(None)

    loop = asyncio.get_running_loop()
    try:
        import signal
        loop.add_signal_handler(signal.SIGTERM, _shutdown)
        loop.add_signal_handler(signal.SIGINT,  _shutdown)
    except (NotImplementedError, AttributeError):
        # Windows: asyncio doesn't support add_signal_handler;
        # KeyboardInterrupt propagates naturally.
        pass

    try:
        await stop
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()
        _log.info("sidecar stopped")


def main(argv=None):
    """Entry point for ``python -m holdem.sidecar_launcher``."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(_run(argv))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
