"""Continuation is an explicit immutable table rule and a deal commitment."""
import struct

import pytest

from crypto_gate import require_crypto
from holdem import client_view
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Session
from holdem.p2p.timeout import FakeClock


def peer(bus=None, seat=0):
    s = Session(is_host=(seat == 0), nickname=f'P{seat}', avatar_b64='',
                transport=InMemoryTransport(bus or InMemoryBus(), f'peer{seat}'),
                clock=FakeClock(), master_secret=bytes([seat + 1]) * 32)
    s.local_conn_id = f'peer{seat}'
    s._host_conn_id = 'peer0'
    return s


@pytest.mark.parametrize('value', [None, True, 1, [], {}, '', 'survivors', 'future-v9'])
def test_missing_or_unsupported_policy_refuses_table_before_adoption(value):
    s = peer(seat=1)
    settings = {'deal_policy': Session.DEAL_POLICY_DETECTION}
    if value is not None:
        settings['continuation_policy'] = value
    s.handle_message('peer0', {'type': 'game_start', 'payload': {
        'seat_order': ['peer0', 'peer1'], 'table_settings': settings}})
    assert s.terminal_state == Session.POLICY_REFUSED
    assert s.continuation_policy is None and s.deal_policy is None
    assert s.terminal_record.previous_state == 'LOBBY'
    assert s.replica is None


def test_adoption_is_idempotent_and_cannot_change_membership_behavior():
    s = peer()
    assert s._adopt_continuation_policy(Session.CONTINUATION_SURVIVORS)
    continuation = s._casual_continuation
    assert s._adopt_continuation_policy(Session.CONTINUATION_SURVIVORS)
    assert s._casual_continuation is continuation
    assert not s._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
    assert s._casual_reliability
    with pytest.raises(ValueError, match='continuation policy'):
        s._adopt_continuation_policy('unknown')


def test_context_and_hand_refuse_missing_policy_before_secret_or_blinds():
    s = peer()
    s.configure_seats(['peer0', 'peer1'])
    s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
    s._master_secret_override = None
    def forbidden():
        pytest.fail('Missing policy reached the secret provider')
    s._secret_provider = forbidden
    with pytest.raises(RuntimeError, match='continuation policy'):
        s._deal_context_bytes()
    with pytest.raises(RuntimeError, match='continuation policy'):
        s.start_p2p_hand(hand_no=1, names=['P0', 'P1'], stacks=[100, 100], sb=5, bb=10)
    assert s.replica is None and s._deal_driver is None and s._hand_no == 0


def test_policy_is_a_length_prefixed_field_in_version_four_context():
    contexts = []
    for policy in (Session.CONTINUATION_END_TABLE, Session.CONTINUATION_SURVIVORS):
        s = peer()
        s.configure_seats(['peer0', 'peer1'])
        s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
        s._adopt_continuation_policy(policy)
        raw = s._deal_context_bytes()
        offset = 4 + struct.unpack_from('>I', raw, 0)[0]
        assert struct.unpack_from('>I', raw, offset)[0] == 4
        offset += 4
        offset += 4 + struct.unpack_from('>I', raw, offset)[0]  # deal policy
        size = struct.unpack_from('>I', raw, offset)[0]
        assert raw[offset + 4:offset + 4 + size].decode('utf-8') == policy
        contexts.append(s._deal_session_id())
    assert contexts[0] != contexts[1]


def test_game_start_adopts_same_policy_without_a_new_exchange():
    from holdem.p2p.session import Player
    bus = InMemoryBus()
    sessions = [peer(bus, seat) for seat in range(2)]
    for seat, s in enumerate(sessions):
        bus.register(f'peer{seat}', s)
    host = sessions[0]
    for seat in range(2):
        cid = f'peer{seat}'
        host.players[cid] = Player(conn_id=cid, peer_id=cid, nickname=cid, avatar_b64='')
    settings = {'deal_policy': Session.DEAL_POLICY_DETECTION,
                'continuation_policy': Session.CONTINUATION_SURVIVORS}
    host.start_game(settings)
    assert bus.drain() == 1  # the existing game_start; no acceptance exchange
    for s in sessions:
        assert s.continuation_policy == Session.CONTINUATION_SURVIVORS
        assert s._casual_continuation is not None
        assert s.state == 'PLAYING'


def test_mismatched_policy_cannot_finish_deal_or_keep_posted_blinds():
    require_crypto()
    bus = InMemoryBus()
    sessions = [peer(bus, seat) for seat in range(2)]
    for seat, s in enumerate(sessions):
        bus.register(f'peer{seat}', s)
        s.configure_seats(['peer0', 'peer1'])
        s._adopt_deal_policy(Session.DEAL_POLICY_BG)
        s._adopt_continuation_policy(
            Session.CONTINUATION_SURVIVORS if seat == 0 else Session.CONTINUATION_END_TABLE)
        s.state = 'PLAYING'
        s.start_p2p_hand(hand_no=1, names=['P0', 'P1'], stacks=[100, 100], sb=5, bb=10)
        assert s.replica.engine.pot == 15
    bus.drain()
    for s in sessions:
        assert s.hand_voided
        assert s.hand_result is None
        assert s.last_settled_stacks == [100, 100]
        view = client_view.snapshot(s)
        assert view['pot'] == 0
        assert [seat['stack'] for seat in view['seats']] == [100, 100]
        assert s._deal_driver.aborted()
