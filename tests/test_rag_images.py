"""RAG text + native-image integration tests for both authentication paths."""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.billing import ApiKeyContext, BillingStore, UserContext
from app.config import get_settings
from app.wewe_store import WeWeStore


class FakeEmbedding:
    def embed_query(self, query: str):
        return [0.1, 0.2]


class FakeIndexer:
    def __init__(self):
        self.search_calls: list[dict] = []
        self.source_calls: list[dict] = []

    def search_chunks(self, **kwargs):
        self.search_calls.append(kwargs)
        return [{
            "score": 0.9,
            "source_filename": "正文.pdf",
            "source_file": "https://example.com/source.pdf",
            "source_manual": None,
            "knowledge_base_id": kwargs["knowledge_base_id"],
            "chunk_index": 0,
            "page_start": 1,
            "page_end": 1,
            "chapter": "第一章",
            "text": "文字依据",
        }]

    def list_sources(self, **kwargs):
        self.source_calls.append(kwargs)
        return []


class FakeChat:
    def answer_with_context(self, question: str, hits: list[dict]):
        return "有文字依据的答案"


class FakeImageEmbedding:
    def embed_text(self, query: str):
        return [0.3, 0.4]


class FakeImageIndex:
    def __init__(self):
        self.search_calls: list[dict] = []

    def search_images(self, **kwargs):
        self.search_calls.append(kwargs)
        return [{
            "score": 0.8,
            "image_id": 42,
            "article_id": 7,
            "article_title": "图片文章",
            "article_url": "https://example.com/article",
            "description": "图片中的曲线",
            "knowledge_base_id": kwargs["knowledge_base_id"],
            "local_path": "/srv/private/wewe/1/7/images/img_42.png",
        }]


@pytest.fixture()
def main_module(mysql_settings, monkeypatch):
    # Importing main creates its production-style collaborators. Point every
    # relational setting at the guarded test DB before that one-time import.
    monkeypatch.setenv("MYSQL_HOST", mysql_settings.mysql_host)
    monkeypatch.setenv("MYSQL_PORT", str(mysql_settings.mysql_port))
    monkeypatch.setenv("MYSQL_USER", mysql_settings.mysql_user)
    monkeypatch.setenv("MYSQL_PASSWORD", mysql_settings.mysql_password)
    monkeypatch.setenv("MYSQL_DATABASE", mysql_settings.mysql_database)
    monkeypatch.setenv("MYSQL_UNIX_SOCKET", mysql_settings.mysql_unix_socket)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "rag-bootstrap@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "test-only-password")
    get_settings.cache_clear()
    main = importlib.import_module("app.main")
    main.settings = mysql_settings.model_copy(
        update={"wewe_enabled": True, "wewe_image_vector_enabled": True}
    )
    main.billing_store = BillingStore(mysql_settings)
    main.wewe_store = WeWeStore(mysql_settings)
    main.pipeline = SimpleNamespace(
        embedding=FakeEmbedding(), indexer=FakeIndexer(), chat=FakeChat()
    )
    main.wewe_image_embedding = FakeImageEmbedding()
    main.wewe_vector_index = FakeImageIndex()
    user = main.billing_store.create_user(
        "rag-admin@example.com", "password-123", is_admin=True
    )
    kb = main.billing_store.ensure_default_knowledge_base(int(user["id"]))
    main.app.dependency_overrides.clear()
    yield main, int(user["id"]), int(kb["id"])
    main.app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.mark.parametrize("auth_path", ["api_key", "session"])
def test_rag_answer_includes_controlled_native_images(main_module, auth_path, monkeypatch):
    main, user_id, kb_id = main_module
    user = UserContext(id=user_id, email="rag-admin@example.com", is_admin=True, is_active=True)
    api_key = ApiKeyContext(
        id=9, user_id=user_id, email=user.email, key_prefix="test", is_admin=True
    )
    main.app.dependency_overrides[main.current_user] = lambda: user
    main.app.dependency_overrides[main.current_api_key] = lambda: api_key
    client = TestClient(main.app)
    if auth_path == "api_key":
        url = f"/api/v1/knowledge-bases/{kb_id}/answer"
    else:
        url = "/api/rag/answer"

    image_searches = []
    original_image_search = main._search_related_images
    def counting_image_search(*args, **kwargs):
        image_searches.append((args, kwargs))
        return original_image_search(*args, **kwargs)
    monkeypatch.setattr(main, "_search_related_images", counting_image_search)
    response = client.post(url, json={
        "query": "曲线说明什么", "knowledge_base_id": kb_id, "include_images": True,
    })

    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["image_hits"]) == 1
    assert data["image_hits"][0]["image_file_url"] == "/api/wewe/images/42/file"
    assert data["citations"][-1]["vector_space"] == "image"
    assert data["citations"][-1]["image_id"] == 42
    assert len(image_searches) == 1
    assert "local_path" not in response.text
    assert "/srv/private" not in response.text


