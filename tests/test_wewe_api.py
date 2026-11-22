"""wewe-rss HTTP 接口测试（使用真实 SQLite 存储 + 替身管线）。"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import UserContext
from app.config import Settings
from app.wewe_api import build_wewe_router
from app.wewe_store import WeWeStore


class FakeImageEmbedding:
    def embed_text(self, query):
        return [0.1, 0.2]


class FakePipeline:
    def __init__(self):
        self.calls: list[dict] = []
        self.image_embedding = FakeImageEmbedding()

    def collect_source(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "task_id": 1,
            "status": "success",
            "article_count": 1,
            "image_count": 0,
            "indexed_count": 1,
            "error": "",
        }

    def delete_article(self, user_id, article_id):
        return {"deleted": True, "article_id": article_id}


class FakeVectorIndex:
    def __init__(self):
        self.deleted: list[tuple[int, int]] = []
        self.search_calls: list[dict] = []
        self.image_hits: list[dict] = []

    def delete_article(self, user_id, article_id):
        self.deleted.append((user_id, article_id))
        return {"text": 1, "image": 1}

    def search_images(
        self, query_vector, user_id, knowledge_base_id=None, top_k=5, article_versions=None
    ):
        self.search_calls.append({
            "query_vector": query_vector, "user_id": user_id,
            "knowledge_base_id": knowledge_base_id, "top_k": top_k,
            "article_versions": article_versions,
        })
        return [dict(hit) for hit in self.image_hits]


class FakeClient:
    login_result = {"message": "等待扫码"}
    created_login_id = "login-test"

    def list_feeds(self):
        feeds = [
            {"upstream_feed_id": "MP_1", "name": "示例号", "intro": "", "cover_url": ""},
            {"upstream_feed_id": "MP_OTHER", "name": "他人公众号", "intro": "私有", "cover_url": ""},
        ]
        return feeds + list(getattr(self, "added_feeds", []))

    def ping(self):
        return {"ok": True, "feed_count": 1}

    def create_login_url(self):
        return {"login_id": self.created_login_id, "scan_url": "https://weread.example/scan?id=test"}

    def get_login_result(self, login_id):
        assert login_id == self.created_login_id
        return dict(self.login_result)

    def add_account(self, result):
        assert result["token"] == "secret-token"
        return {"id": str(result["vid"]), "name": result["username"], "status": 1}

    def list_accounts(self):
        return [{"id": "123", "name": "读者", "status": 1, "blocked": False, "updated_at": "2026-11-21"}]

    def get_mp_info(self, share_link):
        assert share_link.startswith("https://mp.weixin.qq.com/s/")
        return {"id": "MP_NEW", "name": "新公众号", "cover": "https://img/cover.png", "intro": "介绍", "updateTime": 123}

    def add_feed(self, info):
        feed = {"upstream_feed_id": info["id"], "name": info["name"], "intro": info["intro"], "cover_url": info["cover"]}
        self.added_feeds = [*getattr(self, "added_feeds", []), feed]
        return feed


@pytest.fixture()
def api(wewe_settings):
    settings = wewe_settings.model_copy(update={
        "wewe_enabled": True,
        "wewe_base_url": "http://upstream:4000",
        "wewe_image_vector_enabled": True,
    })
    store = WeWeStore(settings)
    store.bind_upstream_feed(1, "MP_1")
    pipeline = FakePipeline()
    index = FakeVectorIndex()

    def current_user():
        return UserContext(id=1, email="u@example.com", is_admin=False, is_active=True)

    def resolve_kb(user_id, knowledge_base_id):
        return {"id": int(knowledge_base_id or 1), "name": "默认知识库"}

    app = FastAPI()
    app.include_router(
        build_wewe_router(
            current_user=current_user,
            store=store,
            pipeline=pipeline,
            vector_index=index,
            client=FakeClient(),
            settings=settings,
            resolve_kb=resolve_kb,
        )
    )
    return TestClient(app), store, pipeline, index


def test_create_list_and_collect_source(api):
    client, store, pipeline, _index = api
    response = client.post(
        "/api/wewe/sources",
        json={"source_type": "mp", "upstream_feed_id": "MP_1", "knowledge_base_id": 1},
    )
    assert response.status_code == 200
    source_id = response.json()["source_id"]

    listing = client.get("/api/wewe/sources").json()
    assert listing["count"] == 1
    assert listing["sources"][0]["upstream_feed_id"] == "MP_1"

    collected_response = client.post("/api/wewe/sources/%d/collect" % source_id, json={"limit": 5})
    assert collected_response.status_code == 202
    collected = collected_response.json()
    assert collected["status"] == "running"
    for _ in range(50):
        tasks = client.get("/api/wewe/tasks").json()["tasks"]
        task = next(item for item in tasks if item["id"] == collected["task_id"])
        if task["status"] != "running":
            break
        time.sleep(0.02)
    assert task["status"] == "success"
    assert pipeline.calls[0]["user_id"] == 1
    assert pipeline.calls[0]["source_id"] == source_id


def test_collect_runs_in_background_and_rejects_duplicate_source(api, monkeypatch):
    client, _store, pipeline, _index = api
    source_id = client.post(
        "/api/wewe/sources",
        json={"source_type": "mp", "upstream_feed_id": "MP_1", "knowledge_base_id": 1},
    ).json()["source_id"]
    started = threading.Event()
    release = threading.Event()

    def blocking_collect(**kwargs):
        started.set()
        release.wait(2)
        return {
            "task_id": kwargs["task_id"], "status": "success", "article_count": 1,
            "image_count": 0, "indexed_count": 1, "error": "",
        }

    monkeypatch.setattr(pipeline, "collect_source", blocking_collect)
    try:
        first = client.post(f"/api/wewe/sources/{source_id}/collect", json={})
        assert first.status_code == 202
        assert started.wait(1)
        second = client.post(f"/api/wewe/sources/{source_id}/collect", json={})
        assert second.status_code == 409
    finally:
        release.set()

    task_id = first.json()["task_id"]
    for _ in range(50):
        task = next(
            item for item in client.get("/api/wewe/tasks").json()["tasks"]
            if item["id"] == task_id
        )
        if task["status"] != "running":
            break
        time.sleep(0.02)
    assert task["status"] == "success"


def test_create_source_validates_input(api):
    client, _store, _pipeline, _index = api
    assert client.post("/api/wewe/sources", json={"source_type": "mp"}).status_code == 400
    assert client.post("/api/wewe/sources", json={"source_type": "web"}).status_code == 400
    assert client.post("/api/wewe/sources", json={"source_type": "web", "target_url": "https://example.com"}).status_code == 200


def test_delete_source_removes_article_vectors(api):
    client, store, _pipeline, index = api
    source_id = client.post(
        "/api/wewe/sources",
        json={"source_type": "mp", "upstream_feed_id": "MP_1", "knowledge_base_id": 1},
    ).json()["source_id"]
    store.upsert_article(
        user_id=1,
        source_id=source_id,
        knowledge_base_id=1,
        upstream_article_id="a1",
        title="文章",
    )
    response = client.delete("/api/wewe/sources/%d" % source_id)
    assert response.status_code == 200
    assert response.json()["removed_articles"] == 1
    assert index.deleted and index.deleted[0][0] == 1


def test_articles_are_scoped_to_current_user(api):
    client, store, _pipeline, _index = api
    client.post(
        "/api/wewe/sources",
        json={"source_type": "mp", "upstream_feed_id": "MP_1", "knowledge_base_id": 1},
    )
    from app.billing import BillingStore

    billing = BillingStore(store.settings)
    other = billing.create_user("other-api@example.com", "password-123")
    other_kb = billing.ensure_default_knowledge_base(int(other["id"]))
    other_source = store.add_source(
        user_id=int(other["id"]),
        knowledge_base_id=int(other_kb["id"]),
        source_type="mp",
        upstream_feed_id="MP_OTHER",
        name="他人来源",
    )
    store.upsert_article(
        user_id=int(other["id"]),
        source_id=other_source,
        knowledge_base_id=int(other_kb["id"]),
        upstream_article_id="x",
        title="他人文章",
    )
    assert client.get("/api/wewe/articles").json()["count"] == 0


def test_image_search_uses_controlled_file_url_and_hides_local_path(api):
    client, store, _pipeline, index = api
    source_id = store.add_source(
        user_id=1, knowledge_base_id=1, source_type="mp", name="版本来源",
        upstream_feed_id="IMAGE_VERSION",
    )
    article_id, _ = store.upsert_article(
        user_id=1, source_id=source_id, knowledge_base_id=1,
        upstream_article_id="image-version", title="图片文章",
    )
    store.update_article(
        article_id, status="indexed", indexed=True, vector_version="current-image-version"
    )
    index.image_hits = [{
        "image_id": 42, "article_id": article_id, "article_title": "图片文章",
        "description": "曲线", "local_path": "/srv/private/image.png", "score": 0.8,
    }]

    response = client.post("/api/wewe/images/search", json={"query": "曲线"})

    assert response.status_code == 200
    hit = response.json()["hits"][0]
    assert hit["image_file_url"] == "/api/wewe/images/42/file"
    assert index.search_calls[-1]["article_versions"] == {article_id: "current-image-version"}
    assert "local_path" not in hit
    assert "/srv/private" not in response.text


def test_qr_login_waits_then_binds_account_without_exposing_token(api):
    client, _store, _pipeline, _index = api
    FakeClient.login_result = {"message": "等待扫码"}
    created = client.post("/api/wewe/accounts/login-sessions", json={})
    assert created.status_code == 200
    session = created.json()
    assert session["login_id"] == "login-test"
    assert session["scan_url"].startswith("https://weread.example/")
    assert "<svg" in session["qr_svg"]
    assert session["expires_in"] >= 60

    pending = client.get("/api/wewe/accounts/login-sessions/login-test")
    assert pending.status_code == 200
    assert pending.json()["status"] == "waiting"

    FakeClient.login_result = {"vid": 123, "username": "读者", "token": "secret-token"}
    complete = client.get("/api/wewe/accounts/login-sessions/login-test")
    assert complete.status_code == 200
    assert complete.json()["status"] == "success"
    assert complete.json()["account"] == {"id": "123", "name": "读者", "status": 1}
    assert "secret-token" not in complete.text
    assert client.get("/api/wewe/accounts/login-sessions/login-test").status_code == 404


def test_account_list_has_visible_status_and_no_credentials(api):
    client, store, _pipeline, _index = api
    store.bind_upstream_account(1, "123")
    response = client.get("/api/wewe/accounts")
    assert response.status_code == 200
    account = response.json()["accounts"][0]
    assert account["status_label"] == "正常"
    assert account["available"] is True
    assert "token" not in response.text.lower()


def test_share_link_subscription_creates_source_and_starts_collection(api):
    client, store, pipeline, _index = api
    response = client.post("/api/wewe/subscriptions", json={
        "share_link": "https://mp.weixin.qq.com/s/article-token", "knowledge_base_id": 1, "limit": 5,
    })
    assert response.status_code == 202
    body = response.json()
    assert body["source"]["name"] == "新公众号"
    assert body["source"]["upstream_feed_id"] == "MP_NEW"
    assert body["collection"]["status"] == "running"
    source = store.get_source(1, body["source"]["id"])
    assert source["subscribed"] == 1
    for _ in range(50):
        task = next(item for item in client.get("/api/wewe/tasks").json()["tasks"] if item["id"] == body["collection"]["task_id"])
        if task["status"] != "running":
            break
        time.sleep(0.02)
    assert task["status"] == "success"
    assert pipeline.calls[-1]["refresh"] is True


def test_share_link_subscription_rejects_non_wechat_url(api):
    client, _store, _pipeline, _index = api
    response = client.post("/api/wewe/subscriptions", json={"share_link": "https://example.com/article"})
    assert response.status_code == 400
    assert "mp.weixin.qq.com/s/" in response.json()["detail"]


def test_status_reports_configuration(api):
    client, _store, _pipeline, _index = api
    status = client.get("/api/wewe/status").json()
    assert status["enabled"] is True
    assert status["upstream_configured"] is True


def test_upstream_accounts_and_feeds_require_local_ownership(api):
    client, _store, _pipeline, _index = api
    assert client.get("/api/wewe/accounts").json()["count"] == 0
    initial_feeds = client.get("/api/wewe/feeds").json()
    assert [item["upstream_feed_id"] for item in initial_feeds["feeds"]] == ["MP_1"]
    forbidden = client.post(
        "/api/wewe/sources",
        json={"source_type": "mp", "upstream_feed_id": "MP_OTHER", "knowledge_base_id": 1},
    )
    assert forbidden.status_code == 403

    subscribed = client.post("/api/wewe/subscriptions", json={
        "share_link": "https://mp.weixin.qq.com/s/article-token", "knowledge_base_id": 1,
    })
    assert subscribed.status_code == 202
    feeds = client.get("/api/wewe/feeds").json()
    assert [item["upstream_feed_id"] for item in feeds["feeds"]] == ["MP_1", "MP_NEW"]

    created = client.post("/api/wewe/accounts/login-sessions", json={})
    assert created.status_code == 200
    FakeClient.login_result = {"vid": 123, "username": "读者", "token": "secret-token"}
    assert client.get("/api/wewe/accounts/login-sessions/login-test").json()["status"] == "success"
    accounts = client.get("/api/wewe/accounts").json()
    assert [item["id"] for item in accounts["accounts"]] == ["123"]

