# Post-release Review: CHG-20261116-003-deploy-and-commit

- Release time: 2026-11-16 00:50 Asia/Shanghai
- Reviewer: main-agent
- Result: healthy

## Health evidence

- Remote Docker Compose build completed successfully on `114.132.239.186`.
- `document-parser-app` was recreated and is running.
- `document-parser-nginx` and `document-parser-qdrant` remain running.
- `https://doc.zhouyuaninfo.com.cn/health` returned HTTP `200` with `{"status":"ok"}`.
- Homepage returned HTTP `200` and contained none of `AG-UI`, `A2UI`, `WebMCP`, `协议工作台`, or `协议能力`.
- `/api/agent/capabilities`, `/api/agent/run`, and `/api/debug-pro` returned HTTP `404`.
- Remote protocol-only files were absent after deployment.

## Backup and rollback

- Backup retained at `/home/ubuntu/document_parsing-backup-deploy-20261116-004931.tgz`.
- Rollback: stop the current Compose app, restore the backup into `/home/ubuntu/document_parsing`, preserve `.env`, `data/`, `artifacts/`, and certificates, then run `sudo docker compose up -d --build`.

## Incidents or unexpected behavior

- No application outage observed during verification.
- Docker Compose emitted a non-blocking warning that buildx is not installed; the default Docker builder completed successfully.

## Security follow-up

- The SSH password was supplied in chat and should be rotated after deployment.
- Any API/cloud credentials present on the server should be rotated if they may have been exposed in the previously detected malicious repository files.
