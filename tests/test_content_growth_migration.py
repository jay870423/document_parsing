from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_production_main_never_auto_migrates_growth_schema():
    tree = ast.parse((ROOT / "app/main.py").read_text())
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "ContentGrowthStore"
    ]
    assert len(calls) == 1
    keywords = {item.arg: item.value for item in calls[0].keywords}
    assert isinstance(keywords.get("auto_migrate"), ast.Constant)
    assert keywords["auto_migrate"].value is False


def test_one_off_growth_migration_is_real_and_idempotent(wewe_settings):
    script = ROOT / "scripts/migrate_content_growth.py"
    env = os.environ.copy()
    env.update({
        "MYSQL_HOST": wewe_settings.mysql_host,
        "MYSQL_PORT": str(wewe_settings.mysql_port),
        "MYSQL_USER": wewe_settings.mysql_user,
        "MYSQL_DATABASE": wewe_settings.mysql_database,
        "MYSQL_PASSWORD": wewe_settings.mysql_password,
        "MYSQL_UNIX_SOCKET": wewe_settings.mysql_unix_socket,
    })
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(script)], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
    import pymysql
    conn = pymysql.connect(
        host=wewe_settings.mysql_host,
        port=wewe_settings.mysql_port,
        user=wewe_settings.mysql_user,
        password=wewe_settings.mysql_password,
        database=wewe_settings.mysql_database,
        unix_socket=wewe_settings.mysql_unix_socket or None,
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema=%s AND table_name LIKE %s""",
                (wewe_settings.mysql_database, "growth\\_%"),
            )
            tables = {row[0] for row in cur.fetchall()}
            assert tables == {
                "growth_profiles", "growth_own_articles", "growth_briefs",
                "growth_drafts", "growth_metrics", "growth_operations",
            }
            cur.execute(
                """SELECT DISTINCT column_name FROM information_schema.statistics
                   WHERE table_schema=%s AND table_name='growth_operations'
                     AND index_name='uq_growth_operation' ORDER BY column_name""",
                (wewe_settings.mysql_database,),
            )
            assert {row[0] for row in cur.fetchall()} == {"user_id", "endpoint", "operation_id"}
    finally:
        conn.close()
