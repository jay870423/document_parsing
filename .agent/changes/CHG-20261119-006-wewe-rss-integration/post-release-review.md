# Post-release Review: wewe-rss 能力集成

- Release: 2026-11-21（服务器时间 10:42–10:50 CST）
- Change: CHG-20261119-006-wewe-rss-integration
- Reviewer: controller
- Result: healthy（线上行为与发布前一致，新能力默认关闭）

## 健康证据

- document-parser-app 已用新镜像重建并正常启动，日志无异常。
- https://doc.zhouyuaninfo.com.cn/health 返回 200 与 {"status":"ok"}。
- 首页与 /docs 均 200；/openapi.json 含 9 条新的 /api/wewe/* 路由。
- 新接口与既有接口同样返回 401（未携带会话/密钥），鉴权未被绕过。
- 生产 SQLite 自动新增 wewe_sources / wewe_articles / wewe_images / wewe_collect_tasks，
  既有表与数据未受影响。
- Qdrant 集合仍为 manual_chunks_v2，未新建集合、未删除既有向量（图片向量默认关闭）。

## 行为影响评估

- 既有文档解析、文本 RAG、计费、认证路径未改动，发布前后接口契约一致。
- 新增的定时采集线程默认不启动（WEWE_COLLECT_ENABLED=false），不会产生后台负载。
- 新增 SQLite 表在应用启动时创建，属幂等 DDL，无迁移风险。

## 备份与回滚

- 备份：/home/ubuntu/document_parsing-backup-wewe-20261121-104217.tgz（5.5MB，281 条目）。
- 回滚步骤见 deployment.md；回滚不涉及 Qdrant 卷删除与数据清空。

## 事故与异常

- 未观察到服务中断；Compose 重建窗口内健康检查正常。
- 服务器内存占用 76%、swap 100%（既有状态，与本次发布无关），建议运维另行关注。

## 安全跟进

- SSH 密码在对话中暴露，建议立即轮换；本次已确保其不进入命令行参数、脚本、文件与远端历史。
- 服务器 .env 未被读取或修改。
- 主机密钥为本次环境首次固定（TOFU），建议与腾讯云控制台核对并归档。

## 后续

- 待上游 WeWe RSS 部署与图片 embedding 模型确认后，再配置 WEWE_* 并开启采集。

