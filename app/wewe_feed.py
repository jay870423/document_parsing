"""WeWe 文章的 RSS 2.0、Atom、JSON Feed 与 OPML 序列化。

输出只使用当前用户已授权的来源/文章，并在边界再次清洗历史 HTML。
服务器本地路径、向量信息与上游授权码不会进入任何导出格式。
"""

from __future__ import annotations

import json
# Output serialization only; untrusted XML parsing uses defusedxml.
import xml.etree.ElementTree as ET  # nosec B405
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from urllib.parse import quote

from app.wewe_sanitize import sanitize_html

ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"


def _xml_bytes(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _parse_date(value: str) -> datetime:
    raw = (value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _iso_date(value: str) -> str:
    return _parse_date(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _rfc2822_date(value: str) -> str:
    return format_datetime(_parse_date(value))


def _source_link(source: dict, articles: list[dict]) -> str:
    target = str(source.get("target_url") or "").strip()
    if target:
        return target
    for article in articles:
        url = str(article.get("url") or "").strip()
        if url:
            return url
    return ""


def _article_identity(article: dict) -> str:
    return str(article.get("upstream_article_id") or article.get("id") or "")


def _article_content(article: dict) -> tuple[str, str]:
    cleaned = sanitize_html(
        str(article.get("content_html") or ""),
        base_url=str(article.get("url") or ""),
    )
    html = cleaned["html"]
    text = cleaned["text"] or str(article.get("text_content") or "")
    if not html and text:
        html = "<p>" + text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</p>"
    return html, text


def _feed_urls(base_url: str, source_id: int) -> dict[str, str]:
    root = base_url.rstrip("/") + "/api/wewe/sources/" + str(int(source_id))
    return {"rss": root + "/feed.rss", "atom": root + "/feed.atom", "json": root + "/feed.json"}


def render_rss(source: dict, articles: list[dict], base_url: str) -> bytes:
    ET.register_namespace("atom", ATOM_NS)
    ET.register_namespace("content", CONTENT_NS)
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = str(source.get("name") or "WeWe RSS")
    ET.SubElement(channel, "link").text = _source_link(source, articles)
    ET.SubElement(channel, "description").text = str(source.get("intro") or source.get("name") or "")
    ET.SubElement(channel, "generator").text = "document-parsing WeWe integration"
    ET.SubElement(channel, "lastBuildDate").text = _rfc2822_date("")
    urls = _feed_urls(base_url, int(source["id"]))
    ET.SubElement(
        channel,
        "{%s}link" % ATOM_NS,
        {"href": urls["rss"], "rel": "self", "type": "application/rss+xml"},
    )
    for article in articles:
        html, text = _article_content(article)
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = str(article.get("title") or "无标题")
        ET.SubElement(item, "link").text = str(article.get("url") or "")
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = _article_identity(article)
        ET.SubElement(item, "pubDate").text = _rfc2822_date(str(article.get("published_at") or ""))
        if str(article.get("author") or ""):
            ET.SubElement(item, "author").text = str(article["author"])
        ET.SubElement(item, "description").text = text[:500]
        ET.SubElement(item, "{%s}encoded" % CONTENT_NS).text = html
    return _xml_bytes(root)


def render_atom(source: dict, articles: list[dict], base_url: str) -> bytes:
    ET.register_namespace("", ATOM_NS)
    root = ET.Element("{%s}feed" % ATOM_NS)
    urls = _feed_urls(base_url, int(source["id"]))
    ET.SubElement(root, "{%s}id" % ATOM_NS).text = urls["atom"]
    ET.SubElement(root, "{%s}title" % ATOM_NS).text = str(source.get("name") or "WeWe RSS")
    ET.SubElement(root, "{%s}subtitle" % ATOM_NS).text = str(source.get("intro") or "")
    ET.SubElement(root, "{%s}updated" % ATOM_NS).text = _iso_date("")
    ET.SubElement(root, "{%s}link" % ATOM_NS, {"href": urls["atom"], "rel": "self"})
    home = _source_link(source, articles)
    if home:
        ET.SubElement(root, "{%s}link" % ATOM_NS, {"href": home, "rel": "alternate"})
    for article in articles:
        html, text = _article_content(article)
        entry = ET.SubElement(root, "{%s}entry" % ATOM_NS)
        identity = str(article.get("url") or "") or "urn:wewe:article:" + quote(_article_identity(article), safe="")
        ET.SubElement(entry, "{%s}id" % ATOM_NS).text = identity
        ET.SubElement(entry, "{%s}title" % ATOM_NS).text = str(article.get("title") or "无标题")
        ET.SubElement(entry, "{%s}updated" % ATOM_NS).text = _iso_date(str(article.get("published_at") or ""))
        if str(article.get("published_at") or ""):
            ET.SubElement(entry, "{%s}published" % ATOM_NS).text = _iso_date(str(article["published_at"]))
        if str(article.get("url") or ""):
            ET.SubElement(entry, "{%s}link" % ATOM_NS, {"href": str(article["url"]), "rel": "alternate"})
        if str(article.get("author") or ""):
            author = ET.SubElement(entry, "{%s}author" % ATOM_NS)
            ET.SubElement(author, "{%s}name" % ATOM_NS).text = str(article["author"])
        ET.SubElement(entry, "{%s}summary" % ATOM_NS).text = text[:500]
        ET.SubElement(entry, "{%s}content" % ATOM_NS, {"type": "html"}).text = html
    return _xml_bytes(root)


def render_json_feed(source: dict, articles: list[dict], base_url: str) -> bytes:
    urls = _feed_urls(base_url, int(source["id"]))
    items = []
    for article in articles:
        html, text = _article_content(article)
        item: dict[str, object] = {
            "id": _article_identity(article),
            "url": str(article.get("url") or ""),
            "title": str(article.get("title") or "无标题"),
            "content_html": html,
            "content_text": text,
            "date_published": _iso_date(str(article.get("published_at") or "")),
            "date_modified": _iso_date(str(article.get("updated_at") or article.get("published_at") or "")),
        }
        if str(article.get("author") or ""):
            item["authors"] = [{"name": str(article["author"])}]
        if str(article.get("cover_url") or ""):
            item["image"] = str(article["cover_url"])
        items.append(item)
    payload = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": str(source.get("name") or "WeWe RSS"),
        "description": str(source.get("intro") or ""),
        "home_page_url": _source_link(source, articles),
        "feed_url": urls["json"],
        "items": items,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def render_opml(sources: list[dict], base_url: str, title: str = "WeWe RSS 订阅") -> bytes:
    root = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = title
    ET.SubElement(head, "dateCreated").text = _rfc2822_date("")
    body = ET.SubElement(root, "body")
    for source in sources:
        urls = _feed_urls(base_url, int(source["id"]))
        home = _source_link(source, [])
        attrs = {
            "text": str(source.get("name") or "未命名来源"),
            "title": str(source.get("name") or "未命名来源"),
            "type": "rss",
            "xmlUrl": urls["rss"],
            "atomUrl": urls["atom"],
            "jsonUrl": urls["json"],
        }
        if home:
            attrs["htmlUrl"] = home
        if str(source.get("intro") or ""):
            attrs["description"] = str(source["intro"])
        ET.SubElement(body, "outline", attrs)
    return _xml_bytes(root)
