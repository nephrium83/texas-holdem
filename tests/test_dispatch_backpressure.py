"""Off-loop dispatch: responsiveness, ordering, bounds, and cleanup.

Inbound frames used to be handled inline on the event-loop thread. Handlers
verify Bayer-Groth proofs at ~35 ms each, so the loop was blocked for the
duration: measured at 34 ms for one verification, 317 ms for nine, and
2.9 seconds across a nine-seat hand's 81
(benchmarks/event_loop_latency.py). A blocked loop cannot read a socket or
service a timeout, so crypto on one hand made timeouts fire spuriously.

Delivery now goes through a single-consumer bounded worker. These tests pin
the three properties that makes it safe to rely on: the loop stays
responsive, ordering is unchanged, and the queue is bounded with a
deterministic overflow response.
"""
import gc
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p import transport as T
from holdem.p2p import wire
from holdem.p2p.dispatch import MessageWorker
from lifecycle import measure_loop_latency, transport_threads, wait_until


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


# ------------------------------------------------- worker in isolation

def test_messages_are_handled_in_order():
    """A single consumer must not reorder within a peer -- the protocol's
    ordering assumptions were written against inline delivery."""
    seen = []
    worker = MessageWorker(lambda cid, msg: seen.append(msg["n"]))
    worker.start()
    try:
        for n in range(200):
            assert worker.submit("peer", {"n": n})
        assert wait_until(lambda: len(seen) == 200, timeout=5)
    finally:
        worker.stop()
    assert seen == list(range(200))


def test_queue_is_bounded_and_refuses_deterministically():
    blocked = threading.Event()
    worker = MessageWorker(lambda cid, msg: blocked.wait(timeout=10),
                           maxsize=8)
    worker.start()
    try:
        accepted = sum(1 for n in range(200)
                       if worker.submit("peer", {"n": n}))
        assert accepted <= 9, f"queue exceeded its bound ({accepted} taken)"
        assert worker.refused > 0, "overflow was not reported"
    finally:
        blocked.set()
        worker.stop()


def test_submit_never_blocks_the_caller():
    """Blocking here would push backpressure onto the event loop -- the
    thread this exists to keep free."""
    blocked = threading.Event()
    worker = MessageWorker(lambda cid, msg: blocked.wait(timeout=10),
                           maxsize=4)
    worker.start()
    try:
        started = time.perf_counter()
        for n in range(50):
            worker.submit("peer", {"n": n})
        assert (time.perf_counter() - started) < 1.0, "submit blocked"
    finally:
        blocked.set()
        worker.stop()


def test_handler_exception_does_not_kill_the_worker():
    """Dying here would leave every later message unprocessed with nothing
    recorded anywhere."""
    seen, errors = [], []

    def handler(cid, msg):
        if msg["n"] == 1:
            raise RuntimeError("deliberate")
        seen.append(msg["n"])

    worker = MessageWorker(handler, on_error=errors.append)
    worker.start()
    try:
        for n in range(4):
            worker.submit("peer", {"n": n})
        assert wait_until(lambda: seen == [0, 2, 3], timeout=5), seen
        assert errors and isinstance(errors[0], RuntimeError)
        assert worker.is_alive()
    finally:
        worker.stop()


def test_stop_is_idempotent_and_joins():
    worker = MessageWorker(lambda cid, msg: None)
    worker.start()
    worker.stop()
    worker.stop()
    assert not worker.is_alive()


def test_stop_without_start_is_harmless():
    MessageWorker(lambda cid, msg: None).stop()


def test_submit_after_stop_is_refused():
    worker = MessageWorker(lambda cid, msg: None)
    worker.start()
    worker.stop()
    assert worker.submit("peer", {"n": 0}) is False


# ------------------------------------------------ loop responsiveness

def test_event_loop_stays_responsive_under_slow_handlers():
    """The regression that motivated the worker.

    A handler that takes ~35 ms, as a proof verification does, must not
    delay the loop. Inline, twenty of these blocked it for the better part
    of a second; off-loop it should stay near its idle latency.
    """
    address = T.start_host(0)
    host, port = address.rsplit(":", 1)
    handled = []

    def slow(conn_id, msg):
        time.sleep(0.035)
        handled.append(msg)

    T.on_message(slow)
    loop = T.event_loop()
    idle = measure_loop_latency(loop, samples=10)

    client = socket.create_connection(("127.0.0.1", int(port)), timeout=5)
    try:
        for n in range(20):
            client.sendall(_frame("chat", {"text": str(n)}))
        loaded = measure_loop_latency(loop, samples=20)
        assert wait_until(lambda: len(handled) >= 20, timeout=20), \
            f"only {len(handled)} of 20 frames handled"
    finally:
        client.close()

    # Generous absolute bound: the point is that one handler's duration
    # (35 ms) no longer shows up as loop latency, not a precise figure.
    assert loaded < 25.0, (
        f"event loop stalled under slow handlers: {loaded:.1f} ms "
        f"(idle was {idle:.1f} ms)")


def test_dispatch_depth_is_observable():
    """Backpressure has to be measurable to be operable."""
    T.start_host(0)
    assert T.dispatch_depth() == 0
    assert T.dispatch_refused() == 0


def test_worker_thread_does_not_outlive_stop():
    address = T.start_host(0)
    host, port = address.rsplit(":", 1)
    T.on_message(lambda cid, msg: None)
    client = socket.create_connection(("127.0.0.1", int(port)), timeout=5)
    try:
        client.sendall(_frame("chat", {"text": "x"}))
        wait_until(lambda: T.dispatch_depth() == 0, timeout=5)
    finally:
        client.close()
    before = {t.name for t in threading.enumerate()}
    T.stop()
    assert wait_until(
        lambda: not [t for t in threading.enumerate()
                     if t.is_alive() and t.name == "p2p-dispatch"],
        timeout=5), "dispatch worker outlived stop()"
    assert before is not None
