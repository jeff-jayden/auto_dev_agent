# AI Dev Agent

_一个可暂停、可恢复、可审计，并由人工 Gate 控制最终交付的软件研发 Agent 平台。_

---

## 📋 项目定位

AI Dev Agent 把一条真实的软件需求拆成可控的交付流程：读取仓库、分析需求、生成技术方案、隔离开发、执行测试、代码审查、创建 GitHub Pull Request，最终由用户决定是否合入。

它不是“让模型一次性生成代码”的 Demo，而是一套围绕 Agent 构建的研发工作台：**模型负责理解、决策和生成，LangGraph 负责流程编排，确定性工具负责执行，人工审批负责关键边界**。

| 项目属性 | 当前实现 |
| --- | --- |
| **后端** | Python 3.12+、FastAPI、Pydantic |
| **Agent 编排** | LangGraph、LangChain `create_agent`、`@tool` |
| **模型接入** | DeepSeek、Ollama、OpenAI-compatible API |
| **持久化** | SQLite、LangGraph SQLite Checkpointer |
| **代码执行** | Git Worktree、受控 Patch、仓库测试命令 |
| **代码交付** | GitHub Draft PR、评论同步、人工合入 |
| **前端** | 原生 HTML/CSS/JavaScript、Monaco Diff Editor |
| **质量保障** | 规则审查、模型审查、Golden Cases、混合 RAG 评测 |

> 📌 **核心原则：** Agent 可以自主探索和提出修改，但不能绕过批准范围、测试、审查、发布与合入 Gate。

## 🎯 核心能力

| 能力 | 解决的问题 | 主要实现 |
| --- | --- | --- |
| **仓库理解** | 避免模型凭空猜测文件和调用关系 | 仓库事实扫描、符号索引、依赖图、混合 RAG |
| **多 Agent 协作** | 将规划、开发和审查职责分离 | Plan、Repository Exploration、Developer、Reviewer Agent |
| **自主工具调用** | 按问题需要补充上下文 | `search_text`、`search_symbol`、`find_references`、`read_file`、`git_history` |
| **受控代码修改** | 防止越权修改和 Patch 污染 | 文件范围、ToolPolicy、结构校验、Git 基线恢复 |
| **测试与自动修复** | 让失败形成闭环 | Apply → Test → Diagnose → Repair，最多三轮 |
| **独立代码审查** | 防止“开发 Agent 自己证明自己正确” | 确定性规则与 LLM Review 双重检查 |
| **暂停与恢复** | 支持长任务中断、进程退出和人工暂停 | Job 租约、心跳、五类检查点、恢复诊断 |
| **可观测性** | 回答 Agent 做了什么、为何失败 | Trace、Span、模型/工具调用、Token、实时任务时间线 |
| **GitHub 交付** | 将本地修改变成可审核成果 | `agent/*` 分支、Draft PR、Review Comment、人工 Merge |
| **UI 验收** | 对页面需求补充可视化证据 | Figma MCP、浏览器截图、DOM 与视觉差异报告 |

当前属于**顺序协作型 Multi-Agent**：各 Agent 按交付阶段接力，Developer 与 Reviewer 内部允许有界循环，但不会无约束地并行修改同一工作区。

## 🔄 交付流程

一条需求从创建到合入会经过以下主链路。测试失败回到开发修复，审查发现阻塞问题也回到开发；发布 PR 和合入始终需要人工操作。

```mermaid
flowchart LR
    accTitle: AI Dev Agent 交付流程
    accDescr: 展示需求从仓库检索和技术方案开始，经过人工审批、开发测试与代码审查，最终发布并合入 GitHub Pull Request 的完整链路

    requirement([📋 创建需求]) --> context[🔍 检索仓库上下文]
    context --> plan[🧠 生成技术方案]
    plan --> approval{👤 方案批准?}
    approval -->|批准| develop[🔧 隔离开发]
    approval -->|退回| plan
    develop --> test{🧪 测试通过?}
    test -->|失败| develop
    test -->|通过| review{🔍 审查通过?}
    review -->|需修改| develop
    review -->|通过| release[👤 发布审批]
    release --> pull_request[📤 创建 Draft PR]
    pull_request --> merge([✅ 人工合入])

    classDef human fill:#fef9c3,stroke:#ca8a04,stroke-width:2px,color:#713f12
    classDef process fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    classDef success fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d

    class approval,release human
    class context,plan,develop,test,review,pull_request process
    class merge success
```

