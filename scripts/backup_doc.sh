#!/usr/bin/env bash
# Create a rollback bundle without stopping doc or touching study/MySQL sidecars.
set -euo pipefail

ROOT="${1:-/home/ubuntu/document_parsing}"
BACKUP_ROOT="${DOC_BACKUP_ROOT:-/home/ubuntu/document_parsing-backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$BACKUP_ROOT/$STAMP"

install -d -m 700 "$BACKUP_ROOT"
install -d -m 700 "$DEST"

for path in app data artifacts; do
  if [[ -e "$ROOT/$path" ]]; then
    cp -a "$ROOT/$path" "$DEST/$path"
  fi
done
for path in docker-compose.yml Dockerfile requirements.txt .env; do
  if [[ -f "$ROOT/$path" ]]; then
    cp -a "$ROOT/$path" "$DEST/$path"
  fi
done
chmod 700 "$DEST"
if [[ -f "$DEST/.env" ]]; then
  chmod 600 "$DEST/.env"
fi

# Replace the copied live SQLite file with a consistent online backup snapshot.
if [[ -f "$ROOT/data/app.db" ]]; then
  python3 - "$ROOT/data/app.db" "$DEST/data/app.db" <<'PY'
import sqlite3
import sys
from pathlib import Path

source_path = Path(sys.argv[1]).resolve()
target_path = Path(sys.argv[2]).resolve()
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
target = sqlite3.connect(target_path)
try:
    source.backup(target)
finally:
    target.close()
    source.close()
check = sqlite3.connect(f"file:{target_path}?mode=ro", uri=True)
try:
    result = check.execute("PRAGMA integrity_check").fetchone()[0]
    violations = len(check.execute("PRAGMA foreign_key_check").fetchall())
finally:
    check.close()
if result != "ok" or violations:
    raise SystemExit(f"snapshot verification failed: integrity={result}, fk_violations={violations}")
print(f"SQLite snapshot verified: integrity={result}, fk_violations={violations}")
PY
  sha256sum "$DEST/data/app.db" > "$DEST/sqlite.sha256"
fi

# The first pre-MySQL backup legitimately has no document_parsing database.
DB_EXISTS="$(sudo -n mysql --batch --skip-column-names -e "SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='document_parsing'")"
if [[ "$DB_EXISTS" == "1" ]]; then
  sudo -n mysqldump --single-transaction --skip-lock-tables --routines --triggers document_parsing > "$DEST/document_parsing.sql"
  chmod 600 "$DEST/document_parsing.sql"
fi

for container in document-parser-app document-parser-nginx document-parser-qdrant; do
  sudo -n docker inspect --format '{{.Id}} {{.Image}} {{.State.Status}}' "$container"
done > "$DEST/containers.txt"
printf 'created_utc=%s\nsource_root=%s\nsource_git_sha=%s\n' \
  "$STAMP" "$ROOT" "${SOURCE_GIT_SHA:-untracked-production}" > "$DEST/manifest.txt"
chmod 600 "$DEST"/*.txt "$DEST"/*.sha256 2>/dev/null || true
printf 'BACKUP_DIR=%s\n' "$DEST"
