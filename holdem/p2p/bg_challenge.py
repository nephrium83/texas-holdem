"""Uniform nonzero Fiat-Shamir challenge derivation for the Bayer-Groth stack.

Every Bayer-Groth sub-argument needs challenges that are nonzero: a zero
challenge collapses the batching polynomial and the verification equation
stops constraining the witness. Each module used to handle that by
reducing its transcript digest and raising if the result was zero:

    x = R.scalar_reduce(h.digest())
    if R.is_zero_scalar(x):
        raise ValueError("Fiat-Shamir challenge reduced to zero")

That fires with probability around 2^-252, so it has never been observed
and never will be. It is still the wrong shape. Rejecting is not a
derivation rule. Under Fiat-Shamir the transcript is not the prover's to
choose -- it is fixed by the statement and the initial message -- so a
prover whose transcript happens to hash to zero has no remedy. There is
nothing to resample. An honest proof would simply be unprovable, and the
verifier, hashing the same bytes, would reach the same dead end. The
failure is unreachable but the semantics are wrong, and three modules had
independently grown three different spellings of it.

This module replaces all of them with a derivation that always succeeds:
append a counter to the transcript and advance it until the reduction is
nonzero.

Design points
-------------
Injectivity comes from the *fixed-width trailing counter*. Two
(label, counter) pairs produce the same appended bytes only if the
counters are equal -- they occupy the last four bytes either way -- and
therefore only if the labels are equal too. The length prefix on the
label is defensive rather than load-bearing: the injectivity argument
above does not need it, but it survives a future change to the counter
width, which that argument would not. Stating this accurately matters,
because an overclaimed security property is worse than none.

Each retry hashes a *different preimage* -- the counter is part of what
gets hashed, not applied to the output. Chaining digests (hashing the
previous digest again) would also terminate, but it is a different
construction with a different security argument, and mixing the two
across modules is exactly the kind of drift this module exists to stop.

The helper *copies* the hash object rather than consuming it. A caller
that needs several challenges builds one transcript and draws each with
its own label, so a prover grinding for a favourable second challenge
necessarily regrinds the first: both move together with any change to
the transcript. Each label also gets its own independent counter
sequence, so a retry on one challenge cannot shift another.

Callers must not derive one digest and split it into several challenges.
Pass the hash object here, once per label.
"""

from __future__ import annotations

import hashlib

from holdem.p2p import ristretto as R
from holdem.p2p.ristretto import Scalar

__all__ = ["MAX_CHALLENGE_ATTEMPTS", "nonzero_challenge"]

# Each attempt fails with probability ~2^-252, so one is essentially
# always enough. The bound exists so a broken reduction cannot spin
# forever; it is not a tuning parameter.
MAX_CHALLENGE_ATTEMPTS = 256


def nonzero_challenge(h: "hashlib._Hash", label: bytes = b"") -> Scalar:
    """Draw a nonzero scalar challenge from the transcript in ``h``.

    ``h`` is the transcript hash object, absorbed but not yet finalized.
    It is copied, never mutated, so the same object can be reused for
    other labels.

    ``label`` separates challenges drawn from the same transcript. Two
    different labels give two independent challenge streams, each with
    its own counter.
    """
    for counter in range(MAX_CHALLENGE_ATTEMPTS):
        attempt = h.copy()
        attempt.update(len(label).to_bytes(4, "big"))
        attempt.update(label)
        attempt.update(counter.to_bytes(4, "big"))
        candidate = R.scalar_reduce(attempt.digest())
        if not R.is_zero_scalar(candidate):
            return candidate
    raise RuntimeError(
        "nonzero challenge derivation exhausted after "
        f"{MAX_CHALLENGE_ATTEMPTS} attempts; the scalar reduction is broken"
    )