流程中的责任边界如下：

1. **需求阶段**：收集仓库事实，生成需求分析、验收标准和技术方案
2. **开发阶段**：创建隔离 Worktree，Developer Agent 探索上下文并按步骤修改、测试和修复
3. **CR 阶段**：展示累计 Diff、测试证据、MR 描述和 Review Finding
4. **交付阶段**：用户确认后创建 Draft PR，再由用户决定是否 Ready 和 Merge

## ⚙️ 系统架构

系统采用“Web/API + Redis/Celery + LangGraph 状态机 + Agent/Tool + Git 工作区”的分层设计。`TaskOrchestrator` 只负责命令入口和依赖适配，下一步执行哪个节点由 `TaskDeliveryGraph` 及其条件边决定。Celery 只负责分发和执行 Job，不替代 LangGraph 的业务状态与检查点。

```mermaid
flowchart TB
    accTitle: AI Dev Agent 系统架构
    accDescr: 展示 FastAPI 保存业务 Job 后经 Redis 分发到独立 Celery Worker，再由 LangGraph 按顺序调用 Agent 和 Git 工作区的流程

    web([👤 Web 工作台]) --> api[🌐 FastAPI]
    api --> database[(💾 SQLite 与 Checkpointer)]
    api --> redis[(📥 Redis Broker)]

    subgraph execution_layer ["⚙️ 后台执行层"]
        redis --> development_worker[Developer Worker]
        redis --> review_worker[Reviewer Worker]
        redis --> github_worker[GitHub Worker]
        beat[⏰ Celery Beat] --> redis
        beat --> database
    end

    development_worker --> workflow_graph[🔄 LangGraph 工作流]
    review_worker --> workflow_graph
    github_worker --> workflow_graph
    workflow_graph --> agents[🧠 Plan、Developer、Reviewer]
    workflow_graph --> database
    agents --> workspace[🔧 Git Worktree 与工具策略]
    agents --> integrations[🔌 LLM、GitHub、Figma]

    classDef interface fill:#f3f4f6,stroke:#6b7280,stroke-width:2px,color:#1f2937
    classDef engine fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    classDef data fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d

    class web,api interface
    class development_worker,review_worker,github_worker,beat,workflow_graph,agents engine
    class database,redis,workspace data
```

| 层 | 职责 | 核心代码 |
| --- | --- | --- |
| **交互层** | 三阶段工作台、实时进度、Diff、审查与发布操作 | `apps/web/` |
| **API 层** | 参数校验、HTTP/SSE 接口、依赖装配 | `apps/api/main.py` |
| **执行层** | Celery 路由、Job 领取、租约、心跳、取消和恢复 | `src/execution/celery_app.py`、`src/execution/worker.py` |
| **编排层** | 顶层交付图、状态迁移、人工中断、检查点 | `src/workflows/` |
| **Agent 层** | 规划、上下文探索、开发、审查和 MR 生成 | `src/agents/` |
| **工具层** | 文件读取、搜索、Patch、测试和策略约束 | `src/tools/` |
| **基础设施层** | SQLite、GitHub、Worktree、模型与 Trace | `src/infrastructure/`、`src/scm/`、`src/sandbox/`、`src/llm/` |

## 👥 Agents 与上下文

### Agent 分工

