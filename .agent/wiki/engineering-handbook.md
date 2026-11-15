# Engineering Handbook

## Project

Document parsing, vector ingestion, knowledge-base retrieval/QA, and API console service.

## Stack

- Python 3.11
- FastAPI and Uvicorn
- Docker Compose
- SQLite for users, subscriptions, and usage data
- Qdrant for vectors
- Nginx for HTTPS and reverse proxy
- Web console: `app/static/console.html`

## Entry points

- `app/main.py`: HTTP APIs, pages, and app assembly
- `app/billing.py`: users, plans, subscriptions, quotas, and API keys
- `app/pipeline.py`: document processing
- `app/rag.py`: retrieval logic
- `docker-compose.yml`: app, Nginx, and Qdrant orchestration
- `.env`: runtime configuration; do not commit or overwrite

## Local checks

```powershell
python -m py_compile app/billing.py app/main.py
git diff --check
docker compose build app
docker compose up -d
```

## Production release

Back up the remote project before release. Preserve `.env`, `data/`, `artifacts/`, and certificates. After release check Compose status, app logs, HTTPS, and key APIs. Never put production passwords in scripts, docs, or logs.
