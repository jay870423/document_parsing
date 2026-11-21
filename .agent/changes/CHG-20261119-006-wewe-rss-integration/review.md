# Review: wewe-rss 能力集成

- Status: PASS with conditions（实现与验收阶段；生产发布未执行）
- Reviewer: controller（主控自评；本次按用户要求未启用 Harness 子角色）
- Date: 2026-11-21

## Review scope

需求基线、架构与数据模型、上游契约、安全（SSRF/HTML 清洗/凭据/隔离/资源限额）、
数据与幂等、接口兼容、测试与 CI 证据。

## Verified findings

- 上游 WeWe RSS 的可调用契约已核实：GET /feeds/、GET /feeds/{id}.json（JSON Feed v1，
  mode=fulltext 可取全文）、POST /trpc/{procedure}（只读 article.list / feed.list）；
  鉴权是 Authorization 头直接承载授权码（apps/server/src/auth/auth.service.ts）。
- 现有 RAG 管线对图像生成的是视觉描述文本并写入文本向量空间，本实现保留该语义，
  并新增独立图片集合存放原生图片向量，两者在 payload 与返回值中分别标注
  （vector_space=text / image，chunk_type=article_text|image_description|image_native）。
- 需求中的"两路图像能力"已实现：图片视觉描述文本进文本索引，原图经多模态 embedding
  进图片空间；查询侧提供 include_images 与 /api/wewe/images/search 独立入口。
- 本次未读取、未复制、未输出上游 .env 与任何凭据；上游授权码仅从环境变量读取并透传。

## Resolved former blockers

| 原阻塞项 | 结论 |
|---|---|
| 上游授权/API 契约未定 | 已冻结（见上），并实现超时/重试/错误分级 |
| 原生图像 embedding 模型与向量 schema 未定 | 采用可配置多模态模型 + 独立集合，失败降级为描述文本索引 |
| SSRF/清理/授权隔离/资源限额未审 | 已实现并有测试证据（risk-review.md R1–R5） |
| 需求与架构无评审产物 | 已补齐 requirements/architecture/design-review/risk-review（主控自评） |

## Conditions

1. 生产启用前须完成：真实上游连通性验证、图片 embedding 模型维度确认、
   小规模端到端演练（公众号 + 网站各 1 个来源）。
2. 上游授权合规与使用条款需与上游确认。
3. 本评审为自评，非独立第三方评审；建议在生产发布门禁补一次人工复核。

## Gate decision

PASS（带条件）进入发布审批阶段。未执行任何生产发布动作，未修改既有业务逻辑。

