from __future__ import annotations

import json
import shutil
import secrets
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.billing import ApiKeyContext, BillingStore, UserContext, estimate_tokens, hash_lookup_secret, normalize_phone, utc_now
from app.config import Settings, get_env_file_path, get_settings
from app.pipeline import DocumentPipeline
from app.sms import TencentSmsClient, friendly_sms_error
from app.wewe_api import build_wewe_router
from app.wewe_client import WeWeRssClient
from app.wewe_images import ImageEmbeddingClient, WeWeVectorIndex, public_image_hit
from app.wewe_pipeline import WeWeIngestPipeline
from app.wewe_runner import WeWeCollectionRunner
from app.wewe_scheduler import WeWeScheduler
from app.wewe_store import WeWeStore

app = FastAPI(
    title=get_settings().app_name, docs_url=None, redoc_url=None, openapi_url=None
)
STATIC_DIR = Path(__file__).resolve().parent / "static"
ARTIFACTS_DIR = Path("artifacts")
ARTIFACT_OWNER_FILE = "_owner.json"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

settings: Settings = get_settings()
pipeline: DocumentPipeline = DocumentPipeline(settings)
billing_store: BillingStore = BillingStore(settings)
billing_store.bootstrap_admin(settings.bootstrap_admin_email, settings.bootstrap_admin_password)

wewe_store: WeWeStore = WeWeStore(settings)
wewe_client: WeWeRssClient = WeWeRssClient(
    base_url=settings.wewe_base_url,
    auth_code=settings.wewe_auth_code,
    timeout=settings.wewe_timeout_seconds,
    max_retries=settings.wewe_max_retries,
)
wewe_vector_index: WeWeVectorIndex = WeWeVectorIndex(settings)
wewe_image_embedding: ImageEmbeddingClient = ImageEmbeddingClient(settings)
wewe_pipeline: WeWeIngestPipeline = WeWeIngestPipeline(
    settings=settings,
    store=wewe_store,
    client=wewe_client,
    vector_index=wewe_vector_index,
    embedding=pipeline.embedding,
    vision=pipeline.vision,
    image_embedding=wewe_image_embedding,
)
wewe_scheduler: WeWeScheduler = WeWeScheduler(settings, wewe_store, wewe_pipeline)
wewe_collection_runner: WeWeCollectionRunner = WeWeCollectionRunner(wewe_store, wewe_pipeline)

AGREEMENT_TERMS_VERSION = "terms-v2026.06.29"
AGREEMENT_PRIVACY_VERSION = "privacy-v2026.06.29"
AGREEMENT_DOCUMENT_HASH = "65f3ec72af63e42349cd7a0cd8bb4e533f1ee0483471620a39d9dd35d74e6280"

CONFIG_KEYS = [
    "APP_NAME",
    "APP_HOST",
    "APP_PORT",
    "LOG_LEVEL",
    "DATABASE_PATH",
    "BOOTSTRAP_ADMIN_EMAIL",
    "BOOTSTRAP_ADMIN_PASSWORD",
    "APP_SMS_ENABLED",
    "APP_SMS_PROVIDER",
    "APP_SMS_SECRET_ID",
    "APP_SMS_SECRET_KEY",
    "APP_SMS_SDK_APP_ID",
    "APP_SMS_SIGN_NAME",
    "APP_SMS_TEMPLATE_ID",
    "APP_SMS_CODE_EXPIRE_SECONDS",
    "APP_SMS_DAILY_PHONE_LIMIT",
    "APP_SMS_HOURLY_IP_LIMIT",
    "APP_SMS_WRONG_LOCK_SECONDS",
    "ARK_API_KEY",
    "ARK_BASE_URL",
    "DOUBAO_VISION_MODEL",
    "VISION_MAX_RETRIES",
    "VISION_TIMEOUT_SECONDS",
    "VISION_TEMPERATURE",
    "VISION_MAX_TOKENS",
    "VISION_QPS",
    "VISION_PROMPT_TEMPLATE",
    "DOUBAO_EMBEDDING_MODEL",
    "DOUBAO_CHAT_MODEL",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "QDRANT_COLLECTION",
    "VECTOR_SIZE",
    "DISTANCE_METRIC",
    "CHUNK_SIZE",
    "CHUNK_OVERLAP",
    "RAG_ANSWER_MAX_TOKENS",
]


class ConfigUpdateRequest(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=30)
    source_filename: str | None = None
    knowledge_base_id: int | None = Field(default=None, gt=0)
    retrieval_mode: Literal["semantic", "keyword", "hybrid"] = "semantic"
    include_images: bool = False


class RagAnswerRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=30)
    source_filename: str | None = None
    knowledge_base_id: int | None = Field(default=None, gt=0)
    retrieval_mode: Literal["semantic", "keyword", "hybrid"] = "semantic"
    include_images: bool = False


class RagFeedbackRequest(BaseModel):
    query_log_id: int = Field(gt=0)
    rating: Literal["up", "down"]
    note: str = Field(default="", max_length=500)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    agreements_accepted: bool = False
    terms_version: str = ""
    privacy_version: str = ""
    agreement_document_hash: str = ""


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    agreements_accepted: bool = False
    terms_version: str = ""
    privacy_version: str = ""
    agreement_document_hash: str = ""


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class SliderChallengeResponse(BaseModel):
    challenge_id: str
    background_svg: str
    piece_svg: str
    width: int
    height: int
    max_offset: int
    piece_top: int
    slot_size: int
    expires_in_seconds: int


class SmsSendRequest(BaseModel):
    phone: str = Field(min_length=11, max_length=20)
    challenge_id: str = Field(min_length=16, max_length=80)
    slider_offset: int = Field(ge=0, le=320)


class PhoneRegisterRequest(BaseModel):
    phone: str = Field(min_length=11, max_length=20)
    sms_code: str = Field(min_length=4, max_length=8)
    password: str = Field(min_length=8, max_length=128)
    agreements_accepted: bool = False
    terms_version: str = ""
    privacy_version: str = ""
    agreement_document_hash: str = ""


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(default="默认密钥", max_length=80)


class KnowledgeBaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


class SubscriptionGrantRequest(BaseModel):
    user_id: int = Field(gt=0)
    plan_code: str = Field(min_length=1)
    call_quota: int | None = Field(default=None, gt=0)


class PurchaseIntentRequest(BaseModel):
    plan_code: str = Field(min_length=1, max_length=80)
    contact_note: str = Field(default="", max_length=500)

class ParseApiResponse(BaseModel):
    status: str
    run_id: str
    source_filename: str
    source_type: str
    image_count: int
    text_block_count: int
    knowledge_base: dict | None = None
    markdown_url: str | None = None
    text_url: str | None = None
    text_blocks_url: str | None = None
    image_records_url: str | None = None
    image_structured_text_url: str | None = None
    result: dict = Field(default_factory=dict)


def current_user(request: Request) -> UserContext:
    token = request.cookies.get("session_token")
    user = billing_store.get_user_by_session(token)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def current_admin(user: UserContext = Depends(current_user)) -> UserContext:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要超管权限")
    return user


def current_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ApiKeyContext:
    raw_key = x_api_key
    if not raw_key and authorization:
        prefix = "Bearer "
        raw_key = authorization[len(prefix) :].strip() if authorization.startswith(prefix) else authorization.strip()
    ctx = billing_store.get_api_key_context(raw_key)
    if ctx is None:
        raise HTTPException(status_code=401, detail="API Key 无效或已禁用")
    if not ctx.is_admin:
        try:
            billing_store.ensure_quota(ctx.user_id)
        except PermissionError as exc:
            raise HTTPException(status_code=402, detail=str(exc)) from exc
    return ctx


def _read_env_file(env_path: Path) -> tuple[list[str], dict[str, str]]:
    if not env_path.exists():
        return [], {}
    lines = env_path.read_text(encoding="utf-8").splitlines()
    kv: dict[str, str] = {}
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        kv[key.strip()] = value.strip()
    return lines, kv


def _write_env_file(env_path: Path, updates: dict[str, str]) -> None:
    lines, kv = _read_env_file(env_path)
    for k, v in updates.items():
        kv[k] = v

    if lines:
        out_lines: list[str] = []
        seen: set[str] = set()
        for line in lines:
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                out_lines.append(line)
                continue
            key = raw.split("=", 1)[0].strip()
            if key in kv:
                out_lines.append(f"{key}={kv[key]}")
                seen.add(key)
            else:
                out_lines.append(line)
        for key in sorted(kv.keys()):
            if key not in seen:
                out_lines.append(f"{key}={kv[key]}")
    else:
        out_lines = [f"{key}={value}" for key, value in sorted(kv.items())]

    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def _reload_runtime() -> None:
    global settings, pipeline
    get_settings.cache_clear()
    settings = get_settings()
    pipeline = DocumentPipeline(settings)


def _mask_sensitive(values: dict[str, str]) -> dict[str, str]:
    masked = dict(values)
    for key in ("ARK_API_KEY", "QDRANT_API_KEY", "APP_SMS_SECRET_ID", "APP_SMS_SECRET_KEY"):
        val = masked.get(key, "")
        if val:
            masked[key] = f"{val[:4]}***{val[-3:]}" if len(val) > 8 else "***"
    return masked


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    real_ip = request.headers.get("x-real-ip", "")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


def _validate_agreement_acceptance(payload) -> None:
    if not getattr(payload, "agreements_accepted", False):
        raise HTTPException(status_code=400, detail="请先阅读并同意用户协议和隐私政策")
    if getattr(payload, "terms_version", "") != AGREEMENT_TERMS_VERSION:
        raise HTTPException(status_code=400, detail="用户协议版本已更新，请刷新页面后重新同意")
    if getattr(payload, "privacy_version", "") != AGREEMENT_PRIVACY_VERSION:
        raise HTTPException(status_code=400, detail="隐私政策版本已更新，请刷新页面后重新同意")
    if getattr(payload, "agreement_document_hash", "") != AGREEMENT_DOCUMENT_HASH:
        raise HTTPException(status_code=400, detail="协议内容校验失败，请刷新页面后重新同意")


def _record_agreement_acceptance(user_id: int, action: str, request: Request) -> None:
    billing_store.record_agreement_consent(
        user_id=user_id,
        action=action,
        terms_version=AGREEMENT_TERMS_VERSION,
        privacy_version=AGREEMENT_PRIVACY_VERSION,
        document_hash=AGREEMENT_DOCUMENT_HASH,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )


