"""既有 WeWe 实现的安全与数据完整性回归测试。"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.wewe_client import FeedItem
from app.wewe_pipeline import WeWeIngestPipeline, _parse_feed_xml
from app.wewe_sanitize import sanitize_html
from app.wewe_scheduler import WeWeScheduler
from app.wewe_store import WeWeStore

from test_wewe_pipeline import FakeClient, FakeImageEmbedding, FakeEmbedding, FakeSession, FakeVectorIndex, FakeVision, _add_source, _pipeline, _settings


@pytest.fixture(autouse=True)
def allow_regression_test_dns(monkeypatch):
    """This module needs the same public-DNS test double as the pipeline tests."""
    monkeypatch.setattr(
        "app.wewe_fetcher.resolve_public_ips",
        lambda host, port=80: ["93.184.216.34"],
    )


def test_sanitize_supports_wechat_lazy_images_and_repairs_markup():
    cleaned = sanitize_html(
        '<p>正文</p><img data-src="https://img.example.com/lazy.png" alt="懒加载图">'
        '<img src="data:image/gif;base64,R0lGODlhAQABAIAAAAUEBAAAACwAAAAAAQABAAACAkQBADs=" '
        'data-src="https://img.example.com/real.jpg">',
        base_url="https://mp.weixin.qq.com/s/a1",
    )
    urls = [item["url"] for item in cleaned["images"]]
    assert urls == ["https://img.example.com/lazy.png", "https://img.example.com/real.jpg"]
    assert cleaned["html"].count("<img") == 2
    assert 'alt="懒加载图"' in cleaned["html"]
    assert 'src="https://img.example.com/real.jpg"' in cleaned["html"]


def test_sanitize_unclosed_drop_elements_fail_closed_before_following_markup():
    cleaned = sanitize_html("<p>前文</p><script>alert(1)<p>后文</p>")
    assert "前文" in cleaned["text"]
    # The apparent paragraph is JavaScript data until a real closing script tag.
    # Fail closed instead of guessing that it is a safe HTML recovery point.
    assert "后文" not in cleaned["text"]
    assert "alert" not in cleaned["text"]
    assert "script" not in cleaned["html"]


def test_sanitize_does_not_reinterpret_markup_inside_closed_script():
    cleaned = sanitize_html(
        '<p>前文</p><script>const template = "<p>不应成为正文</p>"; alert(1);</script><p>后文</p>'
    )
    assert "前文" in cleaned["text"]
    assert "后文" in cleaned["text"]
    assert "不应成为正文" not in cleaned["text"]
    assert "alert" not in cleaned["text"]
    assert "template" not in cleaned["text"]


def test_parse_rss_namespaces_and_atom_link_attributes():
    rss = """<?xml version='1.0'?><rss xmlns:content='http://purl.org/rss/1.0/modules/content/'>
    <channel><item><title>RSS 标题</title><link>https://example.com/a</link>
    <content:encoded><![CDATA[<p>RSS 全文</p>]]></content:encoded></item></channel></rss>"""
    atom = """<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Atom 标题</title>
    <link rel='self' href='https://example.com/feed.atom'/><link rel='alternate' href='https://example.com/b'/>
    <id>tag:b</id><content type='html'>&lt;p&gt;Atom 全文&lt;/p&gt;</content></entry></feed>"""
    rss_items = _parse_feed_xml(rss, "https://example.com/feed")
    atom_items = _parse_feed_xml(atom, "https://example.com/feed")
    assert rss_items[0].content_html == "<p>RSS 全文</p>"
    assert atom_items[0].url == "https://example.com/b"
    assert "Atom 全文" in atom_items[0].content_html


def test_parse_feed_xml_rejects_doctype_and_internal_entities():
    malicious = """<!DOCTYPE rss [<!ENTITY injected 'must-not-expand'>]>
    <rss><channel><item><title>&injected;</title>
    <link>https://example.com/entity</link></item></channel></rss>"""
    assert _parse_feed_xml(malicious, "https://example.com/feed") == []


def test_collect_limit_is_capped_by_server_configuration(tmp_path, wewe_settings):
    store = WeWeStore(wewe_settings)
    items = [
        FeedItem(article_id=f"a{i}", title=f"文章{i}", url=f"https://example.com/{i}", content_html="<p>正文</p>", published_at="")
        for i in range(4)
    ]
    pipeline, _index, _vision, _image_embedding, _session = _pipeline(tmp_path, store, items)
    pipeline.settings.wewe_max_articles_per_source = 3
    source_id = _add_source(store)
    result = pipeline.collect_source(user_id=1, source_id=source_id, limit=1000)
    assert result["article_count"] == 3
    assert pipeline.client.calls[0]["limit"] == 3


