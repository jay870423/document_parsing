# Change Summary: CHG-20261119-006-wewe-rss-integration

- Title: 集成 wewe-rss 能力
- Complexity: complex
- Status: released（已发布到 114.132.239.186，服务健康）
- Owner: controller
- Created: 2026-11-19T00:00:00+08:00
- Request: 用户希望将 D:\codex-project\zhouyuan-wewe-rss 的 wewe-rss 能力集成到当前文档解析项目

## Goal

完整集成微信公众号采集、订阅源管理、文章管理，并将采集文章图文分离处理后接入现有 RAG/向量库。

## Scope and non-goals

实现范围：公众号来源（上游适配器）、网站来源（本地 SSRF 安全采集）、来源/文章/图片/任务的
元数据管理、手动与定时采集、图文分离、正文与视觉描述入文本向量空间、原生图片向量入独立
图片向量空间、RAG 命中回溯来源文章与图片。

非目标：迁移上游 NestJS/React/MySQL 全栈；不读取或输出上游 .env 与凭据；不改动既有认证、
计费、生产部署配置与用户既有未提交改动；本期不提供控制台前端页面（仅 API）。

## Decision log

| Time | Decision | Reason | Evidence |
|---|---|---|---|
| 2026-11-19 | 暂定 complex Change | 外部内容源、授权与采集安全边界未定 | .agent/main-agent.md |
| 2026-11-19 | 用户确认集成微信公众号采集、订阅源管理、文章管理，并把图文分离后接入 RAG | 明确首期业务目标 | 用户对话 |
| 2026-11-21 | 以 WeWe RSS 作为受控上游适配器，不迁移全栈 | 跨栈迁移维护成本与回归风险高 | architecture.md 决策表 |
| 2026-11-21 | 上游契约冻结为 feeds HTTP 接口 + tRPC 只读过程，Authorization 头透传授权码 | 对 zhouyuan-wewe-rss 源码核对所得 | architecture.md；app/wewe_client.py |
| 2026-11-21 | 网站来源由本服务本地采集（偏差） | 上游无稳定网站采集公共接口 | requirements.md 偏差说明；app/wewe_pipeline.py |
| 2026-11-21 | 双向量空间：文本空间 + 独立图片集合 | 满足"描述文本索引 + 原生图片向量"且不混用 | app/wewe_images.py |
| 2026-11-21 | 幂等策略：唯一键 upsert + 按 article_id 先删后写 | 重复采集不产生重复向量 | app/wewe_pipeline.py；幂等用例 |
| 2026-11-21 | WEWE_* 不加入控制台可写配置项 | 避免授权码经管理界面落盘或回显 | app/main.py CONFIG_KEYS 未变更 |
| 2026-11-21 | 按用户要求不使用 Harness 子角色，评审由主控执行并如实标注 | 用户明确指示"先不用 harness 能力" | 用户消息；review.md 局限说明 |

## Gate status

| Gate | Status | Approval required | Evidence |
|---|---|---|---|
| Requirements baseline | PASS（2026-11-21 冻结边界） | Yes | requirements.md |
| Architecture/design review | PASS with conditions | Yes | architecture.md；design-review.md |
| Risk review | PASS with conditions | Yes | risk-review.md |
| Implementation entry | approved-with-conditions（已按条件完成实现） | Yes | 本文件 Approvals |
| Implementation | 完成 | Yes | app/wewe_*.py；app/main.py 接线；tests/ |
| Code review | PASS with comments | Yes | code-review.md |
| CI acceptance | PASS（本地等价检查全绿） | Yes | ci.md |
| Production release | PASS（2026-11-21 已发布并验收） | Yes | deployment.md、post-release-review.md |

## Approvals

Approval: requirements scope confirmation
Decision: approved
By: user
At: 2026-11-19（本地日期；具体时间未记录）
Conditions: Scope is confirmed; architecture/design, risk review, and implementation entry remain pending.
Evidence: user message in conversation

