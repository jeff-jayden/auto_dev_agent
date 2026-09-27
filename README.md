# AI Dev Agent — 第七阶段

这是一个可运行的研发 Agent 纵向切片，当前支持：

1. 注册允许目录内的本地 Git 仓库
2. 使用只读工具识别语言、框架、分支、基线 SHA、入口和测试命令
3. 生成带文件和行号证据的需求分析与技术方案
4. 对方案执行批准、拒绝或要求修改
5. 真实仓库审批后保持只读并进入 `plan_approved`
6. 内置 Demo 审批后在隔离目录修改代码、执行测试并生成 Diff/MR 描述
7. 真实仓库审批后从规划基线创建 Git Worktree，不修改原始工作目录
8. Developer Agent 只能修改技术方案内文件，现有文件优先使用精确搜索/替换，由系统生成 Git Diff；新文件兼容 unified diff
9. 只执行仓库分析阶段批准的测试命令，失败后最多自动修复三次
10. 部署、CI 和数据库迁移等高风险路径会暂停等待人工审批
11. 测试通过后自动生成包含验收清单、风险、回滚方案和 Change Log 的本地 MR 草稿
12. 独立 Reviewer Agent 结合确定性规则和模型审查生成带文件、行号和严重度的 CR
13. high / critical 问题会阻塞流程并触发最多三轮自动修复、测试和重新审查
14. CR 通过后进入 waiting_release_approval，当前阶段不会直接发布线上
15. 方案审批以持久化 Job 入队，API 使用 202 立即返回，不再占用请求直到开发完成
16. 单 Worker 后台执行开发链路，Job 状态、重试次数、错误和结果状态写入 SQLite
17. 相同任务的重复审批会返回同一个活动 Job，防止重复执行
18. 服务重启时恢复未完成 Job；已请求取消的 Job 会保持取消，不会被错误重跑
19. SSE 实时推送任务审计事件，前端同时轮询 Job 状态并展示排队、执行、失败或完成状态
20. 排队 Job 可立即取消；运行中 Job 使用协作式暂停，在下一个安全检查点进入 paused
21. 已取消或暂停 Job 可以创建关联的 resume Job，并校验方案、基线、Worktree 与 Diff 后继续
22. 持久化五类恢复检查点：plan_approved、proposal_ready、patch_applied、test_result_saved、review_round_saved
23. 自动识别 GitHub origin，在人工 Gate 后只向 agent/* 分支提交和推送
24. 使用 GitHub API 创建 Draft Pull Request，并同步本地 Code Review 摘要
25. 用户再次确认后将 Draft 转为 Ready，并使用已审核 head SHA 执行 squash merge
26. GitHub PR 创建、评论和合入均保留任务事件，失败时回到可重试的人工作业状态
27. 每次规划、后台执行、检查点 Dry Run 和 Golden Case 评测都会生成独立 Trace
28. Trace 内使用父子 Span 展示 Agent、LLM 和工具调用，记录耗时、状态、模型与 Token 估算
29. Trace 属性进入 SQLite 前会递归脱敏 token、secret、password、authorization 等字段
30. 任意持久化检查点可做只读 Dry Run，验证方案哈希、基线、Worktree 和 Diff，不修改代码
31. 内置 5 个无模型、无写仓库的 Golden Cases，并在页面展示成功率、耗时和调用量指标
32. 文本替换在写盘前校验完整代码块边界，拒绝“局部块头替换为完整块”造成的尾部重复
33. CSS 修改执行结构校验；React 测试通过后自动执行仓库已有的 build 脚本
34. CR 会阻塞 CSS 结构错误、入口绕过 App、缺少 React 行为测试和 medium 级核心验收缺失
35. Git Diff 使用按文件折叠、双行号、hunk 和增删着色的 GitHub 风格视图
36. 任务恢复协调器会核对 Task、活动 Job、最近检查点、仓库基线、Worktree 和 Diff
37. 执行状态异常但检查点有效时，任务顶部展示可解释的恢复卡和诊断抽屉
38. 用户确认后创建幂等 resume Job，从最近安全检查点继续，不重复已完成步骤
39. 方案、基线、工作区或 Diff 漂移时禁止自动恢复，并明确展示阻塞原因
40. Worker 领取 Job 时写入唯一 worker_id、当前阶段、心跳时间和 90 秒执行租约
41. 长时间模型与测试调用期间由独立心跳线程续租，服务重启只回收租约已过期的 Job
42. 恢复中心区分正常运行与 Worker 失联，并展示当前步骤、最后心跳和租约诊断
43. 用户确认恢复时原子地废弃旧 Job 并创建唯一 resume Job，旧 Worker 后续结果会被隔离
44. 新建任务可绑定 Figma Frame、实现预览 URL 与验收视口，并通过 Figma MCP 固定设计上下文、变量和参考图
45. 开发完成后使用本机 Chrome/Edge 渲染实现页面，保存 DOM、实现截图、视觉差异图和相似度
46. CR 页面展示结构、浏览器和视觉验收证据；浏览器渲染失败会阻塞 PR，像素偏差仅提示
47. Developer 在生成修改前运行有界的只读工具循环，可自主选择文本/符号/引用搜索、分段读文件和 Git 历史
48. 上下文探索最多执行 4 步且禁止写文件、运行 Shell 或扩大审批范围；写入、测试与发布仍由确定性流程控制

对于 React/CRA 项目，分析器会优先选择 `App`、对应测试和样式文件，使用非交互测试参数，并在隔离 Worktree 中复用原仓库已有的 `node_modules`，避免测试进入 watch 模式或因依赖目录未复制而失败。

默认使用确定性的本地规划器，因此没有模型 Key 也能完成仓库分析和内置 Demo。真实仓库开发必须配置 Ollama 或 OpenAI-compatible 模型；没有模型时任务会明确失败，不会伪造代码结果。模型输出如果无效、引用方案外文件或请求未批准命令，也会被执行层拒绝。

Developer 的只读上下文工具循环默认对内置模型网关启用，可通过 `DEVELOPER_CONTEXT_TOOLS_ENABLED=false` 关闭，或使用 `DEVELOPER_CONTEXT_MAX_STEPS=1..8` 调整最大探索步数。工具使用 LangChain `StructuredTool` 与 Pydantic 输入 Schema 统一注册和调用；项目自己的 Orchestrator 继续负责 Git、检查点、ToolPolicy、测试与发布，避免框架接管既有交付边界。

## 启动

```powershell
cd E:\aiTraval\ai-dev-agent
$env:PYTHONPATH="src"
python -m uvicorn apps.api.main:app --reload
```

打开 <http://127.0.0.1:8000>。页面已经预填了示例需求，创建任务后先检查需求分析和技术方案，再点击“批准并执行开发”。

默认只允许注册 `E:\aiTraval` 下的仓库。可通过环境变量追加允许根目录：

```powershell
$env:AGENT_ALLOWED_REPOSITORY_ROOTS="D:\projects;E:\work"
```

可选的 Ollama 配置：

```powershell
$env:MODEL_PROVIDER="ollama"
$env:MODEL_NAME="qwen3:4b"
$env:MODEL_BASE_URL="http://127.0.0.1:11434/v1"
$env:MODEL_TIMEOUT_SECONDS="240"
```

本机已安装 Ollama 时，也可以直接使用启动脚本：

```powershell
.\scripts\start-ollama.ps1
```

需要从项目根目录的本地 `.env` 同时加载模型和 GitHub 配置时，使用：

```powershell
.\scripts\start-local.ps1
```

`.env` 已被 Git 忽略，真实密钥只保存在本机，不要复制到源码或提交记录中。

可选的 Figma Desktop MCP 配置：

```powershell
$env:FIGMA_MCP_URL="http://127.0.0.1:3845/mcp"
```

在 Figma 桌面端 Dev Mode 中启用 MCP 后，创建任务时粘贴具体 Frame 链接（必须包含 `node-id`）。如果同时填写本地预览 URL，开发完成后会自动使用 Chrome/Edge 执行截图验收；未填写时只保存设计基线，不会伪造浏览器验收结果。

GitHub API 配置：

```powershell
$env:GITHUB_TOKEN="github_pat_..."
$env:GITHUB_API_URL="https://api.github.com"
```

Token 建议使用只授权目标仓库的 Fine-grained PAT。HTTPS Git Push 通过子进程临时环境注入认证 Header；Token 不会被拼接到 Git 命令、写入 Git 配置或保存到任务数据库。
启动脚本在未设置 `GITHUB_TOKEN` 时会使用掩码输入读取 Token，只保存在当前服务进程内存中。

## 测试

```powershell
cd E:\aiTraval\ai-dev-agent
$env:PYTHONPATH="src"
python -m unittest discover -s tests -v
```

## API

第五阶段新增异步执行接口：

- `POST /api/tasks/{task_id}/approve`：将审批执行加入队列，返回 HTTP 202 和 ExecutionJob
- `GET /api/jobs/{job_id}`：查询后台 Job 状态
- `POST /api/jobs/{job_id}/cancel`：取消排队 Job 或请求取消运行中 Job
- `POST /api/jobs/{job_id}/resume`：从被取消任务最近的安全阶段继续执行
- `GET /api/tasks/{task_id}/jobs`：读取任务的执行记录
- `GET /api/tasks/{task_id}/checkpoints`：读取任务的持久化检查点
- `GET /api/tasks/{task_id}/recovery`：诊断任务是否中断以及能否安全恢复
- `POST /api/tasks/{task_id}/recovery/resume`：人工确认后从最近安全检查点继续
- `POST /api/tasks/{task_id}/ui-acceptance`：重新执行 Figma 与浏览器 UI 验收
- `GET /api/tasks/{task_id}/ui-acceptance/image/{kind}`：读取 figma、implementation 或 diff 验收图片
- `GET /api/tasks/{task_id}/stream`：通过 SSE 订阅任务事件

第六阶段新增 GitHub 接口：

- `POST /api/tasks/{task_id}/pull-request/publish`：提交 agent/* 分支并创建 Draft PR
- `GET /api/tasks/{task_id}/pull-request`：读取已保存的 GitHub PR

第七阶段新增可观测性与回放接口：

- `GET /api/tasks/{task_id}/traces`：列出任务的规划、执行和回放 Trace
- `GET /api/traces/{trace_id}`：读取 Trace 与父子 Span 调用链
- `GET /api/metrics`：读取成功率、耗时、模型/工具调用和 Token 指标
- `POST /api/tasks/{task_id}/checkpoints/{checkpoint_id}/replay`：只读校验检查点能否安全恢复
- `POST /api/evaluations/run`：运行内置 Golden Cases
- `GET /api/evaluations/latest`：读取最近一次评测结果
- `POST /api/tasks/{task_id}/pull-request/refresh`：同步远端 PR 状态
- `POST /api/tasks/{task_id}/pull-request/merge`：人工确认后将 Draft 转为 Ready 并合入

MR 与审查接口：

- GET /api/tasks/{task_id}/merge-request：读取结构化 MR 草稿
- GET /api/tasks/{task_id}/reviews：读取逐轮 Code Review
- POST /api/tasks/{task_id}/review/run：重新执行 Code Review
- POST /api/tasks/{task_id}/review/approve：人工批准剩余 CR 风险
- POST /api/tasks/{task_id}/release/reject：拒绝进入发布阶段

- `POST /api/tasks`：创建任务并生成方案
- `GET /api/tasks`：任务列表
- `GET /api/tasks/{task_id}`：任务详情
- `POST /api/tasks/{task_id}/reject`：拒绝方案
- `POST /api/tasks/{task_id}/revise`：根据人工意见重生成方案
- `POST /api/tasks/{task_id}/risk/approve`：批准高风险 Patch
- `POST /api/tasks/{task_id}/risk/reject`：拒绝高风险 Patch
- `GET /api/tasks/{task_id}/events`：审计事件
- `GET /api/repositories`：仓库列表
- `POST /api/repositories`：注册只读本地 Git 仓库
- `GET /api/repositories/{id}/analysis`：执行只读仓库分析
- `GET /health`：健康检查

生成的工作区位于 `runtime/tasks/{task_id}/repo`，不会修改 `examples/demo_repository` 模板。
