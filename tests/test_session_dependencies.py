"""Session uses only the transport, secret source, and clock its host supplies."""
from __future__ import annotations

import builtins
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from holdem.p2p.events import ListSink
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Session
from holdem.p2p.timeout import DeadlineToken, FakeClock


def _session(**overrides):
    arguments = dict(is_host=False, nickname="Peer", avatar_b64="",
                     transport=InMemoryTransport(InMemoryBus(), "peer"),
                     clock=FakeClock(), master_secret=b"memory-only-secret")
    arguments.update(overrides)
    return Session(**arguments)


@pytest.mark.parametrize("dependency", ["transport", "clock"])
def test_session_requires_explicit_dependencies(dependency):
    arguments = dict(is_host=False, nickname="Peer", avatar_b64="",
                     transport=InMemoryTransport(InMemoryBus(), "peer"),
                     clock=FakeClock(), master_secret=b"memory-only-secret")
    del arguments[dependency]
    with pytest.raises(TypeError, match=dependency):
        Session(**arguments)


@pytest.mark.parametrize("dependency", ["transport", "clock"])
def test_none_cannot_select_a_host_dependency(dependency):
    with pytest.raises(TypeError, match=dependency):
        _session(**{dependency: None})


def test_secret_source_is_required_even_before_a_hand_starts():
    with pytest.raises(TypeError, match="master_secret or secret_provider"):
        _session(master_secret=None)


def test_secret_provider_must_be_callable():
    with pytest.raises(TypeError, match="secret_provider must be callable"):
        _session(master_secret=None, secret_provider=b"not a provider")


def test_secret_provider_is_lazy_and_success_is_cached():
    calls = []

    def provider():
        calls.append("loaded")
        return b"provided-secret"

    session = _session(master_secret=None, secret_provider=provider)
    assert calls == [], "construction loaded the secret eagerly"
    assert session._deal_master_secret == b"provided-secret"
    assert session._deal_master_secret == b"provided-secret"
    assert calls == ["loaded"], "a successful secret was not cached"


def test_first_hand_announces_the_provided_secret_and_later_hands_reuse_it(
        tmp_path, monkeypatch):
    import crypto_gate
    crypto_gate.require_crypto()
    from holdem.p2p import ristretto as R
    from holdem.p2p.mental_deal import derive_share

    class RecordingTransport(InMemoryTransport):
        def __init__(self):
            super().__init__(InMemoryBus(), "peer")
            self.sent = []

        def broadcast(self, message):
            self.sent.append(dict(message))
            super().broadcast(message)

    calls = []
    secret = bytes(range(32))

    def provider():
        calls.append("loaded")
        return secret

    config = tmp_path / "no-machine-secret"
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(config))
    transport = RecordingTransport()
    session = _session(transport=transport, master_secret=None,
                       secret_provider=provider)
    session.local_conn_id = "peer"
    session.configure_seats(["remote", "peer"])
    session._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
    session._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
    assert calls == [], "lobby setup loaded the secret eagerly"

    announced_keys = []
    for hand in (1, 2):
        session.begin_hand(hand_no=hand, button=0)
        announcements = [message for message in transport.sent
                         if message["type"] == "key_announce"
                         and message["hand"] == hand]
        assert len(announcements) == 1
        announcement = announcements[0]
        expected_share = derive_share(secret, session._deal_session_id(),
                                      hand, session.local_seat)
        assert announcement["seat"] == 1
        assert announcement["X_hex"] == bytes(R.mul_base(expected_share)).hex(), (
            "the deal announcement did not use the provided secret")
        announced_keys.append(announcement["X_hex"])
        assert session._deal_master_secret == secret
        assert calls == ["loaded"], "a later hand reloaded the secret provider"
    assert announced_keys[0] != announced_keys[1]
    assert not config.exists(), "the supplied secret touched host config"


def test_a_failed_provider_can_be_retried_and_then_cached():
    calls = []

    def provider():
        calls.append("attempt")
        if len(calls) == 1:
            raise OSError("host storage temporarily unavailable")
        return b"recovered-secret"

    session = _session(master_secret=None, secret_provider=provider)
    with pytest.raises(OSError, match="temporarily unavailable"):
        session._deal_master_secret
    assert session._deal_master_secret == b"recovered-secret"
    assert session._deal_master_secret == b"recovered-secret"
    assert calls == ["attempt", "attempt"]


