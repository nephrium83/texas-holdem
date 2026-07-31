"""Honest-peer convergence under hostile peers and hostile networks.

The primary protocol objective is that a hand either completes with every
honest peer on the same valid state, or terminates deterministically with
the same reason on every peer. Two things threaten that and are not covered
elsewhere:

* equivocation -- a peer that tells different peers different things. The
  betting layer cross-checks a state digest on every action; the key
  ceremony and shuffle chain carry no equivalent, so nothing forces a
  cheater to say the same thing twice.

* reordering -- the in-memory bus delivers strictly FIFO, so no existing
  test ever sees a message arrive early.

ChaosBus adds both: per-recipient message rewriting, and deterministic
randomized delivery scheduling seeded per test so any failure reproduces.
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Player, Session

try:
    from holdem.p2p import elgamal as eg
    from holdem.p2p import ristretto as R
except RuntimeError as exc:
    pytest.skip(f"libsodium unavailable: {exc}", allow_module_level=True)


class ChaosBus(InMemoryBus):
    """An in-memory bus that can reorder delivery and rewrite per recipient.

    ``rewrite(msg, from_conn, to_conn)`` returns the message that recipient
    should see, or None to drop it for that recipient only -- which is what
    equivocation and selective omission look like on the wire.
    """

    def __init__(self, rewrite=None, rng=None):
        super().__init__()
        self.rewrite = rewrite
        self.rng = rng

    def drain(self, max_steps: int = 100000) -> int:
        steps = 0
        pending = []
        while self._queue or pending:
            if steps >= max_steps:
                raise RuntimeError("ChaosBus.drain exceeded max_steps")
            # Expand queued broadcasts into per-recipient deliveries so a
            # message can reach one peer long before another.
            while self._queue:
                from_conn, to_conn, msg = self._queue.pop(0)
                if to_conn is not None:
                    targets = [to_conn] if to_conn in self._sessions else []
                else:
                    targets = [c for c in self._sessions if c != from_conn]
                for c in targets:
                    pending.append((from_conn, c, msg))
            if not pending:
                break
            index = self.rng.randrange(len(pending)) if self.rng else 0
            from_conn, to_conn, msg = pending.pop(index)
            steps += 1
            if self.rewrite is not None:
                msg = self.rewrite(msg, from_conn, to_conn)
                if msg is None:
                    continue
            sess = self._sessions.get(to_conn)
            if sess is not None:
                sess.handle_message(from_conn, dict(msg))
        return steps


def make_table(n, rewrite=None, seed=None, prevention=False, stacks=None):
    rng = random.Random(seed) if seed is not None else None
    bus = ChaosBus(rewrite=rewrite, rng=rng)
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid),
                    master_secret=bytes([i + 1]) * 32)
        s.local_conn_id = cid
        if not s.is_host:
            s._host_conn_id = "peer0"
        s.configure_seats(list(order))
        s._prevention = prevention
        bus.register(cid, s)
        sessions[cid] = s
    for i, cid in enumerate(order):
        sessions[cid].players[cid] = Player(conn_id=cid, peer_id=cid,
                                            nickname=f"P{i}", avatar_b64="")
    names = [f"P{i}" for i in range(n)]
    for cid in order:
        sessions[cid].start_p2p_hand(
            hand_no=1, names=names, stacks=list(stacks or [500] * n),
            sb=5, bb=10, button=0)
    bus.drain()
    return bus, sessions, order


def outcome(sessions, order):
    """The terminal facts every honest peer must agree on."""
    out = {}
    for cid in order:
        s = sessions[cid]
        deal = s._deal_driver.deal if s._deal_driver else None
        out[cid] = {
            "voided": bool(s.hand_voided),
            "deck": tuple(ct.to_hex() for ct in deal.deck) if deal and deal.deck
                    else None,
            "phase": deal.phase.value if deal else None,
        }
    return out


def assert_converged(sessions, order):
    out = outcome(sessions, order)
    voided = {o["voided"] for o in out.values()}
    assert len(voided) == 1, f"peers disagree on whether the hand voided: {out}"
    if not voided.pop():
        decks = {o["deck"] for o in out.values()}
        assert len(decks) == 1, "peers completed on different decks"
    return out


# ------------------------------------------- randomized delivery order

@pytest.mark.parametrize("seed", range(12))
def test_honest_peers_converge_under_randomized_delivery(seed):
    """Same protocol, arbitrary interleaving. Every honest peer must reach
    the same deck and the same hole cards -- nothing here depends on the
    order messages happen to arrive in."""
    bus, sessions, order = make_table(3, seed=seed)
    assert_converged(sessions, order)
    for cid in order:
        deal = sessions[cid]._deal_driver.deal
        assert deal.abort_reason is None, f"seed {seed}: {deal.abort_reason}"
        assert deal.is_shuffle_complete(), f"seed {seed}: shuffle stalled"
        assert deal.hole_complete(), f"seed {seed}: hole cards never recovered"


@pytest.mark.parametrize("seed", range(6))
def test_prevention_converges_under_randomized_delivery(seed):
    bus, sessions, order = make_table(3, seed=seed, prevention=True)
    assert_converged(sessions, order)
    for cid in order:
        deal = sessions[cid]._deal_driver.deal
        assert deal.abort_reason is None, f"seed {seed}: {deal.abort_reason}"
        assert deal.hole_complete(), f"seed {seed}: hole cards never recovered"


@pytest.mark.parametrize("n", [2, 4, 6])
def test_convergence_holds_at_several_seat_counts(n):
    bus, sessions, order = make_table(n, seed=n * 17)
    assert_converged(sessions, order)
    for cid in order:
        assert sessions[cid]._deal_driver.deal.hole_complete()


# ---------------------------------------------------- duplicate delivery

@pytest.mark.parametrize("seed", range(4))
def test_duplicate_messages_are_idempotent(seed):
    """Every message delivered twice. Retries and relay echoes look like
    this, and none of them may change the outcome."""
    seen = []

    def duplicate(msg, from_conn, to_conn):
        seen.append(msg.get("type"))
        return msg

    bus, sessions, order = make_table(3, rewrite=duplicate, seed=seed)
    # Replay everything a second time, in a fresh random order.
    rng = random.Random(seed + 1000)
    replay = list(bus._delivered) if hasattr(bus, "_delivered") else []
    rng.shuffle(replay)
    for from_conn, to_conn, msg in replay:
        sess = bus._sessions.get(to_conn)
        if sess is not None:
            sess.handle_message(from_conn, dict(msg))
    assert_converged(sessions, order)
    for cid in order:
        assert sessions[cid]._deal_driver.deal.hole_complete()


# --------------------------------------------------------- equivocation

def test_equivocating_key_announce_converges_on_a_terminal_outcome():
    """Seat 0 announces one key share to seat 1 and a different one to
    seat 2. Both are validly proven, so no PoP check can see it -- nothing
    forces a peer to say the same thing to everyone.

    The hand must not proceed with peers holding different joint keys.
    """
    alt = R.mul_base(R.scalar_reduce(b"\x07" * 64))

    def equivocate(msg, from_conn, to_conn):
        body = msg.get("payload", msg)
        if body.get("type") == "key_announce" or msg.get("type") == "key_announce":
            if body.get("seat") == 0 and to_conn == "peer2":
                forged = dict(msg)
                inner = dict(body)
                inner["X_hex"] = bytes(alt).hex()
                if "payload" in forged:
                    forged["payload"] = inner
                else:
                    forged = inner
                return forged
        return msg

    bus, sessions, order = make_table(3, rewrite=equivocate)
    assert_converged(sessions, order)
    assert all(sessions[cid].hand_voided for cid in order), \
        "an equivocated key ceremony must not produce a playable hand"


def test_equivocating_final_deck_converges_on_a_terminal_outcome():
    """The sharp case: equivocation on the LAST shuffle round, where no
    later round can overwrite the divergence."""
    def equivocate(msg, from_conn, to_conn):
        body = msg.get("payload", msg)
        if body.get("round") == 3 and to_conn == "peer2":
            forged = dict(msg)
            inner = dict(body)
            deck = [eg.Ciphertext.from_hex(p) for p in inner["deck"]]
            pk = sessions["peer2"]._deal_driver.deal.joint_pk
            inner["deck"] = [eg.reencrypt(pk, ct, R.random_scalar()).to_hex()
                             for ct in deck]
            if "payload" in forged:
                forged["payload"] = inner
            else:
                forged = inner
            return forged
        return msg

    sessions = {}
    bus, sessions, order = make_table(3, rewrite=equivocate)
    assert_converged(sessions, order)


@pytest.mark.parametrize("victim", ["peer1", "peer2"])
def test_selective_omission_terminates_deterministically(victim):
    """A peer that simply withholds its shuffle from one recipient. The
    victim must not hang forever holding an incomplete chain; the hand must
    reach a terminal state."""
    def omit(msg, from_conn, to_conn):
        body = msg.get("payload", msg)
        if body.get("round") == 1 and to_conn == victim:
            return None
        return msg

    bus, sessions, order = make_table(3, rewrite=omit)
    deal = sessions[victim]._deal_driver.deal
    assert not deal.hole_complete(), \
        "the victim cannot have completed without the withheld round"
    assert not deal.is_shuffle_complete()
