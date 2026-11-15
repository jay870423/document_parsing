# CI Acceptance: CHG-20261115-002-remove-agent-capabilities

- Environment: Windows PowerShell, repository Python environment
- Result: passed for focused local acceptance

## Checks

| Check | Result | Evidence |
|---|---|---|
| Python compilation | passed | `python -m py_compile app/main.py` and per-file compilation of all `app/*.py` |
| Whitespace check | passed | `git diff --check -- app/main.py app/static/index.html app/static/console.html` |
| Removed-reference scan | passed | no AG-UI/A2UI/WebMCP/agent route/debug references in active app files |
| FastAPI route inspection | passed | `/api/agent/*` and `/api/debug-pro` absent; `/api/debug` and `/api/v1/parse` present |
| Harness CI definition review | passed | `.github/workflows/ci.yml` compile, diff, metadata, and test checks reviewed |
| HTTP UI visibility smoke check | passed | temporary Uvicorn server returned HTTP 200 with no removed-capability tokens |
| Browser UI check | not run | Playwright package is not installed in the local environment |

## Notes

- Full hosted GitHub Actions execution was not available in this local session. A broad `compileall` invocation also exited abnormally in the Windows environment; per-file compilation of all application Python files passed and is the relied-upon evidence.
- Docker deployment was not executed because the local Docker daemon is unavailable. Local Uvicorn startup was verified; production deployment remains pending Docker availability.
