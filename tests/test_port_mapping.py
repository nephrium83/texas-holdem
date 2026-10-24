"""Router changes are always faked: this suite never performs SSDP or HTTP."""
from __future__ import annotations

import io
import threading
import time
from urllib.error import HTTPError

import pytest

from holdem import port_mapping as M

_REAL_OPEN_SOAP = M._open_soap


LOCAL = "192.168.1.22"
PUBLIC = "8.8.4.4"
PORT = 42345


class FakeTimer:
    instances = []

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False
        self.started = False
        self.daemon = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


class Router:
    def __init__(self):
        self.lanaddr = LOCAL
        self.public = PUBLIC
        self.entry = None
        self.adds = []
        self.deletes = []
        self.lookups = []
        self.discovered = 1
        self.before_lookup = None
        self.before_add = None
        self.after_add = None
        self.lookup_error = None
        self.add_error = None
        self.delete_error = None

    def discover(self):
        return self.discovered

    def selectigd(self):
        return "http://192.168.1.1:5000/control"

    def externalipaddress(self):
        return self.public

    def getspecificportmapping(self, *args):
        self.lookups.append(args)
        if self.before_lookup:
            self.before_lookup()
        if self.lookup_error:
            raise self.lookup_error
        return self.entry

    def addportmapping(self, *args):
        self.adds.append(args)
        if self.before_add:
            self.before_add()
        self.entry = (args[2], args[3], args[4], True, args[6])
        if self.after_add:
            self.after_add()
        if self.add_error:
            raise self.add_error
        return True

    def deleteportmapping(self, *args):
        self.deletes.append(args)
        if self.delete_error:
            raise self.delete_error
        self.entry = None
        return True


class RenewControl:
    def __init__(self):
        self.entered = threading.Event()
        self.wake = threading.Event()
        self.delays = []

    def __call__(self, stop, delay):
        self.delays.append(delay)
        self.entered.set()
        while not stop.wait(0.002):
            if self.wake.is_set():
                self.wake.clear()
                return False
        return True


def eventually(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.002)
    return predicate()


@pytest.fixture(autouse=True)
def never_network(monkeypatch):
    FakeTimer.instances.clear()

    def forbidden(*args, **kwargs):
        pytest.fail("A port-mapping unit test attempted a real router operation")

    monkeypatch.setattr(M, "_make_client", forbidden)
    monkeypatch.setattr(M, "_open_soap", forbidden)


@pytest.fixture
def leases():
    opened = []
    yield opened
    for lease in opened:
        lease.close()
        assert lease._done.wait(2.0), "fake router worker leaked"


def open_fake(router, leases, **kwargs):
    lease = M._open_tcp_mapping(
        LOCAL, PORT, on_lost=kwargs.pop("on_lost", lambda _: None),
        client_factory=lambda _: router, timer_factory=FakeTimer,
        open_timeout=kwargs.pop("open_timeout", 1.0), **kwargs,
    )
    leases.append(lease)
    return lease


def test_open_verified_finite_tcp_and_idempotent_owned_close(leases):
    router = Router()
    lease = open_fake(router, leases)
    external_port = lease.external_address[1]
    assert lease.external_address == (PUBLIC, external_port)
    assert 49152 <= external_port <= 65535
    assert router.adds == [(external_port, "TCP", LOCAL, PORT, router.entry[2], "", 300)]
    assert router.entry[2].startswith("Poker ")
    assert len(router.entry[2]) == len("Poker ") + 32
    assert FakeTimer.instances[0].daemon
    lease.close()
    lease.close()
    assert router.deletes == [(external_port, "TCP", "")]
    assert router.entry is None


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", "169.254.0.1", "224.0.0.1", "::1", "host.example", True])
def test_invalid_local_address_never_contacts_router(address):
    with pytest.raises(M.MappingError):
        M._open_tcp_mapping(address, PORT, on_lost=lambda _: None)


@pytest.mark.parametrize("port", [0, 65536, -1, True, 1.5, "42"])
def test_invalid_local_port_never_contacts_router(port):
    with pytest.raises(M.MappingError):
        M._open_tcp_mapping(LOCAL, port, on_lost=lambda _: None)


