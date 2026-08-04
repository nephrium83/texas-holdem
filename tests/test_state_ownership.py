"""Ownership of mutable protocol state.

Moving message handling off the event loop fixed a 2.9 s loop stall but
created a hazard: handlers began running on the dispatch thread while
connect/disconnect callbacks still ran on the event-loop thread. Both
mutate the same Session -- players, _join_order, _host_conn_id, is_host --
and Session guards only its player collections with a lock. The protocol
state proper (_deal_driver, _replica, hand_voided, _hand_no, state,
_seat_order) has no lock at all, because it never needed one while a single
thread touched everything.

Serialized handler delivery solves message-to-message ordering. It does not
by itself make cross-thread mutation safe. These tests pin the ownership
model that does: every transport-originated mutation runs on the dispatch
consumer, so there is exactly one writer thread.

They deliberately do NOT assert anything about the main/UI thread, which
also calls into Session (start_p2p_hand, send_bet_action, next_p2p_hand).
That boundary predates this change and is documented as an open item.
"""
import gc
import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p import transport as T
from holdem.p2p import wire
from holdem.p2p.dispatch import MessageWorker
from lifecycle import transport_threads, wait_until


@pytest.fixture(autouse=True)
def _clean_transport():
    T.stop()
    T.reset_callbacks()
    wait_until(lambda: not transport_threads(), timeout=3.0)
    gc.collect()
    yield
    T.stop()
    T.reset_callbacks()
    wait_until(lambda: not transport_threads(), timeout=3.0)
    gc.collect()


def _frame(msg_type: str, payload: dict) -> bytes:
    body = wire.pack(msg_type, payload)
    return struct.pack(">I", len(body)) + body


# --------------------------------------------------- single-writer thread

def test_messages_and_disconnects_share_one_thread():
    """The core ownership property. If these ran on different threads they
    would race on Session state that has no lock."""
    threads = {"msg": set(), "disc": set(), "conn": set()}
    disconnected = threading.Event()

    T.on_message(lambda cid, m: threads["msg"].add(threading.current_thread().name))
    T.on_connect(lambda cid, a: threads["conn"].add(threading.current_thread().name))

    def on_disc(cid):
        threads["disc"].add(threading.current_thread().name)
        disconnected.set()

    T.on_disconnect(on_disc)

    address = T.start_host(0)
    host, port = address.rsplit(":", 1)
    client = socket.create_connection(("127.0.0.1", int(port)), timeout=5)
    client.sendall(_frame("chat", {"text": "x"}))
    wait_until(lambda: threads["msg"], timeout=5)
    client.close()
    assert disconnected.wait(5), "disconnect callback never ran"

    observed = threads["msg"] | threads["disc"] | threads["conn"]
    assert len(observed) == 1, (
        f"transport callbacks ran on multiple threads: {observed}")
    assert observed == {"p2p-dispatch"}, observed


def test_no_transport_callback_runs_on_the_event_loop():
    """The loop must stay free; a callback there is both a stall and a race."""
    seen = []
    T.on_message(lambda cid, m: seen.append(threading.current_thread().name))
    T.on_connect(lambda cid, a: seen.append(threading.current_thread().name))
    T.on_disconnect(lambda cid: seen.append(threading.current_thread().name))

    address = T.start_host(0)
    host, port = address.rsplit(":", 1)
    client = socket.create_connection(("127.0.0.1", int(port)), timeout=5)
    client.sendall(_frame("chat", {"text": "x"}))
    wait_until(lambda: len(seen) >= 2, timeout=5)
    client.close()
    wait_until(lambda: len(seen) >= 3, timeout=5)
    assert not any("p2p-transport" in name for name in seen), \
        f"a callback ran on the event-loop thread: {seen}"


def test_disconnect_cannot_overtake_an_inflight_message():
    """Ordering, not just thread identity: a disconnect that jumped ahead
    would tear down state the handler is about to touch."""
    order = []
    released = threading.Event()
    saw_message = threading.Event()

    def slow_message(cid, msg):
        saw_message.set()
        released.wait(timeout=5)
        order.append("message")

    T.on_message(slow_message)
    T.on_disconnect(lambda cid: order.append("disconnect"))

    address = T.start_host(0)
    host, port = address.rsplit(":", 1)
    client = socket.create_connection(("127.0.0.1", int(port)), timeout=5)
    client.sendall(_frame("chat", {"text": "x"}))
    assert saw_message.wait(5), "handler never started"
    client.close()                       # disconnect while the handler runs
    released.set()
    assert wait_until(lambda: order == ["message", "disconnect"], timeout=5), \
        f"disconnect overtook an in-flight message: {order}"


# ------------------------------------------------- worker event ordering

def test_events_and_messages_interleave_in_submission_order():
    seen = []
    worker = MessageWorker(lambda cid, msg: seen.append(("msg", msg["n"])))
    worker.start()
    try:
        for n in range(10):
            worker.submit("peer", {"n": n})
            worker.submit_event(lambda k=n: seen.append(("event", k)))
        assert wait_until(lambda: len(seen) == 20, timeout=5)
    finally:
        worker.stop()
    expected = []
    for n in range(10):
        expected.append(("msg", n))
        expected.append(("event", n))
    assert seen == expected


def test_event_failure_does_not_kill_the_worker():
    seen, errors = [], []

    def boom():
        raise RuntimeError("deliberate")

    worker = MessageWorker(lambda cid, msg: seen.append(msg["n"]),
                           on_error=errors.append)
    worker.start()
    try:
        worker.submit_event(boom)
        worker.submit("peer", {"n": 1})
        assert wait_until(lambda: seen == [1], timeout=5), seen
        assert errors and isinstance(errors[0], RuntimeError)
        assert worker.is_alive()
    finally:
        worker.stop()


def test_events_are_refused_when_the_queue_is_full():
    """Backpressure must apply to lifecycle events too, or a disconnect
    storm becomes an unbounded queue."""
    blocked = threading.Event()
    entered = threading.Event()

    def park(cid, msg):
        entered.set()
        blocked.wait(timeout=10)

    worker = MessageWorker(park, maxsize=4)
    worker.start()
    try:
        # Park the consumer inside the handler FIRST, so the queue depth is
        # deterministic. Without this the consumer may drain an extra item
        # mid-flood and the accepted count becomes load-dependent.
        worker.submit("peer", {"n": 0})
        assert entered.wait(5), "consumer never entered the handler"
        accepted = sum(1 for _ in range(50)
                       if worker.submit_event(lambda: None))
        assert accepted == 4, f"event queue exceeded its bound ({accepted})"
        assert worker.refused == 46
    finally:
        blocked.set()
        worker.stop()
