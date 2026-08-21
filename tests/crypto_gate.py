"""Did the crypto-gated suites actually run, and was skipping them allowed?

Every crypto-backed module in this suite guards its own import::

    try:
        from holdem.p2p import ristretto as R
    except RuntimeError as exc:          # libsodium not available here
        pytest.skip(f"libsodium/ristretto unavailable: {exc}",
                    allow_module_level=True)

That is right on a developer machine with no libsodium, and it is a
reporting hazard anywhere the result is used as evidence. With the library
missing, the entire mental-poker security estate -- Bayer-Groth prevention,
DLEQ, the deck audit, the shuffle soundness suites -- leaves the run, and
pytest still prints a green summary. The skip is silent by construction:
``pytest -q`` reports a count and never a reason, so "1338 passed, 3
skipped" and "900 passed, 441 skipped" read the same way at a glance.

What pins crypto availability on CI today is accidental rather than
stated. The ``sidecar-integration`` and ``godot-sidecar`` jobs run a real
mental-poker deal in a subprocess with no skip guard, so a runner without
libsodium turns those jobs red. Nothing in the repository says that, and
nothing enforces it -- the ``engine-tests`` job, which is the one carrying
the security suites, would stay green entirely on its own.

This module makes the rule explicit and checkable:

* ``crypto_status()`` answers whether libsodium/Ristretto255 loads in THIS
  interpreter, carrying the loader's own diagnosis when it does not.
* ``crypto_required()`` answers whether a skip is permitted in THIS
  environment. CI requires crypto; a developer machine does not.
  ``HOLDEM_REQUIRE_CRYPTO`` overrides in either direction.
* ``enforce()`` turns the pair into one actionable failure.

Deliberately not a session-level abort. tests/test_crypto_gate.py calls
``enforce`` as an ordinary test, so a runner without libsodium reports one
named failure while every other suite still runs and reports. Aborting
collection would replace a silent gap with a blind run, which is not an
improvement.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

#: Set to 1/true/yes/on to demand crypto, or 0/false/no/off to excuse its
#: absence. Unset means "required iff this looks like CI".
REQUIRE_ENV = "HOLDEM_REQUIRE_CRYPTO"

#: GitHub Actions (and every other runner worth naming) sets this to
#: "true". It is the default signal for "this run is evidence".
CI_ENV = "CI"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class CryptoUnavailable(RuntimeError):
    """libsodium/Ristretto255 did not load somewhere it was required."""


@dataclass(frozen=True)
class CryptoStatus:
    """Whether the crypto stack loads here, plus why."""

    available: bool
    #: The libsodium version when available; the loader's diagnosis when
    #: not. Kept because "unavailable" without the reason sends the reader
    #: back to reproducing the failure by hand.
    detail: str


def flag(value) -> Optional[bool]:
    """Parse an environment flag. ``None`` means unset or unrecognised.

    Three-valued on purpose. A two-valued parse would fold "unset" into
    "false", and unset is exactly the case where the CI default has to
    decide instead.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


_status: Optional[CryptoStatus] = None


def crypto_status(*, refresh: bool = False) -> CryptoStatus:
    """Probe libsodium once per process.

    Imports the same module the guarded suites import, so the two cannot
    disagree: if ``ristretto`` imports here it imports there.

    Catches Exception rather than RuntimeError alone -- broader than the
    guards in the suites, deliberately. A guarded suite meeting a
    non-RuntimeError import failure crashes loudly, which is the correct
    outcome there; this probe only reports, so it must not crash the run
    that is trying to describe the problem.
    """
    global _status
    if _status is None or refresh:
        try:
            from holdem.p2p import ristretto as R
        except RuntimeError as exc:
            # The wrapper's own diagnosis: it names every candidate path
            # it tried and why each was rejected, which is the entire
            # lead for a machine that has the library somewhere odd.
            _status = CryptoStatus(False, str(exc))
        except Exception as exc:
            _status = CryptoStatus(False, f"{type(exc).__name__}: {exc}")
        else:
            try:
                _status = CryptoStatus(True, f"libsodium {R.libsodium_version()}")
            except Exception as exc:
                # Imported and then failed to answer. Not the same as
                # absent, and reporting it as a bare 'unavailable' sends
                # the reader off to hunt for a library that is present.
                _status = CryptoStatus(
                    False, f"loaded but unusable: {type(exc).__name__}: {exc}")
    return _status


def crypto_required(env: Optional[Mapping[str, str]] = None) -> bool:
    """Is a crypto skip forbidden in this environment?

    Explicit beats inferred: ``HOLDEM_REQUIRE_CRYPTO`` decides in either
    direction when it is set to a recognised flag. That second direction
    matters -- a CI job that genuinely cannot install libsodium needs a way
    to say so out loud, in the workflow, where a reviewer sees it, rather
    than by the suite quietly deciding for itself.
    """
    env = os.environ if env is None else env
    explicit = flag(env.get(REQUIRE_ENV))
    if explicit is not None:
        return explicit
    return flag(env.get(CI_ENV)) is True


def enforce(status: CryptoStatus, required: bool) -> None:
    """Raise if the crypto estate is missing where it was required."""
    if status.available or not required:
        return
    raise CryptoUnavailable(
        "libsodium/Ristretto255 did not load, and this environment requires "
        "it: every crypto-gated suite (Bayer-Groth prevention, DLEQ, deck "
        "audit, shuffle soundness) would skip and the run would still "
        "report green. Install a libsodium built with the ristretto255 API, "
        f"point $HOLDEM_LIBSODIUM at one, or set {REQUIRE_ENV}=0 to state "
        "in the open that this run does not cover the crypto estate. "
        f"Loader said: {status.detail}"
    )


def header_line(status: Optional[CryptoStatus] = None,
                required: Optional[bool] = None) -> str:
    """One line for the pytest run header, printed even under ``-q``.

    This is the whole point of the module for a reader of CI logs: the
    header states, on every run, whether the crypto estate is present and
    whether its absence would have been tolerated.
    """
    status = crypto_status() if status is None else status
    required = crypto_required() if required is None else required
    if status.available:
        return f"crypto: {status.detail} — crypto-gated suites RUN"
    if required:
        return (f"crypto: UNAVAILABLE and REQUIRED here — {status.detail}")
    return (f"crypto: UNAVAILABLE — crypto-gated suites SKIP, and that is "
            f"permitted here (set {REQUIRE_ENV}=1 to make it a failure). "
            f"{status.detail}")


def require_crypto() -> str:
    """Guard a test that cannot run without libsodium. Returns the detail.

    Three outcomes, never a silent pass: return the version when the stack
    is here; raise when it is absent somewhere it was required; skip with a
    stated reason when it is absent and this environment was allowed to do
    without it.

    Distinct from ``enforce``, which is the pure policy function and stays
    free of pytest so the module can be read and tested outside a run. This
    is the call site a crypto-dependent test wants: one line, and the
    developer-machine case degrades to a named skip instead of a failure.
    """
    import pytest

    status = crypto_status()
    required = crypto_required()
    if status.available:
        return status.detail
    if required:
        enforce(status, required)          # raises CryptoUnavailable
    pytest.skip(
        f"libsodium unavailable and not required here: {status.detail}")