@pytest.mark.parametrize("address", ["192.168.1.1", "10.0.0.1", "100.64.0.1", "203.0.113.1", "127.0.0.1", "255.255.255.255", "::1", "invalid"])
def test_nonpublic_router_address_refused_without_add(address, leases):
    router = Router()
    router.public = address
    with pytest.raises(M.MappingError):
        open_fake(router, leases)
    assert not router.adds and not router.deletes


def test_wrong_interface_and_missing_router_refused(leases):
    for change in (lambda r: setattr(r, "lanaddr", "192.168.2.22"), lambda r: setattr(r, "discovered", 0)):
        router = Router()
        change(router)
        with pytest.raises(M.MappingError):
            open_fake(router, leases)
        assert not router.adds and not router.deletes


def test_existing_entry_even_same_target_is_never_overwritten(leases):
    router = Router()
    original = (LOCAL, PORT, "Another application", True, 200)
    router.entry = original
    with pytest.raises(M.MappingError, match="already in use"):
        open_fake(router, leases)
    assert router.entry == original
    assert not router.adds and not router.deletes


def test_lookup_failure_is_not_treated_as_vacancy(leases):
    router = Router()
    router.lookup_error = OSError("router not responding")
    with pytest.raises(M.MappingError):
        open_fake(router, leases)
    assert not router.adds and not router.deletes


@pytest.mark.parametrize("replacement", [
    lambda e: (e[0], e[1], e[2], True, 0),
    lambda e: (e[0], e[1], e[2], True, 3600),
    lambda e: (e[0], e[1], e[2], False, 300),
])
def test_invalid_lease_success_is_removed_without_permanent_fallback(replacement, leases):
    router = Router()
    router.after_add = lambda: setattr(router, "entry", replacement(router.entry))
    with pytest.raises(M.MappingError, match="finite"):
        open_fake(router, leases)
    assert eventually(lambda: len(router.deletes) == 1)
    assert len(router.adds) == 1 and router.adds[0][-1] == 300


@pytest.mark.parametrize("replacement", [
    lambda e: ("192.168.1.99", e[1], e[2], True, 300),
    lambda e: (e[0], e[1] + 1, e[2], True, 300),
    lambda e: (e[0], e[1], "unrelated", True, 300),
    lambda e: None,
])
def test_failed_post_add_verification_preserves_unowned_entry(replacement, leases):
    router = Router()
    router.after_add = lambda: setattr(router, "entry", replacement(router.entry))
    with pytest.raises(M.MappingError, match="missing or changed"):
        open_fake(router, leases)
    assert eventually(lambda: router.lookups and len(router.lookups) >= 3)
    assert not router.deletes


def test_add_committed_but_response_failed_cleans_owned_entry(leases):
    router = Router()
    router.add_error = OSError("response was lost")
    with pytest.raises(M.MappingError):
        open_fake(router, leases)
    assert eventually(lambda: len(router.deletes) == 1)


def test_renew_checks_ownership_and_renews_same_finite_entry(leases):
    router = Router()
    control = RenewControl()
    lease = open_fake(router, leases, wait=control)
    assert control.entered.wait(1)
    assert 89 < control.delays[0] <= 90
    control.wake.set()
    assert eventually(lambda: len(router.adds) == 2)
    assert router.adds[1] == router.adds[0]
    lease.close()
    assert len(router.deletes) == 1


def test_stolen_entry_not_renewed_or_deleted_and_notifies_once(leases):
    router = Router()
    control = RenewControl()
    messages = []
    lease = open_fake(router, leases, wait=control, on_lost=messages.append)
    assert control.entered.wait(1)
    replacement = (LOCAL, PORT, "Different host generation", True, 300)
    router.entry = replacement
    control.wake.set()
    assert lease._done.wait(1)
    lease.close()
    assert len(messages) == 1
    assert len(router.adds) == 1 and not router.deletes
    assert router.entry == replacement


def test_public_address_change_loses_lease_and_removes_owned_entry(leases):
    router = Router()
    control = RenewControl()
    messages = []
    lease = open_fake(router, leases, wait=control, on_lost=messages.append)
    assert control.entered.wait(1)
    router.public = "8.8.8.8"
    control.wake.set()
    assert lease._done.wait(1)
    assert len(messages) == 1 and "address changed" in messages[0]
    assert len(router.adds) == 1 and len(router.deletes) == 1


