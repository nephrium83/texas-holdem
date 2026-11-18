"""Numerical/reporting contracts, independent of cryptographic timing noise."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "benchmarks" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_p95_uses_nearest_rank_not_interpolation():
    helper = load("benchmark_support")
    assert helper.summary(list(range(1, 21))) == {"p50": 10.5, "p95": 19, "max": 20}
    assert helper.summary([7]) == {"p50": 7, "p95": 7, "max": 7}


def test_parallel_schedule_tracks_causal_messages_and_removed_work():
    critical = load("critical_path")
    # Seat 0 proves for 2 s, then seat 1 verifies for 1 s after one network hop.
    trace = [(0, 2., [0], None, 2., 0.), (1, 1., [], (0, 0), 0., 1.)]
    assert critical.schedule(trace, [0, 1], {0: 0, 1: 1}, .5) == 3.5
    assert critical.schedule(trace, [0, 1], {0: 0, 1: 1}, .5, "prove") == 1.5
    assert critical.schedule(trace, [0, 1], {0: 0, 1: 1}, .5, "verify") == 2.5


def test_peak_rss_counts_resident_native_memory():
    helper = load("benchmark_support")
    before = helper.peak_rss_bytes()
    allocation = bytearray(24 * 1024 * 1024)
    for index in range(0, len(allocation), 4096):
        allocation[index] = 1
    assert helper.peak_rss_bytes() >= before
    assert helper.peak_rss_bytes() >= len(allocation)


def test_signed_sample_cleans_generated_identity_directory_on_failure(monkeypatch):
    session = load("session_hand_start")
    created = []

    def failed_sample(n, table, modules, directory):
        created.append(directory)
        (directory / "generated-identity").write_bytes(b"test fixture")
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(session, "_sample", failed_sample)
    import pytest
    with pytest.raises(RuntimeError, match="fixture failure"):
        session.sample(2, None, None)
    assert created and not created[0].exists()


def test_coordinator_metadata_is_available_as_a_package_import():
    from benchmarks import mental_deal_startup
    report = mental_deal_startup._metadata()
    assert len(report["commit"]) == 40
    assert report["memory_scope"].startswith("OS process-lifetime peak RSS")
