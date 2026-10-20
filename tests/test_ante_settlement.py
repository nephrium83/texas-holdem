"""Dead antes belong to the shared main pot, never to a private side pot.

The small, hand-worked balances exercise the same settlement used by local
games and peer replicas. Live side-pot stakes and live refunds stay separate.
"""
import random

from holdem.engine import Card, Deck, Engine, Player, settle_forfeit


def _cards(labels):
    return [Card("23456789TJQKA".index(label[0]) + 2,
                 "cdhs".index(label[1])) for label in labels.split()]


def _showdown(live, dead, *, folded=(), stacks=None, board="2c 4d 7h 9s Jc"):
    """A completed betting snapshot: seats have AA, KK, QQ in strength order."""
    players = [Player(seat, f"P{seat}", (stacks or [0] * len(live))[seat])
               for seat in range(len(live))]
    engine = Engine(players, 10, 20, rng=random.Random(7))
    engine.button = 0
    engine.board = _cards(board)
    for player, paid, ante, hole in zip(
            players, live, dead, ("Ac Ad", "Kc Kh", "Qc Qd")):
        player.in_seat = True
        player.folded = player.idx in folded
        player.total_live = paid
        player.total_dead = ante
        player.all_in = player.stack == 0
        player.hole = _cards(hole)
    return engine


def _pots(result):
    return [(pot["amount"], pot["eligible"], pot["winners"])
            for pot in result["pots"]]


def test_losing_big_blind_ante_payer_loses_all_220():
    # Heads up: 100 live each, and the BB posts a 20 ante in addition.
    players = [Player(0, "Aces", 100), Player(1, "Kings", 120)]
    engine = Engine(players, 10, 20, rng=random.Random(7), bb_ante=True)
    assert engine.start_hand()
    assert engine.bb_i == 1
    assert players[1].total_dead == 20
    engine.act(0, "raise", 100)
    engine.act(1, "call")
    engine.board = _cards("2c 4d 7h 9s Jc")
    players[0].hole = _cards("Ac Ad")
    players[1].hole = _cards("Kc Kh")

    result = engine.settle()

    assert [player.stack for player in players] == [220, 0]
    assert _pots(result) == [(220, [0, 1], [0])]
    assert result["refund"] is None


def test_ante_entirely_follows_main_winner_not_side_pot_winner():
    # AA wins 50 x 3 + the whole 20 ante; KK wins the 50 x 2 side pot.
    engine = _showdown([50, 100, 100], [0, 0, 20])
    result = engine.settle()
    assert _pots(result) == [(170, [0, 1, 2], [0]), (100, [1, 2], [1])]
    assert [player.stack for player in engine.players] == [170, 100, 0]


def test_folded_big_blinds_ante_stays_in_shared_main_pot():
    engine = _showdown([100, 100, 20], [0, 0, 20], folded=(2,))
    result = engine.settle()
    assert _pots(result) == [(240, [0, 1], [0])]
    assert [player.stack for player in engine.players] == [240, 0, 0]


def test_partial_big_blind_ante_is_shared_after_blind_first_posting():
    players = [Player(0, "Aces", 100), Player(1, "Kings", 100),
               Player(2, "Queens", 25)]
    engine = Engine(players, 10, 20, rng=random.Random(7), bb_ante=True)
    engine.button = 2  # start_hand advances to button 0, BB 2.
    assert engine.start_hand()
    assert (players[2].total_live, players[2].total_dead) == (20, 5)
    engine.act(0, "call")
    engine.act(1, "call")
    engine.board = _cards("2c 4d 7h 9s Jc")
    for player, hole in zip(players, ("Ac Ad", "Kc Kh", "Qc Qd")):
        player.hole = _cards(hole)

    result = engine.settle()
    assert _pots(result) == [(65, [0, 1, 2], [0])]
    assert [player.stack for player in players] == [145, 80, 0]


def test_uncalled_live_refund_does_not_refund_the_losing_players_ante():
    engine = _showdown([200, 100], [0, 20])
    result = engine.settle()
    assert result["refund"] == (0, 100)
    assert _pots(result) == [(220, [0, 1], [0])]
    assert [player.stack for player in engine.players] == [320, 0]