def test_lost_callback_can_close_without_worker_deadlock(leases):
    router = Router()
    control = RenewControl()
    closed = threading.Event()
    holder = []

    def on_lost(_):
        holder[0].close()
        closed.set()

    lease = open_fake(router, leases, wait=control, on_lost=on_lost)
    holder.append(lease)
    assert control.entered.wait(1)
    router.lookup_error = OSError("offline")
    control.wake.set()
    assert closed.wait(1) and lease._done.wait(1)


def test_close_preserves_replacement_from_rehost(leases):
    router = Router()
    lease = open_fake(router, leases)
    replacement = (LOCAL, PORT, "Poker newer-ownership-marker", True, 300)
    router.entry = replacement
    lease.close()
    assert router.entry == replacement and not router.deletes


def test_delete_failure_is_bounded_and_never_adds_permanent_fallback(leases):
    router = Router()
    router.delete_error = OSError("offline")
    lease = open_fake(router, leases)
    lease.close()
    assert len(router.adds) == 1 and router.entry[-1] == 300
    assert lease._done.is_set()


def test_open_timeout_late_add_gets_owned_cleanup_without_publishing(leases):
    router = Router()
    entered = threading.Event()
    release = threading.Event()

    def block():
        entered.set()
        assert release.wait(2)

    router.before_add = block
    try:
        started = time.monotonic()
        with pytest.raises(M.MappingError, match="timed out"):
            open_fake(router, leases, open_timeout=0.05)
        assert time.monotonic() - started < 0.5 and entered.is_set()
        assert not router.deletes
    finally:
        release.set()
    assert eventually(lambda: len(router.deletes) == 1)
    assert router.entry is None and len(router.adds) == 1


def test_close_during_renew_add_returns_bounded_and_cleans_late_success(leases):
    router = Router()
    control = RenewControl()
    lease = open_fake(router, leases, wait=control, close_timeout=0.02)
    assert control.entered.wait(1)
    entered = threading.Event()
    release = threading.Event()
    router.before_add = lambda: (entered.set(), release.wait(2))
    control.wake.set()
    assert entered.wait(1)
    try:
        started = time.monotonic()
        lease.close()
        assert time.monotonic() - started < 0.3
    finally:
        release.set()
    assert lease._done.wait(1)
    assert len(router.adds) == 2 and len(router.deletes) == 1


def test_expiry_notifies_once_while_native_renewal_is_stalled(leases):
    router = Router()
    control = RenewControl()
    messages = []
    lease = open_fake(router, leases, wait=control, on_lost=messages.append)
    assert control.entered.wait(1)
    entered = threading.Event()
    release = threading.Event()
    router.before_lookup = lambda: (entered.set(), release.wait(2))
    control.wake.set()
    assert entered.wait(1)
    timer = FakeTimer.instances[-1]
    try:
        timer.fire()
        timer.fire()
        assert len(messages) == 1 and "expired" in messages[0]
    finally:
        release.set()
    assert lease._done.wait(1)
    assert len(router.adds) == 1


def test_old_expiry_timer_cannot_close_renewed_lease(leases):
    router = Router()
    control = RenewControl()
    messages = []
    lease = open_fake(router, leases, wait=control, on_lost=messages.append)
    assert control.entered.wait(1)
    old_timer = FakeTimer.instances[-1]
    control.wake.set()
    assert eventually(lambda: len(FakeTimer.instances) == 2)
    old_timer.fire()
    assert not messages and not lease._stop.is_set()
    lease.close()
    FakeTimer.instances[-1].fire()
    assert not messages


def test_expiry_callback_racing_rearm_cannot_claim_new_generation(leases):
    router = Router()
    messages = []
    lease = open_fake(router, leases, on_lost=messages.append)
    old_timer = FakeTimer.instances[-1]
    entered = threading.Event()
    release = threading.Event()
    original_notify = lease._notify_lost

    def pause_before_claim(message, *, expected_generation=None):
        entered.set()
        assert release.wait(2)
        original_notify(message, expected_generation=expected_generation)

    lease._notify_lost = pause_before_claim
    callback = threading.Thread(target=old_timer.fire)
    callback.start()
    try:
        assert entered.wait(1)
        lease._arm_expiry(300)
        assert FakeTimer.instances[-1] is not old_timer
    finally:
        release.set()
        callback.join(1)
    assert not callback.is_alive()
    assert not messages and not lease._stop.is_set()
    lease.close()


