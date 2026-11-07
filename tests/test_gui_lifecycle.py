"""Legacy UI pause and worker lifetime regressions, without a display."""
import queue
import threading
from types import SimpleNamespace

import pytest

from holdem import gui
from holdem.engine import Card
from holdem.gui import Holdem


class Root:
    def __init__(self):
        self.owner = threading.get_ident()
        self.jobs = {}
        self.serial = 0
    def after(self, delay, callback):
        assert threading.get_ident() == self.owner, "worker invoked Tk"
        self.serial += 1
        self.jobs[self.serial] = callback
        return self.serial
    def after_cancel(self, job):
        assert threading.get_ident() == self.owner
        self.jobs.pop(job, None)
    def tick(self):
        jobs, self.jobs = self.jobs, {}
        for callback in jobs.values():
            callback()


class Widget:
    def __init__(self):
        self.values = {}
    def config(self, **kwargs):
        self.values.update(kwargs)


def clock_ui(phase):
    ui = Holdem.__new__(Holdem)
    ui.root = Root()
    ui._mp_mode = True
    ui._mp_local_seat = 0
    ui._mp_is_host = True
    ui.engine = SimpleNamespace(actor=0)
    ui.clock_phase = phase
    ui.clock_until = 110.0
    ui._clock_remaining = None
    ui.bank = 60.0
    ui.paused = False
    ui._mp_game_paused = False
    ui.clock_job = ui.root.after(200, ui._clock_tick)
    ui.l_clock = Widget()
    ui._btn_host_pause, ui._btn_host_resume = Widget(), Widget()
    ui.lock = ui._mp_show_pause_overlay = ui._mp_hide_pause_overlay = lambda: None
    ui._mp_unlock = lambda: None
    ui._forced = lambda *_: pytest.fail("clock forced action during pause")
    return ui


@pytest.mark.parametrize("phase", ["base", "bank"])
def test_host_pause_freezes_exact_remaining_clock(monkeypatch, phase):
    from holdem.p2p import transport
    monkeypatch.setattr(transport, "broadcast", lambda *_: None)
    now = [100.0]
    monkeypatch.setattr(gui.time, "time", lambda: now[0])
    ui = clock_ui(phase)
    ui._mp_host_pause()
    assert ui._clock_remaining == 10
    assert not ui.root.jobs
    now[0] = 1000.0
    ui._clock_tick()  # even a queued callback must not spend time/bank
    assert ui.bank == 60
    assert ui.clock_phase == phase
    assert ui._clock_remaining == 10
    ui._mp_host_resume()
    assert ui.clock_until == 1010
    assert len(ui.root.jobs) == 1
    now[0] += 3
    ui.stop_clock()
    assert ui.bank == (7 if phase == "bank" else 60)
    assert not ui.root.jobs


