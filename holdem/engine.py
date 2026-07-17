"""Texas Hold'em engine: cards, fast evaluator, betting rounds, side pots, AI.

Deliberately free of any tkinter import so it can be unit-tested headlessly.
"""
from __future__ import annotations

import math
import random

# ------------------------------------------------------------------ cards

SUIT_GLYPHS = ("\u2663", "\u2666", "\u2665", "\u2660")   # club diamond heart spade
SUIT_IS_RED = (False, True, True, False)
RANK_STR = {2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9",
            10: "10", 11: "J", 12: "Q", 13: "K", 14: "A"}


class Card:
    __slots__ = ("v", "s")

    def __init__(self, v: int, s: int):
        self.v = v          # 2..14
        self.s = s          # 0..3

    @property
    def rank(self) -> str:
        return RANK_STR[self.v]

    @property
    def suit(self) -> str:
        return SUIT_GLYPHS[self.s]

    @property
    def red(self) -> bool:
        return SUIT_IS_RED[self.s]

    def __str__(self):
        return RANK_STR[self.v] + SUIT_GLYPHS[self.s]

    __repr__ = __str__


FULL_DECK = [Card(v, s) for v in range(2, 15) for s in range(4)]


class Deck:
    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random
        self.cards = list(FULL_DECK)
        self.rng.shuffle(self.cards)

    @classmethod
    def from_indices(cls, indices: list) -> "Deck":
        """Create a pre-ordered Deck from a list of 52 card indices.

        ``indices[0]`` is dealt first (maps to ``FULL_DECK[indices[0]]``).
        Used by the verifiable-shuffle protocol to inject the deterministic
        deck order agreed upon by all players.
        """
        obj = cls.__new__(cls)
        obj.rng = None
        # deal() pops from the end, so reverse to serve index 0 first
        obj.cards = [FULL_DECK[i] for i in reversed(indices)]
        return obj

    def deal(self, n: int = 1):
        out = self.cards[-n:]
        del self.cards[-n:]
        return out


# -------------------------------------------------------------- evaluator
#
# evaluate(cards) -> (category, tiebreakers)
# Bigger tuple compares as the better hand. Handles 5, 6 or 7 cards.
# Straight/flush detection is done with bitmasks and rank counts, which is
# roughly 30x faster than iterating all C(7,5)=21 subsets.

HAND_NAMES = {
    8: "Straight Flush", 7: "Four of a Kind", 6: "Full House", 5: "Flush",
    4: "Straight", 3: "Three of a Kind", 2: "Two Pair", 1: "One Pair",
    0: "High Card",
}

# Precomputed: for every 15-bit rank mask, the high card of the best straight
# (0 if none). Built once at import; makes straight detection a dict lookup.
_STRAIGHT_HIGH = {}


def _build_straight_table():
    for mask in range(1 << 15):
        m = mask
        if m & (1 << 14):          # ace plays low as well
            m |= 1 << 1
        run = 0
        high = 0
        for r in range(14, 0, -1):
            if m & (1 << r):
                run += 1
                if run == 5:
                    high = r + 4
                    break
            else:
                run = 0
        if high:
            _STRAIGHT_HIGH[mask] = high


_build_straight_table()


def evaluate(cards) -> tuple:
    """Best 5-card score out of `cards`. Returns (category, tiebreakers)."""
    suit_count = [0, 0, 0, 0]
    counts = [0] * 15
    mask = 0
    for c in cards:
        suit_count[c.s] += 1
        counts[c.v] += 1
        mask |= 1 << c.v

    # --- flush family (a flush rules out quads and full houses in <=7 cards)
    fs = -1
    for s in range(4):
        if suit_count[s] >= 5:
            fs = s
            break
    if fs >= 0:
        fmask = 0
        fvals = []
        for c in cards:
            if c.s == fs:
                fmask |= 1 << c.v
                fvals.append(c.v)
        sf = _STRAIGHT_HIGH.get(fmask, 0)
        if sf:
            return (8, (sf,))
        fvals.sort(reverse=True)
        return (5, tuple(fvals[:5]))

    quads = trips = 0
    pairs = []
    for v in range(14, 1, -1):
        c = counts[v]
        if c == 4 and not quads:
            quads = v
        elif c == 3:
            if not trips:
                trips = v
            else:
                pairs.append(v)      # second trips plays as a pair
        elif c == 2:
            pairs.append(v)

    if quads:
        kick = max(v for v in range(14, 1, -1) if counts[v] and v != quads)
        return (7, (quads, kick))

    if trips and pairs:
        return (6, (trips, pairs[0]))

    st = _STRAIGHT_HIGH.get(mask, 0)
    if st:
        return (4, (st,))

    if trips:
        ks = [v for v in range(14, 1, -1) if counts[v] and v != trips][:2]
        return (3, (trips, ks[0], ks[1]))

    if len(pairs) >= 2:
        p1, p2 = pairs[0], pairs[1]
        kick = max(v for v in range(14, 1, -1)
                   if counts[v] and v != p1 and v != p2)
        return (2, (p1, p2, kick))

    if pairs:
        p = pairs[0]
        ks = [v for v in range(14, 1, -1) if counts[v] and v != p][:3]
        return (1, (p, ks[0], ks[1], ks[2]))

    top = [v for v in range(14, 1, -1) if counts[v]][:5]
    return (0, tuple(top))


