"""Session protocol state has one serialized owner, enforced not assumed.

Before this, serialization was customary. Transport messages and
disconnects ran on the dispatch thread, local commands on the UI thread,
and timeout callbacks wherever the caller happened to be. terminate()'s
check-then-set survived that on scheduling luck rather than on any
structural guarantee, which is not the same as being correct.

The owner is a re-entrant lock with explicit identity rather than a worker
thread, because every caller here has a synchronous contract
(send_bet_action returns a verdict, the in-memory bus delivers inline and
inspects immediately). What matters for correctness is that mutations are
totally ordered and that check-then-set is atomic -- both of which a single
owner provides without turning every caller into a future.

Two enforcement mechanisms, tested separately:

  @owned          entry points MARSHAL a foreign thread (it blocks to
                  acquire) -- never silently let through
  _assert_owner() inner decision points REJECT an unowned caller outright,
                  so a future call path that bypasses the entry points
                  fails loudly instead of corrupting state quietly
"""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.timeout import FakeClock as _DependencyClock
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import CommandRefused, Player, Session, SessionOwner


def table(n=3):
    from crypto_gate import require_crypto
    require_crypto()
    bus = InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid),
                    master_secret=bytes([i + 1]) * 32,
                    clock=_DependencyClock())
        s.local_conn_id = cid
        s._host_conn_id = "peer0"
        s._join_order = list(order)
        for c in order:
            s.players[c] = Player(conn_id=c, peer_id=c, nickname=c,
                                  avatar_b64="")
        s.configure_seats(list(order))
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        s._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
        s.state = "PLAYING"
        bus.register(cid, s)
        sessions[cid] = s
    for cid in order:
        sessions[cid].start_p2p_hand(
            hand_no=1, names=[f"P{i}" for i in range(n)],
            stacks=[500] * n, sb=5, bb=10, button=0)
    bus.drain()
    return bus, sessions, order


# ------------------------------------------------------- the primitive

def test_owner_reports_identity_correctly():
    owner = SessionOwner()
    assert owner.held() is False
    with owner:
        assert owner.held() is True
        with owner:                      # re-entrant
            assert owner.held() is True
        assert owner.held() is True
    assert owner.held() is False


def test_owner_is_not_held_by_a_different_thread():
    owner = SessionOwner()
    seen = []
    with owner:
        t = threading.Thread(target=lambda: seen.append(owner.held()))
        t.start()
        t.join()
    assert seen == [False], "a foreign thread believed it owned the session"


