# 文档解析服务（图文分离 + 结构化识别 + RAG 问答）

本项目支持：

1. PDF / DOCX 解析  
2. 图片结构化提取（Doubao Vision）  
3. 文本与图片内容按原位置拼接  
4. 向量入库 + RAG 检索/问答（Qdrant）  

## 快速启动

1. 复制配置文件

```powershell
Copy-Item .env.example .env
```

2. 安装依赖并启动

```powershell
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

3. 打开页面：`http://127.0.0.1:8000/`

## Windows 客户一键启动（BAT）

提供 `run_customer.bat`，双击即可完成以下动作：

1. 自动创建虚拟环境 `.venv`  
2. 自动安装/更新依赖（`requirements.txt`）  
3. 自动创建独立配置文件（默认 `.env.customer`）  
4. 自动尝试启动 Qdrant（若本机已安装 Docker）  
5. 启动服务并自动打开浏览器  

默认运行：

```bat
run_customer.bat
```

指定配置文件运行（例如给不同客户不同配置）：

```bat
run_customer.bat .env.client_a
```

说明：

- 程序会通过环境变量 `APP_ENV_FILE` 读取配置。  
- 若配置文件不存在，会从 `.env.customer.example` 自动复制生成。  
- 前端“系统配置”页保存时，会写回当前 `APP_ENV_FILE` 对应的配置文件。  

### 客户未安装 Qdrant / Docker 时

项目包含 `start_qdrant.bat`，并会在 `run_customer.bat` 中自动调用。

自动处理逻辑：

1. 先检测 `QDRANT_URL` 是否指向本地（`127.0.0.1`/`localhost`）。  
2. 若本地 Qdrant 未运行，自动尝试 `docker compose up -d qdrant`。  
3. 若未安装 Docker，会弹出安装引导：  
  - 有 `winget` 时可一键安装 Docker Desktop；  
  - 同时自动打开 Docker Desktop 下载页；  
  - 安装并启动 Docker 后，脚本会再次自动拉起 Qdrant。  
4. 若仍失败，可改为远程 Qdrant（修改配置文件中的 `QDRANT_URL`）。  

## Ark / Doubao 开通与配置

配置文件（如 `.env` / `.env.customer`）关键配置：

```env
ARK_API_KEY=your-api-key
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
DOUBAO_VISION_MODEL=doubao-vision-pro-32k
DOUBAO_EMBEDDING_MODEL=doubao-embedding
DOUBAO_CHAT_MODEL=doubao-pro-32k
VISION_MAX_RETRIES=3
```

### 控制台 SOP（含截图占位路径）

建议把截图放在 `docs/images/ark/`，以下是推荐文件名。

1. 登录与进入方舟  
操作：登录火山引擎控制台，进入火山方舟（Ark）。  
截图：`docs/images/ark/01-enter-ark-console.png`

2. 创建 API Key  
操作路径：`火山方舟 -> API Key 管理 -> 创建 API Key`。  
结果：复制新建的 Key（只展示一次）。  
截图：`docs/images/ark/02-create-api-key.png`

3. 检查模型可用性  
操作路径：`火山方舟 -> 模型列表`。  
目标：确认以下模型可用：  
- `doubao-vision-pro-32k`  
- `doubao-embedding`  
- `doubao-pro-32k`  
截图：`docs/images/ark/04-create-endpoint-vision.png`

4. 模型名不可用时创建 Endpoint  
触发条件：调用报 `model not found` 或权限错误。  
操作路径：`火山方舟 -> 在线推理 -> 创建推理接入点（Endpoint）`。  
做法：分别为 Vision / Embedding / Chat 创建接入点并记录 Endpoint ID。  
截图：  
- `docs/images/ark/04-create-endpoint-vision.png`  

5. 回填当前运行配置文件（如 `.env` 或 `.env.customer`）  
模型名可用时直接填模型名；否则填 Endpoint ID。  
截图：`docs/images/ark/07-env-mapping.png`

```env
DOUBAO_VISION_MODEL=doubao-vision-pro-32k   # 或 endpoint_id_xxx
DOUBAO_EMBEDDING_MODEL=doubao-embedding     # 或 endpoint_id_xxx
DOUBAO_CHAT_MODEL=doubao-pro-32k            # 或 endpoint_id_xxx
```

