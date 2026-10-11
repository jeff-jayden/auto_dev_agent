# 任务短期记忆与项目长期记忆

两种记忆仍通过现有上下文管理和 Token 预算进入模型，不会每轮注入全部历史。

## 短期：同一任务的工作记忆

原始记录保存在 SQLite `tasks.data`。`build_task_context` 从中构建 `working_memory`：
当前状态和错误、最近失败轮次、最新开发输出、未完成步骤、最近审查和 PR 信息。
历史失败标记为历史证据，不能覆盖后续成功记录；测试成功不能推断用户验收或发布成功。
`query_execution_memory` 按开发记录 offset 和 text_offset 分页读取完整输出。
已有 `query_task_history` 继续查询原始需求、反馈和 Diff。

## 长期：同一仓库跨任务使用

SQLite 新表 `repository_memories` 按 repository_id + key 隔离和去重。
支持 architecture、convention、test_command。保存来源、基线 SHA、更新时间和有效状态。
自动记录仓库分析观察，以及 success=true 且 exit_code=0 的实际执行命令；
不会把用户业务要求或模型自述自动提升成项目规范。
基线 SHA 不证明工作区没有未提交修改，所有历史记录均要求核对当前代码。

规划和开发上下文按相关性及类别选取最多四条，每条正文预览最多 600 UTF-8 字节。
开发探索工具 `query_project_memory` 可以分页读取原文，检索结果会传入后续开发上下文。
中文使用相邻双字匹配，英文使用词和路径匹配，加上仓库和类型过滤；这是确定性词法检索，
不提供同义词语义召回，不依赖向量库。更多长期记忆可显式登记，不扫描整段聊天生成事实。

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

2026-10-12 验证结果：14 项记忆专项测试通过。四组固定检索样例（中文组件目录、
英文测试命令、框架名称、文件路径）首条命中 4/4；仓库隔离和失效记录过滤通过。
相关回归测试通过。全量运行曾得到 222 通过、1 失败；失败为已有 API 测试在
检查点后新增文件，却仍期望检查点有效。已在未包含本次改动的 HEAD 临时副本复现。
