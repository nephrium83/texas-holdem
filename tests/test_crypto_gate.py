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
* The environment-policy tests drive ``crypto_required`` over the full
  cross-product of the two variables, so removing the CI default or the
  override fails a named test rather than silently widening what a green
  run is allowed to mean.
"""
from __future__ import annotations

import sys
from pathlib import Path

from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crypto_gate
from crypto_gate import (
    CI_ENV, REQUIRE_ENV, CryptoStatus, CryptoUnavailable,
    crypto_required, crypto_status, enforce, flag, header_line,
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


def test_unrecognised_flag_falls_back_to_the_ci_default():
    """Garbage must not read as 'excused'. ``CI`` decides instead."""
    assert crypto_required({CI_ENV: "true", REQUIRE_ENV: "maybe"}) is True
    assert crypto_required({REQUIRE_ENV: "maybe"}) is False


def test_flag_is_three_valued():
    """Unset must be distinguishable from false, or the CI default has
    nothing left to decide."""
    assert flag(None) is None
    assert flag("") is None
    assert flag("1") is True
    assert flag("0") is False