def test_failed_refresh_preserves_previous_fulltext_and_vectors(tmp_path, wewe_settings):
    store = WeWeStore(wewe_settings)
    good = [FeedItem(article_id="a1", title="旧标题", url="https://example.com/a1", content_html="<p>旧正文必须保留</p>", published_at="")]
    pipeline, index, _vision, _image_embedding, _session = _pipeline(tmp_path, store, good)
    source_id = _add_source(store)
    assert pipeline.collect_source(user_id=1, source_id=source_id)["status"] == "success"
    before = store.get_article(1, store.list_articles(1)[0]["id"])
    delete_count = len(index.deleted)

    pipeline.client.items = [FeedItem(article_id="a1", title="新标题", url="https://example.com/a1", content_html="", published_at="")]
    result = pipeline.collect_source(user_id=1, source_id=source_id)
    after = store.get_article(1, before["id"])
    assert result["status"] == "failed"
    assert after["text_content"] == "旧正文必须保留"
    assert after["content_html"] == before["content_html"]
    assert after["status"] == "indexed"
    assert after["title"] == before["title"]
    assert "no extractable text" in after["error"]
    assert len(index.deleted) == delete_count


def test_refresh_publishes_one_complete_vector_version_and_removes_stale_points(tmp_path, wewe_settings):
    store = WeWeStore(wewe_settings)
    old = [FeedItem(
        article_id="a1", title="旧标题", url="https://example.com/a1",
        content_html="<p>旧正文第一段必须完整保留</p><p>旧正文第二段稍后应被清理</p>", published_at="",
    )]
    pipeline, index, _vision, _image_embedding, _session = _pipeline(tmp_path, store, old)
    source_id = _add_source(store)
    assert pipeline.collect_source(user_id=1, source_id=source_id)["status"] == "success"
    before = dict(store.list_articles(1)[0])
    assert before["vector_version"]
    old_versions = {p["metadata"].get("vector_version") for p in index.text_points}

    pipeline.client.items = [FeedItem(
        article_id="a1", title="新标题", url="https://example.com/a1",
        content_html="<p>唯一的新正文版本</p>", published_at="",
    )]
    assert pipeline.collect_source(user_id=1, source_id=source_id)["status"] == "success"
    after = store.get_article(1, int(before["id"]))
    assert after["vector_version"] != before["vector_version"]
    visible = [p for p in index.text_points if p["article_id"] == int(before["id"])]
    assert visible
    assert {p["metadata"].get("vector_version") for p in visible} == {after["vector_version"]}
    assert [p["chunk"] for p in visible] == ["唯一的新正文版本"]
    assert old_versions.isdisjoint({p["metadata"].get("vector_version") for p in visible})


@pytest.mark.parametrize("failure_stage", ["embed_texts", "upsert_text_chunks", "upsert_image_vectors"])
def test_refresh_failures_never_delete_previous_version(
    tmp_path, wewe_settings, monkeypatch, failure_stage
):
    store = WeWeStore(wewe_settings)
    original = FeedItem(
        article_id="a1",
        title="旧标题",
        url="https://example.com/a1",
        content_html='<p>旧正文必须保留</p><img src="https://img.example.com/a.png">',
        published_at="2026-01-01",
    )
    pipeline, index, _vision, _image_embedding, _session = _pipeline(tmp_path, store, [original])
    source_id = _add_source(store)
    assert pipeline.collect_source(user_id=1, source_id=source_id)["status"] == "success"
    before = dict(store.list_articles(1)[0])
    previous_text_points = [dict(point) for point in index.text_points]
    previous_image_points = [dict(point) for point in index.image_points]
    previous_delete_calls = len(index.deleted)
    assert len(previous_image_points) == 1
    calls = []

    def fail(*args, **kwargs):
        calls.append(failure_stage)
        raise RuntimeError("injected " + failure_stage)

    if failure_stage == "embed_texts":
        monkeypatch.setattr(pipeline.embedding, failure_stage, fail)
    else:
        monkeypatch.setattr(index, failure_stage, fail)
    pipeline.client.items = [FeedItem(
        article_id="a1",
        title="新标题不应覆盖旧版本",
        url="https://example.com/a1",
        content_html='<p>NEW UNINDEXED TEXT</p><img src="https://img.example.com/a.png">',
        published_at="2026-01-02",
    )]

    result = pipeline.collect_source(user_id=1, source_id=source_id)

    assert calls == [failure_stage]
    after = store.get_article(1, before["id"])
    assert result["status"] == "failed"
    assert after["text_content"] == before["text_content"]
    assert after["content_html"] == before["content_html"]
    assert after["title"] == before["title"]
    assert after["status"] == "indexed"
    assert "injected" in after["error"]
    assert len(index.deleted) == previous_delete_calls
    assert index.text_points[:len(previous_text_points)] == previous_text_points
    assert index.image_points[:len(previous_image_points)] == previous_image_points

