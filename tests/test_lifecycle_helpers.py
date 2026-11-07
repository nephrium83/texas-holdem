"""Harness regressions: an unresponsive loop is a failure, not zero latency."""
import asyncio
import pytest
from lifecycle import measure_loop_latency


def test_never_running_loop_is_not_reported_as_low_latency():
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(TimeoutError, match="callback did not run"):
            measure_loop_latency(loop, samples=1, interval=0, timeout=0.01)
    finally:
        loop.close()
