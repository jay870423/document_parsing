"""wewe-rss HTTP 接口测试（使用真实 SQLite 存储 + 替身管线）。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import UserContext
from app.config import Settings
from app.wewe_api import build_wewe_router
from app.wewe_store import WeWeStore


class FakePipeline:
    def __init__(self):
        self.calls: list[dict] = []
        self.image_embedding = None

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

    def delete_article(self, user_id, article_id):
        self.deleted.append((user_id, article_id))
        return {"text": 1, "image": 1}

    def search_images(self, query_vector, user_id, knowledge_base_id=None, top_k=5):
        return []


class FakeClient:
    def list_feeds(self):
        return [{"upstream_feed_id": "MP_1", "name": "示例号", "intro": "", "cover_url": ""}]

    def ping(self):
        return {"ok": True, "feed_count": 1}


@pytest.fixture()
def api(tmp_path):
    settings = Settings(wewe_enabled=True, wewe_base_url="http://upstream:4000")
    store = WeWeStore(str(tmp_path / "app.db"))
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

    collected = client.post("/api/wewe/sources/%d/collect" % source_id, json={"limit": 5}).json()
    assert collected["status"] == "success"
    assert pipeline.calls[0]["user_id"] == 1
    assert pipeline.calls[0]["source_id"] == source_id


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
    source_id = client.post(
        "/api/wewe/sources",
        json={"source_type": "mp", "upstream_feed_id": "MP_1", "knowledge_base_id": 1},
    ).json()["source_id"]
    store.upsert_article(user_id=2, source_id=source_id, knowledge_base_id=1, upstream_article_id="x", title="他人文章")
    assert client.get("/api/wewe/articles").json()["count"] == 0


def test_status_reports_configuration(api):
    client, _store, _pipeline, _index = api
    status = client.get("/api/wewe/status").json()
    assert status["enabled"] is True
    assert status["upstream_configured"] is True

