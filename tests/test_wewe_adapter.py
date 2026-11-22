"""wewe-rss 适配器单元测试：HTML 清洗、SSRF 防护、上游契约解析。"""

from __future__ import annotations

import pytest
import json

from app.wewe_client import WeWeRssClient, WeWeError
from app.wewe_fetcher import (
    FetchError,
    SafeFetcher,
    resolve_public_ips,
    sniff_image,
    validate_url,
)
from app.wewe_sanitize import sanitize_html, strip_tags


# ---------------------------------------------------------------- 清洗
def test_sanitize_removes_scripts_and_event_handlers():
    html = (
        "<div onclick='steal()'>"
        "<script>alert('xss')</script>"
        "<style>.a{color:red}</style>"
        "<p>正常正文</p>"
        "</div>"
    )
    result = sanitize_html(html)
    assert "script" not in result["html"].lower()
    assert "onclick" not in result["html"].lower()
    assert "alert" not in result["html"]
    assert "正常正文" in result["text"]


@pytest.mark.parametrize(
    ("html", "kept"),
    [
        ('<p>前文</p><script>const template = "<p>脚本内内容</p>"; alert(1);', "前文"),
        ("<script>alert(1)", ""),
        ("<style>body{display:none}", ""),
    ],
)
def test_unclosed_drop_elements_fail_closed(html, kept):
    result = sanitize_html(html)
    plain = strip_tags(html)
    assert result["text"] == kept
    assert plain == kept
    assert "脚本内内容" not in result["text"]
    assert "alert" not in result["text"] + plain
    assert "display:none" not in result["text"] + plain


def test_sanitize_drops_javascript_urls():
    html = '<a href="javascript:alert(1)">点我</a><a href="https://example.com/a">正常</a>'
    result = sanitize_html(html)
    assert "javascript:" not in result["html"].lower()
    assert "https://example.com/a" in result["html"]


def test_sanitize_extracts_images_and_resolves_relative_urls():
    html = '<p>图文</p><img src="/img/a.png" alt="封面"><img src="https://cdn.example.com/b.jpg">'
    result = sanitize_html(html, base_url="https://example.com/post/1")
    urls = [item["url"] for item in result["images"]]
    assert "https://example.com/img/a.png" in urls
    assert "https://cdn.example.com/b.jpg" in urls
    assert result["images"][0]["alt"] == "封面"


def test_sanitize_ignores_data_uri_images():
    html = '<img src="data:image/png;base64,AAAA">'
    result = sanitize_html(html)
    assert result["images"] == []


def test_strip_tags_removes_markup():
    assert strip_tags("<p>hello <b>world</b></p>") == "hello world"


# ---------------------------------------------------------------- SSRF
def test_validate_url_rejects_non_http_scheme():
    with pytest.raises(FetchError):
        validate_url("file:///etc/passwd")
    with pytest.raises(FetchError):
        validate_url("javascript:alert(1)")


def test_validate_url_rejects_loopback_literal():
    with pytest.raises(FetchError):
        validate_url("http://127.0.0.1/admin")
    with pytest.raises(FetchError):
        validate_url("http://192.168.1.10/x")


def test_resolve_public_ips_rejects_private_resolution(monkeypatch):
    import socket

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FetchError):
        resolve_public_ips("internal.example.com", 80)


def test_fetcher_follows_redirect_and_revalidates_each_hop(monkeypatch):
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, status_code, headers, content=b""):
            self.status_code = status_code
            self.headers = headers
            self._content = content

        @property
        def ok(self):
            return self.status_code < 400

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

    class FakeSession:
        def get(self, url, **kwargs):
            calls.append(url)
            if url.endswith("/start"):
                return FakeResponse(302, {"Location": "https://example.com/next"})
            if url.endswith("/next"):
                return FakeResponse(302, {"Location": "http://127.0.0.1/internal"})
            return FakeResponse(200, {"Content-Type": "text/html"}, b"ok")

    fetcher = SafeFetcher(session=FakeSession())
    with pytest.raises(FetchError):
        fetcher.get("https://example.com/start")
    assert calls[0].endswith("/start")


def test_fetcher_enforces_size_limit():
    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "text/html", "Content-Length": "999999"}

        @property
        def ok(self):
            return True

        @property
        def is_redirect(self):
            return False

        @property
        def is_permanent_redirect(self):
            return False

        def iter_content(self, chunk_size=1):
            yield b"x" * 1024

        def close(self):
            return None

    class FakeSession:
        def get(self, url, **kwargs):
            return FakeResponse()

    fetcher = SafeFetcher(session=FakeSession(), max_bytes=2048)
    with pytest.raises(FetchError):
        fetcher.get("https://example.com/big")


def test_sniff_image_detects_types():
    assert sniff_image(b"\xff\xd8\xff\xe0payload")[0] == "image/jpeg"
    assert sniff_image(b"\x89PNG\r\n\x1a\npayload")[0] == "image/png"
    assert sniff_image(b"<svg xmlns='x'></svg>") is None


# ---------------------------------------------------------------- 上游契约
class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = ""

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: dict):
        self.responses = responses
        self.requests: list[tuple[str, dict]] = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.requests.append((url, {"method": method, "params": params, "json": json, "headers": headers}))
        for key, value in self.responses.items():
            if url.endswith(key):
                return value
        raise AssertionError("unexpected url " + url)


