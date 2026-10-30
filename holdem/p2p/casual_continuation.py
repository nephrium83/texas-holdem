"""Signed membership boundaries selected by the adopted table policy.

Seats and their keys never move. Departures stop the current deal, every
survivor computes its own carry-forward balance, and the next deal waits
for matching signed reports. This owns no clocks and performs no reconnect.
"""
from __future__ import annotations

import hashlib
import json


class CasualContinuation:
    def __init__(self, session):
        self.session = session
        self.departed = set()
        self.observed = {}                 # signed observations, in relay order
        self.epoch = 0
        self.pending = None
        self.carry = None
        self.early_acks = {}
        self.last_notice = None

    def active(self, stacks):
        return [seat for seat, chips in enumerate(stacks)
                if chips > 0 and seat not in self.departed]

    def timekeeper(self):
        s = self.session
        r = s._replica
        if self.carry is not None:
            playing = self.active(self.carry["stacks"])
        elif s._followed is not None and s._eliminated():
            playing = self.active(s._followed[1])
        elif r is None:
            playing = list(range(len(s._seat_order)))
        elif s.hand_result is not None:
            playing = self.active(r.stacks)
        else:
            # An all-in player has zero uncommitted chips but has not busted.
            playing = [seat for seat in r.seats_dealt
                       if seat not in self.departed]
        unavailable = set(self.observed) | set(
            getattr(s, "_casual_clock_faults", ()))
        host = s._host_seat()
        if host in playing and host not in unavailable:
            return host
        return next((seat for seat in sorted(playing)
                     if seat not in unavailable), None)

    def _fail(self, reason):
        s = self.session
        if s.terminal_state is not None:
            return
        if self.pending is not None:
            notice = self.pending["notice"]
            s._send_hostless({"type": "casual_drop_dispute",
                              "hand": notice["hand"], "epoch": notice["epoch"],
                              "reason": str(reason)[:256]})
        s.terminate(s.ABORTED_PROTOCOL,
                    "casual continuation disputed: " + reason
                    + "; this game keeps its own result")

    def receive_dispute(self, msg):
        notice = (self.pending["notice"] if self.pending is not None
                  else self.last_notice)
        if (notice is None or type(msg.get("seat")) is not int
                or type(msg.get("epoch")) is not int
                or type(msg.get("hand")) is not int
                or msg.get("epoch") != notice["epoch"]
                or msg.get("hand") != notice["hand"]
                or msg.get("seat") not in notice["required"]):
            return
        # Do not echo another participant's report into a message loop.
        s = self.session
        s.terminate(s.ABORTED_PROTOCOL,
                    f"casual continuation disputed by seat {msg['seat']}: "
                    + str(msg.get("reason", "departure results differ"))[:256]
                    + "; this game keeps its own result")

    def request_drop(self, lost, reason="timeout", scope=None):
        s = self.session
        if (type(lost) is not int or not 0 <= lost < len(s._seat_order)
                or lost == s._host_seat() or lost in self.departed
                or s.terminal_state is not None):
            return False
        msg = {"type": "casual_drop_observed", "hand": s._hand_no,
               "seat": s.local_seat, "lost_seat": lost,
               "reason": reason, "scope": scope}
        if not self._valid_observation(msg):
            return False
        if lost in self.observed:
            self._drive()
            return True
        s._send_hostless(dict(msg))
        self.receive_observation(msg)
        return True

    def _valid_observation(self, msg):
        s = self.session
        lost, reporter = msg.get("lost_seat"), msg.get("seat")
        if (type(lost) is not int or not 0 <= lost < len(s._seat_order)
                or lost == s._host_seat() or type(reporter) is not int):
            return False
        reason = msg.get("reason")
        if reason == "disconnect":
            return reporter == s._host_seat()
        if reason == "clock" and reporter == s._host_seat():
            # The transport reports skew only after verifying that peer's
            # signed envelope. A self-reported jump follows the other path.
            return True
        if reporter != self.timekeeper():
            return False
        if reason == "clock":
            return (lost in getattr(s, "_casual_clock_faults", ())
                    or self.observed.get(lost, {}).get("reason") == "clock")
        validator = getattr(s, "_casual_validate_drop_scope", None)
        return (reason == "timeout" and validator is not None
                and validator(msg))

    def receive_observation(self, msg):
        if not self._valid_observation(msg):
            return
        lost = msg["lost_seat"]
        if lost in self.departed:
            return
        self.observed.setdefault(lost, {
            "reason": msg["reason"], "scope": msg.get("scope")})
        self._drive()

    def _own_notice(self, lost):
        s = self.session
        if self.carry is not None:
            # No deal ran between serialized departures. Reserve a fresh
            # protocol number anyway; an abandoned key ceremony is never
            # recreated under the same context/hand/seat derivation inputs.
            return {"hand": self.carry["resume_hand"] - 1,
                    "lost_seat": lost, "dealt": False,
                    "settled": False, "actions": 0}
        return s._drop_notice(lost)

    def _required(self, notice):
        s, lost = self.session, notice["lost_seat"]
        r = s._replica
        if self.carry is not None:
            seats = self.active(self.carry["stacks"])
        elif s._eliminated() and notice["hand"] != s._hand_no:
            known = s._followed[1] if s._followed else r.stacks
            # A settled hand can have just busted another participant.
            # Its ordinary settlement reports have already reached this
            # courier before this sender's later boundary.
            if notice["settled"] and notice["hand"] == s._ended_hand:
                known = s._ended_stacks or known
            seats = self.active(known)
        elif (notice["settled"] or not notice["dealt"]
              or notice["hand"] != s._hand_no):
            known = (r.stacks if s.hand_result is not None
                     else s._hand_stacks)
            seats = self.active(known)
        else:
            seats = list(r.seats_dealt) if r is not None else []
        return sorted(set(seats) - self.departed - {lost})

    def _needed(self, lost):
        if self.carry is not None:
            return lost in self.active(self.carry["stacks"])
        s = self.session
        return not s._eliminated() and s._seat_in_play(lost)

    def _drive(self):
        s = self.session
        if s.terminal_state is not None:
            return
        if self.pending is not None:
            self._maybe_commit()
            if self.pending is not None or s.terminal_state is not None:
                return
        if s.local_seat != self.timekeeper():
            return
        lost = next((seat for seat in self.observed
                     if seat not in self.departed and self._needed(seat)), None)
        if lost is None:
            return
        if s._replica is None or s._table_cfg is None:
            self._fail("a departure preceded the first hand's configuration")
            return
        notice = self._own_notice(lost)
        notice.update({"type": "casual_drop", "seat": s.local_seat,
                       "epoch": self.epoch + 1,
                       "resume_hand": notice["hand"] + 2,
                       "reason": self.observed[lost]["reason"],
                       "scope": self.observed[lost]["scope"],
                       "required": self._required(notice),
                       "departed": sorted(self.departed | {lost})})
        notice.pop("ended", None)
        s._send_hostless(dict(notice))
        self.receive_drop(notice)

    @staticmethod
    def _canonical_notice(msg):
        fields = ("hand", "seat", "lost_seat", "dealt", "settled",
                  "actions", "epoch", "resume_hand", "reason", "scope",
                  "required", "departed")
        return {field: msg.get(field) for field in fields}

    @staticmethod
    def _digest(notice, stacks, positions):
        body = {"boundary": notice, "stacks": stacks,
                "positions": list(positions) if positions is not None else None}
        return hashlib.sha256(json.dumps(body, sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()

    def receive_drop(self, msg):
        s = self.session
        notice = self._canonical_notice(msg)
        epoch, lost = notice["epoch"], notice["lost_seat"]
        if (type(epoch) is not int or type(lost) is not int
                or type(notice["seat"]) is not int):
            return
        if epoch <= self.epoch:
            return
        # Only the elected timekeeper may create or conflict with a
        # membership boundary. A valid signature from another participant
        # proves its identity, not authority to dispute this pending notice.
        if notice["seat"] != self.timekeeper() or lost not in self.observed:
            return
        if self.pending is not None:
            if notice != self.pending["notice"]:
                self._fail("two different membership boundaries overlapped")
            return
        observation = self.observed[lost]
        if (epoch != self.epoch + 1 or lost in self.departed
                or notice["reason"] != observation["reason"]
                or notice["scope"] != observation["scope"]
                or type(notice["hand"]) is not int
                or type(notice["actions"]) is not int
                or notice["actions"] < 0
                or type(notice["dealt"]) is not bool
                or type(notice["settled"]) is not bool
                or type(notice["resume_hand"]) is not int
                or notice["resume_hand"] != notice["hand"] + 2
                or notice["departed"] != sorted(self.departed | {lost})
                or notice["required"] != self._required(notice)):
            self._fail("a signed boundary did not match this game's membership")
            return
        # A spectator did not play this hand. It follows the same reports
        # from every surviving participant instead of inventing a result.
        observer = s._eliminated() and notice["hand"] != s._hand_no
        if not observer and s._hand_no >= notice["resume_hand"]:
            self._fail("the reserved continuation hand was already used")
            return
        self.pending = {"notice": notice, "reports": {}, "own": None,
                        "observer": observer}
        if not observer:
            result = self._compute(notice)
            if result is None or s.terminal_state is not None:
                return
            stacks, positions = result
            report = self._digest(notice, stacks, positions), tuple(stacks), (
                tuple(positions) if positions is not None else None)
            self.pending["own"] = report
            if s.local_seat in notice["required"]:
                self.pending["reports"][s.local_seat] = report
                s._send_hostless({"type": "casual_drop_ack",
                                  "hand": notice["hand"], "epoch": epoch,
                                  "digest": report[0], "stacks": list(stacks),
                                  "positions": list(positions)
                                  if positions is not None else None})
        for ack in self.early_acks.pop(epoch, ()):
            self.receive_ack(ack)
        self._maybe_commit()
        s._notify_state_changed()

    def _compute(self, notice):
        s, lost = self.session, notice["lost_seat"]
        if self.carry is not None:
            own = self._own_notice(lost)
            if any(own[key] != notice[key] for key in
                   ("hand", "dealt", "settled", "actions")):
                self._fail("a serialized departure changed its settled boundary")
                return None
            return list(self.carry["stacks"]), self.carry["positions"]
        r = s._replica
        if r is None:
            self._fail("there was no replica for a signed departure")
            return None
        own = s._drop_notice(lost)
        live = s.hand_result is None and not s.hand_voided
        disputed, undealt = s._drop_dispute(own, live, notice, notice["seat"])
        if live and notice["hand"] < s._hand_no and own["dealt"]:
            disputed = "; this game had already dealt a later hand"
        if live and s._early_settled and not disputed:
            disputed = "; another game had already settled this hand"
        cancel = (not own["dealt"] or undealt
                  or notice["hand"] < s._hand_no)
        if not live:
            result = (list(r.stacks), r.positions) if s.hand_result is not None else (
                list(s._hand_stacks), s._hand_positions)
        elif cancel:
            result = list(s._hand_stacks), s._hand_positions
        else:
            deal = getattr(s._deal_driver, "deal", None)
            if (not s.prevention or deal is None
                    or s.proofs_verified != len(deal.seats_in)):
                self._fail("a dealt hand lacked every Bayer-Groth proof")
                return None
            from holdem.engine import settle_forfeit
            e = r.engine
            in_hand = [player.idx for player in e.contested()]
            if in_hand == [lost]:
                try:
                    s.hand_result = r.finish()
                    stacks = r.stacks
                except Exception as exc:
                    self._fail("the decided hand could not settle: " + str(exc))
                    return None
            else:
                out = settle_forfeit(
                    stacks=[p.stack for p in e.players],
                    live=[p.total_live for p in e.players],
                    dead=[p.total_dead for p in e.players],
                    in_hand=in_hand, dropped=lost, button=e.button)
                stacks = out["stacks"]
            result = list(stacks), r.positions
        s._drop_stacks = list(result[0])
        if disputed:
            self._fail(disputed.lstrip("; "))
            return None
        if live and s.hand_result is None:
            outcome = s.VOID_PEER_LOST if cancel else s.HAND_FORFEIT
            reason = (f"{s._seat_label(lost)} left before the deal; blinds restored"
                      if cancel else f"{s._seat_label(lost)} dropped and forfeits hand {s._hand_no}")
            s._end_hand(outcome, reason, blamed_seat=lost, announce=False)
        # Obsolete keys/shares cannot advance the stopped hand.
        s._deal_outbox.clear()
        deal = getattr(s._deal_driver, "deal", None)
        if deal is not None:
            deal._held.clear()
        s._msg_buffer = [item for item in s._msg_buffer
                         if item.incoming.hand >= notice["resume_hand"]]
        s._clear_deadline()
        return result

    def receive_ack(self, msg):
        s = self.session
        epoch, seat = msg.get("epoch"), msg.get("seat")
        if type(epoch) is not int or type(seat) is not int:
            return
        if self.pending is None:
            if epoch == self.epoch + 1:
                kept = self.early_acks.setdefault(epoch, [])
                if len(kept) < 2 * len(s._seat_order):
                    kept.append(dict(msg))
            return
        notice = self.pending["notice"]
        if epoch != notice["epoch"] or seat not in notice["required"]:
            return
        stacks, positions = msg.get("stacks"), msg.get("positions")
        if (not isinstance(stacks, list) or len(stacks) != len(s._seat_order)
                or any(type(chips) is not int or chips < 0 for chips in stacks)
                or sum(stacks) != s._table_cfg["total_chips"]
                or (positions is not None and (not isinstance(positions, list)
                    or len(positions) != 3
                    or any(type(p) is not int or not 0 <= p < len(stacks)
                           for p in positions)))
                or msg.get("digest") != self._digest(notice, stacks, positions)):
            self._fail("a survivor reported an invalid departure result")
            return
        report = msg["digest"], tuple(stacks), (
            tuple(positions) if positions is not None else None)
        old = self.pending["reports"].get(seat)
        if (old is not None and old != report
                or self.pending["own"] is not None
                and self.pending["own"] != report):
            self._fail(f"seat {seat} reported different departure balances")
            return
        self.pending["reports"][seat] = report
        self._maybe_commit()

    def _maybe_commit(self):
        s, pending = self.session, self.pending
        if pending is None or s.terminal_state is not None:
            return
        notice, reports = pending["notice"], pending["reports"]
        required = set(notice["required"]) - set(self.observed) - set(
            getattr(s, "_casual_clock_faults", ()))
        if not required <= set(reports):
            return
        got = set(reports.values())
        if pending["own"] is not None:
            got.add(pending["own"])
        if len(got) != 1:
            self._fail("the surviving departure reports did not agree")
            return
        _, stacks, positions = got.pop()
        self.departed.update(notice["departed"])
        self.epoch = notice["epoch"]
        self.last_notice = notice
        self.carry = {"stacks": list(stacks), "positions": positions,
                      "resume_hand": notice["resume_hand"]}
        self.pending = None
        s._drop_stacks = list(stacks)
        if pending["observer"]:
            s._followed = (notice["hand"], list(stacks))
        if s.local_seat in self.departed:
            reasons = {
                "timeout": f"The timekeeper timed you out (seat {notice['seat']})",
                "clock": "You left the table because your clock changed (clock)",
                "disconnect": "The host disconnected you",
            }
            s.terminate(s.PEER_LOST, reasons[notice["reason"]],
                        seat=s.local_seat)
            return
        if len(self.active(stacks)) < 2:
            s._finish_session(list(stacks), announce=False)
            return
        self._drive()
        s._notify_state_changed()

    def next_inputs(self):
        if self.pending is not None:
            return "not_ready"
        return self.carry

    def began(self):
        self.carry = None
        self.session._drop_stacks = None

    def missing_ack_seats(self):
        if self.pending is None:
            return []
        return sorted(set(self.pending["notice"]["required"])
                      - set(self.pending["reports"]) - set(self.observed)
                      - set(getattr(self.session, "_casual_clock_faults", ())))

    def ack_scope(self):
        if self.pending is None:
            return None
        notice = self.pending["notice"]
        return {"epoch": notice["epoch"], "hand": notice["hand"],
                "resume_hand": notice["resume_hand"]}

