"""MySQL persistence for the content-growth workspace.

Every method is user-scoped. Article references and generated artefacts remain
independent tables so deleting a source never rewrites an already saved draft.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from app.config import Settings, get_settings
from app.mysql import Connection, connect as mysql_connect
from app.mysql_schema import BILLING_SCHEMA

MAX_OWN_ARTICLES_PER_KB = 50
MAX_OWN_ARTICLE_CHARS_PER_KB = 1_000_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return default


class DraftRevisionConflict(RuntimeError):
    """The caller tried to overwrite a newer draft revision."""


class OperationConflict(RuntimeError):
    """An idempotency key is busy, completed differently, or needs reconciliation."""


CONTENT_GROWTH_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS growth_profiles (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        knowledge_base_id BIGINT NOT NULL,
        audience VARCHAR(1000) NOT NULL DEFAULT '',
        goal VARCHAR(1000) NOT NULL DEFAULT '',
        cta VARCHAR(1000) NOT NULL DEFAULT '',
        forbidden VARCHAR(2000) NOT NULL DEFAULT '',
        style_draft_json LONGTEXT NULL,
        style_json LONGTEXT NULL,
        style_version BIGINT NOT NULL DEFAULT 0,
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        UNIQUE KEY uq_growth_profile_user_kb (user_id, knowledge_base_id),
        INDEX idx_growth_profile_user (user_id, updated_at),
        CONSTRAINT fk_growth_profile_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_growth_profile_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS growth_own_articles (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        knowledge_base_id BIGINT NOT NULL,
        title VARCHAR(500) NOT NULL,
        url VARCHAR(2000) NOT NULL DEFAULT '',
        body LONGTEXT NOT NULL,
        published_at VARCHAR(40) NOT NULL DEFAULT '',
        features_json LONGTEXT NOT NULL,
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        INDEX idx_growth_own_user_kb (user_id, knowledge_base_id, created_at),
        CONSTRAINT fk_growth_own_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_growth_own_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS growth_briefs (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        knowledge_base_id BIGINT NOT NULL,
        analysis_mode VARCHAR(32) NOT NULL,
        focus VARCHAR(2000) NOT NULL DEFAULT '',
        content_json LONGTEXT NOT NULL,
        sources_json LONGTEXT NOT NULL,
        created_at VARCHAR(40) NOT NULL,
        INDEX idx_growth_brief_user_kb (user_id, knowledge_base_id, created_at),
        CONSTRAINT fk_growth_brief_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_growth_brief_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS growth_drafts (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        knowledge_base_id BIGINT NOT NULL,
        brief_id BIGINT NULL,
        title VARCHAR(500) NOT NULL,
        body_markdown LONGTEXT NOT NULL,
        content_json LONGTEXT NOT NULL,
        sources_json LONGTEXT NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'draft',
        revision BIGINT NOT NULL DEFAULT 1,
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        INDEX idx_growth_draft_user_kb (user_id, knowledge_base_id, updated_at),
        CONSTRAINT fk_growth_draft_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_growth_draft_kb FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE,
        CONSTRAINT fk_growth_draft_brief FOREIGN KEY (brief_id) REFERENCES growth_briefs(id) ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS growth_metrics (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        own_article_id BIGINT NOT NULL,
        metric_date VARCHAR(10) NOT NULL,
        window_name VARCHAR(16) NOT NULL,
        `reads` BIGINT NULL DEFAULT NULL,
        shares BIGINT NULL DEFAULT NULL,
        new_follows BIGINT NULL DEFAULT NULL,
        landing_clicks BIGINT NULL DEFAULT NULL,
        registrations BIGINT NULL DEFAULT NULL,
        payments BIGINT NULL DEFAULT NULL,
        account_label VARCHAR(200) NOT NULL,
        source_name VARCHAR(500) NOT NULL,
        campaign_id VARCHAR(200) NOT NULL DEFAULT '',
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        UNIQUE KEY uq_growth_metric_window (user_id, own_article_id, metric_date, window_name),
        INDEX idx_growth_metric_user_date (user_id, metric_date),
        CONSTRAINT fk_growth_metric_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
        CONSTRAINT fk_growth_metric_article FOREIGN KEY (own_article_id) REFERENCES growth_own_articles(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS growth_operations (
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        user_id BIGINT NOT NULL,
        endpoint VARCHAR(80) NOT NULL,
        operation_id VARCHAR(80) NOT NULL,
        request_hash CHAR(64) NOT NULL,
        reservation_id CHAR(32) NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'pending',
        response_json LONGTEXT NULL,
        created_at VARCHAR(40) NOT NULL,
        updated_at VARCHAR(40) NOT NULL,
        UNIQUE KEY uq_growth_operation (user_id, endpoint, operation_id),
        INDEX idx_growth_operation_state (user_id, status, updated_at),
        CONSTRAINT fk_growth_operation_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
)


def _features(body: str) -> dict[str, int]:
    lines = body.splitlines()
    return {
        "char_count": len(body),
        "paragraph_count": len([part for part in body.split("\n\n") if part.strip()]),
        "heading_count": len([line for line in lines if line.lstrip().startswith("#")]),
        "question_count": body.count("？") + body.count("?"),
        "list_item_count": len([line for line in lines if line.lstrip().startswith(("- ", "* "))]),
    }


class ContentGrowthStore:
    def __init__(self, settings: Settings | None = None, *, auto_migrate: bool = True):
        self.settings = settings or get_settings()
        if auto_migrate:
            self.init_db()

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with mysql_connect(self.settings) as conn:
            yield conn

    @contextmanager
    def _using(self, conn: Connection | None) -> Iterator[Connection]:
        if conn is not None:
            yield conn
        else:
            with self.connect() as owned:
                yield owned

    def init_db(self) -> None:
        """Explicit, additive migration; production calls this from a one-off command."""
        with self.connect() as conn:
            for statement in (*BILLING_SCHEMA, *CONTENT_GROWTH_SCHEMA):
                conn.execute(statement)

    def lock_kb(self, conn: Connection, user_id: int, knowledge_base_id: int) -> None:
        """Serialize final artifact writes with KB deletion and re-check ownership."""
        row = conn.execute(
            "SELECT id FROM knowledge_bases WHERE id = ? AND user_id = ? FOR UPDATE",
            (int(knowledge_base_id), int(user_id)),
        ).fetchone()
        if not row:
            raise ValueError("知识库已删除或无权访问，本次结果未保存")

    @staticmethod
    def _profile(row: dict | None) -> dict | None:
        if not row:
            return None
        item = dict(row)
        item["style_draft"] = _loads(item.pop("style_draft_json", None), None)
        item["style_profile"] = _loads(item.pop("style_json", None), None)
        return item

    def get_profile(
        self, user_id: int, knowledge_base_id: int, *, conn: Connection | None = None,
    ) -> dict | None:
        with self._using(conn) as connection:
            row = connection.execute(
                "SELECT * FROM growth_profiles WHERE user_id = ? AND knowledge_base_id = ?",
                (int(user_id), int(knowledge_base_id)),
            ).fetchone()
        return self._profile(dict(row) if row else None)

    def upsert_profile(
        self, user_id: int, knowledge_base_id: int, values: dict, *, conn: Connection | None = None,
    ) -> None:
        allowed = {key: str(values.get(key) or "") for key in ("audience", "goal", "cta", "forbidden")}
        now = _now()
        with self._using(conn) as connection:
            connection.execute(
                """INSERT INTO growth_profiles (
                       user_id, knowledge_base_id, audience, goal, cta, forbidden, created_at, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?)
                   ON DUPLICATE KEY UPDATE audience=VALUES(audience), goal=VALUES(goal), cta=VALUES(cta),
                       forbidden=VALUES(forbidden), updated_at=VALUES(updated_at)""",
                (int(user_id), int(knowledge_base_id), allowed["audience"], allowed["goal"],
                 allowed["cta"], allowed["forbidden"], now, now),
            )

    def save_style_draft(
        self, user_id: int, knowledge_base_id: int, style: dict, *, conn: Connection | None = None,
    ) -> None:
        with self._using(conn) as connection:
            if not self.get_profile(user_id, knowledge_base_id, conn=connection):
                self.upsert_profile(user_id, knowledge_base_id, {}, conn=connection)
            connection.execute(
                """UPDATE growth_profiles SET style_draft_json = ?, updated_at = ?
                   WHERE user_id = ? AND knowledge_base_id = ?""",
                (_json(style), _now(), int(user_id), int(knowledge_base_id)),
            )

    def activate_style(self, user_id: int, knowledge_base_id: int, style: dict) -> None:
        with self.connect() as conn:
            if not self.get_profile(user_id, knowledge_base_id, conn=conn):
                self.upsert_profile(user_id, knowledge_base_id, {}, conn=conn)
            conn.execute(
                """UPDATE growth_profiles SET style_json = ?, style_version = style_version + 1, updated_at = ?
                   WHERE user_id = ? AND knowledge_base_id = ?""",
                (_json(style), _now(), int(user_id), int(knowledge_base_id)),
            )

    def add_own_article(
        self, user_id: int, knowledge_base_id: int, title: str, body: str,
        url: str = "", published_at: str = "",
    ) -> int:
        now = _now()
        with self.connect() as conn:
            # Paid final writes lock KB then profile. Use the same order here so
            # the article INSERT's FK check can never close a deadlock cycle.
            self.lock_kb(conn, user_id, knowledge_base_id)
            if not self.get_profile(user_id, knowledge_base_id, conn=conn):
                self.upsert_profile(user_id, knowledge_base_id, {}, conn=conn)
            # Serialize additions per user/KB so concurrent free imports cannot
            # both pass the count/byte checks and exceed the storage quota.
            conn.execute(
                "SELECT id FROM growth_profiles WHERE user_id = ? AND knowledge_base_id = ? FOR UPDATE",
                (int(user_id), int(knowledge_base_id)),
            ).fetchone()
            # A locking read is required here. The earlier profile read may have
            # established an older REPEATABLE-READ snapshot; after waiting for the
            # profile lock, a plain COUNT/SUM could miss a competitor's committed
            # article and let both requests exceed the quota.
            articles = conn.execute(
                """SELECT id, CHAR_LENGTH(body) AS body_chars
                   FROM growth_own_articles
                   WHERE user_id = ? AND knowledge_base_id = ? FOR UPDATE""",
                (int(user_id), int(knowledge_base_id)),
            ).fetchall()
            article_count = len(articles)
            total_chars = sum(int(row["body_chars"]) for row in articles)
            if article_count >= MAX_OWN_ARTICLES_PER_KB:
                raise ValueError(f"每个知识库最多保存 {MAX_OWN_ARTICLES_PER_KB} 篇本人文章")
            if total_chars + len(body) > MAX_OWN_ARTICLE_CHARS_PER_KB:
                raise ValueError("本人文章存储容量已达上限，请清理后再录入")
            cur = conn.execute(
                """INSERT INTO growth_own_articles (
                       user_id, knowledge_base_id, title, url, body, published_at,
                       features_json, created_at, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (int(user_id), int(knowledge_base_id), title, url, body, published_at,
                 _json(_features(body)), now, now),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _own(row: dict | None) -> dict | None:
        if not row:
            return None
        item = dict(row)
        item["features"] = _loads(item.pop("features_json", None), {})
        return item

    def get_own_article(self, user_id: int, article_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM growth_own_articles WHERE id = ? AND user_id = ?",
                (int(article_id), int(user_id)),
            ).fetchone()
        return self._own(dict(row) if row else None)

    def list_own_articles(self, user_id: int, knowledge_base_id: int, limit: int = 100) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM growth_own_articles WHERE user_id = ? AND knowledge_base_id = ?
                   ORDER BY COALESCE(NULLIF(published_at, ''), created_at) DESC, id DESC LIMIT ?""",
                (int(user_id), int(knowledge_base_id), max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._own(dict(row)) for row in rows]

    def create_brief(
        self, user_id: int, knowledge_base_id: int, analysis_mode: str,
        focus: str, content: dict, sources: list[dict], *, conn: Connection | None = None,
    ) -> int:
        with self._using(conn) as connection:
            cur = connection.execute(
                """INSERT INTO growth_briefs (
                       user_id, knowledge_base_id, analysis_mode, focus, content_json, sources_json, created_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (int(user_id), int(knowledge_base_id), analysis_mode, focus,
                 _json(content), _json(sources), _now()),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _brief(row: dict | None) -> dict | None:
        if not row:
            return None
        item = dict(row)
        item["content"] = _loads(item.pop("content_json", None), {})
        item["sources"] = _loads(item.pop("sources_json", None), [])
        return item

    def get_brief(self, user_id: int, brief_id: int, *, conn: Connection | None = None) -> dict | None:
        with self._using(conn) as connection:
            row = connection.execute(
                "SELECT * FROM growth_briefs WHERE id = ? AND user_id = ?",
                (int(brief_id), int(user_id)),
            ).fetchone()
        return self._brief(dict(row) if row else None)

    def list_briefs(self, user_id: int, knowledge_base_id: int, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM growth_briefs WHERE user_id = ? AND knowledge_base_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (int(user_id), int(knowledge_base_id), max(1, min(int(limit), 100))),
            ).fetchall()
        return [self._brief(dict(row)) for row in rows]

    def create_draft(
        self, user_id: int, knowledge_base_id: int, brief_id: int | None,
        title: str, body_markdown: str, content: dict, sources: list[dict],
        *, conn: Connection | None = None,
    ) -> int:
        now = _now()
        with self._using(conn) as connection:
            cur = connection.execute(
                """INSERT INTO growth_drafts (
                       user_id, knowledge_base_id, brief_id, title, body_markdown,
                       content_json, sources_json, status, created_at, updated_at
                   ) VALUES (?,?,?,?,?,?,?,'draft',?,?)""",
                (int(user_id), int(knowledge_base_id), int(brief_id) if brief_id else None,
                 title, body_markdown, _json(content), _json(sources), now, now),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _draft(row: dict | None) -> dict | None:
        if not row:
            return None
        item = dict(row)
        content = _loads(item.pop("content_json", None), {})
        item["sources"] = _loads(item.pop("sources_json", None), [])
        for key in ("title_options", "outline", "citation_map", "unresolved_facts", "risk_notes"):
            item[key] = content.get(key, [])
        item["suggested_cta"] = content.get("suggested_cta", "")
        item["evidence_invalidated"] = bool(content.get("evidence_invalidated", False))
        return item

    def get_draft(self, user_id: int, draft_id: int, *, conn: Connection | None = None) -> dict | None:
        with self._using(conn) as connection:
            row = connection.execute(
                "SELECT * FROM growth_drafts WHERE id = ? AND user_id = ?",
                (int(draft_id), int(user_id)),
            ).fetchone()
        return self._draft(dict(row) if row else None)

    def list_drafts(self, user_id: int, knowledge_base_id: int, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM growth_drafts WHERE user_id = ? AND knowledge_base_id = ?
                   ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (int(user_id), int(knowledge_base_id), max(1, min(int(limit), 100))),
            ).fetchall()
        return [self._draft(dict(row)) for row in rows]

    def update_draft(
        self, user_id: int, draft_id: int, values: dict, *, expected_revision: int,
    ) -> bool:
        allowed = {key: values[key] for key in ("title", "body_markdown", "status") if key in values}
        with self.connect() as conn:
            row = conn.execute(
                """SELECT title, body_markdown, status, content_json, revision
                   FROM growth_drafts WHERE user_id = ? AND id = ? FOR UPDATE""",
                (int(user_id), int(draft_id)),
            ).fetchone()
            if not row:
                return False
            if int(row["revision"]) != int(expected_revision):
                raise DraftRevisionConflict("草稿已在其他页面更新，请保留当前内容并重新打开最新版本")
            changed = any(key in allowed and allowed[key] != row[key] for key in ("title", "body_markdown", "status"))
            if not changed:
                return True
            if "body_markdown" in allowed and allowed["body_markdown"] != row["body_markdown"]:
                content = _loads(row["content_json"], {})
                content["citation_map"] = []
                content["unresolved_facts"] = []
                content["evidence_invalidated"] = True
                allowed["content_json"] = _json(content)
            allowed["revision"] = int(row["revision"]) + 1
            assignments = ", ".join(f"{key} = ?" for key in allowed)
            params = list(allowed.values()) + [_now(), int(user_id), int(draft_id), int(expected_revision)]
            conn.execute(
                f"""UPDATE growth_drafts SET {assignments}, updated_at = ?
                    WHERE user_id = ? AND id = ? AND revision = ?""",
                params,
            )
            return True

    def upsert_metric(
        self, user_id: int, own_article_id: int, metric_date: str,
        window: str, values: dict,
    ) -> int:
        fields = {
            key: None if values.get(key) is None else int(values[key])
            for key in ("reads", "shares", "new_follows", "landing_clicks", "registrations", "payments")
        }
        account_label = str(values.get("account_label") or "").strip()
        source_name = str(values.get("source_name") or "").strip()
        campaign_id = str(values.get("campaign_id") or "").strip()
        if not account_label or not source_name:
            raise ValueError("指标必须填写公众号账号标识与数据来源")
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO growth_metrics (
                       user_id, own_article_id, metric_date, window_name, `reads`, shares, new_follows,
                       landing_clicks, registrations, payments, account_label, source_name, campaign_id,
                       created_at, updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON DUPLICATE KEY UPDATE `reads`=VALUES(`reads`), shares=VALUES(shares),
                       new_follows=VALUES(new_follows), landing_clicks=VALUES(landing_clicks),
                       registrations=VALUES(registrations), payments=VALUES(payments),
                       account_label=VALUES(account_label), source_name=VALUES(source_name),
                       campaign_id=VALUES(campaign_id), updated_at=VALUES(updated_at)""",
                (int(user_id), int(own_article_id), metric_date, window, fields["reads"],
                 fields["shares"], fields["new_follows"], fields["landing_clicks"],
                 fields["registrations"], fields["payments"], account_label, source_name,
                 campaign_id, now, now),
            )
            row = conn.execute(
                """SELECT id FROM growth_metrics WHERE user_id = ? AND own_article_id = ?
                   AND metric_date = ? AND window_name = ?""",
                (int(user_id), int(own_article_id), metric_date, window),
            ).fetchone()
            return int(row["id"])

    def get_metric(self, user_id: int, knowledge_base_id: int, metric_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT m.* FROM growth_metrics m
                   JOIN growth_own_articles a ON a.id = m.own_article_id AND a.user_id = m.user_id
                   WHERE m.id = ? AND m.user_id = ? AND a.knowledge_base_id = ?""",
                (int(metric_id), int(user_id), int(knowledge_base_id)),
            ).fetchone()
        return dict(row) if row else None

    def list_metrics(self, user_id: int, knowledge_base_id: int, limit: int = 100) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT m.* FROM growth_metrics m
                   JOIN growth_own_articles a ON a.id = m.own_article_id AND a.user_id = m.user_id
                   WHERE m.user_id = ? AND a.knowledge_base_id = ?
                   ORDER BY m.metric_date DESC, m.id DESC LIMIT ?""",
                (int(user_id), int(knowledge_base_id), max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def replay_succeeded_operation(
        self, user_id: int, endpoint: str, operation_id: str, request_hash: str,
    ) -> dict | None:
        """Read a committed result before re-validating mutable source state."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT request_hash, status, response_json FROM growth_operations
                   WHERE user_id = ? AND endpoint = ? AND operation_id = ?""",
                (int(user_id), endpoint, operation_id),
            ).fetchone()
        if not row:
            return None
        if row["request_hash"] != request_hash:
            raise OperationConflict("相同操作编号对应了不同请求，请刷新后重试")
        if row["status"] != "succeeded":
            return None
        return _loads(row["response_json"], {})

    def get_prepared_operation(
        self, user_id: int, endpoint: str, operation_id: str, request_hash: str,
    ) -> dict | None:
        """Return the durable post-AI checkpoint, if this operation has one."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT request_hash, status, response_json FROM growth_operations
                   WHERE user_id = ? AND endpoint = ? AND operation_id = ?""",
                (int(user_id), endpoint, operation_id),
            ).fetchone()
        if not row:
            return None
        if row["request_hash"] != request_hash:
            raise OperationConflict("相同操作编号对应了不同请求，请刷新后重试")
        if row["status"] not in {"prepared", "retryable"} or not row["response_json"]:
            return None
        return _loads(row["response_json"], None)

    def begin_operation(
        self,
        user_id: int,
        endpoint: str,
        operation_id: str,
        request_hash: str,
        reserve: Callable[[Connection], Any],
    ) -> dict[str, Any]:
        """Start or replay one paid operation without holding a transaction across AI."""
        now = _now()
        with self.connect() as conn:
            inserted = conn.execute(
                """INSERT INTO growth_operations (
                       user_id, endpoint, operation_id, request_hash, status, created_at, updated_at
                   ) VALUES (?,?,?,?,'pending',?,?)
                   ON DUPLICATE KEY UPDATE id = id""",
                (int(user_id), endpoint, operation_id, request_hash, now, now),
            )
            is_new = inserted.rowcount == 1
            row = conn.execute(
                """SELECT * FROM growth_operations
                   WHERE user_id = ? AND endpoint = ? AND operation_id = ? FOR UPDATE""",
                (int(user_id), endpoint, operation_id),
            ).fetchone()
            if not row:
                raise RuntimeError("paid operation could not be locked")
            if row["request_hash"] != request_hash:
                raise OperationConflict("相同操作编号对应了不同请求，请刷新后重试")
            if row["status"] == "succeeded":
                return {"state": "succeeded", "response": _loads(row["response_json"], {})}
            if not is_new and row["status"] in {"pending", "prepared"}:
                reservation_id = row["reservation_id"]
                if reservation_id:
                    ledger = conn.execute(
                        """SELECT state FROM call_reservations
                           WHERE reservation_id = ? AND user_id = ?""",
                        (reservation_id, int(user_id)),
                    ).fetchone()
                    if ledger and ledger["state"] == "reserved":
                        raise OperationConflict("相同任务正在处理，请稍后使用相同操作重试")
                    if ledger and ledger["state"] == "succeeded":
                        raise OperationConflict("任务账务状态待对账，请勿重复提交")
                else:
                    updated = datetime.fromisoformat(str(row["updated_at"]))
                    age = (datetime.now(timezone.utc) - updated).total_seconds()
                    if age < 6 * 60 * 60:
                        raise OperationConflict("相同任务正在处理，请稍后使用相同操作重试")
            checkpoint = (
                _loads(row["response_json"], None)
                if row["status"] in {"prepared", "retryable"} else None
            )
            reservation = reserve(conn)
            reservation_id = reservation.reservation_id if reservation is not None else None
            next_status = "prepared" if isinstance(checkpoint, dict) else "pending"
            conn.execute(
                """UPDATE growth_operations
                   SET status = ?, reservation_id = ?, response_json = ?, updated_at = ?
                   WHERE id = ?""",
                (next_status, reservation_id,
                 _json(checkpoint) if checkpoint is not None else None,
                 _now(), int(row["id"])),
            )
            return {
                "state": "resumed" if next_status == "prepared" else "started",
                "reservation": reservation,
                "checkpoint": checkpoint,
            }

    @staticmethod
    def _finish_operation(
        conn: Connection,
        user_id: int,
        endpoint: str,
        operation_id: str,
        reservation_id: str | None,
        status: str,
        response: dict | None,
    ) -> None:
        # MySQL reports changed rows, not matched rows. Microsecond precision keeps
        # repeated prepared-state writes observable even within the same second.
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        if reservation_id:
            cur = conn.execute(
                """UPDATE growth_operations SET status = ?, response_json = ?, updated_at = ?
                   WHERE user_id = ? AND endpoint = ? AND operation_id = ?
                     AND status IN ('pending', 'prepared') AND reservation_id = ?""",
                (status, _json(response) if response is not None else None, now,
                 int(user_id), endpoint, operation_id, reservation_id),
            )
        else:
            cur = conn.execute(
                """UPDATE growth_operations SET status = ?, response_json = ?, updated_at = ?
                   WHERE user_id = ? AND endpoint = ? AND operation_id = ?
                     AND status IN ('pending', 'prepared') AND reservation_id IS NULL""",
                (status, _json(response) if response is not None else None, now,
                 int(user_id), endpoint, operation_id),
            )
        if cur.rowcount != 1:
            raise RuntimeError("paid operation state changed during settlement")

    def checkpoint_operation(
        self,
        conn: Connection,
        user_id: int,
        endpoint: str,
        operation_id: str,
        reservation_id: str | None,
        checkpoint: dict,
    ) -> None:
        """Commit normalized AI output before the separately recoverable DB write."""
        self._finish_operation(
            conn, user_id, endpoint, operation_id, reservation_id, "prepared", checkpoint,
        )

    def complete_operation(
        self,
        conn: Connection,
        user_id: int,
        endpoint: str,
        operation_id: str,
        reservation_id: str | None,
        response: dict,
    ) -> None:
        self._finish_operation(
            conn, user_id, endpoint, operation_id, reservation_id, "succeeded", response,
        )

    def fail_operation(
        self,
        conn: Connection,
        user_id: int,
        endpoint: str,
        operation_id: str,
        reservation_id: str | None,
        checkpoint: dict | None = None,
    ) -> None:
        self._finish_operation(
            conn, user_id, endpoint, operation_id, reservation_id,
            "retryable" if checkpoint is not None else "failed", checkpoint,
        )
