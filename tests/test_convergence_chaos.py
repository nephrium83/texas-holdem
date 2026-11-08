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

from holdem.p2p.timeout import FakeClock as _DependencyClock
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Player, Session

from crypto_gate import initialize_for_tests, NativeCryptoUnavailable

try:
    initialize_for_tests()
    from holdem.p2p import elgamal as eg
    from holdem.p2p import ristretto as R
except NativeCryptoUnavailable as exc:
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
        # Every (from, to, msg) actually handed to a session -- what a
        # duplicate-delivery test replays (audit #144: it used to replay a
        # `_delivered` attribute that never existed, i.e. nothing).
        self.delivered = []

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
                self.delivered.append((from_conn, to_conn, dict(msg)))
                sess.handle_message(from_conn, dict(msg))
        return steps


def make_table(n, rewrite=None, seed=None, prevention=False, stacks=None,
               registry=None):
    """``registry``, if given, is filled with the sessions BEFORE any message
    is delivered, so a rewrite callback can consult them mid-drain (audit
    #141: callbacks read a dict make_table had not returned yet)."""
    rng = random.Random(seed) if seed is not None else None
    bus = ChaosBus(rewrite=rewrite, rng=rng)
    order = [f"peer{i}" for i in range(n)]
    sessions = {}
    for i, cid in enumerate(order):
        s = Session(is_host=(i == 0), nickname=f"P{i}", avatar_b64="",
                    transport=InMemoryTransport(bus, cid),
                    master_secret=bytes([i + 1]) * 32,
                    clock=_DependencyClock())
        s.local_conn_id = cid
        if not s.is_host:
            s._host_conn_id = "peer0"
        s.configure_seats(list(order))
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        s._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
        s._prevention = prevention
        bus.register(cid, s)
        sessions[cid] = s
    for i, cid in enumerate(order):
        sessions[cid].players[cid] = Player(conn_id=cid, peer_id=cid,
                                            nickname=f"P{i}", avatar_b64="")
    if registry is not None:
        registry.update(sessions)
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
            "settled": s.hand_result is not None,
            "deck": tuple(ct.to_hex() for ct in deal.deck) if deal and deal.deck
                    else None,
            "phase": deal.phase.value if deal else None,
            "abort": deal.abort_reason if deal else None,
            "holes": deal.hole_complete() if deal else False,
            # A third shape, introduced by #194: a table with no agreed way
            # to recover ends rather than voiding or settling.
            "terminal": s.terminal_state,
        }
    return out


def assert_converged(sessions, order):
    """Every peer agrees, and the agreement is coherent (audit #143): the
    old check compared only the void flag and, if not void, the deck -- so
    peers on different phases, with different abort states, or stalled
    without holes all passed."""
    out = outcome(sessions, order)
    voided = {o["voided"] for o in out.values()}
    assert len(voided) == 1, f"peers disagree on whether the hand voided: {out}"
    assert not any(o["voided"] and o["settled"] for o in out.values()), \
        f"a peer both voided and settled the hand: {out}"
    if voided.pop():
        return out

    # An ENDED table is the third outcome (#194). With no timekeeper there
    # is no agreed recovery, so the session terminates instead of voiding;
    # such a peer holds no hole cards and is not a live hand, so the
    # live-hand checks below cannot apply to it. What must still hold is
    # that every peer agrees it ended, and ended the same way -- a table
    # where one peer ended and the others play on IS disagreement, and the
    # single-value assertion catches it.
    ended = {o["terminal"] for o in out.values()}
    if any(ended):
        assert len(ended) == 1, f"peers disagree on the terminal state: {out}"
        assert not any(o["settled"] for o in out.values()), \
            f"a peer both ended the session and settled the hand: {out}"
        return out

    for key in ("deck", "phase", "abort", "holes"):
        values = {o[key] for o in out.values()}
        assert len(values) == 1, f"peers disagree on {key}: {out}"
    assert all(o["abort"] is None for o in out.values()), out
    assert all(o["holes"] for o in out.values()), \
        f"a live hand has a peer without hole cards: {out}"
    return out


def drive_to_terminal(bus, sessions, order, rounds=40, step=3600.0):
    """Liveness: advance every peer's clock past its deadline, run the
    PRODUCTION periodic path, deliver what it emits -- until every peer has
    reached a terminal outcome for the hand (or fail).

    reliability_tick, not check_deadlines. check_deadlines reports a wait
    and applies nothing by design, and has no production caller;
    reliability_tick is what a host app polls, and it is where a stalled
    table without a timekeeper is ended (#194). Driving the report-only
    path meant this helper exercised an announcement rather than the
    behaviour its name describes.
    """
    def terminal(s):
        return s.hand_voided or s.hand_result is not None or s.terminal_state
    for _ in range(rounds):
        if all(terminal(sessions[c]) for c in order):
            return
        for c in order:
            sessions[c]._clock.advance(step)
            sessions[c].reliability_tick()
        bus.drain()
    stuck = [c for c in order if not terminal(sessions[c])]
    pytest.fail(f"peers never reached a terminal outcome: {stuck}")


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
    before = outcome(sessions, order)
    # Replay everything a second time, in a fresh random order.
    rng = random.Random(seed + 1000)
    replay = list(bus.delivered)
    types = {m.get("type") for _, _, m in replay}
    assert len(replay) > 0, "nothing was delivered, so nothing was replayed"
    assert {"key_announce", "deck_round", "deal_share"} <= types, types
    rng.shuffle(replay)
    for from_conn, to_conn, msg in replay:
        sess = bus._sessions.get(to_conn)
        if sess is not None:
            sess.handle_message(from_conn, dict(msg))
    bus.drain()
    assert_converged(sessions, order)
    assert outcome(sessions, order) == before, "a duplicate changed the outcome"
    for cid in order:
        assert sessions[cid]._deal_driver.deal.hole_complete()


