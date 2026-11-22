from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def estimate_tokens(text: str | None) -> int:
    clean = text or ""
    if not clean:
        return 0
    ascii_count = sum(1 for ch in clean if ord(ch) < 128)
    non_ascii_count = len(clean) - ascii_count
    return max(1, int(ascii_count / 4) + int(non_ascii_count / 1.8))


def hash_lookup_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_phone(phone: str) -> str:
    clean = "".join(ch for ch in (phone or "").strip() if ch.isdigit() or ch == "+")
    if clean.startswith("+86"):
        clean = clean[3:]
    if clean.startswith("86") and len(clean) == 13:
        clean = clean[2:]
    return clean


def mask_phone(phone: str | None) -> str:
    clean = normalize_phone(phone or "")
    if len(clean) < 7:
        return clean
    return f"{clean[:3]}****{clean[-4:]}"


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return salt, digest.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, actual_hash = hash_password(password, salt)
    return hmac.compare_digest(actual_hash, expected_hash)


@dataclass
class UserContext:
    id: int
    email: str
    is_admin: bool
    is_active: bool


@dataclass
class ApiKeyContext:
    id: int
    user_id: int
    email: str
    key_prefix: str
    is_admin: bool


@dataclass
class CallReservation:
    """One quota unit atomically reserved before an expensive operation."""

    user_id: int
    subscription_id: int
    call_count: int = 1
    reservation_id: str = ""
    settled: bool = False


from app.config import Settings, get_settings
from app.mysql import Connection, IntegrityError, Row, connect as mysql_connect
from app.mysql_schema import BILLING_SCHEMA

PLAN_SEEDS = (
    ("free_trial", "新人免费体验包", "trial", 0, 3, 0),
    ("personal_light", "个人轻量版", "month", 990, 100, 10),
    ("personal_standard", "个人标准版", "month", 1990, 300, 20),
    ("personal_pro", "专业增强版", "month", 4990, 1000, 30),
)


class BillingStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        if not self.settings.mysql_database or not self.settings.mysql_user:
            raise RuntimeError("MYSQL_DATABASE 和 MYSQL_USER 必须显式配置")
        self.init_db()

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with mysql_connect(self.settings) as conn:
            yield conn

    def init_db(self) -> None:
        with self.connect() as conn:
            for statement in BILLING_SCHEMA:
                conn.execute(statement)
            self._ensure_column(conn, "users", "phone", "VARCHAR(32) NULL")
            self._ensure_column(conn, "api_keys", "raw_key", "VARCHAR(255) NULL")
            self._seed_plans(conn)

    def _ensure_column(self, conn: Connection, table: str, column: str, definition: str) -> None:
        allowed = {("users", "phone"), ("api_keys", "raw_key")}
        if (table, column) not in allowed:
            raise ValueError("不允许的 schema 变更")
        row = conn.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_schema = ? AND table_name = ? AND column_name = ?""",
            (self.settings.mysql_database, table, column),
        ).fetchone()
        if not row:
            conn.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")

    def _seed_plans(self, conn: Connection) -> None:
        plans = list(PLAN_SEEDS)
        for code, name, period, price, included, sort_order in plans:
            conn.execute(
                """
                INSERT INTO plans (code, name, billing_period, price_cents, included_calls, sort_order)
                VALUES (?, ?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    name=VALUES(name), billing_period=VALUES(billing_period),
                    price_cents=VALUES(price_cents), included_calls=VALUES(included_calls),
                    sort_order=VALUES(sort_order)
                """,
                (code, name, period, price, included, sort_order),
            )
        active_codes = [code for code, *_ in plans]
        placeholders = ",".join("?" for _ in active_codes)
        conn.execute(f"UPDATE plans SET is_active = 0 WHERE code NOT IN ({placeholders})", active_codes)

    def _grant_signup_trial(self, conn: Connection, user_id: int) -> None:
        plan = conn.execute("SELECT * FROM plans WHERE code = 'free_trial' AND is_active = 1").fetchone()
        if not plan:
            return
        now = utc_now()
        conn.execute(
            """
            INSERT INTO subscriptions
                (user_id, plan_code, status, starts_at, ends_at, call_quota, used_calls, created_at)
            VALUES (?, 'free_trial', 'active', ?, ?, ?, 0, ?)
            """,
            (
                user_id,
                to_iso(now),
                to_iso(now + timedelta(days=365)),
                int(plan["included_calls"]),
                to_iso(now),
            ),
        )

    def bootstrap_admin(self, email: str, password: str) -> None:
        if not email or not password:
            return
        with self.connect() as conn:
            existing_count = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            if existing_count > 0:
                return
            salt, pwd_hash = hash_password(password)
            conn.execute(
                """
                INSERT INTO users (email, password_salt, password_hash, is_admin, is_active, created_at)
                VALUES (?, ?, ?, 1, 1, ?)
                """,
                (email.lower().strip(), salt, pwd_hash, to_iso(utc_now())),
            )

    def create_user(self, email: str, password: str, is_admin: bool = False) -> dict[str, Any]:
        email_norm = email.lower().strip()
        if "@" not in email_norm:
            raise ValueError("邮箱格式不正确")
        if len(password) < 8:
            raise ValueError("密码至少需要 8 位")
        salt, pwd_hash = hash_password(password)
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO users (email, password_salt, password_hash, is_admin, is_active, created_at)
                    VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (email_norm, salt, pwd_hash, 1 if is_admin else 0, to_iso(utc_now())),
                )
            except IntegrityError as exc:
                raise ValueError("该邮箱已注册") from exc
            if not is_admin:
                self._grant_signup_trial(conn, int(cur.lastrowid))
            return self.get_user_by_id(int(cur.lastrowid), conn=conn)

    def create_user_by_phone(self, phone: str, password: str, is_admin: bool = False) -> dict[str, Any]:
        phone_norm = normalize_phone(phone)
        if not self.is_valid_cn_phone(phone_norm):
            raise ValueError("手机号格式不正确")
        if len(password) < 8:
            raise ValueError("密码至少需要 8 位")
        salt, pwd_hash = hash_password(password)
        email_alias = f"{phone_norm}@phone.local"
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO users (email, phone, password_salt, password_hash, is_admin, is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                    """,
                    (email_alias, phone_norm, salt, pwd_hash, 1 if is_admin else 0, to_iso(utc_now())),
                )
            except IntegrityError as exc:
                raise ValueError("该手机号已注册") from exc
            if not is_admin:
                self._grant_signup_trial(conn, int(cur.lastrowid))
            return self.get_user_by_id(int(cur.lastrowid), conn=conn)

    def record_agreement_consent(
        self,
        user_id: int,
        action: str,
        terms_version: str,
        privacy_version: str,
        document_hash: str,
        ip_address: str,
        user_agent: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO agreement_consents (
                    user_id, action, terms_version, privacy_version,
                    document_hash, ip_address, user_agent, accepted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    action[:40],
                    terms_version[:40],
                    privacy_version[:40],
                    document_hash[:128],
                    ip_address[:80],
                    user_agent[:500],
                    to_iso(utc_now()),
                ),
            )

    def authenticate(self, email: str, password: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            login = email.lower().strip()
            phone = normalize_phone(login)
            if self.is_valid_cn_phone(phone):
                row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM users WHERE email = ?", (login,)).fetchone()
            if not row or not int(row["is_active"]):
                return None
            if not verify_password(password, row["password_salt"], row["password_hash"]):
                return None
            return self._user_public(row)

    def change_password(self, user_id: int, old_password: str, new_password: str) -> None:
        if len(new_password) < 8:
            raise ValueError("新密码至少需要 8 位")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row or not int(row["is_active"]):
                raise ValueError("用户不存在或已禁用")
            if not verify_password(old_password, row["password_salt"], row["password_hash"]):
                raise ValueError("旧密码错误")
            salt, pwd_hash = hash_password(new_password)
            conn.execute(
                "UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
                (salt, pwd_hash, user_id),
            )

    def create_session(self, user_id: int, days: int = 7) -> str:
        token = "sess_" + secrets.token_urlsafe(32)
        expires_at = to_iso(utc_now() + timedelta(days=days))
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (user_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (user_id, hash_lookup_secret(token), expires_at, to_iso(utc_now())),
            )
        return token

    def get_user_by_session(self, token: str | None) -> UserContext | None:
        if not token:
            return None
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT u.* FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (hash_lookup_secret(token),),
            ).fetchone()
            sess = conn.execute(
                "SELECT expires_at FROM sessions WHERE token_hash = ?",
                (hash_lookup_secret(token),),
            ).fetchone()
            if not row or not sess or not int(row["is_active"]):
                return None
            expires_at = from_iso(sess["expires_at"])
            if not expires_at or expires_at <= now:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_lookup_secret(token),))
                return None
            return UserContext(
                id=int(row["id"]),
                email=str(row["email"]),
                is_admin=bool(row["is_admin"]),
                is_active=bool(row["is_active"]),
            )

    def delete_session(self, token: str | None) -> None:
        if not token:
            return
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_lookup_secret(token),))

    def create_api_key(self, user_id: int, name: str = "默认密钥") -> tuple[str, dict[str, Any]]:
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM api_keys
                WHERE user_id = ? AND is_active = 1 AND raw_key IS NOT NULL AND raw_key != ''
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if existing:
                return str(existing["raw_key"]), self._api_key_public(existing)

            raw = "dp_" + secrets.token_urlsafe(32)
            key_prefix = raw[:12]
            cur = conn.execute(
                """
                INSERT INTO api_keys (user_id, name, key_prefix, key_hash, raw_key, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (user_id, name.strip() or "默认密钥", key_prefix, hash_lookup_secret(raw), raw, to_iso(utc_now())),
            )
            item = conn.execute("SELECT * FROM api_keys WHERE id = ?", (cur.lastrowid,)).fetchone()
            return raw, self._api_key_public(item)

    def get_api_key_context(self, raw_key: str | None) -> ApiKeyContext | None:
        if not raw_key:
            return None
        key_hash = hash_lookup_secret(raw_key.strip())
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT k.*, u.email, u.is_admin, u.is_active AS user_active
                FROM api_keys k
                JOIN users u ON u.id = k.user_id
                WHERE k.key_hash = ?
                """,
                (key_hash,),
            ).fetchone()
            if not row or not int(row["is_active"]) or not int(row["user_active"]):
                return None
            conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (to_iso(utc_now()), row["id"]))
            return ApiKeyContext(
                id=int(row["id"]),
                user_id=int(row["user_id"]),
                email=str(row["email"]),
                key_prefix=str(row["key_prefix"]),
                is_admin=bool(row["is_admin"]),
            )

    def list_api_keys(self, user_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            return [self._api_key_public(row) for row in rows]

    def ensure_default_knowledge_base(self, user_id: int, conn: Connection | None = None) -> dict[str, Any]:
        own_conn = conn is None
        if own_conn:
            ctx = self.connect()
            conn = ctx.__enter__()
        try:
            if conn is None:
                raise RuntimeError("BillingStore connection is unavailable")
            row = conn.execute(
                """
                SELECT * FROM knowledge_bases
                WHERE user_id = ? AND is_default = 1 AND is_active = 1
                ORDER BY id ASC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row:
                return self._knowledge_base_public(row)

            now = to_iso(utc_now())
            cur = conn.execute(
                """
                INSERT INTO knowledge_bases
                    (user_id, name, description, is_default, is_active, created_at, updated_at)
                VALUES (?, '默认知识库', '系统自动创建，用于承接未指定知识库的文档和问答。', 1, 1, ?, ?)
                """,
                (user_id, now, now),
            )
            row = conn.execute("SELECT * FROM knowledge_bases WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self._knowledge_base_public(row)
        finally:
            if own_conn:
                ctx.__exit__(None, None, None)

    def create_knowledge_base(self, user_id: int, name: str, description: str = "") -> dict[str, Any]:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("知识库名称不能为空")
        if len(clean_name) > 80:
            raise ValueError("知识库名称不能超过 80 个字符")
        clean_desc = (description or "").strip()[:500]
        with self.connect() as conn:
            now = to_iso(utc_now())
            cur = conn.execute(
                """
                INSERT INTO knowledge_bases
                    (user_id, name, description, is_default, is_active, created_at, updated_at)
                VALUES (?, ?, ?, 0, 1, ?, ?)
                """,
                (user_id, clean_name, clean_desc, now, now),
            )
            row = conn.execute("SELECT * FROM knowledge_bases WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self._knowledge_base_public(row)

    def list_knowledge_bases(self, user_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            self.ensure_default_knowledge_base(user_id, conn=conn)
            rows = conn.execute(
                """
                SELECT kb.*,
                       COUNT(kd.id) AS document_count,
                       COALESCE(SUM(kd.vector_count), 0) AS vector_count
                FROM knowledge_bases kb
                LEFT JOIN knowledge_documents kd
                  ON kd.knowledge_base_id = kb.id AND kd.user_id = kb.user_id
                WHERE kb.user_id = ? AND kb.is_active = 1
                GROUP BY kb.id
                ORDER BY kb.is_default DESC, kb.created_at ASC
                """,
                (user_id,),
            ).fetchall()
            return [self._knowledge_base_public(row) for row in rows]

    def get_knowledge_base_for_user(self, user_id: int, knowledge_base_id: int | None) -> dict[str, Any]:
        with self.connect() as conn:
            if knowledge_base_id is None:
                return self.ensure_default_knowledge_base(user_id, conn=conn)
            row = conn.execute(
                """
                SELECT kb.*,
                       COUNT(kd.id) AS document_count,
                       COALESCE(SUM(kd.vector_count), 0) AS vector_count
                FROM knowledge_bases kb
                LEFT JOIN knowledge_documents kd
                  ON kd.knowledge_base_id = kb.id AND kd.user_id = kb.user_id
                WHERE kb.id = ? AND kb.user_id = ? AND kb.is_active = 1
                GROUP BY kb.id
                """,
                (knowledge_base_id, user_id),
            ).fetchone()
            if not row:
                raise ValueError("知识库不存在或无权访问")
            return self._knowledge_base_public(row)

    def record_knowledge_document(
        self,
        user_id: int,
        knowledge_base_id: int,
        run_id: str,
        source_filename: str,
        source_type: str,
        status: str,
        vector_count: int,
        text_block_count: int,
        image_count: int,
        artifact_path: str,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            kb = conn.execute(
                "SELECT * FROM knowledge_bases WHERE id = ? AND user_id = ? AND is_active = 1",
                (knowledge_base_id, user_id),
            ).fetchone()
            if not kb:
                raise ValueError("知识库不存在或无权访问")
            now = to_iso(utc_now())
            cur = conn.execute(
                """
                INSERT INTO knowledge_documents
                    (user_id, knowledge_base_id, run_id, source_filename, source_type, status,
                     vector_count, text_block_count, image_count, artifact_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    knowledge_base_id,
                    run_id,
                    source_filename[:255],
                    source_type[:40],
                    status[:40],
                    int(vector_count or 0),
                    int(text_block_count or 0),
                    int(image_count or 0),
                    artifact_path[:500],
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM knowledge_documents WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self._knowledge_document_public(row)

    def list_knowledge_documents(self, user_id: int, knowledge_base_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM knowledge_documents
                WHERE user_id = ? AND knowledge_base_id = ?
                ORDER BY created_at DESC
                """,
                (user_id, knowledge_base_id),
            ).fetchall()
            return [self._knowledge_document_public(row) for row in rows]

    def get_knowledge_document_for_user(
        self,
        user_id: int,
        knowledge_base_id: int,
        document_id: int,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM knowledge_documents
                WHERE id = ? AND user_id = ? AND knowledge_base_id = ?
                """,
                (document_id, user_id, knowledge_base_id),
            ).fetchone()
            if not row:
                raise ValueError("文档不存在或无权访问")
            return self._knowledge_document_public(row)

    def delete_knowledge_document(
        self,
        user_id: int,
        knowledge_base_id: int,
        document_id: int,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM knowledge_documents
                WHERE id = ? AND user_id = ? AND knowledge_base_id = ?
                """,
                (document_id, user_id, knowledge_base_id),
            ).fetchone()
            if not row:
                raise ValueError("文档不存在或无权访问")
            item = self._knowledge_document_public(row)
            conn.execute("DELETE FROM knowledge_documents WHERE id = ?", (document_id,))
            return item

    def record_rag_query_log(
        self,
        user_id: int,
        api_key_id: int | None,
        knowledge_base_id: int,
        query: str,
        answer: str,
        retrieval_mode: str,
        top_k: int,
        source_filename: str | None,
        hit_count: int,
        citations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self.connect() as conn:
            kb = conn.execute(
                "SELECT * FROM knowledge_bases WHERE id = ? AND user_id = ? AND is_active = 1",
                (knowledge_base_id, user_id),
            ).fetchone()
            if not kb:
                raise ValueError("知识库不存在或无权访问")
            cur = conn.execute(
                """
                INSERT INTO rag_query_logs
                    (user_id, api_key_id, knowledge_base_id, query, answer, retrieval_mode, top_k,
                     source_filename, hit_count, citations_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    api_key_id,
                    knowledge_base_id,
                    query,
                    answer,
                    retrieval_mode[:40],
                    int(top_k),
                    (source_filename or "")[:255] or None,
                    int(hit_count),
                    json.dumps(citations or [], ensure_ascii=False),
                    to_iso(utc_now()),
                ),
            )
            row = conn.execute("SELECT * FROM rag_query_logs WHERE id = ?", (cur.lastrowid,)).fetchone()
            return self._rag_query_log_public(row)

    def update_rag_feedback(
        self,
        user_id: int,
        log_id: int,
        rating: str,
        note: str = "",
    ) -> dict[str, Any]:
        clean_rating = (rating or "").strip().lower()
        if clean_rating not in {"up", "down"}:
            raise ValueError("反馈类型不正确")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM rag_query_logs WHERE id = ? AND user_id = ?",
                (log_id, user_id),
            ).fetchone()
            if not row:
                raise ValueError("问答日志不存在或无权访问")
            conn.execute(
                """
                UPDATE rag_query_logs
                SET feedback_rating = ?, feedback_note = ?, feedback_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (clean_rating, (note or "").strip()[:500], to_iso(utc_now()), log_id, user_id),
            )
            updated = conn.execute("SELECT * FROM rag_query_logs WHERE id = ?", (log_id,)).fetchone()
            return self._rag_query_log_public(updated)

    def list_plans(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM plans WHERE is_active = 1 ORDER BY sort_order").fetchall()
            return [dict(row) | {"price_yuan": round(int(row["price_cents"]) / 100, 2)} for row in rows]

    def grant_subscription(
        self,
        user_id: int,
        plan_code: str,
        call_quota: int | None = None,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> dict[str, Any]:
        starts = starts_at or utc_now()
        with self.connect() as conn:
            plan = conn.execute("SELECT * FROM plans WHERE code = ?", (plan_code,)).fetchone()
            if not plan:
                raise ValueError("套餐不存在")
            if ends_at is None:
                period = str(plan["billing_period"])
                if period == "trial":
                    ends_at = starts + timedelta(days=365)
                elif period == "package":
                    ends_at = starts + timedelta(days=365)
                elif period == "quarter":
                    ends_at = starts + timedelta(days=90)
                elif period == "year":
                    ends_at = starts + timedelta(days=365)
                else:
                    ends_at = starts + timedelta(days=30)
            quota = int(call_quota or plan["included_calls"])
            cur = conn.execute(
                """
                INSERT INTO subscriptions
                    (user_id, plan_code, status, starts_at, ends_at, call_quota, used_calls, created_at)
                VALUES (?, ?, 'active', ?, ?, ?, 0, ?)
                """,
                (user_id, plan_code, to_iso(starts), to_iso(ends_at), quota, to_iso(utc_now())),
            )
            return self.get_subscription(int(cur.lastrowid), conn=conn)

    def create_purchase_intent(self, user_id: int, plan_code: str, contact_note: str = "") -> dict[str, Any]:
        clean_note = (contact_note or "").strip()[:500]
        with self.connect() as conn:
            plan = conn.execute(
                "SELECT * FROM plans WHERE code = ? AND is_active = 1",
                (plan_code,),
            ).fetchone()
            if not plan:
                raise ValueError("套餐不存在或已下架")
            if int(plan["price_cents"]) <= 0:
                raise ValueError("免费体验包注册后自动发放，无需开通")
            cur = conn.execute(
                """
                INSERT INTO purchase_intents (user_id, plan_code, status, contact_note, created_at)
                VALUES (?, ?, 'pending', ?, ?)
                """,
                (user_id, plan_code, clean_note, to_iso(utc_now())),
            )
            row = conn.execute(
                """
                SELECT pi.*, p.name AS plan_name, p.price_cents, p.included_calls, p.billing_period
                FROM purchase_intents pi
                JOIN plans p ON p.code = pi.plan_code
                WHERE pi.id = ?
                """,
                (cur.lastrowid,),
            ).fetchone()
            return dict(row) | {
                "price_yuan": round(int(row["price_cents"]) / 100, 2),
            }

    def get_active_subscription(self, user_id: int, conn: Connection | None = None) -> dict[str, Any] | None:
        own_conn = conn is None
        if own_conn:
            ctx = self.connect()
            conn = ctx.__enter__()
        try:
            if conn is None:
                raise RuntimeError("BillingStore connection is unavailable")
            now = to_iso(utc_now())
            row = conn.execute(
                """
                SELECT s.*, p.name AS plan_name, p.price_cents, p.billing_period
                FROM subscriptions s
                JOIN plans p ON p.code = s.plan_code
                WHERE s.user_id = ?
                  AND s.status = 'active'
                  AND s.starts_at <= ?
                  AND s.ends_at > ?
                ORDER BY s.ends_at DESC
                LIMIT 1
                """,
                (user_id, now, now),
            ).fetchone()
            return self._subscription_public(row) if row else None
        finally:
            if own_conn:
                ctx.__exit__(None, None, None)

    def ensure_quota(self, user_id: int) -> dict[str, Any]:
        # The dependency-layer precheck must not strand the final quota unit
        # behind an abandoned reservation that only reserve_call would refund.
        self.reconcile_abandoned_reservations()
        sub = self.get_active_subscription(user_id)
        if not sub:
            raise PermissionError("当前账号没有可用订阅")
        if int(sub["remaining_calls"]) <= 0:
            raise PermissionError("当前订阅调用次数已用完")
        return sub

    def reserve_call(self, user_id: int, call_count: int = 1) -> CallReservation:
        """Persistently reserve quota before invoking a paid dependency."""
        if call_count < 1:
            raise ValueError("call_count must be positive")
        # Any live worker can help clean up a crashed peer. Only reservations
        # older than the conservative TTL are eligible; normal calls are untouched.
        self.reconcile_abandoned_reservations()
        now = to_iso(utc_now())
        with self.connect() as conn:
            return self._reserve_call_in_connection(conn, user_id, call_count, now)

    def consume_call(
        self,
        user_id: int,
        api_key_id: int | None,
        endpoint: str,
        method: str,
        status_code: int,
        latency_ms: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        meta: dict | None = None,
        call_count: int = 1,
        reservation: CallReservation | None = None,
    ) -> None:
        """Settle a persistent reservation, or charge a successful legacy call."""
        if reservation is not None:
            if reservation.settled:
                raise RuntimeError("call reservation already settled")
            if reservation.user_id != user_id:
                raise ValueError("reservation user mismatch")
            if not reservation.reservation_id:
                raise ValueError("persistent reservation id is required")
        now = to_iso(utc_now())
        successful_billing = 200 <= int(status_code) < 400 and call_count > 0
        if reservation is not None:
            with self.connect() as conn:
                state = "succeeded" if successful_billing else "failed"
                claimed = conn.execute(
                    """UPDATE call_reservations
                       SET state = ?, settled_at = ?
                       WHERE reservation_id = ? AND user_id = ? AND subscription_id = ?
                         AND state = 'reserved'""",
                    (
                        state,
                        now,
                        reservation.reservation_id,
                        user_id,
                        reservation.subscription_id,
                    ),
                )
                if claimed.rowcount != 1:
                    raise RuntimeError("call reservation already settled")
                if not successful_billing:
                    released = conn.execute(
                        """UPDATE subscriptions SET used_calls = used_calls - ?
                           WHERE id = ? AND used_calls >= ?""",
                        (
                            reservation.call_count,
                            reservation.subscription_id,
                            reservation.call_count,
                        ),
                    )
                    if released.rowcount != 1:
                        raise RuntimeError("quota reservation ledger is inconsistent")
                self._insert_usage_event(
                    conn,
                    user_id,
                    api_key_id,
                    endpoint,
                    method,
                    status_code,
                    latency_ms,
                    input_tokens,
                    output_tokens,
                    meta,
                    reservation.call_count if successful_billing else 0,
                )
            reservation.settled = True
            return

        # Compatibility path for older callers. Only successful calls consume
        # quota, and the conditional UPDATE closes the old overdraw race.
        with self.connect() as conn:
            if successful_billing:
                updated = conn.execute(
                    """UPDATE subscriptions
                       SET used_calls = used_calls + ?
                       WHERE id = (
                           SELECT id FROM (
                               SELECT id FROM subscriptions
                               WHERE user_id = ? AND status = 'active'
                                 AND starts_at <= ? AND ends_at > ?
                               ORDER BY ends_at DESC LIMIT 1
                           ) AS active_subscription
                       ) AND used_calls + ? <= call_quota""",
                    (call_count, user_id, now, now, call_count),
                )
                if updated.rowcount != 1:
                    raise PermissionError("套餐剩余调用次数不足")
            self._insert_usage_event(
                conn,
                user_id,
                api_key_id,
                endpoint,
                method,
                status_code,
                latency_ms,
                input_tokens,
                output_tokens,
                meta,
                call_count,
            )

    def reconcile_abandoned_reservations(
        self,
        older_than_seconds: int = 6 * 60 * 60,
        batch_size: int = 100,
    ) -> int:
        """Refund expired reservations left by terminated workers exactly once."""
        if older_than_seconds < 0:
            raise ValueError("older_than_seconds must not be negative")
        batch_size = max(1, min(int(batch_size), 1000))
        cutoff = to_iso(utc_now() - timedelta(seconds=older_than_seconds))
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id, subscription_id, call_count
                   FROM call_reservations
                   WHERE state = 'reserved' AND created_at <= ?
                   ORDER BY id LIMIT ?""",
                (cutoff, batch_size),
            ).fetchall()
        refunded = 0
        for row in rows:
            with self.connect() as conn:
                now = to_iso(utc_now())
                claimed = conn.execute(
                    """UPDATE call_reservations
                       SET state = 'expired', settled_at = ?
                       WHERE id = ? AND state = 'reserved'""",
                    (now, row["id"]),
                )
                if claimed.rowcount != 1:
                    continue
                released = conn.execute(
                    """UPDATE subscriptions SET used_calls = used_calls - ?
                       WHERE id = ? AND used_calls >= ?""",
                    (row["call_count"], row["subscription_id"], row["call_count"]),
                )
                if released.rowcount != 1:
                    raise RuntimeError("abandoned reservation ledger is inconsistent")
                refunded += 1
        return refunded

    @staticmethod
    def _insert_usage_event(
        conn: Connection,
        user_id: int,
        api_key_id: int | None,
        endpoint: str,
        method: str,
        status_code: int,
        latency_ms: int,
        input_tokens: int,
        output_tokens: int,
        meta: dict | None,
        call_count: int,
    ) -> None:
        conn.execute(
            """INSERT INTO usage_events
               (user_id, api_key_id, endpoint, method, call_count, input_tokens,
                output_tokens, status_code, latency_ms, meta_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                api_key_id,
                endpoint,
                method,
                call_count,
                input_tokens,
                output_tokens,
                status_code,
                latency_ms,
                json.dumps(meta or {}, ensure_ascii=False),
                to_iso(utc_now()),
            ),
        )

    def _reserve_call_in_connection(
        self,
        conn: Connection,
        user_id: int,
        call_count: int,
        now: str,
    ) -> CallReservation:
        if call_count < 1:
            raise ValueError("call_count must be positive")
        row = conn.execute(
            """SELECT id FROM subscriptions
               WHERE user_id = ? AND status = 'active' AND starts_at <= ? AND ends_at > ?
               ORDER BY ends_at DESC LIMIT 1""",
            (user_id, now, now),
        ).fetchone()
        if not row:
            raise PermissionError("暂无有效订阅，请先开通套餐")
        subscription_id = int(row["id"])
        updated = conn.execute(
            """UPDATE subscriptions SET used_calls = used_calls + ?
                   WHERE id = ? AND user_id = ? AND status = 'active'
                     AND starts_at <= ? AND ends_at > ? AND used_calls + ? <= call_quota""",
            (call_count, subscription_id, user_id, now, now, call_count),
        )
        if updated.rowcount != 1:
            raise PermissionError("套餐剩余调用次数不足")
        reservation_id = secrets.token_hex(16)
        conn.execute(
            """INSERT INTO call_reservations
               (reservation_id, user_id, subscription_id, call_count, state, created_at)
               VALUES (?, ?, ?, ?, 'reserved', ?)""",
            (reservation_id, user_id, subscription_id, call_count, now),
        )
        return CallReservation(
            user_id=user_id,
            subscription_id=subscription_id,
            call_count=call_count,
            reservation_id=reservation_id,
        )

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT u.*,
                       COALESCE(SUM(CASE WHEN s.status='active' THEN s.call_quota ELSE 0 END), 0) AS total_quota,
                       COALESCE(SUM(CASE WHEN s.status='active' THEN s.used_calls ELSE 0 END), 0) AS total_used
                FROM users u
                LEFT JOIN subscriptions s ON s.user_id = u.id
                GROUP BY u.id
                ORDER BY u.created_at DESC
                """
            ).fetchall()
            return [self._user_public(row) | {"total_quota": row["total_quota"], "total_used": row["total_used"]} for row in rows]

    def usage_summary(self, user_id: int | None = None, days: int = 30) -> dict[str, Any]:
        since = to_iso(utc_now() - timedelta(days=max(1, min(days, 365))))
        params: list[Any] = [since]
        user_filter = ""
        if user_id is not None:
            user_filter = "AND user_id = ?"
            params.append(user_id)
        with self.connect() as conn:
            totals = conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(call_count), 0) AS calls,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
                FROM usage_events
                WHERE created_at >= ? {user_filter}
                """,
                params,
            ).fetchone()
            endpoints = conn.execute(
                f"""
                SELECT endpoint,
                       COUNT(*) AS requests,
                       COALESCE(SUM(call_count), 0) AS calls,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
                FROM usage_events
                WHERE created_at >= ? {user_filter}
                GROUP BY endpoint
                ORDER BY calls DESC
                """,
                params,
            ).fetchall()
            return {
                "days": days,
                "totals": dict(totals),
                "endpoints": [dict(row) for row in endpoints],
            }

    @staticmethod
    def is_valid_cn_phone(phone: str) -> bool:
        return len(phone) == 11 and phone.startswith("1") and phone.isdigit()

    def create_slider_challenge(
        self,
        challenge_id: str,
        target_hash: str,
        expires_at: datetime,
        ip_address: str,
        phone: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO slider_challenges (id, target_hash, expires_at, used_at, ip_address, phone, created_at)
                VALUES (?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    challenge_id,
                    target_hash,
                    to_iso(expires_at),
                    ip_address,
                    normalize_phone(phone or ""),
                    to_iso(utc_now()),
                ),
            )

    def verify_slider_challenge(self, challenge_id: str, accepted_hashes: list[str], ip_address: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM slider_challenges WHERE id = ?", (challenge_id,)).fetchone()
            if not row or row["used_at"]:
                return False
            expires_at = from_iso(row["expires_at"])
            if not expires_at or expires_at <= utc_now():
                return False
            if str(row["ip_address"]) != ip_address:
                return False
            if str(row["target_hash"]) not in set(accepted_hashes):
                return False
            conn.execute("UPDATE slider_challenges SET used_at = ? WHERE id = ?", (to_iso(utc_now()), challenge_id))
            return True

    def sms_send_allowed(
        self,
        phone: str,
        ip_address: str,
        daily_phone_limit: int,
        hourly_ip_limit: int,
    ) -> tuple[bool, str]:
        phone_norm = normalize_phone(phone)
        with self.connect() as conn:
            latest = conn.execute(
                """
                SELECT created_at FROM sms_verification_codes
                WHERE phone = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (phone_norm,),
            ).fetchone()
            if latest:
                latest_at = from_iso(latest["created_at"])
                if latest_at and (utc_now() - latest_at).total_seconds() < 60:
                    return False, "同一手机号 60 秒内不能重复发送验证码"

            day_since = to_iso(utc_now() - timedelta(days=1))
            day_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sms_verification_codes WHERE phone = ? AND created_at >= ?",
                    (phone_norm, day_since),
                ).fetchone()[0]
            )
            if day_count >= daily_phone_limit:
                return False, "该手机号今日验证码发送次数已达上限"

            hour_since = to_iso(utc_now() - timedelta(hours=1))
            ip_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sms_verification_codes WHERE ip_address = ? AND created_at >= ?",
                    (ip_address, hour_since),
                ).fetchone()[0]
            )
            if ip_count >= hourly_ip_limit:
                return False, "当前 IP 发送验证码过于频繁，请稍后再试"
            return True, ""

    def store_sms_code(
        self,
        phone: str,
        code: str,
        purpose: str,
        expires_at: datetime,
        ip_address: str,
        provider_message_id: str | None = None,
    ) -> None:
        salt, code_hash = hash_password(code)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sms_verification_codes
                    (phone, code_salt, code_hash, purpose, expires_at, used_at, attempts,
                     locked_until, ip_address, provider_message_id, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?, ?)
                """,
                (
                    normalize_phone(phone),
                    salt,
                    code_hash,
                    purpose,
                    to_iso(expires_at),
                    ip_address,
                    provider_message_id,
                    to_iso(utc_now()),
                ),
            )

    def verify_sms_code(self, phone: str, code: str, purpose: str, lock_seconds: int) -> tuple[bool, str]:
        phone_norm = normalize_phone(phone)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM sms_verification_codes
                WHERE phone = ? AND purpose = ? AND used_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (phone_norm, purpose),
            ).fetchone()
            if not row:
                return False, "验证码不存在或已使用"
            lock_until = from_iso(row["locked_until"])
            if lock_until and lock_until > utc_now():
                return False, "验证码连续输错次数过多，请稍后再试"
            expires_at = from_iso(row["expires_at"])
            if not expires_at or expires_at <= utc_now():
                return False, "验证码已过期"

            ok = verify_password(code, row["code_salt"], row["code_hash"])
            if ok:
                conn.execute("UPDATE sms_verification_codes SET used_at = ? WHERE id = ?", (to_iso(utc_now()), row["id"]))
                return True, ""

            attempts = int(row["attempts"]) + 1
            locked_until = to_iso(utc_now() + timedelta(seconds=lock_seconds)) if attempts >= 5 else None
            conn.execute(
                "UPDATE sms_verification_codes SET attempts = ?, locked_until = ? WHERE id = ?",
                (attempts, locked_until, row["id"]),
            )
            if attempts >= 5:
                return False, "验证码连续输错 5 次，已临时锁定"
            return False, f"验证码错误，还可尝试 {5 - attempts} 次"

    def get_user_by_id(self, user_id: int, conn: Connection | None = None) -> dict[str, Any] | None:
        own_conn = conn is None
        if own_conn:
            ctx = self.connect()
            conn = ctx.__enter__()
        try:
            if conn is None:
                raise RuntimeError("BillingStore connection is unavailable")
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return self._user_public(row) if row else None
        finally:
            if own_conn:
                ctx.__exit__(None, None, None)

    def get_subscription(self, subscription_id: int, conn: Connection | None = None) -> dict[str, Any]:
        own_conn = conn is None
        if own_conn:
            ctx = self.connect()
            conn = ctx.__enter__()
        try:
            if conn is None:
                raise RuntimeError("BillingStore connection is unavailable")
            row = conn.execute(
                """
                SELECT s.*, p.name AS plan_name, p.price_cents, p.billing_period
                FROM subscriptions s
                JOIN plans p ON p.code = s.plan_code
                WHERE s.id = ?
                """,
                (subscription_id,),
            ).fetchone()
            if not row:
                raise ValueError("订阅不存在")
            return self._subscription_public(row)
        finally:
            if own_conn:
                ctx.__exit__(None, None, None)

    @staticmethod
    def _user_public(row: Row) -> dict[str, Any]:
        email = str(row["email"])
        phone = row["phone"] if "phone" in row.keys() else None
        return {
            "id": int(row["id"]),
            "email": "" if email.endswith("@phone.local") else email,
            "phone": phone,
            "phone_masked": mask_phone(phone),
            "is_admin": bool(row["is_admin"]),
            "is_active": bool(row["is_active"]),
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _api_key_public(row: Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "key_prefix": str(row["key_prefix"]),
            "api_key": str(row["raw_key"] or "") if "raw_key" in row.keys() else "",
            "can_copy": bool(row["raw_key"] if "raw_key" in row.keys() else ""),
            "is_active": bool(row["is_active"]),
            "created_at": str(row["created_at"]),
            "last_used_at": row["last_used_at"],
        }

    @staticmethod
    def _knowledge_base_public(row: Row) -> dict[str, Any]:
        keys = row.keys()
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "name": str(row["name"]),
            "description": str(row["description"] or ""),
            "is_default": bool(row["is_default"]),
            "is_active": bool(row["is_active"]),
            "document_count": int(row["document_count"]) if "document_count" in keys else 0,
            "vector_count": int(row["vector_count"]) if "vector_count" in keys else 0,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _knowledge_document_public(row: Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "knowledge_base_id": int(row["knowledge_base_id"]),
            "run_id": str(row["run_id"]),
            "source_filename": str(row["source_filename"]),
            "source_type": str(row["source_type"] or ""),
            "status": str(row["status"]),
            "vector_count": int(row["vector_count"]),
            "text_block_count": int(row["text_block_count"]),
            "image_count": int(row["image_count"]),
            "artifact_path": str(row["artifact_path"] or ""),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _rag_query_log_public(row: Row) -> dict[str, Any]:
        try:
            citations = json.loads(str(row["citations_json"] or "[]"))
        except Exception:
            citations = []
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "api_key_id": int(row["api_key_id"]) if row["api_key_id"] is not None else None,
            "knowledge_base_id": int(row["knowledge_base_id"]),
            "query": str(row["query"]),
            "answer": str(row["answer"]),
            "retrieval_mode": str(row["retrieval_mode"]),
            "top_k": int(row["top_k"]),
            "source_filename": row["source_filename"],
            "hit_count": int(row["hit_count"]),
            "citations": citations if isinstance(citations, list) else [],
            "citation_count": len(citations) if isinstance(citations, list) else 0,
            "feedback_rating": row["feedback_rating"],
            "feedback_note": row["feedback_note"],
            "feedback_at": row["feedback_at"],
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _subscription_public(row: Row) -> dict[str, Any]:
        quota = int(row["call_quota"])
        used = int(row["used_calls"])
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "plan_code": str(row["plan_code"]),
            "plan_name": str(row["plan_name"]),
            "billing_period": str(row["billing_period"]),
            "status": str(row["status"]),
            "starts_at": str(row["starts_at"]),
            "ends_at": str(row["ends_at"]),
            "call_quota": quota,
            "used_calls": used,
            "remaining_calls": max(0, quota - used),
            "price_cents": int(row["price_cents"]),
            "price_yuan": round(int(row["price_cents"]) / 100, 2),
            "created_at": str(row["created_at"]),
        }