def test_image_limit_is_enforced_during_download_not_after(tmp_path, wewe_settings, monkeypatch):
    monkeypatch.setattr(
        "app.wewe_fetcher.resolve_public_ips", lambda host, port=80: ["93.184.216.34"]
    )
    store = WeWeStore(wewe_settings)
    item = FeedItem(
        article_id="a1",
        title="大图",
        url="https://example.com/a1",
        content_html='<p>正文</p><img src="https://img.example.com/large.png">',
        published_at="",
    )
    settings = _settings(tmp_path, wewe_max_image_bytes=8, wewe_max_download_bytes=1024 * 1024)
    from app.wewe_fetcher import SafeFetcher

    pipeline = WeWeIngestPipeline(
        settings=settings,
        store=store,
        client=FakeClient([item]),
        vector_index=FakeVectorIndex(),
        embedding=FakeEmbedding(),
        vision=FakeVision(),
        image_embedding=FakeImageEmbedding(),
        fetcher=SafeFetcher(session=FakeSession(), timeout=5, max_bytes=1024 * 1024),
    )
    source_id = _add_source(store)
    result = pipeline.collect_source(user_id=1, source_id=source_id)
    article = store.list_articles(1)[0]
    assert result["status"] in {"success", "partial"}
    assert result["image_count"] == 0
    assert article["image_count"] == 0
    assert store.list_images(article["id"])[0]["status"] == "failed"
    assert "too large" in store.list_images(article["id"])[0]["error"]


def test_store_allows_multiple_sources_and_scopes_article_ids_per_source(tmp_path, wewe_settings):
    store = WeWeStore(wewe_settings)
    first = _add_source(store)
    second = store.add_source(
        user_id=1,
        knowledge_base_id=1,
        source_type="mp",
        name="第二个公众号",
        upstream_feed_id="MP_2",
    )
    assert first != second
    article_a, created_a = store.upsert_article(
        user_id=1, source_id=first, knowledge_base_id=1, upstream_article_id="same-id", title="甲"
    )
    article_b, created_b = store.upsert_article(
        user_id=1, source_id=second, knowledge_base_id=1, upstream_article_id="same-id", title="乙"
    )
    assert created_a is True and created_b is True
    assert article_a != article_b
    assert store.get_article(1, article_a)["source_id"] == first
    assert store.get_article(1, article_b)["source_id"] == second


def test_store_migrates_legacy_unique_constraints(tmp_path, mysql_settings):
    from test_sqlite_migration import _legacy_database
    from app.sqlite_migration import migrate_sqlite

    path = tmp_path / "legacy.db"
    _legacy_database(path)
    migrate_sqlite(path, mysql_settings, apply=True)
    store = WeWeStore(mysql_settings)
    assert store.get_source(1, 1)["name"] == "旧来源"
    assert store.get_article(1, 1)["text_content"] == "旧文与中文内容"
    second = store.add_source(
        user_id=1, knowledge_base_id=1, source_type="mp", name="新来源", upstream_feed_id="NEW"
    )
    article_id, created = store.upsert_article(
        user_id=1, source_id=second, knowledge_base_id=1, upstream_article_id="legacy-article", title="同 ID 新文章"
    )
    assert created is True and article_id != 1


def test_scheduler_returns_failed_results_and_continues(tmp_path, wewe_settings):
    store = WeWeStore(wewe_settings)
    first = _add_source(store)
    second = store.add_source(
        user_id=1, knowledge_base_id=1, source_type="mp", name="第二源", upstream_feed_id="MP_2"
    )
    settings = Settings(wewe_collect_enabled=True)

    class Pipeline:
        def collect_source(self, source_id, **kwargs):
            return {"source_id": source_id, "status": "failed" if source_id == first else "success", "error": "x"}

    results = WeWeScheduler(settings, store, Pipeline()).run_once()
    assert [item["status"] for item in results] == ["failed", "success"]
    assert {item["source_id"] for item in results} == {first, second}
