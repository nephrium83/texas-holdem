"""Session uses only the transport, secret source, and clock its host supplies."""
from __future__ import annotations

import builtins
import io
import os

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
