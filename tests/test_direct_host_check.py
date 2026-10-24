"""Diagnostic observations use only fake routers; never SSDP, SOAP or TCP."""
from __future__ import annotations

import base64
from functools import partial
import json
import os
import threading
from types import SimpleNamespace

import pytest

from holdem import port_mapping as M
from tools import check_direct_host as D
from test_port_mapping import FakeTimer, LOCAL, PORT, Router, RenewControl, eventually


@pytest.fixture(autouse=True)
def never_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("A diagnostic unit test attempted a real router operation")

    monkeypatch.setattr(M, "_make_client", forbidden)
    monkeypatch.setattr(M, "_open_soap", forbidden)
    FakeTimer.instances.clear()


def observe(router, **kwargs):
    return D._MappingDiagnostics(
        client_factory=lambda _: router,
        open_mapping=partial(M._open_tcp_mapping, timer_factory=FakeTimer,
                             open_timeout=1, **kwargs))


def test_diagnostic_records_exact_port_owner_and_verified_cleanup():
    router = Router()
    diagnostic = observe(router)
    lease = diagnostic.mapping_factory(LOCAL, PORT, on_lost=lambda _: None)
    lease.close()
    diagnostic.after_close()
    result = diagnostic.snapshot()
    assert result["external_port"] == router.adds[0][0]
    assert result["ownership_marker"] == router.adds[0][4]
    assert result["control_url"] == "http://192.168.1.1:5000/control"
    assert result["add_attempts"] == result["add_successes"] == 1
    assert result["renewal_attempts"] == result["renewal_successes"] == 0
    assert result["delete_attempts"] == result["delete_successes"] == 1
    assert result["cleanup_worker"] == "finished"
    assert result["post_close_lookup"] == {"status": "absent"}
    assert router.lookups[-1] == (result["external_port"], "TCP", "")
    assert len(router.adds) == len(router.deletes) == 1


def test_diagnostic_observes_renewal_without_initiating_an_extra_add():
    router, control = Router(), RenewControl()
    diagnostic = observe(router, wait=control)
    lease = diagnostic.mapping_factory(LOCAL, PORT, on_lost=lambda _: None)
    try:
        assert control.entered.wait(1)
        control.wake.set()
        assert eventually(lambda: diagnostic.snapshot()["renewal_successes"] == 1)
    finally:
        lease.close()
    diagnostic.after_close()
    result = diagnostic.snapshot()
    assert result["add_attempts"] == result["add_successes"] == len(router.adds) == 2
    assert result["renewal_attempts"] == result["renewal_successes"] == 1
    assert result["delete_attempts"] == len(router.deletes) == 1
    assert result["post_close_lookup"]["status"] == "absent"


def test_failed_add_preserves_native_cause_and_failed_setup_ownership():
    router = Router()
    native = OSError("synthetic SOAP rejection")
    wrapped = RuntimeError("synthetic binding failure")
    wrapped.__cause__ = native
    router.add_error = wrapped  # Fake commits before losing its response.
    diagnostic = observe(router)
    with pytest.raises(M.MappingError):
        diagnostic.mapping_factory(LOCAL, PORT, on_lost=lambda _: None)
    diagnostic.after_close()
    result = diagnostic.snapshot()
    assert result["external_port"] == router.adds[0][0]
    assert result["ownership_marker"] == router.adds[0][4]
    assert result["control_url"]
    assert result["add_attempts"] == 1 and result["add_successes"] == 0
    failed = next(event for event in result["operations"] if event["stage"] == "add")
    assert [item["message"] for item in failed["error_chain"]] == [
        "synthetic binding failure", "synthetic SOAP rejection"]
    assert result["delete_successes"] == 1
    assert result["post_close_lookup"]["status"] == "absent"


def test_native_control_url_is_preserved_when_strict_backend_rejects_it():
    router = Router()
    router.selectigd = lambda: "https://192.168.1.1/rejected-control"
    diagnostic = D._MappingDiagnostics(
        client_factory=lambda _: M._MiniupnpcBackend(router),
        open_mapping=partial(M._open_tcp_mapping, timer_factory=FakeTimer, open_timeout=1))
    with pytest.raises(M.MappingError, match="unsupported control URL"):
        diagnostic.mapping_factory(LOCAL, PORT, on_lost=lambda _: None)
    diagnostic.after_close()
    result = diagnostic.snapshot()
    assert result["control_url"] == "https://192.168.1.1/rejected-control"
    assert result["external_port"] is None and result["ownership_marker"] is None
    assert result["add_attempts"] == result["delete_attempts"] == 0
    assert result["post_close_lookup"]["status"] == "unknown"


