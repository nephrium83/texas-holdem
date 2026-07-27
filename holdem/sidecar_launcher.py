"""Standalone launcher: runs a local hostless session against AI
opponents and serves the human seat to a Godot client over
holdem.client_server.

The missing piece noted when the Godot client's networking (sidecar
port hand-off), table/betting UI, and main-scene wiring were built:
until now nothing could actually start a sidecar to connect to.
`python -m holdem` still only opens the retired Tkinter GUI.

Design: every seat, human and AI alike, is a real holdem.p2p.session.
Session -- multiple of them sharing one in-process InMemoryBus, the
exact mechanism tests/test_client_server.py already uses to exercise
real hostless hands end to end. This is not a simplified stand-in for
mental poker: every AI seat goes through the same DKG/shuffle/deal/
audit as a real network peer would, and Brain (engine.py's AI) decides
for each AI seat from THAT seat's own local replica engine, which only
ever has that seat's own hole cards recovered -- exactly the no-leak
property the whole crypto stack exists to guarantee. Only the human
seat's Session is wrapped in a ClientServer and exposed on a TCP port.
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

# Hands that leave a next_hand call meaningful (GODOT_PROTOCOL.md
# section 5's continuous-session lifecycle); bots auto-advance on these,
# same as a human clicking Next Hand, so the human is never left
# waiting on a seat with nobody actually driving it.
_ADVANCEABLE_STATES = ("hand_complete", "voided")


def _make_sessions(seats: int):
    """seats real Sessions on one in-memory bus, seat HUMAN_SEAT reserved
    for the client, the rest named as bots. Mirrors
    tests/test_client_server.py's make_sessions() helper."""
    bus = InMemoryBus()
    order = [f"seat{i}" for i in range(seats)]
    sessions = {}
    for i, conn_id in enumerate(order):
        nickname = "You" if i == HUMAN_SEAT else f"Bot {i}"
        session = Session(is_host=(i == 0), nickname=nickname, avatar_b64="",
                          transport=InMemoryTransport(bus, conn_id))
        session.local_conn_id = conn_id
        session._seat_order = list(order)
        bus.register(conn_id, session)
        sessions[conn_id] = session
    return bus, sessions, order


def _wrap_with_drain(session, bus: InMemoryBus) -> None:
    """The human seat's actions arrive through ClientServer ->
    client_view.apply_command(), which calls send_bet_action/
    next_p2p_hand directly and has no reason to know about the bus --
    that plumbing belongs to this launcher, not to client_server.py.
    Wrapping here (rather than editing the shared client_server module)
    keeps that module transport-and-bus-agnostic, matching how it's
    already tested against a real socket with no bus in sight."""
    orig_send_bet_action = session.send_bet_action
    orig_next_p2p_hand = session.next_p2p_hand

    def send_bet_action(action: str, amount: int = 0) -> str:
        result = orig_send_bet_action(action, amount)
        bus.drain()
        return result

    def next_p2p_hand() -> str:
        result = orig_next_p2p_hand()
        bus.drain()
        return result

    session.send_bet_action = send_bet_action
    session.next_p2p_hand = next_p2p_hand


class BotDriver:
    """Drives every non-human seat's Session: acts via Brain when it is
    that seat's turn, and auto-advances next_p2p_hand() once a hand
    settles or voids. Reacts to the session's own on_state_changed hook
    -- the same hook ClientServer chains onto for the human seat -- so
    a bot never needs polling: it only ever runs in response to a real
    state change on its own session.

    Deliberately does NOT call bus.drain() itself: every meaningful
    state change this reacts to is itself delivered FROM an
    already-active drain() call (the initial deal, or the human's own
    wrapped action). InMemoryBus.drain()'s own docstring is explicit
    that its queue-then-flush design exists so a handler that emits
    further messages doesn't need to recurse -- the bot's
    send_bet_action()/next_p2p_hand() call enqueues, and the outer
    drain() loop already in progress picks the new message up on its
    next iteration, in the correct FIFO order. A recursive drain() call
    here was tried first and caused real, reproducible desyncs (~20% of
    hands stalling on a bot seat that never acted) by re-entering the
    bus's queue processing out of order -- exactly the hazard the
    module's docstring warns about."""

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
        snapshot = client_view.snapshot(session)
        turn_state = snapshot.get("turn", {}).get("state")
        if turn_state in _ADVANCEABLE_STATES:
            session.next_p2p_hand()
            return
        if "legal" not in snapshot.get("you", {}):
            return
        replica = session._replica
        if replica is None:
            return
        action, amount = self._brain.decide(replica.engine, session.local_seat)
        if action == "raise":
            session.send_bet_action("raise", amount)
        else:
            session.send_bet_action(action)


def _deal_first_hand(sessions: dict, order: list, bus: InMemoryBus, *,
                     sb: int, bb: int, stack: int, structure: str) -> None:
    """Every peer calls start_p2p_hand with identical shared config
    (session.py's own contract), THEN one drain lets the mental deal
    -- and any bot turns it immediately produces -- run to completion."""
    names = ["You"] + [f"Bot {i}" for i in range(1, len(order))]
    stacks = [stack] * len(order)
    for conn_id in order:
        sessions[conn_id].start_p2p_hand(
            hand_no=1, names=names, stacks=stacks, sb=sb, bb=bb,
            structure=structure, button=0)
    bus.drain()


async def run(*, seats: int, sb: int, bb: int, stack: int, structure: str,
              port: int, seed: int | None) -> None:
    bus, sessions, order = _make_sessions(seats)
    human_conn_id = order[HUMAN_SEAT]
    human_session = sessions[human_conn_id]
    bot_sessions = {cid: s for cid, s in sessions.items() if cid != human_conn_id}

    _wrap_with_drain(human_session, bus)
    BotDriver(bot_sessions, random.Random(seed))

    server = ClientServer(human_session, host="127.0.0.1", port=port)
    await server.start()
    print(f"Sidecar listening on 127.0.0.1:{server.port}")
    print(f"Launch the Godot client with: --sidecar-port={server.port}")

    _deal_first_hand(sessions, order, bus, sb=sb, bb=bb, stack=stack,
                     structure=structure)

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a local hostless Texas Hold'em session against "
                    "AI opponents, served to a Godot client over TCP.")
    parser.add_argument("--seats", type=int, default=4,
                       help="table size including the human seat (default: 4)")
    parser.add_argument("--sb", type=int, default=5, help="small blind")
    parser.add_argument("--bb", type=int, default=10, help="big blind")
    parser.add_argument("--stack", type=int, default=500,
                       help="starting stack per seat")
    parser.add_argument("--structure", default="No-Limit",
                       choices=["No-Limit", "Pot-Limit", "Fixed-Limit"])
    parser.add_argument("--port", type=int, default=0,
                       help="TCP port to listen on (default: 0, OS-assigned)")
    parser.add_argument("--seed", type=int, default=None,
                       help="seed the AI's RNG for reproducible play")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    if args.seats < 2:
        parser.error("--seats must be at least 2")

    logging.basicConfig(level=args.log_level)
    try:
        asyncio.run(run(
            seats=args.seats, sb=args.sb, bb=args.bb, stack=args.stack,
            structure=args.structure, port=args.port, seed=args.seed))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
