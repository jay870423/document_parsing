# Change Summary: CHG-20261116-003-deploy-and-commit

- Title: Local release validation and Git submission
- Complexity: complex
- Status: closed
- Owner: main-agent
- Created: 2026-11-16
- Request: user request to deploy and submit code to Git

## Goal

Validate and submit the current repository changes while preserving sensitive and runtime-only files.

## Approval

Approval: release attempt
Decision: approved
By: user
At: 2026-11-16
Evidence: user request to deploy and commit

## Gate status

| Gate | Status | Evidence |
|---|---|---|
| Local validation | passed | Uvicorn startup and health route validation |
| Docker deployment | passed | Remote Compose build/recreate completed on `114.132.239.186` |
| Commit | passed | release commit `7bb79b4` and security cleanup commit `3493862`; excluded files absent |
| Push | passed | `origin/main` updated with deployment evidence |

## Rollback

Revert the resulting Git commit. No database or vector migration is performed.

## Evidence

- Remote Docker Compose build/recreate completed successfully on 114.132.239.186.
- https://doc.zhouyuaninfo.com.cn/health returned HTTP 200.
- Homepage returned HTTP 200 with no removed-capability tokens.
- /api/agent/capabilities, /api/agent/run, and /api/debug-pro returned HTTP 404.
- Git push completed successfully; local HEAD equals origin/main.

## Residual risks

Production deployment completed successfully on the specified Tencent Cloud host. Rollback backup is retained remotely. Remote history was found to contain malicious exfiltration files; those files were removed in `3493862`. Credentials potentially exposed by prior workflow execution should be rotated.

## Post-release result

Healthy; see post-release-review.md for remote health, rollback, and security evidence.