def hand_name(score) -> str:
    cat, tb = score
    if cat == 8 and tb[0] == 14:
        return "Royal Flush"
    return HAND_NAMES[cat]


def best_five(cards):
    """The actual 5 cards making the best hand (used to highlight a winner)."""
    from itertools import combinations
    best = None
    best_combo = None
    for combo in combinations(cards, 5):
        sc = evaluate(combo)
        if best is None or sc > best:
            best = sc
            best_combo = combo
    return list(best_combo)


# ----------------------------------------------------------------- equity

def equity(hole, board, n_opp, sims, rng):
    """Monte Carlo. Returns (win_pct, tie_pct, equity) as 0..1 floats."""
    if n_opp <= 0:
        return (1.0, 0.0, 1.0)
    known = {(c.v, c.s) for c in hole}
    known.update((c.v, c.s) for c in board)
    remaining = [c for c in FULL_DECK if (c.v, c.s) not in known]

    need_board = 5 - len(board)
    need = need_board + 2 * n_opp
    if need > len(remaining):
        return None

    sample = rng.sample
    ev = evaluate
    wins = ties = 0

    for _ in range(sims):
        draw = sample(remaining, need)
        full_board = board + draw[:need_board] if need_board else board
        me = ev(hole + full_board)
        k = need_board
        best = None
        for _o in range(n_opp):
            sc = ev((draw[k], draw[k + 1]) + tuple(full_board))
            k += 2
            if best is None or sc > best:
                best = sc
        if me > best:
            wins += 1
        elif me == best:
            ties += 1

    return (wins / sims, ties / sims, (wins + ties * 0.5) / sims)


def chen(hole) -> float:
    """Chen formula preflop strength. AA=20, 72o=-1."""
    a, b = sorted((c.v for c in hole), reverse=True)

    def hv(v):
        if v == 14:
            return 10.0
        if v == 13:
            return 8.0
        if v == 12:
            return 7.0
        if v == 11:
            return 6.0
        return v / 2.0

    s = hv(a)
    if a == b:
        s = max(s * 2.0, 5.0)
    if hole[0].s == hole[1].s:
        s += 2.0
    if a != b:
        gap = a - b - 1
        if gap == 1:
            s -= 1
        elif gap == 2:
            s -= 2
        elif gap == 3:
            s -= 4
        elif gap >= 4:
            s -= 5
        if gap <= 1 and a < 12:
            s += 1
    return math.ceil(s)


# ----------------------------------------------------------------- player

STYLES = {
    #            open  call  3bet   agg   bluff  cbet
    "Nit":    dict(open=11, call=9,  three=15, agg=0.30, bluff=0.04, cbet=0.45),
    "Solid":  dict(open=9,  call=7,  three=12, agg=0.55, bluff=0.12, cbet=0.62),
    "Loose":  dict(open=6,  call=5,  three=10, agg=0.50, bluff=0.20, cbet=0.58),
    "Maniac": dict(open=4,  call=3,  three=8,  agg=0.85, bluff=0.42, cbet=0.85),
    "Hero":   dict(open=9,  call=7,  three=12, agg=0.55, bluff=0.12, cbet=0.62),
}
AI_STYLES = ("Nit", "Solid", "Loose", "Maniac")


