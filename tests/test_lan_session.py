"""LAN controller policy and lifecycle using the real Session/admission code.

The fake transport stands at the verified-envelope boundary. Separate process
acceptance tests exercise the production signatures, sockets and crypto deal.
"""
from __future__ import annotations

import base64
import threading

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from holdem import lan_session
from holdem.p2p import admission, identity, invite
from holdem.p2p.session import Session
from holdem.p2p.timeout import FakeClock


def _identity():
    return identity.Identity(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())


class FakeTransport:
    delivers_verified_envelopes = True

    def __init__(self):
        self.clock = FakeClock()
        self.messages = []
        self.disconnects = []
        self.sent = []
        self.operations = []
        self.connected = []
        self.eager = None
        self.stop_callback = None

    def get_clock(self):
        return self.clock

    def reset_callbacks(self):
        self.messages.clear()
        self.disconnects.clear()

    def on_message(self, callback):
        self.messages.append(callback)

    def on_disconnect(self, callback, *, with_reason=False):
        self.disconnects.append(callback)

    def start_host(self, port):
        assert self.messages and self.disconnects
        self.operations.append("host")
        return "192.168.1.23:45678"

    def connect(self, address):
        assert self.messages and self.disconnects
        self.operations.append("connect")
        self.connected.append(address)
        if self.eager:
            self.deliver("host-hop", self.eager)
        return "host-hop"

    def send(self, conn_id, message):
        self.sent.append((conn_id, message))

    def broadcast(self, message):
        self.sent.append((None, message))

    def broadcast_except(self, conn_id, message):
        self.sent.append((f"except:{conn_id}", message))

    def disconnect(self, conn_id):
        self.operations.append("disconnect")

    def stop(self):
        self.operations.append("stop")
        if self.stop_callback:
            callback, self.stop_callback = self.stop_callback, None
            callback()

    def deliver(self, conn_id, message):
        for callback in list(self.messages):
            callback(conn_id, message)

    def last(self, mtype):
        return next(message for _cid, message in reversed(self.sent)
                    if message["type"] == mtype)


@pytest.fixture
def controller(monkeypatch):
    fake = FakeTransport()
    local = _identity()
    monkeypatch.setattr(lan_session, "transport", fake)
    monkeypatch.setattr(lan_session.identity, "load_or_create", lambda: local)
    monkeypatch.setattr(lan_session.native_crypto, "initialize", lambda: None)
    changes = []
    table = lan_session.LanTable("Alice", {}, lambda: changes.append(True))
    yield table, fake, local, changes
    table.close()


def _packet(mtype, author, payload):
    return {"type": mtype, "pubkey": author.public_key_bytes().hex(), "payload": payload}


def _admit(table, fake, remote, name="Bob", cid="remote", capable=True):
    """Earn an actual host roster seat through the production admission gate."""
    parsed = invite.parse_room_code(table.lobby_state()["invite"])
    pin = admission.JoinerAdmission(
        admission_secret=bytes.fromhex(parsed["admission_secret"]),
        host_pubkey=bytes.fromhex(parsed["host_pubkey"]),
        joiner_pubkey=remote.public_key_bytes(),
        discovery_token=bytes.fromhex(parsed["discovery_token"]),
    )
    fake.deliver(cid, _packet("admission_hello", remote, pin.hello_payload()))
    challenge = fake.last("admission_challenge")
    response = pin.on_challenge(bytes.fromhex(parsed["host_pubkey"]),
                                bytes.fromhex(challenge["client_nonce"]),
                                bytes.fromhex(challenge["server_nonce"]))
    assert response is not None
    fake.deliver(cid, _packet("admission_response", remote, response))
    fake.deliver(cid, _packet("player_info", remote, {"nickname": name}))
    if capable:
        table.session.reliability_tick()
        request = fake.last("casual_clock_request")["payload"]
        fake.deliver(cid, _packet("casual_clock_sample", remote,
                                 dict(request, wall=fake.clock.time())))
    return cid


