"""A wire-mode joiner cannot be built without its invite's host pin (audit #72)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import AUTHOR_MODE_COMPAT, AUTHOR_MODE_WIRE, Session
from holdem.p2p.timeout import FakeClock
from tests import joiner_helpers as JH

HOST = bytes.fromhex("aa" * 32)
OTHER = bytes.fromhex("bb" * 32)


class _Wire:
    delivers_verified_envelopes = True

    def broadcast(self, msg):
        pass

    def broadcast_except(self, exclude, msg):
        pass

    def send(self, to, msg):
        pass


def _joiner(**kw):
    return Session(is_host=False, nickname="J", avatar_b64="", clock=FakeClock(),
                   master_secret=bytes(range(32)), **kw)


def test_a_wire_joiner_without_a_pin_is_refused():
    """It used to start out host-authenticated with nothing pinned: any
    endpoint able to sign an envelope could act as the host to it."""
    with pytest.raises(ValueError, match="joiner on a verified-envelope transport"):
        _joiner(transport=_Wire())


def test_an_explicit_wire_mode_joiner_without_a_pin_is_refused():
    bus = InMemoryBus()
    with pytest.raises(ValueError, match="pinned"):
        _joiner(transport=InMemoryTransport(bus, "j"), author_mode=AUTHOR_MODE_WIRE)


def test_a_compat_joiner_still_needs_no_pin():
    s = _joiner(transport=InMemoryTransport(InMemoryBus(), "j"))
    assert s.author_mode == AUTHOR_MODE_COMPAT
    assert s._host_authenticated


def test_a_pinned_wire_joiner_starts_unauthenticated():
    s = _joiner(transport=_Wire(), joiner_admission=JH.joiner_admission(HOST, OTHER))
    assert s.author_mode == AUTHOR_MODE_WIRE
    assert not s._host_authenticated
    assert s._pinned_host_pubkey is None


def test_only_the_pinned_key_authenticates_the_host():
    s = _joiner(transport=_Wire(), joiner_admission=JH.joiner_admission(HOST, OTHER))
    assert s.mark_host_authenticated("impostor", OTHER) is False
    assert not s._host_authenticated
    assert s.mark_host_authenticated("host", HOST) is True
    assert s._host_authenticated
    assert s._pinned_host_pubkey == HOST.hex()
    assert s._host_conn_id == "host"


def test_the_host_still_requires_its_own_admission_policy():
    with pytest.raises(ValueError, match="host on a verified-envelope transport"):
        Session(is_host=True, nickname="H", avatar_b64="", transport=_Wire(),
                clock=FakeClock(), master_secret=bytes(range(32)))
