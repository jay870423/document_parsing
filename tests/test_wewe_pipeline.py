"""wewe-rss 存储与采集编排测试（全部使用内存替身，不访问外部网络/Qdrant）。"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.wewe_client import FeedItem
from app.wewe_pipeline import WeWeIngestPipeline
from app.wewe_store import WeWeStore

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeSession:
    def __init__(self, content: bytes = PNG_BYTES, content_type: str = "image/png"):
        self.content = content
        self.content_type = content_type
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        payload = self.content
        content_type = self.content_type

        class Resp:
            status_code = 200
            headers = {"Content-Type": content_type}

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
                yield payload

            def close(self):
                return None

        return Resp()


class FakeClient:
    def __init__(self, items):
        self.items = items
        self.calls: list[dict] = []

    def fetch_feed_items(self, feed_id, limit=30, page=1, fulltext=True, refresh=False):
        self.calls.append({"feed_id": feed_id, "limit": limit, "refresh": refresh})
        return self.items


class FakeEmbedding:
    def __init__(self):
        self.calls = 0

    def embed_texts(self, texts):
        self.calls += 1
        return [[0.1 * (index + 1)] * 4 for index, _ in enumerate(texts)]

    def embed_query(self, text):
        return [0.1] * 4


class FakeVision:
    def __init__(self, description="描述文本"):
        self.description = description
        self.calls = 0

    def analyze_image(self, image_path, prompt):
        self.calls += 1
        return self.description


class FakeImageEmbedding:
    def __init__(self):
        self.calls = 0

    def embed_image_file(self, image_path):
        self.calls += 1
        return [0.2, 0.2, 0.2, 0.2]

    def embed_text(self, text):
        return [0.3, 0.3, 0.3, 0.3]


class FakeVectorIndex:
    def __init__(self):
        self.deleted: list[tuple[int, int]] = []
        self.version_deletes: list[tuple] = []
        self.text_points: list[dict] = []
        self.image_points: list[dict] = []

    def delete_article(self, user_id, article_id):
        self.deleted.append((user_id, article_id))
        return {"text": 1, "image": 1}

    def delete_article_version(self, user_id, article_id, vector_version):
        self.version_deletes.append(("exact", user_id, article_id, vector_version))
        self.text_points = [p for p in self.text_points if not (
            p["article_id"] == article_id and p["metadata"].get("vector_version") == vector_version
        )]
        self.image_points = [p for p in self.image_points if not (
            p.get("article_id", article_id) == article_id and p.get("vector_version") == vector_version
        )]

    def delete_stale_article_versions(self, user_id, article_id, current_version):
        self.version_deletes.append(("stale", user_id, article_id, current_version))
        self.text_points = [p for p in self.text_points if not (
            p["article_id"] == article_id and p["metadata"].get("vector_version") != current_version
        )]
        self.image_points = [p for p in self.image_points if not (
            p.get("article_id", article_id) == article_id and p.get("vector_version") != current_version
        )]

    def upsert_text_chunks(self, user_id, knowledge_base_id, article_id, chunks, vectors, metadata, key_prefix="c"):
        for index, chunk in enumerate(chunks):
            self.text_points.append(
                {
                    "user_id": user_id,
                    "kb_id": knowledge_base_id,
                    "article_id": article_id,
                    "chunk": chunk,
                    "metadata": metadata,
                    "key_prefix": key_prefix,
                    "index": index,
                }
            )
        return len(chunks)

    def upsert_image_vectors(self, user_id, knowledge_base_id, article_id, items):
        self.image_points.extend(dict(item, article_id=article_id) for item in items)
        return len(items)


@pytest.fixture()
def store(wewe_settings):
    return WeWeStore(wewe_settings)


@pytest.fixture(autouse=True)
def allow_test_dns(monkeypatch):
    """测试环境无外网 DNS，固定放行被测域名（SSRF 单测另行覆盖）。"""

    import app.wewe_fetcher as fetcher_module

    monkeypatch.setattr(fetcher_module, "resolve_public_ips", lambda host, port=80: ["93.184.216.34"])


def _settings(tmp_path, **overrides):
    base = dict(
        wewe_enabled=True,
        wewe_artifact_dir=str(tmp_path / "artifacts"),
        wewe_max_images_per_article=3,
        wewe_image_vector_enabled=True,
        chunk_size=200,
        chunk_overlap=20,
    )
    base.update(overrides)
    return Settings(**base)


def _pipeline(tmp_path, store, items, image_vector=True):
    settings = _settings(tmp_path, wewe_image_vector_enabled=image_vector)
    session = FakeSession()
    from app.wewe_fetcher import SafeFetcher

    fetcher = SafeFetcher(session=session, timeout=5, max_bytes=1024 * 1024)
    client = FakeClient(items)
    index = FakeVectorIndex()
    vision = FakeVision()
    image_embedding = FakeImageEmbedding()
    pipeline = WeWeIngestPipeline(
        settings=settings,
        store=store,
        client=client,
        vector_index=index,
        embedding=FakeEmbedding(),
        vision=vision,
        image_embedding=image_embedding,
        fetcher=fetcher,
    )
    return pipeline, index, vision, image_embedding, session


def _add_source(store, user_id=1, kb_id=1):
    store.bind_upstream_feed(user_id, "MP_1")
    return store.add_source(
        user_id=user_id,
        knowledge_base_id=kb_id,
        source_type="mp",
        name="示例公众号",
        upstream_feed_id="MP_1",
    )


def test_store_isolates_users(store):
    source_a = _add_source(store, user_id=1)
    store.upsert_article(
        user_id=1,
        source_id=source_a,
        knowledge_base_id=1,
        upstream_article_id="a1",
        title="A",
    )
    assert len(store.list_articles(1)) == 1
    assert store.list_articles(2) == []
    assert store.get_article(2, store.list_articles(1)[0]["id"]) is None


def test_store_upsert_article_is_idempotent(store):
    source_id = _add_source(store)
    first, created_first = store.upsert_article(
        user_id=1, source_id=source_id, knowledge_base_id=1, upstream_article_id="a1", title="旧标题"
    )
    second, created_second = store.upsert_article(
        user_id=1, source_id=source_id, knowledge_base_id=1, upstream_article_id="a1", title="新标题"
    )
    assert first == second
    assert created_first is True and created_second is False
    assert store.get_article(1, first)["title"] == "新标题"


def test_collect_source_indexes_text_and_images(tmp_path, store):
    items = [
        FeedItem(
            article_id="a1",
            title="文章标题",
            url="https://mp.weixin.qq.com/s/a1",
            content_html='<p>这是正文第一段，包含关键信息。</p><img src="https://img.example.com/a.png">',
            published_at="2026-01-01T00:00:00Z",
        )
    ]
    pipeline, index, vision, image_embedding, session = _pipeline(tmp_path, store, items)
    source_id = _add_source(store)

    result = pipeline.collect_source(user_id=1, source_id=source_id)

    assert result["status"] == "success"
    assert result["article_count"] == 1
    assert result["indexed_count"] == 1

    articles = store.list_articles(1)
    assert len(articles) == 1
    article = articles[0]
    print("IMAGES", store.list_images(article["id"]))
    assert article["status"] == "indexed"
    assert "正文" in article["text_content"]
    assert article["image_count"] == 1

    images = store.list_images(article["id"])
    assert len(images) == 1
    assert images[0]["description"] == "描述文本"
    assert images[0]["local_path"]

    # 正文分块进入文本空间
    text_points = [p for p in index.text_points if p["metadata"].get("chunk_type") == "article_text"]
    assert text_points
    assert text_points[0]["metadata"]["vector_space"] == "text"
    assert text_points[0]["metadata"]["article_id"] == article["id"]

    # 视觉描述文本同样进入文本空间，但标记为 image_description
    desc_points = [p for p in index.text_points if p["metadata"].get("chunk_type") == "image_description"]
    assert len(desc_points) == 1

    # 原生图片向量进入图片空间
    assert len(index.image_points) == 1
    assert index.image_points[0]["vector"] == [0.2, 0.2, 0.2, 0.2]


def test_collect_is_idempotent_and_reindexes(tmp_path, store):
    items = [
        FeedItem(
            article_id="a1",
            title="文章标题",
            url="https://mp.weixin.qq.com/s/a1",
            content_html="<p>正文内容</p>",
            published_at="2026-01-01T00:00:00Z",
        )
    ]
    pipeline, index, _vision, _img, _session = _pipeline(tmp_path, store, items)
    source_id = _add_source(store)

    pipeline.collect_source(user_id=1, source_id=source_id)
    first_count = len(store.list_articles(1))
    first_version = store.list_articles(1)[0]["vector_version"]
    pipeline.collect_source(user_id=1, source_id=source_id)

    articles = store.list_articles(1)
    assert len(articles) == first_count == 1
    # Reindexing publishes one new generation and cleans stale points only after commit.
    assert index.deleted == []
    assert len(index.text_points) == 1
    assert index.text_points[0]["metadata"]["vector_version"] == articles[0]["vector_version"]
    assert articles[0]["vector_version"] != first_version


def test_collect_records_failure_without_breaking_batch(tmp_path, store):
    items = [
        FeedItem(article_id="a1", title="坏文章", url="", content_html="", published_at=""),
        FeedItem(article_id="a2", title="好文章", url="https://mp.weixin.qq.com/s/a2", content_html="<p>正文</p>", published_at=""),
    ]
    pipeline, index, _vision, _img, _session = _pipeline(tmp_path, store, items)
    source_id = _add_source(store)

    result = pipeline.collect_source(user_id=1, source_id=source_id)

    assert result["indexed_count"] == 1
    statuses = {article["upstream_article_id"]: article["status"] for article in store.list_articles(1)}
    assert statuses["a1"] == "failed"
    assert statuses["a2"] == "indexed"


def test_image_vector_disabled_degrades_to_description_only(tmp_path, store):
    items = [
        FeedItem(
            article_id="a1",
            title="带图文章",
            url="https://mp.weixin.qq.com/s/a1",
            content_html='<p>正文</p><img src="https://img.example.com/a.png">',
            published_at="",
        )
    ]
    pipeline, index, _vision, _img, _session = _pipeline(tmp_path, store, items, image_vector=False)
    source_id = _add_source(store)

    pipeline.collect_source(user_id=1, source_id=source_id)

    assert index.image_points == []
    desc_points = [p for p in index.text_points if p["metadata"].get("chunk_type") == "image_description"]
    assert len(desc_points) == 1

