"""Cancellable mailbox HTTPS with deterministic DNS/TLS/HTTP stand-ins."""
from __future__ import annotations

import asyncio
import json
import ssl
import sys
import types

import pytest

from holdem import ice_probe


GIST = "ab" * 16


@pytest.fixture
def mailbox(monkeypatch):
    state = types.SimpleNamespace(
        raw=b"", offset=0, block_read=False, block_dns=False, block_close=False,
        dns_call=None, connect_call=None, request=b"", closed=False, aborted=False,
        close_waited=False, opened=None,
    )

    class Resolver:
        async def resolve(self, *args, **kwargs):
            state.dns_call = (args, kwargs)
            if state.block_dns:
                await asyncio.Event().wait()
            return [types.SimpleNamespace(address="192.0.2.10")]

    resolver_module = types.ModuleType("dns.asyncresolver")
    resolver_module.Resolver = Resolver
    if "dns" not in sys.modules:
        dns_module = types.ModuleType("dns")
        dns_module.__path__ = []
        monkeypatch.setitem(sys.modules, "dns", dns_module)
    monkeypatch.setitem(sys.modules, "dns.asyncresolver", resolver_module)

    class Reader:
        async def read(self, count):
            assert 1 <= count <= 16384
            if state.block_read:
                await asyncio.Event().wait()
            result = state.raw[state.offset:state.offset + count]
            state.offset += len(result)
            return result

    class Writer:
        transport = types.SimpleNamespace(abort=lambda: setattr(state, "aborted", True))

        def write(self, data):
            state.request += data

        async def drain(self):
            pass

        def close(self):
            state.closed = True

        async def wait_closed(self):
            state.close_waited = True
            if state.block_close:
                await asyncio.Event().wait()

    async def connect(*args, **kwargs):
        state.connect_call = (args, kwargs)
        if state.opened is not None:
            state.opened.set()
        return Reader(), Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)
    return state


def _metadata(offer=None):
    files = {} if offer is None else {"offer.json": {"content": json.dumps(offer), "truncated": False}}
    return json.dumps({"files": files}).encode("utf-8")


