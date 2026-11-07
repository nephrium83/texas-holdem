"""Legacy onboarding still obeys identity, ownership and honest-save rules."""
from types import SimpleNamespace

import pytest

from holdem import onboarding, settings


class Transport:
    def __init__(self):
        self.calls = []
    def reset_callbacks(self):
        self.calls.append("reset")
    def stop(self):
        self.calls.append("stop")
    def disconnect(self, cid):
        self.calls.append(("disconnect", cid))


def _attempt():
    session = SimpleNamespace(local_conn_id="my-authenticated-id", on_game_start=object(),
                              on_player_list_changed=object())
    holder = SimpleNamespace(_session=session)
    transport = Transport()
    return onboarding._JoinAttempt(session, transport, holder), session, transport, holder


def test_game_start_uses_authenticated_local_identity_not_the_outbound_socket_id():
    attempt, session, _, _ = _attempt()
    payload = {"seat_order": ["host", "other", session.local_conn_id]}
    assert onboarding._local_start_seat(session, payload) == 2


@pytest.mark.parametrize("order", [None, [], ["host"], ["host", "other"],
                                  ["host", "my-authenticated-id", "my-authenticated-id"],
                                  [None, "my-authenticated-id"], ["", "my-authenticated-id"]])
def test_invalid_or_missing_local_roster_refuses_instead_of_defaulting_to_seat_one(order):
    _, session, _, _ = _attempt()
    with pytest.raises(ValueError):
        onboarding._local_start_seat(session, {"seat_order": order})


def test_cancel_is_idempotent_and_suppresses_previously_queued_ui():
    attempt, session, transport, holder = _attempt()
    seen = []
    queued = attempt.guard(lambda: seen.append("updated destroyed UI"))
    attempt.cancel()
    attempt.cancel()
    queued()
    assert not seen
    assert holder._session is None
    assert session.on_game_start is None and session.on_player_list_changed is None
    assert transport.calls == ["reset", "stop"]
    assert not attempt.active


def test_late_connection_is_closed_without_beginning_admission_and_orphan_loop_is_cleaned():
    attempt, _, transport, _ = _attempt()
    seen = []
    attempt.cancel()
    assert not attempt.accept_connection("late-old-socket", lambda cid: seen.append(cid))
    attempt.cleanup_late()
    assert not seen
    assert transport.calls == ["reset", "stop", ("disconnect", "late-old-socket"), "reset", "stop"]


def test_cancel_and_late_result_cannot_stop_a_newer_session():
    attempt, _, transport, holder = _attempt()
    newer = object()
    holder._session = newer
    attempt.cancel()
    assert not attempt.accept_connection("old-socket", lambda cid: pytest.fail("admission started"))
    attempt.cleanup_late()
    assert holder._session is newer
    assert transport.calls == [("disconnect", "old-socket")]


def test_handoff_keeps_live_session_and_transport_while_releasing_dialog_callbacks():
    attempt, session, transport, holder = _attempt()
    queued = attempt.guard(lambda: pytest.fail("late UI update after handoff"))
    assert attempt.accept_connection("socket", lambda cid: None)
    attempt.handoff()
    attempt.cancel()
    queued()
    assert holder._session is session
    assert not transport.calls
    assert session.on_game_start is None and session.on_player_list_changed is None


def test_saved_table_form_values_are_used_without_wiping_other_existing_rules():
    assert settings.save({}, {"rit": "Never", "rabbit": False})
    assert onboarding._save_table_defaults({"players": 4, "sb": 25, "bb": 50, "clock_base": 40})
    saved = settings.load()["last_table"]
    assert {key: saved[key] for key in ("players", "sb", "bb", "clock_base")} == {
        "players": 4, "sb": 25, "bb": 50, "clock_base": 40}
    assert saved["rit"] == "Never" and not saved["rabbit"]
    before = settings.config_path().read_bytes()
    with pytest.raises(ValueError):
        onboarding._save_table_defaults({"sb": 50, "bb": 25})
    assert settings.config_path().read_bytes() == before


