from __future__ import annotations

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.rag import QdrantIndexer
from app.wewe_images import WeWeVectorIndex


def _payload(article_id=None, version=None, user_id=1, knowledge_base_id=1):
    payload = {"owner_user_id": user_id, "knowledge_base_id": knowledge_base_id}
    if article_id is not None:
        payload["article_id"] = article_id
    if version is not None:
        payload["vector_version"] = version
    return payload


def test_qdrant_hides_staging_stale_and_unpublished_article_points():
    client = QdrantClient(":memory:")
    client.create_collection("versions", vectors_config=VectorParams(size=1, distance=Distance.COSINE))
    points = [
        PointStruct(id=1, vector=[1.0], payload=_payload()),  # ordinary document
        PointStruct(id=2, vector=[1.0], payload=_payload(11, "v2")),
        PointStruct(id=3, vector=[1.0], payload=_payload(12)),  # published legacy
        PointStruct(id=4, vector=[1.0], payload=_payload(11, "v1")),  # stale
        PointStruct(id=5, vector=[1.0], payload=_payload(11, "staging")),  # uncommitted
        PointStruct(id=6, vector=[1.0], payload=_payload(13, "v9")),  # not indexed in DB
        PointStruct(id=7, vector=[1.0], payload=_payload(user_id=2)),
        PointStruct(id=8, vector=[1.0], payload=_payload(11, "v2", knowledge_base_id=2)),
    ]
    client.upsert("versions", points=points, wait=True)

    query_filter = QdrantIndexer._build_query_filter(
        user_id=1, knowledge_base_id=1, article_versions={11: "v2", 12: None}
    )
    records, _ = client.scroll("versions", scroll_filter=query_filter, limit=100)

    assert {record.id for record in records} == {1, 2, 3}

    # An empty DB publication map fail-closes every versioned article point while
    # leaving ordinary documents available.
    empty_filter = QdrantIndexer._build_query_filter(
        user_id=1, knowledge_base_id=1, article_versions={}
    )
    records, _ = client.scroll("versions", scroll_filter=empty_filter, limit=100)
    assert {record.id for record in records} == {1}


def test_versioned_point_ids_do_not_collide_and_legacy_ids_stay_stable():
    legacy = WeWeVectorIndex._point_id("text", 1, 2, "chunk-0")
    assert WeWeVectorIndex._point_id("text", 1, 2, "chunk-0") == legacy
    first = WeWeVectorIndex._point_id("text", 1, 2, "chunk-0", "v1")
    second = WeWeVectorIndex._point_id("text", 1, 2, "chunk-0", "v2")
    assert len({legacy, first, second}) == 3
    assert first == WeWeVectorIndex._point_id("text", 1, 2, "chunk-0", "v1")
