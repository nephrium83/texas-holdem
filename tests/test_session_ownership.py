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

from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Player, Session, SessionOwner


def table(n=3):
    bus = InMemoryBus()
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid),
                    master_secret=bytes([i + 1]) * 32)
        s.local_conn_id = cid
        s._host_conn_id = "peer0"
        s._join_order = list(order)
        for c in order:
            s.players[c] = Player(conn_id=c, peer_id=c, nickname=c,
                                  avatar_b64="")
        s.configure_seats(list(order))
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


def test_every_public_mutator_is_owned():
    """Guards against a future method being added without ownership."""
    expected = [
        "handle_message", "handle_disconnect", "terminate", "begin_hand",
        "start_p2p_hand", "next_p2p_hand", "send_bet_action", "start_game",
        "configure_seats", "reveal_board_street", "open_deal_audit",
        "add_local_player", "set_ready", "check_deadlines",
        "broadcast_game_state", "handle_game_action", "set_host_engine",
    ]
    for name in expected:
        method = getattr(Session, name)
        assert getattr(method, "__owned__", False), \
            f"Session.{name} mutates protocol state but is not @owned"


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

    actions = [
        lambda: s.handle_disconnect("peer0"),
        lambda: s.handle_message("peer2", {"type": "bet_action", "hand": 1,
                                           "seq": 0, "seat": 2,
                                           "action": "call", "amount": 0}),
        lambda: s.check_deadlines(),
        lambda: s.terminate(Session.LOCAL_SHUTDOWN, "user quit"),
        lambda: s._void_hand("protocol failure"),
        lambda: s.send_bet_action("call", 0),
    ]
    gate = threading.Barrier(len(actions))

    def run(fn):
        gate.wait()
        try:
            fn()
        except RuntimeError:
            pass                          # refusing loudly is acceptable

    threads = [threading.Thread(target=run, args=(f,)) for f in actions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert all(not t.is_alive() for t in threads), "a racing thread hung"
    assert len(seen) <= 1, f"{len(seen)} terminal notifications"
    if seen:
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
