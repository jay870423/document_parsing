# CI Acceptance: wewe-rss 能力集成

- Change: CHG-20261119-006-wewe-rss-integration
- Date: 2026-11-21
- CI entry: .github/workflows/ci.yml
- Result: PASS（本地等价检查全绿；托管 CI 证据待推送后补录）

## 检查项与结果

| CI 步骤 | 本地等价命令 | 结果 |
|---|---|---|
| Compile Python sources | python -m compileall -q app | PASS（exit 0；仅有 main.py 既有 SyntaxWarning: invalid escape sequence，位于既有 debug 页面代码，非本次改动） |
| Check diff whitespace | git diff --check | PASS（exit 0；仅提示既有 .agent 文件 LF/CRLF 转换警告） |
| Validate Harness metadata | python -c "import yaml; yaml.safe_load(...)" | PASS（Harness metadata OK） |
| Run tests when present | python -m pytest tests -q | PASS（26 passed） |

## 测试清单

tests/test_wewe_adapter.py（15）
- HTML 清洗：脚本/样式/事件属性/危险协议/data URI 移除，正文与图片抽取、相对地址补全
- SSRF：非 http(s) 协议拒绝、回环/内网 IP 拒绝、私网解析拒绝、逐跳重定向复验、响应体超限
- 图片魔数：jpeg/png 识别、SVG 拒绝
- 上游契约：feeds 列表解析、Authorization 头透传、文章 items 解析、缺配置报错、上游错误码

tests/test_wewe_pipeline.py（6）
- 存储隔离（跨用户不可见）、文章 upsert 幂等
- 采集编排：正文入文本空间、描述文本入文本空间、原生图片向量入图片空间
- 重复采集幂等（先删后写）、单篇失败不影响整批、关闭图片向量时降级为描述文本索引

tests/test_wewe_api.py（5）
- 来源创建/列表/采集、参数校验、删除来源级联清理文章向量、跨用户文章不可见、状态接口

## 回归说明

- 未修改 app/billing.py、app/rag.py、app/pipeline.py 的既有行为；
- app/main.py 仅新增路由注册、实例装配与 RAG 的可选 include_images/image_hits 字段，
  既有响应字段保持不变；
- .env.example 仅追加配置示例，未改动既有键。

## 未覆盖项（发布前补）

- 真实上游 WeWe RSS 连通性、真实 Ark 图片 embedding 模型维度与稳定性（需凭据环境）。
- 端到端演练：1 个公众号来源 + 1 个网站来源的小规模采集与 RAG 命中回溯。

