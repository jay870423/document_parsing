"""wewe-rss 集成的 HTTP 接口层。

接口全部走现有会话鉴权（current_user），并沿用知识库归属校验，
保证跨用户/跨知识库隔离。
"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import Settings
from app.wewe_client import WeWeRssClient
from app.wewe_images import WeWeVectorIndex
from app.wewe_pipeline import WeWeCollectError, WeWeIngestPipeline
from app.wewe_store import WeWeStore


class SourceCreateRequest(BaseModel):
    source_type: str = Field(default="mp", max_length=16)
    upstream_feed_id: str = Field(default="", max_length=128)
    target_url: str = Field(default="", max_length=2000)
    name: str = Field(default="", max_length=200)
    intro: str = Field(default="", max_length=1000)
    cover_url: str = Field(default="", max_length=2000)
    knowledge_base_id: int | None = Field(default=None, gt=0)
    subscribed: bool = True


class SourceUpdateRequest(BaseModel):
    subscribed: bool | None = None
    name: str | None = Field(default=None, max_length=200)
    knowledge_base_id: int | None = Field(default=None, gt=0)


class CollectRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=200)
    refresh: bool = True


class ImageSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=30)
    knowledge_base_id: int | None = Field(default=None, gt=0)


def build_wewe_router(
    current_user: Callable,
    store: WeWeStore,
    pipeline: WeWeIngestPipeline,
    vector_index: WeWeVectorIndex,
    client: WeWeRssClient,
    settings: Settings,
    resolve_kb: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api/wewe", tags=["wewe-rss"])

    def _ensure_enabled() -> None:
        if not settings.wewe_enabled:
            raise HTTPException(status_code=503, detail="wewe-rss integration is disabled")

    @router.get("/status")
    def status(user=Depends(current_user)) -> dict:
        upstream_ok = False
        feed_count = 0
        error = ""
        if settings.wewe_base_url:
            try:
                result = client.ping()
                upstream_ok = bool(result.get("ok"))
                feed_count = int(result.get("feed_count") or 0)
            except Exception as exc:
                error = str(exc)[:200]
        return {
            "enabled": bool(settings.wewe_enabled),
            "upstream_configured": bool(settings.wewe_base_url),
            "upstream_ok": upstream_ok,
            "upstream_feed_count": feed_count,
            "upstream_error": error,
            "image_vector_enabled": bool(settings.wewe_image_vector_enabled),
            "image_collection": settings.wewe_image_collection,
            "scheduled_collect": bool(settings.wewe_collect_enabled),
            "user_id": user.id,
        }

    @router.get("/feeds")
    def list_feeds(user=Depends(current_user)) -> dict:
        _ensure_enabled()
        try:
            feeds = client.list_feeds()
        except Exception as exc:
            raise HTTPException(status_code=502, detail="upstream feed list failed: " + str(exc)[:200]) from exc
        return {"count": len(feeds), "feeds": feeds}

    @router.get("/sources")
    def list_sources(
        knowledge_base_id: int | None = None,
        source_type: str | None = None,
        user=Depends(current_user),
    ) -> dict:
        items = store.list_sources(
            user_id=user.id,
            knowledge_base_id=knowledge_base_id,
            source_type=source_type,
        )
        return {"count": len(items), "sources": items}

    @router.post("/sources")
    def create_source(payload: SourceCreateRequest, user=Depends(current_user)) -> dict:
        _ensure_enabled()
        source_type = payload.source_type if payload.source_type in ("mp", "web") else "mp"
        if source_type == "web":
            if not payload.target_url.strip():
                raise HTTPException(status_code=400, detail="target_url is required for web source")
        elif not payload.upstream_feed_id.strip():
            raise HTTPException(status_code=400, detail="upstream_feed_id is required for mp source")

        # 未指定知识库时落到用户的默认知识库，避免向量写入 knowledge_base_id=0 导致检索不到
        kb = resolve_kb(user.id, payload.knowledge_base_id)
        kb_id = int(kb["id"])

        name = payload.name.strip()
        intro = payload.intro.strip()
        cover = payload.cover_url.strip()
        if source_type == "mp" and payload.upstream_feed_id and not name:
            try:
                for feed in client.list_feeds():
                    if feed["upstream_feed_id"] == payload.upstream_feed_id:
                        name = feed.get("name") or payload.upstream_feed_id
                        intro = intro or feed.get("intro") or ""
                        cover = cover or feed.get("cover_url") or ""
                        break
            except Exception:
                name = name or payload.upstream_feed_id

        source_id = store.add_source(
            user_id=user.id,
            knowledge_base_id=kb_id,
            source_type=source_type,
            name=name or payload.upstream_feed_id or payload.target_url,
            upstream_feed_id=payload.upstream_feed_id.strip(),
            intro=intro,
            cover_url=cover,
            target_url=payload.target_url.strip(),
            subscribed=payload.subscribed,
        )
        return {"source_id": source_id, "source": store.get_source(user.id, source_id)}

    @router.patch("/sources/{source_id}")
    def update_source(source_id: int, payload: SourceUpdateRequest, user=Depends(current_user)) -> dict:
        source = store.get_source(user.id, source_id)
        if not source:
            raise HTTPException(status_code=404, detail="source not found")
        fields = {}
        if payload.subscribed is not None:
            fields["subscribed"] = payload.subscribed
        if payload.name is not None:
            fields["name"] = payload.name
        if payload.knowledge_base_id is not None:
            resolve_kb(user.id, payload.knowledge_base_id)
            fields["knowledge_base_id"] = int(payload.knowledge_base_id)
        store.update_source(user.id, source_id, **fields)
        return {"source": store.get_source(user.id, source_id)}

    @router.delete("/sources/{source_id}")
    def delete_source(source_id: int, user=Depends(current_user)) -> dict:
        source = store.get_source(user.id, source_id)
        if not source:
            raise HTTPException(status_code=404, detail="source not found")
        articles = store.list_articles(user.id, source_id=source_id, limit=200)
        for article in articles:
            vector_index.delete_article(user.id, int(article["id"]))
        store.delete_source(user.id, source_id)
        return {"deleted": True, "source_id": source_id, "removed_articles": len(articles)}

    @router.post("/sources/{source_id}/collect")
    def collect_source(source_id: int, payload: CollectRequest, user=Depends(current_user)) -> dict:
        _ensure_enabled()
        source = store.get_source(user.id, source_id)
        if not source:
            raise HTTPException(status_code=404, detail="source not found")
        kb = resolve_kb(user.id, int(source.get("knowledge_base_id") or 0) or None)
        try:
            result = pipeline.collect_source(
                user_id=user.id,
                source_id=source_id,
                knowledge_base_id=int(kb["id"]),
                limit=payload.limit,
                refresh=payload.refresh,
                kind="manual",
            )
        except WeWeCollectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result

    @router.get("/articles")
    def list_articles(
        source_id: int | None = None,
        knowledge_base_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        user=Depends(current_user),
    ) -> dict:
        items = store.list_articles(
            user_id=user.id,
            knowledge_base_id=knowledge_base_id,
            source_id=source_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        for item in items:
            item.pop("content_html", None)
            item.pop("text_content", None)
        return {"count": len(items), "articles": items}

    @router.get("/articles/{article_id}")
    def get_article(article_id: int, user=Depends(current_user)) -> dict:
        article = store.get_article(user.id, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="article not found")
        images = store.list_images(article_id)
        source = store.get_source(user.id, int(article["source_id"])) or {}
        return {
            "article": article,
            "source": {
                "id": source.get("id"),
                "name": source.get("name"),
                "source_type": source.get("source_type"),
                "upstream_feed_id": source.get("upstream_feed_id"),
            },
            "images": images,
        }

    @router.delete("/articles/{article_id}")
    def delete_article(article_id: int, user=Depends(current_user)) -> dict:
        try:
            return pipeline.delete_article(user.id, article_id)
        except WeWeCollectError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/tasks")
    def list_tasks(limit: int = 50, user=Depends(current_user)) -> dict:
        items = store.list_tasks(user.id, limit=limit)
        return {"count": len(items), "tasks": items}

    @router.post("/images/search")
    def search_images(payload: ImageSearchRequest, user=Depends(current_user)) -> dict:
        _ensure_enabled()
        if not settings.wewe_image_vector_enabled:
            raise HTTPException(status_code=503, detail="native image vectors are disabled")
        kb = resolve_kb(user.id, payload.knowledge_base_id)
        vector = pipeline.image_embedding.embed_text(payload.query) if pipeline.image_embedding else None
        if not vector:
            raise HTTPException(status_code=502, detail="image embedding failed")
        hits = vector_index.search_images(
            query_vector=vector,
            user_id=user.id,
            knowledge_base_id=int(kb["id"]),
            top_k=payload.top_k,
        )
        return {
            "query": payload.query,
            "knowledge_base_id": kb["id"],
            "vector_space": "image",
            "count": len(hits),
            "hits": hits,
        }

    return router

