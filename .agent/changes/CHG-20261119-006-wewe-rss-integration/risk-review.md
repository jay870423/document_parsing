# Risk Review: wewe-rss 能力集成

- Change: CHG-20261119-006-wewe-rss-integration
- Date: 2026-11-21
- Reviewer: controller（主控自评，未启用独立 Harness 角色）
- Result: PASS with conditions（生产发布仍需人工批准）

## 风险矩阵

| # | 风险 | 等级 | 缓解措施 | 证据 |
|---|---|---|---|---|
| R1 | SSRF：采集站点/图片地址指向内网或云元数据端点 | 高 | 仅 http/https；DNS 全解析公网校验；逐跳重定向复验；拒绝内网/回环/链路本地/保留/组播；超时与字节上限 | app/wewe_fetcher.py；tests/test_wewe_adapter.py（4 个 SSRF 用例） |
| R2 | 恶意 HTML / XSS 污染正文与控制台 | 高 | 标签属性白名单清洗，丢弃 script/style/iframe/object/embed/svg、事件属性与 javascript: 协议 | app/wewe_sanitize.py；清洗用例 |
| R3 | 凭据泄漏（上游授权码、Ark Key） | 高 | 只从环境变量读取；不入库、不写日志；不加入控制台可写配置项 CONFIG_KEYS | app/config.py、app/main.py CONFIG_KEYS 未新增 WEWE_* |
| R4 | 跨用户/跨知识库数据泄漏 | 高 | 存储层按 user_id 过滤；向量 payload 绑定 owner_user_id + knowledge_base_id 并在检索时过滤 | app/wewe_store.py、app/wewe_images.py |
| R5 | 资源耗尽（超大响应、海量图片、频繁采集） | 中 | 响应体上限、单文图片上限、单源文章上限、超时、串行调度、图片魔数校验 | config 默认值 + wewe_pipeline 限额 |
| R6 | 上游不可用/接口变更/限流 | 中 | 客户端超时与重试；失败记录到 source.last_error 与任务表；手动/定时两条路径均不阻塞其他来源 | app/wewe_client.py、wewe_store 任务表 |
| R7 | 重复文章与重复向量 | 中 | 唯一键 upsert；索引前按 article_id 删除旧点位 | wewe_pipeline._index_article；幂等用例 |
| R8 | 图片向量与描述文本混淆，对外误称原生图片检索 | 中 | 双集合 + vector_space/chunk_type 标注；接口与文档分别说明 | wewe_images.py、docs/wewe-rss-integration.md |
| R9 | 部分成功（图片失败、正文成功）状态不一致 | 中 | 图片行独立状态；文章仍标记 indexed 并记录 image_count；任务状态 success/partial/failed | wewe_pipeline.collect_source |
| R10 | 上游合规与稳定性（授权方式、采集范围、服务条款） | 中 | 上游侧完成授权与订阅源维护；本服务不绕过授权；上线前需与上游确认授权与使用条款 | 需运维确认（发布前检查项） |
| R11 | 视觉模型/图片 embedding 成本与配额 | 低 | 视觉描述已有 QPS/重试限制；图片 embedding 失败降级为仅描述文本 | app/wewe_images.py（返回 None 即降级） |
| R12 | SQLite 并发写入（调度线程 + 请求线程） | 低 | 连接超时 30s、外键开启、事务提交；单表写入量小 | app/wewe_store.py |

## 验证覆盖

- SSRF/协议/重定向：tests/test_wewe_adapter.py
- XSS/清洗：tests/test_wewe_adapter.py
- 隔离：tests/test_wewe_pipeline.py（test_store_isolates_users）、tests/test_wewe_api.py
- 幂等与部分失败：tests/test_wewe_pipeline.py
- 降级（关闭图片向量）：tests/test_wewe_pipeline.py

## 结论

未发现必须阻断实现的高风险项；R1/R2/R3/R4 已有实现级缓解与测试证据。
生产发布前需人工确认：上游授权与合规（R10）、图片向量模型维度（R8/R11）、回滚演练。