def test_stopping_frozen_bank_preserves_unspent_time(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(gui.time, "time", lambda: now[0])
    ui = clock_ui("bank")
    ui._pause_clock()
    now[0] = 9000
    ui.stop_clock()
    assert ui.bank == 10
    ui._resume_clock()
    assert ui.clock_phase == "off" and not ui.root.jobs


def equity_ui():
    ui = Holdem.__new__(Holdem)
    ui.root = Root()
    ui._mp_mode = False
    hero = SimpleNamespace(idx=0, hole=[Card(14, 0), Card(14, 1)], folded=False)
    ui.engine = SimpleNamespace(players=[hero], board=[], contested=lambda: [hero, SimpleNamespace(idx=1)])
    ui.v_odds = SimpleNamespace(get=lambda: True)
    ui.aids_ok = lambda: True
    ui.eq_gen = 0
    ui.eq_value = None
    ui.eq_pending = False
    ui._eq_signature = None
    ui._equity_closed = False
    ui._equity_poll_job = None
    ui._equity_results = queue.SimpleQueue()
    ui.draw_equity = lambda: None
    ui._refresh_player_panel = lambda: None
    return ui


def start_worker(monkeypatch, ui, outcome):
    threads = []
    real_thread = threading.Thread
    entered, release = threading.Event(), threading.Event()
    def calculate(*args):
        entered.set()
        assert release.wait(5), "test did not release equity worker"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    def thread_factory(*args, **kwargs):
        assert ui.root.jobs, "owner polling must be scheduled before work"
        thread = real_thread(*args, **kwargs)
        threads.append(thread)
        return thread
    monkeypatch.setattr(gui, "equity", calculate)
    monkeypatch.setattr(gui.threading, "Thread", thread_factory)
    monkeypatch.setattr(gui.player_info, "made_hand_view", lambda *_: None)
    ui.start_equity()
    assert entered.wait(5)
    return threads[0], release


@pytest.mark.parametrize("outcome", [None, RuntimeError("calculation failed"), (0.5, 0.1, 0.55)])
def test_equity_completion_only_uses_owner_thread_and_always_releases_pending(monkeypatch, outcome):
    ui = equity_ui()
    thread, release = start_worker(monkeypatch, ui, outcome)
    try:
        assert ui.eq_pending
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    ui.root.tick()
    assert not ui.eq_pending
    assert not ui.root.jobs
    if outcome is None or isinstance(outcome, Exception):
        assert ui.eq_value is None and ui._eq_signature is None
        assert ui.eq_text == "Equity unavailable."
    else:
        assert ui.eq_value == 0.55


def test_destroyed_window_invalidates_and_cancels_poll_before_worker_finishes(monkeypatch):
    ui = equity_ui()
    thread, release = start_worker(monkeypatch, ui, (0.8, 0, 0.8))
    try:
        ui._equity_destroyed(SimpleNamespace(widget=object()))
        assert not ui._equity_closed  # child destruction is irrelevant
        ui._equity_destroyed(SimpleNamespace(widget=ui.root))
        assert ui._equity_closed and not ui.eq_pending and not ui.root.jobs
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    ui._poll_equity()
    assert ui.eq_value is None and not ui.root.jobs


def test_last_opponent_departure_rejects_old_equity_completion(monkeypatch):
    ui = equity_ui()
    thread, release = start_worker(monkeypatch, ui, (0.2, 0, 0.2))
    try:
        generation = ui.eq_gen
        ui.engine.contested = lambda: [ui.engine.players[0]]
        ui.start_equity()
        assert ui.eq_gen > generation
        assert ui.eq_value == 1 and ui._eq_signature is None
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    ui.root.tick()
    assert ui.eq_value == 1 and not ui.eq_pending


@pytest.mark.parametrize("entry", ["new_game", "_mp_new_game", "deal", "finish_game"])
def test_every_hand_or_game_reset_fences_previous_worker(entry):
    ui = equity_ui()
    ui.eq_gen = 8
    ui.eq_pending = True
    ui.eq_value = 0.2
    invalidated = []
    class AfterInvalidation(Exception):
        pass
    def invalidate():
        Holdem._invalidate_equity(ui)
        invalidated.append(True)
        raise AfterInvalidation()
    ui._invalidate_equity = invalidate
    with pytest.raises(AfterInvalidation):
        getattr(ui, entry)(*([[]] if entry == "finish_game" else []))
    assert invalidated and ui.eq_gen == 9
    ui._equity_done(8, (0.2, 0, 0.2), [], [], 1)
    assert ui.eq_value is None and not ui.eq_pending


@pytest.mark.parametrize("first_release", ["settings", "host"])
def test_releasing_one_pause_owner_keeps_the_other_frozen(monkeypatch, first_release):
    from holdem.p2p import transport
    monkeypatch.setattr(transport, "broadcast", lambda *_: None)
    now = [100.0]
    monkeypatch.setattr(gui.time, "time", lambda: now[0])
    ui = clock_ui("bank")
    ui.engine.players = [SimpleNamespace(sitting_out=False)]
    ui.settings_win = SimpleNamespace(grab_release=lambda: None, destroy=lambda: None)
    ui.save_config = lambda: None
    ui.v_hint = ui.v_odds = SimpleNamespace(get=lambda: True)
    ui.v_observe = ui.v_straddles = SimpleNamespace(get=lambda: False)
    ui.v_mode = SimpleNamespace(get=lambda: "Cash")
    ui.v_struct = SimpleNamespace(get=lambda: "No-Limit")
    ui.b_straddle = Widget()
    ui.hand_over = False
    ui._mp_host_pause()
    now[0] = 1000.0
    releases = [ui.close_settings, ui._mp_host_resume]
    if first_release == "host":
        releases.reverse()
    releases[0]()
    assert ui.paused and ui._clock_remaining == 10 and not ui.root.jobs
    ui._clock_tick()
    assert ui.bank == 60 and not ui.root.jobs
    releases[1]()
    assert not ui.paused and ui.clock_until == 1010 and len(ui.root.jobs) == 1