def test_zero_live_contender_can_win_dead_main_but_not_live_side_pot():
    # A live contribution of zero does not disqualify AA from the dead 3;
    # only KK/QQ matched the 10 live stakes, so KK wins that separate 20.
    engine = _showdown([0, 10, 10], [0, 0, 3])
    result = engine.settle()
    assert _pots(result) == [(3, [0, 1, 2], [0]), (20, [1, 2], [1])]
    assert [player.stack for player in engine.players] == [3, 20, 0]


def test_dead_only_main_pot_is_paid_even_without_live_contributions():
    engine = _showdown([0, 0, 0], [0, 0, 21], folded=(2,))
    result = engine.settle()
    assert _pots(result) == [(21, [0, 1], [0])]
    assert [player.stack for player in engine.players] == [21, 0, 0]


def test_dead_main_tie_keeps_existing_odd_chip_order():
    engine = _showdown([0, 0, 0], [0, 0, 21], folded=(2,),
                       board="Ts Js Qs Ks As")
    result = engine.settle()
    assert _pots(result) == [(21, [0, 1], [0, 1])]
    assert result["pots"][0]["runs"][0]["payouts"] == {0: 10, 1: 11}
    assert [player.stack for player in engine.players] == [10, 11, 0]


def test_ante_main_pot_splits_over_two_runs_with_odd_chip_to_first_run():
    engine = _showdown([10, 10], [0, 3], board="2c 4d 7h")
    # First board leaves AA ahead; second makes KK three of a kind.
    tail = _cards("9s Jc Kd Qh")
    engine.deck = Deck.from_indices([(card.v - 2) * 4 + card.s for card in tail])
    result = engine.settle(runs=2)
    assert _pots(result) == [(23, [0, 1], [0, 1])]
    assert [(run["amount"], run["winners"], run["payouts"])
            for run in result["pots"][0]["runs"]] == [
                (12, [0], {0: 12}), (11, [1], {1: 11})]
    assert [player.stack for player in engine.players] == [12, 11]


def test_forfeit_equal_live_stakes_share_entire_dead_ante():
    result = settle_forfeit(stacks=[0, 0, 0], live=[100, 100, 100],
                            dead=[0, 20, 0], in_hand=[0, 1, 2], dropped=2,
                            button=0)
    assert result["stacks"] == [160, 160, 0]
    assert result["pots"] == [dict(amount=320, eligible=[0, 1],
                                   payouts={0: 160, 1: 160})]
    assert result["refund"] is None


def test_forfeit_multiple_live_layers_keep_whole_ante_in_main_pot():
    # Dropper's unmatched live 50 joins the top live side pot, but its
    # 20 ante stays in the shared main: 50 x 3 + 20, then 50 x 2 + 50.
    result = settle_forfeit(stacks=[0, 0, 0], live=[50, 100, 150],
                            dead=[0, 0, 20], in_hand=[0, 1, 2], dropped=2,
                            button=0)
    assert result["stacks"] == [85, 235, 0]
    assert result["pots"] == [
        dict(amount=170, eligible=[0, 1], payouts={0: 85, 1: 85}),
        dict(amount=150, eligible=[1], payouts={1: 150}),
    ]
    assert result["refund"] is None


def test_forfeit_dead_only_pot_includes_zero_live_survivors_and_odd_chip():
    result = settle_forfeit(stacks=[0, 0, 0], live=[0, 0, 0],
                            dead=[0, 0, 21], in_hand=[0, 1, 2], dropped=2,
                            button=0)
    assert result["stacks"] == [10, 11, 0]
    assert result["pots"] == [dict(amount=21, eligible=[0, 1],
                                   payouts={0: 10, 1: 11})]


def test_forfeit_zero_live_survivor_shares_ante_but_not_live_side():
    result = settle_forfeit(stacks=[0, 0, 0], live=[0, 10, 10],
                            dead=[0, 0, 4], in_hand=[0, 1, 2], dropped=2,
                            button=0)
    assert result["stacks"] == [2, 22, 0]
    assert result["pots"] == [
        dict(amount=4, eligible=[0, 1], payouts={0: 2, 1: 2}),
        dict(amount=20, eligible=[1], payouts={1: 20}),
    ]
