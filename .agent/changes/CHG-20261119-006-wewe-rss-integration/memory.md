# Memory: wewe-rss 能力集成

## What worked

- 直接阅读上游源码核对契约（feeds.controller.ts / feeds.service.ts / trpc.router.ts /
  auth.service.ts），一次性解决了此前阻塞的"上游接口是否为稳定公共 API"问题；
  结论是 feeds 的 HTTP 接口足够支撑采集，tRPC 可用于只读管理。
- 把"来源/文章/图片/任务"统一为本地 SQLite 模型 + 文本/图片两个向量空间，
  使公众号来源与网站来源共用同一套幂等、隔离与检索逻辑。
- 幂等采用"唯一键 upsert + 按 article_id 先删后写 + 确定性 point id"，
  重复采集不产生重复向量，删除来源可级联清理两个空间。

## What caused rework

- 通过 apply_patch 写大文件时，空行必须显式写 "+"，否则补丁被拒；改为先拼内容再批量加前缀。
- 早期未在测试里隔离 DNS 解析，导致 SSRF 校验把图片下载全部拦掉；
  补充 autouse fixture 固定放行后用例才稳定（生产代码不应放宽该校验）。

## Reusable decision or rule

- 视觉描述文本向量与原生图片向量必须分集合、分标识（vector_space / chunk_type），
  并在接口与文档中分别表述，不得互相宣传。
- 外部内容源接入必须自带 SSRF 防护（协议白名单 + DNS 全解析公网校验 + 逐跳重定向复验 +
  字节上限）与 HTML 白名单清洗，且必须有对应单测。
- 含凭据的新增配置项不应进入控制台可写配置清单，避免落盘与回显。

## Proposed Harness improvement

- Target: rule / skill / Wiki / template / test
- Evidence: 本次 Change 中 26 个用例均为新增；CI 已能自动发现 tests/ 下的用例。
- Owner: controller
- Status: proposed —— 建议后续 Change 增加"外部内容源接入"检查清单模板
  （契约冻结、SSRF、清洗、幂等、双向量空间、隔离、限额、降级）。

## Follow-up Change

- 生产启用前置：真实上游连通性 + 图片 embedding 模型维度验证 + 端到端演练。
- 控制台前端页面（来源/文章/图片浏览与管理）。
- wewe_articles 正文保留期与清理任务。
- 网站来源的正文区域抽取（当前单页 HTML 走整页降级路径）。
- Status: open；Owner: controller