def test_slow_post_add_verification_does_not_extend_lease_lifetime(leases):
    router = Router()
    now = [10.0]
    router.after_add = lambda: now.__setitem__(0, 110.0)
    lease = open_fake(router, leases, clock=lambda: now[0], open_timeout=200.0)
    assert FakeTimer.instances[-1].delay == 200
    lease.close()


def test_cancellation_while_discovering_prevents_any_mapping(leases):
    router = Router()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def discover():
        entered.set()
        assert release.wait(2)
        finished.set()
        return 1

    router.discover = discover
    try:
        with pytest.raises(M.MappingError, match="timed out"):
            open_fake(router, leases, open_timeout=0.03)
        assert entered.is_set()
    finally:
        release.set()
    assert finished.wait(1)
    assert eventually(lambda: not any(t.name == "poker-port-mapping" for t in threading.enumerate()))
    assert not router.adds and not router.deletes


def test_native_workers_are_globally_capped_until_abandoned_calls_finish(leases):
    routers = [Router(), Router()]
    release = threading.Event()
    for router in routers:
        router.before_add = lambda: release.wait(2)
    try:
        for router in routers:
            with pytest.raises(M.MappingError, match="timed out"):
                open_fake(router, leases, open_timeout=0.03)
        third = Router()
        with pytest.raises(M.MappingError, match="still finishing"):
            open_fake(third, leases)
        assert not third.adds and not third.lookups
    finally:
        release.set()
    assert eventually(lambda: all(len(r.deletes) == 1 for r in routers))
    assert eventually(lambda: not any(t.name == "poker-port-mapping" for t in threading.enumerate()))


def test_cancel_between_renewal_lookup_and_add_prevents_mutation(leases):
    router = Router()
    control = RenewControl()
    messages = []
    lease = open_fake(router, leases, wait=control, on_lost=messages.append)
    assert control.entered.wait(1)
    router.before_lookup = lease.close
    control.wake.set()
    assert lease._done.wait(1)
    assert len(router.adds) == 1 and not messages


def test_short_actual_lease_renews_before_actual_expiry(leases):
    router = Router()
    control = RenewControl()
    router.after_add = lambda: setattr(router, "entry", (*router.entry[:4], 30))
    lease = open_fake(router, leases, wait=control)
    assert control.entered.wait(1)
    assert 0 < control.delays[0] <= 10
    assert 29 < FakeTimer.instances[-1].delay <= 30
    lease.close()


def soap_fault(code):
    return (f'<s:Envelope xmlns:s="{M._SOAP_NAMESPACE}"><s:Body><s:Fault>'
            f'<detail><UPnPError xmlns="{M._CONTROL_NAMESPACE}"><errorCode>{code}</errorCode>'
            '</UPnPError></detail></s:Fault></s:Body></s:Envelope>').encode()


def soap_entry(**overrides):
    fields = dict(NewInternalClient=LOCAL, NewInternalPort=str(PORT),
                  NewPortMappingDescription="Poker marker", NewEnabled="1", NewLeaseDuration="300")
    fields.update(overrides)
    children = "".join(f"<{key}>{value}</{key}>" for key, value in fields.items())
    return (f'<s:Envelope xmlns:s="{M._SOAP_NAMESPACE}"><s:Body>'
            f'<u:GetSpecificPortMappingEntryResponse xmlns:u="{M._SERVICE_TYPES[0]}">'
            f'{children}</u:GetSpecificPortMappingEntryResponse></s:Body></s:Envelope>').encode()


def soap_empty_body():
    return f'<s:Envelope xmlns:s="{M._SOAP_NAMESPACE}"><s:Body/></s:Envelope>'.encode()


