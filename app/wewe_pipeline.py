"""wewe-rss 采集与图文分离索引编排。

流程：来源 -> 文章列表 -> HTML 清洗（正文/图片分离）-> 图片受限下载
-> 视觉描述（文本空间）-> 正文分块向量（文本空间）-> 原生图片向量（图片空间）
-> 记录文章与图片资产 -> 任务状态回写。

约束：
- 向量按唯一版本暂存，MySQL 发布当前版本后才对检索可见；
- 图片描述失败不阻断正文入库，原生图片向量失败则降级为仅描述文本；
- 单源文章数、单文图片数、图片字节数均受配置上限约束。
"""

from __future__ import annotations

import logging
import uuid
import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from app.config import Settings
from app.rag import split_text_with_metadata
from app.wewe_client import FeedItem, WeWeRssClient
from app.wewe_fetcher import FetchError, SafeFetcher, download_image
from app.wewe_images import ImageEmbeddingClient, WeWeVectorIndex
from app.wewe_sanitize import sanitize_html, strip_tags
from app.wewe_store import CollectionLeaseLost, WeWeStore

SOURCE_TYPE_MP = "mp"
SOURCE_TYPE_WEB = "web"
logger = logging.getLogger(__name__)


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
    except (ET.ParseError, DefusedXmlException):
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
            atom_links = node.findall("{http://www.w3.org/2005/Atom}link")
            alternate = next(
                (item for item in atom_links if item.attrib.get("rel", "alternate") == "alternate"),
                None,
            )
            link_node = alternate or next((item for item in atom_links if item.attrib.get("rel") != "self"), None)
            if link_node is not None:
                link = link_node.attrib.get("href", "")
        link = urljoin(base_url, link) if link else ""
        description = _text(
            node,
            "{http://purl.org/rss/1.0/modules/content/}encoded",
            "description",
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

    def _store_images(
        self, user_id: int, article_id: int, images: list[dict], task_id: int, vector_version: str
    ) -> list[dict]:
        stored: list[dict] = []
        limit = max(0, int(self.settings.wewe_max_images_per_article))
        for image in images[:limit]:
            self.store.assert_task_lease(task_id)
            url = str(image.get("url") or "")
            if not url or url.lower().startswith("data:"):
                continue
            image_id, existing = self.store.stage_image_identity(
                article_id, user_id, url, vector_version, task_id
            )
            try:
                data, mime, suffix = download_image(
                    self.fetcher, url, max_bytes=self.settings.wewe_max_image_bytes
                )
            except FetchError as exc:
                old_path = Path(str(existing.get("local_path") or ""))
                reusable = (
                    existing.get("status") not in {"staged", "stale"}
                    and bool(existing.get("local_path"))
                    and old_path.is_file()
                )
                if reusable:
                    stored.append({
                        "image_id": image_id,
                        "url": url,
                        "local_path": str(old_path),
                        "mime_type": str(existing.get("mime_type") or ""),
                        "byte_size": int(existing.get("byte_size") or old_path.stat().st_size),
                        "description": str(existing.get("description") or ""),
                        "alt": image.get("alt", ""),
                        "vector_version": vector_version,
                        "staged_file": False,
                    })
                else:
                    stored.append({
                        "image_id": image_id,
                        "url": url,
                        "local_path": "",
                        "mime_type": "",
                        "byte_size": 0,
                        "description": "",
                        "alt": image.get("alt", ""),
                        "vector_version": vector_version,
                        "status": "failed",
                        "error": str(exc)[:500],
                        "staged_file": False,
                    })
                logger.warning(
                    "image fetch failed article=%s image=%s reused=%s error=%s",
                    article_id, image_id, reusable, str(exc)[:200],
                )
                continue
            if len(data) > self.settings.wewe_max_image_bytes:
                continue
            path = self._image_dir(user_id, article_id) / (
                "img_%d_%s%s" % (image_id, vector_version, suffix)
            )
            try:
                with self.store.task_write_guard(task_id):
                    path.write_bytes(data)
            except Exception:
                path.unlink(missing_ok=True)
                raise
            stored.append({
                "image_id": image_id,
                "url": url,
                "local_path": str(path),
                "mime_type": mime,
                "byte_size": len(data),
                "description": "",
                "alt": image.get("alt", ""),
                "vector_version": vector_version,
                "staged_file": True,
            })
        return stored

    def _describe_images(
        self, user_id: int, article_id: int, images: list[dict], task_id: int
    ) -> list[dict]:
        described: list[dict] = []
        prompt = self.settings.vision_prompt_template
        for image in images:
            if image.get("status") == "failed":
                described.append(dict(image))
                continue
            self.store.assert_task_lease(task_id)
            description = ""
            try:
                description = self.vision.analyze_image(image_path=image["local_path"], prompt=prompt) or ""
            except Exception:
                description = ""
            description = (description or "").strip()
            image["description"] = description
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
        task_id: int,
        vector_version: str,
    ) -> dict:
        article_id = int(article["id"])
        self.store.assert_task_lease(task_id)
        source = self.store.get_source(user_id, int(article["source_id"])) or {}
        metadata = {
            "source_type": "wewe_" + str(source.get("source_type") or "mp"),
            "source_filename": str(article.get("title") or article.get("upstream_article_id") or ""),
            "source_file": str(article.get("url") or ""),
            "source_manual": str(source.get("name") or ""),
            "article_id": article_id,
            "vector_version": vector_version,
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
        desc_chunks = []
        desc_meta = []
        for image in images:
            description = (image.get("description") or "").strip()
            if not description:
                continue
            desc_chunks.append(description)
            desc_meta.append({
                "source_type": metadata["source_type"],
                "source_filename": metadata["source_filename"],
                "source_file": metadata["source_file"],
                "source_manual": metadata["source_manual"],
                "article_id": article_id,
                "vector_version": vector_version,
                "source_id": metadata["source_id"],
                "article_title": metadata["article_title"],
                "article_url": metadata["article_url"],
                "image_id": int(image["image_id"]),
                "image_url": str(image.get("url") or ""),
                "chunk_type": "image_description",
                "vector_space": "text",
            })

        image_items = []
        if self.settings.wewe_image_vector_enabled and self.image_embedding is not None:
            for image in images:
                if image.get("status") == "failed":
                    continue
                vector = self.image_embedding.embed_image_file(image["local_path"])
                if not vector:
                    continue
                image_items.append({
                    "article_id": article_id,
                    "vector_version": vector_version,
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
                })

        # Compute every embedding before mutating either vector space. The new
        # generation is invisible until update_article publishes its UUID in MySQL.
        text_vectors = self.embedding.embed_texts(chunks) if chunks else []
        desc_vectors = self.embedding.embed_texts(desc_chunks) if desc_chunks else []

        text_points = 0
        image_points = 0
        try:
            with self.store.task_write_guard(task_id):
                if chunks:
                    text_points = self.vector_index.upsert_text_chunks(
                        user_id=user_id,
                        knowledge_base_id=knowledge_base_id,
                        article_id=article_id,
                        chunks=chunks,
                        vectors=text_vectors,
                        metadata=metadata,
                    )
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
                if image_items:
                    image_points = self.vector_index.upsert_image_vectors(
                        user_id=user_id,
                        knowledge_base_id=knowledge_base_id,
                        article_id=article_id,
                        items=image_items,
                    )
        except Exception:
            try:
                self.vector_index.delete_article_version(user_id, article_id, vector_version)
            except Exception:
                logger.warning("failed to clean staged vectors article=%s version=%s", article_id, vector_version)
            raise
        return {
            "text_points": text_points,
            "image_points": image_points,
            "chunks": len(chunks),
            "chunk_count": text_points + len(desc_chunks),
            "image_count": sum(1 for image in images if image.get("status") != "failed"),
            "vector_version": vector_version,
        }

    def _cleanup_staged_image_files(self, images: list[dict]) -> None:
        """Remove only newly staged files; never unlink a reused published asset."""
        for image in images:
            if not image.get("staged_file"):
                continue
            try:
                Path(str(image.get("local_path") or "")).unlink(missing_ok=True)
            except OSError:
                logger.warning("failed to remove staged image %s", image.get("local_path"))

    # -- 主流程 ----------------------------------------------------
    def collect_source(
        self,
        user_id: int,
        source_id: int,
        knowledge_base_id: int | None = None,
        limit: int | None = None,
        refresh: bool = True,
        kind: str = "manual",
        task_id: int | None = None,
    ) -> dict:
        source = self.store.get_source(user_id, source_id)
        if not source:
            raise WeWeCollectError("source not found")
        kb_id = int(knowledge_base_id or source.get("knowledge_base_id"))
        configured_limit = max(1, int(self.settings.wewe_max_articles_per_source))
        max_articles = min(max(1, int(limit or configured_limit)), configured_limit)
        task_id = int(task_id or self.store.create_task(user_id, source_id, kind=kind))

        article_count = 0
        image_count = 0
        indexed_count = 0
        error = ""
        try:
            self.store.assert_task_lease(task_id)
            if source.get("source_type") == SOURCE_TYPE_WEB:
                items = self._fetch_web_items(source, max_articles)
            else:
                feed_id = str(source.get("upstream_feed_id") or "")
                if not self.store.owns_upstream_feed(user_id, feed_id):
                    raise WeWeCollectError("upstream feed is not owned by this user")
                items = self._fetch_mp_items(source, max_articles, refresh)
            for item in items[:max_articles]:
                self.store.assert_task_lease(task_id)
                article_count += 1
                existing_article = self.store.find_article(user_id, source_id, item.article_id)
                previous_vector_version = str((existing_article or {}).get("vector_version") or "") or None
                vector_version = uuid.uuid4().hex
                images: list[dict] = []
                try:
                    cleaned = sanitize_html(item.content_html, base_url=item.url)
                    text = cleaned["text"]
                    if not text.strip():
                        raise WeWeCollectError("article has no extractable text content")
                    if existing_article:
                        article_id = int(existing_article["id"])
                    else:
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
                            task_id=task_id,
                        )
                    article_row = dict(existing_article or self.store.get_article(user_id, article_id) or {})
                    article_row.update({
                        "id": article_id,
                        "source_id": source_id,
                        "knowledge_base_id": kb_id,
                        "upstream_article_id": item.article_id,
                        "title": item.title,
                        "url": item.url,
                        "author": item.author,
                        "published_at": item.published_at,
                        "cover_url": item.image_url,
                    })
                    images = self._store_images(
                        user_id, article_id, cleaned["images"], task_id, vector_version
                    )
                    images = self._describe_images(user_id, article_id, images, task_id)
                    image_count += sum(1 for image in images if image.get("status") != "failed")
                    index_result = self._index_article(
                        user_id, kb_id, article_row, text, images, task_id, vector_version
                    )
                    # MySQL atomically selects the visible generation. Stale points
                    # are removed only after this authoritative commit succeeds.
                    try:
                        published = self.store.update_article(
                            article_id,
                            status="indexed",
                            error="",
                            chunk_count=index_result["chunk_count"],
                            image_count=index_result["image_count"],
                            vector_version=index_result["vector_version"],
                            indexed=True,
                            text_content=text,
                            content_html=cleaned["html"],
                            title=item.title,
                            url=item.url,
                            author=item.author,
                            published_at=item.published_at,
                            cover_url=item.image_url,
                            knowledge_base_id=kb_id,
                            images=images,
                            task_id=task_id,
                        )
                        if not published:
                            raise WeWeCollectError("article publication failed")
                    except Exception:
                        try:
                            self.vector_index.delete_article_version(
                                user_id, article_id, index_result["vector_version"]
                            )
                        except Exception:
                            logger.warning("failed to clean unpublished vectors article=%s", article_id)
                        raise
                    if previous_vector_version and previous_vector_version != vector_version:
                        try:
                            # Exact old UUID is race-free even if a newer worker has published since.
                            self.vector_index.delete_article_version(
                                user_id, article_id, previous_vector_version
                            )
                        except Exception:
                            logger.warning("failed to clean replaced vectors article=%s", article_id)
                    indexed_count += 1
                except CollectionLeaseLost:
                    self._cleanup_staged_image_files(images)
                    raise
                except Exception as exc:  # 单篇失败不影响整体，失租除外
                    self._cleanup_staged_image_files(images)
                    error = str(exc)[:300]
                    try:
                        current = self.store.find_article(user_id, source_id, item.article_id)
                        if existing_article:
                            # Keep the last published version and only report the refresh error.
                            self.store.update_article(
                                int(existing_article["id"]), error=error, task_id=task_id
                            )
                        elif current:
                            self.store.update_article(
                                int(current["id"]), status="failed", error=error, task_id=task_id
                            )
                        else:
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
                                status="failed",
                                error=error,
                                task_id=task_id,
                            )
                    except CollectionLeaseLost:
                        raise
                    except Exception:
                        logger.exception("failed to persist article collection error article=%s", item.article_id)
            self.store.update_source(
                user_id,
                source_id,
                task_id=task_id,
                last_sync_at=_now(),
                last_error=error,
                status="active",
            )
        except CollectionLeaseLost as exc:
            error = str(exc)[:300]
        except Exception as exc:
            error = str(exc)[:300]
            try:
                self.store.update_source(
                    user_id,
                    source_id,
                    task_id=task_id,
                    last_error=error,
                    status="error",
                )
            except CollectionLeaseLost as lease_exc:
                error = str(lease_exc)[:300]

        status = "failed" if error and indexed_count == 0 else ("partial" if error else "success")
        finished = self.store.finish_task(
            task_id,
            status=status,
            article_count=article_count,
            image_count=image_count,
            indexed_count=indexed_count,
            error=error,
        )
        if not finished:
            status = "failed"
            error = error or "collection task lease was lost before finalization"
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

