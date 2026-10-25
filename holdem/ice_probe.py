"""Bounded engineering proof for packaged, direct-only Windows connections.

This command never changes router/firewall settings or persistent game state.
Machine setup uses an encrypted offer in a temporary secret GitHub gist and an
encrypted answer artifact. Only allowlisted public reports may be uploaded.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
from pathlib import Path

from .ice_signaling import (
    ProbeKeys, SetupError, canonical, load_json, make_ready, open_description,
    read_json, seal_description, validate_context, validate_envelope,
    validate_ready, validate_report, write_json,
)


class ProbeFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _payload(nonce: str, size: int) -> bytes:
    return hashlib.shake_256(b"holdem-direct-probe-v1\x00" + bytes.fromhex(nonce) +
                            size.to_bytes(4, "big")).digest(size)


async def _exchange(peer, role: str, nonce: str, report: dict) -> None:
    """Application receipts prove delivery, not merely local buffer draining."""
    async def send(data):
        await peer.send(data)
        report["sent_messages"] += 1

    async def receive(expected):
        actual = await peer.receive(timeout=35)
        report["received_messages"] += 1
        if actual != expected:
            raise ProbeFailure("exchange_failed")

    for size in (4096, 32768, 512 * 1024):
        data = _payload(nonce, size)
        if role == "host":
            await send(data)
            await receive(data)
        else:
            await receive(data)
            await send(data)
    done = b"DONE:" + bytes.fromhex(nonce)
    ack = b"ACK:" + bytes.fromhex(nonce)
    finish = b"FIN:" + bytes.fromhex(nonce)
    if role == "host":
        await send(done)
        await receive(ack)
        await send(finish)
        # Let the peer acknowledge the last SCTP packet before teardown.
        await asyncio.sleep(0.5)
    else:
        await receive(done)
        await send(ack)
        await receive(finish)
        await asyncio.sleep(0.5)


async def _read_mailbox(gist_id: str) -> dict | None:
    """One cancellable HTTPS request; no executor or credential is involved."""
    import http.client
    import io
    import ipaddress
    import json
    import ssl

    if type(gist_id) is not str or re.fullmatch(r"[0-9a-f]{32}", gist_id) is None:
        raise ProbeFailure("invalid_setup")
    body_limit = 1024 * 1024
    header_limit = 16 * 1024
    wire_limit = body_limit + 64 * 1024
    writer = None

    async def fetch():
        nonlocal writer
        # aioice already depends on dnspython. Its asynchronous resolver keeps
        # cancellation independent of a platform getaddrinfo worker thread.
        from dns.asyncresolver import Resolver
        addresses = await Resolver().resolve(
            "api.github.com", "A", lifetime=5.0, search=False)
        address = str(ipaddress.IPv4Address(next(iter(addresses)).address))
        reader, writer = await asyncio.open_connection(
            address, 443, ssl=ssl.create_default_context(),
            server_hostname="api.github.com", ssl_handshake_timeout=5.0)
        request = (
            "GET /gists/" + gist_id + " HTTP/1.1\r\n"
            "Host: api.github.com\r\n"
            "Accept: application/vnd.github+json\r\n"
            "Accept-Encoding: identity\r\n"
            "User-Agent: holdem-direct-proof\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(request)
        await writer.drain()
        raw = bytearray()
        while True:
            chunk = await reader.read(min(16384, wire_limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > wire_limit:
                raise ProbeFailure("mailbox_unavailable")
            if b"\r\n\r\n" not in raw and len(raw) > header_limit:
                raise ProbeFailure("mailbox_unavailable")
        boundary = raw.find(b"\r\n\r\n")
        if boundary < 0 or boundary + 4 > header_limit:
            raise ProbeFailure("mailbox_unavailable")
        first_line = bytes(raw).split(b"\r\n", 1)[0]
        if re.fullmatch(rb"HTTP/1\.[01] [0-9]{3} [\x20-\x7e]*", first_line) is None:
            raise ProbeFailure("mailbox_unavailable")

        # Parse only bounded in-memory bytes. The standard parser handles
        # chunk sizes, trailers and truncated Content-Length bodies. It never
        # gets a network socket and cannot extend the request's deadline.
        class ResponseBuffer(io.BytesIO):
            def close(self):
                pass

        buffer = ResponseBuffer(raw)

        class ResponseSocket:
            def makefile(self, *_args, **_kwargs):
                return buffer

        response = http.client.HTTPResponse(ResponseSocket())
        response.begin()
        headers = response.getheaders()
        token = set("!#$%&'*+-.^_|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz") | {chr(96)}
        if response.headers.defects or len(headers) > 64 or any(
                not name or any(char not in token for char in name) or
                any(ord(char) < 32 and char != "\t" or ord(char) > 126 for char in value)
                for name, value in headers):
            raise ProbeFailure("mailbox_unavailable")
        length = response.headers.get_all("Content-Length", [])
        transfer = response.headers.get_all("Transfer-Encoding", [])
        encoding = response.headers.get_all("Content-Encoding", [])
        if (len(length) > 1 or len(transfer) > 1 or len(encoding) > 1 or
                (length and transfer) or
                (transfer and transfer[0].strip().lower() != "chunked") or
                (encoding and encoding[0].strip().lower() != "identity")):
            raise ProbeFailure("mailbox_unavailable")
        if length and (re.fullmatch(r"[0-9]{1,10}", length[0]) is None or int(length[0]) > body_limit):
            raise ProbeFailure("mailbox_unavailable")
        if response.status not in (200, 404):
            # In particular, never follow redirects or retry rate-limit errors.
            raise ProbeFailure("mailbox_unavailable")
        body = response.read()
        if len(body) > body_limit or buffer.tell() != len(raw):
            raise ProbeFailure("mailbox_unavailable")
        if response.status == 404:
            return None
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ProbeFailure("mailbox_unavailable")
        # Gist metadata has its own larger bound; its encrypted offer still
        # passes the smaller, duplicate-rejecting setup JSON parser.
        metadata = json.loads(body)
        if type(metadata) is not dict or type(metadata.get("files")) is not dict:
            raise ProbeFailure("invalid_setup")
        entry = metadata["files"].get("offer.json")
        if entry is None:
            return None
        if (type(entry) is not dict or entry.get("truncated") is not False or
                type(entry.get("content")) is not str):
            raise ProbeFailure("invalid_setup")
        try:
            return load_json(entry["content"].encode("utf-8"))
        except SetupError:
            raise ProbeFailure("invalid_setup") from None

    try:
        # One absolute request budget covers DNS, TLS, headers, body and JSON.
        return await asyncio.wait_for(fetch(), timeout=10.0)
    except ProbeFailure:
        raise
    except Exception:
        raise ProbeFailure("mailbox_unavailable") from None
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                try:
                    writer.transport.abort()
                except Exception:
                    pass


async def _wait_offer(gist_id: str) -> dict:
    # At most 30 API requests in a five-minute run, respecting the anonymous
    # limit. A 403/429 fails visibly instead of polling harder.
    while True:
        value = await _read_mailbox(gist_id)
        if value is not None:
            return value
        await asyncio.sleep(10)


async def _wait_file(path: Path) -> dict:
    while not path.exists():
        await asyncio.sleep(0.25)
    return read_json(path)


async def _run(args) -> int:
    from .ice_peer import DirectPeer

    context = validate_context(read_json(args.context_file))
    keys = ProbeKeys.load(args.key_dir)
    role = "host" if args.command == "offer" else "guest"
    if role == "host" and (keys.signing_public != context["host_signing_public"] or
                           keys.encryption_public != context["host_encryption_public"]):
        raise SetupError("host keys do not match workflow")
    if type(args.timeout) is not int or not 30 <= args.timeout <= 300:
        raise SetupError("proof budget must be between 30 and 300 seconds")
    report = {
        "v": 1, "context": context, "role": role, "status": "failed",
        "failure_code": "internal_error", "sent_messages": 0, "received_messages": 0,
        "direct": False, "local_candidate_type": None, "remote_candidate_type": None,
        "protocol": None, "cleanup": {"peer_closed": False, "owned_tasks_pending": 0},
    }
    baseline = set(asyncio.all_tasks())
    peer = DirectPeer(stun_urls=() if args.no_stun else ("stun:stun.l.google.com:19302",))

    async def connect():
        if role == "host":
            ready = validate_ready(read_json(args.guest_ready_file), context)
            description = await peer.create_offer()
            envelope = seal_description(description, context, keys, ready["guest_encryption_public"])
            write_json(args.offer_file, envelope)
            answer = await _wait_file(args.answer_file)
            description = open_description(answer, context, keys, kind="answer",
                                           signer=ready["guest_signing_public"])
            await peer.accept_answer(description)
        else:
            envelope = await _wait_offer(args.mailbox_gist_id)
            description = open_description(envelope, context, keys, kind="offer",
                                           signer=context["host_signing_public"])
            answer = await peer.accept_offer(description)
            write_json(args.answer_file, seal_description(
                answer, context, keys, context["host_encryption_public"]))
        try:
            await peer.wait_open(timeout=45)
        except asyncio.TimeoutError:
            raise ProbeFailure("connect_timeout") from None
        route = peer.diagnostics().get("selected_pair")
        if route:
            report.update(local_candidate_type=route["local_type"],
                          remote_candidate_type=route["remote_type"], protocol=route["protocol"])
            report["direct"] = (route["protocol"] == "udp" and
                                route["local_type"] in ("host", "srflx", "prflx") and
                                route["remote_type"] in ("host", "srflx", "prflx"))
        if not report["direct"]:
            raise ProbeFailure("not_direct")
        await _exchange(peer, role, context["nonce"], report)

    async def cancellation():
        while args.cancel_file is None or not args.cancel_file.exists():
            await asyncio.sleep(0.25)

    operation = asyncio.create_task(connect(), name="holdem-probe-operation")
    cancelled = asyncio.create_task(cancellation(), name="holdem-probe-cancellation")
    try:
        # Reserve time inside the advertised process budget for an in-flight
        # HTTPS close (1s) and the pinned native setup/socket teardown (19s).
        # The workflow's deadline must include cleanup and report publication.
        done, _ = await asyncio.wait((operation, cancelled), timeout=args.timeout - 25,
                                     return_when=asyncio.FIRST_COMPLETED)
        if cancelled in done:
            raise ProbeFailure("cancelled")
        if operation not in done:
            raise ProbeFailure("setup_timeout")
        await operation
        report.update(status="connected", failure_code=None)
    except SetupError:
        report["failure_code"] = "invalid_setup"
    except ProbeFailure as exc:
        report["failure_code"] = exc.code
        if exc.code == "cancelled":
            report["status"] = "cancelled"
    except Exception:
        # Exception strings can include SDP, ICE passwords or addresses. Only
        # fixed failure codes leave this process in a publishable report.
        report["failure_code"] = "exchange_failed"
    finally:
        for task in (operation, cancelled):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, cancelled, return_exceptions=True)
        try:
            await peer.close()
            report["cleanup"]["peer_closed"] = True
        except Exception:
            report.update(status="failed", failure_code="cleanup_failed")
        await asyncio.sleep(0)
        pending = [task for task in asyncio.all_tasks() - baseline if not task.done()]
        report["cleanup"]["owned_tasks_pending"] = len(pending)
        if pending:
            report.update(status="failed", failure_code="cleanup_failed")
    validate_report(report, context)
    write_json(args.report_file, report)
    return 0 if report["status"] == "connected" else 1


async def _self_test() -> dict:
    from .ice_peer import DirectPeer

    baseline = set(asyncio.all_tasks())
    peers = [DirectPeer(stun_urls=()), DirectPeer(stun_urls=())]
    received = []
    route = None
    try:
        answer = await peers[1].accept_offer(await peers[0].create_offer())
        await peers[0].accept_answer(answer)
        await asyncio.gather(*(peer.wait_open(timeout=15) for peer in peers))
        route = peers[0].diagnostics()["selected_pair"]
        async def sender():
            for size in (4096, 32768, 512 * 1024):
                payload = _payload("01" * 32, size)
                await peers[0].send(payload)
                if await peers[0].receive() != payload:
                    raise ProbeFailure("exchange_failed")
        async def receiver():
            for size in (4096, 32768, 512 * 1024):
                payload = await peers[1].receive()
                if payload != _payload("01" * 32, size):
                    raise ProbeFailure("exchange_failed")
                received.append(size)
                await peers[1].send(payload)
        await asyncio.wait_for(asyncio.gather(sender(), receiver()), timeout=45)
    finally:
        await asyncio.gather(*(peer.close() for peer in peers))
    await asyncio.sleep(0)
    if any(not task.done() for task in asyncio.all_tasks() - baseline):
        raise ProbeFailure("cleanup_failed")
    if not route or route["protocol"] != "udp":
        raise ProbeFailure("not_direct")
    return {"schema": "direct-ice-self-test-v1", "status": "passed",
            "sizes": received, "direct": True, "protocol": "udp",
            "peer_closed": True, "owned_tasks_pending": 0}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--self-test", action="store_true")
    checks.add_argument("--mailbox-self-test", action="store_true",
                        help="Check bounded verified HTTPS to the engineering mailbox API")
    sub = parser.add_subparsers(dest="command")
    keygen = sub.add_parser("keygen")
    keygen.add_argument("--key-dir", type=Path, required=True)
    keygen.add_argument("--public-file", type=Path, required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--context-file", type=Path, required=True)
    prepare.add_argument("--key-dir", type=Path, required=True)
    prepare.add_argument("--ready-file", type=Path, required=True)
    validate = sub.add_parser("validate-artifact")
    validate.add_argument("--kind", choices=("ready", "answer", "report"), required=True)
    validate.add_argument("--context-file", type=Path, required=True)
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument("--output", type=Path, required=True)
    def timeout_value(text):
        value = int(text)
        if not 30 <= value <= 300:
            raise argparse.ArgumentTypeError("must be between 30 and 300 seconds")
        return value

    for name in ("offer", "answer"):
        command = sub.add_parser(name)
        command.add_argument("--context-file", type=Path, required=True)
        command.add_argument("--key-dir", type=Path, required=True)
        command.add_argument("--answer-file", type=Path, required=True)
        command.add_argument("--report-file", type=Path, required=True)
        command.add_argument("--cancel-file", type=Path)
        command.add_argument("--timeout", type=timeout_value, default=300, metavar="30..300")
        command.add_argument("--no-stun", action="store_true", help=argparse.SUPPRESS)
        if name == "offer":
            command.add_argument("--guest-ready-file", type=Path, required=True)
            command.add_argument("--offer-file", type=Path, required=True)
        else:
            command.add_argument("--mailbox-gist-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.self_test or args.mailbox_self_test:
            if args.command:
                raise SetupError("self-test cannot be combined with a subcommand")
            if args.mailbox_self_test:
                if asyncio.run(_read_mailbox("0" * 32)) is not None:
                    raise SetupError("unexpected mailbox self-test contents")
                result = {"schema": "direct-ice-mailbox-check-v1", "status": "passed"}
            else:
                result = asyncio.run(_self_test())
            print(canonical(result).decode("ascii"))
            return 0
        if args.command == "keygen":
            write_json(args.public_file, ProbeKeys.generate(args.key_dir).public())
        elif args.command == "prepare":
            context = validate_context(read_json(args.context_file))
            write_json(args.ready_file, make_ready(context, ProbeKeys.generate(args.key_dir)))
        elif args.command == "validate-artifact":
            context = validate_context(read_json(args.context_file))
            value = read_json(args.input)
            if args.kind == "ready":
                validate_ready(value, context)
            elif args.kind == "answer":
                validate_envelope(value, context, kind="answer")
            else:
                validate_report(value, context)
            write_json(args.output, value)
        elif args.command in ("offer", "answer"):
            if args.command == "answer" and re.fullmatch(r"[0-9a-f]{32}", args.mailbox_gist_id) is None:
                raise SetupError("invalid mailbox identifier")
            return asyncio.run(_run(args))
        else:
            parser.error("a subcommand or --self-test is required")
        return 0
    except (SetupError, OSError, ValueError, RuntimeError, ProbeFailure):
        print("Direct connection proof failed; private setup data withheld.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
