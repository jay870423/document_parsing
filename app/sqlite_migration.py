"""Conservative, offline SQLite -> MySQL migration for document-parsing only.

SQLite is opened read-only.  The target is never overwritten: a fresh apply
requires empty target tables; resume inserts missing rows and verifies every
source row by primary key and a canonical SHA-256 row hash.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from app.mysql import raw_connection
from app.billing import PLAN_SEEDS
from app.mysql_schema import BILLING_SCHEMA, WEWE_SCHEMA
from app.wewe_store import source_identity

TABLES = (
    "users",
    "sessions",
    "api_keys",
    "plans",
    "subscriptions",
    "call_reservations",
    "usage_events",
    "knowledge_bases",
    "knowledge_documents",
    "rag_query_logs",
    "slider_challenges",
    "sms_verification_codes",
    "agreement_consents",
    "purchase_intents",
    "wewe_sources",
    "wewe_articles",
    "wewe_images",
    "wewe_collect_tasks",
)


class MigrationError(RuntimeError):
    pass


def _primary_key(table: str) -> str:
    return "code" if table == "plans" else "id"


def _normalise(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _row_hash(rows, columns: list[str]) -> str:
    canonical = [
        {column: _normalise(row[column]) for column in columns}
        for row in rows
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _open_source(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise MigrationError(f"SQLite source does not exist: {path}")
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise MigrationError(f"SQLite integrity_check failed: {integrity}")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise MigrationError(f"SQLite foreign_key_check failed: {len(violations)} violation(s)")
        return conn
    except sqlite3.Error as exc:
        raise MigrationError(f"cannot open SQLite source read-only: {exc}") from exc


def _source_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _source_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if table not in TABLES:
        raise MigrationError("untrusted table identifier")
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _target_columns(raw, database: str, table: str) -> list[str]:
    with raw.cursor() as cur:
        cur.execute(
            """SELECT column_name AS name FROM information_schema.columns
               WHERE table_schema=%s AND table_name=%s AND generation_expression = ''
               ORDER BY ordinal_position""",
            (database, table),
        )
        return [str(row["name"]) for row in cur.fetchall()]


def _declared_target_columns(table: str) -> list[str]:
    """Read the controlled CREATE TABLE declaration without writing during dry-run."""
    if table not in TABLES:
        raise MigrationError("untrusted table identifier")
    statement = next(
        (
            item for item in BILLING_SCHEMA + WEWE_SCHEMA
            if re.search(
                rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?{re.escape(table)}`?",
                item,
                flags=re.IGNORECASE,
            )
        ),
        None,
    )
    if statement is None:
        raise MigrationError(f"target schema declaration is missing table: {table}")
    columns = []
    for raw_line in statement.splitlines():
        match = re.match(r"^\s*`?([a-z_][a-z0-9_]*)`?\s+([A-Z][A-Z0-9]*)\b", raw_line)
        if match and match.group(1) not in {
            "primary", "unique", "index", "constraint", "foreign", "check", "key"
        }:
            columns.append(match.group(1))
    if not columns:
        raise MigrationError(f"target schema declaration has no columns: {table}")
    return columns


def _target_column_incompatibilities(
    raw,
    database: str,
    source: sqlite3.Connection,
    existing: set[str],
    report: dict[str, Any],
    *,
    allow_absent_tables: bool,
) -> list[str]:
    """Reject every source column that cannot be represented by the target."""
    issues = []
    for table in TABLES:
        if table not in existing:
            continue
        target_columns = _target_columns(raw, database, table)
        if allow_absent_tables and not target_columns:
            # A fresh dry-run must validate against the schema apply would create,
            # while still leaving the target database completely untouched.
            target_columns = _declared_target_columns(table)
        missing = [
            column for column in _source_columns(source, table)
            if column not in target_columns
        ]
        report["tables"][table]["missing_target_columns"] = missing
        if missing:
            issues.append(
                f"{table}: target is missing source column(s): {', '.join(missing)}"
            )
    return issues


def _target_count(raw, database: str, table: str) -> int:
    if table not in TABLES:
        raise MigrationError("untrusted table identifier")
    if not _target_columns(raw, database, table):
        return 0
    with raw.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
        return int(cur.fetchone()["n"])


def _target_has_only_plan_seeds(raw: Any) -> bool:
    """Recognize application-created plans without allowing custom business rows."""
    columns = ("code", "name", "billing_period", "price_cents", "included_calls", "is_active", "sort_order")
    with raw.cursor() as cur:
        cur.execute("SELECT code, name, billing_period, price_cents, included_calls, is_active, sort_order FROM plans")
        rows = cur.fetchall()
    actual = sorted(
        tuple(
            int(row[column]) if column in {"price_cents", "included_calls", "is_active", "sort_order"}
            else str(row[column])
            for column in columns
        )
        for row in rows
    )
    expected = sorted(
        (code, name, period, int(price), int(included), 1, int(sort_order))
        for code, name, period, price, included, sort_order in PLAN_SEEDS
    )
    return actual == expected


def _read_source_rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return list(conn.execute(f"SELECT * FROM `{table}` ORDER BY `{_primary_key(table)}`").fetchall())


def _insert_rows(raw, database: str, table: str, rows: list[sqlite3.Row]) -> list[str]:
    if not rows:
        return _target_columns(raw, database, table)
    source_columns = list(rows[0].keys())
    # Preflight has already proved this is a subset. Never intersect here: if
    # the target changes concurrently, the INSERT must fail rather than drop data.
    columns = source_columns.copy()
    if table == "wewe_sources":
        columns.append("identity_hash")
    elif table == "wewe_images":
        columns.append("url_hash")
    columns = list(dict.fromkeys(columns))
    primary_key = _primary_key(table)
    if primary_key not in columns:
        raise MigrationError(f"{table}: source lacks primary key {primary_key}")

    values = []
    for source in rows:
        row = {column: source[column] for column in source_columns}
        if table == "wewe_sources":
            row["identity_hash"] = source_identity(
                int(row["user_id"]), str(row["source_type"]),
                str(row["upstream_feed_id"] or ""), str(row["target_url"] or ""),
            )
        elif table == "wewe_images":
            row["url_hash"] = hashlib.sha256(str(row["url"]).encode("utf-8")).hexdigest()
        values.append(tuple(row[column] for column in columns))

    quoted = ",".join(f"`{column}`" for column in columns)
    placeholders = ",".join(["%s"] * len(columns))
    sql = f"INSERT IGNORE INTO `{table}` ({quoted}) VALUES ({placeholders})"
    with raw.cursor() as cur:
        cur.executemany(sql, values)
    return [column for column in columns if column in source_columns]


def _verify_table(
    raw,
    table: str,
    source_rows: list[sqlite3.Row],
    columns: list[str],
    exact_count: bool,
) -> str:
    primary_key = _primary_key(table)
    quoted = ",".join(f"`{column}`" for column in columns)
    matched = []
    with raw.cursor() as cur:
        for source in source_rows:
            cur.execute(
                f"SELECT {quoted} FROM `{table}` WHERE `{primary_key}`=%s",
                (source[primary_key],),
            )
            target = cur.fetchone()
            if target is None:
                raise MigrationError(f"{table}: hash mismatch; source key {source[primary_key]!r} is missing")
            matched.append(target)
        if exact_count:
            cur.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
            target_count = int(cur.fetchone()["n"])
            if target_count != len(source_rows):
                raise MigrationError(
                    f"{table}: hash mismatch; source rows={len(source_rows)} target rows={target_count}"
                )
    source_hash = _row_hash(source_rows, columns)
    if source_hash != _row_hash(matched, columns):
        raise MigrationError(f"{table}: hash mismatch")
    return source_hash


def _verify_computed_hashes(raw, source_rows: dict[str, list[sqlite3.Row]]) -> None:
    checks = (("wewe_sources", "identity_hash"), ("wewe_images", "url_hash"))
    with raw.cursor() as cur:
        for table, column in checks:
            for source in source_rows.get(table, []):
                cur.execute(f"SELECT * FROM `{table}` WHERE id=%s", (source["id"],))
                target = cur.fetchone()
                if target is None:
                    raise MigrationError(f"{table}: computed hash row is missing")
                if table == "wewe_sources":
                    expected = source_identity(
                        int(source["user_id"]), str(source["source_type"]),
                        str(source["upstream_feed_id"] or ""), str(source["target_url"] or ""),
                    )
                else:
                    expected = hashlib.sha256(str(source["url"]).encode("utf-8")).hexdigest()
                if target[column] != expected:
                    raise MigrationError(f"{table}: computed hash mismatch for id={source['id']}")


def migrate_sqlite(
    sqlite_path: str | Path,
    settings: Any,
    *,
    apply: bool = False,
    resume: bool = False,
) -> dict[str, Any]:
    if resume and not apply:
        raise MigrationError("resume requires apply=True")
    source_path = Path(sqlite_path)
    source = _open_source(source_path)
    raw = raw_connection(settings)
    report: dict[str, Any] = {
        "source": str(source_path.resolve()),
        "database": settings.mysql_database,
        "applied": bool(apply),
        "resume": bool(resume),
        "verified": False,
        "incompatibilities": [],
        "tables": {},
    }
    try:
        existing = _source_tables(source)
        source_rows: dict[str, list[sqlite3.Row]] = {}
        for table in TABLES:
            rows = _read_source_rows(source, table) if table in existing else []
            source_rows[table] = rows
            report["tables"][table] = {
                "source_present": table in existing,
                "source_rows": len(rows),
                "target_rows_before": _target_count(raw, settings.mysql_database, table),
            }
        if not apply:
            report["incompatibilities"] = _target_column_incompatibilities(
                raw,
                settings.mysql_database,
                source,
                existing,
                report,
                allow_absent_tables=True,
            )
            report["verified"] = not report["incompatibilities"]
            return report

        with raw.cursor() as cur:
            for statement in BILLING_SCHEMA + WEWE_SCHEMA:
                cur.execute(statement)
        raw.commit()

        # Check the complete source before any business row is inserted. Target
        # columns absent from SQLite are intentionally allowed to use DB defaults.
        report["incompatibilities"] = _target_column_incompatibilities(
            raw,
            settings.mysql_database,
            source,
            existing,
            report,
            allow_absent_tables=False,
        )
        if report["incompatibilities"]:
            raise MigrationError(
                "incompatible target schema: " + "; ".join(report["incompatibilities"])
            )

        non_empty = []
        for table in TABLES:
            if not _target_count(raw, settings.mysql_database, table):
                continue
            if table == "plans" and _target_has_only_plan_seeds(raw):
                continue
            non_empty.append(table)
        if non_empty and not resume:
            raise MigrationError("target is non-empty: " + ", ".join(non_empty))
        try:
            for table in TABLES:
                rows = source_rows[table]
                if not rows and table not in existing:
                    report["tables"][table].update({
                        "row_hash": "",
                        "target_rows_after": _target_count(raw, settings.mysql_database, table),
                    })
                    continue
                columns = _insert_rows(raw, settings.mysql_database, table, rows)
                row_hash = _verify_table(
                    raw, table, rows, columns, exact_count=not resume and table != "plans"
                )
                report["tables"][table].update({
                    "row_hash": row_hash,
                    "target_rows_after": _target_count(raw, settings.mysql_database, table),
                })
            _verify_computed_hashes(raw, source_rows)
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        report["verified"] = True
        return report
    except MigrationError:
        raise
    except Exception as exc:
        raw.rollback()
        raise MigrationError(f"migration failed: {exc}") from exc
    finally:
        source.close()
        raw.close()
