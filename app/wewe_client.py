"""WeWe RSS 上游适配器（只读采集 + 可选来源管理）。

契约来源：对 zhouyuan-wewe-rss 源码的静态核对（apps/server/src/feeds、
apps/server/src/trpc/trpc.router.ts）：

- GET  {base}/feeds/                          -> [{id,name,intro,cover,syncTime,updateTime}]
- GET  {base}/feeds/{feedId}.json?limit&page&mode=fulltext&update=true
                                               -> JSON Feed v1（items[].content_html 为正文 HTML）
- POST {base}/trpc/{procedure}                -> {"json": input}，响应 {"result":{"data":{"json": ...}}}

鉴权：wewe-rss 的 authService.resolveAuth 直接读取 Authorization 头作为授权码，
因此这里透传 Authorization: <AUTH_CODE>；授权码只来自环境变量，不落库、不写日志。

注意：上游未提供稳定的“网站来源采集”公共接口，因此网站来源由本仓库本地采集
（见 wewe_fetcher 的 SSRF 防护抓取链路），公众号来源统一走上游适配器。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests


class WeWeError(RuntimeError):
    """上游适配器调用失败。"""


@dataclass
class FeedItem:
    article_id: str
    title: str
    url: str
    content_html: str
    published_at: str
    image_url: str = ""
    author: str = ""
    extra: dict = field(default_factory=dict)


class WeWeRssClient:
    def __init__(
        self,
        base_url: str,
        auth_code: str = "",
        timeout: int = 20,
        max_retries: int = 3,
        session: requests.Session | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.auth_code = auth_code or ""
        self.timeout = max(1, int(timeout))
        self.max_retries = max(1, int(max_retries))
        self.session = session or requests.Session()

    # -- internals -----------------------------------------------
    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.auth_code:
            headers["Authorization"] = self.auth_code
        return headers

    def _request(self, method: str, path: str, params: dict | None = None, json_body: dict | None = None) -> object:
        if not self.base_url:
            raise WeWeError("WEWE_BASE_URL is not configured")
        url = self.base_url + path
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                if resp.status_code >= 500 and attempt < self.max_retries - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if not resp.ok:
                    raise WeWeError("upstream %s %s returned %d" % (method, path, resp.status_code))
                return resp.json()
            except WeWeError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
        raise WeWeError("upstream request failed: " + str(last_error))

    # -- feeds ----------------------------------------------------
    def list_feeds(self) -> list[dict]:
        """上游订阅源列表。"""

        data = self._request("GET", "/feeds/")
        if not isinstance(data, list):
            raise WeWeError("unexpected feed list payload")
        feeds: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            feeds.append(
                {
                    "upstream_feed_id": str(item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "intro": str(item.get("intro") or ""),
                    "cover_url": str(item.get("cover") or ""),
                    "sync_time": item.get("syncTime"),
                    "update_time": item.get("updateTime"),
                }
            )
        return feeds

    def fetch_feed_items(
        self,
        feed_id: str,
        limit: int = 30,
        page: int = 1,
        fulltext: bool = True,
        refresh: bool = False,
    ) -> list[FeedItem]:
        """拉取某个订阅源的文章列表（JSON Feed v1）。"""

        target = (feed_id or "").strip()
        if not target:
            raise WeWeError("feed id is required")
        params = {
            "limit": max(1, int(limit)),
            "page": max(1, int(page)),
        }
        if fulltext:
            params["mode"] = "fulltext"
        if refresh:
            params["update"] = "true"
        data = self._request("GET", "/feeds/%s.json" % target, params=params)
        if not isinstance(data, dict):
            raise WeWeError("unexpected feed payload")
        raw_items = data.get("items") or []
        items: list[FeedItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            article_id = str(raw.get("id") or raw.get("guid") or "").strip()
            if not article_id:
                continue
            content_html = raw.get("content_html") or raw.get("content") or ""
            author = ""
            raw_author = raw.get("author")
            if isinstance(raw_author, list) and raw_author:
                first = raw_author[0]
                if isinstance(first, dict):
                    author = str(first.get("name") or "")
                else:
                    author = str(first)
            elif isinstance(raw_author, str):
                author = raw_author
            items.append(
                FeedItem(
                    article_id=article_id,
                    title=str(raw.get("title") or ""),
                    url=str(raw.get("url") or raw.get("external_url") or ""),
                    content_html=str(content_html),
                    published_at=str(raw.get("date_published") or ""),
                    image_url=str(raw.get("image") or ""),
                    author=author,
                )
            )
        return items

    # -- trpc (可选的来源/文章管理) -------------------------------
    def trpc(self, procedure: str, payload: dict | None = None) -> object:
        data = self._request("POST", "/trpc/%s" % procedure.lstrip("/"), json_body={"json": payload or {}})
        if not isinstance(data, dict):
            raise WeWeError("unexpected trpc payload")
        if "error" in data and data.get("error"):
            message = data.get("error")
            if isinstance(message, dict):
                message = message.get("message") or json.dumps(message, ensure_ascii=False)
            raise WeWeError("trpc %s failed: %s" % (procedure, message))
        result = data.get("result")
        if isinstance(result, dict):
            inner = result.get("data")
            if isinstance(inner, dict) and "json" in inner:
                return inner.get("json")
        return result

    def list_articles(self, feed_id: str | None = None, limit: int = 100) -> list[dict]:
        payload: dict = {"limit": max(1, min(int(limit), 1000))}
        if feed_id:
            payload["feedId"] = feed_id
        data = self.trpc("article.list", payload)
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    def ping(self) -> dict:
        feeds = self.list_feeds()
        return {"ok": True, "feed_count": len(feeds)}
