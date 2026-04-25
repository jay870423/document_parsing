from __future__ import annotations

import math
import re
import uuid
from collections import Counter

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams

from app.config import Settings


def _raise_for_status_with_details(resp: requests.Response) -> None:
    if resp.ok:
        return
    detail = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("code") or "")
            else:
                detail = str(data)
    except Exception:
        detail = (resp.text or "").strip()
    if detail:
        raise RuntimeError(f"HTTP {resp.status_code} calling {resp.url}: {detail}")
    resp.raise_for_status()


PAGE_SPLIT_RE = re.compile(r"={6,}\s*第\s*(\d+)\s*页\s*={6,}")
CHAPTER_HEADING_RE = re.compile(
    r"^(#{1,6}\s+\S.*|第[0-9一二三四五六七八九十百千]+章\s*\S*|\d+(?:\.\d+){1,3}\s+\S.*)$"
)


def _iter_chunk_ranges(text: str, chunk_size: int, chunk_overlap: int) -> list[tuple[int, int]]:
    clean = text.strip()
    if not clean:
        return []
    if chunk_overlap >= chunk_size:
        chunk_overlap = max(chunk_size // 4, 1)

    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(clean):
        end = min(start + chunk_size, len(clean))
        ranges.append((start, end))
        if end == len(clean):
            break
        start = end - chunk_overlap
    return ranges


def split_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    clean = text.strip()
    ranges = _iter_chunk_ranges(clean, chunk_size, chunk_overlap)
    return [clean[s:e] for s, e in ranges]


def _collect_headings(text: str) -> list[tuple[int, str]]:
    headings: list[tuple[int, str]] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and len(stripped) <= 120 and CHAPTER_HEADING_RE.match(stripped):
            headings.append((cursor, stripped))
        cursor += len(line)
    return headings


def _find_latest_heading(headings: list[tuple[int, str]], pos: int) -> str | None:
    latest: str | None = None
    for heading_pos, heading_text in headings:
        if heading_pos > pos:
            break
        latest = heading_text
    return latest


def split_text_with_metadata(text: str, chunk_size: int, chunk_overlap: int) -> list[dict]:
    clean = text.strip()
    if not clean:
        return []

    page_matches = list(PAGE_SPLIT_RE.finditer(clean))
    segments: list[dict] = []

    if not page_matches:
        segments.append({"page": None, "text": clean})
    else:
        if page_matches[0].start() > 0:
            prefix = clean[: page_matches[0].start()].strip()
            if prefix:
                first_page = int(page_matches[0].group(1))
                segments.append({"page": first_page, "text": prefix})

        for idx, match in enumerate(page_matches):
            page = int(match.group(1))
            seg_start = match.end()
            seg_end = page_matches[idx + 1].start() if idx + 1 < len(page_matches) else len(clean)
            seg_text = clean[seg_start:seg_end].strip()
            if seg_text:
                segments.append({"page": page, "text": seg_text})

    chunk_records: list[dict] = []
    for seg in segments:
        segment_text = str(seg.get("text", "")).strip()
        if not segment_text:
            continue
        segment_page = seg.get("page")
        headings = _collect_headings(segment_text)
        for start, end in _iter_chunk_ranges(segment_text, chunk_size, chunk_overlap):
            chunk_text = segment_text[start:end]
            if not chunk_text.strip():
                continue
            chapter = _find_latest_heading(headings, start)
            chunk_records.append(
                {
                    "text": chunk_text,
                    "page_start": segment_page,
                    "page_end": segment_page,
                    "chapter": chapter,
                }
            )

    return chunk_records


class DoubaoEmbeddingClient:
    def __init__(self, settings: Settings):
        self.api_key = settings.ark_api_key
        self.model = settings.doubao_embedding_model
        root = settings.ark_base_url.rstrip("/")
        self.base_url = root + "/embeddings"
        self.multimodal_url = root + "/embeddings/multimodal"
        self._mode: str | None = None

    @staticmethod
    def _extract_embedding(data: dict) -> list[float]:
        payload = data.get("data")
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            embedding = payload[0].get("embedding")
            if isinstance(embedding, list):
                return embedding
        if isinstance(payload, dict):
            embedding = payload.get("embedding")
            if isinstance(embedding, list):
                return embedding
        raise RuntimeError("Unexpected embedding response format from Ark.")

    def _embed_via_standard(self, text: str, headers: dict[str, str]) -> list[float]:
        payload = {"model": self.model, "input": text}
        resp = requests.post(self.base_url, headers=headers, json=payload, timeout=60)
        _raise_for_status_with_details(resp)
        return self._extract_embedding(resp.json())

    def _embed_via_multimodal(self, text: str, headers: dict[str, str]) -> list[float]:
        payload = {
            "model": self.model,
            "input": [
                {
                    "type": "text",
                    "text": text,
                }
            ],
        }
        resp = requests.post(self.multimodal_url, headers=headers, json=payload, timeout=60)
        _raise_for_status_with_details(resp)
        return self._extract_embedding(resp.json())

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not self.api_key:
            raise ValueError("ARK_API_KEY is required for embedding.")

        vectors: list[list[float]] = []
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for txt in texts:
            if self._mode == "multimodal":
                vectors.append(self._embed_via_multimodal(txt, headers))
                continue
            if self._mode == "standard":
                vectors.append(self._embed_via_standard(txt, headers))
                continue

            try:
                vector = self._embed_via_standard(txt, headers)
                self._mode = "standard"
            except RuntimeError as exc:
                msg = str(exc).lower()
                need_multimodal_fallback = "/embeddings" in msg and (
                    "does not support this api" in msg
                    or "invalidparameter" in msg
                    or "notfound" in msg
                )
                if not need_multimodal_fallback:
                    raise
                vector = self._embed_via_multimodal(txt, headers)
                self._mode = "multimodal"
            vectors.append(vector)
        return vectors

    def embed_query(self, query: str) -> list[float]:
        vectors = self.embed_texts([query])
        return vectors[0]


class DoubaoChatClient:
    def __init__(self, settings: Settings):
        self.api_key = settings.ark_api_key
        self.model = settings.doubao_chat_model
        self.base_url = settings.ark_base_url.rstrip("/") + "/chat/completions"
        self.max_tokens = settings.rag_answer_max_tokens

    def answer_with_context(self, question: str, hits: list[dict]) -> str:
        if not self.api_key:
            raise ValueError("ARK_API_KEY is required for answer generation.")

        context_parts: list[str] = []
        for idx, hit in enumerate(hits, start=1):
            context_parts.append(
                "\n".join(
                    [
                        f"[{idx}] source_filename={hit.get('source_filename', '')}",
                        f"[{idx}] source_file={hit.get('source_file', '')}",
                        f"[{idx}] chunk_index={hit.get('chunk_index', -1)}",
                        f"[{idx}] score={hit.get('score', 0.0):.4f}",
                        f"[{idx}] text={hit.get('text', '')}",
                    ]
                )
            )
        context_text = "\n\n".join(context_parts)

        system_prompt = (
            "你是芯片手册解析助手。请基于给定检索片段回答问题。"
            "如果信息不足，请明确说“资料不足”。"
            "回答中涉及事实结论时，必须在句末标注来源编号，如 [1]、[2]。"
            "不要编造不存在的寄存器字段或数值。"
        )
        user_prompt = (
            f"问题：{question}\n\n"
            "请根据下列检索片段回答：\n"
            f"{context_text}\n\n"
            "输出要求：\n"
            "1) 先给“结论”段（简明）\n"
            "2) 再给“依据”段（按要点）\n"
            "3) 仅使用提供片段中的信息"
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": self.max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        resp = requests.post(self.base_url, headers=headers, json=payload, timeout=90)
        _raise_for_status_with_details(resp)
        data = resp.json()
        return str(data["choices"][0]["message"]["content"])


class QdrantIndexer:
    KEYWORD_SCAN_LIMIT = 3000
    KEYWORD_PAGE_SIZE = 256

    def __init__(self, settings: Settings):
        self.collection = settings.qdrant_collection
        self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)
        self.vector_size = settings.vector_size
        self.distance = Distance[settings.distance_metric.upper()]

    @staticmethod
    def _build_query_filter(source_filename: str | None = None) -> Filter | None:
        if not source_filename:
            return None
        return Filter(
            must=[
                FieldCondition(
                    key="source_filename",
                    match=MatchValue(value=source_filename),
                )
            ]
        )

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        lower = text.lower()
        tokens = re.findall(r"[a-z0-9_]+", lower)
        for chunk in re.findall(r"[\u4e00-\u9fff]+", lower):
            if len(chunk) == 1:
                tokens.append(chunk)
            else:
                tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
        return tokens

    @staticmethod
    def _hit_key(hit: dict) -> str:
        hit_id = str(hit.get("id", "")).strip()
        if hit_id:
            return hit_id
        source_file = str(hit.get("source_file", ""))
        chunk_index = int(hit.get("chunk_index", -1))
        return f"{source_file}::{chunk_index}"

    @staticmethod
    def _to_optional_int(value) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _to_hit(item, score: float) -> dict:
        payload = item.payload or {}
        hit_source_name = str(payload.get("source_filename", ""))
        chapter_raw = payload.get("chapter")
        chapter = str(chapter_raw).strip() if chapter_raw is not None else ""
        if chapter.lower() == "none":
            chapter = ""
        return {
            "id": str(item.id),
            "score": float(score),
            "text": str(payload.get("text", "")),
            "source_file": str(payload.get("source_file", "")),
            "source_filename": hit_source_name,
            "source_manual": str(payload.get("source_manual", hit_source_name)),
            "source_type": str(payload.get("source_type", "")),
            "chunk_index": int(payload.get("chunk_index", -1)),
            "page_start": QdrantIndexer._to_optional_int(payload.get("page_start")),
            "page_end": QdrantIndexer._to_optional_int(payload.get("page_end")),
            "chapter": chapter or None,
            "merged_output": str(payload.get("merged_output", "")),
        }

    def ensure_collection(self, vector_size: int | None = None, create_if_missing: bool = True) -> bool:
        existing = [c.name for c in self.client.get_collections().collections]
        if self.collection in existing:
            return True
        if not create_if_missing:
            return False
        target_size = int(vector_size or self.vector_size)
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=target_size, distance=self.distance),
        )
        return True

    def upsert_chunks(
        self,
        vectors: list[list[float]],
        chunks: list[str],
        metadata: dict,
        chunk_metadatas: list[dict] | None = None,
    ) -> None:
        if not vectors:
            return
        self.ensure_collection(vector_size=len(vectors[0]), create_if_missing=True)
        points: list[PointStruct] = []
        for i, (vector, chunk) in enumerate(zip(vectors, chunks)):
            payload = dict(metadata)
            if chunk_metadatas and i < len(chunk_metadatas):
                payload.update(chunk_metadatas[i] or {})
            payload.setdefault("chunk_index", i)
            payload["text"] = chunk
            points.append(PointStruct(id=str(uuid.uuid4()), vector=vector, payload=payload))
        self.client.upsert(collection_name=self.collection, points=points)

    def remove_source_chunks(self, source_filename: str) -> int:
        target = (source_filename or "").strip()
        if not target:
            return 0
        if not self.ensure_collection(create_if_missing=False):
            return 0

        query_filter = self._build_query_filter(target)
        if query_filter is None:
            return 0

        count_result = self.client.count(
            collection_name=self.collection,
            count_filter=query_filter,
            exact=True,
        )
        matched = int(getattr(count_result, "count", 0))
        if matched <= 0:
            return 0

        self.client.delete(
            collection_name=self.collection,
            points_selector=query_filter,
            wait=True,
        )
        return matched

    def list_sources(self, max_points: int = 2000) -> list[dict]:
        if not self.ensure_collection(create_if_missing=False):
            return []

        records = self._scan_keyword_candidates(source_filename=None, max_points=max(100, min(max_points, 10000)))
        grouped: dict[str, dict] = {}
        for item in records:
            payload = item.payload or {}
            source_file = str(payload.get("source_file", "")).strip()
            source_filename = str(payload.get("source_filename", "")).strip()
            merged_output = str(payload.get("merged_output", "")).strip()
            if not source_file and not source_filename:
                continue
            key = source_filename or source_file
            current = grouped.get(key)
            if current is None:
                current = {
                    "source_file": source_file,
                    "source_filename": source_filename,
                    "source_manual": str(payload.get("source_manual", source_filename or source_file)),
                    "source_type": str(payload.get("source_type", "")),
                    "merged_output": merged_output,
                    "chunk_count": 0,
                }
                grouped[key] = current
            else:
                if not current.get("source_file") and source_file:
                    current["source_file"] = source_file
                if not current.get("merged_output") and merged_output:
                    current["merged_output"] = merged_output
                    if source_file:
                        current["source_file"] = source_file
            current["chunk_count"] += 1

        items = list(grouped.values())
        items.sort(key=lambda item: (int(item.get("chunk_count", 0)), str(item.get("source_filename", ""))), reverse=True)
        return items

    def _search_semantic_chunks(
        self,
        query_vector: list[float],
        top_k: int = 5,
        source_filename: str | None = None,
    ) -> list[dict]:
        if not self.ensure_collection(create_if_missing=False):
            return []
        query_filter = self._build_query_filter(source_filename)
        results = self.client.search(
            collection_name=self.collection,
            query_vector=query_vector,
            limit=max(1, min(top_k, 30)),
            with_payload=True,
            query_filter=query_filter,
        )

        hits = [self._to_hit(item, float(item.score)) for item in results]
        return hits[:top_k]

    def _scan_keyword_candidates(
        self,
        source_filename: str | None,
        max_points: int,
    ) -> list:
        query_filter = self._build_query_filter(source_filename)
        records = []
        offset = None

        while len(records) < max_points:
            page_size = min(self.KEYWORD_PAGE_SIZE, max_points - len(records))
            page, offset = self.client.scroll(
                collection_name=self.collection,
                scroll_filter=query_filter,
                limit=page_size,
                with_payload=True,
            )
            if not page:
                break
            records.extend(page)
            if offset is None:
                break
        return records

    def _search_keyword_chunks(
        self,
        query_text: str,
        top_k: int = 5,
        source_filename: str | None = None,
        max_points: int = 500,
    ) -> list[dict]:
        if not self.ensure_collection(create_if_missing=False):
            return []

        query_tokens = self._tokenize(query_text)
        if not query_tokens:
            return []
        unique_query_tokens = list(dict.fromkeys(query_tokens))

        candidates = self._scan_keyword_candidates(
            source_filename=source_filename,
            max_points=max(50, min(max_points, self.KEYWORD_SCAN_LIMIT)),
        )
        docs = []
        for item in candidates:
            hit = self._to_hit(item, 0.0)
            text = hit.get("text", "")
            if not text:
                continue
            tokens = self._tokenize(text)
            if not tokens:
                continue
            token_counter = Counter(tokens)
            token_set = set(token_counter.keys())
            docs.append((hit, token_counter, token_set, len(tokens), text.lower()))

        if not docs:
            return []

        doc_count = len(docs)
        avg_len = sum(doc_len for _, _, _, doc_len, _ in docs) / max(1, doc_count)
        df: dict[str, int] = {}
        for token in unique_query_tokens:
            df[token] = sum(1 for _, _, token_set, _, _ in docs if token in token_set)

        k1 = 1.5
        b = 0.75
        query_lower = query_text.lower()
        ranked: list[dict] = []
        for hit, token_counter, token_set, doc_len, text_lower in docs:
            score = 0.0
            for token in unique_query_tokens:
                tf = token_counter.get(token, 0)
                if tf <= 0:
                    continue
                dfi = max(1, df.get(token, 0))
                idf = math.log(1.0 + (doc_count - dfi + 0.5) / (dfi + 0.5))
                denom = tf + k1 * (1.0 - b + b * (doc_len / max(avg_len, 1e-6)))
                score += idf * ((tf * (k1 + 1.0)) / max(denom, 1e-6))

            token_hit_count = sum(1 for token in unique_query_tokens if token in token_set)
            if token_hit_count == len(unique_query_tokens):
                score += 0.2
            if query_lower and query_lower in text_lower:
                score += 0.5

            if score > 0:
                hit["score"] = float(score)
                ranked.append(hit)

        ranked.sort(key=lambda item: item["score"], reverse=True)
        return ranked[:top_k]

    @staticmethod
    def _normalize_scores(hits: list[dict]) -> dict[str, float]:
        if not hits:
            return {}
        scores = [float(hit.get("score", 0.0)) for hit in hits]
        max_score = max(scores)
        min_score = min(scores)
        if math.isclose(max_score, min_score):
            return {QdrantIndexer._hit_key(hit): 1.0 for hit in hits}
        return {
            QdrantIndexer._hit_key(hit): (float(hit.get("score", 0.0)) - min_score) / (max_score - min_score)
            for hit in hits
        }

    def _search_hybrid_chunks(
        self,
        query_text: str,
        query_vector: list[float],
        top_k: int = 5,
        source_filename: str | None = None,
    ) -> list[dict]:
        candidate_size = max(top_k * 4, 20)
        semantic_hits = self._search_semantic_chunks(
            query_vector=query_vector,
            top_k=candidate_size,
            source_filename=source_filename,
        )
        keyword_hits = self._search_keyword_chunks(
            query_text=query_text,
            top_k=candidate_size,
            source_filename=source_filename,
            max_points=max(top_k * 120, 400),
        )

        semantic_norm = self._normalize_scores(semantic_hits)
        keyword_norm = self._normalize_scores(keyword_hits)

        merged: dict[str, dict] = {}
        for hit in semantic_hits:
            key = self._hit_key(hit)
            merged[key] = dict(hit)
            merged[key]["score"] = 0.65 * semantic_norm.get(key, 0.0)

        for hit in keyword_hits:
            key = self._hit_key(hit)
            if key not in merged:
                merged[key] = dict(hit)
                merged[key]["score"] = 0.0
            merged[key]["score"] += 0.35 * keyword_norm.get(key, 0.0)

        results = list(merged.values())
        results.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        return results[:top_k]

    def search_chunks(
        self,
        query_vector: list[float] | None,
        query_text: str,
        top_k: int = 5,
        source_filename: str | None = None,
        retrieval_mode: str = "semantic",
    ) -> list[dict]:
        mode = (retrieval_mode or "semantic").lower()
        if mode == "keyword":
            return self._search_keyword_chunks(
                query_text=query_text,
                top_k=top_k,
                source_filename=source_filename,
                max_points=max(top_k * 120, 400),
            )
        if mode == "hybrid":
            if query_vector is None:
                raise ValueError("query_vector is required for hybrid retrieval.")
            return self._search_hybrid_chunks(
                query_text=query_text,
                query_vector=query_vector,
                top_k=top_k,
                source_filename=source_filename,
            )
        if mode == "semantic":
            if query_vector is None:
                raise ValueError("query_vector is required for semantic retrieval.")
            return self._search_semantic_chunks(
                query_vector=query_vector,
                top_k=top_k,
                source_filename=source_filename,
            )
        raise ValueError(f"Unsupported retrieval mode: {retrieval_mode}")