def _captcha_svg(width: int, height: int, target_x: int, target_y: int, slot: int, piece: bool, variant: int = 0) -> str:
    palettes = [
        ("#cfdde6", "#8fb0bf", "#31505b", "#467484", "#234b5a", "#142d38"),
        ("#d6e5dc", "#9ab8aa", "#405b58", "#527a75", "#2e5658", "#19343a"),
        ("#d9dfeb", "#94a8c4", "#344c69", "#4f6f8c", "#2b4c66", "#142d48"),
    ]
    sky_a, sky_b, sky_c, water_a, water_b, water_c = palettes[variant % len(palettes)]
    if piece:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{slot}" height="{slot}" viewBox="0 0 {slot} {slot}">'
            '<defs>'
            '<linearGradient id="pieceSky" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{water_a}"/><stop offset=".48" stop-color="{water_c}"/><stop offset="1" stop-color="{sky_c}"/>'
            '</linearGradient>'
            '<filter id="pieceShadow" x="-25%" y="-25%" width="150%" height="150%"><feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#071018" flood-opacity=".45"/></filter>'
            '</defs>'
            f'<path filter="url(#pieceShadow)" d="M2 9 Q2 2 9 2 H{slot-12} Q{slot-4} 2 {slot-4} 10 '
            f'C{slot-4} 15 {slot-1} 16 {slot-1} {slot/2:.1f} C{slot-1} {slot-16} {slot-4} {slot-15} {slot-4} {slot-10} '
            f'Q{slot-4} {slot-2} {slot-12} {slot-2} H9 Q2 {slot-2} 2 {slot-10} '
            f'C2 {slot-15} 5 {slot-16} 5 {slot/2:.1f} C5 16 2 15 2 9 Z" '
            'fill="url(#pieceSky)" stroke="#ffffff" stroke-width="2"/>'
            f'<path d="M4 {slot-13} C16 {slot-22} 31 {slot-20} {slot-4} {slot-29}" stroke="#d5edf3" stroke-width="3.2" fill="none" opacity=".8"/>'
            f'<path d="M9 13 H{slot-8} M11 23 H{slot-12} M15 32 H{slot-7}" stroke="#91adb6" stroke-width="2" opacity=".75"/>'
            "</svg>"
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        '<defs>'
        f'<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{sky_a}"/><stop offset=".46" stop-color="{sky_b}"/><stop offset="1" stop-color="{sky_c}"/></linearGradient>'
        f'<linearGradient id="water" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{water_a}"/><stop offset=".48" stop-color="{water_b}"/><stop offset="1" stop-color="{water_c}"/></linearGradient>'
        '<linearGradient id="haze" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#f2f6f7" stop-opacity=".45"/><stop offset=".45" stop-color="#ffffff" stop-opacity=".12"/><stop offset="1" stop-color="#ffffff" stop-opacity=".3"/></linearGradient>'
        '<filter id="softNoise"><feTurbulence type="fractalNoise" baseFrequency=".9" numOctaves="2" seed="8" result="n"/><feColorMatrix in="n" type="saturate" values="0"/><feComponentTransfer><feFuncA type="table" tableValues="0 .11"/></feComponentTransfer><feBlend mode="multiply" in2="SourceGraphic"/></filter>'
        '</defs>'
        '<g filter="url(#softNoise)">'
        '<rect width="100%" height="100%" rx="12" fill="url(#sky)"/>'
        '<path d="M0 63 C35 46 65 44 96 58 C123 70 155 70 188 56 C222 42 262 42 300 30 V0 H0 Z" fill="#e7eef0" opacity=".28"/>'
        '<path d="M0 89 C39 77 76 76 118 91 C164 107 198 91 300 82 V150 H0 Z" fill="url(#water)" opacity=".96"/>'
        '<path d="M0 92 C64 73 112 76 158 86 C205 96 246 88 300 72" stroke="#f2fbfd" stroke-width="4" fill="none" opacity=".45"/>'
        '<path d="M0 113 C72 101 139 102 300 88" stroke="url(#haze)" stroke-width="20" fill="none" opacity=".55"/>'
        '<path d="M34 103 L268 58" stroke="#0e222d" stroke-width="5.8" opacity=".88"/>'
        '<path d="M27 114 L276 67" stroke="#2d586a" stroke-width="4.5" opacity=".86"/>'
        '<path d="M55 29 L55 119 M238 23 L238 101" stroke="#152d38" stroke-width="8.5" stroke-linecap="round"/>'
        '<path d="M45 31 H66 M227 25 H249" stroke="#223f4d" stroke-width="5.2" stroke-linecap="round"/>'
        '<path d="M55 37 C92 8 192 5 238 31" stroke="#102631" stroke-width="4.4" fill="none"/>'
        '<path d="M55 47 C99 22 191 21 238 39" stroke="#e5f2f5" stroke-width="2.2" fill="none" opacity=".8"/>'
        '<g stroke="#dce9ed" stroke-width="1.55" opacity=".76">'
        '<path d="M72 46 L66 110"/><path d="M90 39 L83 106"/><path d="M109 34 L101 103"/><path d="M128 31 L119 99"/>'
        '<path d="M148 29 L139 96"/><path d="M168 29 L158 92"/><path d="M188 31 L178 88"/><path d="M209 35 L197 84"/><path d="M228 42 L214 79"/>'
        '</g>'
        '<path d="M20 130 C74 112 124 122 172 110 C216 99 253 105 300 94 V150 H0 V136 Z" fill="#0f2830" opacity=".28"/>'
        '</g>'
        f'<path d="M{target_x+2} {target_y+9} Q{target_x+2} {target_y+2} {target_x+9} {target_y+2} '
        f'H{target_x+slot-12} Q{target_x+slot-4} {target_y+2} {target_x+slot-4} {target_y+10} '
        f'C{target_x+slot-4} {target_y+15} {target_x+slot-1} {target_y+16} {target_x+slot-1} {target_y+slot/2:.1f} '
        f'C{target_x+slot-1} {target_y+slot-16} {target_x+slot-4} {target_y+slot-15} {target_x+slot-4} {target_y+slot-10} '
        f'Q{target_x+slot-4} {target_y+slot-2} {target_x+slot-12} {target_y+slot-2} '
        f'H{target_x+9} Q{target_x+2} {target_y+slot-2} {target_x+2} {target_y+slot-10} '
        f'C{target_x+2} {target_y+slot-15} {target_x+5} {target_y+slot-16} {target_x+5} {target_y+slot/2:.1f} '
        f'C{target_x+5} {target_y+16} {target_x+2} {target_y+15} {target_x+2} {target_y+9} Z" '
        'fill="#071018" opacity=".6" stroke="#d8edf3" stroke-width="2"/>'
        '<text x="212" y="137" fill="#dbe7eb" font-size="11" font-family="Arial, sans-serif" opacity=".72">AI生成背景</text>'
        "</svg>"
    )


def _to_artifact_url(path_str: str | None) -> str | None:
    if not path_str:
        return None

    try:
        normalized = path_str.replace("\\", "/")
        in_path = Path(normalized)
        base_dir = (Path.cwd() / ARTIFACTS_DIR).resolve()
        resolved = in_path.resolve() if in_path.is_absolute() else (Path.cwd() / in_path).resolve()
        rel = resolved.relative_to(base_dir)
        return f"/artifacts/{rel.as_posix()}"
    except Exception:
        return None