def test_failed_delete_reports_owned_entry_without_retrying_delete():
    router = Router()
    router.delete_error = OSError("synthetic deletion refused")
    diagnostic = observe(router)
    lease = diagnostic.mapping_factory(LOCAL, PORT, on_lost=lambda _: None)
    lease.close()
    diagnostic.after_close()
    result = diagnostic.snapshot()
    assert result["delete_attempts"] == len(router.deletes) == 1
    assert result["delete_successes"] == 0
    assert result["post_close_lookup"]["status"] == "owned_entry_present"
    assert len(router.adds) == 1


def test_failed_post_close_lookup_is_unknown_instead_of_claiming_removal():
    router = Router()
    diagnostic = observe(router)
    lease = diagnostic.mapping_factory(LOCAL, PORT, on_lost=lambda _: None)
    lease.close()
    router.lookup_error = OSError("synthetic read-only lookup failure")
    diagnostic.after_close()
    result = diagnostic.snapshot()
    assert result["delete_successes"] == 1
    assert result["post_close_lookup"]["status"] == "lookup_failed"
    assert result["post_close_lookup"]["error_chain"][0]["message"] == "synthetic read-only lookup failure"


def test_stalled_cleanup_has_bounded_wait_and_no_concurrent_lookup():
    router = Router()
    entered, release = threading.Event(), threading.Event()
    original = router.deleteportmapping

    def blocked_delete(*args):
        entered.set()
        assert release.wait(2)
        return original(*args)

    router.deleteportmapping = blocked_delete
    diagnostic = observe(router, close_timeout=0.005)
    lease = diagnostic.mapping_factory(LOCAL, PORT, on_lost=lambda _: None)
    try:
        lease.close()
        assert entered.wait(1)
        count = len(router.lookups)
        diagnostic.after_close(cleanup_wait=0.005, lookup_wait=0.005)
        result = diagnostic.snapshot()
        assert result["cleanup_worker"] == "pending"
        assert result["post_close_lookup"]["status"] == "unknown"
        assert len(router.lookups) == count
    finally:
        release.set()
        lease.close()
        assert lease._done.wait(1)


def test_stalled_final_read_is_bounded_and_never_changes_mapping():
    router = Router()
    diagnostic = observe(router)
    lease = diagnostic.mapping_factory(LOCAL, PORT, on_lost=lambda _: None)
    lease.close()
    entered, release = threading.Event(), threading.Event()
    router.before_lookup = lambda: (entered.set(), release.wait(2))
    try:
        diagnostic.after_close(cleanup_wait=0.005, lookup_wait=0.005)
        assert entered.is_set()
        assert diagnostic.snapshot()["post_close_lookup"]["status"] == "pending"
        assert len(router.adds) == len(router.deletes) == 1
    finally:
        release.set()
        assert eventually(lambda: diagnostic.snapshot()["post_close_lookup"]["status"] == "absent")


def test_exception_chains_redact_synthetic_capabilities_and_private_values():
    raw = base64.b32encode(bytes([2]) + bytes(range(1, 70))).decode()
    code = "-".join(raw[i:i + 4] for i in range(0, 112, 4))
    cause = ValueError(code + " {'admission_secret': '" + "ab" * 16
                       + "', 'private_key': 'synthetic-private-value'} "
                       + "-----BEGIN PRIVATE KEY-----\nsynthetic-pem-body\n-----END PRIVATE KEY-----")
    outer = RuntimeError("synthetic outer failure")
    outer.__cause__ = cause
    chain = D._exception_chain(outer)
    serialized = json.dumps(chain)
    assert len(chain) == 2 and chain[0]["message"] == "synthetic outer failure"
    assert code not in serialized and "ab" * 16 not in serialized
    assert "synthetic-private-value" not in serialized and "synthetic-pem-body" not in serialized
    assert "[REDACTED_INVITE]" in serialized and "[REDACTED_PRIVATE_KEY]" in serialized


