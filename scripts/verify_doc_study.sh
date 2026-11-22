#!/usr/bin/env bash
# Read-only production checks for doc plus the co-located study site.
set -euo pipefail

DOC_ORIGIN="${DOC_ORIGIN:-https://doc.zhouyuaninfo.com.cn}"
STUDY_ORIGIN="${STUDY_ORIGIN:-https://study.zhouyuaninfo.com.cn}"

status() {
  curl -sS --max-time 20 -o /dev/null -w '%{http_code}' "$1"
}

expect() {
  local expected="$1" url="$2" actual
  actual="$(status "$url")"
  printf '%s expected=%s %s\n' "$actual" "$expected" "$url"
  if [[ "$actual" != "$expected" ]]; then
    printf 'Unexpected HTTP status: %s (expected %s): %s\n' "$actual" "$expected" "$url" >&2
    return 1
  fi
}

expect 200 "$DOC_ORIGIN/health"
expect 200 "$DOC_ORIGIN/api/billing/plans"
expect 200 "$DOC_ORIGIN/static/console.html"
for path in \
  /api/auth/me \
  /api/knowledge-bases \
  /api/admin/users \
  /api/config \
  /api/rag/sources \
  /api/wewe/accounts \
  /docs \
  /api/debug \
  /openapi.json; do
  expect 401 "$DOC_ORIGIN$path"
done

# study currently has no /health endpoint; its root is the public smoke check.
expect 200 "$STUDY_ORIGIN/"

for container in document-parser-app document-parser-nginx document-parser-qdrant; do
  running="$(sudo -n docker inspect --format '{{.State.Running}}' "$container")"
  printf 'container=%s running=%s\n' "$container" "$running"
  [[ "$running" == "true" ]]
done

# Execute inside app so no MySQL/Qdrant port or credential is exposed publicly.
sudo -n docker exec -i document-parser-app python - <<'PY'
from app.config import get_settings
from app.mysql import raw_connection
import requests

settings = get_settings()
conn = raw_connection(settings)
try:
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
finally:
    conn.close()
response = requests.get("http://qdrant:6333/healthz", timeout=10)
response.raise_for_status()
print("MySQL SELECT 1: ok; Qdrant /healthz: ok")
PY

printf 'DOC_STUDY_VERIFICATION=PASS\n'