def _join_authenticated(table, fake, local):
    host = _identity()
    code = invite.generate_room_code(host.public_key_bytes(),
                                     public_address=("192.168.1.23", 45678))
    parsed = invite.parse_room_code(code)
    policy = admission.HostAdmission(
        admission_secret=bytes.fromhex(parsed["admission_secret"]),
        host_pubkey=host.public_key_bytes(),
        discovery_token=bytes.fromhex(parsed["discovery_token"]),
    )
    table.join(code)
    hello = fake.last("admission_hello")
    client_nonce = bytes.fromhex(hello["client_nonce"])
    challenge = policy.on_hello("joiner-hop", local.public_key_bytes(), client_nonce)
    assert challenge is not None
    fake.deliver("host-hop", _packet("admission_challenge", host, challenge))
    response = fake.last("admission_response")
    assert policy.on_response("joiner-hop", local.public_key_bytes(),
                              bytes.fromhex(response["client_nonce"]),
                              bytes.fromhex(response["server_nonce"]),
                              bytes.fromhex(response["mac"]))
    fake.deliver("host-hop", _packet("admission_accept", host,
                                     policy.accept_payload("joiner-hop")))
    return host, code


def _joined_roster(fake, host, local):
    fake.deliver("host-hop", _packet("player_ack", host, {"your_conn_id": "you"}))
    fake.deliver("host-hop", _packet("player_list", host, {"players": [
        {"conn_id": "host", "nickname": "Bob", "ready": True, "is_host": True,
         "ed25519_pubkey_hex": host.public_key_bytes().hex()},
        {"conn_id": "you", "nickname": "Alice", "ready": False,
         "ed25519_pubkey_hex": local.public_key_bytes().hex()},
    ]}))


def test_host_invite_contains_address_and_full_identity(controller):
    table, fake, local, changes = controller
    assert table.session is None
    table.host()
    state = table.lobby_state()
    parsed = invite.parse_room_code(state["invite"])
    assert parsed["public_ip"] == "192.168.1.23"
    assert parsed["public_port"] == 45678
    assert parsed["host_pubkey"] == local.public_key_bytes().hex()
    assert state["state"] == "lobby"
    assert state["players"] == [{"seat": 0, "name": "Alice", "ready": True,
                                  "is_you": True, "is_host": True}]
    assert not state["can_start"]
    assert table.session.author_mode == "wire"
    assert changes


def test_host_start_requires_admitted_ready_players(controller, monkeypatch):
    table, fake, _local, _changes = controller
    table.host()
    assert table.start() == "not_ready"
    remote = _identity()
    cid = _admit(table, fake, remote)
    assert table.lobby_state()["players"][1]["name"] == "Bob"
    assert not table.lobby_state()["can_start"]
    assert table.start() == "not_ready"
    fake.deliver(cid, _packet("ready", remote, {"ready": True}))
    assert table.lobby_state()["can_start"]
    starts = []
    monkeypatch.setattr(Session, "start_p2p_hand", lambda self, **kw: starts.append(kw))
    assert table.start() == "started"
    assert table.start() == "already_started"
    assert table.lobby_state()["state"] == "playing"
    assert table.lobby_state()["invite"] == ""
    assert starts == [{"hand_no": 1, "names": ["Alice", "Bob"],
                       "stacks": [1000, 1000], "sb": 25, "bb": 50,
                       "structure": "No-Limit", "button": 0}]


def test_host_can_unready_and_cannot_start_with_ten_players(controller):
    table, fake, _local, _changes = controller
    table.host()
    for seat in range(1, 10):
        remote = _identity()
        cid = _admit(table, fake, remote, name=f"Player {seat}", cid=f"remote{seat}")
        fake.deliver(cid, _packet("ready", remote, {"ready": True}))
    assert not table.lobby_state()["can_start"]
    assert table.start() == "not_ready"
    table.set_ready(False)
    assert not table.lobby_state()["players"][0]["ready"]


@pytest.mark.parametrize("code", ["invalid", base64.b32encode(bytes(29)).decode(),
                                     invite.generate_room_code(bytes(range(32)))])
def test_invalid_legacy_or_addressless_join_opens_no_socket(controller, code):
    table, fake, _local, _changes = controller
    with pytest.raises(ValueError):
        table.join(code)
    assert not fake.connected
    assert table.session is None


