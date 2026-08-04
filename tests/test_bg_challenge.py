"""Pins holdem/p2p/bg_challenge.py -- uniform nonzero challenge derivation.

Three properties here cannot be seen end-to-end and get direct assertions:

* ``test_counter_advances_the_preimage_not_the_digest`` -- retrying by
  re-hashing the previous digest also terminates, so every completeness
  test passes either way. It is a different construction.
* ``test_hadamard_x_and_y_stay_separated_under_retries`` -- if the two
  labels shared one counter, a retry on x would silently shift y.
* ``test_counter_encodings_cannot_collide`` -- goes through the helper
  rather than re-implementing its encoding, so it can actually fail when
  that encoding changes.

Where a zero reduction is forced, it is forced BY DIGEST, never by call
count. Keying on call count makes prover and verifier hit the zero at
different points and land on different counters, which is the exact
divergence the retry rule exists to prevent.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from holdem.p2p import ristretto as R
    from holdem.p2p import pedersen as P
    from holdem.p2p import bg_challenge as C
    from holdem.p2p import bg_svp as S
    from holdem.p2p import bg_zero as Z
    from holdem.p2p import bg_hadamard as H
except RuntimeError as exc:
    pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                allow_module_level=True)


ZERO = R.Scalar(b"\x00" * 32)
CTX = b"session=1|hand=7"


def _h(*chunks: bytes):
    h = hashlib.sha512()
    for c in chunks:
        h.update(c)
    return h


def _sc(i: int) -> R.Scalar:
    return R.scalar_reduce(hashlib.sha512(f"chal:{i}".encode()).digest())


class _PoisonBy:
    """Wraps ristretto so digests satisfying ``pred`` reduce to zero.

    Installed on bg_challenge only, so nothing but challenge attempts is
    affected. Keyed on the digest, so the same preimage reduces to zero
    for whoever hashes it -- prover and verifier alike.
    """

    def __init__(self, real, pred):
        self._real = real
        self._pred = pred
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def scalar_reduce(self, wide):
        self.calls += 1
        if self._pred(bytes(wide)):
            return ZERO
        return self._real.scalar_reduce(wide)


def _poison_first(monkeypatch, module=C):
    """Force the first challenge preimage seen to reduce to zero."""
    real = module.R
    adopted: set = set()

    def pred(digest):
        if not adopted:
            adopted.add(digest)
        return digest in adopted

    fake = _PoisonBy(real, pred)
    monkeypatch.setattr(module, "R", fake)
    return fake, adopted


# ------------------------------------------------------------- basic shape

def test_returns_a_nonzero_scalar():
    x = C.nonzero_challenge(_h(b"transcript"))
    assert isinstance(x, R.Scalar)
    assert not R.is_zero_scalar(x)


def test_is_deterministic():
    assert bytes(C.nonzero_challenge(_h(b"t"))) == \
        bytes(C.nonzero_challenge(_h(b"t")))


def test_different_transcripts_give_different_challenges():
    assert bytes(C.nonzero_challenge(_h(b"a"))) != \
        bytes(C.nonzero_challenge(_h(b"b")))


def test_different_labels_give_different_challenges():
    h = _h(b"same-transcript")
    assert bytes(C.nonzero_challenge(h, b"x")) != \
        bytes(C.nonzero_challenge(h, b"y"))


def test_default_label_is_the_empty_label():
    h = _h(b"t")
    assert bytes(C.nonzero_challenge(h)) == bytes(C.nonzero_challenge(h, b""))


def test_the_hash_object_is_not_consumed():
    """Callers draw several labelled challenges from one transcript, so
    the helper must copy rather than absorb into the caller's hash."""
    h = _h(b"t")
    first = C.nonzero_challenge(h, b"x")
    C.nonzero_challenge(h, b"y")
    assert bytes(C.nonzero_challenge(h, b"x")) == bytes(first)
    assert h.digest() == _h(b"t").digest()


# ------------------------------------------------------ the encoding itself

