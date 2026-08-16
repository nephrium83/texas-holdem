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

# How often the production ticker sweeps deadlines. check_deadlines' own
# docstring suggests "every second", and the shortest canonical deadline is
# 10s (settlement_ack), so one second is well inside every phase budget
# while costing a no-op call per seat.
_DEADLINE_TICK_SECONDS = 1.0

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


#: Message types that make up the mental-poker deal. Used only by the
#: test-only stall hook below.
_DEAL_TRAFFIC = ("key_announce", "deck_round", "deal_share", "audit_open")


def _stall_seat(session) -> None:
    """Make one seat stop answering deal traffic. TEST SUPPORT ONLY.

    Models a peer that went silent -- which is the condition timeouts
    exist for -- by DROPPING deal messages rather than raising on them.
    Raising would additionally exercise the drain's error path and muddy
    what the test is proving; a silent peer is the honest shape and the
    one the deadline machinery is designed against.

    Needed because the acceptance test drives a real sidecar subprocess
    over a real socket, where a seat cannot be reached to be broken from
    the outside.
    """
    real_handle = session.handle_message

    def deaf_to_the_deal(from_conn, msg):
        if msg.get("type") in _DEAL_TRAFFIC:
            return None
        return real_handle(from_conn, msg)

    session.handle_message = deaf_to_the_deal


def _tick_deadlines(sessions: dict, bus: InMemoryBus) -> bool:
    """One deadline sweep. True if it emitted bus work.

    Session owns timeout SEMANTICS -- what a deadline is, when it has
    expired, what to propose. This supplies only periodic execution, which
    is the single thing that was missing: check_deadlines' own docstring
    says "call this periodically from the event loop", and nothing ever
    did. The machinery was complete, tested, and unreachable, so every
    stalled hand was permanent.

    Every seat is swept, not just the human. Each Session tracks its own
    deadline and the protocol expects peers to notice independently and
    converge on the proposal; check_deadlines is a cheap no-op when no
    deadline is armed.

    Deliberately NOT a general bus pump. It delivers work IT created and
    nothing else, and skips the drain entirely if one is already running:
    that drain shares this queue and will consume the proposal itself, so
    nesting would be both unnecessary and refused.
    """
    before = bus.pending
    for session in sessions.values():
        # Not the guard it looks like: terminate() clears the deadline
        # token, so check_deadlines already no-ops on a dead session, and
        # a control that deletes this line fires nothing. Kept because a
        # background sweep should not poke terminated sessions at all --
        # but the guarantee lives in terminate(), not here.
        if session.terminal_state is None:
            session.check_deadlines()
    emitted = bus.pending > before
    if emitted and not bus.is_draining:
        bus.drain()
    return emitted


