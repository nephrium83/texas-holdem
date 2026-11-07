"""Failed peer cleanup must reap, escalate, join and try every peer."""
import io
import subprocess
from types import SimpleNamespace
import pytest
from peer_cleanup import close_child, close_all


def test_stubborn_child_is_killed_and_reaped_before_pipes_close():
    calls = []
    class Child:
        def __init__(self):
            self.returncode = None
            self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO(), io.StringIO()
            self.waits = 0
        def poll(self):
            return self.returncode
        def wait(self, timeout):
            self.waits += 1
            calls.append("wait")
            if self.waits < 3:
                raise subprocess.TimeoutExpired("owned-test-child", timeout)
            self.returncode = -9
            return self.returncode
        def terminate(self):
            calls.append("terminate")
        def kill(self):
            calls.append("kill")
    child = Child()
    reader = SimpleNamespace(name="collector", join=lambda timeout: calls.append("join"), is_alive=lambda: False)
    close_child(child, quit=lambda: calls.append("quit"), readers=[reader], timeout=0.01)
    assert calls == ["quit", "wait", "terminate", "wait", "kill", "wait", "join"]
    assert child.returncode is not None
    assert all(stream.closed for stream in (child.stdin, child.stdout, child.stderr))


def test_one_cleanup_failure_does_not_abandon_other_peers():
    seen = []
    def fail():
        seen.append("bad")
        raise RuntimeError("stuck reader")
    with pytest.raises(RuntimeError, match="first.*stuck reader"):
        close_all([("first", fail), ("second", lambda: seen.append("good"))])
    assert seen == ["bad", "good"]
