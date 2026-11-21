"""wewe-rss 采集与图文分离索引编排。

流程：来源 -> 文章列表 -> HTML 清洗（正文/图片分离）-> 图片受限下载
-> 视觉描述（文本空间）-> 正文分块向量（文本空间）-> 原生图片向量（图片空间）
-> 记录文章与图片资产 -> 任务状态回写。

约束：
- 所有向量写入前先按 article_id 删除旧点位，保证重复采集幂等；
- 图片描述失败不阻断正文入库，原生图片向量失败则降级为仅描述文本；
- 单源文章数、单文图片数、图片字节数均受配置上限约束。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.rag import split_text_with_metadata
from app.wewe_client import FeedItem, WeWeRssClient
from app.wewe_fetcher import FetchError, SafeFetcher, download_image
from app.wewe_images import ImageEmbeddingClient, WeWeVectorIndex
from app.wewe_sanitize import sanitize_html, strip_tags
from app.wewe_store import WeWeStore

SOURCE_TYPE_MP = "mp"
SOURCE_TYPE_WEB = "web"


class WeWeCollectError(RuntimeError):
    """采集失败。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _guess_source_type(value: str) -> str:
    lowered = (value or "").strip().lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return SOURCE_TYPE_WEB
    return SOURCE_TYPE_MP


def _parse_feed_xml(text: str, base_url: str) -> list[FeedItem]:
    """解析 RSS/Atom，失败时返回空列表。"""

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    items: list[FeedItem] = []

    def _text(node, *names) -> str:
        for name in names:
            found = node.find(name)
            if found is not None and (found.text or "").strip():
                return found.text.strip()
        return ""

    nodes = root.findall(".//item")
    if not nodes:
        nodes = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for index, node in enumerate(nodes):
        title = _text(node, "title", "{http://www.w3.org/2005/Atom}title")
        link = _text(node, "link", "{http://www.w3.org/2005/Atom}link")
        if not link:
            link_node = node.find("{http://www.w3.org/2005/Atom}link")
            if link_node is not None:
                link = link_node.attrib.get("href", "")
        description = _text(
            node,
            "description",
            "{http://purl.org/rss/1.0/modules/content/}encoded",
            "{http://www.w3.org/2005/Atom}content",
            "{http://www.w3.org/2005/Atom}summary",
        )
        guid = _text(node, "guid", "{http://www.w3.org/2005/Atom}id") or link or (title + str(index))
        published = _text(node, "pubDate", "{http://www.w3.org/2005/Atom}updated", "{http://www.w3.org/2005/Atom}published")
        items.append(
            FeedItem(
                article_id=str(guid),
                title=title,
                url=link,
                content_html=description,
                published_at=published,
            )
        )
    return items


