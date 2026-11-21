# Deployment Record: wewe-rss 能力集成（生产发布）

- Change: CHG-20261119-006-wewe-rss-integration
- Date: 2026-11-21（服务器时间 10:42–10:50 CST）
- Target: 114.132.239.186 / ubuntu / 项目目录 /home/ubuntu/document_parsing
- Domain: https://doc.zhouyuaninfo.com.cn/
- Result: 成功发布，服务健康；集成默认关闭（WEWE_ENABLED 默认 false），未改变线上行为

## 目标与授权确认

- Skill: .agent/skills/deployment/SKILL.md
- 主机与域名与 Skill 记录一致；用户在本轮对话中明确要求部署到该机器并提供了访问凭据，
  视为本次生产发布批准（Approval: production release, approved，2026-11-21）。
- 发布内容仅限本 Change 的代码与文档；未包含任何未跟踪的本地临时文件。

## 主机身份校验

- ssh-keyscan 获取并固定主机密钥到本地 known_hosts（StrictHostKeyChecking=yes 生效）：
  - ED25519 SHA256:x9uQta5I5BZdJ8qYKNHUPDOc7381qRTJfN0jBiOfpPA
  - RSA SHA256:dqnI4huGt7iCiteGUw/21+IgFoImkxcTfC6kfYar4HM
- 交叉核对：doc.zhouyuaninfo.com.cn 的 A 记录解析为 114.132.239.186，与发布目标一致。
- 局限：本次为该运维环境的首次记录（TOFU），未与腾讯云控制台指纹逐字比对；
  建议在控制台核对一次并归档。

## 凭据处理

- 密码仅通过交互式 TTY 输入，未出现在命令行参数、脚本、文件或日志中；
  发布完成后已执行 history -c 清理远端会话历史。
- 未读取、未输出、未修改服务器 .env（保持 0600 权限）。
- 密码已在对话中暴露，建议尽快轮换（沿用既有安全结论）。

## 备份

- 位置：/home/ubuntu/document_parsing-backup-wewe-20261121-104217.tgz
- 大小：5,511,544 字节；条目：281（含 .env、data/、artifacts/、deploy/nginx/certs/、.agent/）
- Qdrant 命名卷未做任何破坏性操作（未执行 down -v、未删除卷）。

## 发布过程

1. 本地打包最小发布集（仅本次变更文件）：
   app/config.py、app/main.py、app/wewe_*.py（8 个）、docs/wewe-rss-integration.md、
   README.md、.env.example、.agent/changes/CHG-20261119-006-wewe-rss-integration/；
   打包产物 62,216 字节。
2. 上传至 /home/ubuntu/release-wewe.tgz；解包到 /home/ubuntu/release-wewe-stage，
   python3 -m compileall 通过（COMPILE_OK）。
3. 先比对现网与本地的 md5，确认未修改的文件（merge.py、models.py、vision.py、__init__.py、
   console.html、index.html 等）仅存在换行符差异，故不纳入发布集，避免无关改动。
4. 应用到 /home/ubuntu/document_parsing 后逐文件 md5 与本地一致：
   config.py 2a2aa5d7…、main.py 0ecd50e9…、wewe_api.py 3b7e52d3…、wewe_client.py 83d90c0f…、
   wewe_fetcher.py 9aa8f328…、wewe_images.py 2589d869…、wewe_pipeline.py e91d10b4…、
   wewe_sanitize.py 42ec22bf…、wewe_scheduler.py 23565a0e…、wewe_store.py 4c46004c…
5. sudo docker compose build app -> Built（镜像 document_parsing-app:latest）。
6. sudo docker compose up -d app -> 仅重建 document-parser-app；
   document-parser-nginx 与 document-parser-qdrant 保持运行（未重启）。

## 验收证据

| 检查 | 结果 |
|---|---|
| docker compose ps | app 重建后 Up；nginx / qdrant 持续运行 4 个月 |
| 应用启动日志 | Uvicorn startup complete，无异常 |
| GET /health | 200 {"status":"ok","service":"document-parsing-service"} |
| GET / | 200 |
| GET /docs | 200 |
| GET /openapi.json | 46 条路径，含 9 条 /api/wewe/* 新路由 |
| GET /api/wewe/status | 401（鉴权生效，与既有接口一致） |
| GET /api/rag/sources | 401（既有接口未受影响） |
| GET /api/v1/knowledge-bases | 401（既有 API Key 接口未受影响） |
| 生产 SQLite | 自动创建 wewe_sources / wewe_articles / wewe_images / wewe_collect_tasks |
| Qdrant | 仍仅有 manual_chunks_v2；图片集合未创建（原生图片向量默认关闭） |

## 回滚

1. cd /home/ubuntu/document_parsing
2. sudo docker compose stop app
3. 解压备份恢复代码与状态（保留 .env、data/、artifacts/、deploy/nginx/certs/）：
   tar xzf /home/ubuntu/document_parsing-backup-wewe-20261121-104217.tgz -C /home/ubuntu
4. sudo docker compose up -d --build app
5. 重新执行健康检查（/health、/、/api/rag/sources）

回滚预计耗时 < 5 分钟，不涉及 Qdrant 卷与数据删除。

## 发布后待办

- 轮换已在对话中暴露的 SSH 密码。
- 在腾讯云控制台核对主机密钥指纹并归档。
- 上游 WeWe RSS 与图片 embedding 模型就绪后，按 docs/wewe-rss-integration.md 配置
  WEWE_* 环境变量并做一次小规模采集演练，再开启 WEWE_ENABLED。

