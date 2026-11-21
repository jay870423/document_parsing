# Requirements Baseline: wewe-rss 能力集成

- Date: 2026-11-19
- Requester: 用户
- Status: baseline approved（2026-11-21，边界问题已冻结，见下节）

## Goal

把 `D:\codex-project\zhouyuan-wewe-rss` 的微信公众号采集、订阅源管理和文章管理能力集成到当前基于 FastAPI/Python 的文档解析服务，并将文章中的文字与图片分离处理后接入现有 RAG/向量库。

## User scenarios

1. 用户通过 WeWe RSS 提供的授权/采集能力添加微信公众号订阅源，并管理订阅状态。
2. 系统通过 WeWe RSS 统一采集微信公众号和网站地址内容，按需或定时同步，用户可浏览和管理文章。
3. 正文与图片资产分离处理：正文进入现有文本 RAG；图片视觉描述作为关联文本索引；原始图片另使用兼容的图像 embedding 模型形成原生图片向量。
4. 用户在现有 RAG 查询流程检索公众号/网站文章，并可追溯来源文章、正文片段和相关图片。

## Scope

范围已确认，边界问题已冻结，实现已按 `architecture.md` 完成并通过测试。
- 微信公众号来源管理、手动/定时采集、文章列表与详情管理。
- 文章正文与图片分离、存储、索引并关联至现有 RAG 知识库。
- 复用现有用户/知识库授权、图像解析及向量索引能力；接口和数据模型由评审确定。

## Non-goals

- 不默认整体迁移 wewe-rss 全栈或其 NestJS/React/MySQL 服务；由架构评审比较后确定；
- 不读取、迁移或暴露源项目 `.env`、凭据、用户数据；
- 不改变现有认证、计费、生产部署或数据库结构。

## Assumptions and dependencies

- 当前项目以 Python 3.11、FastAPI、SQLite/Qdrant 为主（`.agent/rules/30-repo-conventions.md`）。
- wewe-rss 项目为 Node/NestJS + React/Vite + Prisma，且具有 feeds、定时采集、RSS/Atom/JSON 输出及知识库功能（项目 README、`apps/server/src/feeds/`、`apps/server/src/trpc/`）。
- 上述是初步静态盘点；独立审查 Agent 正在复核。用户已确认业务目标，具体技术决策仍待评审。

## Resolved boundary decisions（2026-11-21 冻结）

原 review.md 中的阻塞项已由对上游源码的静态核对与本仓库实现确定，结论如下：

1. **公众号添加方式**：由 WeWe RSS 侧完成授权并维护订阅源，本服务通过上游适配器读取
   `GET {base}/feeds/` 与 `GET {base}/feeds/{feedId}.json`；本服务只保存来源元数据与
   采集任务，不复制上游授权逻辑。
2. **网站来源**：上游没有稳定的网站采集公共接口，网站来源由本服务本地采集
   （SSRF 防护 + RSS/Atom/HTML 解析），与公众号来源共用同一套来源/文章/图片模型。
3. **单源上限**：`WEWE_MAX_ARTICLES_PER_SOURCE=50` 篇/次，`WEWE_MAX_IMAGES_PER_ARTICLE=20`
   张/篇，图片单文件 `WEWE_MAX_IMAGE_BYTES=5MB`。
4. **采集周期与并发**：手动采集按来源触发；定时采集 `WEWE_COLLECT_INTERVAL_MINUTES=60`
   分钟（默认关闭），来源之间串行、单个来源失败不影响其他来源。
5. **历史回溯范围**：仅回溯上游已存在的文章（上游接口按 publishTime 倒序分页），
   本服务不做全量历史爬取。
6. **跨用户/跨知识库隔离**：来源、文章、图片、任务表均带 `user_id`；向量 payload 带
   `owner_user_id` + `knowledge_base_id`，检索与删除一律按同一条件过滤。
7. **原生图片向量**：使用多模态 embedding 模型（`WEWE_IMAGE_EMBEDDING_MODEL`）写入独立
   集合 `WEWE_IMAGE_COLLECTION`（`vector_space=image`）；视觉描述文本仍走文本空间
   （`vector_space=text`，`chunk_type=image_description`），两者不可混用、不可互相宣传。
8. **上游授权与凭据**：`WEWE_AUTH_CODE` 只来自环境变量，通过 `Authorization` 头透传，
   不入库、不写日志、不进控制台可写配置项。

## Acceptance criteria

- [ ] Normal path: 获授权用户管理公众号来源、采集文章；正文和图片视觉内容按设计分别索引至所选知识库，RAG 命中结果可追溯来源。
- [ ] Boundary path: 明确公众号添加方式、单源文章/图片上限、采集周期/并发、历史回溯范围及跨用户知识库隔离。
- [ ] Failure path: 采集限流/失效、图片下载/解析失败、重复文章、部分成功、任务重试及删除/重建索引具备幂等且可观察的处理。
- [ ] Compatibility/data: 不破坏现有文档和向量；索引关联来源文章/图片，支持重建/删除并明确 SQLite/Qdrant 兼容策略。
- [ ] Security/permissions: SSRF/重定向防护、凭据保护、用户/知识库授权、HTML 清理、资源与速率限制通过安全评审。
- [ ] Operations/release: 部署、后台任务、监控、备份、生产审批与回滚步骤有记录。

## Risks and open questions

- WeWe RSS 上游授权/采集服务的稳定性、可用接口、凭据保管和服务条款须在设计评审中确认；不能假定其内部代理接口是稳定公共 API。
- 采集端点和文章图片属于不可信外部输入，可能引入 SSRF、恶意 HTML、重定向与资源耗尽风险。
- 两项目技术栈和数据模型不同，直接复制整个项目会引入高维护成本。
- 用户明确要求两路图像能力：图片视觉描述文本索引 + 原生图片多模态向量检索。视觉描述文本向量不得标记或宣传为原生图片向量。
- 需确认图像 embedding 模型/供应商、向量维度、双路检索融合策略、图片向量存储（Qdrant named vectors 或独立 collection）、查询入口和降级行为。
- 需确认 WeWe RSS 授权/部署方式、允许采集范围、历史回溯和删除文章时两类向量的级联处理，以及多租户权限边界。
- 当前仓库已有未提交改动，实施时必须保护不动。

## Definition of done

业务范围已由用户确认；实现细节、最小可行架构及风险经独立审查并取得实施审批后才进入实施。实施需有聚焦测试、代码审查和 CI 证据。
