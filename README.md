# SearchForge Content

SearchForge Content 把公开内容发布事件投影到独立的 SQLite FTS5 索引，并在返回搜索结果前通过内容真源 HTTP API 重新核验内容状态。仓库包含 C3 自有客户端、relay、索引与搜索 API，也按组件归属包含 ContentPlatform 参考实现和 ReportForge 的既有四臂检索库。

## 阅读顺序

1. [GPT_CONTEXT.md](GPT_CONTEXT.md)：项目目标、可支持的主张和证据边界。
2. [RESULTS.md](RESULTS.md)：一页结果汇总，区分作者独立重跑汇总与作者报告值。
3. [ARCHITECTURE.md](ARCHITECTURE.md)：事件、HTTP、索引、Java 和 React 的职责与集成缺口。
4. [content-event-v1.schema.json](contracts/content-event-v1.schema.json)：源端与索引消费者之间的版本化事件契约。
5. `src/searchforge_content/`、`scripts/content.py` 与 `components/searchforge/src/searchforge/community/`：C3 发布投影和 FTS5 内容检索代码。
6. [`src/searchforge_content/retrieval/reportforge/retrieval.py`](src/searchforge_content/retrieval/reportforge/retrieval.py) 与 [`components/retrieval/README.md`](components/retrieval/README.md)：BM25、dense、RRF 与 CrossEncoder 的既有可选库实现及来源说明。
7. `components/content-platform/` 和 `tests/`：源端参考实现及本地验证代码。
8. [PACKAGE_PLAN.json](PACKAGE_PLAN.json)：来源、目标路径、排除项、导入路径改造和组件许可证信息。

## 已实现的功能切片

- ContentPlatform 在一次发布事务中写入内容和独立的 `content.published` 投影事件；通知 Outbox 与索引消费者回执彼此独立。
- C3 relay 通过带专用 bearer token 的 HTTP 拉取事件，先在本地索引事务提交，再向源端 ack。丢失 ack 后可以幂等重放；这不是跨服务 exactly-once。
- `CommunityIndex` 以 SQLite FTS5/BM25 保存公开内容、版本和事件回执。返回候选前，C3 用一次有界批量请求核验源端公开状态、版本和正文。
- 搜索 API 提供 `/healthz`、`/v1/search` 与 `/v1/content/{content_id}`；健康路由只表示进程存活。
- 独立的检索库保留已有 BM25、SentenceTransformers/FAISS dense、RRF 与 CrossEncoder 方法，供使用者对自己的语料调用。它不是当前 C3 FTS5 HTTP 服务的一部分；不同代码、分块与评分口径不能互相替代。
- ContentPlatform Python/SQLite 源端是本地功能检查的参考实现。另附的 Java/PostgreSQL 服务实现相同投影 HTTP 契约，但 Java 到 C3 索引的端到端联调尚无对应验收结果。

React 页面仍调用 ContentPlatform 自己的同源 `/v1/search`，不会调用 C3 的搜索 API。页面和后端可单独构建、运行；它们当前不构成 C3 索引的前端闭环。

## 最小本地运行

运行要求：Python 3.11+，SQLite 带 FTS5。默认 Python/SQLite 路径只使用标准库。先在安全的进程环境中配置 `CONTENT_PROJECTION_TOKEN`；不要将 token 放入文件、命令历史或日志。

在仓库根目录，用一个终端启动新建的源端数据库：

```powershell
New-Item -ItemType Directory -Force .local\state | Out-Null
python -B components/content-platform/scripts/server.py `
  --database .local/state/source.sqlite3 --initialize --port 8812 `
  --projection-token-env CONTENT_PROJECTION_TOKEN `
  --projection-consumer-id searchforge-content-v1
```

通过 ContentPlatform 注册用户、登录并发布一条公开内容后，在另一个终端运行一次有界 relay：

```powershell
python -B scripts/content.py relay `
  --source-url http://127.0.0.1:8812 `
  --source-token-env CONTENT_PROJECTION_TOKEN `
  --index .local/state/community.sqlite3 --initialize `
  --batch-size 100 --max-batches 1
```

启动只读搜索 API：

```powershell
python -B scripts/content.py serve `
  --source-url http://127.0.0.1:8812 `
  --source-token-env CONTENT_PROJECTION_TOKEN `
  --index .local/state/community.sqlite3 --port 8813
```

查询例子：`http://127.0.0.1:8813/v1/search?q=observability`。首次 relay 使用新索引路径并传 `--initialize`；之后不要指向其他应用数据库或历史结果文件。`.local/`、SQLite、日志、模型缓存和原始数据必须留在仓库外或被忽略。

## 检查入口

可运行：

```powershell
python -B scripts/verify.py --output .local/verification/verification.json
```

历史 2026-10-20 收据记录了 60 项 CPU/loopback 功能检查通过。该历史检查只覆盖合成公开文本、临时 SQLite 和本机 HTTP；本次打包没有运行代码、服务、测试或实验，也没有为新组装包生成新验证收据。历史原始收据不随公开包分发。

## 可选四臂检索代码

从仓库根目录安装可选依赖：

```powershell
python -m pip install -e ".[retrieval]"
```

`searchforge_content.retrieval.reportforge.retrieval` 暴露 `build_index` 和 `Retriever`。`Retriever.search` 支持 `method="bm25"`、`"dense"`、`"hybrid"` 和 `"hybrid-rerank"`。真实 dense 需要 `backend="faiss"`；显式 `fixture` 模式使用确定性哈希向量，只用于软件流程。CrossEncoder 模型权重不随仓库分发。R02 评测专用的超长输入分窗函数在 `searchforge_content.retrieval.r02.ce_windowing`，是独立 helper，尚未接入 ReportForge `Retriever`。

## 可选 Java 与 React 组合

Java 服务使用 Java 21、Maven 与外部 PostgreSQL；PostgreSQL 连接值和投影 token 必须由进程环境或密钥管理器提供。React 静态资源需在 `components/content-platform/web/` 执行 `npm ci` 和 `npm run build`；构建会生成 `app.js`，无需将构建输出或 npm 缓存提交。Java 的静态根应指向同一组件的 `web/`。Redis、Kafka 是可选能力，不属于 C3 本地 SQLite 检查的前置条件。

本仓库没有分发原语料、qrels、检索逐题结果、SQLite 数据库、模型、缓存、运行日志或服务主机信息。尚未选定覆盖全仓的统一再许可条款；ReportForge 上游 `pyproject.toml` 声明 MIT，SearchForge Content、SearchForge 和 ContentPlatform 源目录未见根级 LICENSE/NOTICE。组件来源与条款边界见 [PACKAGE_PLAN.json](PACKAGE_PLAN.json)。
