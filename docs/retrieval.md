# 检索与引用

Dense 与 PostgreSQL FTS 在候选 SQL 层应用 `KnowledgeScope` 和 Document READY；两路候选经 RRF 融合，再由 BGE Reranker 排序。Reranker 超时保留 RRF 顺序并标记 degraded。Context Builder 在固定 token budget 内选择证据，生成时保存 document/version/chunk/section/page 快照；无有效快照的事实回答 fail-closed。

Redis、前端或模型不得改变候选权限、RRF 分数或引用身份。术语固定为 PostgreSQL FTS，不称为 BM25。
