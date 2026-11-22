"""wewe-rss 集成的 HTTP 接口层。

接口全部走现有会话鉴权（current_user），并沿用知识库归属校验，
保证跨用户/跨知识库隔离。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.config import Settings
from app.wewe_client import WeWeRssClient
from app.wewe_feed import render_atom, render_json_feed, render_opml, render_rss
from app.wewe_fetcher import sniff_image
from app.wewe_images import WeWeVectorIndex, public_image_hit
from app.wewe_onboarding import LoginSessionRegistry, login_qr_svg
from app.wewe_pipeline import WeWeCollectError, WeWeIngestPipeline
from app.wewe_runner import WeWeCollectionRunner
from app.wewe_store import CollectionAlreadyRunning, WeWeStore


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


class SubscriptionRequest(BaseModel):
    share_link: str = Field(min_length=1, max_length=2000)
    knowledge_base_id: int | None = Field(default=None, gt=0)
    limit: int | None = Field(default=30, ge=1, le=200)


def build_wewe_router(
    current_user: Callable,
    store: WeWeStore,
    pipeline: WeWeIngestPipeline,
    vector_index: WeWeVectorIndex,
    client: WeWeRssClient,
    settings: Settings,
    resolve_kb: Callable,
    collection_runner: WeWeCollectionRunner | None = None,
    current_image_user: Callable | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/wewe", tags=["wewe-rss"])
    runner = collection_runner or WeWeCollectionRunner(store, pipeline)
    login_sessions = LoginSessionRegistry()

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
                feed_count = len(store.list_upstream_feed_ids(user.id))
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

    @router.get("/accounts")
    def list_accounts(user=Depends(current_user)) -> dict:
        _ensure_enabled()
        try:
            items = client.list_accounts()
            owned_ids = store.list_upstream_account_ids(user.id)
            items = [item for item in items if str(item.get("id") or "") in owned_ids]
        except Exception as exc:
            raise HTTPException(status_code=502, detail="读书账号状态暂时不可用，请稍后重试") from exc
        accounts = []
        for item in items:
            status_value = int(item.get("status") or 0)
            blocked = bool(item.get("blocked"))
            label = "今日小黑屋" if blocked else {0: "失效", 1: "正常", 2: "禁用"}.get(status_value, "未知")
            accounts.append({
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "微信读书账号"),
                "status": status_value,
                "blocked": blocked,
                "status_label": label,
                "available": status_value == 1 and not blocked,
                "updated_at": str(item.get("updated_at") or ""),
            })
        return {"count": len(accounts), "accounts": accounts}

    @router.post("/accounts/login-sessions")
    def create_login_session(user=Depends(current_user)) -> dict:
        _ensure_enabled()
        try:
            login = client.create_login_url()
            expires_in = login_sessions.put(login["login_id"], user.id)
            qr_svg = login_qr_svg(login["scan_url"])
        except Exception as exc:
            raise HTTPException(status_code=502, detail="二维码生成失败，请检查读书服务后重试") from exc
        return {
            "login_id": login["login_id"],
            "scan_url": login["scan_url"],
            "qr_svg": qr_svg,
            "expires_in": expires_in,
            "status": "waiting",
        }

    @router.get("/accounts/login-sessions/{login_id}")
    def get_login_session(login_id: str, user=Depends(current_user)) -> dict:
        _ensure_enabled()
        try:
            login_sessions.check(login_id, user.id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="二维码会话不存在或已过期，请重新生成") from exc
        try:
            result = client.get_login_result(login_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="扫码状态查询失败，请稍后重试") from exc
        if result.get("vid") and result.get("token") and result.get("username"):
            try:
                account = client.add_account(result)
                store.bind_upstream_account(user.id, str(account["id"]))
            except Exception as exc:
                raise HTTPException(status_code=502, detail="读书账号绑定失败，请重试") from exc
            login_sessions.pop(login_id, user.id)
            return {"status": "success", "account": account}
        if any(result.get(key) for key in ("vid", "token", "username")):
            login_sessions.pop(login_id, user.id)
            raise HTTPException(status_code=502, detail="扫码登录结果不完整，请重新扫码")
        message = str(result.get("message") or "")
        if not message or message == "等待扫码":
            return {"status": "waiting", "message": message or "等待微信扫码确认"}
        login_sessions.pop(login_id, user.id)
        return {"status": "failed", "message": message[:160]}

    @router.delete("/accounts/login-sessions/{login_id}")
    def cancel_login_session(login_id: str, user=Depends(current_user)) -> dict:
        try:
            login_sessions.pop(login_id, user.id)
        except KeyError:
            pass
        return {"cancelled": True}

    @router.post("/subscriptions", status_code=202)
    def subscribe_mp(payload: SubscriptionRequest, user=Depends(current_user)) -> dict:
        _ensure_enabled()
        share_link = payload.share_link.strip()
        if not share_link.startswith("https://mp.weixin.qq.com/s/"):
            raise HTTPException(status_code=400, detail="请粘贴以 https://mp.weixin.qq.com/s/ 开头的公众号文章分享链接")
        kb = resolve_kb(user.id, payload.knowledge_base_id)
        try:
            info = client.get_mp_info(share_link)
            feed = client.add_feed(info)
            store.bind_upstream_feed(user.id, feed["upstream_feed_id"])
        except Exception as exc:
            raise HTTPException(status_code=502, detail="公众号订阅失败，请确认已有可用读书账号、链接有效，并稍后再试") from exc
        source_id = store.add_source(
            user_id=user.id,
            knowledge_base_id=int(kb["id"]),
            source_type="mp",
            name=feed["name"],
            upstream_feed_id=feed["upstream_feed_id"],
            intro=feed.get("intro") or "",
            cover_url=feed.get("cover_url") or "",
            subscribed=True,
        )
        try:
            collection = runner.start(
                user_id=user.id,
                source_id=source_id,
                knowledge_base_id=int(kb["id"]),
                limit=payload.limit,
                refresh=True,
            )
            already_running = False
        except CollectionAlreadyRunning:
            running = next(
                (item for item in store.list_tasks(user.id, limit=100)
                 if int(item.get("source_id") or 0) == source_id and item.get("status") == "running"),
                None,
            )
            if running is None:
                raise HTTPException(status_code=409, detail="该公众号正在采集，请稍后查看任务状态")
            collection = {"task_id": int(running["id"]), "source_id": source_id, "status": "running"}
            already_running = True
        return {
            "source": store.get_source(user.id, source_id),
            "collection": collection,
            "already_running": already_running,
            "message": "公众号已订阅，文章正在后台采集",
        }

    @router.get("/feeds")
    def list_feeds(user=Depends(current_user)) -> dict:
        _ensure_enabled()
        try:
            feeds = client.list_feeds()
            owned_ids = store.list_upstream_feed_ids(user.id)
            feeds = [feed for feed in feeds if str(feed.get("upstream_feed_id") or "") in owned_ids]
        except Exception as exc:
            raise HTTPException(status_code=502, detail="公众号订阅列表暂时不可用，请稍后重试") from exc
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
        elif not store.owns_upstream_feed(user.id, payload.upstream_feed_id.strip()):
            raise HTTPException(status_code=403, detail="请先通过本公众号文章分享链接完成订阅")

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

    @router.post("/sources/{source_id}/collect", status_code=202)
    def collect_source(source_id: int, payload: CollectRequest, user=Depends(current_user)) -> dict:
        _ensure_enabled()
        source = store.get_source(user.id, source_id)
        if not source:
            raise HTTPException(status_code=404, detail="source not found")
        kb = resolve_kb(user.id, int(source.get("knowledge_base_id") or 0) or None)
        try:
            return runner.start(
                user_id=user.id,
                source_id=source_id,
                knowledge_base_id=int(kb["id"]),
                limit=payload.limit,
                refresh=payload.refresh,
            )
        except CollectionAlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _feed_payload(source_id: int, user) -> tuple[dict, list[dict]]:
        source = store.get_source(user.id, source_id)
        if not source:
            raise HTTPException(status_code=404, detail="source not found")
        articles = store.list_articles(user.id, source_id=source_id, limit=200)
        return source, articles

    def _feed_response(content: bytes, media_type: str) -> Response:
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.get("/sources/{source_id}/feed.rss")
    def source_rss(source_id: int, request: Request, user=Depends(current_user)) -> Response:
        source, articles = _feed_payload(source_id, user)
        return _feed_response(render_rss(source, articles, str(request.base_url)), "application/rss+xml")

    @router.get("/sources/{source_id}/feed.atom")
    def source_atom(source_id: int, request: Request, user=Depends(current_user)) -> Response:
        source, articles = _feed_payload(source_id, user)
        return _feed_response(render_atom(source, articles, str(request.base_url)), "application/atom+xml")

    @router.get("/sources/{source_id}/feed.json")
    def source_json_feed(source_id: int, request: Request, user=Depends(current_user)) -> Response:
        source, articles = _feed_payload(source_id, user)
        return _feed_response(render_json_feed(source, articles, str(request.base_url)), "application/feed+json")

    @router.get("/opml")
    def export_opml(request: Request, user=Depends(current_user)) -> Response:
        sources = [item for item in store.list_sources(user.id) if bool(item.get("subscribed"))]
        return _feed_response(render_opml(sources, str(request.base_url)), "text/x-opml")

    @router.get("/images/{image_id}/file")
    def image_file(image_id: int, user=Depends(current_image_user or current_user)) -> Response:
        image = store.get_image(user.id, image_id)
        if not image or not image.get("local_path"):
            raise HTTPException(status_code=404, detail="image not found")
        root = (
            Path(settings.wewe_artifact_dir).resolve()
            / str(user.id)
            / str(int(image["article_id"]))
            / "images"
        ).resolve()
        path = Path(str(image["local_path"])).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="image not found") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="image not found")
        maximum = max(1, int(settings.wewe_max_image_bytes))
        if path.stat().st_size > maximum:
            raise HTTPException(status_code=404, detail="image not found")
        data = path.read_bytes()
        sniffed = sniff_image(data)
        if not sniffed:
            raise HTTPException(status_code=404, detail="image not found")
        return Response(
            content=data,
            media_type=sniffed[0],
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            },
        )

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
    def get_article(article_id: int, response: Response, user=Depends(current_user)) -> dict:
        response.headers["Cache-Control"] = "private, no-store"
        article = store.get_article(user.id, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="article not found")
        images = store.list_images(article_id)
        for image in images:
            image.pop("local_path", None)
            image["image_file_url"] = f"/api/wewe/images/{int(image['id'])}/file"
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
            article_versions=store.published_vector_versions(user.id, int(kb["id"])),
            top_k=payload.top_k,
        )
        return {
            "query": payload.query,
            "knowledge_base_id": kb["id"],
            "vector_space": "image",
            "count": len(hits),
            "hits": [public_image_hit(hit) for hit in hits],
        }

    return router

