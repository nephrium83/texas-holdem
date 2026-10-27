"""Benchmark reports must not turn partial work into performance evidence."""
import json
import threading
from types import SimpleNamespace

import pytest

from benchmarks import bg_shuffle_performance as proof
from benchmarks import event_loop_latency as latency
from benchmarks import mental_deal_startup as startup
from benchmarks import shuffle_proof_comparison as comparison
from holdem import native_crypto


@pytest.mark.parametrize("count,expected", [(1, 1), (10, 10), (19, 19), (20, 19)])
def test_nearest_rank_p95_for_real_benchmark_sample_counts(count, expected):
    records = [{key: value for key in startup._METRICS}
               for value in range(1, count + 1)]
    assert startup._summary(records)["total_ms"]["p95"] == expected
    assert proof._summary(records)["prove_ms"]["p95"] == expected
    assert latency._stats(list(range(1, count + 1)))["p95"] == expected


def test_coordinator_bytes_describe_messages_and_deliveries(monkeypatch):
    class Deal:
        phase = startup.Phase.DEAL
        abort_reason = None

        def __init__(self, *, seat, **kwargs):
            self.seat = seat

        def start(self):
            return [{"type": "ready"}] if self.seat == 0 else []

        def handle(self, _message):
            return []

        def is_done_with_keygen(self):
            return True

        is_shuffle_complete = is_done_with_keygen
        hole_complete = is_done_with_keygen

    monkeypatch.setattr(startup, "MentalDeal", Deal)
    result = startup._run_hand([0, 1, 2], False, 1)
    assert result["message_bytes"] == 17
    assert result["remote_payload_bytes"] == 34
    assert result["simulated_delivery_bytes"] == 51
    assert "wire_bytes" not in result


def _latency_run(monkeypatch, *, verify=True, execute=True):
    stopped = []
    joined = []
    monkeypatch.setattr(latency, "_statement", lambda: (1, 2, 3, 4, 5))
    monkeypatch.setattr(latency, "bg_commitment_key", lambda: None)
    monkeypatch.setattr(latency.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(latency.T, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(latency.T, "start_host", lambda _port: None)
    monkeypatch.setattr(latency.T, "event_loop", lambda: SimpleNamespace(
        call_soon_threadsafe=lambda callback: callback() if execute else None))
    monkeypatch.setattr(latency.threading, "Thread", lambda **kwargs: SimpleNamespace(
        start=lambda: None, join=lambda **kwargs: joined.append(True),
        is_alive=lambda: False))

    def check(*args):
        if isinstance(verify, Exception):
            raise verify
        return verify

    monkeypatch.setattr(latency.bg_shuffle, "verify", check)
    return stopped, joined


@pytest.mark.parametrize("result", [False, ValueError("malformed statement")])
def test_failed_verification_never_produces_a_timing_report(monkeypatch, result):
    stopped, joined = _latency_run(monkeypatch, verify=result)
    with pytest.raises(RuntimeError, match="workload failed"):
        latency.measure(3)
    assert len(stopped) == 2 and joined == [True]


def test_incomplete_workload_is_a_failure_and_cleans_up(monkeypatch):
    stopped, joined = _latency_run(monkeypatch, execute=False)
    with pytest.raises(TimeoutError, match="did not finish"):
        latency.measure(3, completion_timeout=0.001)
    assert len(stopped) == 2 and joined == [True]


def test_successful_workload_reports_every_verification(monkeypatch):
    stopped, joined = _latency_run(monkeypatch)
    report = latency.measure(3)
    assert report["verify_ms"]["n"] == report["verifications"] == 3
    assert len(stopped) == 2 and joined == [True]


def test_sampler_stops_while_waiting_for_an_unresponsive_loop():
    queued, stop = threading.Event(), threading.Event()
    loop = SimpleNamespace(call_soon_threadsafe=lambda callback: queued.set())
    sampler = threading.Thread(target=latency._sample_latency,
                               args=(loop, stop, []), daemon=True)
    sampler.start()
    try:
        assert queued.wait(1), "sampler never scheduled its callback"
    finally:
        stop.set()
        sampler.join(1)
    assert not sampler.is_alive(), "sampler ignored stop while waiting on the loop"


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_workload_is_rejected_before_starting_transport(monkeypatch, value):
    monkeypatch.setattr(latency, "_statement", lambda: pytest.fail("work started"))
    with pytest.raises(ValueError, match="positive integer"):
        latency.measure(value)


def test_standalone_projection_counts_shuffler_verification(monkeypatch, capsys):
    monkeypatch.setattr(native_crypto, "initialize", lambda: None)
    monkeypatch.setattr(proof, "_sample", lambda *args: {
        "prove_ms": 10, "verify_ms": 3, "proof_bytes": 100})
    monkeypatch.setattr(proof, "_commit", lambda: "verified-source")
    monkeypatch.setattr("sys.argv", ["benchmark", "--samples", "1"])
    proof.main()
    report = json.loads(capsys.readouterr().out)
    assert report["seat_chain_estimates"]["2"]["serial_prove_plus_all_verifiers_ms"] == 32
    assert report["seat_chain_estimates"]["9"]["serial_prove_plus_all_verifiers_ms"] == 333
    assert report["metadata"]["commit"] == "verified-source"


def test_projections_do_not_claim_validation_from_historical_data(monkeypatch, capsys):
    monkeypatch.setattr(native_crypto, "initialize", lambda: None)
    monkeypatch.setattr(comparison.R, "libsodium_version", lambda: "test")
    monkeypatch.setattr(comparison, "_time_bayer_groth", lambda *args: {
        "prove_ms": 10, "verify_ms": 3})
    monkeypatch.setattr(comparison, "_time_cut_and_choose", lambda *args: {
        "prove_ms": 20, "verify_ms": 6, "proof_bytes": 100})
    monkeypatch.setattr(comparison, "_commit", lambda: "verified-source")
    monkeypatch.setattr("sys.argv", ["benchmark", "--samples", "1"])
    comparison.main()
    report = json.loads(capsys.readouterr().out)
    assert report["model_validation"]["status"] == "not_validated"
    assert report["model_validation"]["comparison_to_historical_bg"]
    assert report["metadata"]["commit"] == "verified-source"


@pytest.mark.parametrize("arguments", [
    ["--samples", "0"], ["--samples", "-1"],
    ["--seats", "1"], ["--seats", "10"],
])
def test_invalid_projection_configuration_is_rejected_before_work(monkeypatch, arguments):
    monkeypatch.setattr(native_crypto, "initialize", lambda: None)
    monkeypatch.setattr(comparison, "_time_bayer_groth", lambda *args: pytest.fail("work started"))
    monkeypatch.setattr("sys.argv", ["benchmark", *arguments])
    with pytest.raises(SystemExit, match="positive samples"):
        comparison.main()
