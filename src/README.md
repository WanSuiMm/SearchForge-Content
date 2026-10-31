# 源码目录

`searchforge_content/source.py` 持有专用 HTTP 源客户端；`runtime.py` 持有提交后确认的 relay 和有界批量源端校验；`http_service.py` 提供搜索／详情 API。持久索引复用 SearchForge 的 `community` 模块，真源归 ContentPlatform；没有复制旧组件代码。边界见[架构说明](../ARCHITECTURE.md)。
