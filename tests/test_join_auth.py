"""The SHIPPED join path's authentication ordering.

tests/test_admission.py proves the protocol. tests/test_three_peer_topology.py
proves a harness can drive it. Neither touches what the application actually
runs when a human clicks Join Game, and that is where the old
first-speaker-is-the-host assumption lived.

The driver was extracted from the Tk dialog into holdem/p2p/join_auth.py
precisely so these properties could be asserted without a GUI. Anything
asserted here is asserted against the code onboarding.py calls.
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem import onboarding as _onboarding
from holdem.p2p import admission as adm
from holdem.p2p import identity as _identity
from holdem.p2p import invite as inv
from holdem.p2p.join_auth import JoinAuthenticator, joiner_admission_from_invite
from holdem.p2p.session import Session

HOST_KEY = bytes(range(32))
IMPOSTOR = bytes([9]) * 32


class FakeTransport:
    """Records everything sent; optionally answers eagerly on connect."""

    delivers_verified_envelopes = True

    def __init__(self, eager=None):
        self.sent = []            # (conn_id, msg)
        self.disconnected = []
        self._on_message = None
        self._eager = eager       # msg delivered synchronously from connect()

    # -- the transport surface onboarding uses --------------------------
    def reset_callbacks(self):
        self._on_message = None

    def on_message(self, cb):
        self._on_message = cb

    def on_disconnect(self, cb):
        pass

    def connect(self, addr):
        conn_id = f"conn-{addr}"
        if self._eager is not None:
            # The reader starts as part of connect(). A caller that
            # registers callbacks afterwards loses this frame.
            if self._on_message is not None:
                self._on_message(conn_id, self._eager)
            else:
                self.lost_eager = True
        return conn_id

    def send(self, conn_id, msg):
        self.sent.append((conn_id, msg))

    def disconnect(self, conn_id):
        self.disconnected.append(conn_id)

    def broadcast(self, msg):
        self.sent.append((None, msg))

    def broadcast_except(self, exclude, msg):
        self.sent.append((None, msg))

    # -- helpers --------------------------------------------------------
    def types(self):
        return [m.get("type") for _cid, m in self.sent]


def _invite_pair():
    """A V2 invite plus a host policy built from the same secret."""
    code = inv.generate_room_code(host_pubkey=HOST_KEY)
    parsed = inv.parse_room_code(code)
    host = adm.HostAdmission(
        admission_secret=bytes.fromhex(parsed["admission_secret"]),
        host_pubkey=HOST_KEY,
        discovery_token=bytes.fromhex(parsed["discovery_token"]))
    return parsed, host


def _wire_up(eager=None):
    parsed, host = _invite_pair()
    t = FakeTransport(eager=eager)
    ja = joiner_admission_from_invite(parsed)
    sess = Session(is_host=False, nickname="J", avatar_b64="",
                   transport=t, joiner_admission=ja)
    sess.local_conn_id = "me"
    events = {"auth": [], "failed": []}
    auth = JoinAuthenticator(
        transport=t, session=sess, joiner_admission=ja, nickname="J",
        on_authenticated=lambda cid: events["auth"].append(cid),
        on_failed=lambda why: events["failed"].append(why))
    t.reset_callbacks()
    t.on_message(auth.route)
    return parsed, host, t, ja, sess, auth, events


def _challenge_from(host, conn_id, joiner_key, client_nonce, author=HOST_KEY):
    ch = host.on_hello(conn_id, joiner_key, client_nonce)
    return {"type": "admission_challenge", "pubkey": author.hex(),
            "payload": ch}, bytes.fromhex(ch["server_nonce"])


def _drive_to_accept(t, host, ja, auth, sess, accept_author=HOST_KEY):
    """hello -> challenge -> response -> accept, through the real driver."""
    auth.begin("conn-1")
    cn = bytes.fromhex(t.sent[-1][1]["client_nonce"])
    msg, sn = _challenge_from(host, "conn-1", _identity.public_key_bytes(), cn)
    auth.route("conn-1", msg)

    resp = t.sent[-1][1]
    ok = host.on_response("conn-1", _identity.public_key_bytes(),
                          bytes.fromhex(resp["client_nonce"]),
                          bytes.fromhex(resp["server_nonce"]),
                          bytes.fromhex(resp["mac"]))
    assert ok, "the host refused a response the shipped driver produced"
    auth.route("conn-1", {"type": "admission_accept",
                          "pubkey": accept_author.hex(),
                          "payload": host.accept_payload("conn-1")})
    return cn, sn


# ── ordering: callbacks before connect ────────────────────────────────────

def test_callbacks_are_registered_before_connect_in_the_shipped_path():
    """Structural, and deterministic rather than scheduler-dependent.

    The eager transport delivers a challenge synchronously from connect().
    If the join path dialed first and registered afterwards, that frame
    lands on no handler and the handshake can never complete -- which is
    exactly the race this ordering removes. Asserting it with a real
    message rather than by reading source keeps it true if the code moves.
    """
    parsed, host = _invite_pair()
    eager = {"type": "admission_challenge", "pubkey": HOST_KEY.hex(),
             "payload": {"client_nonce": "00" * 16,
                         "server_nonce": "11" * 16}}
    t = FakeTransport(eager=eager)
    ja = joiner_admission_from_invite(parsed)
    sess = Session(is_host=False, nickname="J", avatar_b64="",
                   transport=t, joiner_admission=ja)
    auth = JoinAuthenticator(transport=t, session=sess, joiner_admission=ja)

    t.reset_callbacks()
    t.on_message(auth.route)          # BEFORE connect, as onboarding does
    t.connect("host:1")

    assert not getattr(t, "lost_eager", False), (
        "a frame delivered during connect() reached no handler; callbacks "
        "must be registered before the socket is opened")


def test_onboarding_installs_the_driver_before_it_dials():
    """The dialog must not reintroduce the ordering the driver enforces."""
    src = inspect.getsource(_onboarding)
    install = src.index("_transport.on_message(authenticator.route)")
    dial = src.index("def _do_connect():")
    assert install < dial, (
        "onboarding registers its message callback after defining/using the "
        "connect path; the reader starts inside connect()")


# ── nothing is revealed before the accept ─────────────────────────────────

def test_no_player_info_before_the_accept():
    _parsed, host, t, ja, sess, auth, _ev = _wire_up()
    auth.begin("conn-1")
    assert t.types() == ["admission_hello"]

    cn = bytes.fromhex(t.sent[-1][1]["client_nonce"])
    msg, _sn = _challenge_from(host, "conn-1", _identity.public_key_bytes(), cn)
    auth.route("conn-1", msg)

    assert t.types() == ["admission_hello", "admission_response"], (
        f"identity leaked before the accept: {t.types()}")
    assert auth.player_info_sent is False


def test_a_valid_challenge_alone_does_not_authenticate():
    """The invariant that is invisible from the happy path."""
    _parsed, host, t, ja, sess, auth, ev = _wire_up()
    auth.begin("conn-1")
    cn = bytes.fromhex(t.sent[-1][1]["client_nonce"])
    msg, _sn = _challenge_from(host, "conn-1", _identity.public_key_bytes(), cn)
    auth.route("conn-1", msg)

    assert ja.verified_host is True, "precondition: the challenge was valid"
    assert sess._host_authenticated is False
    assert sess._host_conn_id == ""
    assert ev["auth"] == [], "the UI was told it was authenticated"


def test_the_accept_completes_and_only_then_sends_identity():
    _parsed, host, t, ja, sess, auth, ev = _wire_up()
    _drive_to_accept(t, host, ja, auth, sess)

    assert t.types() == ["admission_hello", "admission_response",
                         "player_info"]
    assert auth.player_info_sent is True
    assert sess._host_authenticated is True
    assert sess._host_conn_id == "conn-1"
    assert ev["auth"] == ["conn-1"], "Ready was not enabled after the accept"


def test_player_info_is_signed_by_the_key_that_was_admitted():
    """The host enforces this; the joiner must not send something else."""
    _parsed, host, t, ja, sess, auth, _ev = _wire_up()
    _drive_to_accept(t, host, ja, auth, sess)
    info = [m for _cid, m in t.sent if m.get("type") == "player_info"][0]
    assert info["pubkey"] == _identity.public_key_bytes().hex()
    assert host.admitted_key("conn-1") == _identity.public_key_bytes()


# ── failure paths leave the session closed ────────────────────────────────

def test_a_challenge_from_the_wrong_key_refuses_the_connection():
    _parsed, host, t, ja, sess, auth, ev = _wire_up()
    auth.begin("conn-1")
    cn = bytes.fromhex(t.sent[-1][1]["client_nonce"])
    msg, _sn = _challenge_from(host, "conn-1", _identity.public_key_bytes(),
                               cn, author=IMPOSTOR)
    auth.route("conn-1", msg)

    assert "admission_response" not in t.types(), "we answered an impostor"
    assert sess._host_authenticated is False
    assert t.disconnected == ["conn-1"]
    assert ev["failed"], "the dialog was not told the handshake failed"


def test_a_forged_accept_after_a_real_challenge_refuses_the_connection():
    _parsed, host, t, ja, sess, auth, ev = _wire_up()
    _drive_to_accept(t, host, ja, auth, sess, accept_author=IMPOSTOR)

    assert sess._host_authenticated is False
    assert auth.player_info_sent is False
    assert "player_info" not in t.types()
    assert ev["failed"]


def test_an_accept_for_a_different_transcript_refuses_the_connection():
    _parsed, host, t, ja, sess, auth, ev = _wire_up()
    auth.begin("conn-1")
    cn = bytes.fromhex(t.sent[-1][1]["client_nonce"])
    msg, _sn = _challenge_from(host, "conn-1", _identity.public_key_bytes(), cn)
    auth.route("conn-1", msg)
    auth.route("conn-1", {"type": "admission_accept",
                          "pubkey": HOST_KEY.hex(),
                          "payload": {"client_nonce": cn.hex(),
                                      "server_nonce": "22" * 16}})
    assert sess._host_authenticated is False
    assert auth.player_info_sent is False


# ── routing is not authentication ─────────────────────────────────────────

def test_admission_state_never_transfers_between_connections():
    """direct fails -> relay succeeds: the dead socket proves nothing.

    A challenge issued to the failed attempt must not be completable on its
    replacement, and traffic on the abandoned conn_id must be ignored
    outright.
    """
    _parsed, host, t, ja, sess, auth, _ev = _wire_up()
    auth.begin("conn-direct")

    # Routing falls back; a NEW connection becomes the real one.
    auth.begin("conn-relay")
    assert t.types() == ["admission_hello", "admission_hello"]

    # The challenge below is built with the CURRENT client nonce, so it is
    # valid in every respect EXCEPT which connection it arrived on. A
    # weaker version of this test reused the abandoned socket's nonce,
    # which on_challenge rejects on its own -- so the connection binding
    # was never actually exercised and a control that removed it fired
    # nothing. The only thing wrong here is the conn_id.
    live_cn = bytes.fromhex(t.sent[-1][1]["client_nonce"])
    stray, _sn = _challenge_from(host, "conn-direct",
                                 _identity.public_key_bytes(), live_cn)

    auth.route("conn-direct", stray)
    assert t.types() == ["admission_hello", "admission_hello"], (
        "an otherwise-valid challenge delivered on the abandoned connection "
        "produced a response; admission is not bound to one socket")
    assert sess._host_authenticated is False
    assert ja.verified_host is False, (
        "the abandoned socket advanced the live handshake's state")


def test_the_pin_comes_from_the_invite_not_from_routing():
    """Manual address override changes the address and nothing else."""
    parsed, _host = _invite_pair()
    ja = joiner_admission_from_invite(parsed)
    assert ja.host_pubkey == HOST_KEY
    # There is exactly one construction path, so no routing branch can
    # supply different authentication inputs.
    src = inspect.getsource(_onboarding)
    assert src.count("joiner_admission_from_invite(") == 1, (
        "more than one place builds the joiner pin; a routing branch could "
        "supply different authentication inputs")


def _code_only(module) -> str:
    """Source with comments stripped.

    A naive text scan matches the COMMENT that explains why the old
    behaviour was wrong, and then reports the old behaviour as present --
    which is how this test failed the first time it ran. Prose about a
    defect is not the defect.
    """
    out = []
    for line in inspect.getsource(module).split("\n"):
        head = line.split("#", 1)[0]
        if head.strip():
            out.append(head)
    return "\n".join(out)


def test_lan_and_relay_use_only_public_routing_data():
    """No capability may reach discovery or the relay operator."""
    code = _code_only(_onboarding)
    assert "find_peer(discovery_token" in code, "LAN discovery lost its token"
    assert "public_room_id(parsed)" in code, (
        "the relay is no longer given the public room id")
    assert "strip_code(code)" not in code, (
        "the whole invite is being handed to the relay again")
    assert "admission_secret" not in code.split("connect_via_relay")[1][:400], (
        "a capability is reaching the relay call")
    assert "args=(discovery_token" in code, (
        "multicast announce is no longer publishing only the public token")


# ── the UI-marshalling seam ───────────────────────────────────────────────

#: Methods that change what is on screen. Calling any of these from a
#: non-Tk thread is the undefined behaviour this seam exists to prevent.
_WIDGET_METHODS = frozenset({
    "config", "configure", "destroy", "grid", "pack", "place",
    "insert", "delete", "focus_set", "state",
})

#: Widget-bearing names in the Join dialog's scope. Kept explicit rather
#: than "anything with .config", so an unrelated object that happens to
#: expose config() does not produce a false accusation.
_WIDGET_NAMES = frozenset({
    "win", "status_lbl", "connect_btn", "code_entry", "addr_entry",
    "joined_lbl", "conn_info_lbl", "code_lbl", "btn",
})


def _join_worker_region() -> list:
    """Source lines of the Join dialog from the seam to the end of _do_connect.

    Everything in this span runs on the connection worker thread or on a
    transport callback -- never on Tk's thread.
    """
    src = inspect.getsource(_onboarding).split("\n")
    start = next(i for i, ln in enumerate(src) if "def _ui_post(fn):" in ln)
    dial = next(i for i in range(start, len(src))
                if src[i].strip().startswith("def _do_connect():"))
    end = next(i for i in range(dial + 1, len(src))
               if src[i].strip().startswith("threading.Thread(target=_do_connect"))
    return src[start:end]


def test_the_connection_worker_cannot_touch_widgets_directly():
    """Every UI effect off the Tk thread goes through the one seam.

    Not a style rule. This worker is the thread running admission, and
    Tkinter is not thread-safe: a direct widget call from here is not
    reliably a visible error, it is undefined behaviour that usually looks
    fine. When it does raise, it abandons the handshake midway and leaves
    transport and Session state half-established -- an unrelated-looking
    failure sitting directly on the authentication path.

    Done on the AST rather than on text. The first version tracked
    parenthesis depth by hand, double-counted every ``_ui_post(`` -- once
    for the name, once for the bracket -- and so never returned to depth
    zero, which made everything after the first call read as marshalled.
    A control that inserted a bare widget call fired nothing. Structure is
    what this test is about, so it reads structure.
    """
    tree = ast.parse(inspect.getsource(_onboarding))

    worker_fns = {"_do_connect", "_authenticated", "_auth_failed"}
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in worker_fns):
            continue

        # Everything lexically inside a _ui_post(...) call is marshalled.
        marshalled = set()
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_ui_post"):
                marshalled.update(id(n) for n in ast.walk(call))

        for call in ast.walk(node):
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)):
                continue
            if call.func.attr not in _WIDGET_METHODS:
                continue
            target = call.func.value
            name = getattr(target, "id", None)
            if name is None or name not in _WIDGET_NAMES:
                continue
            if id(call) not in marshalled:
                offenders.append(
                    f"{node.name}: {name}.{call.func.attr}() at line "
                    f"{call.lineno}")

    assert offenders == [], (
        "the connection worker mutates Tk widgets directly instead of going "
        f"through _ui_post: {offenders}")


def test_the_worker_region_does_not_bypass_the_seam():
    """win.after is the seam's implementation, not an alternative to it."""
    region = "\n".join(ln.split("#", 1)[0] for ln in _join_worker_region())
    body_start = region.index("def _ui_post(fn):")
    after_seam = region[region.index("win.after(0, fn)", body_start)
                        + len("win.after(0, fn)"):]
    assert "win.after(" not in after_seam, (
        "the worker calls win.after directly; there must be exactly one "
        "marshalling seam so it cannot be partially adopted")


@pytest.mark.parametrize("mtype", sorted(adm.ADMISSION_TYPES))
def test_admission_traffic_never_reaches_the_session(mtype):
    """The driver consumes the handshake; Session sees ordinary traffic only."""
    _parsed, _host, t, _ja, sess, auth, _ev = _wire_up()
    auth.begin("conn-1")
    seen = []
    sess.handle_message = lambda cid, m: seen.append(m)
    auth.route("conn-1", {"type": mtype, "pubkey": HOST_KEY.hex(),
                          "payload": {"client_nonce": "00" * 16,
                                      "server_nonce": "11" * 16}})
    assert seen == [], f"{mtype} was passed through to the Session"


# ── the HOST side of the shipped path ─────────────────────────────────────

def test_the_host_arms_its_session_before_it_opens_the_listener():
    """The host has the same race class the joiner path was restructured
    to remove, and until a review found it, only the joiner had a test.

    start_host() begins accepting connections the moment it returns. If the
    Session and its callbacks are installed afterwards, frames from an
    early peer reach _dispatch_handler with no callbacks registered and are
    dropped, with nothing on either side to retry them.
    """
    code = _code_only(_onboarding)
    host = code.index("def _create_game_dialog")
    body = code[host:code.index("def ", host + 10)]

    install = body.index("_transport.on_message(sess.handle_message)")
    listen = body.index("_transport.start_host()")
    assert install < listen, (
        "the host registers its message callback AFTER start_host(); the "
        "listener accepts connections in that window")

    policy = body.index("HostAdmission(")
    assert policy < listen, (
        "the admission policy is built after the listener is open")


def test_the_join_path_does_not_persist_the_invite():
    """A V2 code carries a live capability, so it must not reach disk.

    It was previously saved and silently truncated to 64 chars by the
    settings validator (a V2 code is 139). That cut the payload at byte 32
    and so happened to stop short of the secret at bytes 41-56 -- the only
    reason the capability was not already at rest. Widening the field to
    fix the truncation would have written the whole thing.
    """
    code = _code_only(_onboarding)
    assert "last_room_code" not in code, (
        "onboarding reads or writes last_room_code again; a V2 invite is a "
        "credential, not a convenience string")


def test_a_v2_secret_does_not_survive_a_settings_round_trip(tmp_path,
                                                            monkeypatch):
    """Functional backstop: check what actually lands on disk.

    Scans the SERIALIZED settings for the admission secret of a real
    invite, rather than trusting that no call site writes one.
    """
    from holdem import settings as cfg

    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(tmp_path))
    parsed = inv.parse_room_code(inv.generate_room_code(host_pubkey=HOST_KEY))
    cfg.save(cfg.defaults(cfg.CLIENT), cfg.defaults(cfg.TABLE_RULE))

    written = "".join(
        f.read_text(encoding="utf-8", errors="replace")
        for f in tmp_path.rglob("*") if f.is_file())
    assert parsed["admission_secret"] not in written, (
        "the admission secret was written to settings")
    assert parsed["host_pubkey"] not in written