def test_client_lists_feeds_and_sends_authorization():
    session = FakeSession({"/feeds/": FakeResponse([{"id": "MP_1", "name": "示例号", "intro": "简介", "cover": "c.png"}])})
    client = WeWeRssClient("http://upstream:4000", auth_code="secret", session=session)
    feeds = client.list_feeds()
    assert feeds[0]["upstream_feed_id"] == "MP_1"
    assert feeds[0]["name"] == "示例号"
    url, kwargs = session.requests[0]
    assert kwargs["headers"]["Authorization"] == "secret"


def test_client_parses_feed_items():
    payload = {
        "version": "https://jsonfeed.org/version/1",
        "items": [
            {
                "id": "abc",
                "title": "标题",
                "url": "https://mp.weixin.qq.com/s/abc",
                "content_html": "<p>正文</p>",
                "date_published": "2026-01-01T00:00:00Z",
                "image": "https://img/1.png",
                "author": [{"name": "作者"}],
            }
        ],
    }
    session = FakeSession({"/feeds/MP_1.json": FakeResponse(payload)})
    client = WeWeRssClient("http://upstream:4000", session=session)
    items = client.fetch_feed_items("MP_1", limit=5, fulltext=True, refresh=True)
    assert len(items) == 1
    assert items[0].article_id == "abc"
    assert items[0].content_html == "<p>正文</p>"
    assert items[0].author == "作者"
    _, kwargs = session.requests[0]
    assert kwargs["params"]["mode"] == "fulltext"
    assert kwargs["params"]["update"] == "true"


def test_client_requires_base_url():
    client = WeWeRssClient("")
    with pytest.raises(WeWeError):
        client.list_feeds()


def test_client_surfaces_upstream_error():
    session = FakeSession({"/feeds/": FakeResponse({"error": "boom"}, status_code=500)})
    client = WeWeRssClient("http://upstream:4000", session=session, max_retries=1)
    with pytest.raises(WeWeError):
        client.list_feeds()


def trpc_payload(value):
    return FakeResponse({"result": {"data": {"json": value}}})


def test_trpc_queries_use_get_and_mutations_use_post():
    session = FakeSession({
        "/trpc/account.list": trpc_payload({"items": [], "blocks": []}),
        "/trpc/article.list": trpc_payload({"items": []}),
        "/trpc/platform.getLoginResult": trpc_payload({"message": "等待扫码"}),
        "/trpc/platform.createLoginUrl": trpc_payload({"uuid": "login-1", "scanUrl": "https://weread.example/scan?id=1"}),
    })
    client = WeWeRssClient("http://upstream:4000", session=session)
    client.list_accounts()
    client.list_articles("feed-1", limit=7)
    client.get_login_result("login-1")
    client.create_login_url()

    requests = {url.rsplit("/", 1)[-1]: kwargs for url, kwargs in session.requests}
    for procedure in ("account.list", "article.list", "platform.getLoginResult"):
        request = requests[procedure]
        assert request["method"] == "GET"
        assert json.loads(request["params"]["input"])["json"] is not None
        assert request["json"] is None
    assert json.loads(requests["article.list"]["params"]["input"])["json"] == {"limit": 7, "feedId": "feed-1"}
    mutation = requests["platform.createLoginUrl"]
    assert mutation["method"] == "POST"
    assert mutation["json"] == {"json": {}}


def test_client_qr_login_and_account_contract_never_returns_token():
    session = FakeSession({
        "/trpc/platform.createLoginUrl": trpc_payload({"uuid": "login-1", "scanUrl": "https://weread.example/scan?id=1"}),
        "/trpc/platform.getLoginResult": trpc_payload({"vid": 123, "username": "读者", "token": "very-secret-token"}),
        "/trpc/account.add": trpc_payload({"id": "123", "name": "读者", "status": 1, "token": "very-secret-token"}),
        "/trpc/account.list": trpc_payload({"items": [{"id": "123", "name": "读者", "status": 1, "updatedAt": "2026-11-21"}], "blocks": []}),
    })
    client = WeWeRssClient("http://upstream:4000", session=session)

    login = client.create_login_url()
    assert login == {"login_id": "login-1", "scan_url": "https://weread.example/scan?id=1"}
    result = client.get_login_result("login-1")
    account = client.add_account(result)
    assert account == {"id": "123", "name": "读者", "status": 1}
    assert client.list_accounts()[0]["blocked"] is False
    assert "very-secret-token" not in str(account)
    add_request = next(kwargs for url, kwargs in session.requests if url.endswith("/trpc/account.add"))
    assert add_request["json"]["json"]["token"] == "very-secret-token"


def test_client_subscribes_mp_from_share_link_contract():
    mp = {"id": "MP_WXS_1", "name": "公众号", "cover": "https://img/cover.png", "intro": "简介", "updateTime": 123}
    session = FakeSession({
        "/trpc/platform.getMpInfo": trpc_payload([mp]),
        "/trpc/feed.add": trpc_payload({"id": "MP_WXS_1", "mpName": "公众号"}),
    })
    client = WeWeRssClient("http://upstream:4000", session=session)

    info = client.get_mp_info("https://mp.weixin.qq.com/s/article-token")
    added = client.add_feed(info)
    assert added["upstream_feed_id"] == "MP_WXS_1"
    assert added["name"] == "公众号"
    feed_request = next(kwargs for url, kwargs in session.requests if url.endswith("/trpc/feed.add"))
    assert feed_request["json"]["json"] == {
        "id": "MP_WXS_1", "mpName": "公众号", "mpCover": "https://img/cover.png",
        "mpIntro": "简介", "updateTime": 123, "status": 1,
    }
    with pytest.raises(WeWeError):
        client.get_mp_info("https://example.com/not-wechat")

