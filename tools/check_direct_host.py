"""Bounded direct-host diagnostic; real router changes require an explicit flag.

The normal desktop host/controller and authenticated TCP listener are used.
No invite is printed or written; isolated identity/config files stay private.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
_CLEANUP_WAIT_SECONDS = 2.0
_LOOKUP_WAIT_SECONDS = 3.0
_INVITE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z2-7]{4}(?:[- \t][A-Z2-7]{4}){27}|[A-Z2-7]{112})(?![A-Za-z0-9])",
    re.IGNORECASE | re.ASCII,
)
_SECRET_FIELD = re.compile(
    r"([\"'](?:admission_secret|device_secret|private_key|secret_key)[\"']\s*[:=]\s*)"
    r"([\"'])(.*?)(\2)", re.DOTALL,
)
_PRIVATE_PEM = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?"
    r"(?:-----END (?:[A-Z ]+ )?PRIVATE KEY-----|$)", re.DOTALL,
)


def _safe_text(value) -> str:
    """No traceback locals, invites, explicit secret fields or private PEMs."""
    def invite(match):
        try:
            raw = base64.b32decode(re.sub(r"[- \t]", "", match.group()).upper())
        except ValueError:
            return match.group()
        return "[REDACTED_INVITE]" if len(raw) == 70 and raw[0] == 2 else match.group()

    text = _INVITE.sub(invite, str(value))
    text = _SECRET_FIELD.sub(lambda match: match[1] + match[2] + "[REDACTED_SECRET]" + match[2], text)
    return _PRIVATE_PEM.sub("[REDACTED_PRIVATE_KEY]", text)


def _exception_chain(exc: BaseException) -> list[dict]:
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        chain.append({"type": type(exc).__name__, "message": _safe_text(exc)})
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    return chain


class _MappingDiagnostics:
    """Observe the existing mapper; never originate an Add or Delete call.

    The only additional router operation is a bounded, read-only exact-port
    lookup after the mapper's worker has actually exited. Details stay in the
    local report rather than stdout. Imports remain lazy for offline mode.
    """

    def __init__(self, *, client_factory=None, open_mapping=None):
        self._client_factory = client_factory
        self._open_mapping = open_mapping
        self._lock = threading.RLock()
        self._client = None
        self._worker = None
        self._state = dict(local_address=None, external_port=None,
                           ownership_marker=None, control_url=None,
                           operations=[], add_attempts=0, add_successes=0,
                           renewal_attempts=0, renewal_successes=0,
                           delete_attempts=0, delete_successes=0,
                           cleanup_worker="unknown", post_close_lookup={"status": "not_checked"})

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def _record(self, operation, invoke, *, result_view=lambda result: result):
        with self._lock:
            event = {"stage": operation, "ok": None}
            self._state["operations"].append(event)
        try:
            result = invoke()
        except BaseException as exc:
            with self._lock:
                event.update(ok=False, error_chain=_exception_chain(exc))
            raise
        with self._lock:
            event.update(ok=True, result=result_view(result))
        return result

    @staticmethod
    def _entry_view(result):
        if result is None:
            return {"status": "absent"}
        if isinstance(result, tuple) and len(result) == 5:
            return dict(status="present", internal_client=_safe_text(result[0]),
                        internal_port=result[1], description=_safe_text(result[2]),
                        enabled=result[3], lease_seconds=result[4])
        return {"status": "invalid", "type": type(result).__name__}

    def _wrap_client(self, local_ip):
        with self._lock:
            self._worker = threading.current_thread()
        if self._client_factory is None:
            from holdem.port_mapping import _make_client
            create = _make_client
        else:
            create = self._client_factory
        client = self._record("create_client", lambda: create(local_ip),
                              result_view=lambda _: "created")
        with self._lock:
            self._client = client
        owner = self
        raw = getattr(client, "_raw", None)
        if raw is not None:
            # Capture the native-selected URL even if the strict backend
            # rejects it before it can construct its lookup object.
            class ObservedSelection:
                def __getattr__(self, name):
                    return getattr(raw, name)

                def selectigd(self):
                    result = owner._record("select_control_url", raw.selectigd,
                                           result_view=_safe_text)
                    with owner._lock:
                        owner._state["control_url"] = _safe_text(result)
                    return result

            client._raw = ObservedSelection()

        class ObservedClient:
            @property
            def lanaddr(self):
                return owner._record("lan_address", lambda: client.lanaddr,
                                     result_view=_safe_text)

            def discover(self):
                return owner._record("discover", client.discover)

            def selectigd(self):
                result = owner._record("select_gateway", client.selectigd,
                                       result_view=lambda _: "selected")
                lookup = getattr(client, "_lookup", None)
                url = getattr(lookup, "_control_url", None)
                if url is None and isinstance(result, str):
                    url = result
                with owner._lock:
                    owner._state["control_url"] = _safe_text(url) if url is not None else None
                return result

            def externalipaddress(self):
                return owner._record("external_address", client.externalipaddress,
                                     result_view=_safe_text)

            def getspecificportmapping(self, port, protocol, remote_host):
                with owner._lock:
                    owner._state["external_port"] = port
                return owner._record("lookup", lambda: client.getspecificportmapping(
                    port, protocol, remote_host), result_view=owner._entry_view)

            def addportmapping(self, *args):
                with owner._lock:
                    renewal = owner._state["add_attempts"] > 0
                    owner._state["add_attempts"] += 1
                    owner._state["renewal_attempts"] += int(renewal)
                    owner._state["external_port"] = args[0]
                    owner._state["ownership_marker"] = _safe_text(args[4])
                result = owner._record("renew" if renewal else "add",
                                       lambda: client.addportmapping(*args))
                with owner._lock:
                    owner._state["add_successes"] += int(result is True)
                    owner._state["renewal_successes"] += int(renewal and result is True)
                return result

            def deleteportmapping(self, *args):
                with owner._lock:
                    owner._state["delete_attempts"] += 1
                result = owner._record("delete", lambda: client.deleteportmapping(*args))
                with owner._lock:
                    owner._state["delete_successes"] += int(result is True)
                return result

        return ObservedClient()

    def mapping_factory(self, local_ip, local_port, *, on_lost):
        with self._lock:
            self._state["local_address"] = [local_ip, local_port]
        if self._open_mapping is None:
            from holdem.port_mapping import _open_tcp_mapping
            open_mapping = _open_tcp_mapping
        else:
            open_mapping = self._open_mapping
        def lost(reason):
            with self._lock:
                self._state["operations"].append(
                    {"stage": "lease_lost", "reason": _safe_text(reason)})
            return on_lost(reason)

        return open_mapping(local_ip, local_port, on_lost=lost,
                            client_factory=self._wrap_client)

    def after_close(self, *, cleanup_wait=_CLEANUP_WAIT_SECONDS,
                    lookup_wait=_LOOKUP_WAIT_SECONDS):
        with self._lock:
            worker, client, port = self._worker, self._client, self._state["external_port"]
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=cleanup_wait)
        pending = worker is not None and worker.is_alive()
        with self._lock:
            self._state["cleanup_worker"] = "pending" if pending else "finished" if worker else "not_observed"
            self._state["post_close_lookup"] = {"status": "unknown"}
        if pending or worker is None or client is None or port is None:
            return
        done = threading.Event()

        def lookup():
            try:
                result = self._entry_view(client.getspecificportmapping(port, "TCP", ""))
                if result["status"] == "present":
                    with self._lock:
                        address = self._state["local_address"]
                        owned = (result["internal_client"] == address[0]
                                 and result["internal_port"] == address[1]
                                 and result["description"] == self._state["ownership_marker"])
                    result["status"] = "owned_entry_present" if owned else "different_entry_present"
            except BaseException as exc:
                result = {"status": "lookup_failed", "error_chain": _exception_chain(exc)}
            with self._lock:
                self._state["post_close_lookup"] = result
            done.set()

        with self._lock:
            self._state["post_close_lookup"] = {"status": "pending"}
        threading.Thread(target=lookup, name="poker-diagnostic-final-lookup", daemon=True).start()
        done.wait(lookup_wait)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--simulate-router", action="store_true")
    mode.add_argument("--allow-router-mapping", action="store_true",
                      help="Create/renew/remove this game's temporary TCP router mapping")
    parser.add_argument("--mapping-details", action="store_true",
                        help="Save local mapping stages and verify exact-port cleanup after closing")
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 300:
        parser.error("--seconds must be between 1 and 300")
    if args.mapping_details and args.simulate_router:
        parser.error("--mapping-details requires --allow-router-mapping")
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    # Set before importing host services; the test never uses a player's identity.
    os.environ["HOLDEM_CONFIG_DIR"] = str(run_dir / "private-config")
    sys.path.insert(0, str(ROOT))
    from holdem.lan_session import LanTable
    from holdem.p2p import invite, transport

    class SimulatedLease:
        def __init__(self, _ip, port, **_kwargs):
            self.external_address = ("127.0.0.1", port)

        def close(self):
            pass

    if args.simulate_router:
        async def no_stun():
            return None
        transport._resolve_stun = no_stun
    diagnostics = _MappingDiagnostics() if args.mapping_details else None
    table = LanTable("Connection check", {}, mapping_factory=(
        SimulatedLease if args.simulate_router else
        diagnostics.mapping_factory if diagnostics is not None else None))
    report = dict(ok=False, simulated=args.simulate_router,
                  requested_seconds=args.seconds, error="", error_chain=[])
    started = time.monotonic()
    try:
        table.host(internet=True)
        state = table.lobby_state()
        parsed = invite.parse_room_code(state["invite"])
        assert state["network"] == "internet" and state["state"] == "lobby"
        assert parsed["flags"] & invite.FLAG_DIRECT_INTERNET
        report["advertised_address"] = [parsed["public_ip"], parsed["public_port"]]
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            if table.lobby_state()["state"] == "closed":
                raise RuntimeError("The direct connection closed during its lease check")
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        report["ok"] = True
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = _safe_text(exc) or type(exc).__name__
        report["error_chain"] = _exception_chain(exc)
    finally:
        try:
            table.close()
        except (Exception, KeyboardInterrupt) as exc:
            report["ok"] = False
            report["cleanup_error_chain"] = _exception_chain(exc)
            if not report["error"]:
                report["error"] = _safe_text(exc) or type(exc).__name__
        if diagnostics is not None:
            diagnostics.after_close()
            report["mapping_diagnostics"] = diagnostics.snapshot()
            report["mapping_cleanup_verified"] = (
                report["mapping_diagnostics"]["post_close_lookup"]["status"] == "absent")
            if not report["mapping_cleanup_verified"]:
                report["ok"] = False
                if not report["error"]:
                    report["error"] = "Router mapping cleanup is pending or could not be verified; see the local report"
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["transport_stopped"] = transport.event_loop() is None
        report["peer_sockets_closed"] = not transport.peer_ids()
        report["ok"] = (report["ok"] and report["transport_stopped"]
                        and report["peer_sockets_closed"])
        (run_dir / "report.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8")
    # Exact port, ownership marker and control URL are local troubleshooting
    # data. Print only the non-detailed summary; never print the invite.
    console = {key: value for key, value in report.items()
               if key not in ("mapping_diagnostics", "error_chain", "cleanup_error_chain")}
    print(json.dumps(console, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