class Player:
    def __init__(self, idx, name, stack, style="Solid", level=2, human=False):
        self.idx = idx
        self.name = name
        self.stack = stack
        self.style = style
        self.level = level
        self.human = human
        self.hole = []
        self.folded = False
        self.all_in = False
        self.in_seat = True       # dealt into this hand
        self.bet = 0              # live chips in front this street
        self.total_live = 0       # live chips committed this hand
        self.total_dead = 0       # dead money this hand (antes, dead blinds)
        self.last_action = ""
        self.won = 0
        # table lifecycle
        self.sitting_out = False
        self.owes_bb = False      # missed a big blind while sitting out
        self.owes_sb = False      # missed a small blind while sitting out
        self.wait_for_bb = False  # entering/returning: wait for natural BB
        self.post_entry = False   # entering/returning: post now instead

    @property
    def total(self):
        """Everything this player has put in the pot this hand."""
        return self.total_live + self.total_dead


# ----------------------------------------------------------------- engine

class Engine:
    """Pure game state. The GUI polls `actor`, calls `act()`, then loops."""

    def __init__(self, players, sb=10, bb=20, structure="No-Limit",
                 rng: random.Random | None = None, bb_ante=False,
                 deal_sitting_out=False):
        self.players = players
        self.rng = rng or random.Random()
        self.sb = sb
        self.bb = bb
        self.structure = structure
        self.bb_ante = bb_ante                  # BB posts a bb-sized ante
        self.deal_sitting_out = deal_sitting_out  # tournaments deal them in
        self.button = len(players) - 1          # seat index; may be vacated
        self.bb_seat = None                     # forward-moving anchor
        self.sb_seat = None
        self.sb_i = None                        # posting occupant or None
        self.bb_i = None
        self.deck = None
        self.board = []
        self.board2 = None                      # second run-it-twice board
        self.street = "idle"
        self.current_bet = 0
        self.min_raise = bb
        self.need_to_act = set()
        self.actor = None
        self.hand_no = 0
        self.raises_this_street = 0
        self.no_raise = set()     # seats facing an under-raise: call/fold only
        self.river_aggr = None    # last bettor/raiser on the river
        self.straddler = None
        self.events = []          # (kind, text) for the log

    # -- helpers -----------------------------------------------------------

    @property
    def pot(self):
        return sum(p.total for p in self.players)

    def seated(self):
        return [p for p in self.players if p.in_seat]

    def contested(self):
        return [p for p in self.players if p.in_seat and not p.folded]

    def can_act(self, p):
        return p.in_seat and not p.folded and not p.all_in and p.stack > 0

    def betting_locked(self):
        """No further betting possible but the hand is still contested."""
        alive = self.contested()
        movers = [p for p in alive if not p.all_in and p.stack > 0]
        return len(alive) > 1 and len(movers) <= 1 and self.actor is None

    def _next(self, i, pred):
        n = len(self.players)
        for k in range(1, n + 1):
            j = (i + k) % n
            if pred(self.players[j]):
                return j
        return None

    def _seek(self, start):
        n = len(self.players)
        for k in range(n):
            j = (start + k) % n
            if j in self.need_to_act:
                return j
        return None

    def emit(self, kind, text):
        self.events.append((kind, text))

    def drain(self):
        out = self.events
        self.events = []
        return out

    # -- table lifecycle ---------------------------------------------------

    def add_chips(self, i, amount):
        """Buy-in top-up. Only between hands."""
        if self.street != "idle" or amount <= 0:
            return False
        self.players[i].stack += int(amount)
        return True

    def sit_out(self, i):
        self.players[i].sitting_out = True

    def sit_in(self, i, post_now=False):
        p = self.players[i]
        p.sitting_out = False
        if p.owes_bb or p.owes_sb:
            if post_now:
                p.post_entry = True
            else:
                p.wait_for_bb = True

    # -- hand setup --------------------------------------------------------

    def _has_chips(self, p):
        return p.stack > 0

    def _blind_eligible(self, p):
        """May the BB anchor land on this seat?"""
        if p.stack <= 0:
            return False
        if p.sitting_out and not self.deal_sitting_out:
            return False
        return True

    def _dealt(self, p):
        """Is this player dealt into the coming hand?"""
        if p.stack <= 0:
            return False
        if p.sitting_out and not self.deal_sitting_out:
            return False
        if p.wait_for_bb and not p.post_entry:
            return False               # cleared when the BB reaches them
        return True

    def _advance_positions(self):
        """Dead-button rule: the BB moves forward exactly one eligible seat;
        the SB and button trail it and may land on vacated seats."""
        n = len(self.players)

        if self.bb_seat is None:                       # first hand
            self.button = self._next(self.button, self._dealt)
            dealt = [p for p in self.players if self._dealt(p)]
            if len(dealt) == 2:
                self.sb_seat = self.button
                self.bb_seat = self._next(self.button, self._dealt)
            else:
                self.sb_seat = self._next(self.button, self._dealt)
                self.bb_seat = self._next(self.sb_seat, self._dealt)
            return

        prev_bb, prev_sb = self.bb_seat, self.sb_seat

        # scan forward from the old BB; skipped live-but-out seats owe blinds
        new_bb = None
        for k in range(1, n + 1):
            s = (prev_bb + k) % n
            q = self.players[s]
            if self._blind_eligible(q):
                new_bb = s
                break
            if q.stack > 0 and q.sitting_out:
                q.owes_bb = True

        self.bb_seat = new_bb
        q = self.players[new_bb]
        q.wait_for_bb = False                          # their BB has arrived
        q.owes_bb = q.owes_sb = False

        dealt = [p for p in self.players if self._dealt(p)]
        if len(dealt) == 2:                            # heads-up override
            other = next(p for p in dealt if p.idx != new_bb)
            self.sb_seat = other.idx
            self.button = other.idx
        else:
            self.sb_seat = prev_bb                     # chain: BB -> SB -> BTN
            self.button = prev_sb if prev_sb is not None else self.button
            sbp = self.players[self.sb_seat]
            if sbp.stack > 0 and sbp.sitting_out and not self.deal_sitting_out:
                sbp.owes_sb = True                     # dead small blind

    def start_hand(self, straddle_fn=None, deck=None):
        """Begin a new hand.

        Parameters
        ----------
        straddle_fn:
            Optional callable(utg_seat) -> bool; controls UTG straddle.
        deck:
            Optional pre-built :class:`Deck` instance (e.g. from
            ``Deck.from_indices(shuffled_indices)`` in the verifiable-shuffle
            protocol).  When *None* a freshly shuffled deck is generated from
            ``self.rng``.
        """
        self._advance_positions()

        for p in self.players:
            p.in_seat = self._dealt(p)
            p.hole = []
            p.folded = not p.in_seat
            p.all_in = False
            p.bet = 0
            p.total_live = 0
            p.total_dead = 0
            p.last_action = ""
            p.won = 0

        live = self.seated()
        if len(live) < 2:
            return False

        self.deck = deck if deck is not None else Deck(self.rng)
        self.board = []
        self.board2 = None
        self.street = "preflop"
        self.current_bet = 0
        self.min_raise = self.bb
        self.raises_this_street = 0
        self.river_aggr = None
        self.straddler = None
        self.hand_no += 1

        self.emit("hand", f"--- Hand #{self.hand_no}  ({self.sb}/{self.bb}"
                          + (" +ante" if self.bb_ante else "") + ") ---")

        # blinds
        self.sb_i = None
        sbp = self.players[self.sb_seat]
        if sbp.in_seat:
            self._post(self.sb_seat, self.sb, "SB")
            self.sb_i = self.sb_seat
        else:
            self.emit("blind", "small blind is dead")
        self.bb_i = self.bb_seat
        self._post(self.bb_seat, self.bb, "BB")

        # big blind ante (posted after the blind, TDA order)
        if self.bb_ante:
            self._post_dead(self.bb_seat, self.bb, "ante")

        # entry/return posts from any position
        for p in live:
            if p.post_entry and p.idx not in (self.sb_seat, self.bb_seat):
                self._post(p.idx, self.bb, "post")
                if p.owes_sb:
                    self._post_dead(p.idx, self.sb, "dead SB")
            p.post_entry = False
            p.owes_bb = p.owes_sb = False
            p.wait_for_bb = False

        self.current_bet = max(p.bet for p in self.players)

        # optional UTG straddle (3+ handed, big-bet games only)
        first = None
        if (straddle_fn is not None and len(live) >= 3
                and self.structure != "Fixed-Limit"):
            utg = self._next(self.bb_seat, lambda q: q.in_seat)
            u = self.players[utg]
            if u.stack > 0 and not u.all_in and straddle_fn(utg):
                self._post(utg, 2 * self.bb, "STR")
                self.straddler = utg
                self.current_bet = max(self.current_bet, u.bet)
                self.min_raise = max(self.min_raise, 2 * self.bb)
                self.emit("blind", f"{u.name} straddles to {u.bet}")
                first = self._next(utg, lambda q: q.in_seat)

        # deal, starting left of the button seat
        order = []
        j = self.button
        for _ in range(len(self.players)):
            j = (j + 1) % len(self.players)
            if self.players[j].in_seat:
                order.append(j)
        for _ in range(2):
            for i in order:
                self.players[i].hole += self.deck.deal(1)

        if first is None:
            if len(live) == 2:
                first = self.button        # HU: button/SB acts first preflop
            else:
                first = self._next(self.bb_seat, lambda q: q.in_seat)
        self._open_round(first)
        return True

    def _post(self, i, amount, label):
        p = self.players[i]
        amt = min(amount, p.stack)
        self._commit(p, amt)
        p.last_action = f"{label} {amt}"
        self.emit("blind", f"{p.name} posts {label} {amt}"
                           + (" (all-in)" if p.all_in else ""))

    def _post_dead(self, i, amount, label):
        p = self.players[i]
        amt = min(amount, p.stack)
        p.stack -= amt
        p.total_dead += amt
        if p.stack == 0:
            p.all_in = True
        self.emit("blind", f"{p.name} posts {label} {amt}"
                           + (" (all-in)" if p.all_in else ""))

    def _commit(self, p, amt):
        amt = max(0, min(amt, p.stack))
        p.stack -= amt
        p.bet += amt
        p.total_live += amt
        if p.stack == 0:
            p.all_in = True

    def _open_round(self, first):
        self.no_raise = set()
        actors = [p for p in self.players if self.can_act(p)]
        if len(actors) < 2 and all(p.bet == self.current_bet for p in actors):
            self.need_to_act = set()
            self.actor = None
            return
        self.need_to_act = {p.idx for p in actors}
        self.actor = self._seek(first % len(self.players))

    # -- legality ----------------------------------------------------------

    def legal(self, i):
        p = self.players[i]
        to_call = min(max(0, self.current_bet - p.bet), p.stack)
        max_to = p.bet + p.stack
        can_raise = p.stack > to_call and i not in self.no_raise

        if self.current_bet == 0:
            min_to = min(p.bet + self.bb, max_to)
        else:
            min_to = min(self.current_bet + self.min_raise, max_to)

        if self.structure == "Fixed-Limit":
            unit = self.bb if self.street in ("preflop", "flop") else 2 * self.bb
            fixed_to = min(self.current_bet + unit, max_to)
            min_to = fixed_to
            max_to = fixed_to
            if self.raises_this_street >= 4:
                can_raise = False
        elif self.structure == "Pot-Limit":
            call_amt = max(0, self.current_bet - p.bet)
            pot_after_call = self.pot + call_amt
            pl_max = self.current_bet + pot_after_call
            max_to = min(max_to, max(pl_max, min_to))

        if min_to > max_to:
            min_to = max_to
        return {
            "to_call": to_call,
            "can_check": to_call == 0,
            "can_raise": can_raise and max_to > self.current_bet,
            "min_to": min_to,
            "max_to": max_to,
            "pot": self.pot,
        }

    # -- actions -----------------------------------------------------------

    def act(self, i, action, amount=0):
        p = self.players[i]
        lg = self.legal(i)

        if action == "fold":
            p.folded = True
            p.last_action = "FOLD"
            self.emit("fold", f"{p.name} folds")

        elif action == "call":
            amt = lg["to_call"]
            if amt == 0:
                p.last_action = "CHECK"
                self.emit("check", f"{p.name} checks")
            else:
                self._commit(p, amt)
                if p.all_in:
                    p.last_action = f"ALL-IN {p.bet}"
                    self.emit("bet", f"{p.name} calls {amt} and is all-in")
                else:
                    p.last_action = f"CALL {amt}"
                    self.emit("bet", f"{p.name} calls {amt}")

        elif action == "raise":
            target = min(int(amount), lg["max_to"])
            if target < lg["min_to"]:
                target = lg["min_to"]
            add = target - p.bet
            if add <= 0:                 # degenerate; treat as a call
                return self.act(i, "call")
            opening = self.current_bet == 0
            self._commit(p, add)
            raise_size = p.bet - self.current_bet
            full = raise_size >= self.min_raise
            reopened = p.bet > self.current_bet
            if reopened:
                self.current_bet = p.bet
                self.raises_this_street += 1
                if self.street == "river":
                    self.river_aggr = i
                if full:
                    self.min_raise = raise_size
                    self.no_raise.clear()
                    self.need_to_act = {q.idx for q in self.players
                                        if self.can_act(q)}
                else:
                    # all-in for less than a full raise: players who already
                    # acted must still call the difference, but can't re-raise
                    behind = {q.idx for q in self.players
                              if self.can_act(q) and q.idx != i
                              and q.bet < self.current_bet}
                    self.no_raise |= behind - self.need_to_act
                    self.need_to_act |= behind
            verb = "bets" if opening else "raises to"
            shown = add if opening else p.bet
            if p.all_in:
                p.last_action = f"ALL-IN {p.bet}"
                self.emit("raise", f"{p.name} is all-in for {p.bet}")
            else:
                p.last_action = (f"BET {add}" if opening
                                 else f"RAISE {p.bet}")
                self.emit("raise", f"{p.name} {verb} {shown}")

        self.need_to_act.discard(i)
        self.actor = self._seek((i + 1) % len(self.players))

    # -- street progression ------------------------------------------------

    def next_street(self):
        for p in self.players:
            p.bet = 0
            if not p.folded and not p.all_in:
                p.last_action = ""
        self.current_bet = 0
        self.min_raise = self.bb
        self.raises_this_street = 0

        if self.street == "preflop":
            self.board += self.deck.deal(3)
            self.street = "flop"
        elif self.street == "flop":
            self.board += self.deck.deal(1)
            self.street = "turn"
        elif self.street == "turn":
            self.board += self.deck.deal(1)
            self.street = "river"
        elif self.street == "river":
            self.street = "showdown"
            return
        self.emit("street",
                  f"=== {self.street.upper()}  "
                  f"[{' '.join(str(c) for c in self.board)}] ===")
        self._open_round((self.button + 1) % len(self.players))

    # -- rabbit hunt -------------------------------------------------------

    def peek_runout(self):
        """The cards that would have completed the board. Non-mutating."""
        need = 5 - len(self.board)
        if need <= 0 or self.deck is None:
            return []
        cards = list(self.deck.cards)
        out = []
        take = min(3 if len(self.board) == 0 else 1, need)
        while need > 0:
            out += cards[-take:]
            del cards[-take:]
            need -= take
            take = min(1, need) or 1
            if need <= 0:
                break
        return out

    # -- settlement --------------------------------------------------------

    def settle(self, runs=1, force_tabled=False):
        """Award the pot. `runs=2` runs the remaining board twice and splits
        each pot between the runs (all-in situations only). `force_tabled`
        marks hands as tabled when betting locked with a complete board."""
        result = {"pots": [], "refund": None, "winners": set(),
                  "runs": [], "order": [], "shown": set(), "mucked": set(),
                  "tabled": force_tabled}

        # 1. return an uncalled live bet (dead money never comes back)
        lives = sorted((p.total_live for p in self.players
                        if p.total_live > 0), reverse=True)
        if len(lives) >= 2 and lives[0] > lives[1]:
            top = [p for p in self.players if p.total_live == lives[0]]
            if len(top) == 1:
                p = top[0]
                back = lives[0] - lives[1]
                p.total_live -= back
                p.stack += back
                p.all_in = p.stack == 0
                result["refund"] = (p.idx, back)
                self.emit("pot", f"{back} returned to {p.name} (uncalled)")

        alive = self.contested()

        # 2. complete the board if the hand ended before the river
        boards = []
        if len(alive) > 1:
            if runs == 2 and len(self.board) < 5:
                base = list(self.board)
                need = 5 - len(base)
                b1 = base + self.deck.deal(need)
                b2 = base + self.deck.deal(need)
                self.board = b1
                self.board2 = b2
                boards = [b1, b2]
                result["tabled"] = True
                self.emit("street", "=== RUN 1  ["
                          + " ".join(str(c) for c in b1) + "] ===")
                self.emit("street", "=== RUN 2  ["
                          + " ".join(str(c) for c in b2) + "] ===")
            else:
                if len(self.board) < 5:
                    result["tabled"] = True
                    while len(self.board) < 5:
                        self.next_street()
                self.street = "showdown"
                boards = [self.board]
        else:
            boards = [self.board]

        # 3. score every player still in, per run
        if len(alive) > 1:
            for b in boards:
                scores = {p.idx: evaluate(p.hole + b) for p in alive}
                best = {p.idx: best_five(p.hole + b) for p in alive}
                result["runs"].append({"board": b, "scores": scores,
                                       "best": best})

        # 4. layered side pots (dead money plays: totals include antes)
        levels = sorted({p.total for p in self.players if p.total > 0})
        prev = 0
        for lvl in levels:
            amount = sum(min(p.total, lvl) - min(p.total, prev)
                         for p in self.players)
            eligible = [p.idx for p in alive if p.total >= lvl]
            prev = lvl
            if amount <= 0 or not eligible:
                continue
            if (result["pots"] and
                    result["pots"][-1]["eligible"] == eligible):
                result["pots"][-1]["amount"] += amount   # merge
            else:
                result["pots"].append({"amount": amount, "eligible": eligible})

        # dead money stacked above every live stake (a folded BB's ante,
        # say) has no eligible layer of its own; it belongs to the top pot
        committed = sum(p.total for p in self.players)
        built = sum(pt["amount"] for pt in result["pots"])
        if result["pots"] and committed > built:
            result["pots"][-1]["amount"] += committed - built

        # 5. award, split across runs
        nruns = max(1, len(result["runs"])) if len(alive) > 1 else 1
        for k, pot in enumerate(result["pots"]):
            elig = pot["eligible"]
            pot["runs"] = []
            union = set()
            shares = [pot["amount"] // nruns] * nruns
            shares[0] += pot["amount"] - sum(shares)     # odd chip to run 1
            for r in range(nruns):
                if len(alive) == 1:
                    winners = [alive[0].idx]
                else:
                    sc = result["runs"][r]["scores"]
                    best = max(sc[i] for i in elig)
                    winners = [i for i in elig if sc[i] == best]
                pot["runs"].append({"winners": winners,
                                    "amount": shares[r]})
                union.update(winners)

                share, rem = divmod(shares[r], len(winners))
                for i in winners:
                    self.players[i].won += share
                    self.players[i].stack += share
                # odd chips go to the first winner left of the button
                j = self.button
                while rem > 0:
                    j = (j + 1) % len(self.players)
                    if j in winners:
                        self.players[j].won += 1
                        self.players[j].stack += 1
                        rem -= 1

            pot["winners"] = sorted(union)
            result["winners"].update(union)

        # 6. showdown order and mucking
        if len(alive) > 1:
            if self.river_aggr is not None and not result["tabled"]:
                start = self.river_aggr
            else:
                start = self._next(self.button,
                                   lambda q: q.in_seat and not q.folded)
            order = []
            n = len(self.players)
            for k in range(n):
                j = (start + k) % n
                if self.players[j].in_seat and not self.players[j].folded:
                    order.append(j)
            result["order"] = order

            best_so_far = None
            sc0 = result["runs"][0]["scores"]
            for j in order:
                must_show = (result["tabled"] or j in result["winners"]
                             or best_so_far is None
                             or sc0[j] >= best_so_far)
                if must_show:
                    result["shown"].add(j)
                    if best_so_far is None or sc0[j] > best_so_far:
                        best_so_far = sc0[j]
                    nm = hand_name(sc0[j])
                    self.emit("show", f"{self.players[j].name} shows {nm}")
                else:
                    result["mucked"].add(j)
                    self.emit("fold", f"{self.players[j].name} mucks")

        # 7. narrate the pots
        for k, pot in enumerate(result["pots"]):
            name = "Main pot" if k == 0 else f"Side pot {k}"
            if len(alive) == 1:
                who = self.players[pot["runs"][0]["winners"][0]].name
                self.emit("pot", f"{who} wins {pot['amount']} "
                                 f"(everyone else folded)")
                continue
            for r, run in enumerate(pot["runs"]):
                who = ", ".join(self.players[i].name for i in run["winners"])
                lab = hand_name(result["runs"][r]["scores"]
                                [run["winners"][0]])
                tag = f" (run {r+1})" if nruns > 1 else ""
                self.emit("pot",
                          f"{name}{tag} {run['amount']} -> {who} ({lab})")

        for p in self.players:
            p.total_live = 0
            p.total_dead = 0
            p.bet = 0
        self.street = "idle"
        return result


class Brain:
    """Decides for one AI seat. Returns ('fold'|'call'|'raise', amount)."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    def sims_for(self, street, n_opp):
        base = {"flop": 220, "turn": 260, "river": 300}.get(street, 200)
        return max(80, int(base / max(1, n_opp * 0.7)))

    def decide(self, e: Engine, i: int):
        p = e.players[i]
        lg = e.legal(i)
        st = STYLES[p.style]
        r = self.rng
        pot = lg["pot"]
        to_call = lg["to_call"]

        opps = [q for q in e.contested() if q.idx != i]
        n_opp = max(1, len(opps))

        if e.street == "preflop":
            return self._preflop(e, p, lg, st, n_opp)

        eq = equity(p.hole, e.board, n_opp,
                    self.sims_for(e.street, n_opp), r)
        eq = eq[2] if eq else 0.3

        # weaker AIs misread their hand
        if p.level == 1:
            eq = min(1.0, max(0.0, eq + r.uniform(-0.18, 0.18)))
        elif p.level == 2:
            eq = min(1.0, max(0.0, eq + r.uniform(-0.07, 0.07)))

        pot_odds = to_call / (pot + to_call) if to_call else 0.0

        if to_call == 0:
            # no bet in front of us: check or take the betting lead
            if eq > 0.78:
                chance = 0.9 * st["agg"] + 0.1
            elif eq > 0.58:
                chance = st["cbet"]
            elif eq > 0.42:
                chance = st["cbet"] * 0.4
            else:
                chance = st["bluff"]
            if lg["can_raise"] and r.random() < chance:
                frac = 0.75 if eq > 0.7 else (0.6 if eq > 0.45 else 0.55)
                if p.style == "Maniac":
                    frac *= 1.3
                target = self._size(e, p, lg, int(pot * frac))
                return ("raise", target)
            return ("call", 0)

        # facing a bet
        raise_edge = eq - pot_odds
        if (lg["can_raise"] and eq > 0.66
                and raise_edge > 0.12 and r.random() < st["agg"]):
            frac = 0.85 if eq > 0.8 else 0.65
            target = self._size(e, p, lg,
                                int(e.current_bet + pot * frac))
            return ("raise", target)

        # semi-bluff / pure bluff raise
        if (lg["can_raise"] and eq < 0.45
                and r.random() < st["bluff"] * 0.35):
            target = self._size(e, p, lg, int(e.current_bet + pot * 0.7))
            return ("raise", target)

        if eq >= pot_odds + 0.02:
            return ("call", 0)

        # cheap call with implied odds
        if to_call <= pot * 0.12 and eq > 0.25 and r.random() < 0.55:
            return ("call", 0)

        return ("fold", 0)

    def _preflop(self, e, p, lg, st, n_opp):
        r = self.rng
        sc = chen(p.hole)
        to_call = lg["to_call"]
        pot = lg["pot"]

        # position: how many players still act behind us
        after = 0
        j = p.idx
        for _ in range(len(e.players) - 1):
            j = (j + 1) % len(e.players)
            q = e.players[j]
            if q.idx == e.bb_i and e.current_bet <= e.bb:
                continue
            if e.can_act(q) and q.idx in e.need_to_act:
                after += 1
        late = after <= 1
        pos_adj = -1.5 if late else 0.0
        if p.level == 1:
            pos_adj = 0.0
        if p.level == 3:
            pos_adj *= 1.4

        open_t = st["open"] + pos_adj
        call_t = st["call"] + pos_adj
        three_t = st["three"] + pos_adj

        bb_stacks = p.stack / max(1, e.bb)
        raised = e.current_bet > e.bb

        # short stack: push/fold
        if bb_stacks <= 12 and lg["can_raise"]:
            if sc >= (call_t + 1) or (sc >= call_t - 1 and late):
                return ("raise", lg["max_to"])

        if to_call == 0:
            # limped to us / BB option
            if sc >= open_t + 1 and lg["can_raise"] and r.random() < 0.85:
                limpers = sum(1 for q in e.players
                              if q.in_seat and not q.folded and q.bet > 0
                              and q.idx not in (e.sb_i, e.bb_i))
                size = int(e.bb * (2.5 + limpers))
                return ("raise", self._size(e, p, lg, max(size, lg["min_to"])))
            if lg["can_raise"] and r.random() < st["bluff"] * 0.5:
                return ("raise", self._size(e, p, lg, int(e.bb * 2.5)))
            return ("call", 0)

        # someone bet
        if sc >= three_t and lg["can_raise"] and r.random() < st["agg"] + 0.15:
            size = int(e.current_bet * (3 if not raised else 2.6))
            return ("raise", self._size(e, p, lg, size))

        # pot odds sanity for calls
        odds = to_call / (pot + to_call)
        need = call_t + (2.5 if raised else 0) + (odds * 6)
        if sc >= need:
            return ("call", 0)

        # cheap flat with a speculative hand
        if to_call <= e.bb and sc >= call_t - 2 and r.random() < 0.6:
            return ("call", 0)

        if r.random() < st["bluff"] * 0.25 and lg["can_raise"]:
            return ("raise", self._size(e, p, lg, int(e.current_bet * 3)))

        return ("fold", 0)

    @staticmethod
    def _size(e, p, lg, target):
        target = max(lg["min_to"], min(int(target), lg["max_to"]))
        # don't leave a silly stub behind
        if lg["max_to"] - target < e.bb:
            target = lg["max_to"]
        return target