@pytest.mark.parametrize("auth_path", ["api_key", "session"])
def test_answer_with_only_image_hits_returns_visible_citations(main_module, auth_path, monkeypatch):
    main, user_id, kb_id = main_module
    user = UserContext(id=user_id, email="rag-admin@example.com", is_admin=True, is_active=True)
    api_key = ApiKeyContext(id=9, user_id=user_id, email=user.email, key_prefix="test", is_admin=True)
    main.app.dependency_overrides[main.current_user] = lambda: user
    main.app.dependency_overrides[main.current_api_key] = lambda: api_key
    monkeypatch.setattr(main.pipeline.indexer, "search_chunks", lambda **_kwargs: [])
    monkeypatch.setattr(
        main.pipeline.chat, "answer_with_context",
        lambda **_kwargs: pytest.fail("chat must not run without text evidence"),
    )
    url = f"/api/v1/knowledge-bases/{kb_id}/answer" if auth_path == "api_key" else "/api/rag/answer"
    response = TestClient(main.app).post(
        url, json={"query": "曲线说明什么", "knowledge_base_id": kb_id, "include_images": True}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["hits"] == []
    assert len(data["image_hits"]) == 1
    assert data["citations"][0]["type"] == "image"
    assert data["citations"][0]["image_file_url"].endswith("/file")
    assert "图片" in data["answer"]


@pytest.mark.parametrize("auth_path", ["api_key", "session"])
def test_exhausted_quota_stops_before_embedding(main_module, auth_path, monkeypatch):
    main, _, _ = main_module
    account = main.billing_store.create_user(
        f"exhausted-{auth_path}@example.com", "password-123"
    )
    kb_id = int(main.billing_store.ensure_default_knowledge_base(account["id"])["id"])
    with main.billing_store.connect() as conn:
        conn.execute(
            "UPDATE subscriptions SET used_calls = call_quota WHERE user_id = ?",
            (account["id"],),
        )
    user = UserContext(
        id=account["id"], email=account["email"], is_admin=False, is_active=True
    )
    api_key = ApiKeyContext(
        id=10, user_id=user.id, email=user.email, key_prefix="test", is_admin=False
    )
    main.app.dependency_overrides[main.current_user] = lambda: user
    main.app.dependency_overrides[main.current_api_key] = lambda: api_key

    def must_not_run(query):
        raise AssertionError("embedding started after quota exhaustion")

    monkeypatch.setattr(main.pipeline.embedding, "embed_query", must_not_run)
    client = TestClient(main.app)
    url = (
        f"/api/v1/knowledge-bases/{kb_id}/answer"
        if auth_path == "api_key" else "/api/rag/answer"
    )
    response = client.post(url, json={
        "query": "不应发起模型调用", "knowledge_base_id": kb_id,
    })

    assert response.status_code == 402, response.text
    assert "不足" in response.text or "用完" in response.text


@pytest.mark.parametrize("auth_path", ["api_key", "session"])
def test_model_failure_releases_api_reservation(main_module, auth_path, monkeypatch):
    main, _, _ = main_module
    account = main.billing_store.create_user(
        f"failed-{auth_path}@example.com", "password-123"
    )
    kb_id = int(main.billing_store.ensure_default_knowledge_base(account["id"])["id"])
    before = main.billing_store.get_active_subscription(account["id"])
    user = UserContext(
        id=account["id"], email=account["email"], is_admin=False, is_active=True
    )
    api_key = ApiKeyContext(
        id=11, user_id=user.id, email=user.email, key_prefix="test", is_admin=False
    )
    main.app.dependency_overrides[main.current_user] = lambda: user
    main.app.dependency_overrides[main.current_api_key] = lambda: api_key

    def fail_chat(question, hits):
        raise RuntimeError("injected model failure")

    monkeypatch.setattr(main.pipeline.chat, "answer_with_context", fail_chat)
    client = TestClient(main.app)
    url = (
        f"/api/v1/knowledge-bases/{kb_id}/answer"
        if auth_path == "api_key" else "/api/rag/answer"
    )
    response = client.post(url, json={
        "query": "触发失败结算", "knowledge_base_id": kb_id,
    })

    assert response.status_code == 500, response.text
    after = main.billing_store.get_active_subscription(account["id"])
    assert after["used_calls"] == before["used_calls"]
    assert after["remaining_calls"] == before["remaining_calls"]


@pytest.mark.parametrize("auth_path", ["api_key", "session"])
@pytest.mark.parametrize("operation", ["search", "answer", "sources"])
def test_rag_entrypoints_use_only_current_vector_versions(
    main_module, auth_path, operation, monkeypatch
):
    main, user_id, kb_id = main_module
    expected = {7: "current-version", 8: None}
    monkeypatch.setattr(
        main.wewe_store, "published_vector_versions",
        lambda user_id, knowledge_base_id: expected,
    )
    user = UserContext(id=user_id, email="rag-admin@example.com", is_admin=True, is_active=True)
    api_key = ApiKeyContext(id=9, user_id=user_id, email=user.email, key_prefix="test", is_admin=True)
    main.app.dependency_overrides[main.current_user] = lambda: user
    main.app.dependency_overrides[main.current_api_key] = lambda: api_key
    client = TestClient(main.app)
    if auth_path == "api_key":
        url = f"/api/v1/knowledge-bases/{kb_id}/{operation}"
    else:
        url = f"/api/rag/{operation}"
    if operation == "sources":
        if auth_path == "session":
            url += f"?knowledge_base_id={kb_id}"
        response = client.get(url)
    else:
        response = client.post(url, json={
            "query": "只看当前版本", "knowledge_base_id": kb_id, "include_images": True,
        })
    assert response.status_code == 200, response.text
    calls = main.pipeline.indexer.source_calls if operation == "sources" else main.pipeline.indexer.search_calls
    assert calls[-1]["article_versions"] == expected
    if operation != "sources":
        assert main.wewe_vector_index.search_calls[-1]["article_versions"] == expected
