# Repository Conventions

- Backend: Python 3.11, FastAPI, and Uvicorn; main entry is usually `app/main.py`.
- Runtime configuration comes from `.env`; never commit or overwrite `.env`.
- Docker Compose services include the app, Nginx, and Qdrant; preserve `data/`, `artifacts/`, and certificate configuration during deployment.
- Billing and user logic is in `app/billing.py`; the web console is in `app/static/console.html`.
- Preferred checks: `python -m py_compile`, focused tests, `git diff --check`, and Docker Compose build/health checks.
- API changes require checking frontend callers, errors, docs, and compatibility.
- Keep UTF-8 encoding and existing line endings when editing files.
- Do not commit logs, databases, build artifacts, certificates, or debug files.
