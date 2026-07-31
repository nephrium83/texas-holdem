"""Single-consumer worker that runs message handlers off the event loop.

Why this exists
---------------
The transport used to deliver each frame by calling the registered
on_message handlers inline, on the event-loop thread. Those handlers verify
Bayer-Groth shuffle proofs at roughly 35 ms each, so the loop was blocked
for the duration of every verification: measured at 34 ms for one, 317 ms
for nine, and 2.9 seconds across a nine-seat hand's 81 verifications
(benchmarks/event_loop_latency.py). While blocked the loop cannot read a
socket, service a timeout, or answer another peer -- so cryptographic work
on one hand made timeouts fire spuriously and honest peers look
unresponsive.

Why a worker thread rather than an executor
-------------------------------------------
Handlers mutate session and MentalDeal state. Running them on a pool would
race, and running them concurrently would reorder within a peer. A single
consumer keeps delivery serialized exactly as inline delivery was, so the
protocol's ordering assumptions are unchanged -- only the thread differs.

Backpressure
------------
The queue is bounded. A peer that produces faster than handlers consume
cannot grow it without limit, and the overflow policy is explicit and
deterministic rather than "block the producer forever": the offending
message is refused and the caller decides (the transport drops that peer).
Refusal is reported, never silent.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Deepest the inbound queue may get before messages are refused. A
# nine-seat hand has at most ~189 deal messages in flight, so honest play
# never approaches this; it bounds what one hostile peer can make the
# process hold.
DEFAULT_MAXSIZE = 1024


class MessageWorker:
    """Serialized, bounded, off-loop delivery of (conn_id, msg) pairs."""

    def __init__(self, handler: Callable[[str, dict], None],
                 maxsize: int = DEFAULT_MAXSIZE,
                 on_error: Optional[Callable[[BaseException], None]] = None,
                 name: str = "p2p-dispatch"):
        self._handler = handler
        self._on_error = on_error
        self._queue: "queue.Queue" = queue.Queue(maxsize=maxsize)
        self._thread = threading.Thread(target=self._run, name=name,
                                        daemon=True)
        self._stopping = threading.Event()
        self._started = False
        self.refused = 0            # messages rejected for backpressure

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def submit(self, conn_id: str, msg: dict) -> bool:
        """Queue one message. False if refused (queue full, or stopping).

        Never blocks: blocking here would push the backpressure problem
        back onto the event loop, which is the thread this exists to keep
        free.
        """
        if self._stopping.is_set() or not self._started:
            return False
        try:
            self._queue.put_nowait((conn_id, msg))
            return True
        except queue.Full:
            self.refused += 1
            log.warning("dispatch: queue full (%d) — refusing message from %s",
                        self._queue.maxsize, conn_id)
            return False

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker and join it. Idempotent."""
        if not self._started:
            self._stopping.set()
            return
        self._stopping.set()
        # The sentinel must get in even when the queue is full, which is
        # exactly the state a backpressured worker is in when shutdown
        # arrives. put_nowait alone raises queue.Full there, leaving the
        # thread running and stop() raising into its caller. Drop pending
        # work to make room: the worker is shutting down, so those messages
        # were never going to be handled.
        while True:
            try:
                self._queue.put_nowait(_SENTINEL)
                break
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass                    # consumer drained it; retry
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.error("dispatch: worker did not stop within %.1fs", timeout)

    # ------------------------------------------------------------ internals

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def is_alive(self) -> bool:
        return self._started and self._thread.is_alive()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                return
            conn_id, msg = item
            try:
                self._handler(conn_id, msg)
            except Exception as exc:
                # A handler failure is a protocol event, not a reason to
                # lose the worker: log it, report it, keep consuming.
                # Dying here would leave every later message unprocessed
                # with nothing recorded.
                log.exception("dispatch: handler failed for %s", conn_id)
                if self._on_error is not None:
                    try:
                        self._on_error(exc)
                    except Exception:
                        log.exception("dispatch: error callback failed")


_SENTINEL = object()

__all__ = ["MessageWorker", "DEFAULT_MAXSIZE"]