def test_explicit_secret_and_later_setter_bypass_the_provider():
    def forbidden():
        pytest.fail("an explicit secret reached the host provider")

    session = _session(master_secret=b"override", secret_provider=forbidden)
    assert session._deal_master_secret == b"override"
    session._deal_master_secret = b"later-override"
    assert session._deal_master_secret == b"later-override"
    unset = _session(master_secret=None, secret_provider=forbidden)
    unset._deal_master_secret = b"set-before-first-use"
    assert unset._deal_master_secret == b"set-before-first-use"


def test_memory_session_never_selects_host_resources(tmp_path, monkeypatch):
    original_import = builtins.__import__
    forbidden_modules = {"holdem.p2p.identity", "holdem.p2p.device_secret",
                         "holdem.p2p.transport", "holdem.settings",
                         "socket", "asyncio"}

    def no_host_import(name, globals=None, locals=None, fromlist=(), level=0):
        requested = {name, *(f"{name}.{entry}" for entry in fromlist or ())}
        assert not requested & forbidden_modules, "session selected host resources"
        return original_import(name, globals, locals, fromlist, level)

    def no_file_access(*args, **kwargs):
        raise AssertionError("session selected host resources through a file")

    config = tmp_path / "config"
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(config))
    with monkeypatch.context() as blocked:
        blocked.setattr(builtins, "__import__", no_host_import)
        blocked.setattr(builtins, "open", no_file_access)
        blocked.setattr(io, "open", no_file_access)
        blocked.setattr(os, "open", no_file_access)
        session = _session(master_secret=None,
                           secret_provider=lambda: b"supplied-in-memory")
        assert session._deal_master_secret == b"supplied-in-memory"
        session.terminate(Session.LOCAL_SHUTDOWN, "memory session complete")
    assert not config.exists()


#: Imported in a FRESH interpreter with the guard already installed, so
#: coupling that happens AT IMPORT TIME is visible. Parameterised by the
#: module under test so the control below can point it at a module that
#: really is host-coupled -- a probe that cannot fail proves nothing.
_IMPORT_PROBE = """
import builtins, sys

FORBIDDEN = {
    "holdem.p2p.identity", "holdem.p2p.device_secret",
    "holdem.p2p.transport", "holdem.settings", "socket", "asyncio",
}
target = sys.argv[1]
_real = builtins.__import__
offenders = []

def guard(name, globals=None, locals=None, fromlist=(), level=0):
    requested = {name}
    for entry in fromlist or ():
        requested.add(name + "." + entry)
    bad = requested & FORBIDDEN
    if bad:
        offenders.append(sorted(bad))
    return _real(name, globals, locals, fromlist, level)

builtins.__import__ = guard
try:
    __import__(target)
finally:
    builtins.__import__ = _real

# Checked twice over: nothing forbidden was REQUESTED through
# builtins.__import__, and nothing forbidden is RESIDENT in sys.modules --
# which catches an import routed around it, e.g. through importlib.
resident = sorted(m for m in FORBIDDEN if m in sys.modules)
if offenders or resident:
    print("REQUESTED=%r RESIDENT=%r" % (offenders, resident))
    sys.exit(1)
print("CLEAN")
"""


def _run_import_probe(target, tmp_path):
    repo = str(Path(__file__).resolve().parents[1])
    environment = dict(os.environ)
    environment["PYTHONPATH"] = repo
    environment["HOLDEM_CONFIG_DIR"] = str(tmp_path / "config")
    return subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE, target],
        cwd=repo, env=environment, capture_output=True, text=True,
        timeout=180)


def test_importing_session_selects_no_host_resource(tmp_path):
    """Import-time coupling, which the in-process test cannot see.

    test_memory_session_never_selects_host_resources patches
    builtins.__import__ inside its own body -- but this module imports
    holdem.p2p.session at the top, so by the time the patch is installed
    the import has already happened, and everything session.py pulls in AT
    IMPORT TIME is invisible to it. A regression that put `import socket`,
    a settings read, or a device-secret load at module scope would pass
    that test unchanged (audit #174).

    Run in a fresh interpreter instead, with the guard installed before
    the subject is imported.
    """
    proc = _run_import_probe("holdem.p2p.session", tmp_path)
    assert proc.returncode == 0, (
        "importing holdem.p2p.session selected a host resource:\n"
        f"{proc.stdout}{proc.stderr}")
    assert "CLEAN" in proc.stdout, proc.stdout + proc.stderr
    assert not (tmp_path / "config").exists(), \
        "importing the session module created a config directory"


