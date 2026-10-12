# 任务短期记忆与项目长期记忆

两种记忆仍通过现有上下文管理和 Token 预算进入模型，不会每轮注入全部历史。

## 短期：同一任务的工作记忆

原始记录保存在 SQLite `tasks.data`。`build_task_context` 从中构建 `working_memory`：
当前状态和错误、最近失败轮次、最新开发输出、未完成步骤、最近审查和 PR 信息。
任务摘要 v3 只保存原始需求、验收标准、有效性/撤销标记、约束及历史索引；
不再包含 actual_progress 或 latest_development。最近对话只保留用户请求与撤销语义，
轮次执行结果和最终 current_result 统一位于 working_memory。
摘要哈希只跟踪需求及撤销变化，执行状态变化不会使需求索引失效。
旧 v2 摘要在下一次保存或组装上下文时重建；完整对话和开发尝试保持原样，可继续分页查询。
历史失败标记为历史证据，不能覆盖后续成功记录；测试成功不能推断用户验收或发布成功。
`query_execution_memory` 按开发记录 offset 和 text_offset 分页读取完整输出。
已有 `query_task_history` 继续查询原始需求、反馈和 Diff。

Redis 只缓存工作记忆，不存唯一副本。SQLite `tasks.revision` 在事务中递增，
缓存键包含数据库命名空间、任务 ID、修订号和输入摘要（包括本轮未保存变更）。
读取时校验修订号和摘要；保存提交后仅清理旧修订缓存，新修订不受并发清理影响。
默认 TTL 为 1800 秒。Redis 不可用、缓存淘汰或内容损坏时直接从 Task 重建；
Task 的完整记录仍从 SQLite 加载。metadata.working_memory_cache_status 可查看命中/回退状态。
此回退针对工作记忆缓存；Celery 的 Broker 仍依赖其 Redis。缓存使用 DB 2，
并非独立 Redis 实例，整个 Redis 服务停机时后台队列也会受影响。

## 长期：同一仓库跨任务使用

SQLite 新表 `repository_memories` 按 repository_id + key 隔离和去重。
支持 architecture、convention、test_command、experience。保存来源、基线 SHA、更新时间和有效状态。
自动记录仓库分析观察，以及 success=true 且 exit_code=0 的实际执行命令；
不会把用户业务要求或模型自述自动提升成项目规范。
基线 SHA 不证明工作区没有未提交修改，所有历史记录均要求核对当前代码。

规划和开发上下文按相关性及类别选取最多四条，每条正文预览最多 600 UTF-8 字节。
开发探索工具 `query_project_memory` 可以分页读取原文，检索结果会传入后续开发上下文。
中文使用相邻双字匹配，英文使用词和路径匹配，并可通过 ChromaDB + 独立 Embedding
加入语义召回。Chroma 按数据库、仓库和 Embedding 模型隔离索引，只保存可重建副本。
搜索时补齐缺失索引、删除失效版本，并在向量调用后回查 SQLite 的内容版本和有效状态。
向量或 Embedding 服务不可用时退回词法检索，semantic_status / retrieval_mode 明确标记降级。
更多长期记忆可显式登记，不扫描整段聊天生成事实。

测试通过且最近审查批准、进入待发布/待合入/已合入状态的任务，可自动积累 experience。
保存需求预览、模型报告的修改摘要、验证结果及任务来源；模型摘要明确标记为报告，
不当作独立证实的修复结论。完整需求、修改和实际状态仍以 SQLite 的任务记录为准。
已有任务可运行 `python scripts/backfill-memory.py` 补齐符合条件的历史经验，
不会重写任务或把当前状态改成完成。本次本地部署补齐了 17 条历史经验。

配置见 `.env.example`：WORKING_MEMORY_REDIS_URL 使用 Redis DB 2（与 Celery 分离），
MEMORY_SEMANTIC_ENABLED 控制语义检索，MEMORY_CHROMA_URL 指向已有 Chroma HTTP 服务。
Embedding 复用 EMBEDDING_PROVIDER / MODEL / BASE_URL 配置；对外部服务配置会发送记忆文本。
MEMORY_MIN_SIMILARITY 默认 0.45，MEMORY_EMBEDDING_TIMEOUT_SECONDS 默认 8 秒。
索引延迟更新且可重建，不参与 SQLite 写入事务。服务恢复后下一次检索补齐索引。

API（现有本地 API 权限边界）：

- GET `/api/repositories/{id}/memories?query=组件&kind=convention&limit=4&offset=0`
- POST 同一路径，字段 key、kind、content、source，以及可选 source_sha。
- DELETE `/api/repositories/{id}/memories/{key}`：使记录失效，保留来源数据。

例如登记：

```json
{"key":"component_location","kind":"convention","content":"页面组件放在 src/pages，通用组件放在 src/components","source":"用户确认的项目约定"}
```

记忆不授予文件写权限，仍须通过 allowed_files 的原有校验。
未加入聊天界面的单独记忆管理面板；查询、登记和失效操作可使用 API 文档页面。

## 效果验证

运行 `python -m pytest tests/test_memory.py -q`。
专项场景包括重启后“重试一下”的失败证据、旧失败与最新成功区分、完整输出分页、
跨任务持久化、中文/英文/路径检索、跨仓库隔离、更新去重、失效过滤、长记录分页、
实际成功命令的来源、API 生命周期，以及规划模型实际收到记忆输入。

这是确定性检索和上下文传递测试，不等于真实模型在任意任务上的修复成功率。

真实服务评估：`python scripts/evaluate-memory.py --output docs/memory-evaluation-2026-10-12.json`。
使用独立临时数据库、人工构造的跨任务经验和真实 embeddinggemma 模型、Redis、Chroma。
四组改写问题中，词法检索 Hit@1=2/4，混合检索 Hit@1=3/4；漏检的远端新增文件问题保留在报告中。
这只是小样本检索对照，不代表真实业务成功率。脚本同时验证 Redis 断连回退、
Chroma/Embedding 不可用时词法回退，以及其他仓库和失效记录不被返回。
本次全量回归：239 通过、1 项既有失败（API 检查点回放测试，原因见下文）。

2026-10-12 验证结果：14 项记忆专项测试通过。四组固定检索样例（中文组件目录、
英文测试命令、框架名称、文件路径）首条命中 4/4；仓库隔离和失效记录过滤通过。
相关回归测试通过。全量运行曾得到 222 通过、1 失败；失败为已有 API 测试在
检查点后新增文件，却仍期望检查点有效。已在未包含本次改动的 HEAD 临时副本复现。
