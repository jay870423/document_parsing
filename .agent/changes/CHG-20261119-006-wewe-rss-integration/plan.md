# Implementation Plan: wewe-rss 能力集成

- Change: CHG-20261119-006-wewe-rss-integration
- Date: 2026-11-21
- Level: complex
- Execution mode: serial（同一批新增模块共享数据模型与接口契约，串行开发、逐项验证）

## T1 上游契约核对与边界冻结

- Outcome: 确认可调用的上游接口、鉴权方式、分页与错误语义，冻结需求边界。
- Files: requirements.md、architecture.md（无代码变更）
- Inputs: zhouyuan-wewe-rss 源码（feeds.controller.ts、feeds.service.ts、trpc.router.ts、auth.service.ts）
- Outputs: 冻结的边界决策与契约表
- Blockers: 无
- Validation: 人工核对源码 + 记录到 requirements.md
- Owner: controller
- Mode: serial

## T2 适配器与安全基座

- Outcome: 可安全获取上游文章与外部资源的客户端。
- Files: app/wewe_client.py、app/wewe_fetcher.py、app/wewe_sanitize.py
- Inputs: T1 契约
- Outputs: 客户端、SSRF 抓取器、HTML 清洗器
- Blockers: T1
- Validation: tests/test_wewe_adapter.py
- Owner: controller
- Mode: serial

## T3 存储与双向量空间索引

- Outcome: 元数据落库 + 文本/图片两个向量空间的写入、删除、检索。
- Files: app/wewe_store.py、app/wewe_images.py、app/config.py
- Inputs: T2 输出
- Outputs: wewe_* 表、WeWeVectorIndex、ImageEmbeddingClient、WEWE_* 配置项
- Blockers: T2
- Validation: tests/test_wewe_pipeline.py（存储隔离、幂等）
- Owner: controller
- Mode: serial

## T4 采集编排与定时调度

- Outcome: 手动/定时采集全链路，图文分离并入索引。
- Files: app/wewe_pipeline.py、app/wewe_scheduler.py
- Inputs: T2、T3
- Outputs: collect_source / delete_article / 调度线程
- Blockers: T3
- Validation: tests/test_wewe_pipeline.py
- Owner: controller
- Mode: serial

## T5 接口层与 RAG 集成

- Outcome: 来源/文章/任务/图片检索接口，RAG 命中可回溯图片。
- Files: app/wewe_api.py、app/main.py
- Inputs: T4
- Outputs: /api/wewe/* 路由；RAG 接口新增 include_images 与 image_hits
- Blockers: T4
- Validation: tests/test_wewe_api.py + python -m compileall -q app
- Owner: controller
- Mode: serial

## T6 测试、文档与验收

- Outcome: 测试证据、运维文档、Change 记录闭环。
- Files: tests/test_wewe_*.py、docs/wewe-rss-integration.md、.env.example、.agent/changes/CHG-20261119-006-*
- Inputs: T1–T5
- Outputs: 26 个用例通过、ci.md、summary.md、memory.md
- Blockers: T5
- Validation: python -m pytest tests -q；git diff --check
- Owner: controller
- Mode: serial

## 依赖图

    T1 -> T2 -> T3 -> T4 -> T5 -> T6

## 结果

全部任务完成并通过验证；未修改既有认证、计费、部署配置与用户既有改动。

