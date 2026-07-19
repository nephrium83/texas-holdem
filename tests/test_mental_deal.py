"""Pins holdem/p2p/mental_deal.py -- Phase A (distributed key ceremony).

Includes the in-process broadcast simulation harness that later phases
reuse. Key properties: every honest seat computes the SAME joint key;
key shares are deterministic (survive a simulated restart); a bad PoP or
rogue-key seat aborts the ceremony with attribution.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
    from holdem.p2p import elgamal as eg
    from holdem.p2p import keygen_pop
    from holdem.p2p.mental_deal import MentalDeal, Phase, derive_share
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


# ------------------------------------------------------ simulation harness

def run_broadcast(seats, master_secrets, session="s", hand=1, button=0):
    """Drive a full peer-symmetric exchange to a fixed point.

    Creates one MentalDeal per seat, calls start() on each, then delivers
    every emitted message to every seat (including the emitter) until no
    new messages are produced. Returns the list of MentalDeal instances.

    Messages may be mutated per-seat by a test (to simulate a cheater)
    before delivery via the ``tamper`` hook.
    """
    deals = {
        s: MentalDeal(session_id=session, hand_no=hand, seat=s,
                      seats_in=list(seats), button=button,
                      master_secret=master_secrets[s])
        for s in seats
    }
    # queue of (msg) to broadcast to everyone
    queue = []
    for s in seats:
        queue.extend(deals[s].start())

    while queue:
        msg = queue.pop(0)
        for s in seats:
            out = deals[s].handle(dict(msg))     # copy: seats can't share refs
            queue.extend(out)
    return deals


def _secrets(seats):
    return {s: f"master-secret-of-seat-{s}".encode() for s in seats}


# ------------------------------------------------------ deterministic shares

def test_share_is_deterministic():
    ms = b"the-master-secret"
    a = derive_share(ms, "sess", 7, 3)
    b = derive_share(ms, "sess", 7, 3)
    assert bytes(a) == bytes(b)


def test_share_varies_by_context():
    ms = b"m"
    base = derive_share(ms, "s", 1, 0)
    assert bytes(base) != bytes(derive_share(ms, "s", 1, 1))    # seat
    assert bytes(base) != bytes(derive_share(ms, "s", 2, 0))    # hand
    assert bytes(base) != bytes(derive_share(ms, "t", 1, 0))    # session
    assert bytes(base) != bytes(derive_share(b"n", "s", 1, 0))  # master


# ------------------------------------------------------ happy path

@pytest.mark.parametrize("n", [2, 3, 6, 9])
def test_ceremony_completes_all_seats_agree(n):
    seats = list(range(n))
    deals = run_broadcast(seats, _secrets(seats))
    # every seat finished keygen and moved to the shuffle phase
    for s in seats:
        assert deals[s].is_done_with_keygen()
        assert deals[s].phase == Phase.SHUFFLE
        assert deals[s].abort_reason is None
    # and they all computed the identical joint key
    pks = {bytes(deals[s].joint_pk) for s in seats}
    assert len(pks) == 1


def test_joint_key_matches_sum_of_shares():
    seats = [0, 1, 2, 3]
    ms = _secrets(seats)
    deals = run_broadcast(seats, ms)
    # recompute PK independently from the deterministic shares
    Xs = [R.mul_base(derive_share(ms[s], "s", 1, s)) for s in seats]
    expected = eg.joint_public_key(Xs)
    assert bytes(deals[0].joint_pk) == bytes(expected)


def test_restart_regenerates_same_share():
    """A crashed seat that reconstructs its MentalDeal derives the same
    share and produces an identical key_announce -- crash survival."""
    seats = [0, 1, 2]
    ms = _secrets(seats)
    d1 = MentalDeal("s", 1, 1, list(seats), 0, ms[1])
    first = d1.start()
    # simulate crash + reopen: brand-new instance, same inputs
    d2 = MentalDeal("s", 1, 1, list(seats), 0, ms[1])
    second = d2.start()
    assert first[0]["X_hex"] == second[0]["X_hex"]     # same public share


# ------------------------------------------------------ soundness / attribution

def test_bad_pop_aborts_with_attribution():
    """One seat sends a valid X but a PoP for a different key -> abort,
    and every honest seat names that seat."""
    seats = [0, 1, 2]
    ms = _secrets(seats)
    deals = {
        s: MentalDeal("s", 1, s, list(seats), 0, ms[s]) for s in seats
    }
    queue = []
    for s in seats:
        queue.extend(deals[s].start())

    # corrupt seat 2's announce: replace its pop with a pop for a random key
    from holdem.p2p.mental_deal import _pop_ctx
    bogus_x = R.random_scalar()
    bad_pop = keygen_pop.prove(bogus_x, _pop_ctx("s", 1, 2))
    for msg in queue:
        if msg["seat"] == 2:
            msg["pop_hex"] = bad_pop.hex()    # X stays, pop no longer matches

    while queue:
        msg = queue.pop(0)
        for s in seats:
            deals[s].handle(dict(msg))

    for s in (0, 1):
        assert deals[s].phase == Phase.ABORTED
        assert deals[s].bad_seat == 2


def test_rogue_key_share_aborts():
    """The rogue-key attack at the ceremony: seat 2 announces
    X_rogue = X_target - sum(others) with a pop it cannot validly make ->
    the ceremony aborts (no valid pop exists for the rogue share)."""
    seats = [0, 1, 2]
    ms = _secrets(seats)
    deals = {s: MentalDeal("s", 1, s, list(seats), 0, ms[s]) for s in seats}
    queue = []
    for s in seats:
        queue.extend(deals[s].start())

    # build the rogue share from the honest announces already in the queue
    from holdem.p2p.mental_deal import _pop_ctx
    honest = {m["seat"]: R.point_from_bytes(bytes.fromhex(m["X_hex"]))
              for m in queue if m["seat"] in (0, 1)}
    x_star = R.random_scalar()
    X_target = R.mul_base(x_star)
    X_rogue = R.sub(X_target, R.add(honest[0], honest[1]))
    # attacker's best attempt: prove for x_star (the log it knows)
    forged = keygen_pop.prove(x_star, _pop_ctx("s", 1, 2))
    for msg in queue:
        if msg["seat"] == 2:
            msg["X_hex"] = bytes(X_rogue).hex()
            msg["pop_hex"] = forged.hex()

    while queue:
        msg = queue.pop(0)
        for s in seats:
            deals[s].handle(dict(msg))

    for s in (0, 1):
        assert deals[s].phase == Phase.ABORTED
        assert deals[s].bad_seat == 2


def test_unknown_seat_aborts():
    seats = [0, 1]
    ms = _secrets(seats)
    d = MentalDeal("s", 1, 0, list(seats), 0, ms[0])
    d.start()
    d.handle({"type": "key_announce", "seat": 5,
                    "X_hex": bytes(R.mul_base(R.random_scalar())).hex(),
                    "pop_hex": keygen_pop.prove(R.random_scalar(), b"x").hex()})
    assert d.phase == Phase.ABORTED
    assert d.bad_seat == 5


def test_malformed_announce_aborts():
    seats = [0, 1]
    ms = _secrets(seats)
    d = MentalDeal("s", 1, 0, list(seats), 0, ms[0])
    d.start()
    d.handle({"type": "key_announce", "seat": 1, "X_hex": "not-hex", "pop_hex": "00"})
    assert d.phase == Phase.ABORTED


def test_conflicting_shares_from_one_seat_aborts():
    """A seat announcing two different valid shares before the ceremony
    completes is a fault. Use 3 seats so keygen is still open when the
    second (conflicting) announce for seat 1 arrives (seat 2 has not yet
    announced, so PK is not yet formed)."""
    seats = [0, 1, 2]
    ms = _secrets(seats)
    d = MentalDeal("s", 1, 0, list(seats), 0, ms[0])
    d.start()
    from holdem.p2p.mental_deal import _pop_ctx
    x1 = R.random_scalar()
    X1 = R.mul_base(x1)
    d.handle({"type": "key_announce", "seat": 1, "X_hex": bytes(X1).hex(),
              "pop_hex": keygen_pop.prove(x1, _pop_ctx("s", 1, 1)).hex()})
    assert d.phase == Phase.KEYGEN            # still open: seat 2 pending
    # a DIFFERENT valid share for seat 1 -> conflict
    x1b = R.random_scalar()
    X1b = R.mul_base(x1b)
    d.handle({"type": "key_announce", "seat": 1, "X_hex": bytes(X1b).hex(),
              "pop_hex": keygen_pop.prove(x1b, _pop_ctx("s", 1, 1)).hex()})
    assert d.phase == Phase.ABORTED
    assert d.bad_seat == 1


def test_seat_not_in_seats_in_raises():
    with pytest.raises(ValueError):
        MentalDeal("s", 1, 9, [0, 1, 2], 0, b"m")


if __name__ == "__main__":
    passed = total = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        marks = getattr(fn, "pytestmark", [])
        params = None
        for m in marks:
            if m.name == "parametrize":
                params = m.args[1]
        cases = params if params else [None]
        for c in cases:
            total += 1
            try:
                fn(c) if params else fn()
                passed += 1
                print(f"  {name}{'['+str(c)+']' if params else ''}: ok")
            except Exception as exc:
                print(f"  {name}{'['+str(c)+']' if params else ''}: FAIL - {exc}")
    print(f"{passed}/{total} passed")