def _response(body, *, status=200, headers=None):
    headers = headers or [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
    head = f"HTTP/1.1 {status} Test\r\n".encode("ascii")
    head += b"".join(f"{name}: {value}\r\n".encode("ascii") for name, value in headers)
    return head + b"\r\n" + body


def test_content_length_offer_uses_verified_tls_and_no_credentials(mailbox):
    offer = {"v": 1, "kind": "offer", "ciphertext": "opaque"}
    mailbox.raw = _response(_metadata(offer))
    assert asyncio.run(ice_probe._read_mailbox(GIST)) == offer
    assert mailbox.dns_call == (("api.github.com", "A"), {"lifetime": 5.0, "search": False})
    args, kwargs = mailbox.connect_call
    assert args == ("192.0.2.10", 443)
    assert kwargs["server_hostname"] == "api.github.com"
    assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert kwargs["ssl"].check_hostname is True
    assert b"Connection: close\r\n" in mailbox.request
    assert b"Accept-Encoding: identity\r\n" in mailbox.request
    assert b"Authorization:" not in mailbox.request
    assert mailbox.closed and mailbox.close_waited


def test_chunked_offer_and_trailer_decode_within_the_same_request(mailbox):
    offer = {"v": 1, "ciphertext": "opaque"}
    body = _metadata(offer)
    chunks = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\nX-Trailer: safe\r\n\r\n"
    mailbox.raw = _response(chunks, headers=[
        ("Content-Type", "application/json; charset=utf-8"), ("Transfer-Encoding", "chunked"),
    ])
    assert asyncio.run(ice_probe._read_mailbox(GIST)) == offer
    assert mailbox.closed


@pytest.mark.parametrize("status,body", [(404, b""), (200, _metadata())])
def test_missing_offer_is_a_bounded_pending_result(mailbox, status, body):
    mailbox.raw = _response(body, status=status)
    assert asyncio.run(ice_probe._read_mailbox(GIST)) is None
    assert mailbox.closed


@pytest.mark.parametrize("raw", [
    _response(b"{}", status=302),
    _response(b"{}", status=403),
    _response(b"{}", status=429),
    _response(b"{}", headers=[("Content-Length", "2"), ("Content-Length", "2")]),
    _response(b"{}", headers=[("Content-Length", "2"), ("Transfer-Encoding", "chunked")]),
    _response(b"{}", headers=[("Content-Encoding", "gzip")]),
    _response(b"{}", headers=[("Content-Length", str(1024 * 1024 + 1))]),
    _response(b"{}", headers=[("Content-Length", "3")]),
    _response(b"{}", headers=[("Content-Type", "text/plain"), ("Content-Length", "2")]),
    _response(b"{}", headers=[("Transfer-Encoding", "gzip")]),
    _response(b"zz\r\n", headers=[("Transfer-Encoding", "chunked")]),
    _response(b"{}", headers=[("Content-Length", "2")]) + b"unexpected-trailing-bytes",
    b"HTTP/1.1 200 Test\r\nX-Too-Large: " + b"a" * (16 * 1024) + b"\r\n\r\n{}",
    _response(b"x" * (1024 * 1024 + 64 * 1024), headers=[("Content-Type", "application/json")]),
], ids=[
    "redirect", "forbidden", "rate-limit", "duplicate-length", "mixed-framing",
    "compressed", "declared-body-limit", "truncated-body", "wrong-content-type",
    "unsupported-transfer", "malformed-chunk", "trailing-bytes", "header-limit", "wire-limit",
])
def test_invalid_or_oversized_http_is_rejected_without_raw_error_output(mailbox, raw):
    mailbox.raw = raw
    with pytest.raises(ice_probe.ProbeFailure, match="^mailbox_unavailable$"):
        asyncio.run(ice_probe._read_mailbox(GIST))
    assert mailbox.closed


@pytest.mark.parametrize("metadata", [
    [],
    {"files": []},
    {"files": {"offer.json": {"truncated": True, "content": "{}"}}},
    {"files": {"offer.json": {"truncated": False, "content": "not json"}}},
])
def test_invalid_offer_metadata_fails_with_a_fixed_setup_code(mailbox, metadata):
    mailbox.raw = _response(json.dumps(metadata).encode())
    with pytest.raises(ice_probe.ProbeFailure, match="^invalid_setup$"):
        asyncio.run(ice_probe._read_mailbox(GIST))
    assert mailbox.closed


def test_whole_request_timeout_cancels_read_and_bounds_tls_shutdown(mailbox, monkeypatch):
    original_wait_for = asyncio.wait_for

    async def short_budget(awaitable, timeout):
        return await original_wait_for(awaitable, timeout=0.02 if timeout in (10.0, 1.0) else timeout)

    monkeypatch.setattr(asyncio, "wait_for", short_budget)
    mailbox.block_read = True
    mailbox.block_close = True

    async def check():
        baseline = set(asyncio.all_tasks())
        with pytest.raises(ice_probe.ProbeFailure, match="^mailbox_unavailable$"):
            await ice_probe._read_mailbox(GIST)
        await asyncio.sleep(0)
        assert set(asyncio.all_tasks()) == baseline

    asyncio.run(check())
    assert mailbox.closed and mailbox.close_waited and mailbox.aborted


@pytest.mark.parametrize("during_dns", [False, True])
def test_cancellation_leaves_no_executor_worker_or_owned_task(mailbox, monkeypatch, during_dns):
    async def check():
        loop = asyncio.get_running_loop()

        def forbidden_executor(*args, **kwargs):
            raise AssertionError("mailbox must not delegate blocking work")

        monkeypatch.setattr(loop, "run_in_executor", forbidden_executor)
        baseline = set(asyncio.all_tasks())
        mailbox.block_read = True
        mailbox.block_dns = during_dns
        mailbox.opened = asyncio.Event()
        operation = asyncio.create_task(ice_probe._read_mailbox(GIST))
        if not during_dns:
            await original_wait(mailbox.opened.wait(), 1)
        else:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        await asyncio.sleep(0)
        assert set(asyncio.all_tasks()) == baseline

    original_wait = asyncio.wait_for
    asyncio.run(check())
    assert mailbox.dns_call is not None
    assert mailbox.connect_call is None if during_dns else mailbox.closed


def test_invalid_mailbox_identifier_never_opens_dns_or_tls(mailbox):
    with pytest.raises(ice_probe.ProbeFailure, match="^invalid_setup$"):
        asyncio.run(ice_probe._read_mailbox("ab\r\nAuthorization: injected"))
    assert mailbox.dns_call is None
    assert mailbox.connect_call is None


def test_invalid_tls_certificate_is_not_retried_or_exposed(mailbox, monkeypatch):
    async def invalid_certificate(*_args, **_kwargs):
        raise ssl.SSLCertVerificationError("private address and certificate details")

    monkeypatch.setattr(asyncio, "open_connection", invalid_certificate)
    with pytest.raises(ice_probe.ProbeFailure, match="^mailbox_unavailable$"):
        asyncio.run(ice_probe._read_mailbox(GIST))


def test_wait_offer_polls_at_ten_second_intervals_without_executor(monkeypatch):
    calls, sleeps = [], []

    async def read(gist):
        calls.append(gist)
        return None if len(calls) == 1 else {"v": 1}

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ice_probe, "_read_mailbox", read)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    assert asyncio.run(ice_probe._wait_offer(GIST)) == {"v": 1}
    assert calls == [GIST, GIST]
    assert sleeps == [10]