def _fake_main(monkeypatch):
    from holdem import lan_session
    from holdem.p2p import invite, transport

    # The standalone CLI mutates these directly. Register their prior state
    # so its in-process test cannot affect the next transport/config user.
    monkeypatch.setattr(transport, "_resolve_stun", transport._resolve_stun)
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", os.environ.get("HOLDEM_CONFIG_DIR", ""))

    class Table:
        def __init__(self, _nickname, _settings, *, mapping_factory):
            self.factory, self.lease = mapping_factory, None

        def host(self, *, internet):
            assert internet
            self.lease = self.factory(LOCAL, PORT, on_lost=lambda _: None)

        def lobby_state(self):
            return dict(network="internet", state="lobby", invite=invite.generate_room_code(
                bytes(range(32)), public_address=self.lease.external_address,
                flags=invite.FLAG_DIRECT_INTERNET))

        def close(self):
            if self.lease is not None:
                self.lease.close()

    monkeypatch.setattr(lan_session, "LanTable", Table)
    monkeypatch.setattr(transport, "event_loop", lambda: None)
    monkeypatch.setattr(transport, "peer_ids", lambda: [])
    now = [0.0]
    monkeypatch.setattr(D, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))


def test_simulation_stays_offline_without_mapping_diagnostics(tmp_path, monkeypatch, capsys):
    _fake_main(monkeypatch)
    assert D.main(["--simulate-router", "--seconds", "1", "--run-dir", str(tmp_path / "run")]) == 0
    report = json.loads((tmp_path / "run/report.json").read_text())
    assert report["ok"] and report["simulated"]
    assert "mapping_diagnostics" not in report
    assert "invite" not in json.loads(capsys.readouterr().out)
    assert not (tmp_path / "run/private-invite.txt").exists()


@pytest.mark.parametrize("existing_config", [False, True], ids=["absent-config", "existing-config"])
def test_simulation_restores_stun_and_environment_before_the_next_user(
        tmp_path, monkeypatch, existing_config):
    from holdem.p2p import transport

    with monkeypatch.context() as baseline:
        if existing_config:
            baseline.setenv("HOLDEM_CONFIG_DIR", str(tmp_path / "previous-config"))
        else:
            baseline.delenv("HOLDEM_CONFIG_DIR", raising=False)
        previous_config = os.environ.get("HOLDEM_CONFIG_DIR")
        previous_stun = transport._resolve_stun
        with monkeypatch.context() as cli:
            _fake_main(cli)
            assert D.main(["--simulate-router", "--seconds", "1",
                           "--run-dir", str(tmp_path / "run")]) == 0
            assert transport._resolve_stun is not previous_stun
            assert os.environ["HOLDEM_CONFIG_DIR"] != previous_config
        # Check after the CLI's own fixture scope, before pytest's outer
        # autouse environment fixture could conceal either leaked mutation.
        assert transport._resolve_stun is previous_stun
        assert os.environ.get("HOLDEM_CONFIG_DIR") == previous_config


def test_detailed_real_mode_with_fake_backend_keeps_details_local(tmp_path, monkeypatch, capsys):
    _fake_main(monkeypatch)
    router, diagnostic = Router(), None
    diagnostic = observe(router)
    monkeypatch.setattr(D, "_MappingDiagnostics", lambda: diagnostic)
    result = D.main(["--allow-router-mapping", "--mapping-details", "--seconds", "1",
                     "--run-dir", str(tmp_path / "run")])
    report = json.loads((tmp_path / "run/report.json").read_text())
    console = capsys.readouterr().out
    assert result == 0 and report["mapping_cleanup_verified"]
    details = report["mapping_diagnostics"]
    assert details["ownership_marker"] and details["control_url"]
    assert details["ownership_marker"] not in console and details["control_url"] not in console
    assert "mapping_diagnostics" not in json.loads(console)


def test_detailed_failed_setup_with_fake_backend_saves_sanitized_cause(tmp_path, monkeypatch, capsys):
    _fake_main(monkeypatch)
    router = Router()
    router.add_error = OSError("synthetic add failed after commit")
    diagnostic = observe(router)
    monkeypatch.setattr(D, "_MappingDiagnostics", lambda: diagnostic)
    result = D.main(["--allow-router-mapping", "--mapping-details", "--seconds", "1",
                     "--run-dir", str(tmp_path / "run")])
    report = json.loads((tmp_path / "run/report.json").read_text())
    assert result == 1 and not report["ok"]
    assert report["error_chain"] and report["mapping_cleanup_verified"]
    details = report["mapping_diagnostics"]
    failed_add = next(event for event in details["operations"] if event["stage"] == "add")
    assert failed_add["error_chain"][0]["message"] == "synthetic add failed after commit"
    assert details["external_port"] and details["ownership_marker"] and details["control_url"]
    console = capsys.readouterr().out
    assert details["ownership_marker"] not in console and details["control_url"] not in console
    assert not (tmp_path / "run/private-invite.txt").exists()
