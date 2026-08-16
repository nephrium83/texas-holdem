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
client connects, reads the lobby snapshot, and sends the ``start_game``
command (GODOT_PROTOCOL.md section 4) to start the table this process was
launched with.  That drives the real hostless path: the host broadcasts
game_start, every seat calls start_p2p_hand, and MentalDealDriver runs the
mental-poker deal.  BotDriver wires all non-human seats and auto-advances
them once a hand is in progress.

This docstring previously claimed the client triggered game start "via the
protocol command defined in GODOT_PROTOCOL.md" while no such command
existed, which is how the entire hostless deal path came to have no
reachable production caller.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random

from holdem import client_view
from holdem.client_server import ClientServer
from holdem.engine import Brain
from holdem.p2p.inmemory_transport import (
    DrainLoopError, InMemoryBus, InMemoryTransport,
)
from holdem.p2p.session import Session

_log = logging.getLogger(__name__)

HUMAN_SEAT = 0

# States where next_p2p_hand() is meaningful.
_ADVANCEABLE_STATES = ("hand_complete", "voided")

# Ceiling on retries when delivery keeps failing. Each drain() consumes at
# least one message before it can raise, so this bounds a pathological
# handler that enqueues as fast as it fails; a healthy table settles in one.
_MAX_DRAIN_ATTEMPTS = 64


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

def _make_sessions(seats: int, nickname: str = "You"):
    """Create *seats* real Sessions on one in-memory bus.

    Seat HUMAN_SEAT is reserved for the Godot client; the rest become bots.

    Every session seats the same local table, which populates each roster.
    That matters because Session.start_game() derives the seat order from
    its roster: with only configure_seats() called, players stayed empty and
    the host would broadcast an EMPTY seat order. The roster is built through
    seat_local_table rather than the player_info handshake because these
    seats are local AI, not peers -- see that method for why impersonating
    joiners here produces a table that deals nothing.
    """
    bus = InMemoryBus()
    order = [f"seat{i}" for i in range(seats)]
    sessions = {}
    for i, conn_id in enumerate(order):
        seat_nick = nickname if i == HUMAN_SEAT else f"Bot {i}"
        session = Session(
            is_host=(i == 0),
            nickname=seat_nick,
            avatar_b64="",
            transport=InMemoryTransport(bus, conn_id),
        )
        session.local_conn_id = conn_id
        bus.register(conn_id, session)
        sessions[conn_id] = session
    nicknames = {cid: sessions[cid].local_nickname for cid in order}
    for conn_id in order:
        sessions[conn_id].seat_local_table(order, nicknames)
    return bus, sessions, order


# ---------------------------------------------------------------------------
# Hand start
# ---------------------------------------------------------------------------

def _wire_hand_start(sessions: dict, order: list) -> None:
    """Make every seat begin its own deal when game_start arrives.

    This is the join between the table starting and the mental-poker deal
    running, and it is the piece that was missing: MentalDealDriver had no
    reachable production caller because nothing ever called start_p2p_hand
    outside tests.

    These callbacks MUST NOT drain the bus. The host's fires synchronously
    inside start_game; every other seat's fires while the bus is delivering
    game_start, so a drain here would re-enter the one already running and
    consume the shared queue out of order. They enqueue, and the outer drain
    in the start_table controller carries the deal to quiescence.
    InMemoryBus.drain() now refuses re-entry outright, so a regression here
    raises rather than desyncing one hand in five.
    """
    for conn_id, session in sessions.items():
        def _on_game_start(payload, _s=session) -> None:
            ts = payload.get("table_settings", {})
            seat_order = list(payload.get("seat_order", [])) or list(order)
            names = [(_s.players[cid].nickname if cid in _s.players else cid)
                     for cid in seat_order]
            stack = int(ts.get("stack", 1000))
            _s.start_p2p_hand(
                hand_no=1,
                names=names,
                stacks=[stack] * len(seat_order),
                sb=int(ts.get("sb", 25)),
                bb=int(ts.get("bb", 50)),
                structure=ts.get("structure", "No-Limit"),
                button=0,
            )
        session.on_game_start = _on_game_start


