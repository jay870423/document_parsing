from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


class BillingStore:
    def __init__(self, db_path: str = "data/app.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
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
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    phone TEXT UNIQUE,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    raw_key TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT
                );

                CREATE TABLE IF NOT EXISTS plans (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    billing_period TEXT NOT NULL,
                    price_cents INTEGER NOT NULL,
                    included_calls INTEGER NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    plan_code TEXT NOT NULL REFERENCES plans(code),
                    status TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    call_quota INTEGER NOT NULL,
                    used_calls INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    api_key_id INTEGER,
                    endpoint TEXT NOT NULL,
                    method TEXT NOT NULL,
                    call_count INTEGER NOT NULL DEFAULT 1,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    status_code INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    knowledge_base_id INTEGER NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    vector_count INTEGER NOT NULL DEFAULT 0,
                    text_block_count INTEGER NOT NULL DEFAULT 0,
                    image_count INTEGER NOT NULL DEFAULT 0,
                    artifact_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rag_query_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    api_key_id INTEGER,
                    knowledge_base_id INTEGER NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
                    query TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    retrieval_mode TEXT NOT NULL,
                    top_k INTEGER NOT NULL,
                    source_filename TEXT,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    feedback_rating TEXT,
                    feedback_note TEXT,
                    feedback_at TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS slider_challenges (
                    id TEXT PRIMARY KEY,
                    target_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    ip_address TEXT NOT NULL,
                    phone TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sms_verification_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    code_salt TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until TEXT,
                    ip_address TEXT NOT NULL,
                    provider_message_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agreement_consents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    terms_version TEXT NOT NULL,
                    privacy_version TEXT NOT NULL,
                    document_hash TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    user_agent TEXT NOT NULL,
                    accepted_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS purchase_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    plan_code TEXT NOT NULL REFERENCES plans(code),
                    status TEXT NOT NULL DEFAULT 'pending',
                    contact_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "users", "phone", "TEXT")
            self._ensure_column(conn, "api_keys", "raw_key", "TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON users(phone) WHERE phone IS NOT NULL AND phone != ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_bases_user ON knowledge_bases(user_id, is_active, created_at)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_bases_default ON knowledge_bases(user_id) WHERE is_default = 1 AND is_active = 1"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_documents_kb ON knowledge_documents(user_id, knowledge_base_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rag_query_logs_user ON rag_query_logs(user_id, knowledge_base_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_purchase_intents_user ON purchase_intents(user_id, created_at)"
            )
            self._seed_plans(conn)

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _seed_plans(self, conn: sqlite3.Connection) -> None:
        plans = [
            ("free_trial", "新人免费体验包", "trial", 0, 3, 0),
            ("personal_light", "个人轻量版", "month", 990, 100, 10),
            ("personal_standard", "个人标准版", "month", 1990, 300, 20),
            ("personal_pro", "专业增强版", "month", 4990, 1000, 30),
        ]
        for code, name, period, price, included, sort_order in plans:
            conn.execute(
                """
                INSERT INTO plans (code, name, billing_period, price_cents, included_calls, sort_order)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name=excluded.name,
                    billing_period=excluded.billing_period,
                    price_cents=excluded.price_cents,
                    included_calls=excluded.included_calls,
                    sort_order=excluded.sort_order
                """,
                (code, name, period, price, included, sort_order),
            )
        active_codes = [code for code, *_ in plans]
        placeholders = ",".join("?" for _ in active_codes)
        conn.execute(
            f"UPDATE plans SET is_active = 0 WHERE code NOT IN ({placeholders})",
            active_codes,
        )

    def _grant_signup_trial(self, conn: sqlite3.Connection, user_id: int) -> None:
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
            except sqlite3.IntegrityError as exc:
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
            except sqlite3.IntegrityError as exc:
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

    def ensure_default_knowledge_base(self, user_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        own_conn = conn is None
        if own_conn:
            ctx = self.connect()
            conn = ctx.__enter__()
        try:
            assert conn is not None
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

    def get_active_subscription(self, user_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        own_conn = conn is None
        if own_conn:
            ctx = self.connect()
            conn = ctx.__enter__()
        try:
            assert conn is not None
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
        sub = self.get_active_subscription(user_id)
        if not sub:
            raise PermissionError("当前账号没有可用订阅")
        if int(sub["remaining_calls"]) <= 0:
            raise PermissionError("当前订阅调用次数已用完")
        return sub

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
        meta: dict[str, Any] | None = None,
        call_count: int = 1,
    ) -> None:
        with self.connect() as conn:
            sub = self.get_active_subscription(user_id, conn=conn)
            if sub and 200 <= status_code < 400:
                conn.execute(
                    "UPDATE subscriptions SET used_calls = used_calls + ? WHERE id = ?",
                    (call_count, int(sub["id"])),
                )
            conn.execute(
                """
                INSERT INTO usage_events
                    (user_id, api_key_id, endpoint, method, call_count, input_tokens, output_tokens,
                     status_code, latency_ms, meta_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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

    def get_user_by_id(self, user_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        own_conn = conn is None
        if own_conn:
            ctx = self.connect()
            conn = ctx.__enter__()
        try:
            assert conn is not None
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return self._user_public(row) if row else None
        finally:
            if own_conn:
                ctx.__exit__(None, None, None)

    def get_subscription(self, subscription_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        own_conn = conn is None
        if own_conn:
            ctx = self.connect()
            conn = ctx.__enter__()
        try:
            assert conn is not None
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
    def _user_public(row: sqlite3.Row) -> dict[str, Any]:
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
    def _api_key_public(row: sqlite3.Row) -> dict[str, Any]:
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
    def _knowledge_base_public(row: sqlite3.Row) -> dict[str, Any]:
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
    def _knowledge_document_public(row: sqlite3.Row) -> dict[str, Any]:
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
    def _rag_query_log_public(row: sqlite3.Row) -> dict[str, Any]:
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
    def _subscription_public(row: sqlite3.Row) -> dict[str, Any]:
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