def test_owner_serializes_two_threads():
    """The property the whole design rests on: no interleaving."""
    owner = SessionOwner()
    log = []

    def work(tag):
        for _ in range(200):
            with owner:
                log.append(("enter", tag))
                log.append(("exit", tag))

    threads = [threading.Thread(target=work, args=(t,)) for t in "AB"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Every enter is immediately followed by its own exit.
    for i in range(0, len(log), 2):
        assert log[i][0] == "enter" and log[i + 1] == ("exit", log[i][1]), \
            f"interleaved at {i}: {log[i:i + 2]}"


# ------------------------------------------ rejection vs marshalling

def test_inner_mutators_reject_an_unowned_caller():
    """_assert_owner is the backstop for a path that bypasses @owned."""
    bus, sessions, order = table()
    s = sessions["peer1"]
    for name, call in [
        ("_end_hand", lambda: s._end_hand(Session.VOID_PROTOCOL, "x")),
        ("_elect_new_host", lambda: s._elect_new_host()),
        ("_invalidate_pending_work", lambda: s._invalidate_pending_work()),
        ("terminate", lambda: s.__class__.terminate.__wrapped__(
            s, Session.HOST_LOST, "x")),
    ]:
        with pytest.raises(RuntimeError, match="outside the owner"):
            call()


def test_owned_entry_points_marshal_a_foreign_thread():
    """A foreign thread must be serialized, not rejected -- these are the
    supported way in."""
    bus, sessions, order = table()
    s = sessions["peer1"]
    result = []

    def foreign():
        result.append(s.send_bet_action("call", 0))

    t = threading.Thread(target=foreign)
    t.start()
    t.join(5)
    assert not t.is_alive()
    assert result and result[0] in ("applied", "rejected")


def test_a_foreign_thread_blocks_until_the_owner_releases():
    bus, sessions, order = table()
    s = sessions["peer1"]
    entered = threading.Event()
    finished = threading.Event()

    def foreign():
        entered.set()
        s.terminate(Session.ABORTED_PROTOCOL, "from another thread")
        finished.set()

    with s._owner:
        t = threading.Thread(target=foreign)
        t.start()
        assert entered.wait(5)
        # The owner is held here, so the foreign thread cannot have run.
        assert not finished.wait(0.2), "a foreign thread mutated concurrently"
        assert s.terminal_state is None
    assert finished.wait(5)
    assert s.terminal_state == Session.ABORTED_PROTOCOL


#: Public Session methods that are deliberately NOT @owned, each with the
#: reason it mutates no protocol state. An entry here is a claim that has to
#: stay true; the test below also fails if one of these disappears, so the
#: exemption cannot outlive what it exempts.
NOT_MUTATORS = {
    "peer_count":             "returns len(self.players) under _lock",
    "player_list":            "copies the roster out under _lock",
    "read_state":             "contextmanager: takes _owner and yields self",
    "deal_done":              "predicate over _deal_driver.is_done()",
    "deal_aborted":           "predicate over _deal_driver.aborted()",
    "author_seq_holes":       "derived gap report over _author_seq_seen",
    "author_seq_hole_report": "formats author_seq_holes for a hand",
    "send_private_cards":     "legacy host send; writes no session state",
}


def test_every_public_mutator_is_owned():
    """Derived from the class, not from a list someone has to remember.

    The old version iterated a hardcoded `expected` list of 23 names, so a
    public method added without ownership was invisible to it -- which is
    the thing it claimed to guard against. Three had already been added and
    never reached the list: casual_timekeeper_seat, seat_local_table and
    start_ready_game. All three happen to be @owned, which the old test
    could not have told us either way.

    Now the public surface is enumerated from Session itself, and every
    entry must be either @owned or declared a non-mutator above with its
    reason. A new public method fails here until it is classified.
    """
    public = {name: value for name, value in vars(Session).items()
              if not name.startswith("_")
              and not isinstance(value, property)
              and callable(value)}
    assert len(public) > 25, f"the surface scan found only {len(public)}"

    unclassified = sorted(
        name for name, method in public.items()
        if not getattr(method, "__owned__", False) and name not in NOT_MUTATORS)
    assert not unclassified, (
        "public Session methods that are neither @owned nor a declared "
        f"non-mutator: {unclassified}")

    stale = sorted(set(NOT_MUTATORS) - set(public))
    assert not stale, f"NOT_MUTATORS exempts methods that no longer exist: {stale}"


# ------------------------------------------------- deterministic races

def test_terminate_has_exactly_one_winner_under_contention():
    """No probabilistic assertion: every thread is released from a barrier
    and the outcome is checked exhaustively, not sampled."""
    bus, sessions, order = table()
    s = sessions["peer1"]
    for trial in range(300):
        # Reset the FULL terminal identity, not just the visible fields:
        # leaving _terminal_seq and state behind makes trials depend on one
        # another and the sequence assertion meaningless after the first.
        s.terminal_state = None
        s.terminal_reason = None
        s.terminal_record = None
        s._terminal_seq = 0
        s.state = "PLAYING"
        seen = []
        s.on_session_terminated = seen.append
        causes = [Session.HOST_LOST, Session.ABORTED_PROTOCOL,
                  Session.LOCAL_SHUTDOWN, Session.ENDED_NORMAL]
        gate = threading.Barrier(len(causes))

        def fire(cause):
            gate.wait()
            s.terminate(cause, f"cause {cause}")

        threads = [threading.Thread(target=fire, args=(c,)) for c in causes]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert all(not t.is_alive() for t in threads), f"trial {trial} hung"
        assert len(seen) == 1, f"trial {trial}: {len(seen)} notifications"
        assert s.terminal_record is seen[0]
        assert s.terminal_state == seen[0].terminal_state
        assert s.terminal_record.sequence == 1


@pytest.mark.parametrize("trial", range(20))
def test_disconnect_message_timeout_and_shutdown_race(trial):
    """The four sources that mutate session state, released together.

    Whatever order they land in, exactly one terminal cause must win, the
    record must match the notification, and nothing may mutate afterwards.
    """
    bus, sessions, order = table()
    s = sessions["peer1"]
    seen = []
    s.on_session_terminated = seen.append

    # (name, callable, may it raise an ownership error). Only _void_hand is
    # private and called from outside an owned context, so only it may; a
    # public entry point raising one is a real failure, which a blanket
    # `except RuntimeError: pass` used to absorb along with the refusals.
    actions = [
        ("handle_disconnect", lambda: s.handle_disconnect("peer0"), False),
        ("handle_message",
         lambda: s.handle_message("peer2", {"type": "bet_action", "hand": 1,
                                            "seq": 0, "seat": 2,
                                            "action": "call", "amount": 0}),
         False),
        ("check_deadlines", lambda: s.check_deadlines(), False),
        ("terminate",
         lambda: s.terminate(Session.LOCAL_SHUTDOWN, "user quit"), False),
        ("_void_hand", lambda: s._void_hand("protocol failure"), True),
        ("send_bet_action", lambda: s.send_bet_action("call", 0), False),
    ]
    gate = threading.Barrier(len(actions))

    errors = []
    completed = []

    def run(name, fn, may_disown):
        try:
            gate.wait()
            fn()
            completed.append(name)
        except CommandRefused:
            completed.append(name)        # refusing loudly is acceptable
        except BaseException as exc:      # noqa: BLE001 - surfaced below
            if may_disown and isinstance(exc, RuntimeError):
                completed.append(name)
            else:
                errors.append((name, exc))

    threads = [threading.Thread(target=run, args=a) for a in actions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert all(not t.is_alive() for t in threads), "a racing thread hung"
    # A thread raising anything other than a tolerated refusal used to
    # vanish: the interpreter prints it and the test never saw it.
    assert not errors, f"racing threads raised: {errors[:3]}"
    assert sorted(completed) == sorted(n for n, _f, _d in actions), (
        f"not every action ran: {sorted(completed)}")
    # terminate(LOCAL_SHUTDOWN) is unconditional among the actions, so one
    # terminal transition ALWAYS happens. `<= 1` was satisfied by every one
    # of them being lost -- and `if seen:` then made the whole absorbing
    # half of this test optional, which is where its value is.
    assert len(seen) == 1, f"{len(seen)} terminal notifications"
    assert s.terminal_record is seen[0]
    assert s.terminal_state == seen[0].terminal_state
    # Absorbing: nothing may move it afterwards.
    before = (s.terminal_state, s.terminal_reason, s.terminal_record)
    s.terminate(Session.ABORTED_PROTOCOL, "late")
    s.handle_disconnect("peer0")
    s._void_hand("late")
    assert (s.terminal_state, s.terminal_reason,
            s.terminal_record) == before


def test_concurrent_local_and_inbound_commands_do_not_interleave():
    """Local commands and inbound messages share one order."""
    bus, sessions, order = table()
    s = sessions["peer1"]
    errors = []

    def local():
        for _ in range(50):
            try:
                s.send_bet_action("call", 0)
                s.next_p2p_hand()
            except Exception as exc:      # noqa: BLE001 - recorded, not hidden
                errors.append(exc)

    def inbound():
        for i in range(50):
            try:
                s.handle_message("peer2", {"type": "bet_action", "hand": 1,
                                           "seq": i, "seat": 2,
                                           "action": "call", "amount": 0})
            except Exception as exc:      # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=local), threading.Thread(target=inbound)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert all(not t.is_alive() for t in threads)
    assert not errors, f"concurrent access raised: {errors[:3]}"