| Agent | 输入 | 自主行为 | 输出 |
| --- | --- | --- | --- |
| **RequirementPlanningAgent** | 需求、仓库事实、设计上下文 | 调用只读规划工具，确定影响文件和开发步骤 | 需求分析、技术方案、验收与风险 |
| **RepositoryExplorationAgent** | 初始上下文、写入范围、失败或审查反馈 | 在最多 1～8 步内自主选择只读工具 | 补充后的代码上下文与工具审计 |
| **CodeDevelopmentAgent** | 已批准方案、当前工作区、上下文 | 生成修改、应用 Patch、运行测试、诊断并修复 | Development Attempts、Diff、测试结果 |
| **CodeReviewAgent** | 需求、方案、累计 Diff、测试证据 | 执行规则审查和模型审查，选择基线恢复项 | Review Round、Finding、是否阻塞 |

### 上下文工程

同一任务采用持久化的提取式摘要、最近对话和按需历史查询。每次保存任务都会刷新
`metadata.task_summary`：用户要求保留来源轮次，实际修改记录单独保存状态、文件和测试结果；
测试通过不推断为用户验收。摘要不会改写或删除原始反馈，撤销状态会重新计算。
默认仅发送最近 3 轮完整反馈，较早反馈发送短预览；明确的保留/禁止等约束单独保留原文。
只读探索工具 `query_task_history` 可以按轮次、关键词或文件查询原始反馈、Agent 结果和 Diff，
通过 `offset` 和 `text_offset` 分页读取；查询证据也会传给随后开发模型。

所有 HTTP 模型调用（含 JSON 修复重试）及 LangChain 模型调用都在发送前检查预算，
计入系统提示、Schema、工具返回和代码，并预留输出空间。目前使用 UTF-8 字节数作为保守
Token 上界估算，而非供应商的精确分词计数。超限时优先去掉可选的重复证据/代码预览；
关键需求、当前代码和审查 Diff 不静默截断，仍超限则停止调用并提示缩小范围或调整预算。
审查因预算未完成时阻止发布，不把缺少审查结果当作通过。

可在 `.env` 配置以下值（修改后重启 API 和 Worker）：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `TASK_CONTEXT_RECENT_ROUNDS` | `3` | 默认传入的完整最近轮次，范围 1～8 |
| `TASK_CONTEXT_MAX_TOKENS` | `8000` | 历史摘要与最近反馈的保守估算预算 |
| `MODEL_CONTEXT_WINDOW_TOKENS` | `65536` | 配置模型实际支持的上下文窗口 |
| `MODEL_OUTPUT_RESERVE_TOKENS` | `8192` | 输出预留，同时限制模型最大输出 |
| `MODEL_CONTEXT_SAFETY_TOKENS` | `1024` | 协议和估算安全余量 |
| `MODEL_MAX_INPUT_TOKENS` | `32768` | 整个请求输入上限 |

有效输入预算取输入上限与“窗口减输出预留、安全余量”的较小值。完整历史仍保存在数据库中，
被省略的摘要轮次会明确标记，模型应回查原始要求；摘要不自动推断旧要求已被新要求覆盖。

系统没有把整个仓库直接塞给模型，而是按阶段组织上下文：

| 上下文层 | 内容 | 使用场景 |
| --- | --- | --- |
| **仓库事实** | 语言、框架、Git HEAD、入口、测试命令 | 创建任务和生成方案 |
| **混合 RAG** | 关键词/符号召回、代码块 Embedding、Chroma、RRF 融合、依赖扩展 | 筛选核心、依赖和测试文件，返回带行号的代码证据 |
| **自主探索** | 文本、符号、引用、文件和 Git 历史 | Developer 判断初始上下文不足时 |
| **修复上下文** | 上次失败、测试输出、当前 Diff、Review Finding | 测试修复、CR 修复和用户继续对话 |
| **Git 基线** | 原始代码片段与删除候选 | 恢复需求未授权删除，避免模型重新编造旧代码 |

支持两种模式：`legacy` 保留原来的确定性哈希检索用于基线对比；`embedding` 使用真实模型向量。Python 按 AST 符号分段，JS/TS/CSS 按声明边界和有界行窗口分段（不是完整语法解析），每块最多 2400 字符。查询时融合关键词/符号和代码块向量排名，扩展依赖及测试文件；Plan 的代码证据工具可读取命中的片段，Developer 继续按候选文件读取当前完整代码并自主探索，不用片段直接替换整文件。

