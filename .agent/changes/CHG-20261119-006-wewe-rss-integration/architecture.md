# Architecture Design: wewe-rss 能力集成（最终方案）

- Date: 2026-11-19（2026-11-21 定稿并实现）
- Requirement: .agent/changes/CHG-20261119-006-wewe-rss-integration/requirements.md
- Status: implemented

## Current state and change boundary

目标项目为 Python/FastAPI 文档解析、结构化提取与 RAG 服务；源项目为 Node/NestJS + React/Vite 的
wewe-rss 应用。变更边界：新增 wewe-rss 相关模块、配置、接口与测试；不修改既有文档解析、
计费、认证逻辑，不迁移上游数据库与账号体系，不读取上游 .env 与凭据。

## Solution overview

把 WeWe RSS 作为**独立授权/采集上游**，本服务负责来源/文章管理、采集调度、HTML 清洗、
图文分离与 RAG 编排：

- 公众号来源：上游适配器读取 feeds 接口（JSON Feed v1）获取文章正文；
- 网站来源：本服务本地采集（SSRF 防护 + RSS/Atom/HTML 解析）——上游无稳定网站采集接口；
- 正文与图片分离：清洗后正文进文本向量空间；图片下载后生成视觉描述文本（文本空间）
  与原生图片向量（独立图片空间）；
- 双路检索：既有 RAG 文本检索 + 可选的原生图片检索，命中可回溯来源文章与图片。

## Components and data flow

    /api/wewe/sources/{id}/collect
        -> WeWeIngestPipeline.collect_source
            -> WeWeRssClient.fetch_feed_items   (公众号)  / SafeFetcher (网站)
            -> sanitize_html  -> 正文 text + 图片 URL 列表
            -> 图片受限下载（魔数校验） -> WeWeStore.wewe_images
            -> DoubaoVisionClient.analyze_image  -> 视觉描述
            -> WeWeVectorIndex.upsert_text_chunks (vector_space=text)
            -> ImageEmbeddingClient.embed_image_file
            -> WeWeVectorIndex.upsert_image_vectors (vector_space=image, 独立集合)

定时采集由 WeWeScheduler 线程按固定间隔触发，来源之间串行。

## Interfaces, state, and data changes

上游契约（对 zhouyuan-wewe-rss 源码核对）：

| 用途 | 调用 |
|---|---|
| 订阅源列表 | GET {base}/feeds/ |
| 文章列表 | GET {base}/feeds/{feedId}.json?limit&page&mode=fulltext[&update=true] |
| 只读管理 | POST {base}/trpc/article.list、POST {base}/trpc/feed.list |

鉴权：Authorization 头 = 上游授权码（上游 authService.resolveAuth 直接读取该头）。

新增 SQLite 表（app/wewe_store.py）：

- wewe_sources(user_id, knowledge_base_id, source_type, upstream_feed_id, name, target_url,
  subscribed, status, last_sync_at, last_error)；
- wewe_articles(user_id, source_id, knowledge_base_id, upstream_article_id UNIQUE, title, url,
  content_html, text_content, status, chunk_count, image_count, indexed_at)；
- wewe_images(article_id, user_id, url, local_path, mime_type, byte_size, description, status)；
- wewe_collect_tasks(user_id, source_id, kind, status, article_count, image_count, indexed_count, error)。

向量 payload 字段：owner_user_id、knowledge_base_id、article_id、source_id、chunk_type、
vector_space、article_title、article_url、image_id（图片通道）、text/description。
文本集合沿用 QDRANT_COLLECTION（维度不变）；图片集合为新集合 WEWE_IMAGE_COLLECTION
（独立维度 WEWE_IMAGE_VECTOR_SIZE，默认 1024）。

## Key decisions and alternatives

| 决策 | 选择 | 理由 |
|---|---|---|
| 集成方式 | 上游适配器 + 本地编排 | 不迁移 NestJS/MySQL 全栈，运维边界小 |
| 授权 | 上游完成，本服务只透传授权码 | 不复制授权逻辑，不保存凭据 |
| 网站来源 | 本服务本地采集 | 上游无稳定网站采集公共接口；与公众号共用来源模型 |
| 正文与图片 | 文本空间 + 独立图片空间 | 满足"描述文本索引 + 原生图片向量"双路要求，二者不混用 |
| 图片向量模型 | 可配置多模态 embedding（默认 doubao-embedding-vision） | 维度/供应商可替换，失败可降级 |
| 幂等 | (user_id, upstream_article_id) 唯一键 + 按 article_id 先删后写 | 重复采集不产生重复向量 |
| 不采用 | 整体迁移 wewe-rss、复制 Prisma schema、改造现有认证/计费 | 超出本次范围，回归风险高 |

## Compatibility, migration, and rollback

- 未修改既有 collection 的维度、命名与 payload 语义；既有文档解析与文本 RAG 不受影响。
- 关闭 WEWE_ENABLED 即停用集成；删除来源/文章会级联清理两个向量空间的点位。
- 新表为新增对象，旧库打开即自动建表（CREATE TABLE IF NOT EXISTS），无需迁移脚本。
- 回滚：回退代码即可；如需清理数据，删除 wewe_* 表与图片集合不影响既有向量。

## Security, observability, and operations

- SSRF：仅 http/https；DNS 全解析结果公网校验；逐跳重定向校验（<=3 跳）；拒绝内网/回环/
  链路本地/保留/组播地址；响应体与图片字节上限；超时控制。
- HTML 清洗：标签/属性白名单，丢弃脚本/样式/可执行标签与事件属性、危险协议；data: 图片不入库。
- 图片：魔数校验，拒绝 SVG；仅允许 jpeg/png/gif/webp。
- 隔离与授权：所有查询按当前 user_id 过滤；向量检索按 owner_user_id + knowledge_base_id 过滤。
- 凭据：WEWE_AUTH_CODE / ARK_API_KEY 只来自环境变量；日志不记录凭据与完整正文。
- 可观察性：采集任务表记录状态、计数与错误；单篇失败标记 status=failed 并记录原因。

## Implementation breakdown

1. 上游契约核对与边界冻结（requirements.md）。
2. 适配器与安全基座：wewe_client / wewe_fetcher / wewe_sanitize。
3. 存储与索引：wewe_store / wewe_images（双向量空间）。
4. 编排与调度：wewe_pipeline / wewe_scheduler。
5. 接口层：wewe_api + main.py 接线 + RAG include_images 扩展。
6. 测试与文档：tests/test_wewe_*.py、docs/wewe-rss-integration.md。

## Validation strategy

- 单元测试：清洗、SSRF、契约解析、存储隔离、编排幂等、接口参数校验（26 个用例，全通过）。
- 编译检查：python -m compileall -q app（通过）。
- 现有 RAG 回归：既有 /api/rag/* 与 /api/v1/* 未改动返回结构（仅新增可选字段 include_images）。

