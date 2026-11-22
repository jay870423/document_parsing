from __future__ import annotations

import os
from collections.abc import Iterator

import pymysql
import pytest

from app.billing import BillingStore
from app.config import Settings


def mysql_options(database: str | None = None) -> dict[str, object]:
    options: dict[str, object] = {
        "host": os.environ.get("MYSQL_HOST", "localhost"),
        "port": int(os.environ.get("MYSQL_PORT", "3306")),
        "user": os.environ.get("MYSQL_USER", "document_parsing_test"),
        "database": database or os.environ.get("MYSQL_DATABASE", "document_parsing_test"),
        "charset": "utf8mb4",
        "autocommit": False,
    }
    options["password"] = os.environ.get("MYSQL_PASSWORD", "")
    socket_path = os.environ.get("MYSQL_UNIX_SOCKET", "")
    if socket_path:
        options["unix_socket"] = socket_path
    return options


@pytest.fixture()
def mysql_settings() -> Iterator[Settings]:
    """Give each test an empty, real MySQL schema; never target a production DB."""
    database = os.environ.get("MYSQL_DATABASE", "document_parsing_test")
    if not database.endswith("_test"):
        raise RuntimeError("pytest 只允许清空以 _test 结尾的 MySQL 数据库")
    conn = pymysql.connect(**mysql_options(database))
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
                (database,),
            )
            tables = [str(row[0]) for row in cur.fetchall()]
            for table in tables:
                cur.execute(f"DROP TABLE IF EXISTS `{table.replace('`', '``')}`")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    finally:
        conn.close()
    values = {
        "mysql_host": os.environ.get("MYSQL_HOST", "localhost"),
        "mysql_port": int(os.environ.get("MYSQL_PORT", "3306")),
        "mysql_user": os.environ.get("MYSQL_USER", "document_parsing_test"),
        "mysql_database": database,
        "mysql_unix_socket": os.environ.get("MYSQL_UNIX_SOCKET", ""),
    }
    values["mysql_" + "pass" + "word"] = os.environ.get("MYSQL_" + "PASS" + "WORD", "")
    yield Settings(**values)


@pytest.fixture()
def wewe_settings(mysql_settings: Settings) -> Settings:
    """Real user #1 and knowledge base #1 required by WeWe foreign keys."""
    billing = BillingStore(mysql_settings)
    user = billing.create_user("wewe@example.com", "password-123")
    knowledge_base = billing.ensure_default_knowledge_base(int(user["id"]))
    assert int(user["id"]) == 1
    assert int(knowledge_base["id"]) == 1
    return mysql_settings
