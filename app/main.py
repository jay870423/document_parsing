from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import Settings, get_env_file_path, get_settings
from app.pipeline import DocumentPipeline

app = FastAPI(title=get_settings().app_name)
STATIC_DIR = Path(__file__).resolve().parent / "static"
ARTIFACTS_DIR = Path("artifacts")
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/artifacts", StaticFiles(directory=str(ARTIFACTS_DIR)), name="artifacts")

settings: Settings = get_settings()
pipeline: DocumentPipeline = DocumentPipeline(settings)

CONFIG_KEYS = [
    "APP_NAME",
    "APP_HOST",
    "APP_PORT",
    "LOG_LEVEL",
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
    retrieval_mode: Literal["semantic", "keyword", "hybrid"] = "semantic"


class RagAnswerRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=30)
    source_filename: str | None = None
    retrieval_mode: Literal["semantic", "keyword", "hybrid"] = "semantic"


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
    for key in ("ARK_API_KEY", "QDRANT_API_KEY"):
        val = masked.get(key, "")
        if val:
            masked[key] = f"{val[:4]}***{val[-3:]}" if len(val) > 8 else "***"
    return masked


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


def _attach_source_urls(hits: list[dict]) -> list[dict]:
    enriched: list[dict] = []
    for hit in hits:
        item = dict(hit)
        item["source_file_url"] = _to_artifact_url(str(item.get("source_file", "")))
        item["source_text_url"] = _to_artifact_url(str(item.get("merged_output", "")))
        enriched.append(item)
    return enriched


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": settings.app_name}


@app.post("/process")
async def process_document(
    file: UploadFile = File(...),
    run_vision: bool = Form(True),
    ingest_vector: bool = Form(False),
    rebuild_index: bool = Form(False),
) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename.")

    run_id = uuid.uuid4().hex
    work_dir = Path("artifacts") / run_id
    input_dir = work_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
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
        )
        merged_output = Path(result["merged_output"])
        try:
            result["merged_output_url"] = "/" + merged_output.as_posix()
        except Exception:
            result["merged_output_url"] = None
        result["text_blocks_url"] = _to_artifact_url(str(work_dir / "text_blocks.json"))
        result["image_records_url"] = _to_artifact_url(str(work_dir / "image_records.json"))
        result["image_structured_text_url"] = _to_artifact_url(str(work_dir / "image_structured_text.json"))
        result["images_dir_url"] = _to_artifact_url(str(work_dir / "images"))
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/config")
def get_config() -> dict:
    env_path = get_env_file_path()
    _, kv = _read_env_file(env_path)
    values = {k: kv.get(k, "") for k in CONFIG_KEYS}
    return {"values": _mask_sensitive(values)}


@app.post("/api/config")
def update_config(payload: ConfigUpdateRequest) -> dict:
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
def rag_search(payload: RagSearchRequest) -> dict:
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is empty")

    try:
        mode = payload.retrieval_mode
        query_vector = pipeline.embedding.embed_query(query) if mode in ("semantic", "hybrid") else None
        hits = pipeline.indexer.search_chunks(
            query_vector=query_vector,
            query_text=query,
            top_k=payload.top_k,
            source_filename=payload.source_filename.strip() if payload.source_filename else None,
            retrieval_mode=mode,
        )
        hits = _attach_source_urls(hits)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"RAG search failed: {exc}") from exc

    return {
        "query": query,
        "top_k": payload.top_k,
        "source_filename": payload.source_filename,
        "retrieval_mode": payload.retrieval_mode,
        "hits": hits,
    }


@app.get("/api/rag/sources")
def rag_sources() -> dict:
    try:
        items = pipeline.indexer.list_sources()
        enriched = []
        for item in items:
            source_item = dict(item)
            source_item["source_file_url"] = _to_artifact_url(str(source_item.get("source_file", "")))
            source_item["source_text_url"] = _to_artifact_url(str(source_item.get("merged_output", "")))
            enriched.append(source_item)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"RAG source listing failed: {exc}") from exc

    return {"count": len(enriched), "sources": enriched}


@app.post("/api/rag/answer")
def rag_answer(payload: RagAnswerRequest) -> dict:
    query = payload.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is empty")

    try:
        mode = payload.retrieval_mode
        query_vector = pipeline.embedding.embed_query(query) if mode in ("semantic", "hybrid") else None
        hits = pipeline.indexer.search_chunks(
            query_vector=query_vector,
            query_text=query,
            top_k=payload.top_k,
            source_filename=payload.source_filename.strip() if payload.source_filename else None,
            retrieval_mode=mode,
        )
        hits = _attach_source_urls(hits)
        if not hits:
            return {
                "query": query,
                "top_k": payload.top_k,
                "source_filename": payload.source_filename,
                "retrieval_mode": payload.retrieval_mode,
                "answer": "No relevant chunks found. Try changing keywords or removing source filter.",
                "citations": [],
                "hits": [],
            }

        answer = pipeline.chat.answer_with_context(question=query, hits=hits)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"RAG answer failed: {exc}") from exc

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
                "chunk_index": hit["chunk_index"],
                "page_start": hit.get("page_start"),
                "page_end": hit.get("page_end"),
                "chapter": hit.get("chapter"),
                "snippet": hit["text"],
            }
        )

    return {
        "query": query,
        "top_k": payload.top_k,
        "source_filename": payload.source_filename,
        "retrieval_mode": payload.retrieval_mode,
        "answer": answer,
        "citations": citations,
        "hits": hits,
    }
