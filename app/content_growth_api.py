"""Session-authenticated HTTP API for the content-growth paid MVP."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Callable, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from app.billing import BillingStore, CallReservation, UserContext, estimate_tokens
from app.content_growth import (
    build_source_package,
    normalise_brief,
    normalise_draft,
    normalise_style_profile,
    parse_ai_object,
    render_draft_export,
    source_prompt,
)
from app.content_growth_store import ContentGrowthStore, DraftRevisionConflict
from app.content_growth_store import OperationConflict
from app.mysql import Connection
from app.wewe_store import WeWeStore

logger = logging.getLogger(__name__)


class ProfileRequest(BaseModel):
    knowledge_base_id: int = Field(gt=0)
    audience: str = Field(default="", max_length=1000)
    goal: str = Field(default="", max_length=1000)
    cta: str = Field(default="", max_length=1000)
    forbidden: str = Field(default="", max_length=2000)


class OwnArticleRequest(BaseModel):
    knowledge_base_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=100, max_length=100_000)
    url: str = Field(default="", max_length=2000)
    published_at: str = Field(default="", max_length=40)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith(("https://", "http://")):
            raise ValueError("文章链接仅支持 http/https")
        return value


class KnowledgeBaseRequest(BaseModel):
    knowledge_base_id: int = Field(gt=0)
    operation_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class StyleActivateRequest(BaseModel):
    knowledge_base_id: int = Field(gt=0)
    style_profile: dict


class BriefRequest(BaseModel):
    knowledge_base_id: int = Field(gt=0)
    operation_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    focus: str = Field(default="", max_length=2000)
    analysis_mode: Literal["structure", "promotion"] = "structure"
    reference_article_ids: list[int] = Field(default_factory=list, max_length=12)


class DraftCreateRequest(BaseModel):
    knowledge_base_id: int = Field(gt=0)
    operation_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    brief_id: int | None = Field(default=None, gt=0)
    topic: str = Field(default="", max_length=2000)
    target_length: int = Field(default=1200, ge=500, le=3000)
    additional_instructions: str = Field(default="", max_length=2000)


class DraftUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body_markdown: str | None = Field(default=None, min_length=1, max_length=100_000)
    status: Literal["draft", "reviewed", "exported", "archived"] | None = None
    expected_revision: int = Field(ge=1)


class MetricRequest(BaseModel):
    knowledge_base_id: int = Field(gt=0)
    own_article_id: int = Field(gt=0)
    metric_date: str = Field(min_length=10, max_length=10)
    window: Literal["24h", "72h", "7d"] = "7d"
    reads: int | None = Field(default=None, ge=0, le=1_000_000_000)
    shares: int | None = Field(default=None, ge=0, le=1_000_000_000)
    new_follows: int | None = Field(default=None, ge=0, le=1_000_000_000)
    landing_clicks: int | None = Field(default=None, ge=0, le=1_000_000_000)
    registrations: int | None = Field(default=None, ge=0, le=1_000_000_000)
    payments: int | None = Field(default=None, ge=0, le=1_000_000_000)
    account_label: str = Field(min_length=1, max_length=200)
    source_name: str = Field(min_length=1, max_length=500)
    campaign_id: str = Field(default="", max_length=200)

    @field_validator("account_label", "source_name")
    @classmethod
    def require_visible_provenance(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("账号标识与数据来源不能只包含空白字符")
        return value

    @field_validator("campaign_id")
    @classmethod
    def trim_campaign(cls, value: str) -> str:
        return value.strip()


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return round(numerator / denominator, 6)


def _public_metric(row: dict) -> dict:
    item = dict(row)
    item["window"] = item.pop("window_name", "")
    item["observed_rates"] = {
        "share_rate": _ratio(item["shares"], item["reads"]),
        "landing_click_rate": _ratio(item["landing_clicks"], item["reads"]),
        "registration_per_click": _ratio(item["registrations"], item["landing_clicks"]),
        "payment_per_read": _ratio(item["payments"], item["reads"]),
        "payment_per_registration": _ratio(item["payments"], item["registrations"]),
    }
    numeric = ("reads", "shares", "new_follows", "landing_clicks", "registrations", "payments")
    missing = [key for key in numeric if item.get(key) is None]
    item["sample_note"] = (
        "单账号、单窗口观察值；未提供：" + "、".join(missing)
        if missing else "单账号、单窗口观察值；所有计数均已提供"
    )
    item["rate_note"] = (
        "比率仅在同一记录的分子和分母都已提供且分母非零时计算；"
        "跨系统指标（点击/注册/支付）必须核对 account_label、source_name、campaign_id，"
        "只能描述相关性，不能据此声称自然增量或因果。"
    )
    item["interpretation"] = "descriptive_only"
    return item


def _draft_summary(row: dict) -> dict:
    item = dict(row)
    item.pop("body_markdown", None)
    item.pop("sources", None)
    item.pop("citation_map", None)
    return item


def _draft_public(row: dict) -> dict:
    keys = {
        "id", "knowledge_base_id", "brief_id", "title", "body_markdown", "status",
        "revision", "created_at", "updated_at", "title_options", "outline", "citation_map",
        "unresolved_facts", "risk_notes", "suggested_cta", "evidence_invalidated", "sources",
    }
    return {key: value for key, value in row.items() if key in keys}


def build_content_growth_router(
    *,
    current_user: Callable,
    store: ContentGrowthStore,
    reference_store: WeWeStore,
    chat,
    billing_store: BillingStore,
    resolve_kb: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api/growth", tags=["content-growth"])

    def _kb(user: UserContext, knowledge_base_id: int) -> dict:
        try:
            result = resolve_kb(user.id, knowledge_base_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=404, detail="知识库不存在")
        return result

    def _reserve(user: UserContext, conn: Connection) -> CallReservation | None:
        if user.is_admin:
            return None
        return billing_store.reserve_call(user.id, conn=conn)

    def _settle(
        user: UserContext, reservation: CallReservation | None, endpoint: str,
        status_code: int, started: float, input_tokens: int = 0, output_tokens: int = 0,
        *, conn: Connection,
    ) -> None:
        success = 200 <= status_code < 400
        billing_store.consume_call(
            user.id, None, endpoint, "POST", status_code,
            int((time.monotonic() - started) * 1000),
            input_tokens=input_tokens, output_tokens=output_tokens,
            meta={"product": "content_growth", "token_source": "estimated"},
            call_count=1 if success and reservation is not None else 0,
            reservation=reservation,
            conn=conn,
        )

    def _operation_hash(payload: BaseModel) -> tuple[str, str, dict]:
        operation_id = str(payload.model_dump()["operation_id"])
        request = payload.model_dump(exclude={"operation_id"})
        request_hash = hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return operation_id, request_hash, request

    def _paid_replay(user: UserContext, endpoint: str, payload: BaseModel) -> dict | None:
        operation_id, request_hash, _request = _operation_hash(payload)
        try:
            response = store.replay_succeeded_operation(user.id, endpoint, operation_id, request_hash)
        except OperationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if response is None:
            return None
        return dict(response) | {"idempotent_replay": True}

    def _paid_checkpoint(user: UserContext, endpoint: str, payload: BaseModel) -> dict | None:
        operation_id, request_hash, _request = _operation_hash(payload)
        try:
            return store.get_prepared_operation(user.id, endpoint, operation_id, request_hash)
        except OperationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _paid_json(
        user: UserContext,
        endpoint: str,
        payload: BaseModel,
        context_factory: Callable[[], dict],
        operation: Callable[[dict, dict], tuple[dict, int, int]],
        finalize: Callable[[Connection, dict], dict],
    ) -> dict:
        # Committed responses and post-AI checkpoints are both recoverable without
        # revalidating mutable sources. This is essential if an old source was deleted.
        replay = _paid_replay(user, endpoint, payload)
        if replay is not None:
            return replay
        checkpoint = _paid_checkpoint(user, endpoint, payload)
        context = None if checkpoint is not None else context_factory()
        operation_id, request_hash, request = _operation_hash(payload)
        try:
            # Expire an abandoned reservation before begin_operation inspects its ledger.
            billing_store.reconcile_abandoned_reservations()
            begun = store.begin_operation(
                user.id, endpoint, operation_id, request_hash,
                lambda conn: _reserve(user, conn),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=402, detail=str(exc)) from exc
        except OperationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if begun["state"] == "succeeded":
            return dict(begun["response"]) | {"idempotent_replay": True}

        reservation = begun["reservation"]
        reservation_id = reservation.reservation_id if reservation is not None else None
        started = time.monotonic()
        usage = {"input_tokens": 0, "output_tokens": 0}
        if checkpoint is None:
            checkpoint = begun.get("checkpoint")
        if checkpoint is not None and not checkpoint.get("usage_reported", False):
            # A hard crash may have left no usage event; a normal failed attempt has
            # already recorded its estimated tokens and must not count them twice.
            usage["input_tokens"] = int(checkpoint.get("input_tokens") or 0)
            usage["output_tokens"] = int(checkpoint.get("output_tokens") or 0)
        try:
            if checkpoint is None:
                artifact, prompt_tokens, output_tokens = operation(context or {}, usage)
                usage["input_tokens"] = prompt_tokens
                usage["output_tokens"] = output_tokens
                checkpoint = {
                    "artifact": artifact,
                    "input_tokens": prompt_tokens,
                    "output_tokens": output_tokens,
                    "usage_reported": False,
                }
                # This transaction commits before final artifact/billing settlement.
                # If the worker dies now, a same-id retry resumes without another AI call.
                with store.connect() as conn:
                    store.checkpoint_operation(
                        conn, user.id, endpoint, operation_id, reservation_id, checkpoint,
                    )
            with store.connect() as conn:
                store.lock_kb(conn, user.id, int(request["knowledge_base_id"]))
                result = finalize(conn, checkpoint["artifact"])
                _settle(
                    user, reservation, endpoint, 200, started,
                    input_tokens=int(usage["input_tokens"]),
                    output_tokens=int(usage["output_tokens"]), conn=conn,
                )
                store.complete_operation(
                    conn, user.id, endpoint, operation_id, reservation_id, result,
                )
            return result
        except Exception as exc:
            failure_status = exc.status_code if isinstance(exc, HTTPException) else 502
            try:
                with store.connect() as conn:
                    _settle(
                        user, reservation, endpoint, failure_status, started,
                        input_tokens=int(usage["input_tokens"]),
                        output_tokens=int(usage["output_tokens"]), conn=conn,
                    )
                    if checkpoint is not None:
                        checkpoint["usage_reported"] = True
                    store.fail_operation(
                        conn, user.id, endpoint, operation_id, reservation_id, checkpoint,
                    )
            except Exception:
                logger.exception("content growth settlement failed at %s", endpoint)
                raise HTTPException(
                    status_code=500,
                    detail="生成未完成，系统正在对账；请稍后使用相同操作编号重试，不要新建任务",
                ) from exc
            if isinstance(exc, HTTPException):
                raise
            logger.warning("content growth generation failed at %s: %s", endpoint, type(exc).__name__)
            raise HTTPException(
                status_code=502,
                detail="生成未完成，本次额度已退回；请使用相同操作编号重试，已保存的 AI 结果不会重复生成",
            ) from exc

    def _generate(chat_client, system: str, prompt: str, max_tokens: int) -> str:
        return chat_client.generate_text(
            system_prompt=system, user_prompt=prompt,
            temperature=0.2, max_tokens=max_tokens,
        )

    def _references(user_id: int, knowledge_base_id: int, ids: list[int]) -> list[dict]:
        if ids:
            rows = []
            for article_id in dict.fromkeys(ids):
                row = reference_store.get_article(user_id, article_id)
                if not row or int(row.get("knowledge_base_id") or 0) != knowledge_base_id:
                    raise HTTPException(status_code=422, detail="所选参考文章不存在或不属于当前知识库")
                rows.append(row)
        else:
            rows = reference_store.list_articles(
                user_id, knowledge_base_id=knowledge_base_id, status="indexed", limit=12,
            )
        rows = [row for row in rows if str(row.get("text_content") or "").strip()]
        if not rows:
            raise HTTPException(status_code=422, detail="请先在账号与采集中获取参考文章")
        return rows[:12]

    def _own_rows(user_id: int, knowledge_base_id: int, limit: int = 6) -> list[dict]:
        return [row for row in store.list_own_articles(user_id, knowledge_base_id, limit) if row.get("body")]

    @router.get("/workspace")
    def workspace(knowledge_base_id: int, user: UserContext = Depends(current_user)) -> dict:
        _kb(user, knowledge_base_id)
        own = []
        for row in store.list_own_articles(user.id, knowledge_base_id, 50):
            item = dict(row)
            item["excerpt"] = str(item.pop("body", ""))[:240]
            own.append(item)
        references = []
        for row in reference_store.list_articles(user.id, knowledge_base_id=knowledge_base_id, limit=30):
            item = dict(row)
            item.pop("content_html", None)
            item.pop("text_content", None)
            item.pop("error", None)
            references.append(item)
        drafts = [_draft_summary(row) for row in store.list_drafts(user.id, knowledge_base_id)]
        metrics = [_public_metric(row) for row in store.list_metrics(user.id, knowledge_base_id)]
        if user.is_admin:
            billing = {"plan_name": "超管不限量", "remaining_calls": None, "ai_success_cost": 1}
        else:
            subscription = billing_store.get_active_subscription(user.id)
            billing = {
                "plan_name": subscription["plan_name"] if subscription else "暂无有效套餐",
                "remaining_calls": subscription["remaining_calls"] if subscription else 0,
                "ai_success_cost": 1,
            }
        return {
            "user_id": user.id,
            "knowledge_base_id": knowledge_base_id,
            "profile": store.get_profile(user.id, knowledge_base_id),
            "own_articles": own,
            "reference_articles": references,
            "briefs": store.list_briefs(user.id, knowledge_base_id, 10),
            "drafts": drafts,
            "metrics": metrics,
            "billing": billing,
            "data_boundaries": {
                "wechat_read_metrics": False,
                "promotion_analysis": "未导入真实数据时，仅分析可见内容特征，不推断阅读或转化效果",
                "auto_publish": False,
                "reference_scope": "仅当前账号、当前知识库已采集文章",
            },
        }

    @router.put("/profile")
    def save_profile(payload: ProfileRequest, user: UserContext = Depends(current_user)) -> dict:
        _kb(user, payload.knowledge_base_id)
        store.upsert_profile(user.id, payload.knowledge_base_id, payload.model_dump())
        return {"profile": store.get_profile(user.id, payload.knowledge_base_id)}

    @router.post("/own-articles")
    def add_own_article(payload: OwnArticleRequest, user: UserContext = Depends(current_user)) -> dict:
        _kb(user, payload.knowledge_base_id)
        try:
            article_id = store.add_own_article(
                user.id, payload.knowledge_base_id, payload.title.strip(), payload.body,
                payload.url, payload.published_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"article": store.get_own_article(user.id, article_id)}

    @router.post("/style-profiles/generate")
    def generate_style(payload: KnowledgeBaseRequest, user: UserContext = Depends(current_user)) -> dict:
        def context_factory() -> dict:
            _kb(user, payload.knowledge_base_id)
            own = _own_rows(user.id, payload.knowledge_base_id, 10)
            if not own:
                raise HTTPException(status_code=422, detail="请先录入至少一篇本人历史文章")
            package = build_source_package([], own)
            prompt = (
                "请分析以下本人历史文章的表达习惯。只抽象可迁移的写作特征，不复制原句，不模仿第三方作者。"
                "样本少时必须降低置信度。只输出 JSON 对象，字段：voice,tone,sentence_style,"
                "structure_preferences(字符串数组),do(字符串数组),avoid(字符串数组),confidence,sample_count。\n\n"
                + source_prompt(package)
            )
            system = "你是中文内容风格分析器。你只做可解释的风格特征提取，不评价文章推广效果，不编造样本。"
            return {"system": system, "prompt": prompt, "package": package, "own_count": len(own)}

        def operation(context: dict, usage: dict) -> tuple[dict, int, int]:
            system, prompt = context["system"], context["prompt"]
            usage["input_tokens"] = estimate_tokens(system + prompt)
            raw = _generate(chat, system, prompt, 2200)
            usage["output_tokens"] = estimate_tokens(raw)
            style = normalise_style_profile(
                parse_ai_object(raw),
                actual_sample_count=context["own_count"],
                allowed_refs={item["ref_id"] for item in context["package"]},
            )
            return {"style_profile": style}, estimate_tokens(system + prompt), estimate_tokens(raw)

        def finalize(conn: Connection, artifact: dict) -> dict:
            style = artifact["style_profile"]
            store.save_style_draft(user.id, payload.knowledge_base_id, style, conn=conn)
            return {
                "style_profile": style, "activated": False,
                "message": "风格草稿已生成，请人工确认后激活",
            }

        return _paid_json(
            user, "/api/growth/style-profiles/generate", payload,
            context_factory, operation, finalize,
        )

    @router.post("/style-profiles/activate")
    def activate_style(payload: StyleActivateRequest, user: UserContext = Depends(current_user)) -> dict:
        _kb(user, payload.knowledge_base_id)
        own_rows = _own_rows(user.id, payload.knowledge_base_id, 10)
        sample_count = len(own_rows)
        allowed_refs = {f"O{index}" for index in range(1, sample_count + 1)}
        try:
            style = normalise_style_profile(
                payload.style_profile,
                actual_sample_count=sample_count,
                allowed_refs=allowed_refs,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.activate_style(user.id, payload.knowledge_base_id, style)
        return {"profile": store.get_profile(user.id, payload.knowledge_base_id), "activated": True}

    @router.post("/briefs")
    def generate_brief(payload: BriefRequest, user: UserContext = Depends(current_user)) -> dict:
        def context_factory() -> dict:
            _kb(user, payload.knowledge_base_id)
            references = _references(user.id, payload.knowledge_base_id, payload.reference_article_ids)
            own = _own_rows(user.id, payload.knowledge_base_id, 3)
            package = build_source_package(references, own)
            mode_note = (
                "当前是推广要素观察模式：只能描述标题、结构、行动引导等可见内容，禁止声称这些因素导致高阅读或高转化。"
                if payload.analysis_mode == "promotion" else
                "当前是内容结构模式：分析主题、标题、结构、论证与读者价值。"
            )
            profile = store.get_profile(user.id, payload.knowledge_base_id) or {}
            prompt = (
                f"分析焦点：{payload.focus or '近期内容选题'}\n分析模式：{mode_note}\n"
                f"账号配置：{json.dumps({k: profile.get(k) for k in ('audience', 'goal', 'cta', 'forbidden')}, ensure_ascii=False)}\n\n"
                "请基于给定来源生成中文选题简报。每个观察与选题必须列 evidence_refs；只能使用给定 R/O 编号。"
                "不得虚构数字、引语、热点日期或推广数据。只输出 JSON：summary,content_observations,topics,data_limitations。"
                "summary 必须是一个字符串，data_limitations 必须是字符串数组，不要用对象代替字符串。"
                "topics 每项字段 title,angle,why_now,audience_value,evidence_refs,risk。\n\n"
                + source_prompt(package)
            )
            system = "你是循证公众号选题策划。内容特征与真实推广效果严格分离；资料不足就写明，不能编造。"
            return {"system": system, "prompt": prompt, "package": package}

        def operation(context: dict, usage: dict) -> tuple[dict, int, int]:
            system, prompt, package = context["system"], context["prompt"], context["package"]
            usage["input_tokens"] = estimate_tokens(system + prompt)
            raw = _generate(chat, system, prompt, 3500)
            usage["output_tokens"] = estimate_tokens(raw)
            content = normalise_brief(
                parse_ai_object(raw), {item["ref_id"] for item in package},
            )
            return {"content": content, "package": package}, estimate_tokens(system + prompt), estimate_tokens(raw)

        def finalize(conn: Connection, artifact: dict) -> dict:
            brief_id = store.create_brief(
                user.id, payload.knowledge_base_id, payload.analysis_mode,
                payload.focus, artifact["content"], artifact["package"], conn=conn,
            )
            brief = store.get_brief(user.id, brief_id, conn=conn)
            if not brief:
                raise RuntimeError("saved brief cannot be read in its transaction")
            return {"brief": brief}

        return _paid_json(
            user, "/api/growth/briefs", payload, context_factory, operation, finalize,
        )

    @router.post("/drafts")
    def generate_draft(payload: DraftCreateRequest, user: UserContext = Depends(current_user)) -> dict:
        def context_factory() -> dict:
            _kb(user, payload.knowledge_base_id)
            profile = store.get_profile(user.id, payload.knowledge_base_id)
            if not profile or not profile.get("style_profile"):
                raise HTTPException(status_code=422, detail="请先生成并确认激活账号写作风格")
            brief = None
            if payload.brief_id:
                brief = store.get_brief(user.id, payload.brief_id)
                if not brief or int(brief["knowledge_base_id"]) != payload.knowledge_base_id:
                    raise HTTPException(status_code=404, detail="选题简报不存在")
                package = brief["sources"]
            else:
                references = _references(user.id, payload.knowledge_base_id, [])
                package = build_source_package(references, _own_rows(user.id, payload.knowledge_base_id, 3))
            topic = payload.topic.strip()
            if not topic and brief and brief["content"].get("topics"):
                topic = brief["content"]["topics"][0]["title"]
            if not topic:
                raise HTTPException(status_code=422, detail="请填写本次写作主题")
            prompt = (
                f"写作主题：{topic}\n目标篇幅：约{payload.target_length}字\n补充要求：{payload.additional_instructions}\n"
                f"账号配置：{json.dumps({k: profile.get(k) for k in ('audience', 'goal', 'cta', 'forbidden')}, ensure_ascii=False)}\n"
                f"已确认风格：{json.dumps(profile['style_profile'], ensure_ascii=False)}\n"
                f"选题简报：{json.dumps(brief['content'] if brief else {}, ensure_ascii=False)[:20000]}\n\n"
                "请写可人工编辑的原创中文公众号草稿，不复刻参考文章句子。事实句在句末使用给定 [R1]/[O1] 编号。"
                "没有依据的数字、引语、效果承诺必须列入 unresolved_facts，不得编造。保留独立观点与可执行建议。"
                "只输出 JSON：title_options(3个),outline(数组),body_markdown,citation_map,unresolved_facts,risk_notes,suggested_cta。"
                "citation_map 每项含 claim,evidence_refs。\n\n" + source_prompt(package)
            )
            system = "你是循证中文公众号写作副驾驶。AI 只产草稿，不自动发布；严格区分事实、观点与待核实内容。"
            return {
                "system": system, "prompt": prompt, "package": package,
                "brief_id": brief["id"] if brief else None,
            }

        def operation(context: dict, usage: dict) -> tuple[dict, int, int]:
            system, prompt, package = context["system"], context["prompt"], context["package"]
            usage["input_tokens"] = estimate_tokens(system + prompt)
            raw = _generate(chat, system, prompt, 6500)
            usage["output_tokens"] = estimate_tokens(raw)
            content = normalise_draft(
                parse_ai_object(raw), {item["ref_id"] for item in package},
            )
            return {
                "content": content, "package": package, "brief_id": context["brief_id"],
            }, estimate_tokens(system + prompt), estimate_tokens(raw)

        def finalize(conn: Connection, artifact: dict) -> dict:
            content = artifact["content"]
            draft_id = store.create_draft(
                user.id, payload.knowledge_base_id, artifact.get("brief_id"),
                content["title_options"][0], content["body_markdown"],
                content, artifact["package"], conn=conn,
            )
            draft = store.get_draft(user.id, draft_id, conn=conn)
            if not draft:
                raise RuntimeError("saved draft cannot be read in its transaction")
            return {"draft": _draft_public(draft)}

        return _paid_json(
            user, "/api/growth/drafts", payload, context_factory, operation, finalize,
        )

    @router.get("/drafts/{draft_id}")
    def get_draft(draft_id: int, user: UserContext = Depends(current_user)) -> dict:
        draft = store.get_draft(user.id, draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="草稿不存在")
        return {"draft": _draft_public(draft)}

    @router.patch("/drafts/{draft_id}")
    def update_draft(draft_id: int, payload: DraftUpdateRequest, user: UserContext = Depends(current_user)) -> dict:
        values = payload.model_dump(exclude_none=True)
        expected_revision = values.pop("expected_revision")
        try:
            updated = store.update_draft(user.id, draft_id, values, expected_revision=expected_revision)
        except DraftRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not updated:
            raise HTTPException(status_code=404, detail="草稿不存在")
        return {"draft": _draft_public(store.get_draft(user.id, draft_id))}

    @router.get("/drafts/{draft_id}/export")
    def export_draft(
        draft_id: int, format: Literal["markdown", "md", "txt", "html"] = "markdown",
        user: UserContext = Depends(current_user),
    ) -> Response:
        draft = store.get_draft(user.id, draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="草稿不存在")
        # Edited text may still contain [R1] markers. Keep their definitions as
        # explicitly labelled historical sources; never export dangling citations.
        sources = draft["sources"]
        unresolved = [] if draft.get("evidence_invalidated") else draft["unresolved_facts"]
        rendered = render_draft_export(draft, sources, unresolved, format)
        extension = "md" if format in {"markdown", "md"} else format
        media_type = {"md": "text/markdown", "txt": "text/plain", "html": "text/html"}[extension]
        filename = f"growth-draft-{draft_id}.{extension}"
        return Response(
            content=rendered, media_type=f"{media_type}; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/metrics")
    def save_metric(payload: MetricRequest, user: UserContext = Depends(current_user)) -> dict:
        _kb(user, payload.knowledge_base_id)
        try:
            datetime.strptime(payload.metric_date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="metric_date 必须是 YYYY-MM-DD") from exc
        own = store.get_own_article(user.id, payload.own_article_id)
        if not own or int(own["knowledge_base_id"]) != payload.knowledge_base_id:
            raise HTTPException(status_code=404, detail="本人文章不存在")
        metric_id = store.upsert_metric(
            user.id, payload.own_article_id, payload.metric_date, payload.window, payload.model_dump(),
        )
        row = store.get_metric(user.id, payload.knowledge_base_id, metric_id)
        if not row:
            raise HTTPException(status_code=404, detail="指标不存在或无权访问")
        return {
            "metric": _public_metric(row), "interpretation": "descriptive_only",
            "message": "已保存人工导入数据；不会据此宣称内容特征与效果存在因果关系",
        }

    return router
