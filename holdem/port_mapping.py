"""Finite, owned UPnP TCP leases for the desktop host adapter.

Importing this module never discovers devices or changes router state.  A lease
owns one daemon worker and one miniupnpc client; all router calls stay on that
worker.  The binding has no cancellable SOAP API, so callers have bounded waits
and at most two workers can exist, including abandoned native calls.  A worker
that returns after cancellation only removes its own entry.  Router leases are
always finite, including when the process exits before cleanup completes.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import ipaddress
import secrets
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import xml.etree.ElementTree as ET


LEASE_SECONDS = 300
RENEW_SECONDS = 90.0
OPEN_TIMEOUT_SECONDS = 20.0
CLOSE_TIMEOUT_SECONDS = 1.0
DISCOVERY_DELAY_MS = 1500
_WORKERS = threading.BoundedSemaphore(2)
_SOAP_NAMESPACE = "http://schemas.xmlsoap.org/soap/envelope/"
_CONTROL_NAMESPACE = "urn:schemas-upnp-org:control-1-0"
_SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
_SOAP_TIMEOUT_SECONDS = 3.0
_SOAP_MAX_BYTES = 65536


class MappingError(RuntimeError):
    """Direct hosting could not obtain or keep a temporary router mapping."""


@dataclass(frozen=True)
class _Entry:
    host: str
    port: int
    description: str
    enabled: bool
    remaining: int


class _UnknownService(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _open_soap(request: Request) -> Any:
    # Router traffic must not go to an environment-configured Internet proxy or
    # follow an SSDP responder's redirect to an unrelated endpoint.
    return build_opener(ProxyHandler({}), _NoRedirect()).open(
        request, timeout=_SOAP_TIMEOUT_SECONDS
    )


class _StrictLookup:
    """Read-only lookup preserving UPnP error codes the native binding discards.

    miniupnpc 2.3.3 returns None for *every* GetSpecific lookup failure.  Only
    fault 714 establishes vacancy, so using that None before AddPortMapping is
    unsafe.  Discover/select/add/delete still use the maintained native library.
    """

    def __init__(self, control_url: str) -> None:
        try:
            if not isinstance(control_url, str) or any(ord(c) < 32 for c in control_url):
                raise ValueError("invalid control endpoint")
            parsed = urlsplit(control_url)
            address = ipaddress.IPv4Address(parsed.hostname or "")
            port = parsed.port if parsed.port is not None else 80
            private_networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            if (
                parsed.scheme != "http" or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or not 1 <= port <= 65535
                or not any(address in ipaddress.IPv4Network(n) for n in private_networks)
            ):
                raise ValueError("invalid control endpoint")
        except (TypeError, ValueError) as exc:
            raise MappingError("The router returned an unsupported control URL.") from exc
        self._control_url = control_url
        self._service: str | None = None

    def lookup(self, port: int) -> tuple[str, int, str, bool, int] | None:
        for service in (self._service,) if self._service else _SERVICE_TYPES:
            assert service is not None
            try:
                result = self._query(port, service)
            except _UnknownService:
                continue
            self._service = service
            return result
        raise MappingError("The router does not support verified TCP mapping lookup.")

    def _query(self, port: int, service: str) -> tuple[str, int, str, bool, int] | None:
        body = (
            '<?xml version="1.0"?>'
            f'<s:Envelope xmlns:s="{_SOAP_NAMESPACE}" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:GetSpecificPortMappingEntry xmlns:u="{service}">'
            '<NewRemoteHost></NewRemoteHost>'
            f'<NewExternalPort>{port}</NewExternalPort>'
            '<NewProtocol>TCP</NewProtocol>'
            '</u:GetSpecificPortMappingEntry></s:Body></s:Envelope>'
        ).encode("ascii")
        request = Request(self._control_url, body, {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service}#GetSpecificPortMappingEntry"',
        }, method="POST")
        try:
            try:
                response = _open_soap(request)
            except HTTPError as exc:
                response = exc  # SOAP faults normally use HTTP 500.
            with response:
                data = response.read(_SOAP_MAX_BYTES + 1)
            if (
                len(data) > _SOAP_MAX_BYTES or b"\0" in data
                or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper()
            ):
                raise MappingError("The router returned an invalid lookup response.")
            envelope = ET.fromstring(data)
        except MappingError:
            raise
        except Exception as exc:
            raise MappingError("The router mapping lookup failed; no mapping was changed.") from exc
        soap_body = envelope.find(f"{{{_SOAP_NAMESPACE}}}Body")
        if envelope.tag != f"{{{_SOAP_NAMESPACE}}}Envelope" or soap_body is None:
            raise MappingError("The router returned an invalid lookup response.")
        fault = soap_body.find(f"{{{_SOAP_NAMESPACE}}}Fault")
        if fault is not None:
            code = fault.findtext(f".//{{{_CONTROL_NAMESPACE}}}errorCode")
            if code == "714":
                return None
            if code == "401":
                raise _UnknownService
            raise MappingError("The router could not verify whether the TCP port is available.")
        result = soap_body.find(f"{{{service}}}GetSpecificPortMappingEntryResponse")
        if result is None:
            raise MappingError("The router returned an invalid lookup response.")
        values: dict[str, str] = {}
        for child in result:
            name = child.tag.rsplit("}", 1)[-1]
            if name in values or len(child):
                raise MappingError("The router returned an invalid port mapping.")
            values[name] = child.text or ""
        try:
            port_text = values["NewInternalPort"]
            lease_text = values["NewLeaseDuration"]
            enabled_text = values["NewEnabled"].strip().lower()
            if not port_text.isascii() or not port_text.isdecimal() or not lease_text.isascii() or not lease_text.isdecimal():
                raise ValueError("invalid numeric field")
            if enabled_text not in ("0", "1", "true", "false"):
                raise ValueError("invalid enabled flag")
            return (
                values["NewInternalClient"], int(port_text),
                values["NewPortMappingDescription"], enabled_text in ("1", "true"),
                int(lease_text),
            )
        except (KeyError, ValueError) as exc:
            raise MappingError("The router returned an invalid port mapping.") from exc


class _MiniupnpcBackend:
    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self._lookup: _StrictLookup | None = None

    @property
    def lanaddr(self) -> str:
        return self._raw.lanaddr

    def discover(self) -> int:
        return self._raw.discover()

    def selectigd(self) -> None:
        self._lookup = _StrictLookup(self._raw.selectigd())

    def externalipaddress(self) -> str:
        return self._raw.externalipaddress()

    def getspecificportmapping(self, port: int, protocol: str, remote_host: str) -> Any:
        assert protocol == "TCP" and not remote_host and self._lookup is not None
        return self._lookup.lookup(port)

    def addportmapping(self, *args: Any) -> bool:
        return self._raw.addportmapping(*args)

    def deleteportmapping(self, *args: Any) -> bool:
        return self._raw.deleteportmapping(*args)


def _make_client(local_ip: str) -> Any:
    # Keep the native dependency out of core imports and offline/local games.
    try:
        import miniupnpc
    except ImportError as exc:
        raise MappingError("Automatic router mapping is unavailable in this build.") from exc
    return _MiniupnpcBackend(miniupnpc.UPnP(
        multicastif=local_ip, discoverdelay=DISCOVERY_DELAY_MS
    ))


def _ipv4(value: Any, *, public: bool = False) -> str:
    try:
        if not isinstance(value, str):
            raise ipaddress.AddressValueError("not an IPv4 string")
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, TypeError) as exc:
        raise MappingError("The router did not report a valid IPv4 address.") from exc
    if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local:
        raise MappingError("A usable network IPv4 address is required.")
    if public and (not address.is_global or address.is_reserved):
        raise MappingError(
            "The router has no public IPv4 address; direct hosting may be behind another router or carrier NAT."
        )
    return str(address)


def _entry(value: Any) -> _Entry | None:
    """Decode miniupnpc 2.3.3 GetSpecificPortMappingEntry's five fields."""
    if value is None:
        return None
    if not isinstance(value, tuple) or len(value) != 5:
        raise MappingError("The router returned an invalid port mapping.")
    host, port, description, enabled, remaining = value
    if (
        not isinstance(host, str)
        or isinstance(port, bool) or not isinstance(port, int)
        or not 1 <= port <= 65535
        or not isinstance(description, str)
        or not isinstance(enabled, bool)
        or isinstance(remaining, bool) or not isinstance(remaining, int)
        or remaining < 0
    ):
        raise MappingError("The router returned an invalid port mapping.")
    return _Entry(host, port, description, enabled, remaining)


