"""Fresh signed roster clock samples and reliability capability agreement.

The host requests a round; every roster identity signs its own sample and
the host relays it unchanged. Each client independently checks the complete
set, with elapsed time measured by its own monotonic clock.
"""
from __future__ import annotations

import hashlib
import json
import math


PROFILE = "casual-lan.reliability.v1"
TYPES = frozenset({"casual_clock_request", "casual_clock_sample"})


class LobbyClocks:
    def __init__(self, session):
        self.session = session
        self.round = ""
        self.roster = ""
        self.samples = {}
        self.started = float("-inf")
        self.serial = 0

    def identities(self):
        return {p.ed25519_pubkey_hex: p for p in self.session.player_list()
                if p.ed25519_pubkey_hex}

    def roster_id(self):
        rows = sorted((p.conn_id, p.ed25519_pubkey_hex)
                      for p in self.session.player_list())
        return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()

    def _local_key(self):
        player = self.session.players.get(self.session.local_conn_id)
        return player.ed25519_pubkey_hex if player is not None else ""

    def tick(self):
        s = self.session
        if s.state != "LOBBY" or s.terminal_state is not None or not s.is_host:
            return
        now = s._clock.monotonic()
        if self.roster == self.roster_id() and now - self.started < 10.0:
            return
        self.serial += 1
        self.roster = self.roster_id()
        self.round = hashlib.sha256(json.dumps(
            [self.roster, s._clock.time(), now, self.serial],
            separators=(",", ":")).encode()).hexdigest()
        self.started = now
        wall = s._clock.time()
        self.samples = {self._local_key(): (wall, now)}
        s._transport.broadcast({"type": "casual_clock_request", "payload": {
            "round": self.round, "roster": self.roster,
            "wall": wall, "profile": PROFILE, "round_serial": self.serial}})
        s._notify_state_changed()

    def receive(self, conn_id, msg):
        s = self.session
        if s.state != "LOBBY" or s.terminal_state is not None:
            return
        body = msg.get("payload", msg)
        if not isinstance(body, dict):
            return
        author = msg.get("pubkey")
        people = self.identities()
        if author not in people or body.get("profile") != PROFILE:
            return
        if body.get("roster") != self.roster_id():
            return
        wall = body.get("wall")
        if isinstance(wall, bool) or not isinstance(wall, (int, float)) or not math.isfinite(wall):
            return
        round_id = body.get("round")
        if not isinstance(round_id, str) or len(round_id) != 64:
            return
        now = s._clock.monotonic()
        if msg["type"] == "casual_clock_request":
            if s.is_host or not people[author].is_host or author != s._pinned_host_pubkey:
                return
            serial = body.get("round_serial")
            if type(serial) is not int or serial <= self.serial:
                # A previous signed request remains within wire's replay
                # window after the next round begins. Its signature does
                # not make that old round fresh again.
                return
            if round_id == self.round:
                return
            self.serial = serial
            self.round, self.roster, self.started = round_id, body["roster"], now
            local_wall = s._clock.time()
            self.samples = {author: (wall, now), self._local_key(): (local_wall, now)}
            s._transport.broadcast({"type": "casual_clock_sample", "payload": {
                "round": self.round, "roster": self.roster,
                "wall": local_wall, "profile": PROFILE}})
        else:
            if round_id != self.round or body["roster"] != self.roster:
                return
            # First sample wins. Replays cannot refresh a sample's freshness.
            if author in self.samples:
                return
            self.samples[author] = (wall, now)
            if s.is_host:
                s._transport.broadcast_except(conn_id, msg)
        s._notify_state_changed()

    def status(self):
        people = self.identities()
        s = self.session
        now = s._clock.monotonic()
        missing = [p.nickname for key, p in people.items() if key not in self.samples]
        if self.roster != self.roster_id() or now - self.started > 15.0:
            missing = [p.nickname for p in people.values()]
        if missing:
            return {"ready": False, "error": "Checking game version and clocks: " + ", ".join(missing)}
        adjusted = {key: value + now - received
                    for key, (value, received) in self.samples.items() if key in people}
        if len(adjusted) != len(people) or not adjusted:
            return {"ready": False, "error": "Checking game version and clocks"}
        earliest = min(adjusted.values())
        latest = max(adjusted.values())
        if latest - earliest > 20.0:
            local = adjusted.get(self._local_key(), earliest)
            names = [people[k].nickname for k, value in adjusted.items()
                     if abs(value - local) > 20.0]
            if not names:
                names = [people[k].nickname for k, value in adjusted.items()
                         if value in (earliest, latest)]
            return {"ready": False, "error": "Clocks differ by more than 20 seconds: " + ", ".join(names)}
        return {"ready": True, "error": None}


def lobby_clocks(s):
    value = getattr(s, "_casual_lobby_clocks", None)
    if value is None:
        value = s._casual_lobby_clocks = LobbyClocks(s)
    return value