def test_counter_encodings_cannot_collide():
    """Distinct (label, counter) pairs must give distinct challenges.

    This goes through the helper rather than re-implementing its
    encoding. An earlier version of this test rebuilt the byte layout
    inline, which meant it could not fail when the real encoding changed
    -- it was testing a copy of itself.

    The labels below are chosen to be exactly the pairs a naive
    concatenation would confuse: b"ab" then b"" collides with b"a" then
    b"b" unless the length prefix or the fixed-width counter separates
    them.
    """
    h = _h(b"t")
    labels = [b"", b"a", b"b", b"ab", b"ba", b"a\x00", b"\x00a",
              b"x", b"y", b"z", b"\x00\x00\x00\x01"]
    drawn = [bytes(C.nonzero_challenge(h, lab)) for lab in labels]
    assert len(set(drawn)) == len(labels)


def test_label_length_prefix_separates_ambiguous_boundaries():
    """b"ab" and b"a" must not be confusable even though one is a prefix
    of the other and the counter follows immediately."""
    h = _h(b"t")
    assert bytes(C.nonzero_challenge(h, b"ab")) != \
        bytes(C.nonzero_challenge(h, b"a"))


def test_attempt_bound_is_enforced_not_infinite(monkeypatch):
    real = C.R
    monkeypatch.setattr(C, "R", _PoisonBy(real, lambda _d: True))
    with pytest.raises(RuntimeError, match="exhausted"):
        C.nonzero_challenge(_h(b"t"))


def test_attempt_bound_is_the_declared_one(monkeypatch):
    real = C.R
    fake = _PoisonBy(real, lambda _d: True)
    monkeypatch.setattr(C, "R", fake)
    with pytest.raises(RuntimeError):
        C.nonzero_challenge(_h(b"t"))
    assert fake.calls == C.MAX_CHALLENGE_ATTEMPTS


# ------------------------------------------------------------ retry semantics

def test_zero_reduction_retries_instead_of_raising(monkeypatch):
    """The old behaviour raised here. Under Fiat-Shamir that left an
    honest prover with no remedy: the transcript is not theirs to
    resample."""
    fake, _adopted = _poison_first(monkeypatch)
    x = C.nonzero_challenge(_h(b"t"))
    assert not R.is_zero_scalar(x)
    assert fake.calls == 2, "should have advanced exactly one counter"


def test_counter_advances_the_preimage_not_the_digest(monkeypatch):
    """Retrying by re-hashing the previous digest also terminates and
    also yields a nonzero scalar, so no end-to-end test can tell the two
    apart. Pin the actual construction: attempt k hashes the transcript
    with counter k appended.
    """
    seen = []
    real = C.R
    real_reduce = real.scalar_reduce

    class Recorder:
        def __getattr__(self, name):
            return getattr(real, name)

        def scalar_reduce(self, wide):
            seen.append(bytes(wide))
            return ZERO if len(seen) == 1 else real_reduce(wide)

    # Forced by call count ONLY because this test drives the helper
    # directly and there is no second party to diverge from.
    monkeypatch.setattr(C, "R", Recorder())
    C.nonzero_challenge(_h(b"transcript"), b"lbl")

    expected = [
        _h(b"transcript", len(b"lbl").to_bytes(4, "big"), b"lbl",
           k.to_bytes(4, "big")).digest()
        for k in (0, 1)
    ]
    assert seen == expected
    # And specifically NOT the digest-chaining alternative.
    assert seen[1] != hashlib.sha512(seen[0]).digest()


def test_retry_is_reproducible_by_the_verifier(monkeypatch):
    """Prover and verifier hash the same bytes, so a digest that reduces
    to zero does so for both and they land on the same counter."""
    _fake, _adopted = _poison_first(monkeypatch)
    first = C.nonzero_challenge(_h(b"t"))
    second = C.nonzero_challenge(_h(b"t"))
    assert bytes(first) == bytes(second)


# --------------------------------------------- label independence under retry

def test_labels_have_independent_counter_sequences(monkeypatch):
    """A retry on one label must not shift another.

    With a shared counter, forcing x to counter 1 would push y to
    counter 2 and change a challenge that had no reason to move.
    """
    h = _h(b"t")
    clean_y = bytes(C.nonzero_challenge(h, b"y"))

    x_zero = _h(b"t", len(b"x").to_bytes(4, "big"), b"x",
                (0).to_bytes(4, "big")).digest()
    real = C.R
    monkeypatch.setattr(C, "R", _PoisonBy(real, lambda d: d == x_zero))

    C.nonzero_challenge(h, b"x")                 # forced to counter 1
    assert bytes(C.nonzero_challenge(h, b"y")) == clean_y