class MappingLease:
    """One temporary TCP entry; close is idempotent and waits at most one second.

    The router API has no compare-and-delete primitive.  Every renewal/deletion
    verifies the random ownership marker and target immediately before acting;
    unrelated/replaced entries observed at that check are never changed.
    """

    def __init__(
        self,
        local_ip: str,
        local_port: int,
        on_lost: Callable[[str], None],
        *,
        client_factory: Callable[[str], Any],
        clock: Callable[[], float],
        wait: Callable[[threading.Event, float], bool],
        timer_factory: Callable[..., Any],
        open_timeout: float,
        close_timeout: float,
    ) -> None:
        self._local_ip = local_ip
        self._local_port = local_port
        self._on_lost = on_lost
        self._client_factory = client_factory
        self._clock = clock
        self._wait = wait
        self._timer_factory = timer_factory
        self._deadline = clock() + open_timeout
        self._close_timeout = close_timeout
        self._description = "Poker " + secrets.token_hex(16)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._done = threading.Event()
        self._state_lock = threading.Lock()
        self._external_address: tuple[str, int] | None = None
        self._failure: MappingError | None = None
        self._notified = False
        self._client: Any = None
        # A fresh high port reduces the unavoidable inter-process check/add race.
        self._external_port = 49152 + secrets.randbelow(16384)
        self._may_own_entry = False
        self._lease_deadline: float | None = None
        self._expiry_timer: Any = None
        self._expiry_generation = 0
        self._thread = threading.Thread(
            target=self._run, name="poker-port-mapping", daemon=True
        )

    @property
    def external_address(self) -> tuple[str, int]:
        with self._state_lock:
            if self._external_address is None:
                raise MappingError("The temporary router mapping is not ready.")
            return self._external_address

    def close(self) -> None:
        self._stop.set()
        self._cancel_expiry()
        # on_lost may close its owning Session, which in turn closes this lease.
        if threading.current_thread() is not self._thread:
            self._done.wait(self._close_timeout)

    def _check_open(self) -> None:
        if self._stop.is_set() or self._clock() >= self._deadline:
            raise MappingError("Automatic router mapping timed out or was cancelled.")

    def _lookup(self) -> _Entry | None:
        return _entry(self._client.getspecificportmapping(self._external_port, "TCP", ""))

    def _owns(self, entry: _Entry | None) -> bool:
        return entry is not None and (
            entry.host == self._local_ip
            and entry.port == self._local_port
            and entry.description == self._description
        )

    def _verified(self, entry: _Entry | None) -> float:
        if not self._owns(entry):
            raise MappingError("The temporary router mapping was missing or changed.")
        assert entry is not None
        if not entry.enabled or not 0 < entry.remaining <= LEASE_SECONDS:
            raise MappingError("The router did not provide an enabled, finite temporary lease.")
        remaining = float(entry.remaining)
        if self._lease_deadline is not None:
            remaining = min(remaining, self._lease_deadline - self._clock())
        if remaining <= 0:
            raise MappingError("The temporary router mapping expired before verification completed.")
        return remaining

    def _add(self) -> None:
        if self._stop.is_set():
            raise MappingError("Automatic router mapping was cancelled.")
        started = self._clock()
        if self._external_address is not None and self._lease_deadline is not None and started >= self._lease_deadline:
            raise MappingError("The temporary router mapping expired before renewal completed.")
        self._lease_deadline = started + LEASE_SECONDS
        result = self._client.addportmapping(
            self._external_port, "TCP", self._local_ip, self._local_port,
            self._description, "", LEASE_SECONDS,
        )
        if result is not True:
            raise MappingError("The router refused a temporary TCP mapping.")

    def _establish(self) -> float:
        self._check_open()
        self._client = self._client_factory(self._local_ip)
        self._check_open()
        if self._client.discover() <= 0:
            raise MappingError("No router supporting automatic port mapping was found.")
        self._check_open()
        self._client.selectigd()
        self._check_open()
        if _ipv4(self._client.lanaddr) != self._local_ip:
            raise MappingError("The discovered router uses a different local network interface.")
        public_ip = _ipv4(self._client.externalipaddress(), public=True)
        self._check_open()
        # Do not use AddAnyPortMapping: miniupnpc 2.3.3 hardcodes lease=0 there.
        # Refuse an occupied port, including one pointing to this same computer.
        if self._lookup() is not None:
            raise MappingError("The requested external TCP port is already in use.")
        self._check_open()
        # Mark before the call: a router can commit an add but lose its response.
        self._may_own_entry = True
        self._add()
        self._check_open()
        remaining = self._verified(self._lookup())
        self._check_open()
        with self._state_lock:
            if self._stop.is_set():
                raise MappingError("Automatic router mapping was cancelled.")
            self._external_address = (public_ip, self._external_port)
        return remaining

    def _renew(self) -> float:
        if self._stop.is_set():
            raise MappingError("Automatic router mapping was cancelled.")
        self._verified(self._lookup())
        if self._stop.is_set():
            raise MappingError("Automatic router mapping was cancelled.")
        if _ipv4(self._client.externalipaddress(), public=True) != self.external_address[0]:
            raise MappingError("The router's public address changed during the game.")
        if self._stop.is_set():
            raise MappingError("Automatic router mapping was cancelled.")
        self._add()
        return self._verified(self._lookup())

    def _cleanup(self) -> None:
        if self._client is None or not self._may_own_entry:
            return
        try:
            # Never delete an entry replaced by another program/host generation.
            if self._owns(self._lookup()):
                self._client.deleteportmapping(self._external_port, "TCP", "")
        except Exception:
            # No permanent fallback: the requested finite lease expires by itself.
            pass

    def _notify_lost(self, message: str, *, expected_generation: int | None = None) -> None:
        with self._state_lock:
            if (
                self._notified or self._stop.is_set()
                or (expected_generation is not None and expected_generation != self._expiry_generation)
            ):
                return
            self._notified = True
            self._stop.set()
        # The callback can re-enter close() and other lifecycle code. No state or
        # backend lock is held, and no further add is issued after the callback.
        try:
            self._on_lost(message)
        except Exception:
            pass

    def _cancel_expiry(self) -> None:
        with self._state_lock:
            self._expiry_generation += 1
            timer, self._expiry_timer = self._expiry_timer, None
        if timer is not None:
            timer.cancel()

    def _arm_expiry(self, remaining: float) -> None:
        self._cancel_expiry()
        with self._state_lock:
            if self._stop.is_set():
                return
            generation = self._expiry_generation

            def expired() -> None:
                self._notify_lost(
                    "The temporary router mapping expired before renewal completed.",
                    expected_generation=generation,
                )

            timer = self._timer_factory(float(remaining), expired)
            timer.daemon = True
            self._expiry_timer = timer
        timer.start()

    def _run(self) -> None:
        established = False
        try:
            remaining = self._establish()
            established = True
            self._arm_expiry(remaining)
            self._ready.set()
            while not self._wait(self._stop, min(RENEW_SECONDS, remaining / 3.0)):
                if self._stop.is_set():
                    break
                remaining = self._renew()
                self._arm_expiry(remaining)
        except Exception as exc:
            failure = exc if isinstance(exc, MappingError) else MappingError(
                "The router could not maintain a temporary TCP mapping."
            )
            if established:
                self._notify_lost(str(failure))
            else:
                self._failure = failure
        finally:
            self._ready.set()
            self._stop.set()
            self._cancel_expiry()
            self._cleanup()
            self._done.set()
            _WORKERS.release()


