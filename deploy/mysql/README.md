# document_parsing 独立 MySQL 8 部署与回滚

## 安全边界

- 只使用 `document_parsing` 数据库和 `document_parsing_app@localhost` 账号；禁止修改、导出或清空其他项目数据库。
- 不修改 MySQL `bind-address`，不重启 MySQL；app 容器只挂载 `/var/run/mysqld/mysqld.sock`。
- 密码只写生产 `.env` 或 root-only env，权限 `600`，不得提交 Git。
- 不执行 `docker compose down`，不重建 nginx/Qdrant，不删除 SQLite、artifacts 或 Qdrant 数据卷。
- 默认 dry-run；只有行数、主键和逐行内容摘要全部一致才允许切换生产。

## 一次性建库与最小权限账号

由管理员通过本机 `sudo mysql` 执行；密码应使用密码管理器生成并只写生产 `.env`：

```sql
CREATE DATABASE document_parsing CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'document_parsing_app'@'localhost' IDENTIFIED BY 'CHANGE_ME_DOC_ONLY';
GRANT ALL PRIVILEGES ON document_parsing.* TO 'document_parsing_app'@'localhost';
FLUSH PRIVILEGES;
```

生产 `.env` 至少需要：

```dotenv
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=document_parsing_app
MYSQL_PASSWORD=CHANGE_ME_DOC_ONLY
MYSQL_DATABASE=document_parsing
MYSQL_UNIX_SOCKET=/var/run/mysqld/mysqld.sock
MYSQL_CONNECT_TIMEOUT=10
MYSQL_READ_TIMEOUT=30
MYSQL_WRITE_TIMEOUT=30
SQLITE_SOURCE_PATH=data/app.db
```

`docker-compose.yml` 的 app 服务必须包含：

```yaml
volumes:
  - /var/run/mysqld/mysqld.sock:/var/run/mysqld/mysqld.sock
```

## 备份、迁移与核验

```bash
# 1. 在线一致性 SQLite 快照 + 代码/env/artifacts + 已有 MySQL dump。
SOURCE_GIT_SHA='<commit>' scripts/backup_doc.sh /home/ubuntu/document_parsing

# 2. 默认 dry-run：只读 SQLite，不创建表、不写业务行。
APP_ENV_FILE=/home/ubuntu/document_parsing/.env \
  python3 scripts/migrate_sqlite_to_mysql.py \
  --sqlite /path/to/consistent-backup/data/app.db \
  --report /path/to/migration-dry-run.json

# 3. 目标业务表必须为空（plans seed 除外）；禁止 --force 清库。
APP_ENV_FILE=/home/ubuntu/document_parsing/.env \
  python3 scripts/migrate_sqlite_to_mysql.py --apply \
  --sqlite /path/to/consistent-backup/data/app.db \
  --report /path/to/migration-apply.json

# 4. resume 只补缺失源行，绝不覆盖目标行，并再次逐行校验。
APP_ENV_FILE=/home/ubuntu/document_parsing/.env \
  python3 scripts/migrate_sqlite_to_mysql.py --apply --resume \
  --sqlite /path/to/consistent-backup/data/app.db \
  --report /path/to/migration-verified.json

# 5. 只重建 doc app。
cd /home/ubuntu/document_parsing
sudo docker compose build app
sudo docker compose up -d --no-deps app

# 6. doc 匿名边界 + MySQL/Qdrant + study 首页。
scripts/verify_doc_study.sh
```

## 回滚

1. 保留 MySQL 新库，禁止把 MySQL 反向覆盖 SQLite（避免丢失切换后的新数据）。
2. 使用备份目录中的旧代码、旧 `.env`、`docker-compose.yml` 和旧镜像 ID 恢复 app。
3. 只执行 `docker compose up -d --no-deps app`，不操作 nginx/Qdrant。
4. 再运行 `scripts/verify_doc_study.sh`；若回滚后需要继续写入，先明确新旧数据源与增量处理方案，不得双库无序双写。