def test_control_the_import_probe_fires_on_a_host_module(tmp_path):
    """The probe above is worthless unless it can fail.

    holdem.p2p.transport is a host module by design -- it is on the
    forbidden list precisely because it owns sockets and the event loop --
    so pointing the probe at it must come back red.
    """
    proc = _run_import_probe("holdem.p2p.transport", tmp_path)
    assert proc.returncode == 1, (
        "the import probe passed on a module that IS host-coupled, so it "
        f"cannot be trusted on session:\n{proc.stdout}{proc.stderr}")
    assert "RESIDENT=" in proc.stdout or "REQUESTED=" in proc.stdout, \
        proc.stdout + proc.stderr


def test_event_and_terminal_records_use_the_supplied_clock():
    clock = FakeClock(12.0, wall_start=1_700_000_000.0)
    sink = ListSink()
    session = _session(clock=clock, sink=sink)
    assert sink.events[0]["ts"] == 1_700_000_000.0, "event clock was bypassed"
    clock.advance_wall(86_400.0)
    clock.advance(3.0)
    with session._owner:
        session._end_hand(Session.VOID_LOCAL_ABORT, "test void", announce=False)
    assert session.hand_record.monotonic_ts == 15.0, "hand clock was bypassed"
    assert sink.of_type("hand_voided")[-1]["ts"] == 1_700_086_403.0
    clock.advance(2.0)
    session.terminate(Session.LOCAL_SHUTDOWN, "test close")
    assert session.terminal_record.monotonic_ts == 17.0, "terminal clock was bypassed"
    assert sink.of_type("sidecar_stopping")[-1]["ts"] == 1_700_086_405.0


def test_wall_clock_jumps_do_not_expire_a_monotonic_deadline(monkeypatch):
    clock = FakeClock(100.0, wall_start=1_700_000_000.0)
    session = _session(clock=clock)
    token = DeadlineToken("memory-hand", "betting", "remote", 0)
    proposed = []
    monkeypatch.setattr(session, "_broadcast_timeout_proposal", proposed.append)
    with session._owner:
        session._start_deadline(token)
    clock.advance_wall(1_000_000.0)
    session.check_deadlines()
    assert proposed == []
    clock.advance(29.0)
    session.check_deadlines()
    assert proposed == []
    clock.advance(1.0)
    session.check_deadlines()
    assert proposed == [token]


def test_fake_clock_has_separate_wall_and_monotonic_origins():
    clock = FakeClock(20.0, wall_start=1_700_000_000.0)
    clock.advance_wall(-300.0)
    assert clock.monotonic() == 20.0
    assert clock.time() == 1_699_999_700.0
    clock.advance(2.5)
    assert clock.monotonic() == 22.5
    assert clock.time() == 1_699_999_702.5
    assert FakeClock(7.0).time() == 7.0


def test_control_eager_loading_fires_the_provider_test(monkeypatch):
    original = Session.__init__

    def eager(self, *args, **kwargs):
        provider = kwargs.get("secret_provider")
        if provider is not None:
            provider()
        original(self, *args, **kwargs)

    monkeypatch.setattr(Session, "__init__", eager)
    with pytest.raises(AssertionError, match="construction loaded the secret eagerly"):
        test_secret_provider_is_lazy_and_success_is_cached()


def test_control_host_fallback_fires_the_resource_test(tmp_path, monkeypatch):
    def host_secret(self):
        from holdem.p2p import device_secret
        return device_secret.load_or_create()

    monkeypatch.setattr(Session, "_deal_master_secret", property(host_secret))
    with pytest.raises(AssertionError, match="session selected host resources"):
        test_memory_session_never_selects_host_resources(tmp_path, monkeypatch)


def test_control_wrong_wall_clock_fires_the_timestamp_test(monkeypatch):
    original = Session._safe_emit

    def wrong_clock(self, event, **extra):
        original(self, event, **dict(extra, ts=-1.0))

    monkeypatch.setattr(Session, "_safe_emit", wrong_clock)
    with pytest.raises(AssertionError, match="event clock was bypassed"):
        test_event_and_terminal_records_use_the_supplied_clock()


def test_control_wrong_monotonic_clock_fires_the_record_test(monkeypatch):
    monkeypatch.setattr(FakeClock, "monotonic", lambda self: -1.0)
    with pytest.raises(AssertionError, match="hand clock was bypassed"):
        test_event_and_terminal_records_use_the_supplied_clock()
