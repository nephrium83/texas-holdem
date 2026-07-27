"""Sidecar launcher: runs a local hostless session against AI opponents
and serves the human seat to a Godot client over holdem.client_server.

Each seat -- human and AI alike -- is a real holdem.p2p.session.Session
sharing one in-process InMemoryBus.  Only the human seat's Session is
wrapped in a ClientServer exposed on a TCP port.

On startup the launcher prints one machine-readable line to stdout::

    SIDECAR_PORT:<n>

and then one human-readable line::

    Sidecar ready on 127.0.0.1:<n>

The sidecar starts in lobby phase (no hand in progress).  The Godot
client connects, reads the lobby snapshot, and triggers game start via
the protocol command defined in GODOT_PROTOCOL.md.  BotDriver wires
all non-human seats and auto-advances them once a hand is in progress.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random

from holdem import client_view
from holdem.client_server import ClientServer
from holdem.engine import Brain
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Session

_log = logging.getLogger(__name__)

HUMAN_SEAT = 0

# States where next_p2p_hand() is meaningful.
_ADVANCEABLE_STATES = ("hand_complete", "voided")


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

def _make_sessions(seats: int):
    """Create *seats* real Sessions on one in-memory bus.

    Seat HUMAN_SEAT is reserved for the Godot client; the rest become bots.
    Mirrors tests/test_client_server.py's make_sessions() helper.
    """
    bus = InMemoryBus()
    order = [f"seat{i}" for i in range(seats)]
    sessions = {}
    for i, conn_id in enumerate(order):
        nickname = "You" if i == HUMAN_SEAT else f"Bot {i}"
        session = Session(
            is_host=(i == 0),
            nickname=nickname,
            avatar_b64="",
            transport=InMemoryTransport(bus, conn_id),
        )
        session.local_conn_id = conn_id
        session.configure_seats(order)
        bus.register(conn_id, session)
        sessions[conn_id] = session
    return bus, sessions, order


# ---------------------------------------------------------------------------
# Drain wrapper
# ---------------------------------------------------------------------------

def _wrap_with_drain(session, bus: InMemoryBus) -> None:
    """Wrap the human seat's action methods so every call drains the bus.

    ClientServer calls send_bet_action / next_p2p_hand directly.  Neither
    knows about the bus, and that's correct -- keeping ClientServer
    bus-agnostic means it can be tested against a real socket with no bus.
    The drain coupling lives here, in the launcher that wires them together.
    """
    orig_send_bet_action = session.send_bet_action
    orig_next_p2p_hand   = session.next_p2p_hand

    def send_bet_action(action: str, amount: int = 0) -> str:
        result = orig_send_bet_action(action, amount)
        bus.drain()
        return result

    def next_p2p_hand() -> str:
        result = orig_next_p2p_hand()
        bus.drain()
        return result

    session.send_bet_action = send_bet_action
    session.next_p2p_hand   = next_p2p_hand


# ---------------------------------------------------------------------------
# Hand starter (test / integration helper)
# ---------------------------------------------------------------------------

def _deal_first_hand(sessions: dict, order: list, bus: InMemoryBus, *,
                     sb: int, bb: int, stack: int,
                     structure: str = "No-Limit") -> None:
    """Start hand 1 on every seat and drive the bus to quiescence.

    Every seat calls ``start_p2p_hand`` with the same shared parameters
    (the same discipline the Godot client uses when it triggers game-start
    for a multi-seat table), then ``bus.drain()`` delivers the mental-poker
    deal messages until the queue is empty.

    NOT called by the production ``run()`` path -- the Godot client
    triggers the first hand.  Used by tests and the two-process harness.
    """
    names  = [sessions[cid].local_nickname for cid in order]
    stacks = [stack] * len(order)
    for cid in order:
        sessions[cid].start_p2p_hand(
            hand_no=1, names=list(names), stacks=list(stacks),
            sb=sb, bb=bb, structure=structure, button=0,
        )
    bus.drain()


# ---------------------------------------------------------------------------
# Bot driver
# ---------------------------------------------------------------------------

class BotDriver:
    """Drives every non-human seat via Brain AI and on_state_changed hooks.

    Deliberately does NOT call bus.drain() itself: every reaction it triggers
    is nested inside an already-active drain() call from the human seat.
    InMemoryBus.drain() is designed to pick up newly-enqueued messages on
    the next loop iteration, so a recursive drain() here would re-enter
    the queue out of order and cause desyncs (~20 % of hands in testing).
    """

    def __init__(self, bot_sessions: dict, rng: random.Random):
        self._brain = Brain(rng)
        for session in bot_sessions.values():
            self._chain_hook(session)

    def _chain_hook(self, session) -> None:
        previous_hook = session.on_state_changed

        def hook():
            if previous_hook is not None:
                previous_hook()
            self._react(session)

        session.on_state_changed = hook

    def _react(self, session) -> None:
        snapshot  = client_view.snapshot(session)
        turn_state = snapshot.get("turn", {}).get("state")
        if turn_state in _ADVANCEABLE_STATES:
            session.next_p2p_hand()
            return
        if "legal" not in snapshot.get("you", {}):
            return
        replica = session.replica
        if replica is None:
            return
        action, amount = self._brain.decide(replica.engine, session.local_seat)
        if action == "raise":
            session.send_bet_action("raise", amount)
        else:
            session.send_bet_action(action)


# ---------------------------------------------------------------------------
# async entry point
# ---------------------------------------------------------------------------

async def run(*, seats: int, sb: int, bb: int, stack: int, structure: str,
              port: int, seed: int | None, nickname: str) -> None:
    bus, sessions, order = _make_sessions(seats)
    human_conn_id = order[HUMAN_SEAT]
    human_session = sessions[human_conn_id]
    human_session.local_nickname = nickname
    bot_sessions  = {cid: s for cid, s in sessions.items()
                     if cid != human_conn_id}

    _wrap_with_drain(human_session, bus)
    BotDriver(bot_sessions, random.Random(seed))

    server = ClientServer(human_session, host="127.0.0.1", port=port)
    await server.start()

    # Machine-readable port announcement (parsed by tests and by Godot launcher).
    print(f"SIDECAR_PORT:{server.port}", flush=True)
    _log.info("sidecar ready on 127.0.0.1:%d", server.port)

    # The sidecar starts in lobby phase.  The Godot client triggers game
    # start via the protocol; BotDriver takes over once a hand begins.
    # _deal_first_hand is available for callers that want immediate play
    # (e.g. the two-process test harness).

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()
        _log.info("sidecar stopped")


# ---------------------------------------------------------------------------
# Argument parsing and validation
# ---------------------------------------------------------------------------

def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m holdem.sidecar_launcher",
        description="Texas Hold'em sidecar for the Godot client.",
    )
    p.add_argument("--seats",       type=int, default=2,    metavar="N")
    # Accept both long form (tests, docs) and short form (CLI convenience).
    p.add_argument("--small-blind", "--sb",
                   type=int, default=25, metavar="SB", dest="small_blind")
    p.add_argument("--big-blind",   "--bb",
                   type=int, default=50, metavar="BB", dest="big_blind")
    p.add_argument("--stack",       type=int, default=1000, metavar="CHIPS")
    p.add_argument("--structure", default="No-Limit",
                   choices=["No-Limit", "Pot-Limit", "Fixed-Limit"])
    p.add_argument("--port",        type=int, default=0,    metavar="PORT")
    p.add_argument("--seed",        type=int, default=None, metavar="SEED")
    p.add_argument("--nickname",    type=str, default="Player", metavar="NAME")
    p.add_argument("--log-level",   default="WARNING",      dest="log_level")
    args = p.parse_args(argv)

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
        p.error("--port: must be 0-65535")

    return args


def main(argv=None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(
            seats=args.seats,
            sb=args.small_blind,
            bb=args.big_blind,
            stack=args.stack,
            structure=args.structure,
            port=args.port,
            seed=args.seed,
            nickname=args.nickname,
        ))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
