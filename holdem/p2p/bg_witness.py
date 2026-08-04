"""The prover-side witness check seam for the Bayer-Groth arguments.

Every Bayer-Groth prover validates its own witness before building a proof
-- that prod(a) really is b, that the diagonal really is zero, and so on.
Those checks catch honest bugs early and give callers a clear error, but
they are NOT what makes the system secure. A real attacker deletes them
first. Soundness is entirely the verifier's job.

Routing the checks through one function makes that boundary explicit and
gives the test suite a supported way to become a cheating prover: patch
this out on a module and confirm the VERIFIER still rejects the forgery.
See tests/test_bg_soundness.py.

This distinction is not academic here. The shuffle argument shipped with a
verifier that read its multi-exponentiation statement out of the proof
instead of recomputing it from the public input deck, so a shuffler could
swap the whole deck. Every test passed, because the suite only ever
exercised provers that checked themselves.
"""
from __future__ import annotations


def require_witness(ok: bool, message: str) -> None:
    """Raise ``ValueError(message)`` unless the prover's witness holds.

    Never call this from a verifier: a verifier must return False rather
    than raise, and must decide for itself rather than trust a prover.
    """
    if not ok:
        raise ValueError(message)


__all__ = ["require_witness"]
