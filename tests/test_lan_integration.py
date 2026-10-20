"""LAN delivery acceptance through separate shipped desktop processes.

The host, each joining seat, admission handshake, Bayer-Groth deal and
betting all use production code. The test speaks the same localhost JSON
protocol as Godot; it never routes peer traffic or fabricates a Session.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
import shutil
import subprocess
import time

import pytest

from crypto_gate import require_crypto
from lan_peer import REPO_ROOT, SidecarPeer, listener_closed, wait_until


_GODOT = (os.environ.get("GODOT_BIN") or shutil.which("godot4")
          or shutil.which("godot"))


def _ok(peer, command, payload=None):
    result = peer.command(command, payload)
    assert result["ok"], peer.context(f"{command} rejected: {result!r}")
    return result


def _host_and_join(peers):
    host = peers[0]
    _ok(host, "host_lan", {"nickname": host.name})
    lobby = host.wait(lambda snap: bool(snap["lobby"].get("invite")),
                      description="host invite")
    invite = lobby["lobby"]["invite"]
    for peer in peers[1:]:
        _ok(peer, "join_lan", {"nickname": peer.name, "invite": invite})
    for peer in peers:
        peer.wait(lambda snap: len(snap["lobby"]["players"]) == len(peers),
                  description=f"all {len(peers)} real players admitted")
    return invite


def _ready_and_start(peers):
    for peer in peers:
        _ok(peer, "set_ready", {"ready": True})
    peers[0].wait(lambda snap: snap["lobby"]["can_start"],
                  description="all real players ready")
    result = _ok(peers[0], "start_game")
    assert result["verdict"] == "started"
    for peer in peers:
        peer.wait(lambda snap: snap["phase"] == "betting",
                  description="Bayer-Groth deal reached betting")


def _public(snap):
    return {
        key: snap.get(key)
        for key in ("phase", "hand_num", "street", "board", "pot",
                    "button", "sb_seat", "bb_seat", "action_on")
    } | {"seats": [{key: seat[key] for key in
                    ("seat", "name", "stack", "bet", "folded", "all_in")}
                   for seat in snap["seats"]]}


def _synchronized(peers, *, hand, phase=None):
    def agreed():
        snaps = [peer.snapshot for peer in peers]
        if any(snap.get("hand_num") != hand
               or snap.get("phase") not in ("betting", "settled")
               or (phase is not None and snap.get("phase") != phase)
               or (snap.get("phase") == "betting"
                   and snap.get("action_on", -1) < 0)
               for snap in snaps):
            return False
        return all(_public(snap) == _public(snaps[0]) for snap in snaps[1:])

    try:
        wait_until(agreed, description=f"hand {hand} replicas agree")
    except AssertionError:
        pytest.fail("\n".join(peer.context(f"hand {hand} did not agree")
                              for peer in peers))
    return [peer.snapshot for peer in peers]


def _check_private_cards(snaps, n):
    # Every process really occupies its own seat and receives only that
    # seat's cards. A host snapshot copied to joiners fails both assertions.
    seats = [snap["seat"] for snap in snaps]
    assert sorted(seats) == list(range(n))
    holes = [card for snap in snaps for card in snap["you"]["hole"]]
    assert len(holes) == len(set(holes)) == 2 * n
    for snap in snaps:
        assert snap["deal_policy"] == "bayer-groth-v1"
        assert snap["proofs_verified"] >= n - 1
        assert len(snap["you"]["hole"]) == 2
        assert all("hole" not in seat for seat in snap["seats"])
        allowed = set(snap["you"]["hole"]) | set(snap["board"])
        assert _card_strings(snap) <= allowed, "snapshot leaked cards outside the public board and own hand"


def _card_strings(value):
    if isinstance(value, str):
        if (len(value) in (2, 3) and value[-1] in "cdhs"
                and value[:-1] in {"2", "3", "4", "5", "6", "7", "8", "9",
                                   "10", "J", "Q", "K", "A"}):
            return {value}
    elif isinstance(value, dict):
        return set().union(*(_card_strings(item) for item in value.values()))
    elif isinstance(value, list):
        return set().union(*(_card_strings(item) for item in value))
    return set()


def _play_checkdown(peers, hand):
    for _ in range(40):
        snaps = _synchronized(peers, hand=hand)
        snap = snaps[0]
        if snap["phase"] == "settled":
            assert len(snap["board"]) == 5
            assert sum(seat["stack"] for seat in snap["seats"]) == 1000 * len(peers)
            assert snap["pot"] == 0
            return snaps
        _check_private_cards(snaps, len(peers))
        actor = snap["action_on"]
        owner = next(peer for peer, state in zip(peers, snaps)
                     if state["seat"] == actor)
        assert "legal" in owner.snapshot["you"]
        result = _ok(owner, "check_call")
        assert result["verdict"] == "applied"
    pytest.fail(f"hand {hand} did not settle after 40 legal check/call actions")


def _advance_all(peers):
    # Settlement snapshots can arrive before the other peers' matching
    # signed reports. Retry a genuinely not-ready seat without accidentally
    # advancing the peers that already accepted this request twice.
    pending = list(peers)
    deadline = time.monotonic() + 35
    while pending:
        for peer in list(pending):
            result = peer.command("next_hand")
            assert result["verdict"] in ("started", "not_ready"), result
            if result["verdict"] == "started":
                pending.remove(peer)
        if pending:
            assert time.monotonic() < deadline, "matching settlement reports did not arrive"
            time.sleep(0.025)


@pytest.mark.parametrize("count", [2, 3], ids=["two-processes", "three-processes"])
def test_separate_lan_peers_play_three_consistent_private_hands(tmp_path, count):
    require_crypto()
    with ExitStack() as stack:
        peers = [stack.enter_context(SidecarPeer(tmp_path, f"Player{i}"))
                 for i in range(count)]
        _host_and_join(peers)
        _ready_and_start(peers)
        for hand in range(1, 4):
            first = _synchronized(peers, hand=hand, phase="betting")
            _check_private_cards(first, count)
            assert [seat["name"] for seat in first[0]["seats"]] == [
                peer.name for peer in peers]
            _play_checkdown(peers, hand)
            if hand < 3:
                # No host-only fixture broadcasts the next hand. Each
                # real seat requests the existing consensus transition.
                _advance_all(peers)


def test_lan_lobby_readiness_leave_and_rehost_clean_up(tmp_path):
    require_crypto()
    from holdem.p2p.invite import parse_room_code

    with SidecarPeer(tmp_path, "Host") as host, SidecarPeer(tmp_path, "Guest") as guest:
        _ok(host, "host_lan", {"nickname": "Host"})
        lobby = host.wait(lambda snap: bool(snap["lobby"].get("invite")),
                          description="host invite")
        old_invite = lobby["lobby"]["invite"]
        parsed = parse_room_code(old_invite)
        lan_address, lan_port = parsed["public_ip"], parsed["public_port"]
        del parsed  # The parsed admission capability is not test evidence.
        assert lan_address and lan_address != "0.0.0.0"
        assert lan_port > 0
        assert not host.snapshot["lobby"]["can_start"]
        assert host.command("start_game")["ok"] is False

        _ok(guest, "join_lan", {"nickname": "Guest", "invite": old_invite})
        host.wait(lambda snap: len(snap["lobby"]["players"]) == 2,
                  description="guest admitted")
        assert host.command("start_game")["ok"] is False
        assert guest.command("start_game")["ok"] is False
        assert all(peer.snapshot["phase"] == "lobby" for peer in (host, guest))

        _ok(guest, "set_ready", {"ready": True})
        host.wait(lambda snap: snap["lobby"]["can_start"],
                  description="ready guest enables host")
        _ok(guest, "set_ready", {"ready": False})
        host.wait(lambda snap: not snap["lobby"]["can_start"],
                  description="unready guest disables host")

        _ok(guest, "leave_table")
        guest.wait(lambda snap: snap["lobby"]["mode"] == "menu",
                   description="guest returns to menu")
        host.wait(lambda snap: len(snap["lobby"]["players"]) == 1,
                  description="departed guest removed")
        assert not host.snapshot["lobby"]["can_start"]

        _ok(guest, "join_lan", {"nickname": "Guest", "invite": old_invite})
        host.wait(lambda snap: len(snap["lobby"]["players"]) == 2,
                  description="guest rejoins the live host")
        _ok(host, "leave_table")
        host.wait(lambda snap: snap["lobby"]["mode"] == "menu",
                  description="host returns to menu")
        guest.wait(lambda snap: snap["lobby"]["state"] == "closed",
                   description="host loss is shown to guest")
        wait_until(lambda: listener_closed(lan_address, lan_port),
                   description="old LAN listener closes")

        _ok(host, "host_lan", {"nickname": "Host again"})
        fresh = host.wait(lambda snap: bool(snap["lobby"].get("invite")),
                          description="fresh host invite")
        changed_invite = fresh["lobby"]["invite"] != old_invite
        assert changed_invite, "rehosting must create a fresh room capability"
        assert len(fresh["lobby"]["players"]) == 1
        assert fresh["lobby"]["players"][0]["name"] == "Host again"
        assert not fresh["lobby"]["can_start"]


def test_lan_peer_process_loss_is_visible_and_survivor_can_leave(tmp_path):
    require_crypto()
    with SidecarPeer(tmp_path, "Host") as host, SidecarPeer(tmp_path, "Guest") as guest:
        _host_and_join([host, guest])
        _ready_and_start([host, guest])
        _synchronized([host, guest], hand=1, phase="betting")
        # Abruptly close only this test's owned host process. A disconnected
        # socket must become a visible table state, not leave playable buttons.
        host.close()
        closed = guest.wait(lambda snap: snap["lobby"]["state"] == "closed",
                            description="lost host is visible during a hand")
        assert closed.get("terminal") or closed["lobby"].get("error")
        assert "legal" not in closed.get("you", {})
        _ok(guest, "leave_table")
        guest.wait(lambda snap: snap["lobby"]["mode"] == "menu",
                   description="survivor can return to menu")


def test_late_join_closes_visibly_without_disrupting_the_playing_table(tmp_path):
    require_crypto()
    with ExitStack() as stack:
        host, guest, late = [stack.enter_context(SidecarPeer(tmp_path, name))
                             for name in ("Host", "Guest", "Late")]
        saved_invite = _host_and_join([host, guest])
        _ready_and_start([host, guest])
        before = _synchronized([host, guest], hand=1, phase="betting")
        expected = _public(before[0])
        # The invite was legitimately shared before Start. Opening it after
        # Start must report the closed admission window rather than wait
        # indefinitely or add a seat to the frozen playing roster.
        late.command("join_lan", {"nickname": "Late", "invite": saved_invite})
        refused = late.wait(lambda snap: snap["lobby"]["state"] == "closed",
                            timeout=20, description="late join is visibly refused")
        assert refused["lobby"].get("error")
        assert "legal" not in refused.get("you", {})
        assert "hole" not in refused.get("you", {})
        unchanged = _synchronized([host, guest], hand=1, phase="betting")
        assert all(_public(snap) == expected for snap in unchanged)
        assert all(len(snap["lobby"]["players"]) == 2 for snap in unchanged)
        _play_checkdown([host, guest], 1)
        _ok(late, "leave_table")
        late.wait(lambda snap: snap["lobby"]["mode"] == "menu",
                  description="refused late joiner can return to menu")


def test_three_peer_lobby_departure_updates_every_remaining_roster(tmp_path):
    require_crypto()
    with ExitStack() as stack:
        host, guest, third = peers = [
            stack.enter_context(SidecarPeer(tmp_path, name))
            for name in ("Host", "Guest", "Third")]
        invite = _host_and_join(peers)
        for peer in peers:
            _ok(peer, "set_ready", {"ready": True})
        host.wait(lambda snap: snap["lobby"]["can_start"],
                  description="three-player table ready")
        _ok(third, "leave_table")
        for peer in (host, guest):
            departed = peer.wait(
                lambda snap: [p["name"] for p in snap["lobby"]["players"]]
                == ["Host", "Guest"],
                description="third player removed from remaining roster")
            assert len(departed["lobby"]["players"]) == 2
        host.wait(lambda snap: snap["lobby"]["can_start"],
                  description="remaining roster completed fresh version and clock checks")
        _ok(third, "join_lan", {"nickname": "Third returns", "invite": invite})
        for peer in peers:
            returned = peer.wait(
                lambda snap: [p["name"] for p in snap["lobby"]["players"]]
                == ["Host", "Guest", "Third returns"],
                description="returning third player replaces the departed roster entry")
            assert [p["seat"] for p in returned["lobby"]["players"]] == [0, 1, 2]
        assert not host.snapshot["lobby"]["can_start"]


def test_desktop_menu_can_still_start_play_and_leave_a_local_bot_game(tmp_path):
    require_crypto()
    with SidecarPeer(tmp_path, "Practice") as peer:
        _ok(peer, "local_game", {"nickname": "Practice"})
        lobby = peer.wait(lambda snap: snap["lobby"]["mode"] == "local",
                          description="local bot table")
        assert lobby["lobby"]["can_start"]
        assert len(lobby["lobby"]["players"]) == 3
        _ok(peer, "start_game")
        for _ in range(40):
            snap = peer.wait(
                lambda state: state.get("phase") == "settled"
                or state.get("turn", {}).get("state") == "your_turn",
                description="local bots progress to human decision or settlement")
            assert snap["deal_policy"] == "bayer-groth-v1"
            assert snap["proofs_verified"] >= 2
            if snap["phase"] == "settled":
                assert sum(seat["stack"] for seat in snap["seats"]) == 3000
                break
            _ok(peer, "check_call")
        else:
            pytest.fail("local bot hand did not settle after 40 human decisions")
        _ok(peer, "leave_table")
        menu = peer.wait(lambda state: state["lobby"]["mode"] == "menu",
                         description="local table returns to main menu")
        assert menu["seats"] == []
        assert "hole" not in menu["you"]


@pytest.mark.skipif(_GODOT is None, reason="godot binary not found in PATH")
def test_godot_host_join_controls_play_three_real_lan_hands(tmp_path):
    """Use the actual menu, invite field, Ready and betting controls."""
    require_crypto()
    with SidecarPeer(tmp_path, "Host") as host, SidecarPeer(tmp_path, "Guest") as guest:
        run = subprocess.run(
            [_GODOT, "--headless", "--path", str(REPO_ROOT / "godot"),
             "-s", "res://sidecar/lan_e2e.gd", "--",
             f"--host-sidecar-port={host.port}",
             f"--join-sidecar-port={guest.port}"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=65,
        )
        lines = run.stdout.splitlines()
        prefix = "LAN_E2E_SUMMARY "
        summaries = [line.removeprefix(prefix).strip() for line in lines
                     if line.startswith(prefix)]
        assert run.returncode == 0, (
            f"Godot LAN acceptance exited {run.returncode}; "
            f"stdout={run.stdout}; stderr={run.stderr}")
        assert len(summaries) == 1, "Godot did not report one LAN acceptance summary"
        summary = json.loads(summaries[0])
        assert not summary.get("error"), summary
        assert summary["hands_settled"] == [3, 3], summary
        assert all(checks > 0 for checks in summary["face_up_checks"]), summary
        assert summary["roster_checks"] == [True, True], summary
        assert summary["returned_to_menu"] == [True, True], summary
        commands = summary["commands"]
        assert commands["host_lan"] == commands["join_lan"] == 1, summary
        assert commands["set_ready"] >= 1, summary
        assert commands["start_game"] == 1, summary
        assert commands["check_call"] > 0 and commands["next_hand"] >= 4, summary
        assert commands["leave_table"] == 2, summary


@pytest.mark.skipif(_GODOT is None, reason="godot binary not found in PATH")
def test_godot_practice_menu_still_plays_ten_complete_bot_hands(tmp_path):
    require_crypto()
    with SidecarPeer(tmp_path, "Player", seats=2, stack=100000, seed=7) as peer:
        run = subprocess.run(
            [_GODOT, "--headless", "--path", str(REPO_ROOT / "godot"),
             "-s", "res://sidecar/main_e2e.gd", "--",
             f"--sidecar-port={peer.port}", "--hands=10"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=50,
        )
        prefix = "E2E_SUMMARY "
        summaries = [line.removeprefix(prefix).strip()
                     for line in run.stdout.splitlines() if line.startswith(prefix)]
        assert run.returncode == 0, (
            f"Godot local-menu acceptance exited {run.returncode}; "
            f"stdout={run.stdout}; stderr={run.stderr}")
        assert len(summaries) == 1, "Godot did not report local-menu acceptance"
        summary = json.loads(summaries[0])
        assert not summary["error"], summary
        assert summary["hands_settled"] >= 10, summary
        assert summary["hands_voided"] == 0, summary
        assert summary["stack_carry_checks"] >= 9, summary
        assert summary["stack_carry_failures"] == [], summary
        assert summary["refused"] == [], summary
        assert summary["face_up_checks"] > 0, summary
        assert summary["face_up_failures"] == [], summary
        assert summary["lobby_names"] == ["Player (You)", "Bot 1"], summary
        assert all(summary["actions"][action] > 0
                   for action in ("check_call", "fold", "raise", "all_in")), summary
