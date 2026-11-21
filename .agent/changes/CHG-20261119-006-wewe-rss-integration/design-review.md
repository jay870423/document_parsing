# Design Review: wewe-rss 能力集成

- Change: CHG-20261119-006-wewe-rss-integration
- Date: 2026-11-21
- Reviewer: controller（主控自评；本次按用户要求未启用 Harness 子角色，见"局限"）
- Result: PASS with conditions

## Scope reviewed

architecture.md（最终方案）、requirements.md（冻结后的边界）、实现代码
（app/wewe_*.py、app/main.py 接线）、测试（tests/test_wewe_*.py）。

## Checklist

| 项 | 结论 | 证据 |
|---|---|---|
| 上游契约真实可调用 | 通过 | 对 zhouyuan-wewe-rss 源码核对：feeds.controller.ts 的 /feeds/、/feeds/:feed.json、trpc.router.ts 的 article.list/feed.list；auth.service.ts 读取 Authorization 头 |
| 不假设上游内部接口为稳定公共 API | 通过 | 只使用 feeds HTTP 接口与 tRPC 只读过程；对非公开字段不做假设；客户端对上游错误返回 WeWeError |
| 双向量空间不混用 | 通过 | app/wewe_images.py：文本集合 payload vector_space=text、图片集合 vector_space=image；图片命中统一带 chunk_type=image_native |
| 视觉描述文本不冒充原生图片向量 | 通过 | 描述文本 chunk_type=image_description 写入文本空间；文档与接口返回值分别标注 |
| 幂等与重建 | 通过 | (user_id, upstream_article_id) 唯一键；索引前按 article_id 删除两个空间点位（wewe_pipeline._index_article） |
| 向量维度兼容 | 通过 | 文本集合沿用既有 vector_size，未修改既有 collection schema；图片集合独立维度 |
| 隔离 | 通过 | 存储层全部按 user_id 过滤；向量检索过滤 owner_user_id + knowledge_base_id |
| 回滚 | 通过 | WEWE_ENABLED=false 即停用；删除来源/文章级联清理向量；新表均为新增对象 |
| 兼容既有接口 | 通过 | 既有 /api/rag/*、/api/v1/* 仅新增可选字段 include_images 与 image_hits |

## Findings

1. （中）网站来源缺少上游能力，走本地采集，属于对原始需求的偏差。已在 architecture.md
   决策表与 requirements.md「偏差说明」显式记录，并说明原因（上游无稳定公共接口）。
   条件：后续若上游提供正式网站采集接口，替换 WeWeIngestPipeline._fetch_web_items 即可。
2. （低）图片向量模型默认 doubao-embedding-vision 未经真实环境验证（无凭据环境）。
   条件：生产启用前需在测试知识库验证模型可用性与维度（WEWE_IMAGE_VECTOR_SIZE）。
3. （低）RAG 引用中的图片条目以 citations 追加形式呈现，未做去重与排序调优。
   条件：观察线上引用质量后再优化。

## Conditions

- 生产启用前必须完成：上游连通性验证、图片向量模型维度确认、一次小规模来源采集的
  端到端演练（含图片下载与视觉描述）。
- 局限：本次设计/风险复核由主控执行，未使用独立 Harness 角色；不能等同于独立第三方评审，
  建议在生产发布门禁补一次人工复核。

## Conclusion

设计满足已冻结的需求与边界，条件项已记录；允许进入实现与验收（实现已完成，见 ci.md）。

