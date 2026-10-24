"""Direct Internet hosting acceptance with a fake router and real peers.

These are intentionally not evidence of real NAT traversal: the fake lease
publishes loopback. They verify the integration around the mapping boundary
while every admission, TCP message, crypto proof and poker hand is real.
"""
from __future__ import annotations

from contextlib import ExitStack

import pytest

from crypto_gate import require_crypto
from direct_internet_peer import DirectInternetPeer
from lan_peer import listener_closed, wait_until
from test_lan_integration import (
    _advance_all, _check_private_cards, _ok, _play_checkdown,
    _ready_and_start, _synchronized,
)


def _host_direct(host):
    from holdem.p2p import invite

    _ok(host, "host_internet", {"nickname": host.name})
    snap = host.wait(lambda value: bool(value["lobby"].get("invite")),
                     description="direct Internet invite")
    assert snap["lobby"]["network"] == "internet"
    code = snap["lobby"]["invite"]
    parsed = invite.parse_room_code(code)
    assert parsed["flags"] & invite.FLAG_DIRECT_INTERNET
    assert parsed["public_ip"] == "127.0.0.1"
    assert parsed["public_port"] > 0
    mapping = host.mappings()
    assert len(mapping["attempts"]) == len(mapping["leases"]) == 1
    assert mapping["leases"][0]["external_address"] == [
        parsed["public_ip"], parsed["public_port"]]
    assert mapping["leases"][0]["close_calls"] == 0
    return code


def _join_direct(peers, code):
    for peer in peers[1:]:
        _ok(peer, "join_lan", {"nickname": peer.name, "invite": code})
    for peer in peers:
        snap = peer.wait(lambda value: len(value["lobby"]["players"]) == len(peers),
                         description="real signed Internet roster admitted")
        assert snap["lobby"]["network"] == "internet"
    # Guests only initiate outbound TCP; they must not request router access.
    assert all(peer.mappings() == {"attempts": [], "leases": []}
               for peer in peers[1:])


def _wait_for_mapping_release(host, index=0):
    wait_until(lambda: host.mappings()["leases"][index]["close_calls"] >= 1,
               description="owned mapping is released")
    address, port = host.mappings()["leases"][index]["local_address"]
    wait_until(lambda: listener_closed(address, port),
               description="mapped TCP listener closes")


@pytest.mark.parametrize("count,hands", [(2, 10), (3, 3)],
                         ids=["two-peers-ten-hands", "three-peers-three-hands"])
def test_direct_invite_plays_real_consistent_private_hands(tmp_path, count, hands):
    require_crypto()
    with ExitStack() as stack:
        peers = [stack.enter_context(DirectInternetPeer(tmp_path, f"Player{i}"))
                 for i in range(count)]
        code = _host_direct(peers[0])
        _join_direct(peers, code)
        _ready_and_start(peers)
        for hand in range(1, hands + 1):
            first = _synchronized(peers, hand=hand, phase="betting")
            _check_private_cards(first, count)
            assert [seat["name"] for seat in first[0]["seats"]] == [
                peer.name for peer in peers]
            _play_checkdown(peers, hand)
            if hand < hands:
                _advance_all(peers)
        _ok(peers[0], "leave_table")
        _wait_for_mapping_release(peers[0])


def test_refused_mapping_leaves_no_invite_or_tcp_listener(tmp_path):
    require_crypto()
    with DirectInternetPeer(tmp_path, "Host", mapping_failure=True) as host:
        result = host.command("host_internet", {"nickname": host.name})
        assert not result["ok"]
        snap = host.wait(lambda value: value["lobby"]["state"] == "closed",
                         description="failed mapping is visible")
        assert snap["lobby"].get("error")
        has_invite = bool(snap["lobby"].get("invite"))
        assert not has_invite, "a failed mapping published an admission capability"
        assert "legal" not in snap.get("you", {})
        mapping = host.mappings()
        assert len(mapping["attempts"]) == 1
        assert mapping["leases"] == []
        attempt = mapping["attempts"][0]
        wait_until(lambda: listener_closed(attempt["local_ip"], attempt["local_port"]),
                   description="failed mapping releases its listener")
        _ok(host, "leave_table")
        assert host.snapshot["lobby"]["mode"] == "menu"


@pytest.mark.parametrize("playing", [False, True], ids=["lobby", "playing"])
def test_mapping_loss_closes_both_peers_visibly_and_releases_lease(tmp_path, playing):
    require_crypto()
    with DirectInternetPeer(tmp_path, "Host") as host, DirectInternetPeer(tmp_path, "Guest") as guest:
        peers = [host, guest]
        _join_direct(peers, _host_direct(host))
        if playing:
            _ready_and_start(peers)
            _synchronized(peers, hand=1, phase="betting")
        host.lose_mapping()
        for peer in peers:
            closed = peer.wait(lambda value: value["lobby"]["state"] == "closed",
                               description="lost Internet mapping is visible")
            assert closed["lobby"].get("error") or closed.get("terminal")
            assert "legal" not in closed.get("you", {})
            has_invite = bool(closed["lobby"].get("invite"))
            assert not has_invite, "a closed table retained an admission capability"
        _wait_for_mapping_release(host)
        for peer in peers:
            _ok(peer, "leave_table")
            assert peer.snapshot["lobby"]["mode"] == "menu"


def test_leave_rehost_ignores_the_old_mapping_callback(tmp_path):
    require_crypto()
    with DirectInternetPeer(tmp_path, "Host") as host:
        old_code = _host_direct(host)
        _ok(host, "leave_table")
        _wait_for_mapping_release(host)
        _ok(host, "host_internet", {"nickname": "Host again"})
        fresh = host.wait(lambda value: bool(value["lobby"].get("invite")),
                          description="fresh direct Internet invite")
        changed_invite = fresh["lobby"]["invite"] != old_code
        assert changed_invite, "rehost reused its admission capability"
        assert fresh["lobby"]["network"] == "internet"
        assert len(host.mappings()["leases"]) == 2
        callback = host.lose_mapping(0, "A stale callback must not close the replacement room")
        assert callback["finished"], "the stale callback did not complete"
        snap = host.snapshot
        assert snap["lobby"]["state"] == "lobby"
        unchanged_invite = snap["lobby"]["invite"] == fresh["lobby"]["invite"]
        assert unchanged_invite, "a stale lease callback changed the replacement invite"
        assert host.mappings()["leases"][1]["close_calls"] == 0
        _ok(host, "leave_table")
        _wait_for_mapping_release(host, 1)


def test_existing_lan_host_does_not_attempt_router_discovery(tmp_path):
    require_crypto()
    from holdem.p2p import invite

    with DirectInternetPeer(tmp_path, "Host") as host:
        _ok(host, "host_lan", {"nickname": host.name})
        snap = host.wait(lambda value: bool(value["lobby"].get("invite")),
                         description="ordinary LAN invite")
        parsed = invite.parse_room_code(snap["lobby"]["invite"])
        assert not parsed["flags"] & invite.FLAG_DIRECT_INTERNET
        assert snap["lobby"]["network"] == "lan"
        assert host.mappings() == {"attempts": [], "leases": []}
