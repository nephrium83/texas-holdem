"""Host-integration checks, deliberately separate from poker rule tests."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import random

import pytest

from holdem.embedding import API_VERSION, PlayerSeat


_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "rpg_tavern.py"
_SPEC = importlib.util.spec_from_file_location("rpg_tavern_example", _EXAMPLE)
assert _SPEC is not None and _SPEC.loader is not None
tavern_example = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tavern_example)
FakeWallet = tavern_example.FakeWallet
Tavern = tavern_example.Tavern


def _result(table_id="visit-1", final=None):
    return {"api_version": API_VERSION, "table_id": table_id,
            "result_id": f"{table_id}/close",
            "initial_chips": {"alice": 200, "bob": 200},
            "final_chips": {"alice": 250, "bob": 150} if final is None else final}


def test_buy_ins_and_payouts_are_atomic_and_retryable():
    wallet = FakeWallet({"alice": 1000, "bob": 1000})
    assert wallet.reserve("visit-1", {"alice": 200, "bob": 200})
    assert not wallet.reserve("visit-1", {"alice": 200, "bob": 200})
    assert wallet.balances() == {"alice": 800, "bob": 800}
    assert wallet.reserved_total() == 400
    for invalid in ({"alice": 100, "missing": 100},
                    {"alice": 100, "bob": 5000}):
        before = wallet.balances()
        with pytest.raises(ValueError):
            wallet.reserve("bad-visit", invalid)
        assert wallet.balances() == before
        assert wallet.reserved_total() == 400
    with pytest.raises(ValueError, match="different reservation"):
        wallet.reserve("visit-1", {"alice": 100, "bob": 200})
    result = _result()
    assert wallet.settle(result)
    assert wallet.balances() == {"alice": 1050, "bob": 950}
    assert wallet.reserved_total() == 0
    assert not wallet.settle(deepcopy(result))
    result["final_chips"]["alice"] = 300
    result["final_chips"]["bob"] = 100
    with pytest.raises(ValueError, match="different close result"):
        wallet.settle(result)
    assert wallet.balances() == {"alice": 1050, "bob": 950}
    with pytest.raises(ValueError, match="cannot be reused"):
        wallet.reserve("visit-1", {"alice": 200, "bob": 200})


@pytest.mark.parametrize("change", [
    {"table_id": "another-visit"},
    {"result_id": "another-visit/close"},
    {"api_version": True},
    {"initial_chips": {"alice": 199, "bob": 201}},
    {"final_chips": {"alice": 250, "stranger": 150}},
    {"final_chips": {"alice": 251, "bob": 150}},
    {"final_chips": {"alice": -1, "bob": 401}},
    {"final_chips": {"alice": True, "bob": 399}},
])
def test_misrouted_or_malformed_close_result_never_credits(change):
    wallet = FakeWallet({"alice": 1000, "bob": 1000})
    wallet.reserve("visit-1", {"alice": 200, "bob": 200})
    result = _result()
    result.update(change)
    with pytest.raises(ValueError):
        wallet.settle(result)
    assert wallet.balances() == {"alice": 800, "bob": 800}
    assert wallet.reserved_total() == 400
    assert wallet.settle(_result())  # Rejection did not poison the retry.


def test_host_can_retry_leave_but_must_finish_live_hand():
    wallet = FakeWallet({"alice": 1000, "bob": 1000})
    tavern = Tavern(wallet)
    seats = (PlayerSeat("alice", "Alice", 200), PlayerSeat("bob", "Bob", 200))
    first = tavern.sit(seats, rng=random.Random(7))
    tavern.play(first)
    before = wallet.balances()
    with pytest.raises(ValueError):
        tavern.leave(first)
    assert wallet.balances() == before
    tavern_example.play_to_showdown(tavern, first, ["alice", "bob"])
    result = tavern.leave(first)
    paid = wallet.balances()
    result["final_chips"]["alice"] = -1  # The caller owns the returned object.
    assert tavern.leave(first)["final_chips"]["alice"] >= 0
    assert wallet.balances() == paid
    second = tavern.sit(seats, rng=random.Random(7))
    assert second != first
    assert tavern.phase(second) == "lobby"
    assert tavern.view(second, "alice")["you"].get("hole", []) == []
    tavern.leave(second)
    assert wallet.balances() == paid


def test_close_receipt_survives_a_failed_wallet_attempt(monkeypatch):
    wallet = FakeWallet({"alice": 1000, "bob": 1000})
    tavern = Tavern(wallet)
    table_id = tavern.sit((PlayerSeat("alice", "Alice", 200),
                           PlayerSeat("bob", "Bob", 200)))
    settle = wallet.settle

    def temporarily_unavailable(result):
        raise RuntimeError("wallet unavailable before committing")

    monkeypatch.setattr(wallet, "settle", temporarily_unavailable)
    with pytest.raises(RuntimeError, match="wallet unavailable"):
        tavern.leave(table_id)
    assert tavern.phase(table_id) == "closed"
    assert wallet.balances() == {"alice": 800, "bob": 800}
    assert wallet.reserved_total() == 400
    monkeypatch.setattr(wallet, "settle", settle)
    receipt = tavern.leave(table_id)
    assert receipt["result_id"] == f"{table_id}/close"
    assert wallet.balances() == {"alice": 1000, "bob": 1000}
    assert wallet.reserved_total() == 0
    assert tavern.leave(table_id) == receipt
    assert wallet.balances() == {"alice": 1000, "bob": 1000}


def test_two_simultaneous_tables_privacy_and_funds_demo():
    report = tavern_example.run_demo()
    assert report["tables_completed"] == 2
    assert report["fresh_visit"]
    assert report["private_views_checked"]
    assert report["duplicate_payout_prevented"]
    assert report["actions"] > 0
    assert report["total_funds"] == 8000
