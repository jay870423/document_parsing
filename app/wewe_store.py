"""WeWe 来源、文章、图片和采集任务的 MySQL 元数据存储。"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from app.billing import CallReservation, WEWE_TASK_RESOURCE_TYPE
from app.config import Settings, get_settings
from app.mysql import Connection, IntegrityError, OperationalError, connect as mysql_connect
from app.mysql_schema import BILLING_SCHEMA, WEWE_SCHEMA


class CollectionAlreadyRunning(RuntimeError):
    """The source already has a committed running collection task."""


class CollectionLeaseLost(RuntimeError):
    """The current worker may no longer publish this collection task."""


class TaskLease:
    def __init__(self, task_id: int):
        self.task_id = int(task_id)
        self.lost = threading.Event()


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _precise_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def source_identity(user_id: int, source_type: str, upstream_feed_id: str, target_url: str) -> str:
    raw = f"{int(user_id)}\0{source_type}\0{upstream_feed_id or ''}\0{target_url or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class WeWeStore:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
    ):
        self.settings = settings or get_settings()
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex}"
        self.lease_seconds = max(
            0.1,
            float(lease_seconds if lease_seconds is not None else self.settings.wewe_task_lease_seconds),
        )
        self._lease_events: dict[int, threading.Event] = {}
        self._lease_events_lock = threading.Lock()
        self.init_db()

    def _lease_deadline(self) -> str:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)
        return deadline.isoformat(timespec="milliseconds")

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with mysql_connect(self.settings) as conn:
            yield conn

    def init_db(self) -> None:
        with self.connect() as conn:
            # Billing DDL is idempotent and guarantees referenced tables exist.
            for statement in (*BILLING_SCHEMA, *WEWE_SCHEMA):
                conn.execute(statement)
            # Existing local sources already have authoritative user ownership. Backfill only
            # that derived feed mapping; upstream accounts intentionally require a fresh QR bind.
            conn.execute(
                """INSERT IGNORE INTO wewe_feed_owners (user_id, upstream_feed_id, created_at)
                   SELECT user_id, upstream_feed_id, ? FROM wewe_sources
                   WHERE source_type = 'mp' AND upstream_feed_id <> ''""",
                (_now(),),
            )
            for column, definition in (
                ("worker_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
                ("heartbeat_at", "VARCHAR(40) NULL"),
                ("lease_expires_at", "VARCHAR(40) NULL"),
            ):
                self._ensure_task_column(conn, column, definition)
            self._ensure_article_column(conn, "vector_version", "VARCHAR(64) NULL")
            self._ensure_image_column(conn, "vector_version", "VARCHAR(64) NULL")

    def _ensure_image_column(self, conn: Connection, column: str, definition: str) -> None:
        if column != "vector_version":
            raise ValueError("unsupported image schema column")
        row = conn.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_schema = ? AND table_name = 'wewe_images' AND column_name = ?""",
            (self.settings.mysql_database, column),
        ).fetchone()
        if row:
            return
        try:
            conn.execute(f"ALTER TABLE `wewe_images` ADD COLUMN `{column}` {definition}")
        except OperationalError as exc:
            if not exc.args or int(exc.args[0]) != 1060:
                raise

    def _ensure_article_column(self, conn: Connection, column: str, definition: str) -> None:
        if column != "vector_version":
            raise ValueError("unsupported article schema column")
        row = conn.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_schema = ? AND table_name = 'wewe_articles' AND column_name = ?""",
            (self.settings.mysql_database, column),
        ).fetchone()
        if row:
            return
        try:
            conn.execute(f"ALTER TABLE `wewe_articles` ADD COLUMN `{column}` {definition}")
        except OperationalError as exc:
            if not exc.args or int(exc.args[0]) != 1060:
                raise

    def _ensure_task_column(self, conn: Connection, column: str, definition: str) -> None:
        """Add lease columns to an existing installation, safely across workers."""
        if column not in {"worker_id", "heartbeat_at", "lease_expires_at"}:
            raise ValueError("unsupported task schema column")
        row = conn.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_schema = ? AND table_name = 'wewe_collect_tasks' AND column_name = ?""",
            (self.settings.mysql_database, column),
        ).fetchone()
        if row:
            return
        try:
            conn.execute(f"ALTER TABLE `wewe_collect_tasks` ADD COLUMN `{column}` {definition}")
        except OperationalError as exc:
            # Another worker may complete the same additive migration concurrently.
            if not exc.args or int(exc.args[0]) != 1060:
                raise

    def bind_upstream_account(self, user_id: int, account_id: str) -> None:
        account_id = str(account_id or "").strip()
        if not account_id or len(account_id) > 255:
            raise ValueError("invalid upstream account id")
        with self.connect() as conn:
            conn.execute(
                "INSERT IGNORE INTO wewe_account_owners (user_id, upstream_account_id, created_at) VALUES (?,?,?)",
                (int(user_id), account_id, _now()),
            )

    def list_upstream_account_ids(self, user_id: int) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT upstream_account_id FROM wewe_account_owners WHERE user_id = ?",
                (int(user_id),),
            ).fetchall()
        return {str(row["upstream_account_id"]) for row in rows}

    def bind_upstream_feed(self, user_id: int, feed_id: str) -> None:
        feed_id = str(feed_id or "").strip()
        if not feed_id or len(feed_id) > 255:
            raise ValueError("invalid upstream feed id")
        with self.connect() as conn:
            conn.execute(
                "INSERT IGNORE INTO wewe_feed_owners (user_id, upstream_feed_id, created_at) VALUES (?,?,?)",
                (int(user_id), feed_id, _now()),
            )

    def list_upstream_feed_ids(self, user_id: int) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT upstream_feed_id FROM wewe_feed_owners WHERE user_id = ?",
                (int(user_id),),
            ).fetchall()
        return {str(row["upstream_feed_id"]) for row in rows}

    def owns_upstream_feed(self, user_id: int, feed_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM wewe_feed_owners WHERE user_id = ? AND upstream_feed_id = ?",
                (int(user_id), str(feed_id or "")),
            ).fetchone()
        return bool(row)

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
        identity = source_identity(user_id, source_type, upstream_feed_id, target_url)
        with self.connect() as conn:
            now = _now()
            conn.execute(
                """
                INSERT INTO wewe_sources (
                    user_id, knowledge_base_id, source_type, upstream_feed_id, identity_hash, name,
                    intro, cover_url, target_url, subscribed, status, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?)
                ON DUPLICATE KEY UPDATE
                    name=VALUES(name), intro=VALUES(intro), cover_url=VALUES(cover_url),
                    knowledge_base_id=VALUES(knowledge_base_id), updated_at=VALUES(updated_at)
                """,
                (
                    int(user_id), int(knowledge_base_id), source_type, upstream_feed_id or "", identity,
                    name or "", intro or "", cover_url or "", target_url or "", 1 if subscribed else 0, now, now,
                ),
            )
            row = conn.execute("SELECT id FROM wewe_sources WHERE identity_hash=?", (identity,)).fetchone()
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

    def update_source(
        self, user_id: int, source_id: int, *, task_id: int | None = None, **fields
    ) -> bool:
        allowed = {"subscribed", "status", "name", "intro", "last_sync_at", "last_error", "knowledge_base_id"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        if "subscribed" in updates:
            updates["subscribed"] = 1 if updates["subscribed"] else 0
        assignments = ", ".join("%s = ?" % key for key in updates)
        params = list(updates.values()) + [_now(), int(source_id), int(user_id)]
        with self.connect() as conn:
            if task_id is not None:
                self._assert_task_write(conn, task_id, for_update=True)
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
        task_id: int | None = None,
    ) -> tuple[int, bool]:
        with self.connect() as conn:
            if task_id is not None:
                self._assert_task_write(conn, task_id, for_update=True)
            existing = conn.execute(
                "SELECT id FROM wewe_articles WHERE user_id = ? AND source_id = ? AND upstream_article_id = ?",
                (int(user_id), int(source_id), upstream_article_id),
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
        vector_version: str | None = None,
        indexed: bool | None = None,
        text_content: str | None = None,
        content_html: str | None = None,
        title: str | None = None,
        url: str | None = None,
        author: str | None = None,
        published_at: str | None = None,
        cover_url: str | None = None,
        knowledge_base_id: int | None = None,
        images: list[dict] | None = None,
        task_id: int | None = None,
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
        if knowledge_base_id is not None:
            fields["knowledge_base_id"] = int(knowledge_base_id)
        if vector_version is not None:
            fields["vector_version"] = str(vector_version)
        if text_content is not None:
            fields["text_content"] = text_content
        if content_html is not None:
            fields["content_html"] = content_html
        for key, value in (
            ("title", title), ("url", url), ("author", author),
            ("published_at", published_at), ("cover_url", cover_url),
        ):
            if value is not None:
                fields[key] = value
        if indexed is not None:
            fields["indexed_at"] = _now() if indexed else None
        if not fields:
            return False
        assignments = ", ".join("%s = ?" % key for key in fields)
        params = list(fields.values()) + [_now(), int(article_id)]
        with self.connect() as conn:
            if task_id is not None:
                self._assert_task_write(conn, task_id, for_update=True)
            cur = conn.execute(
                "UPDATE wewe_articles SET %s, updated_at = ? WHERE id = ?" % assignments,
                params,
            )
            if cur.rowcount and images is not None:
                conn.execute(
                    """UPDATE wewe_images SET status = 'stale', updated_at = ?
                       WHERE article_id = ? AND status NOT IN ('staged', 'stale')""",
                    (_now(), int(article_id)),
                )
                for image in images:
                    image_cur = conn.execute(
                        """UPDATE wewe_images SET local_path = ?, mime_type = ?, byte_size = ?,
                           description = ?, status = ?, error = ?, vector_version = ?, updated_at = ?
                           WHERE id = ? AND article_id = ?""",
                        (
                            str(image.get("local_path") or ""),
                            str(image.get("mime_type") or ""),
                            int(image.get("byte_size") or 0),
                            str(image.get("description") or ""),
                            str(image.get("status") or (
                                "described" if str(image.get("description") or "").strip() else "downloaded"
                            )),
                            str(image.get("error") or ""),
                            str(image.get("vector_version") or vector_version or "") or None,
                            _now(),
                            int(image["image_id"]),
                            int(article_id),
                        ),
                    )
                    if image_cur.rowcount != 1:
                        raise RuntimeError("published image identity does not match article")
        return cur.rowcount > 0

    def find_article(self, user_id: int, source_id: int, upstream_article_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM wewe_articles
                   WHERE user_id = ? AND source_id = ? AND upstream_article_id = ?""",
                (int(user_id), int(source_id), upstream_article_id),
            ).fetchone()
        return dict(row) if row else None

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

    def published_vector_versions(
        self, user_id: int, knowledge_base_id: int
    ) -> dict[int, str | None]:
        """Return the only vector generation visible for each published article."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id, vector_version FROM wewe_articles
                   WHERE user_id = ? AND knowledge_base_id = ? AND status = 'indexed'""",
                (int(user_id), int(knowledge_base_id)),
            ).fetchall()
        return {
            int(row["id"]): (str(row["vector_version"]) if row["vector_version"] else None)
            for row in rows
        }

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
        task_id: int | None = None,
    ) -> int:
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        with self.connect() as conn:
            if task_id is not None:
                self._assert_task_write(conn, task_id, for_update=True)
            now = _now()
            conn.execute(
                """
                INSERT INTO wewe_images (
                    article_id, user_id, url, url_hash, local_path, mime_type, byte_size,
                    description, status, error, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON DUPLICATE KEY UPDATE
                    local_path=VALUES(local_path), mime_type=VALUES(mime_type), byte_size=VALUES(byte_size),
                    description=VALUES(description), status=VALUES(status), error=VALUES(error),
                    updated_at=VALUES(updated_at)
                """,
                (
                    int(article_id), int(user_id), url, url_hash, local_path, mime_type,
                    int(byte_size), description, status, error, now, now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM wewe_images WHERE article_id = ? AND url_hash = ?",
                (int(article_id), url_hash),
            ).fetchone()
        return int(row["id"])

    def stage_image_identity(
        self, article_id: int, user_id: int, url: str, vector_version: str, task_id: int
    ) -> tuple[int, dict]:
        """Allocate a stable image id without changing the published asset generation."""
        if not vector_version:
            raise ValueError("vector_version is required for staged images")
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        now = _now()
        with self.connect() as conn:
            self._assert_task_write(conn, task_id, for_update=True)
            cur = conn.execute(
                """INSERT INTO wewe_images (
                    article_id, user_id, url, url_hash, description, status, error,
                    vector_version, created_at, updated_at
                ) VALUES (?,?,?,?,?,'staged','',?,?,?)
                ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)""",
                (
                    int(article_id), int(user_id), url, url_hash, "",
                    vector_version, now, now,
                ),
            )
            image_id = int(cur.lastrowid)
            row = conn.execute(
                "SELECT * FROM wewe_images WHERE id = ? AND article_id = ? AND user_id = ?",
                (image_id, int(article_id), int(user_id)),
            ).fetchone()
        if not row:
            raise RuntimeError("failed to allocate staged image identity")
        return image_id, dict(row)

    def update_image(
        self,
        image_id: int,
        description: str | None = None,
        status: str | None = None,
        error: str | None = None,
        task_id: int | None = None,
    ) -> bool:
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
            if task_id is not None:
                self._assert_task_write(conn, task_id, for_update=True)
            cur = conn.execute(
                "UPDATE wewe_images SET %s, updated_at = ? WHERE id = ?" % assignments,
                params,
            )
        return cur.rowcount > 0

    def get_image(self, user_id: int, image_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT i.* FROM wewe_images AS i
                JOIN wewe_articles AS a ON a.id = i.article_id
                WHERE i.id = ? AND i.user_id = ? AND a.user_id = ?
                  AND i.status NOT IN ('staged', 'stale')
                  AND (i.vector_version IS NULL OR a.vector_version IS NULL
                       OR i.vector_version = a.vector_version)
                """,
                (int(image_id), int(user_id), int(user_id)),
            ).fetchone()
        return dict(row) if row else None

    def list_images(self, article_id: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT i.* FROM wewe_images AS i
                   JOIN wewe_articles AS a ON a.id = i.article_id
                   WHERE i.article_id = ? AND i.status NOT IN ('staged', 'stale')
                     AND (i.vector_version IS NULL OR a.vector_version IS NULL
                          OR i.vector_version = a.vector_version)
                   ORDER BY i.id ASC""",
                (int(article_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- tasks ----------------------------------------------------
    def create_task(
        self,
        user_id: int,
        source_id: int | None,
        kind: str = "manual",
        *,
        reservation: CallReservation | None = None,
    ) -> int:
        now = _now()
        heartbeat_at = _precise_now()
        if reservation is not None and reservation.settled:
            raise ValueError("call reservation already settled")
        try:
            with self.connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO wewe_collect_tasks (
                        user_id, source_id, kind, status, worker_id, heartbeat_at,
                        lease_expires_at, started_at, created_at
                    ) VALUES (?,?,?,'running',?,?,?,?,?)
                    """,
                    (
                        int(user_id), source_id if source_id is not None else None, kind,
                        self.worker_id, heartbeat_at, self._lease_deadline(), now, now,
                    ),
                )
                task_id = int(cur.lastrowid)
                if reservation is not None:
                    attached = conn.execute(
                        """UPDATE call_reservations
                           SET resource_type = ?, resource_id = ?
                           WHERE reservation_id = ? AND user_id = ? AND subscription_id = ?
                             AND state = 'reserved'
                             AND resource_type IS NULL AND resource_id IS NULL""",
                        (
                            WEWE_TASK_RESOURCE_TYPE, str(task_id), reservation.reservation_id,
                            int(user_id), reservation.subscription_id,
                        ),
                    )
                    if attached.rowcount != 1:
                        raise RuntimeError("call reservation cannot be attached to collection task")
                if source_id is not None:
                    conn.execute(
                        "INSERT INTO wewe_collect_locks (source_id, task_id, created_at) VALUES (?,?,?)",
                        (int(source_id), task_id, now),
                    )
        except IntegrityError as exc:
            raise CollectionAlreadyRunning("collection already running for this source") from exc
        return task_id

    def heartbeat_task(self, task_id: int) -> bool:
        """Renew only a live task owned by this worker; never resurrect another task."""
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE wewe_collect_tasks
                SET heartbeat_at = ?, lease_expires_at = ?
                WHERE id = ? AND worker_id = ? AND status = 'running'
                """,
                (_precise_now(), self._lease_deadline(), int(task_id), self.worker_id),
            )
        return cur.rowcount == 1

    def _mark_lease_lost(self, task_id: int) -> None:
        with self._lease_events_lock:
            event = self._lease_events.get(int(task_id))
        if event is not None:
            event.set()

    def _assert_task_write(self, conn: Connection, task_id: int, *, for_update: bool) -> None:
        """Fence a write to the currently owned, locked and unexpired task."""
        suffix = " FOR UPDATE" if for_update else ""
        row = conn.execute(
            """SELECT t.status, t.worker_id, t.lease_expires_at, t.source_id,
                      l.task_id AS lock_task_id
               FROM wewe_collect_tasks AS t
               LEFT JOIN wewe_collect_locks AS l ON l.task_id = t.id
               WHERE t.id = ?""" + suffix,
            (int(task_id),),
        ).fetchone()
        now = _precise_now()
        valid = bool(
            row
            and row["status"] == "running"
            and row["worker_id"] == self.worker_id
            and row["lease_expires_at"]
            and str(row["lease_expires_at"]) > now
            and (row["source_id"] is None or int(row["lock_task_id"] or 0) == int(task_id))
        )
        if not valid:
            self._mark_lease_lost(task_id)
            raise CollectionLeaseLost(f"collection task {int(task_id)} lease is lost or expired")

    def assert_task_lease(self, task_id: int) -> None:
        """Checkpoint used before external side effects and between articles."""
        with self._lease_events_lock:
            event = self._lease_events.get(int(task_id))
        if event is not None and event.is_set():
            raise CollectionLeaseLost(f"collection task {int(task_id)} lease is lost")
        with self.connect() as conn:
            self._assert_task_write(conn, task_id, for_update=False)

    @contextmanager
    def task_write_guard(self, task_id: int):
        """Hold the task/lock rows across one external side effect.

        Recovery updates the same task row, so it cannot take over until the
        old Qdrant/file write has finished. Do not start another fenced DB
        write inside this context: each MySQL connection has its own lock.
        """
        with self.connect() as conn:
            self._assert_task_write(conn, task_id, for_update=True)
            yield

    def _fenced_mutation(self, task_id: int | None, operation):
        if task_id is None:
            with self.connect() as conn:
                return operation(conn)
        with self.connect() as conn:
            self._assert_task_write(conn, task_id, for_update=True)
            return operation(conn)

    @contextmanager
    def task_heartbeat(self, task_id: int) -> Iterator[TaskLease]:
        """Renew a task and expose lease loss to the collection thread."""
        if not self.heartbeat_task(task_id):
            raise CollectionLeaseLost("collection task lease is not owned by this worker")
        lease = TaskLease(task_id)
        with self._lease_events_lock:
            self._lease_events[int(task_id)] = lease.lost
        stop = threading.Event()
        interval = max(0.05, min(30.0, self.lease_seconds / 3.0))
        last_success = time.monotonic()

        def renew() -> None:
            nonlocal last_success
            while not stop.wait(interval):
                try:
                    if not self.heartbeat_task(task_id):
                        lease.lost.set()
                        return
                    last_success = time.monotonic()
                except Exception as exc:
                    logger.warning("task %s heartbeat failed: %s", task_id, str(exc)[:200])
                    if time.monotonic() - last_success >= self.lease_seconds:
                        lease.lost.set()
                        return

        thread = threading.Thread(
            target=renew, name=f"wewe-heartbeat-{int(task_id)}", daemon=True
        )
        thread.start()
        try:
            yield lease
        finally:
            stop.set()
            thread.join(timeout=max(1.0, interval * 2))
            with self._lease_events_lock:
                if self._lease_events.get(int(task_id)) is lease.lost:
                    self._lease_events.pop(int(task_id), None)

    def finish_task(
        self,
        task_id: int,
        status: str,
        article_count: int = 0,
        image_count: int = 0,
        indexed_count: int = 0,
        error: str = "",
    ) -> bool:
        if status not in {"success", "partial", "failed"}:
            raise ValueError("invalid collection task status")
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE wewe_collect_tasks
                SET status = ?, article_count = ?, image_count = ?, indexed_count = ?,
                    error = ?, finished_at = ?, lease_expires_at = NULL
                WHERE id = ? AND worker_id = ? AND status = 'running'
                  AND lease_expires_at > ?
                """,
                (
                    status, int(article_count), int(image_count), int(indexed_count), error,
                    _now(), int(task_id), self.worker_id, _precise_now(),
                ),
            )
            if cur.rowcount:
                conn.execute("DELETE FROM wewe_collect_locks WHERE task_id = ?", (int(task_id),))
        return cur.rowcount > 0

    def recover_interrupted_tasks(self, source_id: int | None = None) -> int:
        """Recover only legacy/no-lease or expired tasks; live peers are untouched."""
        recovered = 0
        now = _precise_now()
        expired = """status = 'running' AND (
            lease_expires_at IS NULL OR lease_expires_at = '' OR lease_expires_at <= ?
        )"""
        source_filter = ""
        source_params: list[int] = []
        if source_id is not None:
            source_filter = " AND source_id = ?"
            source_params.append(int(source_id))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM wewe_collect_tasks WHERE {expired}{source_filter}",
                [now, *source_params],
            ).fetchall()
            for row in rows:
                task_id = int(row["id"])
                # Recheck the lease in the UPDATE so a concurrent heartbeat wins.
                cur = conn.execute(
                    f"""
                    UPDATE wewe_collect_tasks
                    SET status = 'failed', error = 'worker lease expired; interrupted task recovered',
                        finished_at = ?
                    WHERE id = ? AND {expired}{source_filter}
                    """,
                    [_now(), task_id, now, *source_params],
                )
                if cur.rowcount:
                    conn.execute("DELETE FROM wewe_collect_locks WHERE task_id = ?", (task_id,))
                    recovered += 1
        return recovered

    def get_task(self, task_id: int) -> dict | None:
        """Return the authoritative task state for safe billing finalization."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM wewe_collect_tasks WHERE id = ?", (int(task_id),)
            ).fetchone()
        return dict(row) if row else None

    def list_tasks(self, user_id: int, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM wewe_collect_tasks WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (int(user_id), max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]