def test_join_pins_before_socket_and_does_not_ready_before_roster(controller):
    table, fake, local, _changes = controller
    fake.eager = _packet("game_start", _identity(), {
        "table_settings": {"deal_policy": Session.DEAL_POLICY_BG},
        "seat_order": ["host", "you"],
    })
    host, _code = _join_authenticated(table, fake, local)
    assert table.session.state == "LOBBY"
    assert table.lobby_state()["state"] == "connecting"
    assert "player_info" in [m["type"] for _cid, m in fake.sent]
    assert "ready" not in [m["type"] for _cid, m in fake.sent]
    with pytest.raises(RuntimeError, match="connecting"):
        table.set_ready(True)
    _joined_roster(fake, host, local)
    state = table.lobby_state()
    assert state["state"] == "lobby"
    assert state["invite"] == ""
    assert state["players"][1]["is_you"]
    table.set_ready(True)
    assert fake.last("ready") == {"type": "ready", "payload": {"ready": True}}
    with pytest.raises(ValueError, match="Only the host"):
        table.start()


def test_host_disconnect_closes_joiner_without_promoting_it(controller):
    table, fake, local, _changes = controller
    host, _code = _join_authenticated(table, fake, local)
    _joined_roster(fake, host, local)
    for callback in fake.disconnects:
        callback("host-hop")
    assert table.session.terminal_state == Session.HOST_LOST
    assert not table.session.is_host
    assert table.lobby_state()["state"] == "closed"
    assert "host disconnected" in table.lobby_state()["error"]


def test_early_disconnect_explains_that_the_room_may_have_started(controller):
    table, fake, _local, _changes = controller
    code = invite.generate_room_code(_identity().public_key_bytes(),
                                     public_address=("192.168.1.23", 45678))
    table.join(code)
    for callback in fake.disconnects:
        callback("host-hop")
    assert table.lobby_state()["state"] == "closed"
    assert "closed or already started" in table.lobby_state()["error"]
    assert table.session.terminal_state == Session.HOST_LOST


def test_signed_clock_rejection_explains_clock_settings(controller):
    table, fake, _local, _changes = controller
    code = invite.generate_room_code(_identity().public_key_bytes(),
                                     public_address=("192.168.1.23", 45678))
    table.join(code)
    for callback in fake.disconnects:
        callback("host-hop", "clock")
    assert "Clocks differ" in table.lobby_state()["error"]
    assert "date and time" in table.lobby_state()["error"]


def test_playing_host_refuses_new_admission_without_changing_the_table(controller, monkeypatch):
    table, fake, _local, _changes = controller
    table.host()
    remote = _identity()
    cid = _admit(table, fake, remote)
    fake.deliver(cid, _packet("ready", remote, {"ready": True}))
    monkeypatch.setattr(Session, "start_p2p_hand", lambda self, **kw: None)
    assert table.start() == "started"
    previous = table.lobby_state()
    previous_messages = len(fake.sent)
    fake.deliver("latecomer", _packet("admission_hello", _identity(),
                                     {"client_nonce": bytes(32).hex()}))
    assert fake.operations[-1] == "disconnect"
    assert len(fake.sent) == previous_messages, "late admission received a challenge"
    assert table.lobby_state() == previous


class ManualWatchdog:
    def __init__(self, interval, callback, args):
        self.interval = interval
        self.callback = callback
        self.args = args
        self.daemon = False
        self.cancelled = False
        self.started = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # Deliberately allow a canceled callback to fire. Its generation and
        # state checks must still make it harmless after leave/rejoin.
        self.callback(*self.args)


@pytest.fixture
def watchdogs(monkeypatch):
    timers = []

    def make(interval, callback, args):
        timer = ManualWatchdog(interval, callback, args)
        timers.append(timer)
        return timer

    monkeypatch.setattr(lan_session.threading, "Timer", make)
    return timers


