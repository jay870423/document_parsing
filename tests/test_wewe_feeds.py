"""WeWe RSS/Atom/JSON Feed、OPML 与本地图片交付测试。"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import UserContext
from app.config import Settings
from app.wewe_api import build_wewe_router
from app.wewe_store import WeWeStore


class EmptyPipeline:
    image_embedding = None


class EmptyIndex:
    def search_images(self, **kwargs):
        return []


class EmptyClient:
    def list_feeds(self):
        return []

    def ping(self):
        return {"ok": True, "feed_count": 0}


@pytest.fixture()
def feed_api(tmp_path, wewe_settings):
    settings = wewe_settings.model_copy(update={
        "wewe_enabled": True,
        "wewe_base_url": "http://upstream:4000",
        "wewe_artifact_dir": str(tmp_path / "artifacts"),
    })
    store = WeWeStore(settings)

    def current_user():
        return UserContext(id=1, email="reader@example.com", is_admin=False, is_active=True)

    def resolve_kb(user_id, knowledge_base_id):
        return {"id": int(knowledge_base_id or 1), "name": "默认知识库"}

    app = FastAPI()
    app.include_router(
        build_wewe_router(
            current_user=current_user,
            store=store,
            pipeline=EmptyPipeline(),
            vector_index=EmptyIndex(),
            client=EmptyClient(),
            settings=settings,
            resolve_kb=resolve_kb,
        )
    )
    client = TestClient(app)
    first = store.add_source(
        user_id=1,
        knowledge_base_id=1,
        source_type="mp",
        upstream_feed_id="MP_A",
        name="儿童阅读 & 科学",
        intro="全文 <订阅>",
    )
    second = store.add_source(
        user_id=1,
        knowledge_base_id=1,
        source_type="web",
        target_url="https://example.com/feed.xml",
        name="暂停的网站",
        subscribed=False,
    )
    article_id, _ = store.upsert_article(
        user_id=1,
        source_id=first,
        knowledge_base_id=1,
        upstream_article_id="article-1",
        title="第一篇 <文章>",
        url="https://mp.weixin.qq.com/s/article-1",
        author="作者",
        published_at="2026-11-20T08:30:00+00:00",
        content_html='<p>安全正文</p><img src="https://img.example.com/a.png" />',
        text_content="安全正文",
        status="indexed",
    )
    store.upsert_article(
        user_id=1,
        source_id=second,
        knowledge_base_id=1,
        upstream_article_id="article-2",
        title="其他来源文章",
        content_html="<p>不应混入第一个源</p>",
        text_content="不应混入第一个源",
        status="indexed",
    )
    return client, store, first, article_id, settings


def test_rss_contains_only_owned_source_fulltext(feed_api):
    client, _store, source_id, _article_id, _settings = feed_api
    response = client.get(f"/api/wewe/sources/{source_id}/feed.rss")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    root = ET.fromstring(response.content)
    assert root.tag == "rss"
    assert root.findtext("channel/title") == "儿童阅读 & 科学"
    items = root.findall("channel/item")
    assert len(items) == 1
    assert items[0].findtext("title") == "第一篇 <文章>"
    encoded = items[0].findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
    assert "安全正文" in encoded
    assert "其他来源文章" not in response.text
    assert "local_path" not in response.text


def test_atom_uses_standard_link_and_html_content(feed_api):
    client, _store, source_id, _article_id, _settings = feed_api
    response = client.get(f"/api/wewe/sources/{source_id}/feed.atom")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/atom+xml")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(response.content)
    assert root.tag == "{http://www.w3.org/2005/Atom}feed"
    entry = root.find("a:entry", ns)
    assert entry is not None
    assert entry.find("a:link", ns).attrib["href"] == "https://mp.weixin.qq.com/s/article-1"
    assert entry.find("a:content", ns).attrib["type"] == "html"
    assert "安全正文" in (entry.find("a:content", ns).text or "")


def test_json_feed_is_json_feed_1_1(feed_api):
    client, _store, source_id, _article_id, _settings = feed_api
    response = client.get(f"/api/wewe/sources/{source_id}/feed.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/feed+json")
    payload = response.json()
    assert payload["version"] == "https://jsonfeed.org/version/1.1"
    assert payload["title"] == "儿童阅读 & 科学"
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["id"] == "article-1"
    assert item["url"] == "https://mp.weixin.qq.com/s/article-1"
    assert item["content_html"].startswith("<p>安全正文</p>")
    assert item["content_text"] == "安全正文"
    assert item["date_published"].endswith("Z")


def test_opml_exports_only_subscribed_sources_with_all_formats(feed_api):
    client, _store, source_id, _article_id, _settings = feed_api
    response = client.get("/api/wewe/opml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-opml")
    root = ET.fromstring(response.content)
    assert root.tag == "opml"
    outlines = root.findall("body/outline")
    assert len(outlines) == 1
    outline = outlines[0]
    assert outline.attrib["type"] == "rss"
    assert outline.attrib["title"] == "儿童阅读 & 科学"
    assert outline.attrib["xmlUrl"] == f"http://testserver/api/wewe/sources/{source_id}/feed.rss"
    assert outline.attrib["atomUrl"].endswith("/feed.atom")
    assert outline.attrib["jsonUrl"].endswith("/feed.json")


def test_local_image_is_served_by_id_without_leaking_server_path(feed_api, tmp_path):
    client, store, _source_id, article_id, settings = feed_api
    image_path = tmp_path / "artifacts" / "1" / str(article_id) / "images" / "img_1.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    image_id = store.upsert_image(
        article_id=article_id,
        user_id=1,
        url="https://img.example.com/a.png",
        local_path=str(image_path),
        mime_type="image/png",
        byte_size=image_path.stat().st_size,
        status="described",
    )

    detail = client.get(f"/api/wewe/articles/{article_id}")
    assert detail.status_code == 200
    image = detail.json()["images"][0]
    assert image["image_file_url"] == f"/api/wewe/images/{image_id}/file"
    assert "local_path" not in image

    downloaded = client.get(image["image_file_url"])
    assert downloaded.status_code == 200
    assert downloaded.content == image_path.read_bytes()
    assert downloaded.headers["content-type"].startswith("image/png")


def test_feed_and_opml_do_not_cross_users(wewe_settings):
    settings = wewe_settings.model_copy(update={"wewe_enabled": True, "wewe_base_url": "http://upstream:4000"})
    store = WeWeStore(settings)
    from app.billing import BillingStore

    other = BillingStore(settings).create_user("other@example.com", "password-123")
    assert int(other["id"]) == 2
    source_id = store.add_source(
        user_id=1,
        knowledge_base_id=1,
        source_type="mp",
        upstream_feed_id="PRIVATE",
        name="他人订阅",
    )
    app = FastAPI()

    def other_user():
        return UserContext(id=2, email="other@example.com", is_admin=False, is_active=True)

    app.include_router(
        build_wewe_router(
            current_user=other_user,
            store=store,
            pipeline=EmptyPipeline(),
            vector_index=EmptyIndex(),
            client=EmptyClient(),
            settings=settings,
            resolve_kb=lambda user_id, kb_id: {"id": int(kb_id or 1)},
        )
    )
    client = TestClient(app)
    assert client.get(f"/api/wewe/sources/{source_id}/feed.rss").status_code == 404
    assert client.get(f"/api/wewe/sources/{source_id}/feed.atom").status_code == 404
    assert client.get(f"/api/wewe/sources/{source_id}/feed.json").status_code == 404
    assert ET.fromstring(client.get("/api/wewe/opml").content).findall("body/outline") == []
