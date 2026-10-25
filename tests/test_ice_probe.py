"""Real local host/guest proof in independent loops without a public service."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from holdem import ice_probe
from holdem.ice_signaling import ProbeKeys, make_ready, read_json, write_json


def test_real_probe_exchange_and_public_artifact_validation(tmp_path, monkeypatch):
    pytest.importorskip("aiortc")
    host = ProbeKeys.generate(tmp_path / "host-keys")
    context = {"repository": "nephrium83/texas-holdem", "run_id": "123456",
               "run_attempt": "1", "nonce": "01" * 32, "source_commit": "02" * 20,
               "package_sha256": "03" * 32, "host_signing_public": host.signing_public,
               "host_encryption_public": host.encryption_public}
    context_path = tmp_path / "context.json"
    ready_path = tmp_path / "ready.json"
    write_json(context_path, context)
    assert ice_probe.main(["prepare", "--context-file", str(context_path), "--key-dir",
                           str(tmp_path / "guest-keys"), "--ready-file", str(ready_path)]) == 0
    offer_path = tmp_path / "offer.json"
    answer_path = tmp_path / "answer.json"
    common = dict(context_file=context_path, answer_file=answer_path, cancel_file=None,
                  timeout=45, no_stun=True, game_hands=0)
    host_args = argparse.Namespace(**common, command="offer", key_dir=tmp_path / "host-keys",
                                  guest_ready_file=ready_path, offer_file=offer_path,
                                  report_file=tmp_path / "host-report.json")
    guest_args = argparse.Namespace(**common, command="answer", key_dir=tmp_path / "guest-keys",
                                   mailbox_gist_id="ab" * 16, report_file=tmp_path / "guest-report.json")

    async def local_mailbox(_):
        return await ice_probe._wait_file(offer_path)

    monkeypatch.setattr(ice_probe, "_wait_offer", local_mailbox)
    with ThreadPoolExecutor(max_workers=2) as pool:
        host_result = pool.submit(asyncio.run, ice_probe._run(host_args))
        guest_result = pool.submit(asyncio.run, ice_probe._run(guest_args))
        assert host_result.result(timeout=60) == 0
        assert guest_result.result(timeout=60) == 0
    for role in ("host", "guest"):
        report = read_json(tmp_path / (role + "-report.json"))
        assert report["status"] == "connected"
        assert report["cleanup"] == {"peer_closed": True, "owned_tasks_pending": 0}
        assert "192." not in str(report)
        assert ice_probe.main(["validate-artifact", "--kind", "report", "--context-file", str(context_path),
                               "--input", str(tmp_path / (role + "-report.json")),
                               "--output", str(tmp_path / (role + "-public.json"))]) == 0
    for kind in ("ready", "answer"):
        assert ice_probe.main(["validate-artifact", "--kind", kind, "--context-file", str(context_path),
                               "--input", str(tmp_path / (kind + ".json")),
                               "--output", str(tmp_path / (kind + "-public.json"))]) == 0


def test_cancellation_writes_safe_report_and_closes_peer(tmp_path):
    pytest.importorskip("aiortc")
    host = ProbeKeys.generate(tmp_path / "host")
    guest = ProbeKeys.generate(tmp_path / "guest")
    context = {"repository": "owner/repo", "run_id": "1", "run_attempt": "1", "nonce": "01" * 32,
               "source_commit": "02" * 20, "package_sha256": "03" * 32,
               "host_signing_public": host.signing_public,
               "host_encryption_public": host.encryption_public}
    write_json(tmp_path / "context.json", context)
    write_json(tmp_path / "ready.json", make_ready(context, guest))
    (tmp_path / "cancel").write_text("cancel")
    code = ice_probe.main(["offer", "--context-file", str(tmp_path / "context.json"),
                           "--key-dir", str(tmp_path / "host"), "--guest-ready-file", str(tmp_path / "ready.json"),
                           "--offer-file", str(tmp_path / "offer.json"), "--answer-file", str(tmp_path / "answer.json"),
                           "--report-file", str(tmp_path / "report.json"), "--cancel-file", str(tmp_path / "cancel"),
                           "--timeout", "30", "--no-stun"])
    assert code == 1
    report = read_json(tmp_path / "report.json")
    assert report["status"] == "cancelled"
    assert report["cleanup"] == {"peer_closed": True, "owned_tasks_pending": 0}


def test_invalid_mailbox_and_context_fail_without_private_output(tmp_path, capsys):
    assert ice_probe.main(["answer", "--context-file", str(tmp_path / "secret-context.json"),
                           "--key-dir", str(tmp_path), "--mailbox-gist-id", "not-a-gist",
                           "--answer-file", str(tmp_path / "answer.json"),
                           "--report-file", str(tmp_path / "report.json")]) == 1
    output = capsys.readouterr().out
    assert "secret-context" not in output
    assert not (tmp_path / "report.json").exists()


def test_stalled_setup_reserves_cleanup_inside_process_deadline(tmp_path, monkeypatch):
    import time
    from holdem import ice_peer

    pytest.importorskip("cryptography")
    closed = []

    class StalledPeer:
        def __init__(self, **_):
            pass

        async def create_offer(self):
            await asyncio.Event().wait()

        async def close(self):
            await asyncio.sleep(0.05)
            closed.append(True)

    monkeypatch.setattr(ice_peer, "DirectPeer", StalledPeer)
    host = ProbeKeys.generate(tmp_path / "host")
    guest = ProbeKeys.generate(tmp_path / "guest")
    context = {"repository": "owner/repo", "run_id": "1", "run_attempt": "1", "nonce": "01" * 32,
               "source_commit": "02" * 20, "package_sha256": "03" * 32,
               "host_signing_public": host.signing_public,
               "host_encryption_public": host.encryption_public}
    write_json(tmp_path / "context.json", context)
    write_json(tmp_path / "ready.json", make_ready(context, guest))
    args = argparse.Namespace(command="offer", context_file=tmp_path / "context.json",
                              key_dir=tmp_path / "host", guest_ready_file=tmp_path / "ready.json",
                              offer_file=tmp_path / "offer.json", answer_file=tmp_path / "answer.json",
                              report_file=tmp_path / "report.json", cancel_file=None,
                              timeout=30, no_stun=True, game_hands=0)
    started = time.monotonic()
    assert asyncio.run(ice_probe._run(args)) == 1
    elapsed = time.monotonic() - started
    assert 5 <= elapsed < 10, elapsed
    assert closed == [True]
    report = read_json(tmp_path / "report.json")
    assert report["failure_code"] == "setup_timeout"
    assert report["cleanup"] == {"peer_closed": True, "owned_tasks_pending": 0}