def soap_generic(**overrides):
    fields = dict(NewRemoteHost="", NewExternalPort="50000", NewProtocol="TCP",
                  NewInternalClient=LOCAL, NewInternalPort=str(PORT),
                  NewPortMappingDescription="Other app", NewEnabled="1", NewLeaseDuration="0")
    fields.update(overrides)
    children = "".join(f"<{key}>{value}</{key}>" for key, value in fields.items())
    return (f'<s:Envelope xmlns:s="{M._SOAP_NAMESPACE}"><s:Body>'
            f'<u:GetGenericPortMappingEntryResponse xmlns:u="{M._SERVICE_TYPES[0]}">'
            f'{children}</u:GetGenericPortMappingEntryResponse></s:Body></s:Envelope>').encode()


def canned_soap(monkeypatch, responses):
    replies = iter(responses)
    requests = []

    def respond(request):
        requests.append(request)
        return io.BytesIO(next(replies))

    monkeypatch.setattr(M, "_open_soap", respond)
    return requests


def test_empty_specific_uses_complete_two_entry_enumeration_before_vacancy(monkeypatch):
    calls = canned_soap(monkeypatch, [
        soap_empty_body(), soap_generic(),
        soap_generic(NewExternalPort="50001", NewProtocol="UDP", NewInternalClient="192.168.1.33"),
        soap_fault(713),
    ])
    lookup = M._StrictLookup("http://192.168.1.1/control")
    assert lookup.lookup(55000) is None
    assert len(calls) == 4
    assert "GetSpecificPortMappingEntry" in calls[0].get_header("Soapaction")
    for index, request in enumerate(calls[1:]):
        assert "GetGenericPortMappingEntry" in request.get_header("Soapaction")
        assert f"<NewPortMappingIndex>{index}</NewPortMappingIndex>".encode() in request.data


def test_empty_specific_enumeration_returns_exact_matching_owned_entry(monkeypatch):
    calls = canned_soap(monkeypatch, [
        soap_empty_body(),
        soap_generic(NewExternalPort="55000", NewRemoteHost="8.8.8.8"),
        soap_generic(NewExternalPort="55000", NewProtocol="UDP"),
        soap_generic(NewExternalPort="55000", NewPortMappingDescription="Poker marker", NewLeaseDuration="299"),
        soap_fault(713),
    ])
    assert M._StrictLookup("http://192.168.1.1/control").lookup(55000) == (
        LOCAL, PORT, "Poker marker", True, 299,
    )
    assert len(calls) == 5


@pytest.mark.parametrize("terminal", [soap_empty_body(), soap_fault(714), soap_fault(401),
    soap_fault(501), b"not XML"], ids=["empty-generic", "714", "401", "unknown-fault", "malformed"])
def test_generic_scan_needs_explicit_713_terminator(terminal, monkeypatch):
    calls = canned_soap(monkeypatch, [soap_empty_body(), soap_generic(), terminal])
    with pytest.raises(M.MappingError):
        M._StrictLookup("http://192.168.1.1/control").lookup(55000)
    assert len(calls) == 3


def test_duplicate_generic_key_rejected_even_after_matching_entry(monkeypatch):
    calls = canned_soap(monkeypatch, [
        soap_empty_body(), soap_generic(NewExternalPort="55000"),
        soap_generic(NewExternalPort="55000", NewPortMappingDescription="Other owner"), soap_fault(713),
    ])
    with pytest.raises(M.MappingError, match="repeated"):
        M._StrictLookup("http://192.168.1.1/control").lookup(55000)
    assert len(calls) == 3


@pytest.mark.parametrize("overrides", [
    dict(NewExternalPort="0"), dict(NewExternalPort="65536"), dict(NewExternalPort="1.5"),
    dict(NewProtocol="SCTP"), dict(NewRemoteHost="host.example"),
    dict(NewRemoteHost="224.0.0.1"), dict(NewInternalClient="host.example"),
    dict(NewInternalClient="127.0.0.1"), dict(NewInternalPort="0"),
    dict(NewEnabled="maybe"), dict(NewLeaseDuration="-1"),
    dict(NewLeaseDuration="4294967296"), dict(UnexpectedField="extra"),
    dict(NewInternalClient="255.255.255.255"), dict(NewExternalPort="9" * 5000),
], ids=["zero-external", "large-external", "fractional-external", "protocol", "remote-hostname",
        "remote-multicast", "target-hostname", "target-loopback", "zero-internal", "enabled",
        "negative-lease", "large-lease", "extra-field", "target-broadcast", "oversized-external-integer"])
