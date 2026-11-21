"""图片原生多模态向量通道。

与"视觉描述文本向量"严格区分：
- 视觉描述文本向量：走既有文本 embedding 与文本 collection（vector_space=text）；
- 原生图片向量：走多模态 embedding 模型与独立 image collection（vector_space=image）。

模型/供应商/维度均可配置；调用失败时返回 None，由上层降级为
"只索引视觉描述文本"，不阻断正文入库。
"""

from __future__ import annotations

import base64
import mimetypes
import uuid
from pathlib import Path

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
        raise RuntimeError("HTTP %s calling %s: %s" % (resp.status_code, resp.url, detail))
    resp.raise_for_status()


class ImageEmbeddingClient:
    """多模态 embedding 客户端（文本与图像同空间）。"""

    def __init__(self, settings: Settings):
        self.api_key = settings.ark_api_key
        self.model = settings.wewe_image_embedding_model
        self.base_url = settings.ark_base_url.rstrip("/") + "/embeddings/multimodal"
        self.timeout = settings.vision_timeout_seconds

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
        raise RuntimeError("Unexpected multimodal embedding response format.")

    def _embed(self, inputs: list[dict]) -> list[float]:
        if not self.api_key:
            raise RuntimeError("ARK_API_KEY is required for image embedding.")
        payload = {"model": self.model, "input": inputs}
        headers = {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"}
        resp = requests.post(self.base_url, headers=headers, json=payload, timeout=self.timeout)
        _raise_for_status_with_details(resp)
        return self._extract_embedding(resp.json())

    def embed_text(self, text: str) -> list[float] | None:
        try:
            return self._embed([{"type": "text", "text": text}])
        except Exception:
            return None

    def embed_image_file(self, image_path: str) -> list[float] | None:
        path = Path(image_path)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        mime, _ = mimetypes.guess_type(str(path))
        mime = mime or "image/png"
        payload = "data:" + mime + ";base64," + base64.b64encode(data).decode("utf-8")
        try:
            return self._embed([{"type": "image_url", "image_url": {"url": payload}}])
        except Exception:
            return None


class WeWeVectorIndex:
    """wewe-rss 文章的文本空间与图片空间索引。"""

    def __init__(self, settings: Settings):
        self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)
        self.text_collection = settings.qdrant_collection
        self.image_collection = settings.wewe_image_collection
        self.text_vector_size = int(settings.vector_size)
        self.image_vector_size = int(settings.wewe_image_vector_size)
        self.distance = Distance[settings.distance_metric.upper()]

    # -- collection helpers ---------------------------------------
    def _ensure_collection(self, name: str, size: int) -> bool:
        existing = [c.name for c in self.client.get_collections().collections]
        if name in existing:
            return True
        self.client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=size, distance=self.distance),
        )
        return True

    def ensure_text_collection(self) -> bool:
        return self._ensure_collection(self.text_collection, self.text_vector_size)

    def ensure_image_collection(self) -> bool:
        return self._ensure_collection(self.image_collection, self.image_vector_size)

    @staticmethod
    def _point_id(space: str, user_id: int, article_id: int, key: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "wewe:%s:%s:%s:%s" % (space, user_id, article_id, key)))

    @staticmethod
    def _filter(user_id: int, knowledge_base_id: int | None = None, article_id: int | None = None) -> Filter:
        conditions = [FieldCondition(key="owner_user_id", match=MatchValue(value=int(user_id)))]
        if knowledge_base_id is not None:
            conditions.append(FieldCondition(key="knowledge_base_id", match=MatchValue(value=int(knowledge_base_id))))
        if article_id is not None:
            conditions.append(FieldCondition(key="article_id", match=MatchValue(value=int(article_id))))
        return Filter(must=conditions)

    # -- writes ----------------------------------------------------
    def upsert_text_chunks(
        self,
        user_id: int,
        knowledge_base_id: int,
        article_id: int,
        chunks: list[str],
        vectors: list[list[float]],
        metadata: dict,
        key_prefix: str = "c",
    ) -> int:
        if not chunks or not vectors:
            return 0
        self.ensure_text_collection()
        points: list[PointStruct] = []
        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            payload = dict(metadata)
            payload.update(
                {
                    "owner_user_id": int(user_id),
                    "knowledge_base_id": int(knowledge_base_id),
                    "article_id": int(article_id),
                    "chunk_index": index if key_prefix == "c" else -1,
                    "vector_space": "text",
                    "text": chunk,
                }
            )
            points.append(
                PointStruct(
                    id=self._point_id("text", user_id, article_id, key_prefix + str(index)),
                    vector=vector,
                    payload=payload,
                )
            )
        self.client.upsert(collection_name=self.text_collection, points=points)
        return len(points)

    def upsert_image_vectors(
        self,
        user_id: int,
        knowledge_base_id: int,
        article_id: int,
        items: list[dict],
    ) -> int:
        """items: [{image_id, vector, image_url, description, ...metadata}]"""

        usable = [item for item in items if item.get("vector")]
        if not usable:
            return 0
        self.ensure_image_collection()
        points: list[PointStruct] = []
        for item in usable:
            image_id = int(item["image_id"])
            payload = {
                "owner_user_id": int(user_id),
                "knowledge_base_id": int(knowledge_base_id),
                "article_id": int(article_id),
                "image_id": image_id,
                "vector_space": "image",
                "chunk_type": "image_native",
                "text": str(item.get("description") or ""),
                "description": str(item.get("description") or ""),
                "image_url": str(item.get("image_url") or ""),
                "local_path": str(item.get("local_path") or ""),
                "source_id": int(item.get("source_id") or 0),
                "source_type": str(item.get("source_type") or ""),
                "source_name": str(item.get("source_name") or ""),
                "source_filename": str(item.get("source_filename") or ""),
                "source_file": str(item.get("source_file") or ""),
                "article_title": str(item.get("article_title") or ""),
                "article_url": str(item.get("article_url") or ""),
                "chunk_index": 0,
            }
            points.append(
                PointStruct(
                    id=self._point_id("image", user_id, article_id, str(image_id)),
                    vector=list(item["vector"]),
                    payload=payload,
                )
            )
        self.client.upsert(collection_name=self.image_collection, points=points)
        return len(points)

    # -- deletes ---------------------------------------------------
    def delete_article(self, user_id: int, article_id: int) -> dict:
        """删除文章在两个向量空间中的全部点位（幂等）。"""

        removed = {"text": 0, "image": 0}
        query_filter = self._filter(user_id, article_id=article_id)
        existing = [c.name for c in self.client.get_collections().collections]
        for space, collection in (("text", self.text_collection), ("image", self.image_collection)):
            if collection not in existing:
                continue
            try:
                self.client.delete(
                    collection_name=collection,
                    points_selector=query_filter,
                    wait=True,
                )
                removed[space] = 1
            except Exception:
                removed[space] = 0
        return removed

    def count_article_points(self, user_id: int, article_id: int) -> dict:
        counts = {"text": 0, "image": 0}
        query_filter = self._filter(user_id, article_id=article_id)
        existing = [c.name for c in self.client.get_collections().collections]
        for space, collection in (("text", self.text_collection), ("image", self.image_collection)):
            if collection not in existing:
                continue
            try:
                result = self.client.count(collection_name=collection, count_filter=query_filter, exact=True)
                counts[space] = int(getattr(result, "count", 0))
            except Exception:
                counts[space] = 0
        return counts

    # -- search ----------------------------------------------------
    def search_images(
        self,
        query_vector: list[float],
        user_id: int,
        knowledge_base_id: int | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        existing = [c.name for c in self.client.get_collections().collections]
        if self.image_collection not in existing:
            return []
        results = self.client.search(
            collection_name=self.image_collection,
            query_vector=query_vector,
            limit=max(1, min(int(top_k), 30)),
            with_payload=True,
            query_filter=self._filter(user_id, knowledge_base_id),
        )
        hits: list[dict] = []
        for item in results:
            payload = item.payload or {}
            hits.append(
                {
                    "id": str(item.id),
                    "score": float(item.score),
                    "vector_space": "image",
                    "chunk_type": "image_native",
                    "article_id": payload.get("article_id"),
                    "image_id": payload.get("image_id"),
                    "image_url": payload.get("image_url"),
                    "local_path": payload.get("local_path"),
                    "description": payload.get("description"),
                    "article_title": payload.get("article_title"),
                    "article_url": payload.get("article_url"),
                    "source_id": payload.get("source_id"),
                    "source_type": payload.get("source_type"),
                    "source_name": payload.get("source_name"),
                    "knowledge_base_id": payload.get("knowledge_base_id"),
                    "owner_user_id": payload.get("owner_user_id"),
                }
            )
        return hits