#### 启用真实 RAG

```powershell
python -m pip install -e ".[rag]"
# 单独终端启动 Chroma（API/Celery 共享一个向量服务）
.\scripts\start-chroma.ps1
# 另一个终端启动 Ollama
ollama serve
# 在另一个终端下载模型（首次约 621 MB）
ollama pull embeddinggemma
```

在本地 `.env` 中设置（API 和 Celery Worker 都需重启加载）：

```dotenv
RAG_RETRIEVAL_MODE=embedding
RAG_VECTOR_ROOT=runtime/vectors
RAG_CHROMA_URL=http://127.0.0.1:8001
EMBEDDING_PROVIDER=ollama
EMBEDDING_BASE_URL=http://127.0.0.1:11434
EMBEDDING_MODEL=embeddinggemma
EMBEDDING_TIMEOUT_SECONDS=60
```

也支持 `EMBEDDING_PROVIDER=openai` 的兼容 Embedding API，地址填写含 `/v1` 的根地址，密钥只设置到本地 `EMBEDDING_API_KEY`。与 DeepSeek 对话配置相互独立，不会自动把对话模型当作 Embedding 模型。接外部服务会发送代码块，应先确认仓库隐私要求。服务异常会明确报错，不静默退回旧检索。

Chroma 在本机 `127.0.0.1:8001` **独立服务进程**运行，由 `scripts/start-chroma.ps1` 启动，数据存储在 `runtime/vectors`；API/Celery 通过 HTTP 访问，不并发直接写持久化目录。Redis 仍仅用于 Celery，与向量库无关。单进程 CLI/单元测试可不配 `RAG_CHROMA_URL` 使用内嵌模式；Celery 模式会强制检查该配置。需要 Docker 时可独立部署 Chroma 并替换服务地址，不必改 Agent 链路。

按仓库、模型配置和内容快照隔离 collection；未提交文件修改、删除、新增也会更新索引。内容向量缓存复用未变化的块。历史快照保留用于并行和恢复任务，尚未自动清理；长期运行需安排保留策略。更换同名模型的权重时也应使用新的模型版本名或向量目录。

#### 真实仓库 A/B 评测

在已设置上述环境变量的终端运行（命令行入口不会自行读取 `.env`）：

```powershell
python -m evaluation.retrieval --repository . --dataset tests/fixtures/rag_repository_cases.json --k 3 --output runtime/evaluations/rag-ab.json
```

同一份工作区、同一标注集对比 **原哈希混合检索 → Embedding + Chroma 混合检索**，报告包含文件级 Recall@K、MRR、Hit@K、无关文件率、平均上下文文件数、查询耗时、命中代码块行号、模型标识、HEAD 和内容指纹。首次建库/预热耗时单独记录。示例提供本项目 8 个文件级标注，属于小样本 smoke benchmark；应补充真实需求、人工校正多文件标签和独立测试集，不能把此结果直接当作开发成功率。

内置的 `src/evaluation/datasets/cfe_unit_commit_rag_cases.json` 包含 `cfe-unit` 最终完整代码快照上的 48 条历史需求文件召回标签。标签来自原始 R02–R49 功能提交并经过文件职责校正；R01、R50 仅修改文档，因此不纳入代码召回。可以直接从 RAG 评测页导入，也可以通过命令行运行：

```powershell
python -m evaluation.retrieval --repository E:\aiTraval\playground\cfe-unit `
  --dataset src/evaluation/datasets/cfe_unit_commit_rag_cases.json --k 3 `
  --output runtime/evaluations/cfe-unit-rag-ab.json
```

启用 `embedding` 后页面“运行 Golden + RAG 评测”也使用真实仓库集，标明模型和数据来源；完整报告保存在 `runtime/evaluations`。可通过 `RAG_EVALUATION_REPOSITORY` / `RAG_EVALUATION_DATASET` 选择其他仓库及相应人工标注集。`legacy` 模式仍跑原 6 条模拟契约用例，页面会明确区分。真实评测首次建库较慢，当前同步入口适合本机小样本。

