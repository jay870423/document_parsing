"""Qdrant visibility rules for atomically published WeWe article versions."""

from __future__ import annotations

from qdrant_client.models import (
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchAny,
    PayloadField,
)


def with_published_article_versions(
    base: Filter,
    article_versions: dict[int, str | None] | None,
) -> Filter:
    """Expose document points and only the DB-published generation of each article.

    Version UUIDs are globally unique, so one MatchAny can select all current
    non-legacy generations without a large article/version OR tree. Legacy WeWe
    points have no vector_version and are matched by their published article ids.
    """
    if article_versions is None:
        return base
    current_versions = sorted({str(v) for v in article_versions.values() if v})
    legacy_ids = sorted(int(article_id) for article_id, version in article_versions.items() if not version)
    should = [
        # Ordinary document chunks do not have an article_id and remain visible.
        Filter(must=[IsEmptyCondition(is_empty=PayloadField(key="article_id"))])
    ]
    if current_versions:
        should.append(Filter(must=[
            FieldCondition(key="vector_version", match=MatchAny(any=current_versions))
        ]))
    if legacy_ids:
        should.append(Filter(must=[
            FieldCondition(key="article_id", match=MatchAny(any=legacy_ids)),
            IsEmptyCondition(is_empty=PayloadField(key="vector_version")),
        ]))
    return Filter(
        must=list(base.must or []),
        should=should,
        must_not=list(base.must_not or []),
    )