6. 本地验证  
操作：启动服务后访问 `GET /health`，再在前端执行一次“文档处理 + RAG 问答”。  
截图：  
- `docs/images/ark/08-health-check.png`  
- `docs/images/ark/09-rag-ui-success.png`

### 常见报错

1. `401 Unauthorized`：`ARK_API_KEY` 错误或过期。  
2. `model not found`：模型未开通，或应改用 Endpoint ID。  
3. `403 Forbidden`：账号无权限或地域不可用。  
4. `timeout`：网络/限流问题，降低并发并保留重试。  

## 前端页面

- `文档处理`：上传并解析文档，可选 Vision 与向量入库。  
  - `开始处理`：按当前选项执行处理。  
  - `清空并重建索引后处理`：先按 `source_filename` 清空 Qdrant 中同名手册索引，再重新入库，避免同名多次上传导致重复召回。  
  - `图文分离可视化`：处理完成后自动展示 `text_blocks` 与 `image_records`，支持按页筛选、图片在线预览、查看原始 JSON。  
- `系统配置`：在线修改当前 `APP_ENV_FILE` 指向的配置文件并热重载。  
- `RAG 问答`：仅检索/生成答案/证据联动。  

### 第4步拉通（前端闭环）

1. 在 `文档处理` 页上传手册，勾选 `处理后写入 Qdrant`。  
2. 处理成功后会自动完成切块+向量化+入库，并自动切换到 `RAG 问答` 页。  
3. `RAG 问答` 页支持：
   - 三种检索模式：`semantic / keyword / hybrid`
   - 来源筛选：可输入 `source_filename`，也可点 `刷新来源` 拉取已入库手册列表
   - 来源在线预览：证据区支持点击 `原始文件` 与 `解析全文` 链接
4. 检索证据返回包含分块元数据：`chunk_index / page_start / page_end / chapter / source_manual`。  

## 截图预览（docs/images/ark）
下面是当前仓库已放入的截图，使用 Markdown 直接预览：

### 01-enter-ark-console
![01-enter-ark-console](docs/images/ark/01-enter-ark-console.png)

### 02-create-api-key
![02-create-api-key](docs/images/ark/02-create-api-key.png)

### 04-create-endpoint-vision
![04-create-endpoint-vision](docs/images/ark/04-create-endpoint-vision.png)

### 07-env-mapping
![07-env-mapping](docs/images/ark/07-env-mapping.png)

### 08-health-check
![08-health-check](docs/images/ark/08-health-check.png)

### 09-rag-ui-success
![09-rag-ui-success](docs/images/ark/09-rag-ui-success.png)

## API

- `GET /health`  
- `POST /process`  
- `GET /api/config`  
- `POST /api/config`  
- `GET /api/rag/sources`  
- `POST /api/rag/search`  
- `POST /api/rag/answer`  

`POST /process` 表单参数支持：
- `run_vision`: `true` | `false`
- `ingest_vector`: `true` | `false`
- `rebuild_index`: `true` | `false`（当为 `true` 且 `ingest_vector=true` 时，先清空同名索引再入库）

`/api/rag/search` 与 `/api/rag/answer` 请求体支持：

- `retrieval_mode`: `semantic` | `keyword` | `hybrid`（默认 `semantic`）

### 检索模式区别

| 模式 | 原理 | 优点 | 适用场景 |
|---|---|---|---|
| `semantic`（语义检索） | 将问题向量化后做向量相似度检索 | 能识别同义表达，语义泛化好 | 用户描述不固定、问题偏自然语言 |
| `keyword`（关键词检索） | 基于文本分词与关键词匹配打分（BM25 风格） | 对精确词命中更敏感，包含“寄存器名/编号/型号”等术语时更稳 | 已知关键字段、精确词检索、术语型查询 |
| `hybrid`（混合检索） | 同时做语义检索和关键词检索，再融合排序 | 兼顾语义召回与精确命中，整体更均衡 | 默认推荐，通用问答场景优先使用 |

### 模式选择建议

1. 不确定用哪种时，优先 `hybrid`。  
2. 问题里有明确寄存器名、地址、编号、型号时，优先 `keyword`。  
3. 问题是口语化描述、没有固定术语时，优先 `semantic`。  

## Docker（可选）

```bash
docker compose up -d --build
```