def test_join_startup_timeout_closes_and_releases_transport(controller, watchdogs):
    table, fake, _local, _changes = controller
    code = invite.generate_room_code(_identity().public_key_bytes(),
                                     public_address=("192.168.1.23", 45678))
    table.join(code)
    session = table.session
    watchdog = watchdogs[0]
    assert watchdog.started and watchdog.daemon and watchdog.interval == 15.0
    watchdog.fire()
    state = table.lobby_state()
    assert state["state"] == "closed"
    assert "did not finish connecting" in state["error"]
    assert code not in state["error"]
    assert table.session is None
    assert session.terminal_state == Session.LOCAL_SHUTDOWN
    assert fake.operations[-1] == "stop"
    assert not fake.messages and not fake.disconnects
    assert watchdog.cancelled


def test_roster_cancels_watchdog_and_late_timer_cannot_close_joined_room(controller, watchdogs):
    table, fake, local, _changes = controller
    host, _code = _join_authenticated(table, fake, local)
    _joined_roster(fake, host, local)
    watchdog = watchdogs[0]
    assert watchdog.cancelled
    previous = table.lobby_state()
    watchdog.fire()
    assert table.lobby_state() == previous
    assert table.session.terminal_state is None


def test_leave_cancels_watchdog_and_stale_expiry_does_not_touch_new_room(controller, watchdogs):
    table, fake, _local, _changes = controller
    code = invite.generate_room_code(_identity().public_key_bytes(),
                                     public_address=("192.168.1.23", 45678))
    table.join(code)
    watchdog = watchdogs[0]
    table.close()
    assert watchdog.cancelled
    table.host()
    previous = table.lobby_state()
    session = table.session
    watchdog.fire()
    assert table.session is session
    assert table.lobby_state() == previous


def test_failed_join_cleans_up_without_exposing_invite(controller, monkeypatch):
    table, fake, _local, _changes = controller
    code = invite.generate_room_code(_identity().public_key_bytes(),
                                     public_address=("192.168.1.23", 45678))
    monkeypatch.setattr(fake, "connect", lambda _address: (_ for _ in ()).throw(
        ConnectionError("connection refused")))
    with pytest.raises(ConnectionError):
        table.join(code)
    assert table.session is None
    assert table.lobby_state()["state"] == "closed"
    assert code not in table.lobby_state()["error"]
    assert fake.operations[-1] == "stop"


def test_close_rehost_and_late_callbacks_do_not_reanimate_old_room(controller):
    table, fake, _local, _changes = controller
    table.host()
    old_session = table.session
    old_roster = old_session.on_player_list_changed
    old_game = old_session.on_game_start
    old_message = fake.messages[0]
    # stop() may join a dispatch callback. It must run with no controller
    # metadata lock held, or this worker cannot finish.
    def dispatch_during_stop():
        worker = threading.Thread(target=lambda: old_roster([]))
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive(), "close held metadata while joining dispatch"
    fake.stop_callback = dispatch_during_stop
    table.close()
    assert old_session.terminal_state == Session.LOCAL_SHUTDOWN
    assert table.session is None
    table.host()
    current = table.session
    old_roster([])
    old_game({})
    old_message("stranger", {"type": "game_start"})
    assert table.session is current
    assert table.lobby_state()["state"] == "lobby"
    assert current.terminal_state is None
    table.close()
    table.close()
    assert table.lobby_state()["players"] == []


def test_lan_refuses_crypto_downgrade():
    with pytest.raises(ValueError, match="Bayer-Groth"):
        lan_session.LanTable("Alice", {"deal_policy": Session.DEAL_POLICY_DETECTION})


def test_old_preview_cannot_start_a_reliability_table(controller):
    table, fake, _local, _changes = controller
    table.host()
    remote = _identity()
    cid = _admit(table, fake, remote, capable=False)
    fake.deliver(cid, _packet("ready", remote, {"ready": True}))
    table.session.reliability_tick()
    assert not table.lobby_state()["can_start"]
    assert "version and clocks: Bob" in table.lobby_state()["error"]
    assert table.start() == "not_ready"


def test_leave_stops_the_owned_reliability_scheduler(controller):
    table, _fake, _local, _changes = controller
    table.host()
    thread = table._ticker_thread
    assert thread.is_alive()
    table.close()
    assert not thread.is_alive()


@pytest.mark.parametrize("settings", [{"sb": 0}, {"bb": 25}, {"stack": 49},
                                      {"bb": True}, {"structure": "Other"}])
def test_invalid_table_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        lan_session.LanTable("Alice", settings)