## 💾 状态、任务与恢复

系统把“业务走到哪一步”和“后台由谁执行”分开记录：

| 对象 | 表达的含义 | 状态来源 |
| --- | --- | --- |
| **LangGraph State** | 任务当前业务阶段、恢复位置、待人工动作 | SQLite Checkpointer，业务状态源 |
| **Task** | API 和页面读取的任务投影 | Graph 状态迁移后同步 |
| **ExecutionJob** | 某次批准、重试、恢复或审查由哪个 Worker 执行 | SQLite 业务记录；Redis 只传递 Job ID |
| **TaskCheckpoint** | 可展示、回放和恢复的安全节点 | Graph 检查点投影 |

```mermaid
stateDiagram-v2
    accTitle: 任务业务状态生命周期
    accDescr: 展示任务从需求分析到开发测试、代码审查、发布审批和最终合入的状态变化，以及失败后的修复循环

    [*] --> RequirementAnalysis: 📋 创建任务
    RequirementAnalysis --> WaitingPlanApproval: 🧠 方案已生成
    WaitingPlanApproval --> Developing: ✅ 人工批准
    Developing --> Testing: 🔧 修改已应用
    Testing --> Developing: ❌ 测试失败
    Testing --> Reviewing: ✅ 测试通过
    Reviewing --> ChangesRequested: ❌ 发现阻塞问题
    ChangesRequested --> Developing: 🔄 自动或人工修复
    Reviewing --> WaitingReleaseApproval: ✅ 审查通过
    WaitingReleaseApproval --> WaitingMergeApproval: 📤 发布 PR
    WaitingMergeApproval --> Merged: ✅ 人工合入
    Merged --> [*]: 🏁 完成
```

开发链路持久化五类安全检查点：

| 检查点 | 已完成内容 | 恢复后继续执行 |
| --- | --- | --- |
| `plan_approved` | 技术方案已批准 | 创建或校验隔离工作区 |
| `proposal_ready` | 本轮修改方案已生成 | 应用修改 |
| `patch_applied` | 修改已写入 Worktree | 运行测试 |
| `test_result_saved` | 测试结果已保存 | 修复失败或生成 MR |
| `review_round_saved` | 审查轮次已保存 | 修复 Finding 或等待人工决策 |

Worker 默认每 10 秒更新一次 Job 心跳，并持有 90 秒执行租约。进程退出后心跳停止，Celery Beat 每 30 秒扫描租约过期的 Job，并根据有效检查点重新投递；继续执行前还会校验方案、仓库 HEAD、Worktree 和 Diff 是否漂移。Celery 自身的 Worker 心跳只代表进程在线，不能替代业务 Job 心跳。

## 🚀 快速开始

### 前置条件

| 依赖 | 要求 | 用途 |
| --- | --- | --- |
| Python | 3.12+ | API、Agent 和 Worker |
| WSL Docker | 可运行 Compose | 启动 Redis Broker |
| Git | 可执行命令行 | 仓库分析、Worktree、Diff 和提交 |
| Chrome 或 Edge | 可选 | UI 截图验收 |
| Ollama 或模型 API Key | 开发阶段必需 | 规划、开发和审查模型调用 |
| GitHub Token | 可选 | 发布和合入 Pull Request |

### 安装

```powershell
git clone https://github.com/jeff-jayden/auto_dev_agent.git
cd auto_dev_agent

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

Copy-Item .env.example .env
```

编辑 `.env`，至少配置可用模型。以 DeepSeek 为例：

```dotenv
MODEL_PROVIDER=deepseek
MODEL_NAME=deepseek-flash
MODEL_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=your_deepseek_api_key
MODEL_TIMEOUT_SECONDS=180
AGENT_ALLOWED_REPOSITORY_ROOTS=E:\projects
```

> ⚠️ **安全提示：** `.env` 已被 Git 忽略。不要把真实 API Key 或 GitHub Token 写入 README、源码、Issue 或提交记录。

### 启动

当前本地部署有意将 Redis 与代码执行进程分开：

