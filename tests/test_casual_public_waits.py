"""Public wait projection identifies missing work without exposing cards."""
import json

import pytest

from crypto_gate import initialize_for_tests, NativeCryptoUnavailable

try:
    initialize_for_tests()
    from holdem.p2p.mental_deal import MentalDeal, Phase
except NativeCryptoUnavailable as exc:
    pytest.skip(str(exc), allow_module_level=True)


def _deal():
    return MentalDeal("wait-test", 1, 0, [0, 1, 2], 0, b"local-secret")


def test_keygen_and_each_shuffle_round_name_only_missing_actor():
    deal = _deal()
    deal._pubkeys = {0: object(), 2: object()}
    assert deal.public_waits() == [dict(phase="keygen", actor=1, detail="")]
    deal.phase = Phase.SHUFFLE
    deal._shuffle_round = 1
    assert deal.public_waits() == [dict(phase="shuffle", actor=1, detail="2")]
    deal._shuffle_round = 2
    assert deal.public_waits() == [dict(phase="shuffle", actor=2, detail="3")]


def test_hole_owner_withheld_shares_are_never_timed_out():
    deal = _deal()
    deal.phase = Phase.DEAL
    deal._hole_pos = {0: [0, 1], 1: [2, 3], 2: [4, 5]}
    deal._shares = {pos: {seat: object() for seat in deal.seats_in if seat != owner}
                    for owner, positions in deal._hole_pos.items() for pos in positions}
    assert deal.public_waits() == []
    # The local game cannot recover seat 1's cards, but it can observe
    # that seat 2 has failed to broadcast its required public contribution.
    del deal._shares[2][2]
    del deal._shares[3][2]
    assert deal.public_waits() == [dict(phase="holes", actor=2, detail="")]


def test_board_waits_only_exist_for_a_requested_street():
    deal = _deal()
    deal.phase = Phase.DEAL
    deal._board_pos = [10, 11, 12, 13, 14]
    assert deal.public_waits() == []
    deal._revealed_streets.add("flop")
    deal._shares = {pos: {0: object(), 2: object()} for pos in [10, 11, 12]}
    assert deal.public_waits() == [dict(phase="board", actor=1, detail="flop")]
    deal._shares[10][1] = object()
    assert deal.public_waits() == [dict(phase="board", actor=1, detail="flop")]
    for pos in [11, 12]:
        deal._shares[pos][1] = object()
    assert deal.public_waits() == []
    deal._revealed_streets.add("turn")
    assert len(deal.public_waits()) == 3
    assert all(wait["detail"] == "turn" for wait in deal.public_waits())


def test_audit_and_terminal_projection_are_public_json_only():
    deal = _deal()
    deal.phase = Phase.AUDIT
    deal._audit_shares = {0: [object()], 1: [object()]}
    waits = deal.public_waits()
    assert waits == [dict(phase="audit", actor=2, detail="")]
    assert set(waits[0]) == {"phase", "actor", "detail"}
    assert "local-secret" not in json.dumps(waits)
    # Mutating the returned public description cannot alter deal state.
    waits[0]["actor"] = 1
    assert deal.public_waits()[0]["actor"] == 2
    for phase in [Phase.DONE, Phase.ABORTED]:
        deal.phase = phase
        assert deal.public_waits() == []


def test_real_deal_observers_identify_a_withheld_public_share():
    """An observer times missing broadcasts without decrypting others' holes."""
    from holdem.p2p.deal_map import hole_positions

    seats = [0, 1, 2]
    deals = {seat: MentalDeal("real-wait", 1, seat, seats, 0,
                             f"secret-{seat}".encode()) for seat in seats}
    pending = [message for deal in deals.values() for message in deal.start()]
    target_positions = set(hole_positions(0, seats)[1])
    held = []
    while pending:
        message = pending.pop(0)
        if (message["type"] == "deal_share" and message["seat_from"] == 2
                and message["position"] in target_positions):
            held.append(message)
            continue
        for deal in deals.values():
            pending.extend(deal.handle(dict(message)))
    assert len(held) == 2
    assert deals[0].hole_complete()  # The observer's own cards are ready.
    assert not deals[1].hole_complete()
    for observer in [0, 1]:
        assert deals[observer].public_waits() == [
            dict(phase="holes", actor=2, detail="")]
    for message in held:
        for deal in deals.values():
            deal.handle(dict(message))
    assert all(deal.public_waits() == [] for deal in deals.values())
    assert all(deal.hole_complete() for deal in deals.values())
