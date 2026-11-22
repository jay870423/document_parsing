"""Published article/vector/image generations must survive refresh failures and stale workers."""
from __future__ import annotations

import pytest

from app.wewe_fetcher import FetchError
from app.wewe_store import WeWeStore
from test_wewe_pipeline import PNG_BYTES, _add_source, _pipeline
from app.wewe_client import FeedItem


@pytest.fixture()
def store(wewe_settings):
    return WeWeStore(wewe_settings)


@pytest.fixture(autouse=True)
def allow_test_dns(monkeypatch):
    import app.wewe_fetcher as fetcher_module

    monkeypatch.setattr(fetcher_module, "resolve_public_ips", lambda host, port=80: ["93.184.216.34"])


def _item(image_url="https://img.example.com/a.png"):
    return FeedItem(
        article_id="a1",
        title="文章标题",
        url="https://mp.weixin.qq.com/s/a1",
        content_html=f'<p>正文内容</p><img src="{image_url}">',
        published_at="2026-01-01T00:00:00Z",
    )


def test_stale_worker_cleanup_cannot_delete_newer_published_version(tmp_path, store, monkeypatch):
    pipeline, index, _vision, _img, _session = _pipeline(tmp_path, store, [_item()])
    source_id = _add_source(store)
    pipeline.collect_source(user_id=1, source_id=source_id)
    article = store.list_articles(1)[0]
    first_version = article["vector_version"]

    original_update = store.update_article
    newer_version = "worker-b-published-version"
    injected = False

    def publish_b_after_a(article_id, **kwargs):
        nonlocal injected
        result = original_update(article_id, **kwargs)
        if kwargs.get("status") == "indexed" and kwargs.get("task_id") is not None and not injected:
            injected = True
            index.text_points.append({
                "article_id": article_id,
                "chunk": "worker B 正文",
                "metadata": {"vector_version": newer_version, "vector_space": "text"},
            })
            # Simulate worker B committing after A and before A's stale cleanup.
            original_update(article_id, vector_version=newer_version)
        return result

    monkeypatch.setattr(store, "update_article", publish_b_after_a)
    pipeline.collect_source(user_id=1, source_id=source_id)

    assert injected is True
    assert store.get_article(1, article["id"])["vector_version"] == newer_version
    assert any(p["metadata"].get("vector_version") == newer_version for p in index.text_points)
    assert not any(p["metadata"].get("vector_version") == first_version for p in index.text_points)
    assert not any(item[0] == "stale" for item in index.version_deletes)


def test_failed_publication_preserves_previous_image_row_and_file(tmp_path, store, monkeypatch):
    pipeline, _index, vision, _img, session = _pipeline(tmp_path, store, [_item()])
    source_id = _add_source(store)
    pipeline.collect_source(user_id=1, source_id=source_id)
    article = store.list_articles(1)[0]
    old_version = article["vector_version"]
    old_image = store.list_images(article["id"])[0]
    old_path = old_image["local_path"]
    old_bytes = PNG_BYTES

    session.content = b"\x89PNG\r\n\x1a\n" + b"NEW-IMAGE" * 8
    vision.description = "新描述"

    def fail_publication(*args, **kwargs):
        raise RuntimeError("simulated publication failure")

    monkeypatch.setattr(pipeline, "_index_article", fail_publication)
    result = pipeline.collect_source(user_id=1, source_id=source_id)

    assert result["status"] == "failed"
    current_article = store.get_article(1, article["id"])
    assert current_article["vector_version"] == old_version
    current_image = store.list_images(article["id"])[0]
    assert current_image["local_path"] == old_path
    assert current_image["description"] == "描述文本"
    assert current_image["status"] == "described"
    with open(old_path, "rb") as handle:
        assert handle.read() == old_bytes


def test_failed_download_reuses_published_image_without_clearing_metadata(tmp_path, store, monkeypatch):
    pipeline, index, _vision, _img, _session = _pipeline(tmp_path, store, [_item()])
    source_id = _add_source(store)
    pipeline.collect_source(user_id=1, source_id=source_id)
    article = store.list_articles(1)[0]
    old_image = store.list_images(article["id"])[0]

    def fail_download(*args, **kwargs):
        raise FetchError("temporary image fetch failure")

    monkeypatch.setattr("app.wewe_pipeline.download_image", fail_download)
    result = pipeline.collect_source(user_id=1, source_id=source_id)

    assert result["status"] == "success"
    current_article = store.get_article(1, article["id"])
    assert current_article["image_count"] == 1
    current_image = store.list_images(article["id"])[0]
    assert current_image["local_path"] == old_image["local_path"]
    assert current_image["description"] == old_image["description"] == "描述文本"
    assert current_image["vector_version"] == current_article["vector_version"]
    assert index.image_points[-1]["vector_version"] == current_article["vector_version"]


def test_successful_refresh_exposes_only_current_image_generation(tmp_path, store):
    pipeline, _index, vision, _img, session = _pipeline(tmp_path, store, [_item()])
    source_id = _add_source(store)
    pipeline.collect_source(user_id=1, source_id=source_id)
    article_id = store.list_articles(1)[0]["id"]
    old_image = store.list_images(article_id)[0]

    pipeline.client.items = [_item("https://img.example.com/b.png")]
    session.content = b"\x89PNG\r\n\x1a\n" + b"SECOND" * 8
    vision.description = "第二版描述"
    result = pipeline.collect_source(user_id=1, source_id=source_id)

    assert result["status"] == "success"
    article = store.get_article(1, article_id)
    images = store.list_images(article_id)
    assert len(images) == 1
    assert images[0]["url"] == "https://img.example.com/b.png"
    assert images[0]["description"] == "第二版描述"
    assert images[0]["vector_version"] == article["vector_version"]
    assert store.get_image(1, old_image["id"]) is None