def test_hadamard_x_and_y_stay_separated_under_retries(monkeypatch):
    """The same property at the call site that has two labels."""
    transcript = _h(b"hadamard-transcript")
    clean_x, clean_y = H._challenges(transcript)

    y_zero = _h(b"hadamard-transcript", len(b"y").to_bytes(4, "big"), b"y",
                (0).to_bytes(4, "big")).digest()
    real = C.R
    monkeypatch.setattr(C, "R", _PoisonBy(real, lambda d: d == y_zero))

    x, y = H._challenges(_h(b"hadamard-transcript"))
    assert bytes(x) == bytes(clean_x), "y's retry moved x"
    assert bytes(y) != bytes(clean_y), "y should have advanced a counter"


def test_forced_zero_on_counter_zero_succeeds_using_counter_one(monkeypatch):
    """Pins where the counter sits.

    If the counter were written BEFORE the label, the counter-0 preimage
    for label b"x" would be a different byte string than the one built
    here and the poison would miss, so this test would report success
    without a retry ever happening. The ``calls`` assertion is what
    catches that.
    """
    x_zero = _h(b"t", len(b"x").to_bytes(4, "big"), b"x",
                (0).to_bytes(4, "big")).digest()
    real = C.R
    fake = _PoisonBy(real, lambda d: d == x_zero)
    monkeypatch.setattr(C, "R", fake)

    x = C.nonzero_challenge(_h(b"t"), b"x")
    assert fake.calls == 2, "poison missed; counter is not where we think"
    expected = R.scalar_reduce(
        _h(b"t", len(b"x").to_bytes(4, "big"), b"x",
           (1).to_bytes(4, "big")).digest())
    assert bytes(x) == bytes(expected)


# ------------------------------------------ prover/verifier agreement in situ

def _svp_setup(n=4):
    ck = P.CommitmentKey.generate(n, seed=b"chal-svp")
    a = [_sc(i + 1) for i in range(n)]
    r = _sc(500)
    b = a[0]
    for v in a[1:]:
        b = R.scalar_mul(b, v)
    return ck, a, r, b, P.commit(ck, a, r)


def test_svp_agrees_across_a_retry(monkeypatch):
    ck, a, r, b, c_a = _svp_setup()
    _poison_first(monkeypatch)
    proof = S.prove(ck, a, r, b, CTX)
    assert S.verify(ck, c_a, len(a), b, CTX, proof)


def _zero_setup(m=2, n=2):
    ck = P.CommitmentKey.generate(n, seed=b"chal-zero")
    A = [[_sc(10 * i + j) for j in range(n)] for i in range(m + 1)]
    rA = [_sc(700 + i) for i in range(m + 1)]
    return ck, A, rA


def test_hadamard_agrees_across_a_retry(monkeypatch):
    ck = P.CommitmentKey.generate(3, seed=b"chal-had")
    m, n = 3, 3
    a = [[_sc(100 * (i + 1) + j) for j in range(n)] for i in range(m)]
    r = [_sc(900 + i) for i in range(m)]
    c_A = [P.commit(ck, a[i], r[i]) for i in range(m)]
    b = list(a[0])
    for i in range(1, m):
        b = [R.scalar_mul(b[j], a[i][j]) for j in range(n)]
    s = _sc(950)
    c_b = P.commit(ck, b, s)

    _poison_first(monkeypatch)
    proof = H.prove(ck, c_A, a, r, c_b, b, s, CTX)
    assert H.verify(ck, c_A, c_b, n, CTX, proof)


# ------------------------------------------------------------- no stragglers

def test_no_module_still_raises_on_a_zero_challenge():
    """The point of this commit: one derivation rule, not four spellings
    of a rejection."""
    import inspect
    for mod in (S, Z, H):
        src = inspect.getsource(mod)
        assert "reduced to zero" not in src, mod.__name__
        assert "nonzero_challenge" in src, mod.__name__


def test_shuffle_uses_the_shared_helper():
    import inspect
    from holdem.p2p import bg_shuffle as SH
    src = inspect.getsource(SH)
    assert "reduced to zero" not in src
    assert src.count("nonzero_challenge") >= 4