def _make_start_table(sessions: dict, order: list, bus: InMemoryBus,
                      table_settings: dict):
    """Build the controller callable behind the client's start_game command.

    The controller owns proposed table configuration; the Session owns
    accepted protocol state. So the settings live here, in the process that
    was launched with them, and are handed to start_game rather than parked
    on the Session where they would compete with _last_table_settings.

    This is also the one place that drains. It runs on the socket read loop,
    outside any active delivery, so its drain is the OUTER one that every
    on_game_start callback relies on.
    """
    host = sessions[order[0]]

    def start_table() -> str:
        if host.state != "LOBBY":
            # Covers a live table AND a terminated one: terminate() sets
            # state to ENDED, and a dead session cannot start either.
            return "already_started"
        verdict = "started"
        try:
            host.start_game(dict(table_settings))
        except (RuntimeError, ValueError) as exc:
            # Two very different failures arrive down this one path, and
            # collapsing them lies to the client. start_game validates the
            # policy, THEN broadcasts game_start and sets PLAYING, and only
            # then runs on_game_start -- which in this launcher deals the
            # host seat's entire first hand. So an exception may mean the
            # table was refused before anything happened, or that the table
            # is irreversibly live and its first hand died.
            #
            # The session's own state is the discriminator: start_game
            # commits PLAYING before it can reach the hand.
            if host.state == "LOBBY":
                # Refused before anything was broadcast, so there is nothing
                # to deliver and the session is still startable.
                _log.warning("start_table refused: %s", exc)
                return "refused"
            _log.error("table started but its first hand failed: %s", exc)
            verdict = "hand_failed"

        # Drained here rather than in a finally, and to QUIESCENCE rather
        # than to the first failure. A finally that raises DISCARDS the
        # pending return: _wire_hand_start installs the same callback on
        # every session, so the realistic failure is one every seat shares,
        # which makes the drain raise too. The verdict then never returned
        # and the RuntimeError escaped into client_server, whose handler
        # would drop the client's connection rather than answer.
        delivered = _drain_to_quiescence(bus)
        if verdict == "started":
            # Measured, not inferred. A first version reported hand_failed
            # whenever ANY drain attempt had raised -- so one transient
            # hiccup in a bot's state hook branded a table that dealt
            # perfectly, three verified proofs a seat, as failed. What the
            # verdict is actually about is whether every seat began the
            # hand, so ask that.
            missing = [cid for cid, sess in sessions.items()
                       if sess.replica is None]
            if bus.pending or missing:
                _log.error("table started but seats %s never began the hand "
                           "(%d message(s) undelivered, clean=%s)",
                           missing or "-", bus.pending, delivered)
                verdict = "hand_failed"
            elif not delivered:
                # Something raised and a retry covered for it. Worth a line,
                # not a verdict: every seat dealt and nothing is queued.
                _log.warning("start_table: delivery hit an error but "
                             "recovered; the table is playing")
        return verdict

    return start_table


# ---------------------------------------------------------------------------
# Drain wrapper
# ---------------------------------------------------------------------------

def _drain_to_quiescence(bus: InMemoryBus) -> bool:
    """Deliver everything queued, surviving handler failures. True if clean.

    bus.drain() re-raises the first handler error, which unwinds its own
    delivery loop and leaves the REST of the queue undelivered. A single
    try/except around one drain() call therefore stops at the first failing
    message and abandons everything behind it -- the same defect as leaving
    game_start queued, which is what the containment was added to prevent.

    So it is called repeatedly until the queue is empty, bounded by an
    attempt count rather than by queue length. Length is NOT a progress
    signal here: drain() pops before delivering, so a failing message is
    always consumed, but the handlers that ran before the failure enqueue
    their own traffic -- a broken seat 1 leaves the queue LONGER than it
    started while genuine progress was made. A first version compared
    lengths and bailed with three messages still queued.
    """
    clean = True
    for _ in range(_MAX_DRAIN_ATTEMPTS):
        if not bus.pending:
            return clean
        try:
            bus.drain()
        except DrainLoopError:
            # Not retried. The step limit is a conclusion about the whole
            # exchange, already reached by counting; running it again just
            # repeats the work. Retrying this 64 times at the default limit
            # is 6.4 million deliveries while the client blocks on a socket.
            _log.exception("sidecar: runaway message loop; abandoning delivery")
            return False
        except Exception:                          # noqa: BLE001
            clean = False
            _log.exception("sidecar: a handler failed during delivery")
    if bus.pending:
        _log.error("sidecar: delivery did not settle; %d message(s) queued "
                   "after %d attempts", bus.pending, _MAX_DRAIN_ATTEMPTS)
        return False
    return clean