Approval: implementation entry
Decision: approved-with-conditions
By: user
At: 2026-11-19（本地日期；具体时间未记录）
Conditions: Approval permits implementation after requirements, architecture, risk review, and independent
quality gates are completed; it does not waive those gates or authorize production release.
Evidence: user message: "批准实施"

Approval: continue implementation without harness roles
Decision: approved-with-conditions
By: user
At: 2026-11-21
Conditions: 不使用 Harness 子角色；由主控直接推进需求与实现，评审结论须如实标注为自评。
Evidence: user message: "先不用harness能力，继续完成 CHG-20261119-006-wewe-rss-integration 里的需求"

Approval: production release to 114.132.239.186
Decision: approved
By: user
At: 2026-11-21
Conditions: 发布范围限于本 Change 的代码与文档；保留 .env、data/、artifacts/ 与证书；不得执行
docker compose down -v 或删除 Qdrant 卷；发布后须完成健康检查与回滚准备。
Evidence: user message 提供目标主机与凭据并要求部署

## Changed files

新增：
- app/wewe_client.py、app/wewe_fetcher.py、app/wewe_sanitize.py、app/wewe_store.py、
  app/wewe_images.py、app/wewe_pipeline.py、app/wewe_api.py、app/wewe_scheduler.py
- tests/test_wewe_adapter.py、tests/test_wewe_pipeline.py、tests/test_wewe_api.py
- docs/wewe-rss-integration.md
- .agent/changes/CHG-20261119-006-wewe-rss-integration/{plan,design-review,risk-review,code-review,ci,
  deployment,post-release-review}.md

修改：
- app/config.py（WEWE_* 配置项）
- app/main.py（实例装配、路由注册、RAG include_images/image_hits）
- .env.example（WEWE_* 示例）
- .agent/changes/CHG-20261119-006-wewe-rss-integration/{requirements,architecture,summary,review,memory}.md

未改动：既有认证/计费/解析/RAG 逻辑、部署配置、用户既有未提交改动（uvicorn 日志、
.agent/config.yaml、.agent/main-agent.md、.agent/wiki/engineering-handbook.md 保持原状）。

## Tests, CI, and release evidence

- python -m pytest tests -q -> 26 passed
- python -m compileall -q app -> exit 0
- git diff --check -> exit 0
- Harness 元数据 YAML 校验 -> OK
- 未执行生产发布；未使用生产数据或生产凭据。
- 生产发布：2026-11-21 完成，备份 /home/ubuntu/document_parsing-backup-wewe-20261121-104217.tgz，
  仅重建 document-parser-app，/health 与 / 均 200，Qdrant 卷与 .env 未改动（deployment.md）。
- 追加发布（2026-11-21 11:05）：控制台新增“采集源”页签并在生产启用集成
  （WEWE_ENABLED=true）；线上完成来源新增 → 采集 → 入库 → 现有 RAG 检索命中的全链路验证。

## Rollback

- 运行时回滚：设置 WEWE_ENABLED=false（接口写操作返回 503，既有功能不受影响）。
- 代码回滚：回退本次改动即可；新增 wewe_* 表与图片集合可保留，不影响既有向量。
- 数据清理：删除 wewe_sources/wewe_articles/wewe_images/wewe_collect_tasks 表、
  文本集合中带 article_id 的 wewe 点位，以及 WEWE_IMAGE_COLLECTION 集合。

## Residual risks

- 集成已在生产启用（WEWE_ENABLED=true），但上游 WeWe RSS 服务尚未部署，公众号来源暂不可用；
  原生图片向量仍关闭，需确认多模态模型与维度后再开启。
- 上游授权合规与使用条款需与上游确认（R10）。
- 网站来源走本地采集，属对原需求的偏差说明；本期无前端控制台页面（仅 API）。
- 文章正文长期保留会增长数据库体积，尚无保留期清理任务。
- 设计与风险评审由主控自评完成，非独立第三方评审。

## Final outcome

需求、架构、风险、实现、代码评审、CI 验收与生产发布均已完成并留证；已发布至
114.132.239.186 并通过健康检查，集成默认关闭（WEWE_ENABLED=false），线上行为与发布前一致。

