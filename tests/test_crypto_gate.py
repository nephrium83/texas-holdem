"""The crypto estate must not leave the run unannounced.

One load-bearing test and its policy unit tests. The invariant:

    A run that reports success must either have exercised the crypto-gated
    suites, or have said out loud that it did not.

Why this is a security test rather than housekeeping: the mental-poker
security properties -- Bayer-Groth prevention (#37 invariant 7), DLEQ,
deck-audit soundness, shuffle soundness -- are asserted ONLY by suites that
skip themselves at module import when libsodium is missing. A runner
without the library therefore drops several hundred adversarial tests and
prints green. Nothing distinguishes that from a full pass in the summary
line, so evidence quoted from such a run is wrong without anyone lying.

DELIBERATE-BREAK CONTROLS

The controls here are executed on every run rather than described, because
the interesting break -- "libsodium is missing" -- is a property of the
machine and cannot be produced by editing code. Instead the policy takes
its inputs by argument, so both branches are driven for real:

* ``test_enforce_raises_when_required_and_unavailable`` IS the break: a
  synthetic unavailable status plus a required environment must raise. If
  ``enforce`` is reduced to ``return``, this test fails.
* ``test_enforce_is_silent_when_available`` and
  ``test_enforce_is_silent_when_not_required`` are the negative controls,
  so the break above cannot be satisfied by a function that always raises.
* ``test_control_a_typoed_arming_used_to_leave_crypto_optional`` stages the
  earlier fallback policy and shows the observable flip back, with
  ``test_control_the_refusal_does_not_swallow_a_readable_override`` beside
  it so "refuse everything" cannot pass for a fix.
* The environment-policy tests drive ``crypto_required`` over the full
  cross-product of the two variables, so removing the CI default or the
  override fails a named test rather than silently widening what a green
  run is allowed to mean. That cross-product includes the values the
  override does NOT recognise, because "what does a typo mean" is a policy
  decision here and not a detail.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crypto_gate
from crypto_gate import (
    CI_ENV, REQUIRE_ENV, CryptoGateMisconfigured, CryptoStatus,
    CryptoUnavailable, crypto_required, crypto_status, enforce, flag,
    header_line,
)

_MISSING = CryptoStatus(False, "RuntimeError: libsodium ... could not be loaded")
_PRESENT = CryptoStatus(True, "libsodium 1.0.20")


# ------------------------------------------------------- the real check

def test_crypto_is_available_wherever_it_is_required():
    """The load-bearing one.

    On CI this fails the run when the crypto suites would have skipped. On
    a developer machine without libsodium it SKIPS with a stated reason
    rather than passing quietly -- a pass here would be the same silent
    green this file exists to remove.
    """
    status = crypto_status()
    required = crypto_required()
    if not status.available and not required:
        pytest.skip(
            f"libsodium unavailable and not required in this environment "
            f"({REQUIRE_ENV} unset or 0, {CI_ENV} not set): the crypto-gated "
            f"suites did not run. {status.detail}")
    enforce(status, required)


def test_a_present_libsodium_is_the_one_the_protocol_needs():
    """Loading is not enough -- it must expose the Ristretto255 group.

    ristretto.py refuses a build without the API at import time, so this
    reads the same fact from the other side: the group sizes the shuffle
    and the DLEQ proofs are built against. It matters because a libsodium
    can be present and still be the wrong one -- PyNaCl's bundled copy
    does not expose Ristretto255 (see native/README.md), so "pip install
    pynacl" is exactly the near-miss this check catches.
    """
    crypto_gate.require_crypto()
    from holdem.p2p import ristretto as R

    assert (R.POINT_BYTES, R.SCALAR_BYTES, R.HASH_BYTES) == (32, 32, 64)


def test_a_status_that_loaded_but_cannot_answer_is_not_reported_absent():
    """'Absent' and 'broken' send the reader to different places.

    The stub replaces the ATTRIBUTE on holdem.p2p rather than the
    sys.modules entry: crypto_status does ``from holdem.p2p import
    ristretto``, which reads the attribute off the already-imported
    package and never consults sys.modules again.
    """
    import holdem.p2p

    class Stub:
        @staticmethod
        def libsodium_version():
            raise OSError("truncated library")

    with mock.patch.object(holdem.p2p, "ristretto", Stub):
        st = crypto_status(refresh=True)
    crypto_status(refresh=True)          # restore the real probe result

    assert st.available is False, (
        "a library that imports and then fails to answer is not 'absent'")
    assert "loaded but unusable" in st.detail
    assert "truncated library" in st.detail


def test_status_and_header_agree():
    """The header cannot claim the suites ran while the probe disagrees.

    Matched on the whole phrase rather than on "RUN": the unavailable
    header carries the loader's diagnosis, which is full of filesystem
    paths, and a machine whose paths happen to contain those three letters
    would flip this assertion for no reason at all.
    """
    status = crypto_status()
    line = header_line(status, required=False)
    assert line.startswith("crypto: ")
    assert ("suites RUN" in line) is status.available
    assert ("suites SKIP" in line) is not status.available
    assert status.detail in line


# ------------------------------------------------------------- enforce

def test_enforce_raises_when_required_and_unavailable():
    with pytest.raises(CryptoUnavailable) as exc:
        enforce(_MISSING, required=True)
    message = str(exc.value)
    assert REQUIRE_ENV in message, "the failure must say how to excuse it"
    assert _MISSING.detail in message, "the failure must carry the diagnosis"


def test_enforce_is_silent_when_available():
    enforce(_PRESENT, required=True)


def test_enforce_is_silent_when_not_required():
    enforce(_MISSING, required=False)


# -------------------------------------------------------------- policy

def test_ci_requires_crypto_by_default():
    assert crypto_required({CI_ENV: "true"}) is True


def test_a_developer_machine_does_not():
    assert crypto_required({}) is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_explicit_requirement_wins_off_ci(value):
    assert crypto_required({REQUIRE_ENV: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_explicit_excusal_wins_on_ci(value):
    """A runner that genuinely cannot install libsodium must say so in the
    workflow, where a reviewer reads it -- not by the suite deciding."""
    assert crypto_required({CI_ENV: "true", REQUIRE_ENV: value}) is False


@pytest.mark.parametrize("env", [
    {REQUIRE_ENV: "tru"},                       # the arming typo, off CI
    {REQUIRE_ENV: "maybe"},
    {CI_ENV: "true", REQUIRE_ENV: "maybe"},     # and on it
])
def test_an_unreadable_requirement_is_refused_not_guessed(env):
    """A value the gate cannot read must not resolve to a posture.

    This is the security half of the override. Falling back to the ``CI``
    default is harmless on CI, where the default is "required" anyway, and
    wrong exactly where the override gets typed: ``HOLDEM_REQUIRE_CRYPTO=tru
    pytest`` on a developer machine is somebody ARMING the gate, and the
    fallback answers "not required" -- so the run they armed drops the whole
    crypto estate and still reports success. A run cannot be evidence and be
    unable to say whether it covered the crypto suites.

    The failure has to be actionable, hence the two assertions: it names the
    variable and quotes the value back, because "misconfigured" without the
    offending string sends the reader to re-read their own shell history.
    """
    with pytest.raises(CryptoGateMisconfigured) as exc:
        crypto_required(env)
    message = str(exc.value)
    assert REQUIRE_ENV in message
    assert repr(env[REQUIRE_ENV]) in message


@pytest.mark.parametrize("value", ["", "   "])
def test_a_set_but_empty_requirement_reads_as_unset(value):
    """``VAR= cmd`` is a shell neutralising a variable, not a typo.

    Treated as unset, so the ``CI`` default still decides -- which keeps the
    strictness above aimed at values that were meant to say something.
    """
    assert crypto_required({REQUIRE_ENV: value}) is False
    assert crypto_required({CI_ENV: "true", REQUIRE_ENV: value}) is True


def test_an_unreadable_ci_flag_is_not_a_claim_that_this_run_is_evidence():
    """``CI`` is inferred, not instructed, so it is read leniently.

    The asymmetry is deliberate. ``CI`` is set by a runner and merely
    signals "this looks like CI"; a value nobody recognises is no signal.
    ``HOLDEM_REQUIRE_CRYPTO`` is typed by a human to state a posture, and an
    unreadable statement of posture is a broken instruction, not a
    non-signal.
    """
    assert crypto_required({CI_ENV: "possibly"}) is False


def test_the_header_reports_a_misconfigured_gate_rather_than_crashing():
    """The reporter must survive what the policy refuses.

    ``header_line`` runs from ``pytest_report_header`` and
    ``pytest_terminal_summary``; raising there would turn a typo into an
    INTERNALERROR and bury the results of every test that did run. The gate
    still fails -- through the tests above, as a named failure -- while the
    log says in one line what is wrong.
    """
    with mock.patch.dict(os.environ, {REQUIRE_ENV: "tru"}):
        line = header_line()

    assert "MISCONFIGURED" in line
    assert REQUIRE_ENV in line
    assert "'tru'" in line


def test_require_crypto_refuses_a_misconfigured_gate_before_availability():
    """A gate nobody can read must not be what excuses the suites it guards.

    ``require_crypto`` is the one-line guard a crypto-dependent test calls,
    and its whole job is to choose between running, skipping and failing.
    With the posture unreadable it cannot make that choice, so it raises --
    regardless of whether libsodium happens to be present here, which is
    what makes this test deterministic on both kinds of machine.
    """
    with mock.patch.dict(os.environ, {REQUIRE_ENV: "yess"}):
        with pytest.raises(CryptoGateMisconfigured):
            crypto_gate.require_crypto()


def test_flag_is_three_valued():
    """Unset must be distinguishable from false, or the CI default has
    nothing left to decide."""
    assert flag(None) is None
    assert flag("") is None
    assert flag("1") is True
    assert flag("0") is False


# ------------------------------------------------------------- controls

def _prefix_crypto_required(env=None):
    """``crypto_required`` as it stood before the invalid-value refusal.

    The whole difference is the missing middle branch: an explicit value
    that parses to neither true nor false fell through to the ``CI``
    default, exactly as if the variable had never been set.
    """
    env = os.environ if env is None else env
    explicit = flag(env.get(REQUIRE_ENV))
    if explicit is not None:
        return explicit
    return flag(env.get(CI_ENV)) is True


def test_control_a_typoed_arming_used_to_leave_crypto_optional():
    """CONTROL FOR test_an_unreadable_requirement_is_refused_not_guessed.

    The break is staged as a function rather than a monkeypatch because the
    policy already takes its environment by argument -- patching the module
    would prove nothing the direct call does not. Off CI the old fallback
    answers ``False`` to somebody who just typed a requirement: the gate
    reports "skips permitted here", the crypto suites leave the run, and the
    summary is green. The named test's ``pytest.raises`` cannot hold against
    this behaviour, which is what makes it load-bearing.
    """
    assert _prefix_crypto_required({REQUIRE_ENV: "tru"}) is False, (
        "the pre-fix fallback read a mistyped arming as 'not required'")


def test_control_the_refusal_does_not_swallow_a_readable_override():
    """NEGATIVE CONTROL: against the REAL policy, both directions.

    A ``crypto_required`` that raised on everything would satisfy the break
    above and destroy the override, which is the more useful half of it.
    """
    assert crypto_required({REQUIRE_ENV: "1"}) is True
    assert crypto_required({CI_ENV: "true", REQUIRE_ENV: "0"}) is False