def _open_tcp_mapping(
    local_ip: str,
    local_port: int,
    *,
    on_lost: Callable[[str], None],
    client_factory: Callable[[str], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    wait: Callable[[threading.Event, float], bool] = lambda event, delay: event.wait(delay),
    timer_factory: Callable[..., Any] = threading.Timer,
    open_timeout: float = OPEN_TIMEOUT_SECONDS,
    close_timeout: float = CLOSE_TIMEOUT_SECONDS,
) -> MappingLease:
    local_ip = _ipv4(local_ip)
    if isinstance(local_port, bool) or not isinstance(local_port, int) or not 1 <= local_port <= 65535:
        raise MappingError("A valid local TCP listening port is required.")
    if not callable(on_lost):
        raise TypeError("on_lost must be callable")
    if not _WORKERS.acquire(blocking=False):
        raise MappingError("A previous router operation is still finishing; try again shortly.")
    try:
        lease = MappingLease(
            local_ip, local_port, on_lost, client_factory=client_factory or _make_client, clock=clock,
            wait=wait, timer_factory=timer_factory, open_timeout=open_timeout,
            close_timeout=close_timeout,
        )
        lease._thread.start()
    except Exception:
        _WORKERS.release()
        raise
    if not lease._ready.wait(open_timeout):
        lease._stop.set()
        raise MappingError("Automatic router mapping timed out; direct hosting is unavailable.")
    if lease._failure is not None:
        raise lease._failure
    if lease._stop.is_set():
        raise MappingError("The temporary router mapping was lost while starting.")
    return lease


def open_tcp_mapping(
    local_ip: str, local_port: int, *, on_lost: Callable[[str], None]
) -> MappingLease:
    """Open one verified, renewable 300-second TCP mapping for a live listener.

    This changes router state and must only be called after the user chooses
    Internet hosting. No VPN, relay, permanent mapping, or firewall rule is added.
    """
    return _open_tcp_mapping(local_ip, local_port, on_lost=on_lost)