# --------------------------------------------------------- equivocation

def test_equivocating_key_announce_converges_on_a_terminal_outcome():
    """Seat 0 announces one key share to seat 1 and a different one to
    seat 2. Both are validly proven, so no PoP check can see it -- nothing
    forces a peer to say the same thing to everyone.

    The hand must not proceed with peers holding different joint keys.
    """
    # Audit #142: the second announcement must be individually VALID -- a
    # different scalar with its own matching proof under seat 0's real
    # context -- or it is rejected as an ordinary bad proof, which is not
    # the equivocation threat at all.
    from holdem.p2p import keygen_pop
    from holdem.p2p.mental_deal import _pop_ctx
    alt_x = R.scalar_reduce(b"\x07" * 64)
    registry, fired = {}, []

    def equivocate(msg, from_conn, to_conn):
        body = msg.get("payload", msg)
        if body.get("type") == "key_announce" or msg.get("type") == "key_announce":
            if body.get("seat") == 0 and to_conn == "peer2":
                deal = registry["peer0"]._deal_driver.deal
                ctx = _pop_ctx(deal.session_id, deal.hand_no, 0)
                inner = dict(body)
                inner["X_hex"] = bytes(R.mul_base(alt_x)).hex()
                inner["pop_hex"] = keygen_pop.prove(alt_x, ctx).hex()
                assert keygen_pop.verify(R.mul_base(alt_x),
                                         bytes.fromhex(inner["pop_hex"]), ctx)
                fired.append(1)
                forged = dict(msg)
                if "payload" in forged:
                    forged["payload"] = inner
                else:
                    forged = inner
                return forged
        return msg

    bus, sessions, order = make_table(3, rewrite=equivocate, registry=registry)
    assert fired, "the equivocating announcement was never delivered"
    drive_to_terminal(bus, sessions, order)
    assert_converged(sessions, order)
    assert all(sessions[cid].hand_voided for cid in order), \
        "an equivocated key ceremony must not produce a playable hand"


def test_equivocating_final_deck_converges_on_a_terminal_outcome():
    """The sharp case: equivocation on the LAST shuffle round, where no
    later round can overwrite the divergence."""
    registry, fired = {}, []

    def equivocate(msg, from_conn, to_conn):
        body = msg.get("payload", msg)
        # Round 3 is shuffled BY peer2 and a broadcast is not echoed to its
        # sender, so peer2 never receives it: the original rewrite targeted
        # a delivery that cannot happen. peer1 does receive it.
        if body.get("round") == 3 and to_conn == "peer1":
            fired.append(1)
            forged = dict(msg)
            inner = dict(body)
            deck = [eg.Ciphertext.from_hex(p) for p in inner["deck"]]
            pk = registry["peer1"]._deal_driver.deal.joint_pk
            inner["deck"] = [eg.reencrypt(pk, ct, R.random_scalar()).to_hex()
                             for ct in deck]
            if "payload" in forged:
                forged["payload"] = inner
            else:
                forged = inner
            return forged
        return msg

    bus, sessions, order = make_table(3, rewrite=equivocate, registry=registry)
    assert fired, "the round-3 equivocation branch never ran (audit #141)"
    out = assert_converged(sessions, order)
    assert all(o["voided"] for o in out.values()), \
        f"peers on different final decks must not play the hand: {out}"


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
    # Audit #143 -- SAFETY, which does hold: nobody plays or settles a hand
    # whose shuffle chain one peer never saw, and peers that did finish the
    # shuffle agree on the deck. (Liveness -- reaching a terminal outcome --
    # is pinned separately below as a known gap.)
    out = outcome(sessions, order)
    assert not any(o["settled"] for o in out.values()), out
    done = {o["deck"] for c, o in out.items()
            if sessions[c]._deal_driver.deal.is_shuffle_complete()}
    assert len(done) <= 1, f"peers finished the shuffle on different decks: {out}"


@pytest.mark.parametrize("victim", ["peer1", "peer2"])
def test_selective_omission_reaches_a_terminal_outcome(victim):
    """A withheld shuffle round must not leave the table waiting for ever.

    Audit #194, and the owner decision of 2026-11-07: recover under the
    table's agreed rules, or end clearly. This table runs no casual
    reliability, so it has no timekeeper and no drop rule -- there is
    nothing to recover under, and _end_if_stalled ends it.

    It does NOT void. The previous expectation here was a void, which
    assumed the timekeeper rule 9 defines for CASUAL tables and that this
    path has never had: a void is recoverable and redeals the same seats at
    the same button, so on a clock it would redeal straight back into the
    same stall. That expectation rode along under a strict xfail, so it was
    never run against the behaviour it described.
    """
    def omit(msg, from_conn, to_conn):
        body = msg.get("payload", msg)
        if body.get("round") == 1 and to_conn == victim:
            return None
        return msg

    bus, sessions, order = make_table(3, rewrite=omit)
    drive_to_terminal(bus, sessions, order)
    out = assert_converged(sessions, order)

    assert all(o["terminal"] == Session.ABORTED_PROTOCOL
               for o in out.values()), out
    assert not any(o["voided"] or o["settled"] for o in out.values()), out
    # The reason has to say what was waited on, or an operator reading a
    # dead table learns nothing from it.
    for cid in order:
        reason = sessions[cid].terminal_reason
        assert "deal_shuffle" in reason, reason
        assert "no agreed way to recover" in reason, reason