def _write_artifact_owner(work_dir: Path, owner_user_id: int, api_key_id: int | None) -> None:
    payload = {
        "owner_user_id": int(owner_user_id),
        "api_key_id": int(api_key_id) if api_key_id is not None else None,
        "created_at": utc_now().isoformat(),
    }
    (work_dir / ARTIFACT_OWNER_FILE).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _artifact_owner_for_path(path: Path) -> dict | None:
    try:
        rel = path.resolve().relative_to((Path.cwd() / ARTIFACTS_DIR).resolve())
    except Exception:
        return None
    if not rel.parts:
        return None
    owner_file = ARTIFACTS_DIR / rel.parts[0] / ARTIFACT_OWNER_FILE
    if not owner_file.exists():
        return None
    try:
        data = json.loads(owner_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _artifact_actor(
    request: Request,
    authorization: str | None,
    x_api_key: str | None,
) -> tuple[int, int | None] | None:
    session_user = billing_store.get_user_by_session(request.cookies.get("session_token"))
    if session_user is not None:
        return session_user.id, None

    raw_key = x_api_key
    if not raw_key and authorization:
        prefix = "Bearer "
        raw_key = authorization[len(prefix) :].strip() if authorization.startswith(prefix) else authorization.strip()
    api_key = billing_store.get_api_key_context(raw_key)
    if api_key is None:
        return None
    return api_key.user_id, api_key.id


def current_wewe_image_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> UserContext:
    """Allow the same Session/API Key identities as other protected artifacts."""
    actor = _artifact_actor(request, authorization, x_api_key)
    if actor is None:
        raise HTTPException(status_code=401, detail="请先登录或提供有效 API Key")
    return UserContext(id=actor[0], email="", is_admin=False, is_active=True)


@app.get("/artifacts/{artifact_path:path}")
def artifact_file(
    artifact_path: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> FileResponse:
    actor = _artifact_actor(request, authorization, x_api_key)
    if actor is None:
        raise HTTPException(status_code=401, detail="请先登录或提供有效 API Key")

    try:
        target = (ARTIFACTS_DIR / artifact_path).resolve()
        target.relative_to((Path.cwd() / ARTIFACTS_DIR).resolve())
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")

    owner = _artifact_owner_for_path(target)
    owner_user_id = int(owner.get("owner_user_id")) if owner and owner.get("owner_user_id") is not None else None
    if owner_user_id is None or owner_user_id != actor[0]:
        raise HTTPException(status_code=403, detail="Artifact does not belong to current user")

    return FileResponse(target)


def _attach_source_urls(hits: list[dict]) -> list[dict]:
    enriched: list[dict] = []
    for hit in hits:
        item = dict(hit)
        item["source_file_url"] = _to_artifact_url(str(item.get("source_file", "")))
        item["source_text_url"] = _to_artifact_url(str(item.get("merged_output", "")))
        enriched.append(item)
    return enriched


def _search_related_images(query: str, user_id: int, knowledge_base_id: int, top_k: int = 5) -> list[dict]:
    """在原生图片向量空间检索与查询相关的图片（失败时静默降级为空列表）。"""

    if not settings.wewe_enabled or not settings.wewe_image_vector_enabled:
        return []
    try:
        vector = wewe_image_embedding.embed_text(query)
        if not vector:
            return []
        hits = wewe_vector_index.search_images(
            query_vector=vector,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            article_versions=wewe_store.published_vector_versions(user_id, knowledge_base_id),
            top_k=top_k,
        )
    except Exception:
        return []
    return [public_image_hit(hit) for hit in hits]


def _image_citations_from_hits(image_hits: list[dict], start_index: int = 1) -> list[dict]:
    citations = []
    for offset, image_hit in enumerate(image_hits, start=start_index):
        file_url = image_hit.get("image_file_url")
        citations.append({
            "index": offset,
            "type": "image",
            "score": image_hit.get("score", 0.0),
            "source_filename": image_hit.get("article_title") or image_hit.get("source_name") or "",
            "source_file": image_hit.get("article_url") or "",
            "source_file_url": file_url,
            "image_file_url": file_url,
            "knowledge_base_id": image_hit.get("knowledge_base_id"),
            "chunk_index": -1,
            "chapter": None,
            "snippet": image_hit.get("description") or "",
            "vector_space": "image",
            "image_id": image_hit.get("image_id"),
            "article_id": image_hit.get("article_id"),
        })
    return citations


def _enrich_process_result(result: dict, work_dir: Path) -> dict:
    merged_output = Path(result["merged_output"])
    result["merged_output_url"] = _to_artifact_url(str(merged_output))
    result["markdown_url"] = result["merged_output_url"]
    result["text_output_url"] = _to_artifact_url(str(result.get("text_output", "")))
    result["text_blocks_url"] = _to_artifact_url(str(work_dir / "text_blocks.json"))
    result["image_records_url"] = _to_artifact_url(str(work_dir / "image_records.json"))
    result["image_structured_text_url"] = _to_artifact_url(str(work_dir / "image_structured_text.json"))
    result["images_dir_url"] = _to_artifact_url(str(work_dir / "images"))
    return result


def _citations_from_hits(hits: list[dict]) -> list[dict]:
    citations = []
    for i, hit in enumerate(hits, start=1):
        citations.append(
            {
                "index": i,
                "score": hit["score"],
                "source_filename": hit["source_filename"],
                "source_manual": hit.get("source_manual"),
                "source_file": hit["source_file"],
                "source_file_url": hit.get("source_file_url"),
                "source_text_url": hit.get("source_text_url"),
                "knowledge_base_id": hit.get("knowledge_base_id"),
                "chunk_index": hit["chunk_index"],
                "page_start": hit.get("page_start"),
                "page_end": hit.get("page_end"),
                "chapter": hit.get("chapter"),
                "snippet": hit["text"],
            }
        )
    return citations


def _resolve_user_knowledge_base(user_id: int, knowledge_base_id: int | None) -> dict:
    try:
        return billing_store.get_knowledge_base_for_user(user_id, knowledge_base_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


app.include_router(
    build_wewe_router(
        current_user=current_user,
        store=wewe_store,
        pipeline=wewe_pipeline,
        vector_index=wewe_vector_index,
        client=wewe_client,
        settings=settings,
        resolve_kb=_resolve_user_knowledge_base,
        collection_runner=wewe_collection_runner,
        current_image_user=current_wewe_image_user,
    )
)


@app.on_event("startup")
def _start_wewe_scheduler() -> None:
    billing_store.reconcile_abandoned_reservations()
    wewe_store.recover_interrupted_tasks()
    wewe_scheduler.start()


@app.on_event("shutdown")
def _stop_wewe_scheduler() -> None:
    wewe_scheduler.stop()
    wewe_collection_runner.close()


def _public_base_url(request: Request) -> str:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").strip()
    scheme = forwarded_proto or request.url.scheme or "https"
    if forwarded_host:
        return f"{scheme}://{forwarded_host}".rstrip("/")
    base = str(request.base_url).rstrip("/")
    return base or "https://doc.zhouyuaninfo.com.cn"


def _latest_document_for_user(user_id: int, knowledge_base_id: int | None) -> dict | None:
    kb = billing_store.get_knowledge_base_for_user(user_id, knowledge_base_id)
    docs = billing_store.list_knowledge_documents(user_id, int(kb["id"]))
    return docs[0] if docs else None

def _remove_artifact_dir(artifact_path: str) -> bool:
    if not artifact_path:
        return False
    try:
        base = (Path.cwd() / ARTIFACTS_DIR).resolve()
        target = Path(artifact_path)
        resolved = target.resolve() if target.is_absolute() else (Path.cwd() / target).resolve()
        resolved.relative_to(base)
    except Exception:
        return False
    if not resolved.exists() or not resolved.is_dir():
        return False
    shutil.rmtree(resolved)
    return True


async def _run_uploaded_parse(
    file: UploadFile,
    run_vision: bool,
    ingest_vector: bool,
    rebuild_index: bool,
    owner_user_id: int,
    knowledge_base_id: int,
    api_key_id: int | None = None,
) -> tuple[str, dict]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename.")

    run_id = uuid.uuid4().hex
    work_dir = ARTIFACTS_DIR / run_id
    input_dir = work_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    _write_artifact_owner(work_dir, owner_user_id=owner_user_id, api_key_id=api_key_id)
    file_path = input_dir / file.filename

    with file_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    try:
        result = pipeline.process(
            file_path=str(file_path),
            output_dir=str(work_dir),
            run_vision=run_vision,
            ingest_vector=ingest_vector,
            rebuild_index=rebuild_index,
            owner_user_id=owner_user_id,
            knowledge_base_id=knowledge_base_id,
            api_key_id=api_key_id,
            run_id=run_id,
        )
        enriched = _enrich_process_result(result, work_dir)
        billing_store.record_knowledge_document(
            user_id=owner_user_id,
            knowledge_base_id=knowledge_base_id,
            run_id=run_id,
            source_filename=str(enriched.get("source_filename", "")),
            source_type=str(enriched.get("source_type", "")),
            status="indexed" if int(enriched.get("vector_count") or 0) > 0 else "parsed",
            vector_count=int(enriched.get("vector_count") or 0),
            text_block_count=int(enriched.get("text_block_count") or 0),
            image_count=int(enriched.get("image_count") or 0),
            artifact_path=str(work_dir),
        )
        return run_id, enriched
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/openapi.json", include_in_schema=False)
def authenticated_openapi(_: UserContext = Depends(current_user)) -> dict:
    return app.openapi()


@app.get("/")
def console() -> FileResponse:
    return FileResponse(STATIC_DIR / "console.html")


@app.get("/app")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/docs", include_in_schema=False)
def custom_docs(_: UserContext = Depends(current_user)) -> HTMLResponse:
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>接口文档 - 文档解析开放平台</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css" />
  <style>
    body { margin: 0; background: #f6f4ee; color: #172033; font-family: Georgia, "Microsoft YaHei", serif; }
    .doc-shell { max-width: 1180px; margin: 24px auto 0; padding: 0 18px; }
    .doc-header {
      display: flex; align-items: end; justify-content: space-between; gap: 16px;
      border-bottom: 2px solid #172033; padding-bottom: 16px; margin-bottom: 12px;
    }
    .doc-title { font-size: 30px; font-weight: 700; }
    .doc-sub { color: #657083; margin-top: 6px; font-size: 14px; }
    .doc-nav { display: flex; gap: 8px; flex-wrap: wrap; }
    .doc-nav a {
      min-height: 38px; display: inline-flex; align-items: center; justify-content: center;
      border: 1px solid #172033; border-radius: 6px; padding: 8px 12px;
      color: #172033; text-decoration: none; background: #fff;
    }
    .doc-nav a.primary { background: #087f7a; border-color: #087f7a; color: #fff; }
    #swagger-ui { max-width: 1180px; margin: 0 auto 36px; background: #fff; border: 1px solid #d8dde8; border-radius: 8px; }
    .swagger-ui .topbar { display: none; }
    @media (max-width: 760px) {
      .doc-header { align-items: flex-start; flex-direction: column; }
      .doc-title { font-size: 24px; }
    }
  </style>
</head>
<body>
  <main class="doc-shell">
    <header class="doc-header">
      <div>
        <div class="doc-title">接口文档</div>
        <div class="doc-sub">开放解析 API、认证、套餐、统计与后台管理接口。</div>
      </div>
      <nav class="doc-nav" aria-label="页面导航">
        <a class="primary" href="/">返回首页</a>
        <a href="/app">文档处理工作台</a>

      </nav>
    </header>
  </main>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = () => {
      window.ui = SwaggerUIBundle({
        url: "/openapi.json",
        dom_id: "#swagger-ui",
        deepLinking: true,
        displayRequestDuration: true,
        persistAuthorization: true,
      });
    };
  </script>
</body>
</html>
"""
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": settings.app_name}


@app.post("/api/auth/register")
def register(payload: RegisterRequest, request: Request, response: Response) -> dict:
    _validate_agreement_acceptance(payload)
    try:
        user = billing_store.create_user(payload.email, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record_agreement_acceptance(int(user["id"]), "email_register", request)
    token = billing_store.create_session(int(user["id"]))
    response.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=7 * 24 * 3600)
    return {"status": "ok", "user": user, "subscription": billing_store.get_active_subscription(int(user["id"]))}


@app.get("/api/auth/slider-challenge", response_model=SliderChallengeResponse)
def slider_challenge(request: Request, phone: str = "") -> dict:
    phone_norm = normalize_phone(phone)
    if phone_norm and not billing_store.is_valid_cn_phone(phone_norm):
        raise HTTPException(status_code=400, detail="手机号格式不正确")
    width = 300
    height = 150
    slot = 42
    target_x = secrets.randbelow(width - slot - 94) + 72
    target_y = secrets.randbelow(height - slot - 44) + 34
    variant = secrets.randbelow(3)
    challenge_id = "sl_" + secrets.token_urlsafe(24)
    target_hash = hash_lookup_secret(f"{challenge_id}:{target_x}")
    expire_seconds = 180
    billing_store.create_slider_challenge(
        challenge_id=challenge_id,
        target_hash=target_hash,
        expires_at=time_to_datetime(expire_seconds),
        ip_address=_client_ip(request),
        phone=phone_norm,
    )
    return {
        "challenge_id": challenge_id,
        "background_svg": _captcha_svg(width, height, target_x, target_y, slot, piece=False, variant=variant),
        "piece_svg": _captcha_svg(width, height, target_x, target_y, slot, piece=True, variant=variant),
        "width": width,
        "height": height,
        "max_offset": width - slot,
        "piece_top": target_y,
        "slot_size": slot,
        "expires_in_seconds": expire_seconds,
    }


def time_to_datetime(seconds_from_now: int):
    return utc_now() + timedelta(seconds=seconds_from_now)


@app.post("/api/auth/sms/send")
def send_sms_code(payload: SmsSendRequest, request: Request) -> dict:
    phone = normalize_phone(payload.phone)
    if not billing_store.is_valid_cn_phone(phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确")

    ip_address = _client_ip(request)
    tolerance = 24
    accepted_hashes = [
        hash_lookup_secret(f"{payload.challenge_id}:{x}")
        for x in range(max(0, payload.slider_offset - tolerance), payload.slider_offset + tolerance + 1)
    ]
    if not billing_store.verify_slider_challenge(payload.challenge_id, accepted_hashes, ip_address):
        raise HTTPException(status_code=400, detail="滑块验证失败，请重试")

    allowed, reason = billing_store.sms_send_allowed(
        phone=phone,
        ip_address=ip_address,
        daily_phone_limit=settings.app_sms_daily_phone_limit,
        hourly_ip_limit=settings.app_sms_hourly_ip_limit,
    )
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    code = f"{secrets.randbelow(1_000_000):06d}"
    provider_message_id = None
    debug_code = None
    if settings.app_sms_enabled:
        if settings.app_sms_provider.lower() != "tencent":
            raise HTTPException(status_code=500, detail="暂仅支持腾讯云短信")
        result = TencentSmsClient(settings).send_code(
            phone=phone,
            code=code,
            expire_minutes=max(1, int(settings.app_sms_code_expire_seconds / 60)),
        )
        if not result.ok:
            error_text = friendly_sms_error(result.error)
            raise HTTPException(status_code=502, detail=error_text)
        provider_message_id = result.provider_message_id
    else:
        debug_code = code

    billing_store.store_sms_code(
        phone=phone,
        code=code,
        purpose="register",
        expires_at=time_to_datetime(settings.app_sms_code_expire_seconds),
        ip_address=ip_address,
        provider_message_id=provider_message_id,
    )
    response = {
        "status": "ok",
        "message": "验证码已发送，5 分钟内有效",
        "expires_in_seconds": settings.app_sms_code_expire_seconds,
        "resend_after_seconds": 60,
    }
    if debug_code:
        response["debug_code"] = debug_code
    return response


@app.post("/api/auth/register-phone")
def register_phone(payload: PhoneRegisterRequest, request: Request, response: Response) -> dict:
    _validate_agreement_acceptance(payload)
    phone = normalize_phone(payload.phone)
    if not billing_store.is_valid_cn_phone(phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确")
    ok, reason = billing_store.verify_sms_code(
        phone=phone,
        code=payload.sms_code.strip(),
        purpose="register",
        lock_seconds=settings.app_sms_wrong_lock_seconds,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=reason)
    try:
        user = billing_store.create_user_by_phone(phone, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record_agreement_acceptance(int(user["id"]), "phone_register", request)
    token = billing_store.create_session(int(user["id"]))
    response.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=7 * 24 * 3600)
    return {"status": "ok", "user": user, "subscription": billing_store.get_active_subscription(int(user["id"]))}


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    _validate_agreement_acceptance(payload)
    user = billing_store.authenticate(payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    _record_agreement_acceptance(int(user["id"]), "login", request)
    token = billing_store.create_session(int(user["id"]))
    response.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=7 * 24 * 3600)
    return {"status": "ok", "user": user, "subscription": billing_store.get_active_subscription(int(user["id"]))}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    billing_store.delete_session(request.cookies.get("session_token"))
    response.delete_cookie("session_token")
    return {"status": "ok"}


@app.post("/api/auth/change-password")
def change_password(payload: ChangePasswordRequest, user: UserContext = Depends(current_user)) -> dict:
    try:
        billing_store.change_password(user.id, payload.old_password, payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}


@app.get("/api/auth/me")
def me(user: UserContext = Depends(current_user)) -> dict:
    return {
        "user": user.__dict__,
        "subscription": billing_store.get_active_subscription(user.id),
        "api_keys": billing_store.list_api_keys(user.id),
        "usage": billing_store.usage_summary(user_id=user.id, days=30),
    }


@app.post("/api/auth/api-keys")
def create_api_key(payload: ApiKeyCreateRequest, user: UserContext = Depends(current_user)) -> dict:
    raw_key, item = billing_store.create_api_key(user.id, payload.name)
    return {"status": "ok", "api_key": raw_key, "item": item}


@app.get("/api/auth/api-keys")
def list_api_keys(user: UserContext = Depends(current_user)) -> dict:
    return {"api_keys": billing_store.list_api_keys(user.id)}


@app.get("/api/knowledge-bases")
def list_knowledge_bases(user: UserContext = Depends(current_user)) -> dict:
    items = billing_store.list_knowledge_bases(user.id)
    return {"count": len(items), "knowledge_bases": items}


@app.post("/api/knowledge-bases")
def create_knowledge_base(payload: KnowledgeBaseCreateRequest, user: UserContext = Depends(current_user)) -> dict:
    try:
        item = billing_store.create_knowledge_base(user.id, payload.name, payload.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "knowledge_base": item}


@app.get("/api/knowledge-bases/{knowledge_base_id}/documents")
def list_knowledge_documents(knowledge_base_id: int, user: UserContext = Depends(current_user)) -> dict:
    kb = _resolve_user_knowledge_base(user.id, knowledge_base_id)
    docs = billing_store.list_knowledge_documents(user.id, int(kb["id"]))
    return {"knowledge_base": kb, "count": len(docs), "documents": docs}


@app.delete("/api/knowledge-bases/{knowledge_base_id}/documents/{document_id}")
def delete_knowledge_document(
    knowledge_base_id: int,
    document_id: int,
    user: UserContext = Depends(current_user),
) -> dict:
    kb = _resolve_user_knowledge_base(user.id, knowledge_base_id)
    try:
        doc = billing_store.get_knowledge_document_for_user(user.id, int(kb["id"]), document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    deleted_vectors = pipeline.indexer.remove_run_chunks(
        run_id=str(doc.get("run_id", "")),
        user_id=user.id,
        knowledge_base_id=int(kb["id"]),
    )
    deleted_artifacts = _remove_artifact_dir(str(doc.get("artifact_path", "")))
    deleted_doc = billing_store.delete_knowledge_document(user.id, int(kb["id"]), document_id)
    return {
        "status": "ok",
        "knowledge_base": kb,
        "document": deleted_doc,
        "deleted_vectors": deleted_vectors,
        "deleted_artifacts": deleted_artifacts,
    }


@app.get("/api/billing/plans")
def list_plans() -> dict:
    return {
        "plans": billing_store.list_plans(),
        "pricing_note": "注册赠送 3 次免费体验；付费套餐按低门槛 C 端月包计费，调用次数用于文档解析、知识库检索和知识库问答。",
    }


@app.get("/api/billing/me")
def billing_me(user: UserContext = Depends(current_user)) -> dict:
    return {
        "subscription": billing_store.get_active_subscription(user.id),
        "usage": billing_store.usage_summary(user_id=user.id, days=30),
    }


@app.post("/api/billing/purchase-intents")
def create_purchase_intent(payload: PurchaseIntentRequest, user: UserContext = Depends(current_user)) -> dict:
    try:
        intent = billing_store.create_purchase_intent(
            user_id=user.id,
            plan_code=payload.plan_code,
            contact_note=payload.contact_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "ok",
        "intent": intent,
        "message": "开通申请已提交，平台会尽快联系并协助开通。正式支付通道上线后将支持自助付款。",
    }

@app.get("/api/admin/users")
def admin_users(_: UserContext = Depends(current_admin)) -> dict:
    return {"users": billing_store.list_users()}


@app.post("/api/admin/subscriptions")
def admin_grant_subscription(payload: SubscriptionGrantRequest, _: UserContext = Depends(current_admin)) -> dict:
    try:
        subscription = billing_store.grant_subscription(
            user_id=payload.user_id,
            plan_code=payload.plan_code,
            call_quota=payload.call_quota,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "subscription": subscription}


@app.get("/api/admin/stats")
def admin_stats(days: int = 30, _: UserContext = Depends(current_admin)) -> dict:
    return billing_store.usage_summary(days=days)


def _reserve_call_quota(user_id: int, is_admin: bool = False):
    """Reserve before paid work; HTTP 402 means no model/fetch was started."""
    if is_admin:
        return None
    try:
        return billing_store.reserve_call(user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc


@app.post("/process")
async def process_document(
    file: UploadFile = File(...),
    run_vision: bool = Form(False),
    ingest_vector: bool = Form(False),
    rebuild_index: bool = Form(False),
    knowledge_base_id: int | None = Form(None),
    user: UserContext = Depends(current_user),
) -> dict:
    start = time.perf_counter()
    run_id = ""
    kb = _resolve_user_knowledge_base(user.id, knowledge_base_id)
    reservation = _reserve_call_quota(user.id, user.is_admin)
    try:
        run_id, result = await _run_uploaded_parse(
            file,
            run_vision,
            ingest_vector,
            rebuild_index,
            owner_user_id=user.id,
            knowledge_base_id=int(kb["id"]),
            api_key_id=None,
        )
        result["knowledge_base"] = kb
        output_text = ""
        text_path = result.get("text_output")
        if text_path:
            output_text = Path(str(text_path)).read_text(encoding="utf-8", errors="ignore")
        billing_store.consume_call(
            user_id=user.id,
            api_key_id=None,
            endpoint="/process",
            method="POST",
            status_code=200,
            latency_ms=int((time.perf_counter() - start) * 1000),
            output_tokens=estimate_tokens(output_text),
            meta={"run_id": run_id, "source_filename": result.get("source_filename"), "knowledge_base_id": kb["id"]},
            reservation=reservation,
            call_count=0 if user.is_admin else 1,
        )
        return result
    except Exception as exc:
        billing_store.consume_call(
            user_id=user.id,
            api_key_id=None,
            endpoint="/process",
            method="POST",
            status_code=getattr(exc, "status_code", 500),
            latency_ms=int((time.perf_counter() - start) * 1000),
            meta={"run_id": run_id, "knowledge_base_id": kb["id"], "error": str(getattr(exc, "detail", str(exc)))},
            reservation=reservation,
            call_count=0 if user.is_admin else 1,
        )
        raise


@app.post("/api/v1/parse", response_model=ParseApiResponse)
async def api_parse_document(
    file: UploadFile = File(...),
    output_format: Literal["txt", "markdown", "json", "both"] = Form("both"),
    run_vision: bool = Form(False),
    ingest_vector: bool = Form(False),
    rebuild_index: bool = Form(False),
    knowledge_base_id: int | None = Form(None),
    api_key: ApiKeyContext = Depends(current_api_key),
) -> dict:
    start = time.perf_counter()
    run_id = ""
    status_code = 200
    kb = _resolve_user_knowledge_base(api_key.user_id, knowledge_base_id)
    reservation = _reserve_call_quota(api_key.user_id, api_key.is_admin)
    try:
        run_id, result = await _run_uploaded_parse(
            file,
            run_vision,
            ingest_vector,
            rebuild_index,
            owner_user_id=api_key.user_id,
            knowledge_base_id=int(kb["id"]),
            api_key_id=api_key.id,
        )
        output_text = ""
        text_path = result.get("text_output")
        if text_path:
            output_text = Path(str(text_path)).read_text(encoding="utf-8", errors="ignore")

        markdown_url = result.get("markdown_url") if output_format in ("markdown", "both") else None
        text_url = result.get("text_output_url") if output_format in ("txt", "both") else None
        response = {
            "status": "ok",
            "run_id": run_id,
            "source_filename": str(result.get("source_filename", "")),
            "source_type": str(result.get("source_type", "")),
            "image_count": int(result.get("image_count", 0)),
            "text_block_count": int(result.get("text_block_count", 0)),
            "knowledge_base": kb,
            "markdown_url": markdown_url,
            "text_url": text_url,
            "text_blocks_url": result.get("text_blocks_url"),
            "image_records_url": result.get("image_records_url"),
            "image_structured_text_url": result.get("image_structured_text_url"),
            "result": result,
        }
        billing_store.consume_call(
            user_id=api_key.user_id,
            api_key_id=api_key.id,
            endpoint="/api/v1/parse",
            method="POST",
            status_code=200,
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=estimate_tokens(file.filename or ""),
            output_tokens=estimate_tokens(output_text),
            meta={
                "run_id": run_id,
                "source_filename": result.get("source_filename"),
                "knowledge_base_id": kb["id"],
                "output_format": output_format,
                "run_vision": run_vision,
                "ingest_vector": ingest_vector,
            },
            reservation=reservation,
            call_count=0 if api_key.is_admin else 1,
        )
        return response
    except Exception as exc:
        status_code = getattr(exc, "status_code", 500)
        billing_store.consume_call(
            user_id=api_key.user_id,
            api_key_id=api_key.id,
            endpoint="/api/v1/parse",
            method="POST",
            status_code=status_code,
            latency_ms=int((time.perf_counter() - start) * 1000),
            meta={"run_id": run_id, "knowledge_base_id": kb["id"], "error": str(getattr(exc, "detail", str(exc)))},
            reservation=reservation,
            call_count=0,
        )
        raise


@app.get("/api/v1/knowledge-bases")
def api_list_knowledge_bases(api_key: ApiKeyContext = Depends(current_api_key)) -> dict:
    items = billing_store.list_knowledge_bases(api_key.user_id)
    return {"count": len(items), "knowledge_bases": items}


@app.post("/api/v1/knowledge-bases")
def api_create_knowledge_base(
    payload: KnowledgeBaseCreateRequest,
    api_key: ApiKeyContext = Depends(current_api_key),
) -> dict:
    try:
        item = billing_store.create_knowledge_base(api_key.user_id, payload.name, payload.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "knowledge_base": item}


@app.get("/api/v1/knowledge-bases/{knowledge_base_id}/sources")
def api_knowledge_sources(knowledge_base_id: int, api_key: ApiKeyContext = Depends(current_api_key)) -> dict:
    kb = _resolve_user_knowledge_base(api_key.user_id, knowledge_base_id)
    try:
        items = pipeline.indexer.list_sources(
            user_id=api_key.user_id,
            knowledge_base_id=int(kb["id"]),
            article_versions=wewe_store.published_vector_versions(api_key.user_id, int(kb["id"])),
        )
        enriched = []
        for item in items:
            source_item = dict(item)
            source_item["source_file_url"] = _to_artifact_url(str(source_item.get("source_file", "")))
            source_item["source_text_url"] = _to_artifact_url(str(source_item.get("merged_output", "")))
            enriched.append(source_item)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"RAG source listing failed: {exc}") from exc
    return {"knowledge_base": kb, "count": len(enriched), "sources": enriched}


@app.get("/api/v1/knowledge-bases/{knowledge_base_id}/documents")
def api_knowledge_documents(knowledge_base_id: int, api_key: ApiKeyContext = Depends(current_api_key)) -> dict:
    kb = _resolve_user_knowledge_base(api_key.user_id, knowledge_base_id)
    docs = billing_store.list_knowledge_documents(api_key.user_id, int(kb["id"]))
    return {"knowledge_base": kb, "count": len(docs), "documents": docs}


@app.delete("/api/v1/knowledge-bases/{knowledge_base_id}/documents/{document_id}")
def api_delete_knowledge_document(
    knowledge_base_id: int,
    document_id: int,
    api_key: ApiKeyContext = Depends(current_api_key),
) -> dict:
    kb = _resolve_user_knowledge_base(api_key.user_id, knowledge_base_id)
    try:
        doc = billing_store.get_knowledge_document_for_user(api_key.user_id, int(kb["id"]), document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    deleted_vectors = pipeline.indexer.remove_run_chunks(
        run_id=str(doc.get("run_id", "")),
        user_id=api_key.user_id,
        knowledge_base_id=int(kb["id"]),
    )
    deleted_artifacts = _remove_artifact_dir(str(doc.get("artifact_path", "")))
    deleted_doc = billing_store.delete_knowledge_document(api_key.user_id, int(kb["id"]), document_id)
    return {
        "status": "ok",
        "knowledge_base": kb,
        "document": deleted_doc,
        "deleted_vectors": deleted_vectors,
        "deleted_artifacts": deleted_artifacts,
    }


@app.post("/api/v1/knowledge-bases/{knowledge_base_id}/search")
def api_knowledge_search(
    knowledge_base_id: int,
    payload: RagSearchRequest,
    api_key: ApiKeyContext = Depends(current_api_key),
) -> dict:
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is empty")
    kb = _resolve_user_knowledge_base(api_key.user_id, knowledge_base_id)
    start = time.perf_counter()
    reservation = _reserve_call_quota(api_key.user_id, api_key.is_admin)
    try:
        mode = payload.retrieval_mode
        query_vector = pipeline.embedding.embed_query(query) if mode in ("semantic", "hybrid") else None
        hits = pipeline.indexer.search_chunks(
            query_vector=query_vector,
            query_text=query,
            top_k=payload.top_k,
            source_filename=payload.source_filename.strip() if payload.source_filename else None,
            user_id=api_key.user_id,
            knowledge_base_id=int(kb["id"]),
            article_versions=wewe_store.published_vector_versions(api_key.user_id, int(kb["id"])),
            retrieval_mode=mode,
        )
        hits = _attach_source_urls(hits)
        image_hits = (
            _search_related_images(query, api_key.user_id, int(kb["id"]), top_k=payload.top_k)
            if payload.include_images
            else []
        )
        billing_store.consume_call(
            user_id=api_key.user_id,
            api_key_id=api_key.id,
            endpoint="/api/v1/knowledge-bases/{id}/search",
            method="POST",
            status_code=200,
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=estimate_tokens(query),
            output_tokens=sum(estimate_tokens(str(hit.get("text", ""))) for hit in hits),
            meta={"retrieval_mode": mode, "top_k": payload.top_k, "knowledge_base_id": kb["id"]},
            reservation=reservation,
            call_count=0 if api_key.is_admin else 1,
        )
    except Exception as exc:
        billing_store.consume_call(
            user_id=api_key.user_id,
            api_key_id=api_key.id,
            endpoint="/api/v1/knowledge-bases/{id}/search",
            method="POST",
            status_code=500,
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=estimate_tokens(query),
            meta={"knowledge_base_id": kb["id"], "error": str(exc)},
            reservation=reservation,
            call_count=0,
        )
        raise HTTPException(status_code=500, detail=f"RAG search failed: {exc}") from exc
    return {
        "query": query,
        "top_k": payload.top_k,
        "source_filename": payload.source_filename,
        "knowledge_base": kb,
        "retrieval_mode": payload.retrieval_mode,
        "hits": hits,
        "image_hits": image_hits,
    }


@app.post("/api/v1/knowledge-bases/{knowledge_base_id}/answer")
def api_knowledge_answer(
    knowledge_base_id: int,
    payload: RagAnswerRequest,
    api_key: ApiKeyContext = Depends(current_api_key),
) -> dict:
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is empty")
    kb = _resolve_user_knowledge_base(api_key.user_id, knowledge_base_id)
    start = time.perf_counter()
    reservation = _reserve_call_quota(api_key.user_id, api_key.is_admin)
    try:
        mode = payload.retrieval_mode
        query_vector = pipeline.embedding.embed_query(query) if mode in ("semantic", "hybrid") else None
        hits = pipeline.indexer.search_chunks(
            query_vector=query_vector,
            query_text=query,
            top_k=payload.top_k,
            source_filename=payload.source_filename.strip() if payload.source_filename else None,
            user_id=api_key.user_id,
            knowledge_base_id=int(kb["id"]),
            article_versions=wewe_store.published_vector_versions(api_key.user_id, int(kb["id"])),
            retrieval_mode=mode,
        )
        hits = _attach_source_urls(hits)
        image_hits = (
            _search_related_images(query, api_key.user_id, int(kb["id"]), top_k=payload.top_k)
            if payload.include_images
            else []
        )
        if not hits:
            answer = (
                "未找到文字依据；已返回相关图片，请结合图片引用查看。"
                if image_hits else "知识库中未找到可用于回答该问题的依据。"
            )
            citations: list[dict] = _image_citations_from_hits(image_hits)
            log_item = billing_store.record_rag_query_log(
                user_id=api_key.user_id,
                api_key_id=api_key.id,
                knowledge_base_id=int(kb["id"]),
                query=query,
                answer=answer,
                retrieval_mode=mode,
                top_k=payload.top_k,
                source_filename=payload.source_filename,
                hit_count=0,
                citations=citations,
            )
            billing_store.consume_call(
                user_id=api_key.user_id,
                api_key_id=api_key.id,
                endpoint="/api/v1/knowledge-bases/{id}/answer",
                method="POST",
                status_code=200,
                latency_ms=int((time.perf_counter() - start) * 1000),
                input_tokens=estimate_tokens(query),
                output_tokens=estimate_tokens(answer),
                meta={"retrieval_mode": mode, "top_k": payload.top_k, "hit_count": 0, "knowledge_base_id": kb["id"]},
                reservation=reservation,
                call_count=0 if api_key.is_admin else 1,
            )
            return {
                "query": query,
                "top_k": payload.top_k,
                "source_filename": payload.source_filename,
                "knowledge_base": kb,
                "retrieval_mode": payload.retrieval_mode,
                "answer": answer,
                "query_log_id": log_item["id"],
                "citations": citations,
                "hits": [],
                "image_hits": image_hits,
            }

        answer = pipeline.chat.answer_with_context(question=query, hits=hits)
        citations = _citations_from_hits(hits)
        for offset, image_hit in enumerate(image_hits, start=len(citations) + 1):
            citations.append(
                {
                    "index": offset,
                    "score": image_hit.get("score", 0.0),
                    "source_filename": image_hit.get("article_title") or image_hit.get("source_name") or "",
                    "source_file": image_hit.get("article_url") or "",
                    "source_file_url": image_hit.get("image_file_url"),
                    "knowledge_base_id": image_hit.get("knowledge_base_id"),
                    "chunk_index": -1,
                    "chapter": None,
                    "snippet": image_hit.get("description") or "",
                    "vector_space": "image",
                    "image_id": image_hit.get("image_id"),
                    "article_id": image_hit.get("article_id"),
                }
            )
        log_item = billing_store.record_rag_query_log(
            user_id=api_key.user_id,
            api_key_id=api_key.id,
            knowledge_base_id=int(kb["id"]),
            query=query,
            answer=answer,
            retrieval_mode=mode,
            top_k=payload.top_k,
            source_filename=payload.source_filename,
            hit_count=len(hits),
            citations=citations,
        )
        billing_store.consume_call(
            user_id=api_key.user_id,
            api_key_id=api_key.id,
            endpoint="/api/v1/knowledge-bases/{id}/answer",
            method="POST",
            status_code=200,
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=estimate_tokens(query) + sum(estimate_tokens(str(hit.get("text", ""))) for hit in hits),
            output_tokens=estimate_tokens(answer),
            meta={"retrieval_mode": mode, "top_k": payload.top_k, "hit_count": len(hits), "knowledge_base_id": kb["id"]},
            reservation=reservation,
            call_count=0 if api_key.is_admin else 1,
        )
    except Exception as exc:
        billing_store.consume_call(
            user_id=api_key.user_id,
            api_key_id=api_key.id,
            endpoint="/api/v1/knowledge-bases/{id}/answer",
            method="POST",
            status_code=500,
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=estimate_tokens(query),
            meta={"knowledge_base_id": kb["id"], "error": str(exc)},
            reservation=reservation,
            call_count=0,
        )
        raise HTTPException(status_code=500, detail=f"RAG answer failed: {exc}") from exc

    return {
        "query": query,
        "top_k": payload.top_k,
        "source_filename": payload.source_filename,
        "knowledge_base": kb,
        "retrieval_mode": payload.retrieval_mode,
        "answer": answer,
        "query_log_id": log_item["id"],
        "citations": citations,
        "hits": hits,
        "image_hits": image_hits,
    }


@app.post("/api/v1/rag/feedback")
def api_rag_feedback(payload: RagFeedbackRequest, api_key: ApiKeyContext = Depends(current_api_key)) -> dict:
    try:
        item = billing_store.update_rag_feedback(
            user_id=api_key.user_id,
            log_id=payload.query_log_id,
            rating=payload.rating,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc
    return {"status": "ok", "feedback": item}


@app.get("/api/debug", response_class=HTMLResponse)
def api_debug_page(_: UserContext = Depends(current_user)) -> str:
    return """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>文档解析 API 调试台</title>
  <style>
    :root {
      --ink: #172033;
      --muted: #647084;
      --line: #d8dfeb;
      --panel: #ffffff;
      --paper: #fbfaf7;
      --teal: #087f7a;
      --teal-2: #066b67;
      --red: #b33838;
      --soft: #edf8f5;
      --shadow: 0 18px 42px rgba(23, 32, 51, 0.09);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font-family: Georgia, "Noto Serif SC", "Microsoft YaHei", serif;
      background:
        linear-gradient(120deg, #f7f1df 0 24%, transparent 24%),
        radial-gradient(circle at 88% 10%, #eef7f4 0, transparent 30%),
        var(--paper);
    }
    main { max-width: 1180px; margin: 28px auto; padding: 0 16px 42px; }
    header {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 16px;
      align-items: end;
      border-bottom: 2px solid var(--ink);
      padding-bottom: 18px;
      margin-bottom: 18px;
    }
    h1 { margin: 0; font-size: 34px; letter-spacing: 0; }
    h2 { margin: 0 0 12px; font-size: 19px; }
    .sub { color: var(--muted); font-size: 14px; line-height: 1.6; }
    .nav { display: flex; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
    a, button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      border: 1px solid var(--ink);
      border-radius: 7px;
      padding: 8px 13px;
      background: var(--ink);
      color: #fff;
      cursor: pointer;
      text-decoration: none;
      font-size: 14px;
      transition: background .15s ease, border-color .15s ease, transform .15s ease;
    }
    a.secondary, button.secondary { background: #fff; color: var(--ink); }
    button.teal { border-color: var(--teal); background: var(--teal); }
    button.teal:hover { border-color: var(--teal-2); background: var(--teal-2); }
    button:hover, a:hover { transform: translateY(-1px); }
    button:disabled { opacity: .55; cursor: not-allowed; transform: none; }
    .grid { display: grid; grid-template-columns: minmax(330px, 420px) minmax(0, 1fr); gap: 14px; align-items: start; }
    .panel {
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.92);
      border-radius: 10px;
      padding: 16px;
      box-shadow: var(--shadow);
    }
    .stack { display: grid; gap: 12px; }
    label { display: block; font-size: 13px; font-weight: 700; margin: 12px 0 6px; }
    input, select {
      width: 100%;
      border: 1px solid #c8d0dd;
      border-radius: 7px;
      padding: 11px 12px;
      font-size: 14px;
      background: #fff;
      color: var(--ink);
    }
    input:focus, select:focus { outline: 2px solid rgba(8, 127, 122, 0.16); border-color: #6bbab4; }
    .endpoint-box {
      display: grid;
      gap: 8px;
      border: 1px solid #cfe2df;
      border-radius: 8px;
      background: var(--soft);
      padding: 12px;
    }
    .endpoint-url {
      font-family: Consolas, "Courier New", monospace;
      word-break: break-all;
      color: var(--teal);
      font-size: 13px;
    }
    .check-row {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 9px;
      align-items: start;
      border: 1px solid #dce4ee;
      border-radius: 8px;
      padding: 10px;
      background: #fff;
      margin-top: 10px;
    }
    .check-row input { width: auto; margin-top: 3px; }
    .check-title { font-weight: 700; font-size: 13px; }
    .check-desc { color: var(--muted); font-size: 12px; margin-top: 3px; line-height: 1.5; }
    .tabs {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 8px;
      margin: 10px 0 12px;
    }
    .tab { background: #fff; color: var(--ink); border-color: var(--line); }
    .tab.active { background: var(--teal); color: #fff; border-color: var(--teal); }
    .code-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-bottom: 8px;
    }
    .copy-btn { min-height: 32px; padding: 6px 10px; background: #fff; color: var(--ink); border-color: var(--line); }
    pre {
      max-height: 430px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      background: #111827;
      color: #e8f0ff;
      border-radius: 8px;
      padding: 14px;
      margin: 0;
      font-family: Consolas, "Courier New", monospace;
      font-size: 13px;
      line-height: 1.55;
    }
    #result { min-height: 260px; }
    .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .links { margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
    .links a { min-height: 32px; padding: 6px 10px; background: #fff; color: var(--teal); border-color: #94c9c5; }
    .status { min-height: 22px; color: var(--muted); font-size: 13px; margin-top: 10px; }
    .status.err { color: var(--red); }
    .mode-fields { display: grid; gap: 0; }
    .mode-fields[hidden] { display: none; }
    @media (max-width: 860px) {
      header, .grid { grid-template-columns: 1fr; }
      .nav { justify-content: flex-start; }
      .tabs { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      h1 { font-size: 28px; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>文档解析 API 调试台</h1>
        <div class="sub">POST /api/v1/parse · X-API-Key 认证 · Vision 与向量入库默认关闭。</div>
      </div>
      <nav class="nav" aria-label="调试台导航">
        <a class="secondary" href="/">开放平台</a>
        <a class="secondary" href="/docs">接口文档</a>
      </nav>
    </header>
    <section class="grid">
      <form id="debugForm" class="panel">
        <h2>请求配置</h2>
        <label for="domain">接口 domain</label>
        <input id="domain" autocomplete="off" />
        <label for="apiMode">接口类型</label>
        <select id="apiMode">
          <option value="parse">文档解析 / 入库</option>
          <option value="search">知识库检索</option>
          <option value="answer">知识库问答</option>
        </select>
        <div class="endpoint-box">
          <div class="sub">当前接口地址</div>
          <div id="endpointUrl" class="endpoint-url">-</div>
        </div>
        <label for="apiKey">API Key</label>
        <input id="apiKey" type="password" placeholder="dp_xxx" autocomplete="off" />
        <label id="knowledgeBaseIdLabel" for="knowledgeBaseId">知识库 ID（可选）</label>
        <input id="knowledgeBaseId" type="number" min="1" placeholder="留空则使用默认知识库" />
        <div id="ragFields" class="mode-fields" hidden>
          <label for="ragQuery">检索 / 问答问题</label>
          <input id="ragQuery" autocomplete="off" placeholder="例如：售后退换货规则是什么？" />
          <label for="ragTopK">召回数量 Top K</label>
          <input id="ragTopK" type="number" min="1" max="30" value="5" />
          <label for="ragRetrievalMode">检索模式</label>
          <select id="ragRetrievalMode">
            <option value="hybrid">hybrid 混合检索</option>
            <option value="semantic">semantic 语义检索</option>
            <option value="keyword">keyword 关键词检索</option>
          </select>
          <label for="ragSourceFilename">限定文档名（可选）</label>
          <input id="ragSourceFilename" autocomplete="off" placeholder="例如：manual.pdf" />
        </div>
        <div id="parseFields" class="mode-fields">
          <label for="file">文档文件</label>
          <input id="file" type="file" accept=".txt,.md,.markdown,.pdf,.doc,.docx" />
          <label for="outputFormat">返回格式</label>
          <select id="outputFormat">
            <option value="both">TXT + Markdown</option>
            <option value="json">仅结构化 JSON</option>
            <option value="markdown">仅 Markdown</option>
            <option value="txt">仅 TXT</option>
          </select>
          <label class="check-row" for="runVision">
            <input id="runVision" type="checkbox" />
            <span>
              <span class="check-title">启用图片结构化识别</span>
              <span class="check-desc">仅在本次调试请求中开启，默认关闭。</span>
            </span>
          </label>
          <label class="check-row" for="ingestVector">
            <input id="ingestVector" type="checkbox" />
            <span>
              <span class="check-title">写入当前账号向量库</span>
              <span class="check-desc">仅在本次调试请求中开启，数据与当前 API Key 绑定隔离。</span>
            </span>
          </label>
        </div>
        <button id="sendBtn" class="teal" type="submit">发送解析请求</button>
        <div id="status" class="status"></div>
        <div id="links" class="links"></div>
      </form>
      <section class="stack">
        <section class="panel">
          <h2>响应结果</h2>
          <pre id="result">等待请求...</pre>
        </section>
        <section class="panel">
          <h2>HTTP 调用 demo</h2>
          <div class="tabs" aria-label="示例类型">
            <button class="tab active" data-demo="curl" type="button">curl</button>
            <button class="tab" data-demo="node" type="button">Node.js</button>
            <button class="tab" data-demo="python" type="button">Python</button>
            <button class="tab" data-demo="wechat" type="button">小程序</button>
            <button class="tab" data-demo="http" type="button">HTTP</button>
          </div>
          <div class="code-head">
            <div id="demoTitle" class="sub">curl</div>
            <button id="copyDemoBtn" class="copy-btn" type="button">复制示例</button>
          </div>
          <pre id="demoCode"></pre>
        </section>
      </section>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let activeDemo = "curl";

    function cleanDomain(value) {
      return String(value || "").trim().replace(/\\/+$/, "");
    }

    function endpointUrl() {
      const domain = cleanDomain($("domain").value || window.location.origin);
      const mode = $("apiMode").value;
      const kbId = $("knowledgeBaseId").value.trim() || "{knowledge_base_id}";
      if (mode === "search") return `${domain}/api/v1/knowledge-bases/${kbId}/search`;
      if (mode === "answer") return `${domain}/api/v1/knowledge-bases/${kbId}/answer`;
      return `${domain}/api/v1/parse`;
    }

    function updateEndpoint() {
      $("endpointUrl").textContent = endpointUrl();
      renderDemo();
    }

    function demoOptions() {
      return {
        url: endpointUrl(),
        host: cleanDomain($("domain").value || window.location.origin).replace(/^https?:\/\//, ""),
        outputFormat: $("outputFormat").value,
        runVision: $("runVision").checked ? "true" : "false",
        ingestVector: $("ingestVector").checked ? "true" : "false",
        knowledgeBaseId: $("knowledgeBaseId").value.trim(),
        apiMode: $("apiMode").value,
        query: $("ragQuery").value.trim() || "售后退换货规则是什么？",
        topK: $("ragTopK").value.trim() || "5",
        retrievalMode: $("ragRetrievalMode").value,
        sourceFilename: $("ragSourceFilename").value.trim(),
      };
    }

    function requestPath(url) {
      try {
        return new URL(url).pathname;
      } catch {
        return String(url || "").replace(/^https?:\/\/[^/]+/, "") || "/";
      }
    }

    function ragPayload(o) {
      const payload = {
        query: o.query,
        top_k: Number(o.topK) || 5,
        retrieval_mode: o.retrievalMode,
      };
      if (o.sourceFilename) payload.source_filename = o.sourceFilename;
      return payload;
    }

    function jsonBody(value) {
      return JSON.stringify(value, null, 2);
    }

    function demos() {
      const o = demoOptions();
      if (o.apiMode === "search" || o.apiMode === "answer") {
        const payload = ragPayload(o);
        const body = jsonBody(payload);
        return {
          curl: `curl -X POST "${o.url}" \\
  -H "X-API-Key: YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '${body}'`,
          node: `const payload = ${body};

const resp = await fetch("${o.url}", {
  method: "POST",
  headers: {
    "X-API-Key": process.env.DOC_PARSE_API_KEY,
    "Content-Type": "application/json",
  },
  body: JSON.stringify(payload),
});

console.log(await resp.json());`,
          python: `import requests

url = "${o.url}"
headers = {
    "X-API-Key": "YOUR_API_KEY",
    "Content-Type": "application/json",
}
payload = ${body.replace(/true/g, "True").replace(/false/g, "False").replace(/null/g, "None")}

resp = requests.post(url, headers=headers, json=payload, timeout=60)
resp.raise_for_status()
print(resp.json())`,
          wechat: `wx.request({
  url: "${o.url}",
  method: "POST",
  header: {
    "X-API-Key": "YOUR_API_KEY",
    "Content-Type": "application/json"
  },
  data: ${body},
  success(res) {
    console.log(res.data);
  },
  fail(err) {
    console.error(err);
  }
});`,
          http: `POST ${requestPath(o.url)} HTTP/1.1
Host: ${o.host}
X-API-Key: YOUR_API_KEY
Content-Type: application/json

${body}`,
        };
      }
      return {
        curl: `curl -X POST "${o.url}" \\
  -H "X-API-Key: YOUR_API_KEY" \\
  -F "file=@./demo.pdf" \\
  -F "output_format=${o.outputFormat}" \\
  -F "run_vision=${o.runVision}" \\
  -F "ingest_vector=${o.ingestVector}"${o.knowledgeBaseId ? ` \\
  -F "knowledge_base_id=${o.knowledgeBaseId}"` : ""}`,
        node: `import fs from "node:fs";

const form = new FormData();
form.append("file", new Blob([fs.readFileSync("./demo.pdf")]), "demo.pdf");
form.append("output_format", "${o.outputFormat}");
form.append("run_vision", "${o.runVision}");
form.append("ingest_vector", "${o.ingestVector}");
${o.knowledgeBaseId ? `form.append("knowledge_base_id", "${o.knowledgeBaseId}");` : `// form.append("knowledge_base_id", "1"); // 可选：指定专属知识库`}

const resp = await fetch("${o.url}", {
  method: "POST",
  headers: { "X-API-Key": process.env.DOC_PARSE_API_KEY },
  body: form,
});

console.log(await resp.json());`,
        python: `import requests

url = "${o.url}"
headers = {"X-API-Key": "YOUR_API_KEY"}
data = {
    "output_format": "${o.outputFormat}",
    "run_vision": "${o.runVision}",
    "ingest_vector": "${o.ingestVector}",
${o.knowledgeBaseId ? `    "knowledge_base_id": "${o.knowledgeBaseId}",` : `    # "knowledge_base_id": "1",  # 可选：指定专属知识库`}
}

with open("./demo.pdf", "rb") as f:
    files = {"file": ("demo.pdf", f, "application/pdf")}
    resp = requests.post(url, headers=headers, data=data, files=files, timeout=120)
    resp.raise_for_status()
    print(resp.json())`,
        wechat: `wx.uploadFile({
  url: "${o.url}",
  filePath: tempFilePath,
  name: "file",
  header: {
    "X-API-Key": "YOUR_API_KEY"
  },
  formData: {
    output_format: "${o.outputFormat}",
    run_vision: "${o.runVision}",
    ingest_vector: "${o.ingestVector}"${o.knowledgeBaseId ? `,
    knowledge_base_id: "${o.knowledgeBaseId}"` : ""}
  },
  success(res) {
    const data = JSON.parse(res.data);
    console.log(data);
  },
  fail(err) {
    console.error(err);
  }
});`,
        http: `POST /api/v1/parse HTTP/1.1
Host: ${o.host}
X-API-Key: YOUR_API_KEY
Content-Type: multipart/form-data; boundary=----Boundary

------Boundary
Content-Disposition: form-data; name="output_format"

${o.outputFormat}
------Boundary
Content-Disposition: form-data; name="run_vision"

${o.runVision}
------Boundary
Content-Disposition: form-data; name="ingest_vector"

${o.ingestVector}
${o.knowledgeBaseId ? `------Boundary
Content-Disposition: form-data; name="knowledge_base_id"

${o.knowledgeBaseId}
` : ""}------Boundary
Content-Disposition: form-data; name="file"; filename="demo.pdf"
Content-Type: application/pdf

<binary>
------Boundary--`,
      };
    }

    function renderDemo() {
      const labels = { curl: "curl", node: "Node.js 后端", python: "Python 后端", wechat: "微信小程序", http: "原始 HTTP" };
      $("demoTitle").textContent = labels[activeDemo] || "调用示例";
      $("demoCode").textContent = demos()[activeDemo] || "";
    }

    function showStatus(message, level = "") {
      $("status").className = `status ${level}`;
      $("status").textContent = message || "";
    }

    function updateModeFields() {
      const mode = $("apiMode").value;
      const isParse = mode === "parse";
      $("parseFields").hidden = !isParse;
      $("ragFields").hidden = isParse;
      $("knowledgeBaseIdLabel").textContent = isParse ? "知识库 ID（入库时可选）" : "知识库 ID（必填）";
      $("knowledgeBaseId").placeholder = isParse ? "留空则使用默认知识库" : "请输入要检索的专属知识库 ID";
      $("sendBtn").textContent = mode === "answer" ? "发送问答请求" : mode === "search" ? "发送检索请求" : "发送解析请求";
    }

    function buildParseRequest() {
      const file = $("file").files[0];
      if (!file) throw new Error("请选择要解析的文档文件。");
      const form = new FormData();
      form.append("file", file);
      form.append("output_format", $("outputFormat").value);
      form.append("run_vision", $("runVision").checked ? "true" : "false");
      form.append("ingest_vector", $("ingestVector").checked ? "true" : "false");
      if ($("knowledgeBaseId").value.trim()) form.append("knowledge_base_id", $("knowledgeBaseId").value.trim());
      return {
        body: form,
        headers: {},
        waitingText: "请求已发送，正在等待解析结果...",
        successText: "解析完成。",
      };
    }

    function buildRagRequest() {
      const kbId = $("knowledgeBaseId").value.trim();
      const query = $("ragQuery").value.trim();
      if (!kbId) throw new Error("请填写知识库 ID。");
      if (!query) throw new Error("请填写检索 / 问答问题。");
      const payload = {
        query,
        top_k: Number($("ragTopK").value || 5),
        retrieval_mode: $("ragRetrievalMode").value,
      };
      if ($("ragSourceFilename").value.trim()) payload.source_filename = $("ragSourceFilename").value.trim();
      return {
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
        waitingText: $("apiMode").value === "answer" ? "请求已发送，正在生成知识库问答..." : "请求已发送，正在检索知识库...",
        successText: $("apiMode").value === "answer" ? "问答完成。" : "检索完成。",
      };
    }

    $("domain").value = window.location.origin;
    updateModeFields();
    updateEndpoint();

    $("domain").addEventListener("input", updateEndpoint);
    $("apiMode").addEventListener("change", () => {
      updateModeFields();
      updateEndpoint();
    });
    $("outputFormat").addEventListener("change", renderDemo);
    $("knowledgeBaseId").addEventListener("input", updateEndpoint);
    $("ragQuery").addEventListener("input", renderDemo);
    $("ragTopK").addEventListener("input", renderDemo);
    $("ragRetrievalMode").addEventListener("change", renderDemo);
    $("ragSourceFilename").addEventListener("input", renderDemo);
    $("runVision").addEventListener("change", renderDemo);
    $("ingestVector").addEventListener("change", renderDemo);

    document.querySelectorAll(".tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        activeDemo = btn.dataset.demo;
        document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === btn));
        renderDemo();
      });
    });

    $("copyDemoBtn").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("demoCode").textContent);
        showStatus("调用示例已复制。");
      } catch {
        showStatus("复制失败，请手动选择示例内容。", "err");
      }
    });

    $("debugForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const key = $("apiKey").value.trim();
      if (!key) {
        $("result").textContent = "请填写 API Key。";
        showStatus("请先补齐请求参数。", "err");
        return;
      }
      let requestConfig;
      try {
        requestConfig = $("apiMode").value === "parse" ? buildParseRequest() : buildRagRequest();
      } catch (err) {
        $("result").textContent = err.message || String(err);
        showStatus("请先补齐请求参数。", "err");
        return;
      }
      $("sendBtn").disabled = true;
      $("result").textContent = "处理中...";
      showStatus(requestConfig.waitingText);
      $("links").innerHTML = "";
      try {
        const resp = await fetch(endpointUrl(), {
          method: "POST",
          headers: { "X-API-Key": key, ...requestConfig.headers },
          body: requestConfig.body,
        });
        const data = await resp.json();
        $("result").textContent = JSON.stringify(data, null, 2);
        if (!resp.ok) {
          showStatus(data.detail || "请求失败，请检查参数或 API Key。", "err");
          return;
        }
        const links = [];
        if (data.text_url) links.push(`<a href="${data.text_url}" target="_blank">下载 TXT</a>`);
        if (data.markdown_url) links.push(`<a href="${data.markdown_url}" target="_blank">下载 Markdown</a>`);
        if (data.text_blocks_url) links.push(`<a href="${data.text_blocks_url}" target="_blank">text_blocks.json</a>`);
        if (data.image_records_url) links.push(`<a href="${data.image_records_url}" target="_blank">image_records.json</a>`);
        if (data.image_structured_text_url) links.push(`<a href="${data.image_structured_text_url}" target="_blank">image_structured_text.json</a>`);
        $("links").innerHTML = links.join("");
        showStatus(requestConfig.successText);
      } catch (err) {
        $("result").textContent = String(err);
        showStatus("请求异常，请检查接口 domain 或网络连接。", "err");
      } finally {
        $("sendBtn").disabled = false;
      }
    });
  </script>
</body>
</html>
"""



@app.get("/api/config")
def get_config(_: UserContext = Depends(current_admin)) -> dict:
    env_path = get_env_file_path()
    _, kv = _read_env_file(env_path)
    values = {k: kv.get(k, "") for k in CONFIG_KEYS}
    return {"values": _mask_sensitive(values)}


@app.post("/api/config")
def update_config(payload: ConfigUpdateRequest, _: UserContext = Depends(current_admin)) -> dict:
    if not payload.values:
        raise HTTPException(status_code=400, detail="values is empty")

    updates: dict[str, str] = {}
    for key, value in payload.values.items():
        if key not in CONFIG_KEYS:
            continue
        v = str(value).strip()
        if key in ("ARK_API_KEY", "QDRANT_API_KEY") and "***" in v:
            continue
        updates[key] = v

    if not updates:
        raise HTTPException(status_code=400, detail="No valid config values to update.")

    env_path = get_env_file_path()
    _write_env_file(env_path, updates)
    _reload_runtime()
    return {"status": "ok", "updated_keys": sorted(updates.keys())}


@app.post("/api/rag/search")
def rag_search(payload: RagSearchRequest, user: UserContext = Depends(current_user)) -> dict:
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is empty")
    start = time.perf_counter()
    kb = _resolve_user_knowledge_base(user.id, payload.knowledge_base_id)
    reservation = _reserve_call_quota(user.id, user.is_admin)
    try:
        mode = payload.retrieval_mode
        query_vector = pipeline.embedding.embed_query(query) if mode in ("semantic", "hybrid") else None
        hits = pipeline.indexer.search_chunks(
            query_vector=query_vector,
            query_text=query,
            top_k=payload.top_k,
            source_filename=payload.source_filename.strip() if payload.source_filename else None,
            user_id=user.id,
            knowledge_base_id=int(kb["id"]),
            article_versions=wewe_store.published_vector_versions(user.id, int(kb["id"])),
            retrieval_mode=mode,
        )
        hits = _attach_source_urls(hits)
        image_hits = (
            _search_related_images(query, user.id, int(kb["id"]), top_k=payload.top_k)
            if payload.include_images
            else []
        )
        billing_store.consume_call(
            user_id=user.id,
            api_key_id=None,
            endpoint="/api/rag/search",
            method="POST",
            status_code=200,
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=estimate_tokens(query),
            output_tokens=sum(estimate_tokens(str(hit.get("text", ""))) for hit in hits),
            meta={"retrieval_mode": mode, "top_k": payload.top_k, "knowledge_base_id": kb["id"]},
            reservation=reservation,
            call_count=0 if user.is_admin else 1,
        )
    except Exception as exc:
        billing_store.consume_call(
            user_id=user.id,
            api_key_id=None,
            endpoint="/api/rag/search",
            method="POST",
            status_code=500,
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=estimate_tokens(query),
            meta={"error": str(exc)},
            reservation=reservation,
            call_count=0,
        )
        raise HTTPException(status_code=500, detail=f"RAG search failed: {exc}") from exc

    return {
        "query": query,
        "top_k": payload.top_k,
        "source_filename": payload.source_filename,
        "knowledge_base": kb,
        "retrieval_mode": payload.retrieval_mode,
        "hits": hits,
        "image_hits": image_hits,
    }


@app.get("/api/rag/sources")
def rag_sources(knowledge_base_id: int | None = None, user: UserContext = Depends(current_user)) -> dict:
    kb = _resolve_user_knowledge_base(user.id, knowledge_base_id)
    try:
        items = pipeline.indexer.list_sources(
            user_id=user.id,
            knowledge_base_id=int(kb["id"]),
            article_versions=wewe_store.published_vector_versions(user.id, int(kb["id"])),
        )
        enriched = []
        for item in items:
            source_item = dict(item)
            source_item["source_file_url"] = _to_artifact_url(str(source_item.get("source_file", "")))
            source_item["source_text_url"] = _to_artifact_url(str(source_item.get("merged_output", "")))
            enriched.append(source_item)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"RAG source listing failed: {exc}") from exc

    return {"knowledge_base": kb, "count": len(enriched), "sources": enriched}


@app.post("/api/rag/feedback")
def rag_feedback(payload: RagFeedbackRequest, user: UserContext = Depends(current_user)) -> dict:
    try:
        item = billing_store.update_rag_feedback(
            user_id=user.id,
            log_id=payload.query_log_id,
            rating=payload.rating,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc
    return {"status": "ok", "feedback": item}


@app.post("/api/rag/answer")
def rag_answer(payload: RagAnswerRequest, user: UserContext = Depends(current_user)) -> dict:
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is empty")
    start = time.perf_counter()
    kb = _resolve_user_knowledge_base(user.id, payload.knowledge_base_id)
    reservation = _reserve_call_quota(user.id, user.is_admin)
    try:
        mode = payload.retrieval_mode
        query_vector = pipeline.embedding.embed_query(query) if mode in ("semantic", "hybrid") else None
        hits = pipeline.indexer.search_chunks(
            query_vector=query_vector,
            query_text=query,
            top_k=payload.top_k,
            source_filename=payload.source_filename.strip() if payload.source_filename else None,
            user_id=user.id,
            knowledge_base_id=int(kb["id"]),
            article_versions=wewe_store.published_vector_versions(user.id, int(kb["id"])),
            retrieval_mode=mode,
        )
        hits = _attach_source_urls(hits)
        image_hits = (
            _search_related_images(query, user.id, int(kb["id"]), top_k=payload.top_k)
            if payload.include_images
            else []
        )
        if not hits:
            answer = (
                "未找到文字依据；已返回相关图片，请结合图片引用查看。"
                if image_hits else "知识库中未找到可用于回答该问题的依据。"
            )
            citations: list[dict] = _image_citations_from_hits(image_hits)
            log_item = billing_store.record_rag_query_log(
                user_id=user.id,
                api_key_id=None,
                knowledge_base_id=int(kb["id"]),
                query=query,
                answer=answer,
                retrieval_mode=mode,
                top_k=payload.top_k,
                source_filename=payload.source_filename,
                hit_count=0,
                citations=citations,
            )
            billing_store.consume_call(
                user_id=user.id,
                api_key_id=None,
                endpoint="/api/rag/answer",
                method="POST",
                status_code=200,
                latency_ms=int((time.perf_counter() - start) * 1000),
                input_tokens=estimate_tokens(query),
                output_tokens=estimate_tokens(answer),
                meta={"retrieval_mode": mode, "top_k": payload.top_k, "hit_count": 0, "knowledge_base_id": kb["id"]},
                reservation=reservation,
                call_count=0 if user.is_admin else 1,
            )
            return {
                "query": query,
                "top_k": payload.top_k,
                "source_filename": payload.source_filename,
                "knowledge_base": kb,
                "retrieval_mode": payload.retrieval_mode,
                "answer": answer,
                "query_log_id": log_item["id"],
                "citations": citations,
                "hits": [],
                "image_hits": image_hits,
            }

        answer = pipeline.chat.answer_with_context(question=query, hits=hits)
        citations = _citations_from_hits(hits)
        for offset, image_hit in enumerate(image_hits, start=len(citations) + 1):
            citations.append(
                {
                    "index": offset,
                    "score": image_hit.get("score", 0.0),
                    "source_filename": image_hit.get("article_title") or image_hit.get("source_name") or "",
                    "source_file": image_hit.get("article_url") or "",
                    "source_file_url": image_hit.get("image_file_url"),
                    "knowledge_base_id": image_hit.get("knowledge_base_id"),
                    "chunk_index": -1,
                    "chapter": None,
                    "snippet": image_hit.get("description") or "",
                    "vector_space": "image",
                    "image_id": image_hit.get("image_id"),
                    "article_id": image_hit.get("article_id"),
                }
            )
        log_item = billing_store.record_rag_query_log(
            user_id=user.id,
            api_key_id=None,
            knowledge_base_id=int(kb["id"]),
            query=query,
            answer=answer,
            retrieval_mode=mode,
            top_k=payload.top_k,
            source_filename=payload.source_filename,
            hit_count=len(hits),
            citations=citations,
        )
        billing_store.consume_call(
            user_id=user.id,
            api_key_id=None,
            endpoint="/api/rag/answer",
            method="POST",
            status_code=200,
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=estimate_tokens(query) + sum(estimate_tokens(str(hit.get("text", ""))) for hit in hits),
            output_tokens=estimate_tokens(answer),
            meta={"retrieval_mode": mode, "top_k": payload.top_k, "hit_count": len(hits), "knowledge_base_id": kb["id"]},
            reservation=reservation,
            call_count=0 if user.is_admin else 1,
        )
    except Exception as exc:
        billing_store.consume_call(
            user_id=user.id,
            api_key_id=None,
            endpoint="/api/rag/answer",
            method="POST",
            status_code=500,
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=estimate_tokens(query),
            meta={"error": str(exc)},
            reservation=reservation,
            call_count=0,
        )
        raise HTTPException(status_code=500, detail=f"RAG answer failed: {exc}") from exc

    return {
        "query": query,
        "top_k": payload.top_k,
        "source_filename": payload.source_filename,
        "knowledge_base": kb,
        "retrieval_mode": payload.retrieval_mode,
        "answer": answer,
        "query_log_id": log_item["id"],
        "citations": citations,
        "hits": hits,
        "image_hits": image_hits,
    }