class WeWeIngestPipeline:
    def __init__(
        self,
        settings: Settings,
        store: WeWeStore,
        client: WeWeRssClient,
        vector_index: WeWeVectorIndex,
        embedding,
        vision,
        image_embedding: ImageEmbeddingClient | None = None,
        fetcher: SafeFetcher | None = None,
    ):
        self.settings = settings
        self.store = store
        self.client = client
        self.vector_index = vector_index
        self.embedding = embedding
        self.vision = vision
        self.image_embedding = image_embedding
        self.fetcher = fetcher or SafeFetcher(
            timeout=settings.wewe_timeout_seconds,
            max_bytes=settings.wewe_max_download_bytes,
        )

    # -- 外部输入 -------------------------------------------------
    def _fetch_mp_items(self, source: dict, limit: int, refresh: bool) -> list[FeedItem]:
        return self.client.fetch_feed_items(
            source.get("upstream_feed_id") or "",
            limit=limit,
            page=1,
            fulltext=True,
            refresh=refresh,
        )

    def _fetch_web_items(self, source: dict, limit: int) -> list[FeedItem]:
        target = (source.get("target_url") or "").strip()
        if not target:
            raise WeWeCollectError("web source has no target url")
        result = self.fetcher.get(target, headers={"Accept": "application/rss+xml, application/xml, text/html"})
        text = result.text
        items = _parse_feed_xml(text, result.url)
        if items:
            return items[:limit]
        # 非订阅格式：当作单页文章处理
        title = strip_tags(text)[:120] or target
        return [
            FeedItem(
                article_id="web:" + target,
                title=title,
                url=result.url,
                content_html=text,
                published_at=_now(),
            )
        ][:limit]

    # -- 图片 ------------------------------------------------------
    def _image_dir(self, user_id: int, article_id: int) -> Path:
        target = Path(self.settings.wewe_artifact_dir) / str(user_id) / str(article_id) / "images"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _store_images(self, user_id: int, article_id: int, images: list[dict]) -> list[dict]:
        stored: list[dict] = []
        limit = max(0, int(self.settings.wewe_max_images_per_article))
        for image in images[:limit]:
            url = str(image.get("url") or "")
            if not url or url.lower().startswith("data:"):
                continue
            try:
                data, mime, suffix = download_image(self.fetcher, url)
            except FetchError as exc:
                self.store.upsert_image(
                    article_id=article_id,
                    user_id=user_id,
                    url=url,
                    status="failed",
                    error=str(exc)[:300],
                )
                continue
            if len(data) > self.settings.wewe_max_image_bytes:
                continue
            image_id = self.store.upsert_image(
                article_id=article_id,
                user_id=user_id,
                url=url,
                mime_type=mime,
                byte_size=len(data),
                status="downloaded",
            )
            path = self._image_dir(user_id, article_id) / ("img_%d%s" % (image_id, suffix))
            path.write_bytes(data)
            self.store.upsert_image(
                article_id=article_id,
                user_id=user_id,
                url=url,
                local_path=str(path),
                mime_type=mime,
                byte_size=len(data),
                status="downloaded",
            )
            stored.append(
                {
                    "image_id": image_id,
                    "url": url,
                    "local_path": str(path),
                    "mime_type": mime,
                    "alt": image.get("alt", ""),
                }
            )
        return stored

    def _describe_images(self, user_id: int, article_id: int, images: list[dict]) -> list[dict]:
        described: list[dict] = []
        prompt = self.settings.vision_prompt_template
        for image in images:
            description = ""
            try:
                description = self.vision.analyze_image(image_path=image["local_path"], prompt=prompt) or ""
            except Exception:
                description = ""
            description = (description or "").strip()
            if description:
                self.store.update_image(image["image_id"], description=description, status="described")
            else:
                self.store.update_image(image["image_id"], status="described", error="vision failed")
            item = dict(image)
            item["description"] = description
            described.append(item)
        return described

    # -- 索引 ------------------------------------------------------
    def _index_article(
        self,
        user_id: int,
        knowledge_base_id: int,
        article: dict,
        text: str,
        images: list[dict],
    ) -> dict:
        article_id = int(article["id"])
        source = self.store.get_source(user_id, int(article["source_id"])) or {}
        # 幂等：先清掉该文章的历史向量
        self.vector_index.delete_article(user_id, article_id)

        metadata = {
            "source_type": "wewe_" + str(source.get("source_type") or "mp"),
            "source_filename": str(article.get("title") or article.get("upstream_article_id") or ""),
            "source_file": str(article.get("url") or ""),
            "source_manual": str(source.get("name") or ""),
            "article_id": article_id,
            "source_id": int(source.get("id") or 0),
            "article_title": str(article.get("title") or ""),
            "article_url": str(article.get("url") or ""),
            "author": str(article.get("author") or ""),
            "published_at": str(article.get("published_at") or ""),
            "chunk_type": "article_text",
            "vector_space": "text",
        }

        chunk_records = split_text_with_metadata(text, self.settings.chunk_size, self.settings.chunk_overlap)
        chunks = [str(item.get("text", "")) for item in chunk_records if str(item.get("text", "")).strip()]
        text_points = 0
        if chunks:
            vectors = self.embedding.embed_texts(chunks)
            text_points = self.vector_index.upsert_text_chunks(
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
                article_id=article_id,
                chunks=chunks,
                vectors=vectors,
                metadata=metadata,
            )

        # 视觉描述文本进入文本空间（明确标记为描述文本，不是原生图片向量）
        desc_chunks = []
        desc_meta = []
        for image in images:
            description = (image.get("description") or "").strip()
            if not description:
                continue
            desc_chunks.append(description)
            desc_meta.append(
                {
                    "source_type": metadata["source_type"],
                    "source_filename": metadata["source_filename"],
                    "source_file": metadata["source_file"],
                    "source_manual": metadata["source_manual"],
                    "article_id": article_id,
                    "source_id": metadata["source_id"],
                    "article_title": metadata["article_title"],
                    "article_url": metadata["article_url"],
                    "image_id": int(image["image_id"]),
                    "image_url": str(image.get("url") or ""),
                    "chunk_type": "image_description",
                    "vector_space": "text",
                }
            )
        if desc_chunks:
            desc_vectors = self.embedding.embed_texts(desc_chunks)
            for offset, meta in enumerate(desc_meta):
                self.vector_index.upsert_text_chunks(
                    user_id=user_id,
                    knowledge_base_id=knowledge_base_id,
                    article_id=article_id,
                    chunks=[desc_chunks[offset]],
                    vectors=[desc_vectors[offset]],
                    metadata=meta,
                    key_prefix="d" + str(offset),
                )

        # 原生图片向量进入独立图片空间
        image_points = 0
        if self.settings.wewe_image_vector_enabled and self.image_embedding is not None:
            items = []
            for image in images:
                vector = self.image_embedding.embed_image_file(image["local_path"])
                if not vector:
                    continue
                items.append(
                    {
                        "image_id": image["image_id"],
                        "vector": vector,
                        "image_url": image.get("url", ""),
                        "local_path": image.get("local_path", ""),
                        "description": image.get("description", ""),
                        "source_id": metadata["source_id"],
                        "source_type": metadata["source_type"],
                        "source_name": metadata["source_manual"],
                        "source_filename": metadata["source_filename"],
                        "source_file": metadata["source_file"],
                        "article_title": metadata["article_title"],
                        "article_url": metadata["article_url"],
                    }
                )
            image_points = self.vector_index.upsert_image_vectors(
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
                article_id=article_id,
                items=items,
            )

        self.store.update_article(
            article_id,
            status="indexed",
            error="",
            chunk_count=text_points + len(desc_chunks),
            image_count=len(images),
            indexed=True,
        )
        return {"text_points": text_points, "image_points": image_points, "chunks": len(chunks)}

    # -- 主流程 ----------------------------------------------------
    def collect_source(
        self,
        user_id: int,
        source_id: int,
        knowledge_base_id: int | None = None,
        limit: int | None = None,
        refresh: bool = True,
        kind: str = "manual",
    ) -> dict:
        source = self.store.get_source(user_id, source_id)
        if not source:
            raise WeWeCollectError("source not found")
        kb_id = int(knowledge_base_id or source.get("knowledge_base_id"))
        max_articles = max(1, int(limit or self.settings.wewe_max_articles_per_source))
        task_id = self.store.create_task(user_id, source_id, kind=kind)

        article_count = 0
        image_count = 0
        indexed_count = 0
        error = ""
        try:
            if source.get("source_type") == SOURCE_TYPE_WEB:
                items = self._fetch_web_items(source, max_articles)
            else:
                items = self._fetch_mp_items(source, max_articles, refresh)
            for item in items[:max_articles]:
                article_count += 1
                try:
                    cleaned = sanitize_html(item.content_html, base_url=item.url)
                    text = cleaned["text"]
                    if not text:
                        text = strip_tags(item.content_html)
                    if not text.strip():
                        raise WeWeCollectError("article has no extractable text content")
                    article_id, _created = self.store.upsert_article(
                        user_id=user_id,
                        source_id=source_id,
                        knowledge_base_id=kb_id,
                        upstream_article_id=item.article_id,
                        title=item.title,
                        url=item.url,
                        author=item.author,
                        published_at=item.published_at,
                        cover_url=item.image_url,
                        content_html=cleaned["html"],
                        text_content=text,
                        status="fetched",
                    )
                    images = self._store_images(user_id, article_id, cleaned["images"])
                    images = self._describe_images(user_id, article_id, images)
                    image_count += len(images)
                    article_row = self.store.get_article(user_id, article_id) or {}
                    self._index_article(user_id, kb_id, article_row, text, images)
                    indexed_count += 1
                except Exception as exc:  # 单篇失败不影响整体
                    error = str(exc)[:300]
                    try:
                        article_id, _created = self.store.upsert_article(
                            user_id=user_id,
                            source_id=source_id,
                            knowledge_base_id=kb_id,
                            upstream_article_id=item.article_id,
                            title=item.title,
                            url=item.url,
                            status="failed",
                            error=error,
                        )
                        self.store.update_article(article_id, status="failed", error=error)
                    except Exception:
                        pass
            self.store.update_source(user_id, source_id, last_sync_at=_now(), last_error=error, status="active")
        except Exception as exc:
            error = str(exc)[:300]
            self.store.update_source(user_id, source_id, last_error=error, status="error")

        status = "failed" if error and indexed_count == 0 else ("partial" if error else "success")
        self.store.finish_task(
            task_id,
            status=status,
            article_count=article_count,
            image_count=image_count,
            indexed_count=indexed_count,
            error=error,
        )
        return {
            "task_id": task_id,
            "status": status,
            "article_count": article_count,
            "image_count": image_count,
            "indexed_count": indexed_count,
            "error": error,
        }

    def delete_article(self, user_id: int, article_id: int) -> dict:
        article = self.store.get_article(user_id, article_id)
        if not article:
            raise WeWeCollectError("article not found")
        self.vector_index.delete_article(user_id, article_id)
        self.store.delete_article(user_id, article_id)
        return {"deleted": True, "article_id": article_id}

