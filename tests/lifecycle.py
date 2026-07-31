"""Reusable leak detectors for lifecycle tests.

A lifecycle test that passes while leaving a listening socket, a live
event loop, or a task that will raise later is not testing anything
useful. These helpers make the leak the failure.

They are deliberately not fixtures: several tests need to assert on the
delta around one specific call rather than around the whole test.
"""
from __future__ import annotations

import asyncio
import gc
import socket
import threading
import time
from contextlib import contextmanager
from typing import Optional


def open_socket_count() -> int:
    """Sockets alive in this process, via the GC.

    psutil would be more direct but is not a dependency; walking the GC
    finds Python-level socket objects, which is what leaks in this code
    base (asyncio servers, transports, and the multicast sockets).
    """
    gc.collect()
    return sum(1 for obj in gc.get_objects()
               if isinstance(obj, socket.socket) and obj.fileno() != -1)


def pending_tasks(loop: Optional[asyncio.AbstractEventLoop]) -> list:
    """Tasks still alive on *loop*, excluding ones already finished."""
    if loop is None or loop.is_closed():
        return []
    try:
        tasks = asyncio.all_tasks(loop)
    except RuntimeError:
        return []
    return [t for t in tasks if not t.done()]


def transport_threads() -> list:
    """Live threads the transport layer owns, by name."""
    return [t for t in threading.enumerate()
            if t.is_alive() and (t.name.startswith("p2p-transport")
                                 or t.name.startswith("tcp-reader")
                                 or t.name.startswith("tcp-accept"))]


def wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    """Poll *predicate* until true or *timeout*.

    Lifecycle assertions need to tolerate a scheduler hop without becoming
    sleep-timing tests: the condition is what matters, the delay is not.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@contextmanager
def no_socket_leaks(slack: int = 0):
    """Fail if the block leaves more sockets open than it found."""
    before = open_socket_count()
    yield
    assert wait_until(lambda: open_socket_count() <= before + slack), (
        f"socket leak: {open_socket_count()} open, started with {before}")


@contextmanager
def no_thread_leaks():
    """Fail if the block leaves a new transport thread running."""
    before = {id(t) for t in transport_threads()}
    yield
    def settled():
        return not [t for t in transport_threads() if id(t) not in before]
    assert wait_until(settled, timeout=3.0), (
        f"thread leak: {[t.name for t in transport_threads()]}")


class LoopWatch:
    """Records tasks on a loop so a test can assert none outlive shutdown."""

    def __init__(self, loop):
        self.loop = loop

    def pending(self) -> list:
        return pending_tasks(self.loop)

    def assert_drained(self, timeout: float = 3.0) -> None:
        assert wait_until(lambda: not self.pending(), timeout=timeout), (
            f"tasks still pending: "
            f"{[t.get_name() for t in self.pending()]}")


def measure_loop_latency(loop, samples: int = 20,
                         interval: float = 0.005) -> float:
    """Worst observed scheduling delay on *loop*, in milliseconds.

    Schedules a no-op repeatedly and measures how late it runs. A loop
    blocked by CPU-bound work shows up here as a large maximum, which is
    the number that matters for responsiveness -- a mean hides exactly the
    stall we care about.
    """
    worst = 0.0
    for _ in range(samples):
        sent = time.perf_counter()
        done = threading.Event()

        def _mark():
            nonlocal worst
            worst = max(worst, (time.perf_counter() - sent) * 1000)
            done.set()

        loop.call_soon_threadsafe(_mark)
        done.wait(timeout=5)
        time.sleep(interval)
    return worst
