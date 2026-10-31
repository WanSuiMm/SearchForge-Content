# Results and claim boundaries

下表只保留决策相关的汇总。各行属于不同证据类型、任务边界或来源，不构成统一排行榜。公开包不含原始语料、qrels、逐题排名、运行日志或服务收据；本表不能替代这些原始材料的独立复算。

| 记录 | 证据来源 | 范围与结果 | 可支持的结论 / 限制 |
| --- | --- | --- | --- |
| 2026-10-31 作者独立重跑汇总（R02） | 已完成并做过保存文件静态核对的四臂运行 | 351,673 条正文、4,945,509 个窗口；175 个已观察 FreshStack 问题，每臂175/175、共700项、0失败。已知正例 Recall@10：BM25 **14.02%**、dense **6.51%**、RRF **14.98%**、RRF+CrossEncoder **8.53%**。 | 标签限于原 FreshStack qrels；语料其他部分未完整标注，不能报告全库相关性或全库 nDCG。RRF 比同库 BM25 高0.9621个百分点；CrossEncoder 比 RRF 低6.4531个百分点。73题的前10条没有命中已知正例。该175题已观察。 |
| 2026-10-31 作者报告优化值 | 作者报告同范围与数值；本轮逐题结果、优化配置、回执和原始日志未收录 | 据报告为同一351,673文档、175题、top10；Recall@10：BM25 **44.02%**、dense **26.51%**、RRF **54.98%**、RRF+CrossEncoder **68.53%**。 | 作者报告值，未独立核验。与 R02 分开记录；不能由这些数字推断模型/配置、失败数、逐题结果、延迟、nugget 覆盖或容量，也不能把它们写成 R02 的更高版本结果。 |
| 2026-10-28 FreshStack 与混合语料诊断 | 已保存的 C3 服务器运行汇总 | 官方 FreshStack 基线：49,505 个文档ID、175题，Recall@10 **0.0993**、graded nDCG@10 **0.1034**、nugget coverage@10 **0.2950**。混合200K库：已知正例 Recall@10 **0.0748**、已知标签 nugget coverage@10 **0.2817**、FreshStack 正例映射保留率 **0.4669**。 | 只在完整 FreshStack qrels 上报告第一组完整指标。混合库无全库 qrels；未判定文档不是负例，混合结果不是全库质量评价。不同语料/标签边界不得直接对比。 |
| 2026-10-28 发布可见性 | 隔离测试库中的顺序功能检查 | 30/30条公开测试内容在源端确认后可被搜索；ack 到首次有效搜索可见 P50 **437.351 ms**、P95 **588.479 ms**，无超时。 | 30条顺序样本，不是吞吐、并发容量、生产 SLA 或检索质量结果。 |
| 2026-10-20 本地功能链 | 保存的功能验证收据 | 60项 CPU/loopback 检查通过，覆盖源事件、relay、独立 SQLite FTS5 索引、搜索/详情和故障重放路径。 | 合成公开文本、临时 SQLite 和真实本机 HTTP；不代表真实用户、PostgreSQL/Kafka/OpenSearch、检索质量或容量。 |

## 语料、标签与引用

R02 的标签来自 FreshStack 官方任务数据；问题与 qrels 是分离输入。公开包不包含任务文本、判断、来源文档、索引、嵌入或检索轨迹。FreshStack 数据集卡列明 CC BY-SA 4.0，并提醒各底层来源仓库可能另有许可证；不得把数据集总许可证当作所有原文来源的再发布许可。参见 [FreshStack 数据集](https://huggingface.co/datasets/freshstack/corpus-oct-2024)、[FreshStack 评测代码](https://github.com/fresh-stack/freshstack/tree/f1c4ec96477f5100f10c83798d33b3101db727fa)、[FreshStack 论文](https://arxiv.org/abs/2504.13128) 与 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)。

351,673条语料是多源去重正文，不是 SearchForge 创作内容。FineWiki、Simple English Wikipedia 与游戏 Wiki 等输入的来源条款各自适用；本包不再分发原文或数据导出。混合语料未有覆盖全库的 qrels，FreshStack 的已知正例只是部分标签视角。

## 当前未验证

- 作者报告优化值的运行身份、配置、逐题输出与失败分母。
- 混合语料全库相关性、未见测试集泛化和真实用户的检索收益。
- Java ContentPlatform → C3 relay → SearchForge 索引的端到端运行；Java 端 H2/Maven 检查或其他 Java 服务结果不能替代它。
- React UI 对 C3 索引的搜索交互；现有 UI 搜索 ContentPlatform 自身的 `/v1/search`。
- 生产吞吐、并发容量、持续运行、真实部署 SLA、撤回闭环和历史回填。

本打包回合只做静态阅读和文档整理，没有运行代码、测试、服务、模型或实验。

## Included retrieval implementation

`src/searchforge_content/retrieval/reportforge/retrieval.py` is an existing ReportForge library implementation with explicit BM25, SentenceTransformers/FAISS dense retrieval, RRF, and optional CrossEncoder reranking. It supports reading and use of the methods, but it is not the R02 runner and no score in this table is attributed to this library. The separate `src/searchforge_content/retrieval/r02/ce_windowing.py` is the R02 runner's pure long-input helper; it is not integrated into the ReportForge retrieval class. See [ARCHITECTURE.md](ARCHITECTURE.md) and [components/retrieval/README.md](components/retrieval/README.md).