async def _deadline_ticker(sessions: dict, bus: InMemoryBus,
                           interval: float) -> None:
    """Run _tick_deadlines forever, until cancelled.

    One failing sweep must not end liveness for the whole session -- that
    would silently restore the defect this exists to fix -- so an
    exception is logged and the next tick still happens. Cancellation is
    re-raised: shutdown must actually stop it.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            _tick_deadlines(sessions, bus)
        except asyncio.CancelledError:
            raise
        except Exception:                          # noqa: BLE001
            _log.exception("deadline ticker iteration failed")


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
            # "Did every seat get as far as having a hand to play." Four
            # predicates were tried here and this is the only one that is
            # both correct on healthy tables and catches a seat that never
            # started. The rejected three, recorded so they are not
            # re-attempted:
            #
            #   * "any drain attempt raised" -- branded a perfectly healthy
            #     table hand_failed over one incidental hook error;
            #   * "every seat completed the deal" (deal_done) -- the driver
            #     stays not-done until the board completes, so this fails
            #     at hand start on a healthy table;
            #   * "every seat holds hole cards" -- this drain runs an
            #     unbounded amount of gameplay, so the bots may be several
            #     hands along when it returns and hole cards reset per
            #     hand; it failed the real-socket reachability test.
            #
            # KNOWN GAP, deliberately left rather than chased with a fifth
            # attempt: the replica is built BEFORE the deal runs, so a seat
            # that accepts game_start and then dies on deal traffic has one
            # and is reported as started, leaving the client on a table
            # frozen at "Dealing". Catching that needs a stable notion of
            # "this hand is progressing", which this seam does not have --
            # it is a synchronous drain over an open-ended amount of play.
            missing = [cid for cid, sess in sessions.items()
                       if sess.replica is None]
            if bus.pending or missing:
                _log.error("table started but seats %s never began a hand "
                           "(%d message(s) undelivered, clean=%s)",
                           missing or "-", bus.pending, delivered)
                verdict = "hand_failed"
            elif not delivered:
                # An error that delivery recovered from. A log line, not a
                # verdict: every seat has a hand and nothing is queued.
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


def _wrap_with_drain(session, bus: InMemoryBus) -> None:
    """Wrap the human seat's action methods so every call drains the bus.

    ClientServer calls send_bet_action / next_p2p_hand directly.  Neither
    knows about the bus, and that's correct -- keeping ClientServer
    bus-agnostic means it can be tested against a real socket with no bus.
    The drain coupling lives here, in the launcher that wires them together.

    A bare drain, deliberately, and NOT the start_table helper. These two
    were briefly routed through _drain_to_quiescence for "one drain policy"
    consistency, which is how they came to swallow delivery failures and
    report a bet as applied when peers never saw it. The three call sites
    genuinely differ: start_table must survive a partial failure and turn
    it into a verdict; these must not invent one. An escaping error here is
    now safe -- client_server catches RuntimeError and logs it, rather than
    dropping the socket as it once did -- so the simple thing is also the
    correct thing.
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
              port: int, seed: int | None, nickname: str,
              deadline_scale: float = 1.0,
              tick_interval: float = _DEADLINE_TICK_SECONDS,
              stall_seat: int | None = None) -> None:
    bus, sessions, order = _make_sessions(seats, nickname=nickname)
    if stall_seat is not None:
        _log.warning("TEST MODE: seat %d will not answer deal traffic",
                     stall_seat)
        _stall_seat(sessions[order[stall_seat]])
    if deadline_scale != 1.0:
        # TEST ONLY. Compresses the existing deadline durations so an
        # integration test can drive the real timeout path without waiting
        # thirty real seconds. Touches nothing else -- same clock, same
        # check_deadlines, same proposal, same void.
        _log.warning("TEST MODE: deadlines scaled by %.4f", deadline_scale)
        for session in sessions.values():
            session.scale_deadlines(deadline_scale)
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

    # THE point of this module's liveness: Session.check_deadlines had no
    # production caller at all, so a stalled deal or a peer that simply
    # stopped answering left the hand permanently stuck. The timeout
    # machinery was complete and unreachable.
    ticker = asyncio.create_task(
        _deadline_ticker(sessions, bus, tick_interval))

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        # Cancelled and awaited before the server goes: a tick that fired
        # against a half-stopped sidecar would be delivering into a session
        # nobody is reading any more.
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
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
    p.add_argument(
        "--test-deadline-scale", type=float, default=1.0, metavar="FACTOR",
        dest="test_deadline_scale",
        help="INTERNAL / TEST ONLY. Multiply every phase deadline by FACTOR "
             "(0 < FACTOR <= 1) so integration tests can exercise the real "
             "timeout path without waiting out a 30s deadline. Affects "
             "deadline durations ONLY -- never table settings, protocol "
             "messages, or anything the client can observe. Leave at 1.0.")
    p.add_argument(
        "--test-tick-interval", type=float, default=_DEADLINE_TICK_SECONDS,
        metavar="SECONDS", dest="test_tick_interval",
        help="INTERNAL / TEST ONLY. How often the deadline ticker sweeps. "
             "Leave at the default.")
    p.add_argument(
        "--test-stall-seat", type=int, default=None, metavar="SEAT",
        dest="test_stall_seat",
        help="INTERNAL / TEST ONLY. Make SEAT stop answering mental-deal "
             "traffic, modelling a peer that went silent, so the timeout "
             "path can be exercised end to end. Never set this in "
             "production.")
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
    if not 0.0 < args.test_deadline_scale <= 1.0:
        p.error("--test-deadline-scale: must be in (0, 1]")
    if args.test_tick_interval <= 0:
        p.error("--test-tick-interval: must be positive")
    if args.test_stall_seat is not None and             not 0 <= args.test_stall_seat < args.seats:
        p.error(f"--test-stall-seat: must be 0-{args.seats - 1}")

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
            deadline_scale=args.test_deadline_scale,
            tick_interval=args.test_tick_interval,
            stall_seat=args.test_stall_seat,
        ))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
