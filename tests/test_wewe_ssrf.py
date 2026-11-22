from __future__ import annotations

import socket

import pytest
import requests

import app.wewe_fetcher as fetcher_module
from app.wewe_fetcher import SafeFetcher


class _Response:
    def __init__(self, status_code=200, headers=None, content=b"ok"):
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/html"}
        self._content = content

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308)

    @property
    def is_permanent_redirect(self):
        return self.status_code in (301, 308)

    def iter_content(self, chunk_size=1):
        yield self._content

    def close(self):
        return None


def _dns_answer(address):
    return lambda host, port, *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
    ]


def test_fetcher_pins_validated_ip_and_never_resolves_again(monkeypatch):
    dns_calls = []
    pinned_calls = []

    def rebinding_dns(host, port, *args, **kwargs):
        dns_calls.append(host)
        # A second resolution would convert the validated public target into loopback.
        address = "8.8.8.8" if len(dns_calls) == 1 else "127.0.0.1"
        return _dns_answer(address)(host, port)

    def pinned_send(self, request, **kwargs):
        pinned_calls.append((self.pinned_ip, request.url))
        return _Response()

    # The legacy implementation takes this branch; keep it offline so the RED
    # assertion proves that no pinned adapter was used.
    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kwargs: _Response())
    monkeypatch.setattr(socket, "getaddrinfo", rebinding_dns)
    monkeypatch.setattr(fetcher_module.PinnedDNSAdapter, "send", pinned_send)

    result = SafeFetcher().get("http://rebind.example/article")

    assert result.content == b"ok"
    assert pinned_calls == [("8.8.8.8", "http://rebind.example/article")]
    assert dns_calls == ["rebind.example"]


def test_every_redirect_hop_is_resolved_once_and_pinned(monkeypatch):
    dns_calls = []
    pinned_calls = []

    def public_dns(host, port, *args, **kwargs):
        dns_calls.append(host)
        return _dns_answer("8.8.8.8" if host == "first.example" else "1.1.1.1")(host, port)

    def response_for(url):
        if url.endswith("/start"):
            return _Response(302, {"Location": "https://second.example/next"})
        return _Response(200, {"Content-Type": "text/html"}, b"done")

    def pinned_send(self, request, **kwargs):
        pinned_calls.append((self.pinned_ip, request.url))
        return response_for(request.url)

    monkeypatch.setattr(
        requests.Session, "get", lambda self, url, **kwargs: response_for(url)
    )
    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(fetcher_module.PinnedDNSAdapter, "send", pinned_send)

    result = SafeFetcher().get("https://first.example/start")

    assert result.content == b"done"
    assert pinned_calls == [
        ("8.8.8.8", "https://first.example/start"),
        ("1.1.1.1", "https://second.example/next"),
    ]
    assert dns_calls == ["first.example", "second.example"]


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_pinned_pool_preserves_origin_host_but_connects_to_validated_ip(scheme, monkeypatch):
    adapter = fetcher_module.PinnedDNSAdapter("8.8.8.8")
    created_socket = None
    try:
        request = requests.Request("GET", f"{scheme}://origin.example/path").prepare()
        pool = adapter.get_connection_with_tls_context(request, verify=True, proxies={})
        connection = pool._new_conn()
        assert connection.host == "origin.example"
        targets = []

        def fake_create_connection(address, *args, **kwargs):
            nonlocal created_socket
            targets.append(address)
            created_socket = socket.socket()
            return created_socket

        monkeypatch.setattr("urllib3.connection.connection.create_connection", fake_create_connection)
        connection._new_conn()
        assert targets == [("8.8.8.8", 443 if scheme == "https" else 80)]
        assert connection.host == "origin.example"
    finally:
        if created_socket is not None:
            created_socket.close()
        adapter.close()