| 组件 | 部署位置 | 原因 |
| --- | --- | --- |
| Redis | WSL Docker 容器 `ai-dev-agent-redis` | 提供可靠消息队列，数据写入 Docker Named Volume |
| FastAPI | Windows Python 进程 | 提供页面和 API，并访问 Windows 本地路径 |
| Celery Worker | Windows Python 进程 | 直接访问 `E:\...` 仓库和 Git Worktree |
| Celery Beat | Windows Python 进程 | 扫描过期租约和轮询 GitHub 指令 |

先在 WSL Docker 中启动 Redis：

```powershell
.\scripts\start-redis.ps1
```

然后分别打开三个 PowerShell 终端：

```powershell
# 终端 1：执行 development、review、github 和 maintenance 队列
.\scripts\start-celery-worker.ps1

# 终端 2：定时恢复和 GitHub 评论轮询
.\scripts\start-celery-beat.ps1

# 终端 3：Web 与 API
.\scripts\start-local.ps1
```

Redis 配置位于 `compose.yaml`，宿主机连接地址为 `127.0.0.1:6379`。持久化数据位于 Docker 管理的 `redis-data` Named Volume，而不是项目的 `runtime/`。如果只需运行不依赖 Redis 的测试，可临时设置 `EXECUTION_BACKEND=local` 使用原有单 Worker 回退模式。

打开 <http://127.0.0.1:8765>，或检查健康状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

首次使用时：

1. 在“需求”页面通过 GitHub 地址或本地目录注册仓库
2. 新建任务并填写标题、需求描述和目标仓库
3. 核对仓库事实、需求分析、影响文件和技术方案
4. 批准方案后，在“开发”页面观察实时进度、Agent 对话和累计 Diff
5. 在“代码 CR”页面处理 Finding，确认后发布 Draft PR

如果使用本地 Ollama，可直接运行 `scripts/start-ollama.ps1`；该脚本默认连接 `http://127.0.0.1:11434/v1`。

## 🔧 配置

| 环境变量 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `MODEL_PROVIDER` | 是 | `disabled` | `deepseek`、`ollama`、`openai` 或 `openai_compatible` |
| `MODEL_NAME` | 是 | 按 Provider | 模型名称 |
| `MODEL_BASE_URL` | 否 | 按 Provider | OpenAI-compatible API 地址 |
| `MODEL_API_KEY` | 按 Provider | 空 | 通用模型 API Key |
| `DEEPSEEK_API_KEY` | DeepSeek | 回退到 `MODEL_API_KEY` | DeepSeek API Key |
| `MODEL_TIMEOUT_SECONDS` | 否 | `90`～`240` | 单次模型调用超时 |
| `AGENT_ALLOWED_REPOSITORY_ROOTS` | 本地仓库 | 项目父目录 | 允许注册的本地仓库根目录，Windows 使用分号分隔 |
| `GITHUB_TOKEN` | 发布 PR | 空 | 建议使用只授权目标仓库的 Fine-grained PAT |
| `GITHUB_API_URL` | 否 | `https://api.github.com` | GitHub API 地址 |
| `FIGMA_MCP_URL` | UI 验收 | 空 | Figma Desktop MCP 地址 |
| `DEVELOPER_CONTEXT_TOOLS_ENABLED` | 否 | `true` | 是否启用 Developer 自主上下文探索 |
| `DEVELOPER_CONTEXT_MAX_STEPS` | 否 | `4` | 单轮只读探索步数，限制为 1～8 |
| `EXECUTION_BACKEND` | 否 | `local` | `celery` 使用 Redis 分发；`local` 使用单 Worker 兼容模式 |
| `CELERY_BROKER_URL` | Celery | `redis://127.0.0.1:6379/0` | Job 消息代理地址 |
| `CELERY_RESULT_BACKEND` | 否 | `redis://127.0.0.1:6379/1` | Celery 基础设施任务结果；不是业务状态源 |

运行数据写入 `runtime/`：