def test_host_invite_omits_an_unconfigured_private_fleet_relay(monkeypatch):
    calls = []
    transport = SimpleNamespace(
        get_clock=lambda: object(), reset_callbacks=lambda: None,
        on_message=lambda callback: None, on_disconnect=lambda callback: None,
        start_host=lambda: ("127.0.0.1", 12345), get_public_address=lambda: None,
        announce=lambda *args: None)
    session = SimpleNamespace(add_local_player=lambda *args, **kwargs: None,
                              handle_message=lambda *args: None, handle_disconnect=lambda *args: None)
    identity = SimpleNamespace(peer_id=lambda: "host-id", public_key_bytes=lambda: b"k" * 32,
                               x25519_public_key_bytes=lambda: b"x" * 32)
    monkeypatch.setattr(onboarding, "_transport", transport)
    monkeypatch.setattr(onboarding._native_crypto, "initialize", lambda: None)
    monkeypatch.setattr(onboarding._identity, "load_or_create", lambda: identity)
    monkeypatch.setattr(onboarding._admission, "HostAdmission", lambda **kwargs: object())
    monkeypatch.setattr(onboarding._session_mod, "Session", lambda **kwargs: session)
    monkeypatch.setattr(onboarding._p2p_pkg, "_session", None)
    monkeypatch.setattr(onboarding, "generate_room_code", lambda **kwargs: calls.append(kwargs) or "code")
    monkeypatch.setattr(onboarding.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    class ReachedWindow(Exception):
        pass
    def window(*args):
        raise ReachedWindow()
    monkeypatch.setattr(onboarding.tk, "Toplevel", window)
    flow = onboarding.OnboardingFlow.__new__(onboarding.OnboardingFlow)
    flow.root, flow.nickname = object(), "Test host"
    with pytest.raises(ReachedWindow):
        flow._create_game_dialog()
    assert len(calls) == 1
    assert calls[0]["relay_address"] is None



def test_cancel_can_join_a_worker_already_posting_ui_without_tk_servicing_it():
    import ast
    import inspect
    import queue
    import threading

    attempt, _, transport, _ = _attempt()
    reached, resume_tk = threading.Event(), threading.Event()
    owner = threading.get_ident()
    callbacks, errors = [], []

    class UIQueue(queue.SimpleQueue):
        def put(self, fn):
            super().put(fn)
            reached.set()

    class Window:
        def after(self, delay, fn):
            if threading.get_ident() != owner:
                # Model cross-thread Tk.after's wait for the mainloop. The
                # mainloop cannot resume while cancel joins this worker.
                reached.set()
                resume_tk.wait(2)
            callbacks.append(fn)

    # Exercise the actual production nested seam without opening real
    # sockets or a display. Its closed-over names become this test namespace.
    tree = ast.parse(inspect.getsource(onboarding))
    seam = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == '_ui_post')
    namespace = dict(attempt=attempt, win=Window(), threading=threading,
                     tk_thread=owner, ui_queue=UIQueue(), tk=onboarding.tk, log=onboarding.log)
    exec(compile(ast.Module(body=[seam], type_ignores=[]), '<actual join UI seam>', 'exec'), namespace)
    def post():
        try:
            namespace['_ui_post'](lambda: pytest.fail('stale UI update'))
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=post)
    def stop():
        worker.join(0.3)
        assert not worker.is_alive(), 'cancellation waited on Tk-dependent dispatch'
    transport.stop = stop
    worker.start()
    try:
        assert reached.wait(2)
        attempt.cancel()
        assert not errors
        assert not callbacks, 'worker invoked Tk directly'
        while not namespace['ui_queue'].empty():
            namespace['ui_queue'].get_nowait()()  # cancelled guard drops it
    finally:
        resume_tk.set()
        worker.join(2)