def _require_delivered(bus: InMemoryBus, what: str) -> None:
    """Deliver, and refuse to pretend an action propagated when it did not.

    The action paths originally called bus.drain() bare, so a delivery
    failure raised. Routing them through _drain_to_quiescence for policy
    consistency accidentally made them SWALLOW it: the bool was discarded
    and the client was told its bet "applied" while the peers may never
    have seen it. That also defeated the logging added at
    client_server._handle_command, because the error stopped arriving.

    Raising is right here, and safe now in a way it was not before:
    _handle_command catches RuntimeError, so this reports an error to the
    client instead of dropping the socket. The local action really did
    apply -- what failed is its propagation, and that is worth saying.
    """
    if not _drain_to_quiescence(bus):
        raise RuntimeError(
            f"{what}: the action applied locally but could not be "
            f"delivered to every seat")


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
        _require_delivered(bus, "send_bet_action")
        return result

    def next_p2p_hand() -> str:
        result = orig_next_p2p_hand()
        _require_delivered(bus, "next_p2p_hand")
        return result

    session.send_bet_action = send_bet_action
    session.next_p2p_hand   = next_p2p_hand


# ---------------------------------------------------------------------------
# Hand starter (test / integration helper)
# ---------------------------------------------------------------------------

def _deal_first_hand(sessions: dict, order: list, bus: InMemoryBus, *,
                     sb: int, bb: int, stack: int,
                     structure: str = "No-Limit",
                     deal_policy: str = Session.DEAL_POLICY_BG) -> None:
    """Start hand 1 on every seat and drive the bus to quiescence.

    Every seat calls ``start_p2p_hand`` with the same shared parameters
    (the same discipline the Godot client uses when it triggers game-start
    for a multi-seat table), then ``bus.drain()`` delivers the mental-poker
    deal messages until the queue is empty.

    NOT called by the production ``run()`` path, which starts tables through
    the client's start_game command and the real host broadcast.  Used by
    tests and the two-process harness, which want a hand without a socket.

    Adopts the policy directly rather than going through start_game,
    because that is the whole point of this helper: it skips the table.
    Defaults to Bayer-Groth so a harness that says nothing still exercises
    the same deal the product runs.
    """
    for cid in order:
        if not sessions[cid]._adopt_deal_policy(deal_policy):
            raise RuntimeError(
                f"{cid} already holds a different deal policy "
                f"({sessions[cid].deal_policy!r})")
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
    bus, sessions, order = _make_sessions(seats, nickname=nickname)
    human_conn_id = order[HUMAN_SEAT]
    human_session = sessions[human_conn_id]
    bot_sessions  = {cid: s for cid, s in sessions.items()
                     if cid != human_conn_id}

    _wrap_with_drain(human_session, bus)
    BotDriver(bot_sessions, random.Random(seed))

    table_settings = {
        "sb": sb, "bb": bb, "stack": stack, "structure": structure,
        # Stated, never defaulted. The sidecar runs in compat (its bus
        # carries no envelopes), so detection-only would be legal here --
        # and that is exactly why it has to say Bayer-Groth out loud. This
        # is the product's active client path; it should exercise the same
        # deal the wire mandate requires, not the weaker one compat permits.
        "deal_policy": Session.DEAL_POLICY_BG,
    }
    _wire_hand_start(sessions, order)
    start_table = _make_start_table(sessions, order, bus, table_settings)

    server = ClientServer(human_session, host="127.0.0.1", port=port,
                          start_table=start_table)
    await server.start()

    # Machine-readable port announcement (parsed by tests and by Godot launcher).
    print(f"SIDECAR_PORT:{server.port}", flush=True)
    _log.info("sidecar ready on 127.0.0.1:%d", server.port)

    # The sidecar starts in lobby phase. The Godot client sends the
    # start_game command (GODOT_PROTOCOL.md section 4), which runs the
    # controller above: the host's Session broadcasts game_start, every
    # seat begins its mental-poker deal, and one drain carries it to
    # quiescence. BotDriver takes over from there.

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
