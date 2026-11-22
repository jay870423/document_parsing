# wewe-rss 能力集成

把 WeWe RSS（公众号/网站采集）接入本服务的来源管理、文章管理与 RAG 索引。
本服务不迁移 WeWe RSS 的 NestJS/React/MySQL 全栈，只把它当作**受控上游采集器**。

## 1. 组件与数据流

    用户 -> /api/wewe/* -> WeWeStore(SQLite)          # 来源/文章/图片/任务元数据
                    |-> WeWeRssClient -> WeWe RSS 上游 # 公众号文章（JSON Feed）
                    |-> SafeFetcher   -> 目标站点      # 网站来源（RSS/Atom/HTML）
                    |-> 清洗 -> 正文 + 图片资产分离
                    |-> 文本向量（现有 collection，vector_space=text）
                    |-> 原生图片向量（独立 collection，vector_space=image）

模块位置：

| 模块 | 职责 |
|---|---|
| app/wewe_client.py | 上游 WeWe RSS 适配器（feeds JSON / tRPC 只读调用） |
| app/wewe_fetcher.py | SSRF 防护的 HTTP 抓取与图片魔数校验 |
| app/wewe_sanitize.py | HTML 清洗（白名单）、正文抽取、图片地址抽取 |
| app/wewe_store.py | SQLite 元数据：来源/文章/图片/采集任务 |
| app/wewe_images.py | 多模态图片 embedding + 双向量空间索引 |
| app/wewe_pipeline.py | 采集编排、图文分离、索引与幂等重建 |
| app/wewe_api.py | HTTP 接口层（会话鉴权 + 知识库归属校验） |
| app/wewe_scheduler.py | 定时采集线程（默认关闭） |

## 2. 上游契约（已对源码核对）

来源：D:\codex-project\zhouyuan-wewe-rss\apps\server\src

| 用途 | 调用 |
|---|---|
| 订阅源列表 | GET {base}/feeds/ |
| 文章列表（含全文） | GET {base}/feeds/{feedId}.json?limit=&page=&mode=fulltext |
| 触发上游刷新 | 同上加 &update=true |
| 文章/来源只读管理 | GET {base}/trpc/article.list、GET {base}/trpc/feed.list（input 为 JSON 查询参数） |

tRPC v10 的 query 必须使用 GET，并将 `{"json": input}` JSON 编码到 `input` 查询参数；
mutation 使用 POST，请求体为同样的 JSON envelope。响应均为
`{"result":{"data":{"json": ...}}}`。

鉴权：Authorization 头携带 WEWE_AUTH_CODE（上游 authService.resolveAuth 直接读取该请求头）。
授权码只来自环境变量，不入库、不写日志。

**偏差说明**：上游没有提供稳定的"网站来源采集"公共接口，因此网站来源由本服务本地采集
（SafeFetcher + RSS/Atom/HTML 解析），公众号来源统一走上游适配器。此偏差记录在
Change 的 architecture.md 决策表中。

## 3. 配置

见 .env.example 的 WEWE_* 段。关键项：

- WEWE_ENABLED：集成总开关，关闭时写操作返回 503，不影响既有解析与 RAG。
- WEWE_BASE_URL / WEWE_AUTH_CODE：上游地址与授权码。
- WEWE_MAX_ARTICLES_PER_SOURCE：单次采集文章上限（默认 50）。
- WEWE_MAX_IMAGES_PER_ARTICLE：单篇文章图片上限（默认 20）。
- WEWE_COLLECT_ENABLED + WEWE_COLLECT_INTERVAL_MINUTES：定时采集（默认关闭，60 分钟）。
- WEWE_IMAGE_VECTOR_ENABLED：是否启用原生图片向量（默认关闭）。

> WEWE_* 未加入控制台的可写配置项清单（CONFIG_KEYS），避免授权码经管理界面落盘或回显。

## 4. 接口一览（会话鉴权）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/wewe/status | 集成与上游连通性状态 |
| GET | /api/wewe/feeds | 上游订阅源列表 |
| GET | /api/wewe/sources | 我方来源列表 |
| POST | /api/wewe/sources | 新增来源（mp 公众号 / web 网站） |
| PATCH | /api/wewe/sources/{id} | 订阅状态、名称、所属知识库 |
| DELETE | /api/wewe/sources/{id} | 删除来源（级联删除文章与两类向量） |
| POST | /api/wewe/sources/{id}/collect | 手动采集（可指定 limit / 是否触发上游刷新） |
| GET | /api/wewe/articles | 文章列表（按来源/知识库/状态过滤、分页） |
| GET | /api/wewe/articles/{id} | 文章详情（含图片资产与视觉描述） |
| DELETE | /api/wewe/articles/{id} | 删除文章并清理两个向量空间 |
| GET | /api/wewe/tasks | 采集任务与结果 |
| POST | /api/wewe/images/search | 原生图片向量检索（需开启图片向量） |

既有 RAG 接口新增可选参数 include_images：

- POST /api/rag/search
- POST /api/rag/answer
- POST /api/v1/knowledge-bases/{id}/search
- POST /api/v1/knowledge-bases/{id}/answer

开启后返回 image_hits（原生图片向量命中，含 article_id/image_id/图片地址/描述），
answer 的 citations 中图片条目带 vector_space=image，与正文片段引用区分。

## 5. 两类向量空间的区分

| 通道 | 模型 | 集合 | payload 标识 |
|---|---|---|---|
| 正文/图片描述文本 | DOUBAO_EMBEDDING_MODEL（文本） | QDRANT_COLLECTION | vector_space=text，chunk_type=article_text 或 image_description |
| 原生图片向量 | WEWE_IMAGE_EMBEDDING_MODEL（多模态） | WEWE_IMAGE_COLLECTION | vector_space=image，chunk_type=image_native |

视觉描述文本不是原生图片向量，两者在 payload 与返回值中分别标注，检索入口不同。

## 6. 安全要点

- SSRF：仅 http/https；DNS 解析结果全部为公网才放行；逐跳校验重定向（最多 3 跳）；
  内网/回环/链路本地/保留地址一律拒绝；响应体与图片均有字节上限。
- HTML 清洗：标签/属性白名单，丢弃 script/style/iframe/object/embed/svg 及事件属性、
  javascript: 等危险协议；data: 内联图片不进入资产链路。
- 图片：按魔数校验（jpeg/png/gif/webp），拒绝 SVG 等可执行内容。
- 隔离：来源/文章/图片/任务表均带 user_id，查询强制按当前用户过滤；向量 payload 带
  owner_user_id + knowledge_base_id，检索时按同一条件过滤。
- 凭据：上游授权码与 Ark Key 只从环境变量读取，不写日志、不入库。

## 7. 幂等与失败处理

- 文章唯一键 (user_id, upstream_article_id)，重复采集更新而非新增。
- 每次索引前按 article_id 删除两个向量空间的旧点位，重复采集不会产生重复向量。
- 单篇失败只标记该文章 status=failed 并记录错误，不中断整批采集；任务状态区分
  success / partial / failed。
- 图片下载或视觉描述失败：记录图片行 status，正文仍正常入库（部分成功）。
- 原生图片向量失败：降级为仅保留视觉描述文本索引。

## 8. 本地验证

    python -m compileall -q app
    python -m pytest tests -q

覆盖：HTML 清洗、SSRF 拦截与逐跳重定向、上游契约解析、存储隔离、采集编排与幂等、
接口鉴权与参数校验。

