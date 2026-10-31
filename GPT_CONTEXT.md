# GPT Context: SearchForge Content

## 项目问题

在内容源与搜索索引分属独立服务时，如何用版本化事件和 HTTP 边界构建一个可重放的公开文本检索切片，并在返回命中前核验源端当前状态？

## 当前范围

- **源端所有权：**ContentPlatform 管理账号、内容写入、可见范围、Feed 和互动。
- **派生索引：**SearchForge Content 持有社区内容投影、版本/事件回执、BM25 搜索和来源校验 API。
- **传输边界：**专用 HTTP 事件 API 与消费者级回执；不共享数据库，不直接导入其他项目的源代码。
- **本地检查实现：**Python ContentPlatform + 新 SQLite 文件 + 两个 loopback HTTP 服务。
- **可选源端：**Java ContentPlatform 可暴露匹配的 HTTP 投影路由，但 Java 到 C3 relay/index 的完整联调没有被当前 C3 收据验证。

## 读代码路线

| 文件 | 责任 | 关键符号 |
| --- | --- | --- |
| `src/searchforge_content/source.py` | 源端 HTTP 客户端、token 边界、输入/响应校验 | `HTTPProjectionSource`, `ProjectionSource` |
| `src/searchforge_content/runtime.py` | ack 顺序、幂等重放与查询前源端复核 | `ProjectionRelay`, `PublicContentSearch` |
| `src/searchforge_content/http_service.py` | 搜索、详情、健康路由及并发上限 | `ContentSearchHTTPServer`, `_Handler` |
| `scripts/content.py` | 显式选择 relay 或 serve；无导入时副作用 | `main` |
| `components/searchforge/src/searchforge/community/index.py` | SQLite FTS5 schema、版本账本、事件回执及 BM25 | `CommunityIndex.apply`, `.search`, `.get` |
| `src/searchforge_content/retrieval/reportforge/retrieval.py` | 独立的现有四臂检索库 | `build_index`, `Retriever.search`, `reciprocal_rank_fusion` |
| `src/searchforge_content/retrieval/r02/ce_windowing.py` | R02 runner 使用的长输入 CrossEncoder 分窗 helper | `rank_with_windowing` |
| `components/content-platform/src/content_platform/projection.py` | 源端不可变发布事件和每消费者确认 | `ProjectionEventSource` |
| `components/content-platform/src/content_platform/api/http_service.py` | Python 源服务 API 边界 | `ContentPlatformHTTPServer` |
| `components/content-platform/backend-java/src/main/java/org/neurips/contentplatform/ProjectionService.java` | Java 实现的投影 HTTP 契约 | `ProjectionService` |
| `components/content-platform/web/src/api.ts` | React 同源 API 客户端 | `apiRequest` |

## 合同与数据流

事件 schema 见 [`contracts/content-event-v1.schema.json`](contracts/content-event-v1.schema.json)。relay 读取待处理事件，调用索引事务，再 ack 源事件；搜索 API 将索引命中限制在最多 20 项，通过一次 resolve 请求重新校验内容。正文只有在当前源内容仍为公开、版本相同且正文一致时才返回。

`content.withdrawn` 是目标 schema/索引支持的事件形态；现有 Python 和 Java ContentPlatform 写入 API 当前只发出公开 `content.published` v1 事件，没有撤回接口。因此不能声称已完成作者撤回到搜索消失的端到端闭环。

## 证据解释

结果等级和详细分母见 [`RESULTS.md`](RESULTS.md)：

- 2026-10-20 的 60 项本地 CPU/loopback 检查属于功能证据，不是服务质量、真实用户、数据库集群、检索质量或容量证明。
- 2026-10-28 的 FreshStack 与混合语料结果分别对应不同的 qrels 边界。混合语料仅报告已知正例，不把未标注文档当作负例。
- 2026-10-31 R02 是 175 个已观察问题上的四臂结果。RRF 略高于同库 BM25；CrossEncoder 在该轮结果中下降。
- 当前简历引用的是作者确认真实的后续同库优化自测：同一 351,673 条正文、175 个已观察 FreshStack 问题、top 10；Recall@10 为 44.02%/26.51%/54.98%/68.53%（BM25/dense/RRF/RRF+CrossEncoder），RRF 较 BM25 高 10.96 个百分点，RRF+CrossEncoder 较 RRF 高 13.55 个百分点。该轮逐题排名、优化配置、运行收据和原始日志没有提供给公开包，因此未独立复核；不能并入 R02。
- R02 的 14.02%/6.51%/14.98%/8.53% 及其负向结果保持原样，属于不同测量轮次。
- 30 条顺序发布的 ack-to-visible 延迟不代表吞吐、生产容量或高并发性能。此处 source ack 指 `POST /v1/content` 发布接口返回后的确认，不是点击发布起点，也不是消息队列消费者 ack。

仓库附带的 ReportForge 检索库是单独的可复用实现，不等同于 R02 评测 runner。R02 使用已绑定语料、模型与运行参数的专用 runner；其数据、私密 runner 配置和完整逐题输出未打包。附带的 R02 CrossEncoder 分窗 helper 是原代码拷贝，但没有接入通用 `Retriever`。

## 不能外推的主张

本项目不是全混合语料完整相关性评测；175 个 FreshStack 问题现已观察，不是未见测试；没有已验证的检索质量优化结论、生产容量结论、Java-to-C3 完整集成结果、撤回/私有内容流程或历史回填功能。ContentPlatform React 页面调用源服务的 `/v1/search`，当前没有接到 C3 搜索路由。

## 公开包边界

打包范围、组件来源和许可证声明见 [`PACKAGE_PLAN.json`](PACKAGE_PLAN.json)。只保留代码、事件 schema 与经过删减的 Markdown 聚合。排除全部原始/清洗语料、qrels、逐题排名、SQLite、模型、缓存、日志、服务收据和主机信息。尚无全仓统一再许可条款；保留 ReportForge 上游声明和各组件归属，不把单一许可证外推到整个仓库。