- `runtime/agent.db`：Task、Job、Event、Trace、Evaluation 与 LangGraph Checkpoint
- `runtime/repositories/`：通过远端地址注册的仓库
- `runtime/tasks/{task_id}/repo`：任务隔离 Worktree
- `runtime/indexes/`：按仓库和 Git HEAD 缓存的代码索引
- `runtime/ui-acceptance/`：UI 验收截图与差异证据

这些内容均属于本地运行状态，不应提交到 Git。

## 📚 项目结构

```text
ai-dev-agent/
├── apps/
│   ├── api/main.py                 # FastAPI、路由、依赖装配与 Worker 生命周期
│   └── web/                        # 三阶段工作台和 Monaco Diff 界面
├── src/
│   ├── agents/                     # 规划、探索、开发、审查与 MR Agent
│   ├── code_intelligence/          # Git-aware 索引、混合 RAG 和依赖扩展
│   ├── domain/                     # Task、Plan、Job、Review、Trace 等领域模型
│   ├── evaluation/                 # Golden Cases 与检索指标评测
│   ├── execution/                  # 后台 Worker、租约、心跳和恢复执行
│   │   └── celery_app.py           # Redis Broker、队列路由、Celery Task 与 Beat 调度
│   ├── infrastructure/             # SQLite 持久化
│   ├── llm/                        # 多 Provider 模型网关
│   ├── observability/              # Trace、Span 和脱敏
│   ├── repository/                 # 仓库注册、克隆和事实分析
│   ├── sandbox/                    # Git Worktree 隔离工作区
│   ├── scm/                        # GitHub API、分支、PR 和合入
│   ├── tools/                      # Developer 工具与 ToolPolicy
│   ├── workflows/                  # 顶层 LangGraph、Orchestrator 和恢复协调器
│   └── ui_validation.py            # Figma MCP 与浏览器 UI 验收
├── scripts/                        # 本地和 Ollama 启动脚本
├── compose.yaml                    # WSL Docker Redis 服务与持久化卷
├── tests/                          # 单元与集成测试
├── .env.example                    # 无密钥的配置模板
├── pyproject.toml                  # 包信息与依赖
└── TODO.md                         # 暂缓功能与后续计划
```

关键入口：

- `apps/api/main.py`：启动服务并构造所有依赖
- `src/workflows/workflow_graph.py`：唯一业务状态源与顶层交付图
- `src/workflows/orchestrator.py`：把 API/Worker 命令适配为 Graph 输入
- `src/execution/worker.py`：异步执行 Job 并维护心跳租约
- `src/execution/celery_app.py`：将 Job 分发到 Celery 队列并执行定时恢复
- `src/agents/code_development_agent.py`：开发、测试和修复子图
- `src/agents/code_review_agent.py`：审查和修复子图

## ✅ 测试、API 与当前边界

### 运行测试

```powershell
python -m pytest -q
```

当前测试结果为 **107 passed**；测试集覆盖任务 API、仓库分析、上下文探索、分步开发、工具策略、后台 Job、恢复、GitHub 交付、可观测性、UI 验收和 LangGraph 工作流。

### 主要 API

启动后可访问 <http://127.0.0.1:8765/docs> 查看完整 OpenAPI 文档。

| API 组 | 代表接口 | 用途 |
| --- | --- | --- |
| **仓库** | `POST /api/repositories` | 注册本地目录或远端 Git 仓库 |
| **任务** | `POST /api/tasks` | 创建需求并生成方案 |
| **开发** | `POST /api/tasks/{id}/approve` | 批准方案并创建后台 Job |
| **反馈** | `POST /api/tasks/{id}/feedback` | 基于当前工作区继续对话和修改 |
| **恢复** | `GET /api/tasks/{id}/recovery` | 诊断中断任务并恢复 |
| **审查** | `POST /api/tasks/{id}/review/run` | 刷新工作区快照并重新审查 |
| **交付** | `POST /api/tasks/{id}/pull-request/publish` | 创建 GitHub Draft PR |
| **可观测性** | `GET /api/traces/{trace_id}` | 查看 Agent、LLM 和工具调用链 |
| **实时状态** | `GET /api/tasks/{id}/stream` | 通过 SSE 订阅任务事件 |
| **评测** | `POST /api/evaluations/run` | 运行 Golden Cases 与 RAG 对比评测 |