def test_every_generic_entry_key_and_target_must_be_valid(overrides, monkeypatch):
    canned_soap(monkeypatch, [soap_empty_body(), soap_generic(**overrides), soap_fault(713)])
    with pytest.raises(M.MappingError):
        M._StrictLookup("http://192.168.1.1/control").lookup(55000)


def test_generic_entry_limit_requires_terminator_within_cap(monkeypatch):
    monkeypatch.setattr(M, "_ENUMERATION_MAX_ENTRIES", 2)
    calls = canned_soap(monkeypatch, [
        soap_empty_body(), soap_generic(), soap_generic(NewExternalPort="50001"),
        soap_generic(NewExternalPort="50002"), soap_fault(713),
    ])
    with pytest.raises(M.MappingError, match="entry limit"):
        M._StrictLookup("http://192.168.1.1/control").lookup(55000)
    assert len(calls) == 4


def test_generic_missing_and_duplicate_fields_are_rejected(monkeypatch):
    valid = soap_generic()
    for malformed in (
        valid.replace(b"<NewProtocol>TCP</NewProtocol>", b""),
        valid.replace(b"<NewProtocol>TCP</NewProtocol>", b"<NewProtocol>TCP</NewProtocol>" * 2),
    ):
        canned_soap(monkeypatch, [soap_empty_body(), malformed, soap_fault(713)])
        with pytest.raises(M.MappingError):
            M._StrictLookup("http://192.168.1.1/control").lookup(55000)


def test_generic_scan_accepts_exact_cap_followed_by_explicit_713(monkeypatch):
    monkeypatch.setattr(M, "_ENUMERATION_MAX_ENTRIES", 2)
    canned_soap(monkeypatch, [
        soap_empty_body(), soap_generic(), soap_generic(NewExternalPort="50001"), soap_fault(713),
    ])
    assert M._StrictLookup("http://192.168.1.1/control").lookup(55000) is None


def test_generic_enumeration_whole_lookup_deadline_is_enforced(monkeypatch):
    now = [0.0]
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return io.BytesIO(soap_empty_body())
        now[0] = 6.0
        return io.BytesIO(soap_fault(713))

    monkeypatch.setattr(M, "_open_soap", respond)
    lookup = M._StrictLookup("http://192.168.1.1/control", clock=lambda: now[0])
    with pytest.raises(M.MappingError, match="time limit"):
        lookup.lookup(55000)
    assert len(calls) == 2


def test_generic_calls_use_remaining_lookup_budget(monkeypatch):
    now = [0.0]
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            now[0] = 4.5
            return io.BytesIO(soap_empty_body())
        return io.BytesIO(soap_fault(713))

    monkeypatch.setattr(M, "_open_soap", respond)
    lookup = M._StrictLookup("http://192.168.1.1/control", clock=lambda: now[0])
    assert lookup.lookup(55000) is None
    assert [request._poker_timeout for request in calls] == [3.0, 1.5]


