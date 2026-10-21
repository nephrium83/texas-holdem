"""Real desktop sessions with a deterministic presentation clock."""
from __future__ import annotations

import threading

import pytest

from crypto_gate import require_crypto
from holdem.desktop_table import DesktopTable
from holdem.engine import Brain
from holdem.p2p.session import Session
from holdem.paced_bots import BOT_TURN_SECONDS, HAND_PRESENTATION_SECONDS


class Scheduled:
    def __init__(self, when, callback):
        self.when = when
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class Scheduler:
    def __init__(self):
        self.now = 0.0
        self.events = []

    def schedule(self, delay, callback):
        item = Scheduled(self.now + delay, callback)
        self.events.append(item)
        return item

    def advance(self, seconds):
        end = self.now + seconds
        for _ in range(1000):
            due = [event for event in self.events
                   if not event.cancelled and event.when <= end]
            if not due:
                self.now = end
                return
            item = min(due, key=lambda event: event.when)
            self.events.remove(item)
            self.now = item.when
            item.callback()
        raise AssertionError("presentation scheduler failed to settle")

    def next(self):
        due = [event.when for event in self.events if not event.cancelled]
        assert due, "a bot was waiting without a scheduled action"
        self.advance(max(0.0, min(due) - self.now))


def _table(clock, *, seats=2, on_change=lambda: None):
    require_crypto()
    return DesktopTable(
        seats=seats, nickname="Alice", seed=7, on_change=on_change,
        settings=dict(sb=25, bb=50, stack=100_000, structure="No-Limit",
                      deal_policy=Session.DEAL_POLICY_BG),
        local_clock=lambda: clock.now, local_schedule=clock.schedule,
    )


def _start(table):
    assert table.snapshot()["lobby"]["mode"] == "menu"
    assert table.apply_command("local_game", {"nickname": "Alice"})["ok"]
    assert table.snapshot()["phase"] == "lobby"
    assert table.apply_command("start_game", {})["verdict"] == "started"
    return table._local[1][table._local[2][0]]


def test_start_publishes_dealt_unacted_hand_before_opening_bot_can_fold(monkeypatch):
    clock = Scheduler()
    decisions = []

    def fold(self, engine, seat):
        decisions.append((clock.now, seat))
        return "fold", 0

    monkeypatch.setattr(Brain, "decide", fold)
    table = _table(clock)
    try:
        human = _start(table)
        first = table.snapshot()
        assert first["phase"] == "betting"
        assert first["hand_num"] == 1
        assert first["turn"]["state"] == "waiting"
        assert len(first["you"]["hole"]) == 2
        assert first["settlement"] is None
        assert human.replica.next_seq == 0
        assert human.replica.actor == 1
        assert not decisions
        # Invalid human actions and repeated observations cannot shorten or
        # restart the original opening deadline.
        assert not table.apply_command("check_call", {})["ok"]
        clock.advance(HAND_PRESENTATION_SECONDS - 0.01)
        assert human.replica.next_seq == 0
        assert not decisions
        clock.advance(0.01)
        assert decisions == [(HAND_PRESENTATION_SECONDS, 1)]
        assert table.snapshot()["phase"] == "settled"
        assert human.replica.next_seq == 1
        assert sum(human.replica.stacks) == 200_000
    finally:
        table.close()


def test_every_new_hand_gets_a_deal_grace_and_each_bot_turn_gets_a_pause(monkeypatch):
    clock = Scheduler()
    actions = []

    def call(self, engine, seat):
        actions.append((clock.now, seat, engine.street))
        return "call", 0

    monkeypatch.setattr(Brain, "decide", call)
    table = _table(clock)
    try:
        human = _start(table)
        clock.next()
        assert table.snapshot()["you"].get("legal")
        assert table.apply_command("check_call", {})["ok"]
        # The big blind acts last preflop and first on the flop heads-up.
        assert table.snapshot()["you"].get("legal")
        assert table.apply_command("check_call", {})["ok"]
        before = len(actions)
        clock.advance(BOT_TURN_SECONDS - 0.01)
        assert len(actions) == before
        clock.advance(0.01)
        assert len(actions) == before + 1
        for _ in range(100):
            snap = table.snapshot()
            if snap["phase"] == "settled":
                break
            if "legal" in snap["you"]:
                assert table.apply_command("check_call", {})["ok"]
            else:
                clock.next()
        else:
            pytest.fail("check/call hand never settled")
        # Bots' Next Hand readiness has only the short pause. It must not be
        # postponed behind the deal grace for a hand not yet started.
        clock.advance(BOT_TURN_SECONDS)
        assert table.apply_command("next_hand", {})["verdict"] == "started"
        assert table.snapshot()["hand_num"] == 2
        assert human.replica.next_seq == 0
        before = len(actions)
        # Seat0 acts first on this hand. Its normal command still goes through;
        # the next bot must respect the new hand's presentation deadline.
        assert "legal" in table.snapshot()["you"]
        assert table.apply_command("check_call", {})["ok"]
        clock.advance(HAND_PRESENTATION_SECONDS - 0.01)
        assert len(actions) == before
        clock.advance(0.01)
        assert len(actions) == before + 1
    finally:
        table.close()


