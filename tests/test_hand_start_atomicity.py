"""Audit finding 73: failed local preparation cannot replace a live hand."""
from types import SimpleNamespace

import pytest

from crypto_gate import require_crypto
from holdem.p2p.inmemory_transport import InMemoryBus, InMemoryTransport
from holdem.p2p.session import Session
from holdem.p2p.timeout import FakeClock


def session():
    require_crypto()
    s = Session(is_host=True, nickname='Host', avatar_b64='',
                transport=InMemoryTransport(InMemoryBus(), 'peer0'),
                clock=FakeClock(), master_secret=b'a' * 32)
    s.local_conn_id = 'peer0'
    s.configure_seats(['peer0', 'peer1'])
    s._adopt_deal_policy(Session.DEAL_POLICY_DETECTION)
    s._adopt_continuation_policy(Session.CONTINUATION_END_TABLE)
    s.state = 'PLAYING'
    s._hand_no = 7
    s._deal_hole = ['old hole']
    s._deal_board = ['old board']
    s._deal_outbox = [{'old': 'message'}]
    s._deal_driver = SimpleNamespace(deal=SimpleNamespace(_held={}))
    s._replica = SimpleNamespace(stacks=[100, 100])
    s.hand_result = {'previous': 'settled'}
    s._hand_stacks = [95, 105]
    s._hand_positions = (1, 0, 0)
    s._table_cfg = {'previous': 'configuration'}
    return s


FIELDS = ('_deal_driver', '_deal_hole', '_deal_board', '_deal_outbox',
          '_hand_no', '_replica', 'hand_result', '_hand_stacks',
          '_hand_positions', '_table_cfg', '_hand_record')


def start(s, continuous):
    if continuous:
        s.start_p2p_hand(hand_no=8, names=['Host', 'Guest'], stacks=[100, 100],
                         sb=5, bb=10)
    else:
        s.begin_hand(8)


@pytest.mark.parametrize('continuous', [False, True])
@pytest.mark.parametrize('failure', ['provider', 'constructor', 'start'])
def test_local_failure_preserves_previous_hand_and_balances(monkeypatch, continuous, failure):
    s = session()
    before = {key: getattr(s, key) for key in FIELDS}
    from holdem.p2p import mental_deal_driver

    class Driver:
        def __init__(self, **kwargs):
            if failure == 'constructor':
                raise RuntimeError('constructor failed')
            self.send = kwargs['send']

        def start(self):
            self.send({'new': 'private emission'})
            raise RuntimeError('start failed')

    def provider():
        raise RuntimeError('provider failed')

    if failure == 'provider':
        s._master_secret_override = None
        s._secret_provider = provider
    monkeypatch.setattr(mental_deal_driver, 'MentalDealDriver', Driver)
    with pytest.raises(RuntimeError, match=failure + ' failed'):
        start(s, continuous)
    for key, value in before.items():
        assert getattr(s, key) is value, key
    assert s.last_settled_stacks == [100, 100]
    assert s.terminal_state is None


@pytest.mark.parametrize('failure', ['_flush_deal', '_replay_buffer'])
def test_post_publication_failure_is_terminal_without_restoring_old_driver(monkeypatch, failure):
    s = session()
    old_driver = s._deal_driver
    from holdem.p2p import mental_deal_driver

    class Driver:
        def __init__(self, **kwargs):
            self.deal = SimpleNamespace(_held={})

        def start(self):
            pass

    def fail():
        raise OSError('delivery uncertain')

    monkeypatch.setattr(mental_deal_driver, 'MentalDealDriver', Driver)
    monkeypatch.setattr(s, '_flush_deal', lambda: None)
    monkeypatch.setattr(s, '_replay_buffer', lambda: None)
    monkeypatch.setattr(s, failure, fail)
    with pytest.raises(OSError, match='delivery uncertain'):
        start(s, continuous=True)
    assert s.terminal_state == Session.ABORTED_PROTOCOL
    assert s._deal_driver is not old_driver
    assert s._hand_no == 8
    assert s.hand_result is None
    assert s.last_settled_stacks == [100, 100]
    assert sum(s.replica.stacks) + s.replica.engine.pot == 200
    assert s.send_bet_action('call') != 'applied'
