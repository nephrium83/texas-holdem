"""The test suite never resolves the developer's real config (audit #121).

Before this guard, anything started from a class/module/session-scoped
fixture ran before the per-test isolation fixture and inherited the real
environment: test_two_process's sidecar workers loaded the real identity and
device secret, and one run wrote settings.json into %APPDATA%\\holdem on a
developer machine. These tests pin the guarantee at every scope, including a
real child process spawned from a class-scoped fixture.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest
from holdem import settings


def _real_default_config_dir():
    """Where the app would look with no override at all."""
    saved = os.environ.pop("HOLDEM_CONFIG_DIR", None)
    try:
        return Path(settings.config_dir()).resolve()
    finally:
        if saved is not None:
            os.environ["HOLDEM_CONFIG_DIR"] = saved


def _is_within(child, parent):
    child, parent = Path(child).resolve(), Path(parent).resolve()
    return child == parent or parent in child.parents


def _assert_not_real(path):
    real_default = _real_default_config_dir()
    assert not _is_within(path, real_default), \
        f"{path} resolves inside the real default config {real_default}"
    if conftest.ORIGINAL_CONFIG_DIR:
        assert not _is_within(path, conftest.ORIGINAL_CONFIG_DIR), \
            f"{path} is the developer's own HOLDEM_CONFIG_DIR"


@pytest.fixture(scope="class")
def class_scope_view():
    """What a class-scoped fixture -- and a process it spawns -- sees.
    This runs BEFORE the function-scoped isolation fixture."""
    in_process = os.environ.get("HOLDEM_CONFIG_DIR")
    child = subprocess.run(
        [sys.executable, "-c",
         "import os; print(os.environ.get('HOLDEM_CONFIG_DIR', ''))"],
        capture_output=True, text=True, timeout=60).stdout.strip()
    return in_process, child


class TestWideScopeIsolation:
    def test_class_scope_sees_the_session_guard(self, class_scope_view):
        in_process, child = class_scope_view
        assert in_process == conftest.SESSION_CONFIG_DIR
        _assert_not_real(in_process)

    def test_a_child_spawned_at_class_scope_inherits_the_guard(self, class_scope_view):
        _, child = class_scope_view
        assert child == conftest.SESSION_CONFIG_DIR
        _assert_not_real(child)


def test_session_guard_is_a_fresh_throwaway_dir():
    d = conftest.SESSION_CONFIG_DIR
    assert d and Path(d).is_dir()
    _assert_not_real(d)


def test_function_scope_is_isolated_per_test(tmp_path):
    current = os.environ["HOLDEM_CONFIG_DIR"]
    assert _is_within(current, tmp_path)
    _assert_not_real(current)


def test_two_process_workers_cannot_be_started_without_their_own_config():
    import test_two_process
    with pytest.raises(TypeError):
        test_two_process._start_worker("--role", "host")    # no config_dir
