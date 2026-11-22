from __future__ import annotations

import sqlite3

import pytest

from app.mysql import raw_connection
from app.billing import BillingStore
from app.mysql_schema import BILLING_SCHEMA, WEWE_SCHEMA
from app.sqlite_migration import TABLES, MigrationError, _declared_target_columns, migrate_sqlite
from app.wewe_store import WeWeStore


def _legacy_database(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
            password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE TABLE knowledge_bases (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            is_default INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE wewe_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            knowledge_base_id INTEGER NOT NULL, source_type TEXT NOT NULL,
            upstream_feed_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL,
            intro TEXT NOT NULL DEFAULT '', cover_url TEXT NOT NULL DEFAULT '',
            target_url TEXT NOT NULL DEFAULT '', subscribed INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active', last_sync_at TEXT,
            last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE (user_id, source_type, upstream_feed_id, target_url)
        );
        CREATE TABLE wewe_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL, knowledge_base_id INTEGER NOT NULL,
            upstream_article_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '', cover_url TEXT NOT NULL DEFAULT '',
            content_html TEXT NOT NULL DEFAULT '', text_content TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
            chunk_count INTEGER NOT NULL DEFAULT 0, image_count INTEGER NOT NULL DEFAULT 0,
            indexed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE (user_id, upstream_article_id)
        );
        CREATE TABLE wewe_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT, article_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, url TEXT NOT NULL, local_path TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT '', byte_size INTEGER NOT NULL DEFAULT 0,
            description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(article_id, url)
        );
        INSERT INTO users VALUES
            (1, '中文用户@example.com', 'salt', 'hash', 0, 1, '2026-01-01T00:00:00+00:00');
        INSERT INTO knowledge_bases VALUES
            (1, 1, '默认知识库', '中文描述', 1, 1, '2026-01-01', '2026-01-01');
        INSERT INTO wewe_sources VALUES
            (1, 1, 1, 'mp', 'OLD', '旧来源', '简介', '', '', 1, 'active', NULL, '',
             '2026-01-01', '2026-01-01');
        INSERT INTO wewe_articles VALUES
            (1, 1, 1, 1, 'legacy-article', '旧文章', 'https://example.com/a', '', '', '',
             '<p>旧文与中文内容</p>', '旧文与中文内容', 'indexed', '', 1, 1,
             '2026-01-01', '2026-01-01', '2026-01-01');
        INSERT INTO wewe_images VALUES
            (1, 1, 1, 'https://img.example.com/' || printf('%.*c', 1800, 'x') || '/图.png',
             '/srv/only/local.png', 'image/png', 123, '图片描述', 'described', '',
             '2026-01-01', '2026-01-01');
        """
    )
    conn.commit()
    conn.close()


def _empty_target_schema(settings) -> None:
    with raw_connection(settings) as conn:
        with conn.cursor() as cur:
            for statement in BILLING_SCHEMA + WEWE_SCHEMA:
                cur.execute(statement)
        conn.commit()


def _legacy_database_with_unknown_article_field(path) -> None:
    _legacy_database(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "ALTER TABLE wewe_articles "
        "ADD COLUMN legacy_business_field TEXT NOT NULL DEFAULT 'must not be lost'"
    )
    conn.commit()
    conn.close()


def test_sqlite_dry_run_apply_resume_and_hash_verification(tmp_path, mysql_settings):
    source = tmp_path / "legacy.db"
    _legacy_database(source)

    dry = migrate_sqlite(source, mysql_settings, apply=False)
    assert dry["verified"] is True
    assert dry["applied"] is False
    assert dry["tables"]["users"]["source_rows"] == 1
    assert dry["tables"]["wewe_images"]["source_rows"] == 1
    with raw_connection(mysql_settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name='users'",
                (mysql_settings.mysql_database,),
            )
            assert int(cur.fetchone()["n"]) == 0

    applied = migrate_sqlite(source, mysql_settings, apply=True)
    assert applied["applied"] is True
    assert applied["verified"] is True
    assert applied["tables"]["wewe_articles"]["row_hash"]
    assert applied["tables"]["wewe_articles"]["source_rows"] == 1

    store = WeWeStore(mysql_settings)
    assert store.get_source(1, 1)["name"] == "旧来源"
    assert store.get_article(1, 1)["text_content"] == "旧文与中文内容"
    images = store.list_images(1)
    assert len(images) == 1
    assert images[0]["url"].endswith("/图.png")
    assert len(images[0]["url"]) > 1800

    second = store.add_source(
        user_id=1, knowledge_base_id=1, source_type="mp", name="新来源", upstream_feed_id="NEW"
    )
    article_id, created = store.upsert_article(
        user_id=1,
        source_id=second,
        knowledge_base_id=1,
        upstream_article_id="legacy-article",
        title="同 ID 新文章",
    )
    assert created is True
    assert article_id != 1

    resumed = migrate_sqlite(source, mysql_settings, apply=True, resume=True)
    assert resumed["verified"] is True


def test_sqlite_migration_refuses_to_overwrite_non_empty_target(tmp_path, mysql_settings):
    source = tmp_path / "legacy.db"
    _legacy_database(source)
    migrate_sqlite(source, mysql_settings, apply=True)
    with pytest.raises(MigrationError, match="non-empty"):
        migrate_sqlite(source, mysql_settings, apply=True, resume=False)


def test_sqlite_migration_resume_detects_existing_content_mismatch(tmp_path, mysql_settings):
    source = tmp_path / "legacy.db"
    _legacy_database(source)
    migrate_sqlite(source, mysql_settings, apply=True)
    with raw_connection(mysql_settings) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE wewe_articles SET title='被人工修改' WHERE id=1")
        conn.commit()
    with pytest.raises(MigrationError, match="hash mismatch"):
        migrate_sqlite(source, mysql_settings, apply=True, resume=True)


def test_sqlite_migration_rejects_missing_target_columns_before_writing(tmp_path, mysql_settings):
    source = tmp_path / "legacy-with-business-field.db"
    _legacy_database_with_unknown_article_field(source)
    _empty_target_schema(mysql_settings)

    with pytest.raises(MigrationError, match="wewe_articles.*legacy_business_field"):
        migrate_sqlite(source, mysql_settings, apply=True)

    with raw_connection(mysql_settings) as conn:
        with conn.cursor() as cur:
            for table in TABLES:
                cur.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
                assert int(cur.fetchone()["n"]) == 0, table


def test_sqlite_migration_dry_run_reports_missing_target_columns(tmp_path, mysql_settings):
    source = tmp_path / "legacy-with-business-field.db"
    _legacy_database_with_unknown_article_field(source)
    _empty_target_schema(mysql_settings)

    dry = migrate_sqlite(source, mysql_settings, apply=False)

    assert dry["verified"] is False
    assert dry["tables"]["wewe_articles"]["missing_target_columns"] == [
        "legacy_business_field"
    ]
    assert any(
        "wewe_articles" in issue and "legacy_business_field" in issue
        for issue in dry["incompatibilities"]
    )


def test_sqlite_fresh_dry_run_checks_intended_schema_without_creating_tables(
    tmp_path, mysql_settings
):
    source = tmp_path / "fresh-legacy-with-business-field.db"
    _legacy_database_with_unknown_article_field(source)

    dry = migrate_sqlite(source, mysql_settings, apply=False)

    assert dry["verified"] is False
    assert "legacy_business_field" in dry["tables"]["wewe_articles"]["missing_target_columns"]
    with raw_connection(mysql_settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=%s",
                (mysql_settings.mysql_database,),
            )
            assert int(cur.fetchone()["n"]) == 0


def test_sqlite_migration_allows_target_only_columns_with_defaults(tmp_path, mysql_settings):
    source = tmp_path / "legacy.db"
    _legacy_database(source)
    _empty_target_schema(mysql_settings)
    with raw_connection(mysql_settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE users ADD COLUMN target_only_field "
                "VARCHAR(32) NOT NULL DEFAULT 'target-default'"
            )
        conn.commit()

    dry = migrate_sqlite(source, mysql_settings, apply=False)
    assert dry["verified"] is True
    assert dry["tables"]["users"]["missing_target_columns"] == []

    applied = migrate_sqlite(source, mysql_settings, apply=True)
    assert applied["verified"] is True
    with raw_connection(mysql_settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT target_only_field FROM users WHERE id=1")
            assert cur.fetchone()["target_only_field"] == "target-default"


def test_sqlite_fresh_migration_allows_only_application_plan_seeds(tmp_path, mysql_settings):
    source = tmp_path / "legacy.db"
    _legacy_database(source)
    BillingStore(mysql_settings)  # application startup creates exactly four plan seeds

    applied = migrate_sqlite(source, mysql_settings, apply=True)
    assert applied["verified"] is True
    assert applied["tables"]["plans"]["target_rows_after"] == 4


def test_sqlite_fresh_migration_rejects_extra_plan_business_data(tmp_path, mysql_settings):
    source = tmp_path / "legacy.db"
    _legacy_database(source)
    store = BillingStore(mysql_settings)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO plans (code, name, billing_period, price_cents, included_calls, sort_order) "
            "VALUES ('unexpected-plan', '额外套餐', 'month', 100, 1, 99)"
        )
    with pytest.raises(MigrationError, match="non-empty: plans"):
        migrate_sqlite(source, mysql_settings, apply=True)


def test_declared_target_columns_include_backtick_quoted_query():
    columns = _declared_target_columns("rag_query_logs")
    assert "query" in columns
    assert "answer" in columns
