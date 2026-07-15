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
        self.in_seat = True       # has chips, dealt into this hand
        self.bet = 0              # chips in front this street
        self.total = 0            # chips committed this hand
        self.last_action = ""
        self.won = 0


# ----------------------------------------------------------------- engine

class Engine:
    """Pure game state. The GUI polls `actor`, calls `act()`, then `step()`."""

    def __init__(self, players, sb=10, bb=20, structure="No-Limit",
                 rng: random.Random | None = None):
        self.players = players
        self.rng = rng or random.Random()
        self.sb = sb
        self.bb = bb
        self.structure = structure
        self.button = len(players) - 1     # so hand 1 puts the button on seat 0
        self.deck = None
        self.board = []
        self.street = "idle"
        self.current_bet = 0
        self.min_raise = bb
        self.need_to_act = set()
        self.actor = None
        self.hand_no = 0
        self.raises_this_street = 0
        self.no_raise = set()     # seats facing an under-raise: call/fold only
        self.sb_i = self.bb_i = None
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

    # -- hand setup --------------------------------------------------------

    def start_hand(self):
        for p in self.players:
            p.in_seat = p.stack > 0
            p.hole = []
            p.folded = not p.in_seat
            p.all_in = False
            p.bet = 0
            p.total = 0
            p.last_action = ""
            p.won = 0

        live = self.seated()
        if len(live) < 2:
            return False

        self.deck = Deck(self.rng)
        self.board = []
        self.street = "preflop"
        self.current_bet = 0
        self.min_raise = self.bb
        self.raises_this_street = 0
        self.hand_no += 1

        self.button = self._next(self.button, lambda p: p.in_seat)

        if len(live) == 2:                      # heads-up: button is the SB
            self.sb_i = self.button
            self.bb_i = self._next(self.button, lambda p: p.in_seat)
        else:
            self.sb_i = self._next(self.button, lambda p: p.in_seat)
            self.bb_i = self._next(self.sb_i, lambda p: p.in_seat)

        self.emit("hand", f"--- Hand #{self.hand_no}  ({self.sb}/{self.bb}) ---")

        self._post(self.sb_i, self.sb, "SB")
        self._post(self.bb_i, self.bb, "BB")
        self.current_bet = max(p.bet for p in self.players)

        order = [self.sb_i]
        j = self.sb_i
        while True:
            j = self._next(j, lambda p: p.in_seat)
            if j == self.sb_i:
                break
            order.append(j)
        for _ in range(2):
            for i in order:
                self.players[i].hole += self.deck.deal(1)

        if len(live) == 2:
            first = self.button                 # HU: SB/button acts first preflop
        else:
            first = self._next(self.bb_i, lambda p: p.in_seat)
        self._open_round(first)
        return True

    def _post(self, i, amount, label):
        p = self.players[i]
        amt = min(amount, p.stack)
        self._commit(p, amt)
        p.last_action = f"{label} {amt}"
        self.emit("blind", f"{p.name} posts {label} {amt}"
                           + (" (all-in)" if p.all_in else ""))

    def _commit(self, p, amt):
        amt = max(0, min(amt, p.stack))
        p.stack -= amt
        p.bet += amt
        p.total += amt
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

    # -- settlement --------------------------------------------------------

    def settle(self):
        """Award the pot. Returns a result dict for the UI."""
        result = {"pots": [], "refund": None, "winners": set(),
                  "scores": {}, "best": {}}

        # 1. return an uncalled bet (bettor put in more than anyone matched)
        totals = sorted((p.total for p in self.players if p.total > 0),
                        reverse=True)
        if len(totals) >= 2 and totals[0] > totals[1]:
            top = [p for p in self.players if p.total == totals[0]]
            if len(top) == 1:
                p = top[0]
                back = totals[0] - totals[1]
                p.total -= back
                p.stack += back
                p.all_in = p.stack == 0
                result["refund"] = (p.idx, back)
                self.emit("pot", f"{back} returned to {p.name} (uncalled)")

        alive = self.contested()

        # 2. if the hand ends with the board incomplete (e.g. a multi-way
        #    all-in with no further action), run out the remaining streets
        if len(alive) > 1:
            while len(self.board) < 5:
                self.next_street()
            self.street = "showdown"

        # 3. score every player still in
        if len(alive) > 1:
            for p in alive:
                sc = evaluate(p.hole + self.board)
                result["scores"][p.idx] = sc
                result["best"][p.idx] = best_five(p.hole + self.board)

        # 4. layered side pots
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

        # 5. award
        for k, pot in enumerate(result["pots"]):
            elig = pot["eligible"]
            if len(alive) == 1:
                winners = [alive[0].idx]
            else:
                best = max(result["scores"][i] for i in elig)
                winners = [i for i in elig if result["scores"][i] == best]
            pot["winners"] = winners
            result["winners"].update(winners)

            share, rem = divmod(pot["amount"], len(winners))
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

            name = "Main pot" if k == 0 else f"Side pot {k}"
            who = ", ".join(self.players[i].name for i in winners)
            if len(alive) == 1:
                self.emit("pot", f"{who} wins {pot['amount']} "
                                 f"(everyone else folded)")
            else:
                lab = hand_name(result["scores"][winners[0]])
                self.emit("pot", f"{name} {pot['amount']} -> {who} ({lab})")

        for p in self.players:
            p.total = 0
            p.bet = 0
        self.street = "idle"
        return result


# --------------------------------------------------------------------- AI

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