def test_folded_human_can_watch_bots_finish_and_play_ten_conserving_hands(monkeypatch):
    clock = Scheduler()
    monkeypatch.setattr(Brain, "decide", lambda self, engine, seat: ("call", 0))
    table = _table(clock, seats=3)
    try:
        human = _start(table)
        sessions = table._local[1]
        results = {cid: {} for cid in sessions}
        for cid, session in sessions.items():
            session.on_hand_settled = (
                lambda result, cid=cid, session=session:
                results[cid].__setitem__(session.replica.hand_no,
                                        list(session.replica.stacks)))
        finished = set()
        for _ in range(1200):
            snap = table.snapshot()
            assert len(table._local_bots._hand_ready) <= 2
            if snap["phase"] == "settled":
                finished.add(snap["hand_num"])
                assert sum(human.replica.stacks) == 300_000
                if len(finished) == 10:
                    break
                verdict = table.apply_command("next_hand", {})["verdict"]
                if verdict == "not_ready":
                    clock.next()
                else:
                    assert verdict == "started"
            elif "legal" in snap["you"]:
                # Force a complete bot-versus-bot runout in hand1, then call
                # through the remaining hands without depending on RNG luck.
                command = "fold" if snap["hand_num"] == 1 else "check_call"
                assert table.apply_command(command, {})["ok"]
            else:
                clock.next()
        else:
            pytest.fail("paced table did not finish ten hands")
        assert finished == set(range(1, 11))
        assert results["seat0"] == results["seat1"] == results["seat2"]
        assert len(results["seat0"]) == 10
        assert all(sum(stacks) == 300_000 for stacks in results["seat0"].values())
    finally:
        table.close()


@pytest.mark.parametrize("failure", ["brain", "refused", "partial_delivery"])
def test_bot_failure_closes_visibly_without_retry_or_corrupting_a_new_table(
        monkeypatch, failure):
    clock = Scheduler()
    table = _table(clock)
    try:
        human = _start(table)
        driver = table._local_bots
        bus, sessions, order = table._local
        bot = sessions[order[1]]
        old_callback = next(event.callback for event in clock.events
                            if not event.cancelled)

        def broken_brain(engine, seat):
            raise RuntimeError("injected opponent failure")

        monkeypatch.setattr(driver._brain, "decide",
                            broken_brain if failure == "brain"
                            else lambda engine, seat: ("call", 0))
        if failure == "refused":
            monkeypatch.setattr(bot, "send_bet_action",
                                lambda action, amount: "not_your_turn")
        elif failure == "partial_delivery":
            drain = bus.drain

            def broken_drain():
                # Duplicate a legitimate queued action, as a network may do.
                # Deliver the first copy, then fail with the second still
                # pending so the human replica has already changed.
                bus.enqueue(*bus._queue[0])
                drain(max_steps=1)

            monkeypatch.setattr(bus, "drain", broken_drain)
        clock.next()
        snap = table.snapshot()
        assert snap["lobby"]["mode"] == "local"
        assert snap["turn"]["state"] == "table_closed"
        assert snap["turn"]["headline"] == (
            "A local opponent could not finish its turn. "
            "Leave the table and start a new game.")
        assert "legal" not in snap["you"]
        assert snap["terminal"]["state"] == Session.LOCAL_SHUTDOWN
        assert snap["terminal"]["last_settled_stacks"] == [100_000, 100_000]
        assert [seat["stack"] for seat in snap["seats"]] == [100_000, 100_000]
        assert snap["pot"] == 0
        assert all(session.terminal_state == Session.LOCAL_SHUTDOWN
                   for session in sessions.values())
        assert not driver._pending and not driver._hand_ready
        assert not [event for event in clock.events if not event.cancelled]
        previous_seq = human.replica.next_seq
        assert previous_seq == (1 if failure == "partial_delivery" else 0)
        clock.advance(100)
        old_callback()
        assert human.replica.next_seq == previous_seq

        assert table.apply_command("leave_table", {})["ok"]
        fresh = _start(table)
        fresh_snap = table.snapshot()
        assert fresh_snap["phase"] == "betting"
        assert fresh_snap["terminal"] is None
        assert fresh.replica.next_seq == 0
        old_callback()
        assert table.snapshot() == fresh_snap
        assert human.replica.next_seq == previous_seq
    finally:
        table.close()


@pytest.mark.parametrize("closing", [False, True])
def test_leave_or_close_cancels_even_a_callback_already_waiting_for_owner_lock(
        monkeypatch, closing):
    clock = Scheduler()
    decisions = []
    changes = []
    monkeypatch.setattr(Brain, "decide", lambda self, engine, seat:
                        (decisions.append(seat) or ("fold", 0)))
    table = _table(clock, on_change=lambda: changes.append(True))
    old = _start(table)
    callback = next(event.callback for event in clock.events if not event.cancelled)
    entered = threading.Event()

    def fire():
        entered.set()
        callback()

    try:
        with table._lock:
            worker = threading.Thread(target=fire)
            worker.start()
            assert entered.wait(2)
            if closing:
                table.close()
            else:
                table.apply_command("leave_table", {})
                table.apply_command("local_game", {"nickname": "Alice"})
            notifications = len(changes)
        worker.join(timeout=2)
        assert not worker.is_alive()
        # Force even cancelled callbacks, as a timer may already have started.
        clock.now = 100.0
        for event in list(clock.events):
            event.callback()
        assert not decisions
        assert len(changes) == notifications
        assert old.replica.next_seq == 0
        assert old.terminal_state == Session.LOCAL_SHUTDOWN
        assert table.snapshot()["phase"] == "lobby"
    finally:
        table.close()