def test_generic_transport_timeout_is_not_vacancy(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return io.BytesIO(soap_empty_body())
        raise TimeoutError("fake router timeout")

    monkeypatch.setattr(M, "_open_soap", respond)
    with pytest.raises(M.MappingError, match="lookup failed"):
        M._StrictLookup("http://192.168.1.1/control").lookup(55000)
    assert len(calls) == 2


def test_unproven_enumeration_never_reaches_add_mapping(monkeypatch, leases):
    canned_soap(monkeypatch, [soap_empty_body(), soap_generic(), soap_empty_body()])
    lookup = M._StrictLookup("http://192.168.1.1/control")
    router = Router()
    router.getspecificportmapping = lambda port, *_: lookup.lookup(port)
    with pytest.raises(M.MappingError):
        open_fake(router, leases)
    assert not router.adds and not router.deletes


def test_strict_lookup_only_explicit_714_means_vacancy(monkeypatch):
    requests = []
    monkeypatch.setattr(M, "_open_soap", lambda req: (requests.append(req), io.BytesIO(soap_fault(714)))[1])
    lookup = M._StrictLookup("http://192.168.1.1:5000/control")
    assert lookup.lookup(55000) is None
    assert len(requests) == 1
    assert b"<NewExternalPort>55000</NewExternalPort>" in requests[0].data
    assert requests[0].get_method() == "POST"


def test_strict_lookup_decodes_verified_five_field_response(monkeypatch):
    monkeypatch.setattr(M, "_open_soap", lambda _: io.BytesIO(soap_entry()))
    lookup = M._StrictLookup("http://192.168.1.1/control")
    assert lookup.lookup(55000) == (LOCAL, PORT, "Poker marker", True, 300)


@pytest.mark.parametrize("fault", [501, 606, 718, 725])
def test_other_soap_faults_never_imply_vacancy_or_change_service(fault, monkeypatch):
    calls = []
    monkeypatch.setattr(M, "_open_soap", lambda req: (calls.append(req), io.BytesIO(soap_fault(fault)))[1])
    with pytest.raises(M.MappingError):
        M._StrictLookup("http://192.168.1.1/control").lookup(55000)
    assert len(calls) == 1


def test_only_invalid_action_401_negotiates_next_service(monkeypatch):
    replies = iter([soap_fault(401), soap_fault(714), soap_fault(714)])
    calls = []
    monkeypatch.setattr(M, "_open_soap", lambda req: (calls.append(req), io.BytesIO(next(replies)))[1])
    lookup = M._StrictLookup("http://192.168.1.1/control")
    assert lookup.lookup(55000) is None and lookup.lookup(55000) is None
    assert M._SERVICE_TYPES[0] in calls[0].get_header("Soapaction")
    assert M._SERVICE_TYPES[1] in calls[1].get_header("Soapaction")
    assert M._SERVICE_TYPES[1] in calls[2].get_header("Soapaction")


def test_http_500_explicit_714_is_vacancy(monkeypatch):
    def fault(_):
        raise HTTPError("http://192.168.1.1/control", 500, "fault", {}, io.BytesIO(soap_fault(714)))
    monkeypatch.setattr(M, "_open_soap", fault)
    assert M._StrictLookup("http://192.168.1.1/control").lookup(55000) is None


@pytest.mark.parametrize("response", [b"", b"no", b"x" * 65537,
    b'<!DOCTYPE a [<!ENTITY x "bad">]><a/>',
    '<!DOCTYPE a [<!ENTITY x "bad">]><a/>'.encode("utf-16"),
    '<!DOCTYPE a [<!ENTITY x "bad">]><a/>'.encode("utf-32"),
    b"<Envelope><Body/></Envelope>",
    soap_entry(NewEnabled="yes"), soap_entry(NewLeaseDuration="-1"), soap_entry(NewInternalPort="1.5")],
    ids=["empty", "not-xml", "oversized", "doctype", "utf16-doctype", "utf32-doctype",
         "wrong-envelope", "enabled", "negative-lease", "fractional-port"])
def test_invalid_soap_response_is_failure_not_vacancy(response, monkeypatch):
    monkeypatch.setattr(M, "_open_soap", lambda _: io.BytesIO(response))
    with pytest.raises(M.MappingError):
        M._StrictLookup("http://192.168.1.1/control").lookup(55000)


@pytest.mark.parametrize("url", ["http://example.test/control", "http://8.8.8.8/control",
    "http://127.0.0.1/control", "http://169.254.1.1/control", "https://192.168.1.1/control",
    "http://user:pass@192.168.1.1/control", "http://192.168.1.1:70000/control", "file:///x",
    "http://192.168.1.1/control#fragment", "http://[::1]/control"])
def test_control_endpoint_must_be_literal_private_http_router(url):
    with pytest.raises(M.MappingError):
        M._StrictLookup(url)


def test_soap_opener_disables_proxy_and_redirects(monkeypatch):
    captured = []

    class Opener:
        def open(self, request, *, timeout):
            captured.append(timeout)
            return "fake response"

    def build(*handlers):
        captured.extend(handlers)
        return Opener()

    monkeypatch.setattr(M, "build_opener", build)
    assert _REAL_OPEN_SOAP(object()) == "fake response"
    assert captured[0].proxies == {}
    assert captured[1].redirect_request(None, None, None, None, None, None) is None
    assert captured[2] == 3.0