### 真实需求的 RAG 标注与评测闭环

左侧 **更多 → RAG 评测** 可独立使用，不必先选择任务。

1. 若要直接验证文件召回，点击 **导入 cfe-unit 48 条测试集**。页面会自动匹配包含固定快照的已注册仓库，把样本设为“已确认 / 测试集”；`src/reportWebVitals.js` 与 `src/setupTests.js` 不属于任何核心答案，且不在固定快照中。也可以选择仓库，提取“已合入 PR”或“所有历史需求”的候选文件。重复导入不会覆盖已有标注。
2. 逐条确认需求，把候选标成 **必要 / 辅助 / 无关**；可从开发前基线补充漏掉的文件。保存草稿不参与评测，必须人工确认且至少有一个必要文件。新建文件在开发前不存在，不能作为召回答案。
3. 将样本分为开发集（调参）与测试集（保留验收），点击“评测已确认样本”。同仓库完全相同的需求、带 `followup_of` 关联的后续任务不允许跨组重复；近似需求及未关联的重复任务仍需人工放在同组，避免泄漏。
4. 对同一需求、同一开发前 Git 快照、同一 Top K，对比字段化 BM25F + 本地语义基线与 Embedding + Chroma 检索，展示 Recall@K、必要文件 Recall@K、Precision@K、MRR、Hit@K、查询耗时和逐条召回。

基线从任务保存的 `repository_analysis.head_sha` 获取，不以当前 HEAD 或合入后 PR 的 base.sha 代替。评测通过 `git archive` 导出临时快照，不 checkout/reset 工作仓库；缺少历史基线的样本会明确报错。远端 PR 文件只是候选，不能自动当标准答案。GitHub 未配置时会提示仅有本地历史来源。

标签及版本保存在 `runtime/agent.db` 的 `rag_cases`，评测报告及本次标签快照保存在 `rag_dataset_runs`。未确认/排除样本不计分，运行失败的样本会显示原因及实际覆盖数，不伪造为 0 分。指标为逐需求宏平均；Precision@K 使用固定 K 为分母，必要文件 Recall 只看必要标签，其余相关性指标使用必要+辅助标签。小样本及不完整标注不能代表生产准确率。

接口：`GET /api/rag/dataset`、`POST /api/rag/dataset/import`、`POST /api/rag/dataset/import-builtin`、`PUT /api/rag/dataset/cases/{id}`、`GET /api/rag/dataset/cases/{id}/files`、`POST /api/rag/dataset/evaluate`。第一版评测为同步接口，运行期间页面显示执行中；需要 Embedding/Chroma 服务可用。

### 当前边界（部署与集成）

- Redis/Celery 已将任务分发与 FastAPI 进程解耦；本地脚本使用 Windows `solo` Worker，启动多个 Worker 进程可以并行处理不同需求
- SQLite 仍承载业务数据和 Checkpointer；当前适合单机少量 Worker，高并发或跨机器部署前应迁移到 PostgreSQL
- 真实 RAG 已支持独立 Embedding 与 Chroma；默认示例模式仍为 legacy，生产效果需更大规模独立人工标注集验证
- Figma MCP 依赖 Figma Desktop Dev Mode；未配置时不影响普通代码需求
- 系统生成 Draft PR，但最终发布和合入仍由用户确认
- 仓库当前未声明开源许可证，复制、分发或商用前应先补充许可证

### 参考资料

- [LangGraph 文档](https://docs.langchain.com/oss/python/langgraph/overview)
- [LangChain Agents 文档](https://docs.langchain.com/oss/python/langchain/agents)
- [FastAPI 文档](https://fastapi.tiangolo.com/)
- [GitHub Pull Requests REST API](https://docs.github.com/en/rest/pulls/pulls)
- [Monaco Editor 文档](https://microsoft.github.io/monaco-editor/)

---

_当前版本：`0.1.0` · 最后更新：2026-09-29_
