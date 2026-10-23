"""Select a usable local address when virtual/disconnected adapters exist."""
import socket

from holdem.p2p import transport


def test_active_route_wins_over_a_disconnected_virtual_adapter(monkeypatch):
    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def connect(self, target):
            assert target == ("192.0.2.1", 9)

        def getsockname(self):
            return "192.168.1.22", 40000

    monkeypatch.setattr(socket, "socket", lambda *_: Probe())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [
        (socket.AF_INET, 0, 0, "", ("169.254.83.107", 0)),
    ])
    assert transport._get_lan_ip() == "192.168.1.22"


def test_no_route_skips_link_local_and_loopback_in_hostname_fallback(monkeypatch):
    def unavailable(*_):
        raise OSError("no route")

    monkeypatch.setattr(socket, "socket", unavailable)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [
        (socket.AF_INET, 0, 0, "", ("169.254.83.107", 0)),
        (socket.AF_INET, 0, 0, "", ("127.0.0.1", 0)),
        (socket.AF_INET, 0, 0, "", ("192.168.1.22", 0)),
    ])
    assert transport._get_lan_ip() == "192.168.1.22"


def test_only_disconnected_addresses_fall_back_to_loopback(monkeypatch):
    def unavailable(*_):
        raise OSError("no route")

    monkeypatch.setattr(socket, "socket", unavailable)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [])
    monkeypatch.setattr(socket, "gethostbyname", lambda *_: "169.254.83.107")
    assert transport._get_lan_ip() == "127.0.0.1"
