import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_app_can_reach_doc_only_mysql_socket_without_restarting_sidecars():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    app = compose.split("  app:", 1)[1].split("  qdrant:", 1)[0]
    assert "/var/run/mysqld/mysqld.sock:/var/run/mysqld/mysqld.sock" in app
    assert "restart: unless-stopped" in app
    assert "./artifacts:/app/artifacts" in app
    assert "./data:/app/data" in app
    assert "docker compose down" not in compose


def test_environment_templates_use_mysql_and_keep_sqlite_read_only_source():
    for name in (".env.example", ".env.tencent.example", ".env.customer.example"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "DATABASE_PATH=" not in text, name
        assert "MYSQL_DATABASE=document_parsing" in text, name
        assert "MYSQL_USER=document_parsing_app" in text, name
        assert "MYSQL_UNIX_SOCKET=/var/run/mysqld/mysqld.sock" in text, name
        assert "SQLITE_SOURCE_PATH=data/app.db" in text, name
    main = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=" in main
    assert "MINIMAX_MODEL=MiniMax-M3" in main
    assert "WEWE_TASK_LEASE_SECONDS=120" in main


def test_release_scripts_backup_consistently_and_verify_both_sites():
    backup = (ROOT / "scripts" / "backup_doc.sh").read_text(encoding="utf-8")
    verify = (ROOT / "scripts" / "verify_doc_study.sh").read_text(encoding="utf-8")
    assert "source.backup" in backup
    assert "mysqldump" in backup
    assert "--single-transaction" in backup
    assert "docker compose down" not in backup
    assert "https://doc.zhouyuaninfo.com.cn" in verify
    assert "https://study.zhouyuaninfo.com.cn" in verify
    assert "/api/wewe/accounts" in verify
    assert "/openapi.json" in verify
    assert "docker compose down" not in verify
    assert (ROOT / "deploy" / "mysql" / "README.md").is_file()


def test_backup_rejects_missing_source_instead_of_creating_empty_bundle(tmp_path):
    backup_root = tmp_path / "backups"
    env = dict(os.environ, DOC_BACKUP_ROOT=str(backup_root))
    result = subprocess.run(
        [str(ROOT / "scripts" / "backup_doc.sh"), str(tmp_path / "missing")],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "missing production source" in result.stderr
    assert not list(backup_root.rglob("manifest.txt")) if backup_root.exists() else True
