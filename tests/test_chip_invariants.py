"""Property tests: no sequence of legal actions may create or destroy chips.

The betting tests elsewhere drive scripted lines -- everyone calls,
everyone folds, everyone jams. Those exercise the paths someone thought to
write down. Chip conservation is a property that must hold over EVERY legal
line, so this drives randomly chosen legal actions over many seeds, seat
counts, and betting structures, and checks the invariant after every single
action rather than only at settlement.

Each case is seeded and the seed is reported on failure, so any counter-
example reproduces exactly.
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.engine import Card

try:
    from holdem.p2p.replica_table import (
        PHASE_BETTING, PHASE_HAND_OVER, PHASE_SETTLED, PHASE_SHOWDOWN,
        PHASE_STREET_OVER, ReplicaTable)
except RuntimeError as exc:
    pytest.skip(f"libsodium unavailable: {exc}", allow_module_level=True)


STRUCTURES = ["No-Limit", "Pot-Limit", "Fixed-Limit"]


def _table(n, stacks, structure, hand_no=1):
    return ReplicaTable(
        session_id="chips", hand_no=hand_no,
        names=[f"P{i}" for i in range(n)], stacks=list(stacks),
        sb=5, bb=10, structure=structure)


def _total(table) -> int:
    """Every chip in the system.

    engine.pot is a RUNNING total that already includes the current
    street's live bets -- after posting blinds at a 3x500 table it reads
    15 while the players' bets read 10/0/5. Adding both double-counts, so
    the invariant is stacks + pot and nothing else.
    """
    e = table.engine
    return sum(p.stack for p in e.players) + e.pot


def _fresh_board(rng, used):
    """Board cards that do not collide with anything already dealt."""
    out = []
    while len(out) < 5:
        card = Card(rng.randrange(2, 15), rng.randrange(4))
        key = (card.v, card.s)
        if key not in used:
            used.add(key)
            out.append(card)
    return out


def _play(rng, table, bankroll, structure):
    """Drive random legal actions to settlement, checking chips throughout."""
    used = set()
    board = _fresh_board(rng, used)
    holes = {}
    for seat in table.seats_dealt:
        pair = []
        while len(pair) < 2:
            card = Card(rng.randrange(2, 15), rng.randrange(4))
            if (card.v, card.s) not in used:
                used.add((card.v, card.s))
                pair.append(card)
        holes[seat] = pair

    seq = 0
    street = 0
    for _ in range(400):                     # generous bound; must terminate
        assert _total(table) == bankroll, (
            f"chips changed mid-hand: {_total(table)} != {bankroll}")

        if table.phase == PHASE_BETTING:
            seat = table.actor
            if seat is None:
                break
            legal = table.engine.legal(seat)
            choices = ["fold", "call"]
            if legal.get("can_raise"):
                choices.append("raise")
            action = rng.choice(choices)
            amount = 0
            if action == "raise":
                lo, hi = legal["min_to"], legal["max_to"]
                amount = rng.randint(lo, hi) if hi > lo else lo
            verdict = table.apply_action(seq, seat, action, amount)
            assert verdict == "applied", (
                f"legal action refused: seat {seat} {action} {amount} "
                f"-> {verdict} (legal={legal})")
            seq += 1
        elif table.phase == PHASE_STREET_OVER:
            # Reveal the next street: flop three, then one at a time.
            table.advance_street(board[:3] if street == 0
                                 else [board[2 + street]])
            street += 1
        elif table.phase in (PHASE_SHOWDOWN, PHASE_HAND_OVER):
            table.set_all_holes(holes)
            table.finish(force_tabled=True)
            break
        else:
            break

    assert table.phase == PHASE_SETTLED, (
        f"hand did not terminate (phase={table.phase})")
    return table


# ------------------------------------------------------ conservation

@pytest.mark.parametrize("seed", range(40))
def test_chips_are_conserved_over_random_legal_lines(seed):
    rng = random.Random(seed)
    n = rng.randint(2, 9)
    stacks = [rng.randrange(20, 2000) for _ in range(n)]
    structure = rng.choice(STRUCTURES)
    bankroll = sum(stacks)
    table = _table(n, stacks, structure)
    table.start_hand(button=rng.randrange(n))
    _play(rng, table, bankroll, structure)
    assert sum(table.stacks) == bankroll, (
        f"seed={seed} n={n} structure={structure}: "
        f"{sum(table.stacks)} != {bankroll}")


@pytest.mark.parametrize("structure", STRUCTURES)
def test_conservation_holds_per_structure(structure):
    for seed in range(12):
        rng = random.Random(seed * 31 + len(structure))
        n = rng.randint(2, 6)
        stacks = [rng.randrange(50, 500) for _ in range(n)]
        bankroll = sum(stacks)
        table = _table(n, stacks, structure)
        table.start_hand(button=rng.randrange(n))
        _play(rng, table, bankroll, structure)
        assert sum(table.stacks) == bankroll, \
            f"{structure} seed={seed}: {sum(table.stacks)} != {bankroll}"


@pytest.mark.parametrize("seed", range(15))
def test_very_short_stacks_conserve_chips(seed):
    """Short stacks force all-ins and side pots, where odd chips and
    uncalled-bet refunds are easiest to get wrong."""
    rng = random.Random(1000 + seed)
    n = rng.randint(3, 7)
    stacks = [rng.choice([7, 11, 13, 20, 45, 300]) for _ in range(n)]
    bankroll = sum(stacks)
    table = _table(n, stacks, "No-Limit")
    table.start_hand(button=rng.randrange(n))
    _play(rng, table, bankroll, "No-Limit")
    assert sum(table.stacks) == bankroll, \
        f"seed={seed} stacks={stacks}: {sum(table.stacks)} != {bankroll}"


# -------------------------------------------------------- invariants

@pytest.mark.parametrize("seed", range(25))
def test_no_stack_goes_negative(seed):
    rng = random.Random(5000 + seed)
    n = rng.randint(2, 9)
    stacks = [rng.randrange(15, 800) for _ in range(n)]
    table = _table(n, stacks, rng.choice(STRUCTURES))
    table.start_hand(button=rng.randrange(n))
    _play(rng, table, sum(stacks), "any")
    assert all(s >= 0 for s in table.stacks), \
        f"seed={seed}: negative stack in {table.stacks}"


@pytest.mark.parametrize("seed", range(25))
def test_illegal_actions_are_refused_not_applied(seed):
    """A raise outside [min_to, max_to] must be rejected without moving
    chips. Hostile peers send exactly this."""
    rng = random.Random(9000 + seed)
    n = rng.randint(2, 6)
    stacks = [rng.randrange(50, 600) for _ in range(n)]
    table = _table(n, stacks, "No-Limit")
    table.start_hand(button=rng.randrange(n))

    seat = table.actor
    legal = table.engine.legal(seat)
    before = _total(table)
    for bad in (legal["max_to"] + 1, legal["max_to"] + 10 ** 9, -1,
                legal["min_to"] - 1 if legal["min_to"] > 0 else -5):
        table.apply_action(0, seat, "raise", bad)
        assert _total(table) == before, \
            f"seed={seed}: illegal raise {bad} moved chips"
    assert table.phase == PHASE_BETTING


@pytest.mark.parametrize("seed", range(20))
def test_action_out_of_turn_is_refused(seed):
    rng = random.Random(7000 + seed)
    n = rng.randint(3, 8)
    stacks = [500] * n
    table = _table(n, stacks, "No-Limit")
    table.start_hand(button=rng.randrange(n))
    actor = table.actor
    before = _total(table)
    for seat in range(n):
        if seat == actor:
            continue
        verdict = table.apply_action(0, seat, "call", 0)
        assert verdict != "applied", \
            f"seed={seed}: seat {seat} acted out of turn (actor={actor})"
    assert _total(table) == before
