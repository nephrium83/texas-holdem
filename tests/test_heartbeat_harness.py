"""Controls for the process test's measurements and failure cleanup."""
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import test_heartbeat_processes as harness


@pytest.mark.parametrize("silence,accepted", [(11, False), (14, False), (19.9, True), (20.1, True), (26, False)])
def test_silence_measurement_rejects_early_or_late_expiry(silence, accepted):
    observer = SimpleNamespace(events=lambda: [{"type": "frame", "at": 100.0}], report=lambda: "timing control")
    if accepted:
        harness._assert_silence_interval(observer, {"at": 100 + silence})
    else:
        with pytest.raises(AssertionError):
            harness._assert_silence_interval(observer, {"at": 100 + silence})


def test_pause_targets_reported_worker_instead_of_redirector(monkeypatch):
    peer = harness.Peer.__new__(harness.Peer)
    worker = os.getpid() + 100000
    peer.proc = SimpleNamespace(pid=worker + 1)
    peer.seen = lambda predicate: {"type": "ready", "pid": worker}
    peer._suspended = False
    suspended, resumed = Mock(), Mock()
    monkeypatch.setattr(harness, "_suspend_process", suspended)
    monkeypatch.setattr(harness, "_resume_process", resumed)
    peer.suspend()
    peer.resume()
    suspended.assert_called_once_with(worker)
    resumed.assert_called_once_with(worker)


def test_teardown_escalates_and_joins_reader_after_failed_terminate():
    peer = harness.Peer.__new__(harness.Peer)
    peer.resume = Mock()
    peer.command = Mock()
    peer._job = Mock()
    peer.proc = Mock()
    peer.proc.wait.side_effect = [subprocess.TimeoutExpired("peer", 8), subprocess.TimeoutExpired("peer", 5), 0]
    peer.proc.poll.return_value = None
    peer._reader = Mock()
    peer._reader.is_alive.return_value = False
    peer._stderr = Mock()
    peer.close()
    peer.proc.terminate.assert_called_once()
    peer.proc.kill.assert_called_once()
    assert peer.proc.wait.call_count == 3
    peer._job.close.assert_called_once()
    peer._reader.join.assert_called_once_with(timeout=5)
    peer.proc.stdin.close.assert_called_once()
    peer.proc.stdout.close.assert_called_once()


def test_pair_cleanup_attempts_both_peers_when_first_fails(tmp_path, monkeypatch):
    events = [{"type": "ready", "addr": "127.0.0.1:12345", "heartbeat": 5., "silence": 20.},
              {"type": "connected"}]
    peers = [Mock(), Mock()]
    for peer in peers:
        peer.wait_for.side_effect = lambda predicate, timeout: next(e for e in events if predicate(e))
    peers[1].close.side_effect = OSError("injected cleanup failure")
    monkeypatch.setattr(harness, "Peer", Mock(side_effect=peers))
    with pytest.raises(RuntimeError, match="cleanup failed"):
        with harness.peer_pair(tmp_path):
            pass
    for peer in peers:
        peer.close.assert_called_once()
