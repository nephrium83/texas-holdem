"""Silent/partial output and stderr backpressure must not defeat deadlines."""
import subprocess
import sys
import time

import pytest

from process_output import capture, collect_stdout, read_port, stop_process


def _child(source):
    return subprocess.Popen([sys.executable, "-u", "-c", source], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8")


@pytest.mark.parametrize("prefix", ["", "partial without newline"])
def test_silent_or_partial_child_obeys_port_and_collection_deadlines(prefix):
    proc = _child(f"import time; print({prefix!r}, end='', flush=True); time.sleep(30)")
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="SIDECAR_PORT"):
            read_port(proc, 0.2)
        assert collect_stdout(proc, "DONE", 0.2) == []
        assert time.monotonic() - started < 3
        assert proc.poll() is None  # deadline did not wait for the child exit
    finally:
        stop_process(proc)
    assert proc.poll() is not None
    assert all(not thread.is_alive() for thread in capture(proc).threads)


def test_stderr_flood_is_drained_while_waiting_for_stdout():
    proc = _child("import sys,time; sys.stderr.write('e'*300000); sys.stderr.flush(); "
                  "print('SIDECAR_PORT:12345', flush=True); print('DONE', flush=True); time.sleep(30)")
    try:
        assert read_port(proc, 5) == 12345
        assert collect_stdout(proc, "DONE", 1) == ["DONE"]
        assert len(capture(proc).stderr_tail()) <= 65536
    finally:
        stop_process(proc)


def test_eof_without_port_is_reported_and_reaped():
    proc = _child("print('no port')")
    try:
        with pytest.raises(TimeoutError, match="SIDECAR_PORT"):
            read_port(proc, 2)
    finally:
        stop_process(proc)
    assert proc.returncode == 0


def test_cleanup_escalates_and_waits_after_a_failed_terminate():
    calls = []

    class Child:
        def poll(self):
            return None
        def terminate(self):
            calls.append("terminate")
        def kill(self):
            calls.append("kill")
        def wait(self, timeout):
            calls.append("wait")
            if calls.count("wait") == 1:
                raise subprocess.TimeoutExpired("test child", timeout)

    stop_process(Child(), timeout=0.1)
    assert calls == ["terminate", "wait", "kill", "wait"]
