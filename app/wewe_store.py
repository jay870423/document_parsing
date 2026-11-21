"""wewe-rss 集成的关系型元数据存储（SQLite）。

表：
- wewe_sources        订阅源（公众号 / 网站）
- wewe_articles       文章（含清洗后的正文与索引状态）
- wewe_images         文章图片资产（原图落盘 + 视觉描述）
- wewe_collect_tasks  采集任务与结果

所有查询都带 user_id 过滤，跨用户隔离由 SQL 层保证。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WeWeStore:
    def __init__(self, db_path: str = "data/app.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS wewe_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    knowledge_base_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    upstream_feed_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    intro TEXT NOT NULL DEFAULT '',
                    cover_url TEXT NOT NULL DEFAULT '',
                    target_url TEXT NOT NULL DEFAULT '',
                    subscribed INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'active',
                    last_sync_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (user_id, source_type, upstream_feed_id, target_url)
                );

                CREATE TABLE IF NOT EXISTS wewe_articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    source_id INTEGER NOT NULL REFERENCES wewe_sources(id) ON DELETE CASCADE,
                    knowledge_base_id INTEGER NOT NULL,
                    upstream_article_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    cover_url TEXT NOT NULL DEFAULT '',
                    content_html TEXT NOT NULL DEFAULT '',
                    text_content TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    image_count INTEGER NOT NULL DEFAULT 0,
                    indexed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (user_id, upstream_article_id)
                );

                CREATE TABLE IF NOT EXISTS wewe_images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id INTEGER NOT NULL REFERENCES wewe_articles(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    local_path TEXT NOT NULL DEFAULT '',
                    mime_type TEXT NOT NULL DEFAULT '',
                    byte_size INTEGER NOT NULL DEFAULT 0,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (article_id, url)
                );

                CREATE TABLE IF NOT EXISTS wewe_collect_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    source_id INTEGER,
                    kind TEXT NOT NULL DEFAULT 'manual',
                    status TEXT NOT NULL DEFAULT 'running',
                    article_count INTEGER NOT NULL DEFAULT 0,
                    image_count INTEGER NOT NULL DEFAULT 0,
                    indexed_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_wewe_sources_user ON wewe_sources(user_id, knowledge_base_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_wewe_articles_user ON wewe_articles(user_id, source_id, published_at);
                CREATE INDEX IF NOT EXISTS idx_wewe_articles_kb ON wewe_articles(user_id, knowledge_base_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_wewe_images_article ON wewe_images(article_id);
                CREATE INDEX IF NOT EXISTS idx_wewe_tasks_user ON wewe_collect_tasks(user_id, created_at);
                """
            )

    # -- sources --------------------------------------------------
    def add_source(
        self,
        user_id: int,
        knowledge_base_id: int,
        source_type: str,
        name: str,
        upstream_feed_id: str = "",
        intro: str = "",
        cover_url: str = "",
        target_url: str = "",
        subscribed: bool = True,
    ) -> int:
        with self.connect() as conn:
            now = _now()
            conn.execute(
                """
                INSERT INTO wewe_sources (
                    user_id, knowledge_base_id, source_type, upstream_feed_id, name,
                    intro, cover_url, target_url, subscribed, status, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)
                ON CONFLICT (user_id, source_type, upstream_feed_id, target_url) DO UPDATE SET
                    name = excluded.name,
                    intro = excluded.intro,
                    cover_url = excluded.cover_url,
                    knowledge_base_id = excluded.knowledge_base_id,
                    updated_at = excluded.updated_at
                """,
                (
                    int(user_id),
                    int(knowledge_base_id),
                    source_type,
                    upstream_feed_id or "",
                    name or "",
                    intro or "",
                    cover_url or "",
                    target_url or "",
                    1 if subscribed else 0,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT id FROM wewe_sources
                WHERE user_id = ? AND source_type = ? AND upstream_feed_id = ? AND target_url = ?
                """,
                (int(user_id), source_type, upstream_feed_id or "", target_url or ""),
            ).fetchone()
        return int(row["id"])

    def list_sources(
        self,
        user_id: int,
        knowledge_base_id: int | None = None,
        source_type: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM wewe_sources WHERE user_id = ?"
        params: list = [int(user_id)]
        if knowledge_base_id is not None:
            sql += " AND knowledge_base_id = ?"
            params.append(int(knowledge_base_id))
        if source_type:
            sql += " AND source_type = ?"
            params.append(source_type)
        sql += " ORDER BY id DESC"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def get_source(self, user_id: int, source_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM wewe_sources WHERE id = ? AND user_id = ?",
                (int(source_id), int(user_id)),
            ).fetchone()
        return dict(row) if row else None

    def update_source(self, user_id: int, source_id: int, **fields) -> bool:
        allowed = {"subscribed", "status", "name", "intro", "last_sync_at", "last_error", "knowledge_base_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        if "subscribed" in updates:
            updates["subscribed"] = 1 if updates["subscribed"] else 0
        assignments = ", ".join("%s = ?" % key for key in updates)
        params = list(updates.values()) + [_now(), int(source_id), int(user_id)]
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE wewe_sources SET %s, updated_at = ? WHERE id = ? AND user_id = ?" % assignments,
                params,
            )
        return cur.rowcount > 0

    def delete_source(self, user_id: int, source_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM wewe_sources WHERE id = ? AND user_id = ?",
                (int(source_id), int(user_id)),
            )
        return cur.rowcount > 0

    def list_subscribed_sources(self, knowledge_base_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM wewe_sources WHERE subscribed = 1 AND status = 'active'"
        params: list = []
        if knowledge_base_id is not None:
            sql += " AND knowledge_base_id = ?"
            params.append(int(knowledge_base_id))
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # -- articles -------------------------------------------------
    def upsert_article(
        self,
        user_id: int,
        source_id: int,
        knowledge_base_id: int,
        upstream_article_id: str,
        title: str = "",
        url: str = "",
        author: str = "",
        published_at: str = "",
        cover_url: str = "",
        content_html: str = "",
        text_content: str = "",
        status: str = "pending",
        error: str = "",
    ) -> tuple[int, bool]:
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM wewe_articles WHERE user_id = ? AND upstream_article_id = ?",
                (int(user_id), upstream_article_id),
            ).fetchone()
            now = _now()
            if existing:
                conn.execute(
                    """
                    UPDATE wewe_articles SET
                        source_id = ?, knowledge_base_id = ?, title = ?, url = ?, author = ?,
                        published_at = ?, cover_url = ?, content_html = ?, text_content = ?,
                        status = ?, error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(source_id),
                        int(knowledge_base_id),
                        title,
                        url,
                        author,
                        published_at,
                        cover_url,
                        content_html,
                        text_content,
                        status,
                        error,
                        now,
                        int(existing["id"]),
                    ),
                )
                return int(existing["id"]), False
            cur = conn.execute(
                """
                INSERT INTO wewe_articles (
                    user_id, source_id, knowledge_base_id, upstream_article_id, title, url, author,
                    published_at, cover_url, content_html, text_content, status, error, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(user_id),
                    int(source_id),
                    int(knowledge_base_id),
                    upstream_article_id,
                    title,
                    url,
                    author,
                    published_at,
                    cover_url,
                    content_html,
                    text_content,
                    status,
                    error,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid), True

    def update_article(
        self,
        article_id: int,
        status: str | None = None,
        error: str | None = None,
        chunk_count: int | None = None,
        image_count: int | None = None,
        indexed: bool | None = None,
        text_content: str | None = None,
        content_html: str | None = None,
    ) -> bool:
        fields: dict = {}
        if status is not None:
            fields["status"] = status
        if error is not None:
            fields["error"] = error
        if chunk_count is not None:
            fields["chunk_count"] = int(chunk_count)
        if image_count is not None:
            fields["image_count"] = int(image_count)
        if text_content is not None:
            fields["text_content"] = text_content
        if content_html is not None:
            fields["content_html"] = content_html
        if indexed is not None:
            fields["indexed_at"] = _now() if indexed else None
        if not fields:
            return False
        assignments = ", ".join("%s = ?" % key for key in fields)
        params = list(fields.values()) + [_now(), int(article_id)]
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE wewe_articles SET %s, updated_at = ? WHERE id = ?" % assignments,
                params,
            )
        return cur.rowcount > 0

    def get_article(self, user_id: int, article_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM wewe_articles WHERE id = ? AND user_id = ?",
                (int(article_id), int(user_id)),
            ).fetchone()
        return dict(row) if row else None

    def list_articles(
        self,
        user_id: int,
        knowledge_base_id: int | None = None,
        source_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        sql = "SELECT * FROM wewe_articles WHERE user_id = ?"
        params: list = [int(user_id)]
        if knowledge_base_id is not None:
            sql += " AND knowledge_base_id = ?"
            params.append(int(knowledge_base_id))
        if source_id is not None:
            sql += " AND source_id = ?"
            params.append(int(source_id))
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY COALESCE(published_at, created_at) DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def delete_article(self, user_id: int, article_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM wewe_articles WHERE id = ? AND user_id = ?",
                (int(article_id), int(user_id)),
            )
        return cur.rowcount > 0

    # -- images ---------------------------------------------------
    def upsert_image(
        self,
        article_id: int,
        user_id: int,
        url: str,
        local_path: str = "",
        mime_type: str = "",
        byte_size: int = 0,
        description: str = "",
        status: str = "pending",
        error: str = "",
    ) -> int:
        with self.connect() as conn:
            now = _now()
            conn.execute(
                """
                INSERT INTO wewe_images (
                    article_id, user_id, url, local_path, mime_type, byte_size,
                    description, status, error, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (article_id, url) DO UPDATE SET
                    local_path = excluded.local_path,
                    mime_type = excluded.mime_type,
                    byte_size = excluded.byte_size,
                    description = excluded.description,
                    status = excluded.status,
                    error = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    int(article_id),
                    int(user_id),
                    url,
                    local_path,
                    mime_type,
                    int(byte_size),
                    description,
                    status,
                    error,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM wewe_images WHERE article_id = ? AND url = ?",
                (int(article_id), url),
            ).fetchone()
        return int(row["id"])

    def update_image(self, image_id: int, description: str | None = None, status: str | None = None, error: str | None = None) -> bool:
        fields: dict = {}
        if description is not None:
            fields["description"] = description
        if status is not None:
            fields["status"] = status
        if error is not None:
            fields["error"] = error
        if not fields:
            return False
        assignments = ", ".join("%s = ?" % key for key in fields)
        params = list(fields.values()) + [_now(), int(image_id)]
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE wewe_images SET %s, updated_at = ? WHERE id = ?" % assignments,
                params,
            )
        return cur.rowcount > 0

    def list_images(self, article_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wewe_images WHERE article_id = ? ORDER BY id ASC",
                (int(article_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- tasks ----------------------------------------------------
    def create_task(self, user_id: int, source_id: int | None, kind: str = "manual") -> int:
        now = _now()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO wewe_collect_tasks (user_id, source_id, kind, status, started_at, created_at)
                VALUES (?,?,?,'running',?,?)
                """,
                (int(user_id), source_id if source_id is not None else None, kind, now, now),
            )
        return int(cur.lastrowid)

    def finish_task(
        self,
        task_id: int,
        status: str,
        article_count: int = 0,
        image_count: int = 0,
        indexed_count: int = 0,
        error: str = "",
    ) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE wewe_collect_tasks
                SET status = ?, article_count = ?, image_count = ?, indexed_count = ?, error = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, int(article_count), int(image_count), int(indexed_count), error, _now(), int(task_id)),
            )
        return cur.rowcount > 0

    def list_tasks(self, user_id: int, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wewe_collect_tasks WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (int(user_id), max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]
