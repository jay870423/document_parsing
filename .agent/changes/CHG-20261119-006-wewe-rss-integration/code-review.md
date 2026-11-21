# Code Review: wewe-rss 能力集成

- Change: CHG-20261119-006-wewe-rss-integration
- Date: 2026-11-21
- Reviewer: controller（主控自评；按用户要求未启用独立 Harness 角色）
- Result: PASS with comments

## 审查范围

app/wewe_client.py、app/wewe_fetcher.py、app/wewe_sanitize.py、app/wewe_store.py、
app/wewe_images.py、app/wewe_pipeline.py、app/wewe_api.py、app/wewe_scheduler.py、
app/config.py、app/main.py（增量）、tests/test_wewe_*.py。

## 结论摘要

| 维度 | 结论 |
|---|---|
| 功能正确性 | 通过：来源→采集→清洗→图文分离→双空间索引→检索回溯链路有测试覆盖 |
| 安全性 | 通过：无凭据硬编码、无注入点（全部参数化 SQL）、SSRF 与 HTML 清洗有实现与用例 |
| 兼容性 | 通过：既有接口返回结构不变，既有 collection 维度不变 |
| 可维护性 | 通过：模块职责单一、配置外置、注释说明与上游契约一致 |
| 测试充分性 | 条件通过：单元与接口用例 26 个；缺真实上游与真实模型的端到端验证 |

## 具体发现

1. （低）app/wewe_client.py 文档字符串中有一处笔误（"webe 采集链路"），应为"web 采集链路"。
   → 已在本 Change 记录中标注，建议下个 Change 顺带修正。
2. （低）app/wewe_images.py 的 delete_article 不返回删除条数（Qdrant delete 不返回计数），
   改为返回每空间 0/1 标记；如需精确计数可先用 count 接口。→ 已在实现注释中标明。
3. （低）app/wewe_pipeline.py 中网站来源的 HTML 单页处理会把整页当一篇文章，未做正文区域
   抽取。→ 已限定为"网站来源单页降级路径"，首期以 RSS/Atom 为主。
4. （提示）wewe_articles.content_html / text_content 保存完整正文，数据库体积会随采集增长，
   后续可加保留期清理任务。→ 记入 memory.md 的后续 Change 建议。

## 无发现项（已确认）

- 未发现把视觉描述文本向量当作原生图片向量对外输出的路径。
- 未发现跨用户数据越权路径（存储与向量两层都按 user_id 过滤，并有测试）。
- 未发现硬编码密钥、令牌或上游 .env 内容被写入代码或文档。
- 未发现对既有认证、计费、部署配置文件的修改。

## 结论

实现符合已批准的需求与架构；遗留项均为低风险且已记录。允许进入发布审批（生产发布仍需人工批准）。

