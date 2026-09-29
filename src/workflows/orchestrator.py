"""需求交付流程编排器。

连接 Agent、仓库、工作区、检查点、GitHub 和 UI 验收服务，实现从需求创建到 PR 合入的业务动作。
"""

from __future__ import annotations

import hashlib
import difflib
import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agents import (
    CodeReviewAgent,
    CodeDevelopmentAgent,
    RequirementPlanningAgent,
    MergeRequestBuilder,
)
from domain.models import (
    Approval,
    DevelopmentProposal,
    ExecutionResult,
    StepExecution,
    Task,
    TaskCheckpoint,
    TaskEvent,
    TaskStatus,
    ReplayResult,
)
from infrastructure.store import SQLiteTaskStore
from repository import RepositoryAnalyzer
from sandbox import WorkspaceManager
from scm import GitHubDeliveryService
from ui_validation import FigmaMCPClient, UIAcceptanceService
from .workflow_graph import TaskDeliveryGraph, TaskDeliveryState, WorkflowStateStore


class TaskOrchestrator:
    def __init__(
        self,
        store: SQLiteTaskStore,
        requirement_planning_agent: RequirementPlanningAgent,
        workspace_manager: WorkspaceManager,
        repository_analyzer: RepositoryAnalyzer,
        code_development_agent: CodeDevelopmentAgent,
        merge_request_builder: MergeRequestBuilder,
        code_review_agent: CodeReviewAgent,
        github_delivery: GitHubDeliveryService | None = None,
        tracer=None,
        figma_client: FigmaMCPClient | None = None,
        ui_acceptance_service: UIAcceptanceService | None = None,
        workflow_state: WorkflowStateStore | None = None,
    ):
        """组装需求从规划、开发、审查到交付的顶层业务编排器。

        Args:
            store: 持久化任务、Job、事件、检查点和评测结果的 SQLite 仓储。
            requirement_planning_agent: 生成需求分析和技术方案的规划 Agent。
            workspace_manager: 基于规划基线创建和管理隔离 Git Worktree。
            repository_analyzer: 识别仓库技术栈、基线、测试命令和相关代码上下文。
            code_development_agent: 在批准范围内探索上下文、生成修改并执行测试的开发 Agent。
            merge_request_builder: 根据最终 Diff、测试证据和验收标准生成 MR 草稿。
            code_review_agent: 对修改执行确定性检查和模型代码审查的 Reviewer Agent。
            github_delivery: 可选的 GitHub 分支、Pull Request、评论和合入服务。
            tracer: 可选的 Trace/Span 记录器，用于记录 Agent、模型和工具调用。
            figma_client: 可选的 Figma MCP 客户端，用于固定设计基线。
            ui_acceptance_service: 可选的浏览器截图与视觉差异验收服务。
            workflow_state: 可选的 LangGraph 状态存储；未提供时使用内存 Checkpointer。
        """
        self.store = store
        self.requirement_planning_agent = requirement_planning_agent
        self.workspace_manager = workspace_manager
        self.repository_analyzer = repository_analyzer
        self.code_development_agent = code_development_agent
        self.merge_request_builder = merge_request_builder
        self.code_review_agent = code_review_agent
        self.github_delivery = github_delivery
        self.tracer = tracer
        self.figma_client = figma_client
        self.ui_acceptance_service = ui_acceptance_service
        self.workflow_state = workflow_state or WorkflowStateStore.memory()
        self.workflow_state.migrate(self.store.list_tasks(), self.store.latest_checkpoint)
        self.delivery_graph = TaskDeliveryGraph(self)

    def create_task(
        self,
        title: str,
        requirement: str,
        repository_id: str = "",
        *,
        figma_url: str = "",
        preview_url: str = "",
        viewport_width: int = 1440,
        viewport_height: int = 900,
    ) -> Task:
        """创建任务并完成仓库分析、需求分析和技术方案生成。

        Args:
            title: 用户可识别的任务标题。
            requirement: 需要 Agent 完成的完整需求描述。
            repository_id: 目标仓库 ID；未提供且系统仅有一个仓库时自动选择。
            figma_url: 可选的 Figma 文件或节点地址，用于固定 UI 设计基线。
            preview_url: 可选的实现页面地址，用于后续 UI 验收。
            viewport_width: UI 验收视口宽度，单位为像素。
            viewport_height: UI 验收视口高度，单位为像素。

        Returns:
            已生成需求分析和技术方案、等待人工审批的任务。

        Raises:
            ValueError: 无法唯一确定目标仓库，或仓库、设计基线分析失败。
        """
        if not repository_id:
            repositories = self.store.list_repositories()
            if len(repositories) != 1:
                raise ValueError("Repository must be selected before creating a task")
            repository_id = repositories[0].id
        if self.tracer is not None:
            with self.tracer.trace(
                "task.create", kind="planning", metadata={"repository_id": repository_id}
            ) as trace:
                task = self.delivery_graph.create(
                    title, requirement, repository_id,
                    figma_url=figma_url, preview_url=preview_url,
                    viewport_width=viewport_width, viewport_height=viewport_height,
                )
                trace.task_id = task.id
                self.store.save_trace(trace)
                return task
        return self.delivery_graph.create(
            title, requirement, repository_id,
            figma_url=figma_url, preview_url=preview_url,
            viewport_width=viewport_width, viewport_height=viewport_height,
        )

    def rerun_task(self, task_id: str) -> Task:
        """基于已结束任务的原始需求创建一次全新的规划执行。

        Args:
            task_id: 已合入、失败或被拒绝的源任务 ID。

        Returns:
            使用仓库最新基线重新规划的新任务。

        Raises:
            ValueError: 源任务状态不允许重跑。
        """
        original = self._require_task(task_id)
        rerunnable = {TaskStatus.MERGED, TaskStatus.FAILED, TaskStatus.REJECTED}
        if original.status not in rerunnable:
            raise ValueError(
                "Only merged, failed, or rejected tasks can be rerun as a new task"
            )
        task = self.create_task(
            original.title,
            original.requirement,
            original.repository_id or "",
            figma_url=original.design_reference.url if original.design_reference else "",
            preview_url=original.design_reference.preview_url if original.design_reference else "",
            viewport_width=original.design_reference.viewport_width if original.design_reference else 1440,
            viewport_height=original.design_reference.viewport_height if original.design_reference else 900,
        )
        task.metadata["rerun_of"] = original.id
        self.store.save_task(task)
        self._event(
            task,
            "task_rerun_created",
            f"已基于历史任务 {original.id} 创建重跑任务，并使用仓库最新基线重新规划",
            {"source_task_id": original.id, "source_status": original.status.value},
        )
        return task

    def create_followup_task(
        self, task_id: str, actor: str, feedback: str
    ) -> Task:
        """针对已合入任务创建继承上下文的后续修改任务。

        Args:
            task_id: 已合入的源任务 ID。
            actor: 提出后续修改的用户标识。
            feedback: 基于线上最新代码继续修改的意见。

        Returns:
            使用默认分支最新基线规划的后续任务。

        Raises:
            ValueError: 源任务未合入或关联仓库不存在。
        """
        original = self._require_task(task_id)
        if original.status != TaskStatus.MERGED:
            raise ValueError("Only a merged task can create a follow-up task")
        repository = self.store.get_repository(original.repository_id or "")
        if repository is None:
            raise ValueError("Repository not found")
        if repository.provider == "github" and original.repository_analysis:
            branch = original.repository_analysis.default_branch or "main"
            self.workspace_manager.sync_default_branch(Path(repository.local_path), branch)
        followup_requirement = (
            f"基于已合入任务 {original.id} 的最新代码继续开发。\n"
            f"原任务目标：{original.requirement}\n\n"
            f"本次后续修改意见：{feedback.strip()}"
        )
        task = self.create_task(
            f"{original.title} · 后续修改",
            followup_requirement,
            original.repository_id or "",
            figma_url=original.design_reference.url if original.design_reference else "",
            preview_url=original.design_reference.preview_url if original.design_reference else "",
            viewport_width=original.design_reference.viewport_width if original.design_reference else 1440,
            viewport_height=original.design_reference.viewport_height if original.design_reference else 900,
        )
        task.metadata.update({
            "followup_of": original.id,
            "followup_actor": actor,
            "inherited_conversation": [
                item.get("feedback", "")
                for item in original.metadata.get("user_feedback_rounds", [])
                if item.get("feedback")
            ],
        })
        self.store.save_task(task)
        self._event(
            task,
            "followup_task_created",
            f"已从合入任务 {original.id} 创建后续任务，并使用仓库最新基线",
            {"source_task_id": original.id, "actor": actor},
        )
        return task

    def retry_failed_task(
        self,
        task_id: str,
        job_id: str,
        should_pause: Callable[[], bool] | None = None,
    ) -> Task:
        """从失败上下文继续同一任务，并让 Agent 针对失败原因重新开发。

        Args:
            task_id: 处于失败状态的任务 ID。
            job_id: 承载本次续跑的后台 Job ID，用于保存检查点。
            should_pause: 可选暂停判定函数，在安全检查点决定是否停止。

        Returns:
            续跑后的最新任务状态。

        Raises:
            ValueError: 任务状态、工作区、仓库模式或技术方案不支持失败续跑。
        """
        task = self._require_task(task_id)
        if task.status != TaskStatus.FAILED:
            raise ValueError("Only a failed task can continue from its failure context")
        if not task.workspace or not Path(task.workspace).is_dir():
            raise ValueError("The failed task workspace is no longer available")
        repository = self.store.get_repository(task.repository_id or "")
        if repository is None:
            raise ValueError("Repository not found")
        if repository.execution_mode != "plan_only":
            raise ValueError("Failure-aware retry is only supported for model-driven repositories")
        if task.technical_plan is None:
            raise ValueError("Technical plan is missing")

        failure_context = self._failure_context(task)
        diagnosed_files = self._diagnose_failure_files(Path(task.workspace), failure_context)
        dependency_files = self.requirement_planning_agent.ensure_dependency_scope(
            task.technical_plan,
            task.repository_analysis,
            task.requirement,
        ) if task.repository_analysis else []
        added_files = [
            path for path in [*dependency_files, *diagnosed_files]
            if path not in task.technical_plan.affected_files
        ]
        # ensure_dependency_scope mutates the plan before this calculation, so
        # dependency_files are already present; retain them in retry metadata.
        diagnosed_added = [
            path for path in diagnosed_files
            if path not in task.technical_plan.affected_files
        ]
        task.technical_plan.affected_files.extend(diagnosed_added)
        added_files = list(dict.fromkeys([*dependency_files, *diagnosed_added]))
        if added_files:
            task.technical_plan.implementation_steps.append(
                "修复最近失败日志直接指向的文件：" + "、".join(added_files)
            )
        retry_number = int(task.metadata.get("failure_retry_count", 0)) + 1
        task.metadata["failure_retry_count"] = retry_number
        task.metadata["last_retry_added_files"] = added_files
        task.error = None
        self._transition(task, TaskStatus.DEVELOPING)
        self._event(
            task,
            "failure_retry_started",
            f"开始第 {retry_number} 次失败续跑，Agent 将先分析上次失败原因",
            {"job_id": job_id, "added_files": added_files},
        )
        return self._run_real_development(
            task,
            repository,
            checkpoint_handler=self._checkpoint_handler(task, job_id, should_pause),
            resume_stage="test_result_saved",
            resume_payload={
                "next_attempt": 1,
                "repair_context": failure_context,
                "review_feedback": "",
            },
            offset_attempts=True,
            attempt_event_prefix="失败续跑尝试",
        )

    @staticmethod
    def _failure_context(task: Task) -> str:
        recent = task.development_attempts[-3:]
        last_executed = next(
            (attempt for attempt in reversed(task.development_attempts) if attempt.test_command),
            None,
        )
        attempts = ([last_executed] if last_executed and last_executed not in recent else []) + recent
        details = [f"Task stopped with: {task.error or 'unknown failure'}"]
        for attempt in attempts:
            if attempt.output:
                details.append(
                    f"Attempt {attempt.attempt} output:\n{attempt.output[-8000:]}"
                )
        return "\n\n".join(details)[-18000:]

    @staticmethod
    def _diagnose_failure_files(workspace: Path, failure_context: str) -> list[str]:
        lines = failure_context.splitlines()
        keywords = ("error", "failed", "failure", "syntax", "unexpected", "not found")
        diagnostic_lines: list[str] = []
        for index, line in enumerate(lines):
            if any(keyword in line.lower() for keyword in keywords):
                diagnostic_lines.extend(lines[max(0, index - 1): index + 3])
        diagnostic_text = "\n".join(diagnostic_lines).replace("\\", "/").lower()
        matches: list[str] = []
        for path in workspace.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            relative = path.relative_to(workspace).as_posix()
            if relative.lower() in diagnostic_text and relative not in matches:
                matches.append(relative)
        return matches[:8]

    def _graph_create_task(self, state: TaskDeliveryState) -> dict:
        repository_id = state["repository_id"]
        repository = self.store.get_repository(repository_id)
        if repository is None:
            raise ValueError("Repository not found")
        task_id = uuid4().hex[:12]
        design_reference = None
        figma_url = state.get("figma_url", "")
        if figma_url.strip():
            if self.figma_client is None:
                raise ValueError("Figma MCP 尚未配置，请设置 FIGMA_MCP_URL")
            design_reference = self.figma_client.capture(
                task_id,
                figma_url.strip(),
                state.get("preview_url", "").strip() or None,
                state.get("viewport_width", 1440),
                state.get("viewport_height", 900),
            )
        task = Task(
            id=task_id,
            title=state["title"].strip(),
            requirement=state["requirement"].strip(),
            repository_id=repository_id,
            status=TaskStatus.REQUIREMENT_ANALYSIS,
            design_reference=design_reference,
        )
        self.store.save_task(task)
        self._event(task, "task_created", "任务已创建，开始分析需求")
        if design_reference:
            self._event(
                task,
                "figma_baseline_captured",
                f"已保存 Figma 节点 {design_reference.node_id} 的设计基线",
                {"snapshot_hash": design_reference.snapshot_hash},
            )
        return {"task": task, "repository": repository}

    def _graph_analyze_repository(self, state: TaskDeliveryState) -> dict:
        task = state["task"]
        repository = state["repository"]
        span = self.tracer.span("repository.analyze", kind="tool") if self.tracer else None
        if span:
            with span:
                task.repository_analysis = self.repository_analyzer.analyze(Path(repository.local_path), task.requirement)
        else:
            task.repository_analysis = self.repository_analyzer.analyze(Path(repository.local_path), task.requirement)
        self._event(
            task,
            "repository_analyzed",
            f"已完成仓库分析：{task.repository_analysis.language}，{task.repository_analysis.file_count} 个文件",
            {"tool_calls": [item.model_dump(mode="json") for item in task.repository_analysis.tool_calls]},
        )
        return {"task": task}

    def _graph_plan(self, state: TaskDeliveryState) -> dict:
        task = state["task"]
        span = self.tracer.span("agent.requirement_planning", kind="agent") if self.tracer else None
        if span:
            with span:
                task.analysis, task.technical_plan = self.requirement_planning_agent.plan(
                    task.title, task.requirement, task.repository_analysis,
                    task.design_reference.context if task.design_reference else "",
                )
        else:
            task.analysis, task.technical_plan = self.requirement_planning_agent.plan(
                task.title, task.requirement, task.repository_analysis,
                task.design_reference.context if task.design_reference else "",
            )
        task.metadata["planning_head_sha"] = task.repository_analysis.head_sha
        task.metadata["index_version"] = (
            task.repository_analysis.context_pack.index_version
            if task.repository_analysis.context_pack else None
        )
        task.metadata["plan_hash"] = self._technical_plan_hash(task)
        self._transition(task, TaskStatus.WAITING_REQUIREMENT_APPROVAL)
        self._event(task, "plan_ready", "需求分析和技术方案已生成，等待人工审批")
        return {"task": task}

    def approve(
        self,
        task_id: str,
        actor: str,
        comment: str = "",
        *,
        checkpoint_job_id: str | None = None,
        should_pause: Callable[[], bool] | None = None,
    ) -> Task:
        """批准技术方案并启动开发交付图。

        Args:
            task_id: 等待需求审批的任务 ID。
            actor: 执行审批的用户标识。
            comment: 可选的审批说明。
            checkpoint_job_id: 可选后台 Job ID，用于将开发阶段检查点关联到本次执行。
            should_pause: 可选暂停判定函数，在安全检查点决定是否停止。

        Returns:
            开发、测试和审查推进后的最新任务。
        """
        return self.delivery_graph.approve(
            task_id,
            actor,
            comment,
            checkpoint_job_id=checkpoint_job_id,
            should_pause=should_pause,
        )

    def _graph_approve_plan(self, state: TaskDeliveryState) -> dict:
        task = self._require_task(state["task_id"])
        if task.status != TaskStatus.WAITING_REQUIREMENT_APPROVAL:
            raise ValueError("Only a task waiting for approval can be approved")
        repository = self.store.get_repository(task.repository_id or "")
        if repository is None:
            raise ValueError("Repository not found")
        if self._replan_if_repository_changed(task, repository):
            return {"task": task, "repository": repository, "route": "done"}

        actor = state["actor"]
        comment = state.get("comment", "")
        task.approval = Approval(decision="approved", actor=actor, comment=comment)
        task.metadata["approved_plan_hash"] = self._technical_plan_hash(task)
        task.metadata["development_head_sha"] = (
            task.repository_analysis.head_sha if task.repository_analysis else None
        )
        task.metadata["approved_index_version"] = task.metadata.get("index_version")
        self.store.save_task(task)
        self._transition(task, TaskStatus.DEVELOPING)
        message = (
            f"{actor} 已批准技术方案，开始创建隔离 Worktree"
            if repository.execution_mode == "plan_only"
            else f"{actor} 已批准技术方案"
        )
        self._event(task, "approved", message, {"comment": comment})
        handler = self._checkpoint_handler(
            task, state.get("checkpoint_job_id"), state.get("should_pause")
        )
        if handler and handler("plan_approved", "prepare_workspace", {}):
            return {
                "task": task,
                "repository": repository,
                "checkpoint_handler": handler,
                "route": "done",
            }
        return {
            "task": task,
            "repository": repository,
            "checkpoint_handler": handler,
            "route": "develop",
        }

    def _graph_develop(self, state: TaskDeliveryState) -> dict:
        task = state["task"]
        repository = state["repository"]
        handler = state.get("checkpoint_handler")
        task = self._run_real_development(
            task,
            repository,
            checkpoint_handler=handler,
            finalize_pipeline=False,
        )
        return {"task": task}

    def _graph_load_review(self, state: TaskDeliveryState) -> dict:
        task = self._require_task(state["task_id"])
        if task.status == TaskStatus.REVIEW_REPAIRING:
            raise ValueError(
                "Code Review 修复正在执行或曾被中断，请通过任务恢复入口继续"
            )
        if task.status not in {TaskStatus.CHANGE_READY, TaskStatus.CHANGES_REQUESTED}:
            raise ValueError("Task is not ready for code review")
        repository = self.store.get_repository(task.repository_id or "")
        if repository is None:
            raise ValueError("Repository not found")
        handler = self._checkpoint_handler(
            task,
            state.get("checkpoint_job_id"),
            state.get("should_pause"),
        )
        if task.status == TaskStatus.CHANGES_REQUESTED:
            refreshed = self._refresh_review_snapshot(task, repository, handler)
            return {
                "task": task,
                "repository": repository,
                "checkpoint_handler": handler,
                "route": "review" if refreshed else "done",
            }
        return {
            "task": task,
            "repository": repository,
            "checkpoint_handler": handler,
        }

    def _refresh_review_snapshot(self, task: Task, repository_config, checkpoint_handler=None) -> bool:
        """Validate the live workspace and replace stale review inputs before rerunning CR."""
        task.metadata["review_cycle_start"] = len(task.reviews)
        task.error = None
        self._transition(task, TaskStatus.REVIEW_REPAIRING)
        self._event(
            task,
            "review_snapshot_refresh_started",
            "重新审查前正在验证当前工作区并刷新 Diff 与测试证据",
        )
        previous_findings = (
            self._review_feedback(task.reviews[-1]) if task.reviews else ""
        )
        refresh_feedback = (
            "以下审查意见来自上一次代码快照，必须先结合当前工作区和 Git 基线重新核对，"
            "不要机械重复已经失效的修改：\n" + previous_findings
        )
        outcome = self.code_development_agent.run(
            task,
            Path(task.workspace),
            dependency_repository=Path(repository_config.local_path),
            review_feedback=refresh_feedback,
            checkpoint_handler=checkpoint_handler,
            resume_stage="test_result_saved",
            resume_payload={
                "next_attempt": 1,
                "repair_context": (
                    "重新审查前必须先验证当前工作区。重新读取当前文件、运行批准的测试命令，"
                    "如果失败则基于真实测试错误继续修复；不得沿用数据库中缓存的旧 Diff。"
                ),
                "review_feedback": refresh_feedback,
            },
        )
        self._append_attempts(task, outcome.attempts, "重新审查预检")
        if outcome.kind == "paused":
            if outcome.result is not None:
                task.result = outcome.result
            self.store.save_task(task)
            return False
        if outcome.kind == "risk_approval":
            task.pending_proposal = outcome.pending_proposal
            task.metadata["risk_reasons"] = outcome.risk_reasons
            task.metadata["review_repair_pending"] = True
            task.metadata["pending_repair_feedback"] = refresh_feedback
            self._transition(task, TaskStatus.WAITING_RISK_APPROVAL)
            self._event(task, "risk_approval_required", "重新审查预检涉及高风险文件，等待人工审批")
            return False
        if outcome.kind != "success" or outcome.result is None:
            task.error = outcome.error or "Review snapshot refresh failed"
            self._transition(task, TaskStatus.CHANGES_REQUESTED)
            self._event(
                task,
                "review_snapshot_refresh_failed",
                "当前工作区验证未通过，未使用旧 Diff 继续审查",
                {"error": task.error},
            )
            return False

        task.result = outcome.result
        task.error = None
        diff_sha256 = hashlib.sha256(task.result.diff.encode("utf-8")).hexdigest()
        task.metadata["review_snapshot"] = {
            "diff_sha256": diff_sha256,
            "test_exit_code": task.result.exit_code,
            "refreshed_at": datetime.now(UTC).isoformat(),
        }
        self._transition(task, TaskStatus.GENERATING_MR)
        task.merge_request = self._write_merge_request(task)
        self._event(
            task,
            "review_snapshot_refreshed",
            "已使用当前工作区的最新 Diff 与测试结果重新生成 MR 草稿",
            {"diff_sha256": diff_sha256, "test_exit_code": task.result.exit_code},
        )
        return True

    def _graph_generate_mr(self, state: TaskDeliveryState) -> dict:
        task = state["task"]
        self._prepare_review_pipeline(task)
        return {"task": task}

    def _graph_review(self, state: TaskDeliveryState) -> dict:
        task = self._review_loop(
            state["task"],
            state["repository"],
            checkpoint_handler=state.get("checkpoint_handler"),
        )
        return {"task": task}

    def reject(self, task_id: str, actor: str, comment: str) -> Task:
        """拒绝当前技术方案并终止本次任务。

        Args:
            task_id: 等待需求审批的任务 ID。
            actor: 执行拒绝操作的用户标识。
            comment: 拒绝原因或修改建议。

        Returns:
            状态已变为 ``rejected`` 的任务。
        """
        task = self._require_task(task_id)
        if task.status != TaskStatus.WAITING_REQUIREMENT_APPROVAL:
            raise ValueError("Only a task waiting for approval can be rejected")
        task.approval = Approval(decision="rejected", actor=actor, comment=comment)
        self._transition(task, TaskStatus.REJECTED)
        self._event(task, "rejected", f"{actor} 已拒绝技术方案", {"comment": comment})
        return task

    def approve_risk(self, task_id: str, actor: str, comment: str = "") -> Task:
        """批准 Developer Agent 提出的高风险代码修改方案并继续执行。

        Args:
            task_id: 等待风险审批的任务 ID。
            actor: 批准高风险修改的用户标识。
            comment: 可选的审批说明。

        Returns:
            应用高风险方案并继续开发或 CR 修复后的任务。
        """
        task = self._require_task(task_id)
        if task.status != TaskStatus.WAITING_RISK_APPROVAL or task.pending_proposal is None:
            raise ValueError("Task is not waiting for a risk approval")
        repository = self.store.get_repository(task.repository_id or "")
        if repository is None:
            raise ValueError("Repository not found")
        proposal = task.pending_proposal
        task.pending_proposal = None
        if task.metadata.pop("review_repair_pending", False):
            self._transition(task, TaskStatus.REVIEW_REPAIRING)
            self._event(task, "risk_approved", f"{actor} 已批准 CR 高风险修复", {"comment": comment})
            outcome = self.code_development_agent.run(
                task,
                Path(task.workspace),
                approved_proposal=proposal,
                high_risk_approved=True,
                dependency_repository=Path(repository.local_path),
                review_feedback=task.metadata.pop(
                    "pending_repair_feedback",
                    self._review_feedback(task.reviews[-1]),
                ),
            )
            self._append_attempts(task, outcome.attempts)
            if outcome.kind != "success":
                task.error = outcome.error or "Review repair failed"
                self._transition(task, TaskStatus.CHANGES_REQUESTED)
                return task
            task.result = outcome.result
            self._transition(task, TaskStatus.GENERATING_MR)
            task.merge_request = self._write_merge_request(task)
            self._event(task, "merge_request_regenerated", "高风险 CR 修复完成，已重新生成 MR")
            completed = self._review_loop(task, repository)
            comment_keys = [
                str(key) for key in completed.metadata.get("pending_github_comment_keys", [])
            ]
            if not comment_keys:
                return completed
            if completed.status == TaskStatus.WAITING_RELEASE_APPROVAL:
                completed = self.publish_pull_request(task_id, actor)
                changed = ", ".join(completed.merge_request.changed_files) if completed.merge_request else ""
                detail = (
                    f"修改文件：{changed or '见 PR Diff'}\n"
                    f"测试：{completed.merge_request.test_summary if completed.merge_request else '已通过'}\n"
                    f"Commit：`{completed.remote_pull_request.head_sha[:12]}`"
                )
                return self.complete_github_comment_feedback(
                    task_id, comment_keys, True, detail
                )
            return self.complete_github_comment_feedback(
                task_id,
                comment_keys,
                False,
                completed.error or "高风险修改完成后 Code Review 仍未通过。",
            )
        self._transition(task, TaskStatus.DEVELOPING)
        self._event(task, "risk_approved", f"{actor} 已批准高风险修改", {"comment": comment})
        return self._run_real_development(task, repository, proposal, high_risk_approved=True)

    def reject_risk(self, task_id: str, actor: str, comment: str) -> Task:
        """拒绝待审批的高风险代码修改方案。

        Args:
            task_id: 等待风险审批的任务 ID。
            actor: 执行拒绝操作的用户标识。
            comment: 拒绝原因。

        Returns:
            已清除待处理方案并标记为拒绝的任务。
        """
        task = self._require_task(task_id)
        if task.status != TaskStatus.WAITING_RISK_APPROVAL:
            raise ValueError("Task is not waiting for a risk approval")
        task.pending_proposal = None
        task.approval = Approval(decision="risk_rejected", actor=actor, comment=comment)
        self._transition(task, TaskStatus.REJECTED)
        self._event(task, "risk_rejected", f"{actor} 已拒绝高风险修改", {"comment": comment})
        return task

    def _run_real_development(
        self,
        task: Task,
        repository_config,
        approved_proposal=None,
        *,
        high_risk_approved: bool = False,
        checkpoint_handler=None,
        resume_stage: str | None = None,
        resume_payload: dict | None = None,
        offset_attempts: bool = False,
        attempt_event_prefix: str = "开发尝试",
        finalize_pipeline: bool = True,
    ) -> Task:
        try:
            if not task.workspace and self._replan_if_repository_changed(task, repository_config):
                return task
            if not task.workspace and not self._approved_snapshot_is_valid(task):
                task.approval = None
                task.metadata.pop("approved_plan_hash", None)
                task.metadata.pop("development_head_sha", None)
                self._transition(task, TaskStatus.WAITING_REQUIREMENT_APPROVAL)
                self._event(
                    task,
                    "approved_snapshot_invalid",
                    "已批准的方案或索引快照发生变化，请重新审批",
                )
                return task
            if not self.code_development_agent.model_gateway.enabled:
                raise ValueError(
                    "No model provider is configured. Real-repository development requires an LLM."
                )
            if task.workspace:
                workspace = Path(task.workspace)
            else:
                baseline = task.repository_analysis.head_sha if task.repository_analysis else None
                if not baseline:
                    raise ValueError("Planning baseline SHA is missing")
                workspace = self.workspace_manager.prepare_worktree(
                    task.id, Path(repository_config.local_path), baseline
                )
                task.workspace = str(workspace)
                self._event(task, "worktree_ready", "已从规划基线创建隔离 Git Worktree", {"path": str(workspace), "baseline": baseline})

            use_steps = bool(
                approved_proposal is None
                and task.technical_plan
                and task.technical_plan.development_steps
            )
            if use_steps:
                outcome = self._run_development_steps(
                    task,
                    workspace,
                    repository_config,
                    checkpoint_handler=checkpoint_handler,
                    resume_stage=resume_stage,
                    resume_payload=resume_payload,
                )
            else:
                outcome = self.code_development_agent.run(
                    task,
                    workspace,
                    approved_proposal=approved_proposal,
                    high_risk_approved=high_risk_approved,
                    dependency_repository=Path(repository_config.local_path),
                    checkpoint_handler=checkpoint_handler,
                    resume_stage=resume_stage,
                    resume_payload=resume_payload,
                )
            if offset_attempts:
                self._append_attempts(task, outcome.attempts, attempt_event_prefix)
            else:
                task.development_attempts.extend(outcome.attempts)
                for attempt in outcome.attempts:
                    self._event(
                        task,
                        "development_attempt",
                        f"开发尝试 #{attempt.attempt}：测试退出码 {attempt.exit_code}",
                        {"attempt": attempt.model_dump(mode="json")},
                    )
            if outcome.kind == "paused":
                if outcome.result is not None:
                    task.result = outcome.result
                self.store.save_task(task)
                return task
            if outcome.kind == "risk_approval":
                task.pending_proposal = outcome.pending_proposal
                task.metadata["risk_reasons"] = outcome.risk_reasons
                self._transition(task, TaskStatus.WAITING_RISK_APPROVAL)
                self._event(task, "risk_approval_required", "检测到高风险修改，等待人工审批", {"reasons": outcome.risk_reasons})
            elif outcome.kind == "success":
                self._transition(task, TaskStatus.TESTING)
                task.result = outcome.result
                self._transition(task, TaskStatus.CHANGE_READY)
                self._event(task, "change_ready", "真实仓库隔离开发完成，测试通过")
                if finalize_pipeline:
                    return self._finish_review_pipeline(
                        task, repository_config, checkpoint_handler=checkpoint_handler
                    )
                return task
            else:
                task.error = outcome.error or "Development failed"
                self._transition(task, TaskStatus.FAILED)
                self._event(task, "development_failed", "真实仓库开发失败", {"error": task.error})
        except Exception as error:
            task.error = str(error)
            if task.status in {TaskStatus.DEVELOPING, TaskStatus.TESTING, TaskStatus.REPAIRING}:
                self._transition(task, TaskStatus.FAILED)
            self._event(task, "development_failed", "真实仓库开发失败", {"error": str(error)})
        return task

    def _run_development_steps(
        self,
        task: Task,
        workspace: Path,
        repository_config,
        *,
        checkpoint_handler=None,
        resume_stage: str | None = None,
        resume_payload: dict | None = None,
    ):
        """Run all bounded plan steps inside one task-level Developer Session."""
        session_id = task.metadata.get("developer_session_id")
        if not session_id:
            session_id = uuid4().hex[:16]
            task.metadata["developer_session_id"] = session_id
            self._event(task, "developer_session_started", "Developer Agent 会话已启动", {
                "developer_session_id": session_id,
                "step_count": len(task.technical_plan.development_steps),
            })
        scope = self.tracer.span(
            "agent.code_development_session",
            kind="agent",
            attributes={
                "developer_session_id": session_id,
                "step_count": len(task.technical_plan.development_steps),
            },
        ) if self.tracer else None
        if scope is not None:
            with scope:
                return self._execute_development_session(
                    task, workspace, repository_config,
                    checkpoint_handler=checkpoint_handler,
                    resume_stage=resume_stage,
                    resume_payload=resume_payload,
                    session_id=session_id,
                )
        return self._execute_development_session(
            task, workspace, repository_config,
            checkpoint_handler=checkpoint_handler,
            resume_stage=resume_stage,
            resume_payload=resume_payload,
            session_id=session_id,
        )

    def _execute_development_session(
        self,
        task: Task,
        workspace: Path,
        repository_config,
        *,
        checkpoint_handler=None,
        resume_stage: str | None = None,
        resume_payload: dict | None = None,
        session_id: str,
    ):
        from agents.code_development_agent import DevelopmentRunOutcome

        steps = task.technical_plan.development_steps
        existing = {item.step_id: item for item in task.step_executions}
        task.step_executions = [
            existing.get(step.id) or StepExecution(step_id=step.id, title=step.title)
            for step in steps
        ]
        all_attempts = []
        next_attempt_number = len(task.development_attempts) + 1
        session_context: dict[str, str] = {}
        session_files = list(task.metadata.get("developer_session_files", []))
        latest_result = task.result
        resume_payload = resume_payload or {}
        resume_index = resume_payload.get("development_step_index")

        for index, step in enumerate(steps):
            execution = task.step_executions[index]
            if execution.status == "completed":
                continue
            if resume_index is not None and index < int(resume_index):
                continue

            execution.status = "running"
            execution.error = None
            self._event(task, "development_step_started", f"开始步骤 {index + 1}/{len(steps)}：{step.title}", {
                "step_id": step.id, "step_index": index, "allowed_files": step.allowed_files,
            })

            def step_checkpoint(stage, next_action, payload):
                enriched = {
                    **payload,
                    "development_step_id": step.id,
                    "development_step_title": step.title,
                    "development_step_index": index,
                    "development_step_count": len(steps),
                }
                return checkpoint_handler(stage, next_action, enriched) if checkpoint_handler else False

            current_resume_stage = resume_stage if resume_index is not None and index == int(resume_index) else None
            current_resume_payload = resume_payload if current_resume_stage else None
            if (
                current_resume_stage == "test_result_saved"
                and current_resume_payload.get("success")
            ):
                outcome = DevelopmentRunOutcome(
                    kind="success",
                    result=ExecutionResult.model_validate(current_resume_payload["result"]),
                )
            else:
                outcome = self.code_development_agent.run(
                    task,
                    workspace,
                    dependency_repository=Path(repository_config.local_path),
                    checkpoint_handler=step_checkpoint,
                    resume_stage=current_resume_stage,
                    resume_payload=current_resume_payload,
                    allowed_files=step.allowed_files,
                    step_context={
                        **step.model_dump(mode="json"),
                        "developer_session_id": session_id,
                        "step_index": index,
                        "step_count": len(steps),
                    },
                    session_context=session_context,
                    session_files=session_files,
                )
            for attempt in outcome.attempts:
                attempt.step_id = step.id
                attempt.step_title = step.title
                attempt.step_attempt = attempt.attempt
                attempt.attempt = next_attempt_number
                next_attempt_number += 1
            task.metadata["developer_session_files"] = list(session_context)[:50]
            all_attempts.extend(outcome.attempts)
            execution.attempt_count += len(outcome.attempts)
            execution.changed_files = list(dict.fromkeys([
                *execution.changed_files,
                *(path for attempt in outcome.attempts for path in attempt.changed_files),
            ]))
            if outcome.attempts:
                execution.diff = outcome.attempts[-1].diff
                execution.test_output = outcome.attempts[-1].output

            if outcome.kind == "paused":
                execution.status = "paused"
                latest_result = outcome.result or latest_result
                return DevelopmentRunOutcome(
                    kind="paused", attempts=all_attempts, result=latest_result,
                    paused_at=outcome.paused_at,
                )
            if outcome.kind != "success":
                execution.status = "failed"
                execution.error = outcome.error or "本步骤执行失败"
                self._event(task, "development_step_failed", f"步骤失败：{step.title}", {
                    "step_id": step.id, "step_index": index, "error": execution.error,
                })
                return DevelopmentRunOutcome(
                    kind=outcome.kind,
                    attempts=all_attempts,
                    pending_proposal=outcome.pending_proposal,
                    risk_reasons=outcome.risk_reasons,
                    error=execution.error,
                )

            latest_result = outcome.result or latest_result
            execution.status = "completed"
            execution.error = None
            if outcome.result:
                execution.diff = outcome.result.diff
                execution.test_output = outcome.result.output
            self._event(task, "development_step_completed", f"步骤完成：{step.title}", {
                "step_id": step.id, "step_index": index,
                "changed_files": execution.changed_files,
            })
            self.store.save_task(task)
            resume_stage = None
            resume_payload = {}
            resume_index = None

        if latest_result is None:
            return DevelopmentRunOutcome(kind="failed", attempts=all_attempts, error="No development step produced a result")
        return DevelopmentRunOutcome(kind="success", attempts=all_attempts, result=latest_result)

    def run_review(
        self,
        task_id: str,
        *,
        job_id: str | None = None,
        should_pause: Callable[[], bool] | None = None,
    ) -> Task:
        """对当前代码重新生成 MR 草稿并执行 Code Review 流程。

        Args:
            task_id: 已具备代码改动、可进入审查的任务 ID。
            job_id: 可选后台审查 Job ID，用于保存检查点。
            should_pause: 可选暂停判定函数，在安全检查点决定是否停止。

        Returns:
            审查通过、要求修改或暂停后的最新任务。
        """
        return self.delivery_graph.review(
            task_id,
            checkpoint_job_id=job_id,
            should_pause=should_pause,
        )

    def apply_user_feedback(
        self,
        task_id: str,
        actor: str,
        feedback: str,
        job_id: str | None = None,
        should_pause: Callable[[], bool] | None = None,
    ) -> Task:
        """在同一工作区中应用用户验收意见，并重新测试和审查累计修改。

        Args:
            task_id: 等待用户验收或存在 CR 修改要求的任务 ID。
            actor: 提交反馈的用户标识。
            feedback: 本轮具体修改意见；也可以是受支持的撤销指令。
            job_id: 可选后台 Job ID，用于保存本轮修改检查点。
            should_pause: 可选暂停判定函数，在安全检查点决定是否停止。

        Returns:
            本轮反馈处理后的最新任务。

        Raises:
            ValueError: 当前状态不接受反馈、反馈为空或 Developer Agent 不可用。
        """
        task = self._require_task(task_id)
        if task.status not in {
            TaskStatus.WAITING_RELEASE_APPROVAL,
            TaskStatus.CHANGES_REQUESTED,
            TaskStatus.WAITING_MERGE_APPROVAL,
        }:
            raise ValueError("Task is not waiting for user acceptance feedback")
        repository = self.store.get_repository(task.repository_id or "")
        if repository is None:
            raise ValueError("Repository not found")
        normalized_feedback = feedback.strip()
        if not normalized_feedback:
            raise ValueError("Feedback cannot be empty")
        rollback_requested = self._is_feedback_rollback(normalized_feedback)
        if (
            not rollback_requested
            and (
                repository.execution_mode != "plan_only"
                or not self.code_development_agent.model_gateway.enabled
            )
        ):
            raise ValueError("User feedback repair requires a configured Developer Agent")

        before_snapshot = self._workspace_snapshot(task)
        snapshot_id = self._save_feedback_snapshot(task, before_snapshot)
        feedback_round = {
            "actor": actor,
            "feedback": normalized_feedback,
            "submitted_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "snapshot_id": snapshot_id,
            "agent_message": "Agent 正在根据这条意见修改代码。",
            "changed_files": [],
            "diff": "",
        }
        task.metadata.setdefault("user_feedback_rounds", []).append(feedback_round)
        task.metadata["review_cycle_start"] = len(task.reviews)
        task.error = None
        if task.status in {
            TaskStatus.WAITING_RELEASE_APPROVAL,
            TaskStatus.WAITING_MERGE_APPROVAL,
        }:
            self._transition(task, TaskStatus.CHANGES_REQUESTED)
        self._event(
            task,
            "user_feedback_submitted",
            f"{actor} 查看 Diff 后要求继续修改",
            {"feedback": normalized_feedback, "job_id": job_id},
        )

        if rollback_requested:
            return self._rollback_previous_feedback(
                task, feedback_round, before_snapshot, actor
            )
        self._transition(task, TaskStatus.REVIEW_REPAIRING)
        repair_feedback = (
            "用户在发布 PR 前验收代码后提出以下修改意见。必须基于当前工作区继续修改，"
            "保留此前正确实现，不要新建任务：\n- " + normalized_feedback
        )
        outcome = self.code_development_agent.run(
            task,
            Path(task.workspace),
            dependency_repository=Path(repository.local_path),
            review_feedback=repair_feedback,
            checkpoint_handler=self._checkpoint_handler(task, job_id, should_pause),
        )
        self._append_attempts(task, outcome.attempts, "用户反馈修复")
        if outcome.kind == "paused":
            if outcome.result is not None:
                task.result = outcome.result
            self._finish_user_feedback_round(
                task, feedback_round, before_snapshot, "paused", "执行已在安全检查点暂停，可以稍后继续。"
            )
            return task

        if outcome.kind == "risk_approval":
            task.pending_proposal = outcome.pending_proposal
            task.metadata["risk_reasons"] = outcome.risk_reasons
            task.metadata["review_repair_pending"] = True
            task.metadata["pending_repair_feedback"] = repair_feedback
            self._transition(task, TaskStatus.WAITING_RISK_APPROVAL)
            self._event(task, "risk_approval_required", "用户反馈修复涉及高风险文件，等待人工审批")
            self._finish_user_feedback_round(
                task, feedback_round, before_snapshot, "waiting_risk_approval",
                "已生成修改方案，但涉及高风险文件，需要批准后才能应用。",
            )
            return task
        if outcome.kind != "success":
            task.error = outcome.error or "User feedback repair failed"
            self._transition(task, TaskStatus.CHANGES_REQUESTED)
            self._event(task, "user_feedback_repair_failed", "用户反馈修复失败", {"error": task.error})
            self._finish_user_feedback_round(
                task, feedback_round, before_snapshot, "failed",
                f"本轮修改未完成：{task.error}",
            )
            return task

        task.result = outcome.result
        task.error = None
        self._transition(task, TaskStatus.GENERATING_MR)
        task.merge_request = self._write_merge_request(task)
        self._event(task, "merge_request_regenerated", "用户反馈修复测试通过，已重新生成 MR 草稿")
        completed = self._review_loop(
            task,
            repository,
            checkpoint_handler=self._checkpoint_handler(task, job_id, should_pause),
        )
        decision = completed.reviews[-1].decision if completed.reviews else "not_run"
        message = (
            "已按意见完成修改，测试和 Code Review 均已通过，请查看当前文件对比。"
            if completed.status == TaskStatus.WAITING_RELEASE_APPROVAL
            else "已完成代码修改和测试，但 Code Review 仍有阻塞问题，请查看当前文件对比和审查意见。"
        )
        self._finish_user_feedback_round(
            completed, feedback_round, before_snapshot, completed.status.value, message,
            review_decision=decision,
        )
        self._event(
            completed,
            "user_feedback_completed",
            "Agent 已完成本轮用户反馈处理",
            {"changed_files": feedback_round["changed_files"], "review_decision": decision},
        )
        return completed

    def _replan_if_repository_changed(self, task: Task, repository_config) -> bool:
        if repository_config.execution_mode != "plan_only" or task.repository_analysis is None:
            return False
        repository_path = Path(repository_config.local_path)
        planned_head = task.repository_analysis.head_sha
        current_head = self.repository_analyzer.current_head(repository_path)
        context = task.repository_analysis.context_pack
        index_stale = bool(
            context and context.index_version != self.repository_analyzer.index_version
        )
        if (not planned_head or not current_head or planned_head == current_head) and not index_stale:
            return False

        previous_head = planned_head
        task.repository_analysis = self.repository_analyzer.analyze(
            repository_path, task.requirement
        )
        task.analysis, task.technical_plan = self.requirement_planning_agent.plan(
            task.title, task.requirement, task.repository_analysis,
            task.design_reference.context if task.design_reference else "",
        )
        task.approval = None
        task.error = None
        task.metadata["planning_head_sha"] = task.repository_analysis.head_sha
        task.metadata["index_version"] = (
            task.repository_analysis.context_pack.index_version
            if task.repository_analysis.context_pack else None
        )
        task.metadata["plan_hash"] = self._technical_plan_hash(task)
        task.metadata.pop("approved_plan_hash", None)
        task.metadata.pop("development_head_sha", None)
        task.metadata["baseline_refresh_count"] = int(
            task.metadata.get("baseline_refresh_count", 0)
        ) + 1
        if task.status == TaskStatus.DEVELOPING:
            self._transition(task, TaskStatus.WAITING_REQUIREMENT_APPROVAL)
        else:
            self.store.save_task(task)
        context = task.repository_analysis.context_pack
        self._event(
            task,
            "repository_baseline_changed",
            "仓库 HEAD 已变化，索引和技术方案已更新，请重新审批",
            {
                "previous_head": previous_head,
                "current_head": current_head,
                "index_mode": context.index_mode if context else None,
                "changed_files": context.changed_files if context else [],
            },
        )
        return True

    def _approved_snapshot_is_valid(self, task: Task) -> bool:
        if not task.metadata.get("approved_plan_hash"):
            return False
        context = task.repository_analysis.context_pack if task.repository_analysis else None
        return all((
            task.metadata.get("approved_plan_hash") == self._technical_plan_hash(task),
            task.metadata.get("development_head_sha") == (
                task.repository_analysis.head_sha if task.repository_analysis else None
            ),
            task.metadata.get("approved_index_version") == (
                context.index_version if context else None
            ),
        ))

    @staticmethod
    def _technical_plan_hash(task: Task) -> str:
        serialized = task.technical_plan.model_dump_json() if task.technical_plan else ""
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def approve_review(self, task_id: str, actor: str, comment: str = "") -> Task:
        """人工覆盖阻塞性 Code Review 结论并进入发布审批。

        Args:
            task_id: 状态为 ``changes_requested`` 的任务 ID。
            actor: 执行人工批准的用户标识。
            comment: 可选的覆盖原因。

        Returns:
            等待发布审批的任务。
        """
        task = self._require_task(task_id)
        if task.status != TaskStatus.CHANGES_REQUESTED:
            raise ValueError("Only a task with requested changes can be manually approved")
        task.metadata.setdefault("review_overrides", []).append({"actor": actor, "comment": comment})
        self._transition(task, TaskStatus.REVIEW_APPROVED)
        self._event(task, "review_override_approved", f"{actor} 已人工批准 Code Review", {"comment": comment})
        self._transition(task, TaskStatus.WAITING_RELEASE_APPROVAL)
        self._event(task, "release_approval_required", "Code Review 已通过，等待发布审批")
        return task

    def reject_release(self, task_id: str, actor: str, comment: str = "") -> Task:
        """在发布或合入审批阶段拒绝任务。

        Args:
            task_id: 等待发布或合入审批的任务 ID。
            actor: 执行拒绝操作的用户标识。
            comment: 可选的拒绝原因。

        Returns:
            状态已变为 ``rejected`` 的任务。
        """
        task = self._require_task(task_id)
        if task.status not in {TaskStatus.WAITING_RELEASE_APPROVAL, TaskStatus.WAITING_MERGE_APPROVAL}:
            raise ValueError("Task is not waiting for release or merge approval")
        task.approval = Approval(decision="release_rejected", actor=actor, comment=comment)
        self._transition(task, TaskStatus.REJECTED)
        self._event(task, "release_rejected", f"{actor} 已拒绝进入发布阶段", {"comment": comment})
        return task

    def publish_pull_request(self, task_id: str, actor: str) -> Task:
        """将已验收修改推送到 GitHub 并创建或更新 Pull Request。

        Args:
            task_id: 已通过测试、CR 和 UI 验收，等待发布的任务 ID。
            actor: 确认发布的用户标识。

        Returns:
            已关联远程 PR、等待人工合入的任务。

        Raises:
            ValueError: 任务未就绪、UI 验收阻塞、GitHub 未配置或发布失败。
        """
        task = self._require_task(task_id)
        if task.status != TaskStatus.WAITING_RELEASE_APPROVAL:
            raise ValueError("Task is not ready to publish a GitHub pull request")
        if task.ui_acceptance and task.ui_acceptance.blocking:
            raise ValueError("UI 验收存在阻塞问题，请修复后重新验收")
        if self.github_delivery is None:
            raise ValueError("GitHub delivery is not configured")
        repository = self.store.get_repository(task.repository_id or "")
        if repository is None:
            raise ValueError("Repository not found")
        previous_pull_request = task.remote_pull_request
        self._transition(task, TaskStatus.PUBLISHING_PULL_REQUEST)
        self._event(task, "pull_request_publish_started", f"{actor} 已确认发布 GitHub Pull Request")
        try:
            task.remote_pull_request = self.github_delivery.publish(task, repository)
            task.error = None
            self._transition(task, TaskStatus.WAITING_MERGE_APPROVAL)
            updated_existing = (
                previous_pull_request is not None
                and previous_pull_request.number == task.remote_pull_request.number
            )
            event_type = "pull_request_updated" if updated_existing else "pull_request_published"
            event_message = (
                f"GitHub PR #{task.remote_pull_request.number} 已更新，等待人工合入"
                if updated_existing
                else f"GitHub PR #{task.remote_pull_request.number} 已创建，等待人工合入"
            )
            self._event(
                task,
                event_type,
                event_message,
                {"url": task.remote_pull_request.url, "number": task.remote_pull_request.number},
            )
            return task
        except Exception as error:
            task.error = str(error)
            self._transition(task, TaskStatus.WAITING_RELEASE_APPROVAL)
            self._event(task, "pull_request_publish_failed", "GitHub PR 发布失败", {"error": str(error)})
            raise ValueError(str(error)) from error

    def refresh_pull_request(self, task_id: str) -> Task:
        """从 GitHub 刷新 Pull Request 状态并同步外部合入结果。

        Args:
            task_id: 已关联 GitHub PR 的任务 ID。

        Returns:
            PR 元数据和任务状态已刷新的任务。
        """
        task = self._require_task(task_id)
        if task.remote_pull_request is None or self.github_delivery is None:
            raise ValueError("Task has no GitHub pull request")
        task.remote_pull_request = self.github_delivery.refresh(task.remote_pull_request)
        if (
            task.remote_pull_request.state == "merged"
            and task.status == TaskStatus.WAITING_MERGE_APPROVAL
        ):
            self._transition(task, TaskStatus.MERGING)
            self._transition(task, TaskStatus.MERGED)
            self._event(task, "pull_request_merged", "检测到 GitHub PR 已在外部合入")
        else:
            self.store.save_task(task)
        return task

    def sync_pull_request_comments(self, task_id: str) -> Task:
        """同步 GitHub Pull Request 的会话评论和行级审查意见。

        Args:
            task_id: 已关联且尚未合入 GitHub PR 的任务 ID。

        Returns:
            评论列表及处理状态已更新的任务。
        """
        task = self._require_task(task_id)
        if task.remote_pull_request is None or self.github_delivery is None:
            raise ValueError("Task has no GitHub pull request")
        if task.remote_pull_request.state == "merged" or task.status == TaskStatus.MERGED:
            raise ValueError("Merged pull requests no longer accept review fixes")
        incoming = self.github_delivery.list_review_comments(task.remote_pull_request)
        existing = {
            str(item.get("key")): item
            for item in task.metadata.get("github_review_comments", [])
            if item.get("key")
        }
        synchronized: list[dict] = []
        new_count = 0
        for item in incoming:
            previous = existing.get(item["key"])
            if previous:
                item.update({
                    "status": previous.get("status", "pending"),
                    "job_id": previous.get("job_id"),
                    "result": previous.get("result"),
                    "reply_status": previous.get("reply_status"),
                })
            else:
                item["status"] = "pending"
                item["job_id"] = None
                item["result"] = None
                item["reply_status"] = None
                new_count += 1
            synchronized.append(item)
        task.metadata["github_review_comments"] = synchronized
        task.metadata["github_comments_synced_at"] = datetime.now(UTC).isoformat()
        task.updated_at = datetime.now(UTC)
        self.store.save_task(task)
        if new_count:
            self._event(
                task,
                "github_comments_synced",
                f"已同步 GitHub PR 评论，新增 {new_count} 条",
                {"total": len(synchronized), "new": new_count},
            )
        return task

    def update_github_comment_status(
        self,
        task_id: str,
        comment_keys: list[str],
        status: str,
        *,
        job_id: str | None = None,
        result: str | None = None,
    ) -> Task:
        """批量更新已同步 GitHub 评论的本地处理状态。

        Args:
            task_id: 评论所属任务 ID。
            comment_keys: 需要更新的评论唯一键列表。
            status: 新处理状态，例如 ``queued``、``resolved`` 或 ``failed``。
            job_id: 可选的评论修复 Job ID。
            result: 可选的 Agent 处理结果说明。

        Returns:
            评论状态已持久化的任务。

        Raises:
            ValueError: 一个或多个评论键已不存在。
        """
        task = self._require_task(task_id)
        selected = set(comment_keys)
        matched = 0
        for comment in task.metadata.get("github_review_comments", []):
            if comment.get("key") not in selected:
                continue
            comment["status"] = status
            if job_id is not None:
                comment["job_id"] = job_id
            if result is not None:
                comment["result"] = result
            matched += 1
        if matched != len(selected):
            raise ValueError("One or more GitHub comments are no longer available")
        task.updated_at = datetime.now(UTC)
        return self.store.save_task(task)

    def complete_github_comment_feedback(
        self,
        task_id: str,
        comment_keys: list[str],
        success: bool,
        detail: str,
    ) -> Task:
        """完成 GitHub 评论修复，并将处理结果回复到远程 PR 页面。

        Args:
            task_id: 评论所属任务 ID。
            comment_keys: 本轮已处理的评论唯一键列表。
            success: Agent 是否成功完成评论要求。
            detail: 修改文件、测试和提交等结果说明，或失败原因。

        Returns:
            评论状态和远程回复状态已更新的任务。
        """
        task = self.update_github_comment_status(
            task_id,
            comment_keys,
            "resolved" if success else "failed",
            result=detail,
        )
        if task.remote_pull_request is None or self.github_delivery is None:
            return task
        selected = set(comment_keys)
        reply = (
            "✅ Agent 已处理这条意见并更新当前 PR。\n\n"
            if success
            else "⚠️ Agent 未能完成这条意见。\n\n"
        ) + detail
        for comment in task.metadata.get("github_review_comments", []):
            if comment.get("key") not in selected:
                continue
            try:
                self.github_delivery.reply_to_review_comment(
                    task.remote_pull_request, comment, reply
                )
                comment["reply_status"] = "replied"
            except Exception as error:
                comment["reply_status"] = "failed"
                comment["reply_error"] = str(error)[:500]
        task.updated_at = datetime.now(UTC)
        self.store.save_task(task)
        self._event(
            task,
            "github_comments_processed",
            "GitHub PR 评论处理完成" if success else "GitHub PR 评论处理失败",
            {"comment_keys": comment_keys, "success": success},
        )
        return task

    def merge_pull_request(self, task_id: str, actor: str, comment: str = "") -> Task:
        """在用户最终确认后合入 GitHub Pull Request。

        Args:
            task_id: 等待 PR 合入审批的任务 ID。
            actor: 确认合入的用户标识。
            comment: 可选的合入说明。

        Returns:
            已记录合入 SHA、状态为 ``merged`` 的任务。

        Raises:
            ValueError: 任务未就绪、GitHub 未配置或远程合入失败。
        """
        task = self._require_task(task_id)
        if task.status != TaskStatus.WAITING_MERGE_APPROVAL or task.remote_pull_request is None:
            raise ValueError("Task is not waiting for pull request merge approval")
        if self.github_delivery is None:
            raise ValueError("GitHub delivery is not configured")
        self._transition(task, TaskStatus.MERGING)
        self._event(task, "pull_request_merge_started", f"{actor} 已确认合入 GitHub PR", {"comment": comment})
        try:
            task.remote_pull_request = self.github_delivery.merge(task.remote_pull_request)
            task.error = None
            self._transition(task, TaskStatus.MERGED)
            self._event(
                task,
                "pull_request_merged",
                f"GitHub PR #{task.remote_pull_request.number} 已合入",
                {"merged_sha": task.remote_pull_request.merged_sha},
            )
            return task
        except Exception as error:
            task.error = str(error)
            self._transition(task, TaskStatus.WAITING_MERGE_APPROVAL)
            self._event(task, "pull_request_merge_failed", "GitHub PR 合入失败", {"error": str(error)})
            raise ValueError(str(error)) from error

    def _finish_review_pipeline(self, task: Task, repository_config, checkpoint_handler=None) -> Task:
        self._prepare_review_pipeline(task)
        return self._review_loop(task, repository_config, checkpoint_handler=checkpoint_handler)

    def _prepare_review_pipeline(self, task: Task) -> Task:
        self._transition(task, TaskStatus.GENERATING_MR)
        task.merge_request = self._write_merge_request(task)
        self._event(
            task,
            "merge_request_generated",
            "已根据最终 Diff、验收标准和测试结果生成 MR 草稿",
            {"changed_files": task.merge_request.changed_files},
        )
        return task

    def _write_merge_request(self, task: Task):
        if task.design_reference and self.ui_acceptance_service:
            task.ui_acceptance = self.ui_acceptance_service.validate(task)
            self.store.save_task(task)
            self._event(
                task,
                "ui_acceptance_completed",
                task.ui_acceptance.summary if task.ui_acceptance else "UI 验收未执行",
                {
                    "status": task.ui_acceptance.status if task.ui_acceptance else "not_run",
                    "similarity_score": (
                        task.ui_acceptance.similarity_score if task.ui_acceptance else None
                    ),
                },
            )
        return self.merge_request_builder.write(task)

    def run_ui_acceptance(self, task_id: str) -> Task:
        """手动重新执行任务的 UI 设计验收并更新 MR 草稿。

        Args:
            task_id: 已绑定 Figma 设计基线的任务 ID。

        Returns:
            已保存最新 UI 验收报告的任务。

        Raises:
            ValueError: 任务没有设计基线或 UI 验收服务未配置。
        """
        task = self._require_task(task_id)
        if task.design_reference is None:
            raise ValueError("Task has no Figma design reference")
        if self.ui_acceptance_service is None:
            raise ValueError("UI acceptance service is not configured")
        task.ui_acceptance = self.ui_acceptance_service.validate(task)
        self.store.save_task(task)
        self._event(
            task,
            "ui_acceptance_completed",
            task.ui_acceptance.summary,
            {
                "status": task.ui_acceptance.status,
                "similarity_score": task.ui_acceptance.similarity_score,
                "manual": True,
            },
        )
        if task.merge_request:
            task.merge_request = self.merge_request_builder.write(task)
            self.store.save_task(task)
        return task

    def _review_loop(self, task: Task, repository_config, checkpoint_handler=None) -> Task:
        cycle_start = int(task.metadata.get("review_cycle_start", 0))

        def review_node(_state: dict) -> dict:
            self._transition(task, TaskStatus.REVIEWING)
            review = self.code_review_agent.review(
                task,
                len(task.reviews) + 1,
                baseline_deletion_candidates=self._baseline_deletion_candidates(task),
            )
            task.reviews.append(review)
            self._event(
                task,
                "code_review_completed",
                f"Code Review 第 {review.round} 轮：{review.decision}",
                {"review": review.model_dump(mode="json")},
            )
            if checkpoint_handler and checkpoint_handler(
                "review_round_saved",
                "finish_review" if review.decision == "approved" else "repair_review",
                {"review": review.model_dump(mode="json")},
            ):
                return {"route": "done", "review": review}
            if review.decision == "approved":
                task.error = None
                self._transition(task, TaskStatus.REVIEW_APPROVED)
                self._event(task, "review_approved", "Code Review 已通过")
                self._transition(task, TaskStatus.WAITING_RELEASE_APPROVAL)
                self._event(task, "release_approval_required", "MR 和 Code Review 已就绪，等待发布审批")
                return {"route": "done", "review": review}

            self._transition(task, TaskStatus.CHANGES_REQUESTED)
            self._event(task, "review_changes_requested", "Reviewer 提出阻塞问题，准备自动修复")
            if len(task.reviews) - cycle_start >= 3:
                task.error = "Automatic review repair exhausted after 3 review rounds"
                self._event(task, "review_repair_exhausted", "已达到三轮 Code Review 上限，等待人工处理")
                return {"route": "done", "review": review}
            if repository_config.execution_mode != "plan_only" or not self.code_development_agent.model_gateway.enabled:
                task.error = "Blocking review findings require a configured Developer Agent"
                self._event(task, "review_repair_unavailable", "当前执行模式无法自动修复 CR 问题")
                return {"route": "done", "review": review}

            return {
                "route": "repair",
                "review": review,
                "feedback": self._review_feedback(review),
            }

        def repair_node(state: dict) -> dict:
            self._transition(task, TaskStatus.REVIEW_REPAIRING)
            review = state["review"]
            restored = self._restore_review_baseline_deletions(task, review)
            feedback = self._review_feedback(review, restored)
            outcome = self.code_development_agent.run(
                task,
                Path(task.workspace),
                dependency_repository=Path(repository_config.local_path),
                review_feedback=feedback,
                checkpoint_handler=checkpoint_handler,
            )
            self._append_attempts(task, outcome.attempts)
            if outcome.kind == "paused":
                if outcome.result is not None:
                    task.result = outcome.result
                self.store.save_task(task)
                return {"route": "done"}
            if outcome.kind == "risk_approval":
                task.pending_proposal = outcome.pending_proposal
                task.metadata["risk_reasons"] = outcome.risk_reasons
                task.metadata["review_repair_pending"] = True
                task.metadata["pending_repair_feedback"] = feedback
                self._transition(task, TaskStatus.WAITING_RISK_APPROVAL)
                self._event(task, "risk_approval_required", "CR 修复涉及高风险文件，等待人工审批")
                return {"route": "done"}
            if outcome.kind != "success":
                task.error = outcome.error or "Review repair failed"
                self._transition(task, TaskStatus.CHANGES_REQUESTED)
                self._event(task, "review_repair_failed", "CR 自动修复失败", {"error": task.error})
                return {"route": "done"}

            task.result = outcome.result
            task.error = None
            self._transition(task, TaskStatus.GENERATING_MR)
            task.merge_request = self._write_merge_request(task)
            self._event(task, "merge_request_regenerated", "CR 修复测试通过，已重新生成 MR 草稿")
            return {"route": "review"}

        return self.code_review_agent.run_loop(task, review_node, repair_node)

    def _append_attempts(self, task: Task, attempts, label: str = "CR 修复尝试") -> None:
        offset = len(task.development_attempts)
        for index, attempt in enumerate(attempts, 1):
            attempt.step_attempt = attempt.step_attempt or attempt.attempt
            attempt.attempt = offset + index
            attempt.step_title = attempt.step_title or label
            task.development_attempts.append(attempt)
            self._event(
                task,
                "development_attempt",
                f"{label} #{attempt.attempt}：测试退出码 {attempt.exit_code}",
                {"attempt": attempt.model_dump(mode="json")},
            )

    @staticmethod
    def _workspace_snapshot(task: Task) -> dict[str, str | None]:
        if not task.workspace or not task.technical_plan:
            return {}
        workspace = Path(task.workspace).resolve()
        snapshot: dict[str, str | None] = {}
        for relative in task.technical_plan.affected_files[:40]:
            target = (workspace / relative).resolve()
            if workspace not in target.parents:
                continue
            snapshot[relative] = (
                target.read_text(encoding="utf-8", errors="replace")
                if target.is_file() else None
            )
        return snapshot

    @staticmethod
    def _is_feedback_rollback(feedback: str) -> bool:
        compact = "".join(feedback.lower().split()).strip("。！!，,")
        actions = ("取消", "撤销", "回退", "还原", "恢复")
        targets = (
            "上一次修改", "上一轮修改", "刚刚的修改", "刚才的修改",
            "本轮修改", "这次修改", "之前的修改",
        )
        return any(action in compact for action in actions) and any(
            target in compact for target in targets
        )

    def _feedback_snapshot_dir(self, task: Task) -> Path:
        if not task.workspace:
            raise ValueError("Task workspace is not available")
        return Path(task.workspace).resolve().parent / "feedback_snapshots"

    def _save_feedback_snapshot(
        self, task: Task, snapshot: dict[str, str | None]
    ) -> str:
        snapshot_id = uuid4().hex[:16]
        directory = self._feedback_snapshot_dir(task)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{snapshot_id}.json"
        target.write_text(
            json.dumps({"files": snapshot}, ensure_ascii=False), encoding="utf-8"
        )
        return snapshot_id

    def _load_feedback_snapshot(
        self, task: Task, snapshot_id: str
    ) -> dict[str, str | None]:
        target = self._feedback_snapshot_dir(task) / f"{snapshot_id}.json"
        if not target.is_file():
            raise ValueError("上一轮修改没有可恢复的工作区快照")
        payload = json.loads(target.read_text(encoding="utf-8"))
        files = payload.get("files", {})
        if not isinstance(files, dict):
            raise ValueError("上一轮修改的工作区快照无效")
        return {
            str(path): content if isinstance(content, str) else None
            for path, content in files.items()
        }

    def _restore_feedback_snapshot(
        self, task: Task, snapshot: dict[str, str | None]
    ) -> None:
        if not task.workspace:
            raise ValueError("Task workspace is not available")
        workspace = Path(task.workspace).resolve()
        for relative, content in snapshot.items():
            target = (workspace / relative).resolve()
            if workspace not in target.parents:
                raise ValueError(f"Snapshot path is outside task workspace: {relative}")
            if content is None:
                if target.is_file():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    @staticmethod
    def _reverse_legacy_feedback_diff(task: Task, diff: str) -> None:
        if not task.workspace or not diff.strip():
            raise ValueError("上一轮修改没有可恢复的工作区快照")
        command = ["git", "apply", "--reverse", "--whitespace=nowarn", "-"]
        check = subprocess.run(
            command[:2] + ["--check"] + command[2:],
            cwd=task.workspace,
            input=diff,
            text=True,
            capture_output=True,
            check=False,
        )
        if check.returncode != 0:
            raise ValueError("旧任务的上一轮 Diff 已与当前代码不一致，无法安全撤销")
        applied = subprocess.run(
            command,
            cwd=task.workspace,
            input=diff,
            text=True,
            capture_output=True,
            check=False,
        )
        if applied.returncode != 0:
            raise ValueError(f"撤销上一轮 Diff 失败：{applied.stderr.strip()}")

    def _rollback_previous_feedback(
        self,
        task: Task,
        feedback_round: dict,
        before_snapshot: dict[str, str | None],
        actor: str,
    ) -> Task:
        rounds = task.metadata.get("user_feedback_rounds", [])
        candidates = [
            item for item in rounds[:-1]
            if (item.get("snapshot_id") or item.get("diff"))
            and item.get("operation") != "rollback"
            and not item.get("rolled_back_at")
        ]
        if not candidates:
            feedback_round.update({
                "status": "failed",
                "agent_message": "没有找到可撤销的上一轮修改快照。",
                "completed_at": datetime.now(UTC).isoformat(),
                "operation": "rollback",
            })
            self.store.save_task(task)
            raise ValueError("没有找到可撤销的上一轮修改快照")

        target_round = candidates[-1]
        snapshot_id = target_round.get("snapshot_id")
        if snapshot_id:
            snapshot = self._load_feedback_snapshot(task, str(snapshot_id))
            self._restore_feedback_snapshot(task, snapshot)
        else:
            self._reverse_legacy_feedback_diff(task, str(target_round.get("diff", "")))
        rolled_back_at = datetime.now(UTC).isoformat()
        target_round["rolled_back_at"] = rolled_back_at
        target_round["rolled_back_by"] = actor
        feedback_round["operation"] = "rollback"
        task.error = None
        task.pending_proposal = None
        task.merge_request = None
        task.metadata.pop("pending_repair_feedback", None)
        task.metadata.pop("review_repair_pending", None)
        task.metadata["validation_stale"] = True
        if task.status in {
            TaskStatus.WAITING_RELEASE_APPROVAL,
            TaskStatus.WAITING_MERGE_APPROVAL,
        }:
            self._transition(task, TaskStatus.CHANGES_REQUESTED)
        self._finish_user_feedback_round(
            task,
            feedback_round,
            before_snapshot,
            TaskStatus.CHANGES_REQUESTED.value,
            "已恢复到上一轮修改开始前的工作区；未触发模型、测试或旧需求的自动修复，可以继续提出新的修改意见。",
        )
        self._event(
            task,
            "user_feedback_rolled_back",
            "已撤销上一轮用户反馈产生的代码修改",
            {"snapshot_id": snapshot_id, "actor": actor},
        )
        return task

    def current_workspace_files(self, task: Task) -> list[dict]:
        """Return baseline/current file pairs used by the Monaco diff editor."""
        if not task.workspace:
            return []
        workspace = Path(task.workspace)
        if not workspace.is_dir():
            return []
        baseline = task.repository_analysis.head_sha if task.repository_analysis else None
        try:
            return self.workspace_manager.diff_files_from_baseline(workspace, baseline)
        except (OSError, ValueError, subprocess.SubprocessError):
            return []

    def _finish_user_feedback_round(
        self,
        task: Task,
        feedback_round: dict,
        before_snapshot: dict[str, str | None],
        status: str,
        message: str,
        review_decision: str | None = None,
    ) -> None:
        after_snapshot = self._workspace_snapshot(task)
        changed_files: list[str] = []
        diff_parts: list[str] = []
        for path in sorted(set(before_snapshot) | set(after_snapshot)):
            before = before_snapshot.get(path) or ""
            after = after_snapshot.get(path) or ""
            if before == after:
                continue
            changed_files.append(path)
            body = "".join(difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            ))
            diff_parts.append(f"diff --git a/{path} b/{path}\n{body}")
        feedback_round.update({
            "status": status,
            "agent_message": message,
            "changed_files": changed_files,
            "diff": "\n".join(diff_parts)[-100_000:],
            "completed_at": datetime.now(UTC).isoformat(),
            "review_decision": review_decision,
            "test_exit_code": task.result.exit_code if task.result else None,
        })
        task.metadata["user_feedback_rounds"] = list(
            task.metadata.get("user_feedback_rounds", [])
        )
        task.updated_at = datetime.now(UTC)
        self.store.save_task(task)

    def _baseline_deletion_candidates(self, task: Task) -> list[dict]:
        if not task.workspace or not task.repository_analysis or not task.technical_plan:
            return []
        return self.workspace_manager.baseline_deletion_candidates(
            Path(task.workspace),
            task.repository_analysis.head_sha,
            task.technical_plan.affected_files,
        )

    def _restore_review_baseline_deletions(self, task: Task, review) -> list[dict]:
        candidate_ids = list(dict.fromkeys(review.baseline_restore_ids))
        if not candidate_ids:
            return []
        if not task.workspace or not task.repository_analysis or not task.technical_plan:
            raise ValueError("Task baseline is unavailable for deterministic review repair")
        restored = self.workspace_manager.restore_baseline_deletions(
            Path(task.workspace),
            task.repository_analysis.head_sha,
            task.technical_plan.affected_files,
            candidate_ids,
        )
        self._event(
            task,
            "baseline_content_restored",
            f"已从 Git 基线确定性恢复 {len(restored)} 个误删片段",
            {
                "review_round": review.round,
                "restorations": [
                    {
                        "id": item["id"],
                        "file": item["file"],
                        "baseline_start_line": item["baseline_start_line"],
                        "baseline_end_line": item["baseline_end_line"],
                    }
                    for item in restored
                ],
            },
        )
        return restored

    @staticmethod
    def _review_feedback(review, restored: list[dict] | None = None) -> str:
        lines = [
            f"- [{item.severity}] {item.file or 'general'}"
            f"{':' + str(item.line) if item.line else ''}: {item.message} {item.suggestion}"
            for item in review.findings if item.blocking
        ]
        restoration_note = ""
        if restored:
            restored_ranges = ", ".join(
                f"{item['file']}:{item['baseline_start_line']}-{item['baseline_end_line']}"
                for item in restored
            )
            restoration_note = (
                "\n系统已从任务 Git 基线原样恢复以下误删片段："
                f"{restored_ranges}。不要重新编造这些内容；在恢复后的真实文件上仅完成需求授权的修改。"
            )
        return "Code Review 阻塞问题，必须全部修复：\n" + "\n".join(lines) + restoration_note

    def resume_from_checkpoint(
        self,
        task_id: str,
        job_id: str,
        should_pause: Callable[[], bool] | None = None,
    ) -> Task:
        """校验最近持久化检查点并从对应工作流阶段继续执行。

        Args:
            task_id: 需要恢复的任务 ID。
            job_id: 承载本次恢复执行的后台 Job ID。
            should_pause: 可选暂停判定函数，允许恢复后再次在安全检查点停止。

        Returns:
            从检查点继续开发、测试或审查后的最新任务。

        Raises:
            ValueError: 检查点缺失、已漂移或其阶段无法恢复。
        """
        task = self._require_task(task_id)
        checkpoint = self.latest_workflow_checkpoint(task_id)
        if checkpoint is None:
            raise ValueError("No durable checkpoint is available for this task")
        self._validate_checkpoint(task, checkpoint)
        task.metadata.pop("paused_checkpoint", None)
        self.store.save_task(task)
        repository = self.store.get_repository(task.repository_id or "")
        if repository is None:
            raise ValueError("Repository not found")
        handler = self._checkpoint_handler(task, job_id, should_pause)
        self._event(
            task,
            "checkpoint_resume_started",
            f"从检查点 {checkpoint.stage} 继续执行",
            {"checkpoint_id": checkpoint.id, "next_action": checkpoint.next_action},
        )

        if checkpoint.stage == "plan_approved":
            return self._run_real_development(task, repository, checkpoint_handler=handler)

        if checkpoint.payload.get("development_step_index") is not None:
            if repository.execution_mode != "plan_only":
                raise ValueError("Step checkpoint is only valid for a model-driven repository")
            return self._run_real_development(
                task,
                repository,
                checkpoint_handler=handler,
                resume_stage=checkpoint.stage,
                resume_payload=checkpoint.payload,
            )

        if checkpoint.stage in {"proposal_ready", "patch_applied"}:
            if repository.execution_mode != "plan_only":
                raise ValueError("This checkpoint is only valid for a model-driven repository")
            if task.status == TaskStatus.REVIEW_REPAIRING:
                return self._resume_review_developer_checkpoint(
                    task, repository, checkpoint, handler
                )
            return self._run_real_development(
                task,
                repository,
                checkpoint_handler=handler,
                resume_stage=checkpoint.stage,
                resume_payload=checkpoint.payload,
            )

        if checkpoint.stage == "test_result_saved":
            if checkpoint.payload.get("success"):
                task.result = ExecutionResult.model_validate(checkpoint.payload["result"])
                if task.status == TaskStatus.REVIEW_REPAIRING:
                    task.error = None
                    self._transition(task, TaskStatus.GENERATING_MR)
                    task.merge_request = self._write_merge_request(task)
                    self._event(
                        task,
                        "merge_request_regenerated",
                        "从测试检查点恢复 CR 修复并重新生成 MR",
                    )
                    return self._review_loop(
                        task, repository, checkpoint_handler=handler
                    )
                if task.status == TaskStatus.DEVELOPING:
                    self._transition(task, TaskStatus.TESTING)
                self._transition(task, TaskStatus.CHANGE_READY)
                self._event(task, "change_ready", "从测试检查点恢复，测试结果有效")
                return self._finish_review_pipeline(
                    task, repository, checkpoint_handler=handler
                )
            if task.status == TaskStatus.REVIEW_REPAIRING:
                return self._resume_review_developer_checkpoint(
                    task, repository, checkpoint, handler
                )
            return self._run_real_development(
                task,
                repository,
                checkpoint_handler=handler,
                resume_stage=checkpoint.stage,
                resume_payload=checkpoint.payload,
            )

        if checkpoint.stage == "review_round_saved":
            review = task.reviews[-1]
            if review.decision == "approved":
                self._transition(task, TaskStatus.REVIEW_APPROVED)
                self._event(task, "review_approved", "从审查检查点恢复，Code Review 已通过")
                self._transition(task, TaskStatus.WAITING_RELEASE_APPROVAL)
                self._event(task, "release_approval_required", "MR 和 Code Review 已就绪，等待发布审批")
                return task
            return self._resume_review_repair(task, repository, review, handler)

        raise ValueError(f"Unsupported checkpoint stage: {checkpoint.stage}")

    def _resume_review_developer_checkpoint(self, task, repository, checkpoint, handler) -> Task:
        outcome = self.code_development_agent.run(
            task,
            Path(task.workspace),
            dependency_repository=Path(repository.local_path),
            review_feedback=str(checkpoint.payload.get("review_feedback", "")),
            checkpoint_handler=handler,
            resume_stage=checkpoint.stage,
            resume_payload=checkpoint.payload,
        )
        self._append_attempts(task, outcome.attempts)
        if outcome.kind == "paused":
            if outcome.result is not None:
                task.result = outcome.result
            self.store.save_task(task)
            return task
        if outcome.kind != "success":
            task.error = outcome.error or "Review repair failed"
            self._transition(task, TaskStatus.CHANGES_REQUESTED)
            return task
        task.result = outcome.result
        task.error = None
        self._transition(task, TaskStatus.GENERATING_MR)
        task.merge_request = self._write_merge_request(task)
        self._event(task, "merge_request_regenerated", "从检查点恢复 CR 修复并重新生成 MR")
        return self._review_loop(task, repository, checkpoint_handler=handler)

    def _resume_review_repair(self, task: Task, repository, review, handler) -> Task:
        self._transition(task, TaskStatus.CHANGES_REQUESTED)
        self._event(task, "review_changes_requested", "从审查检查点恢复，准备自动修复")
        if len(task.reviews) >= 3:
            task.error = "Automatic review repair exhausted after 3 review rounds"
            return task
        if repository.execution_mode != "plan_only" or not self.code_development_agent.model_gateway.enabled:
            task.error = "Blocking review findings require a configured Developer Agent"
            return task
        self._transition(task, TaskStatus.REVIEW_REPAIRING)
        restored = self._restore_review_baseline_deletions(task, review)
        outcome = self.code_development_agent.run(
            task,
            Path(task.workspace),
            dependency_repository=Path(repository.local_path),
            review_feedback=self._review_feedback(review, restored),
            checkpoint_handler=handler,
        )
        self._append_attempts(task, outcome.attempts)
        if outcome.kind == "paused":
            if outcome.result is not None:
                task.result = outcome.result
            self.store.save_task(task)
            return task
        if outcome.kind != "success":
            task.error = outcome.error or "Review repair failed"
            self._transition(task, TaskStatus.CHANGES_REQUESTED)
            return task
        task.result = outcome.result
        task.error = None
        self._transition(task, TaskStatus.GENERATING_MR)
        task.merge_request = self._write_merge_request(task)
        self._event(task, "merge_request_regenerated", "CR 修复测试通过，已重新生成 MR 草稿")
        return self._review_loop(task, repository, checkpoint_handler=handler)

    def _checkpoint_handler(
        self,
        task: Task,
        job_id: str | None,
        should_pause: Callable[[], bool] | None,
    ):
        if not job_id:
            return None

        def save(stage: str, next_action: str, payload: dict) -> bool:
            plan_json = task.technical_plan.model_dump_json() if task.technical_plan else ""
            diff = ""
            if task.workspace and Path(task.workspace).is_dir():
                diff = self.workspace_manager.diff(Path(task.workspace))
            checkpoint = TaskCheckpoint(
                id=uuid4().hex[:16],
                task_id=task.id,
                job_id=job_id,
                stage=stage,
                next_action=next_action,
                workspace=task.workspace,
                baseline_sha=(task.repository_analysis.head_sha if task.repository_analysis else None),
                plan_hash=hashlib.sha256(plan_json.encode("utf-8")).hexdigest(),
                diff_hash=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                payload=payload,
            )
            # LangGraph is the execution-state source. The legacy checkpoint
            # row is retained only as an API/history projection during cut-over.
            self.workflow_state.record_checkpoint(task, checkpoint)
            self.store.save_checkpoint(checkpoint)
            self._event(
                task,
                "checkpoint_saved",
                f"已保存检查点：{stage}",
                {"checkpoint_id": checkpoint.id, "next_action": next_action},
            )
            pause = bool(should_pause and should_pause())
            if pause:
                task.metadata["paused_checkpoint"] = {
                    "checkpoint_id": checkpoint.id,
                    "job_id": job_id,
                    "stage": stage,
                }
                self.store.save_task(task)
                self._event(
                    task,
                    "execution_paused",
                    f"已在安全检查点暂停：{stage}",
                    {"checkpoint_id": checkpoint.id, "job_id": job_id},
                )
            return pause

        return save

    def _validate_checkpoint(self, task: Task, checkpoint: TaskCheckpoint) -> None:
        plan_json = task.technical_plan.model_dump_json() if task.technical_plan else ""
        plan_hash = hashlib.sha256(plan_json.encode("utf-8")).hexdigest()
        if plan_hash != checkpoint.plan_hash:
            raise ValueError("Technical plan changed after the checkpoint")
        baseline = task.repository_analysis.head_sha if task.repository_analysis else None
        if baseline != checkpoint.baseline_sha:
            raise ValueError("Repository baseline changed after the checkpoint")
        if checkpoint.workspace:
            workspace = Path(checkpoint.workspace)
            if not workspace.is_dir() or task.workspace != checkpoint.workspace:
                raise ValueError("Checkpoint workspace is missing or no longer belongs to the task")
            diff = self.workspace_manager.diff(workspace)
            diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
            if diff_hash != checkpoint.diff_hash:
                raise ValueError("Workspace diff changed after the checkpoint")

    def dry_run_checkpoint(self, task_id: str, checkpoint_id: str) -> ReplayResult:
        """只读验证检查点是否仍可恢复，不修改任务或工作区。

        Args:
            task_id: 检查点所属任务 ID。
            checkpoint_id: 待验证的检查点 ID。

        Returns:
            包含有效性、下一动作、检查项和警告的演练结果。

        Raises:
            KeyError: 检查点不存在或不属于指定任务。
            ValueError: Trace 服务未配置。
        """
        task = self._require_task(task_id)
        checkpoint = self.store.get_checkpoint(checkpoint_id)
        if checkpoint is None or checkpoint.task_id != task_id:
            raise KeyError(checkpoint_id)
        if self.tracer is None:
            raise ValueError("Tracing is not configured")
        with self.tracer.trace(
            "checkpoint.dry_run", task_id=task_id, kind="replay",
            metadata={"checkpoint_id": checkpoint_id, "stage": checkpoint.stage},
        ) as trace:
            checks = ["检查点归属当前任务"]
            warnings: list[str] = []
            try:
                with self.tracer.span("checkpoint.validate", kind="replay"):
                    self._validate_checkpoint(task, checkpoint)
                checks.extend(["技术方案哈希一致", "仓库基线一致"])
                if checkpoint.workspace:
                    checks.extend(["隔离工作区存在", "工作区 Diff 未漂移"])
                valid = True
            except ValueError as error:
                valid = False
                warnings.append(str(error))
            return ReplayResult(
                task_id=task_id, checkpoint_id=checkpoint.id, stage=checkpoint.stage,
                valid=valid, next_action=checkpoint.next_action,
                checks=checks, warnings=warnings, trace_id=trace.id,
            )

    def revise(self, task_id: str, actor: str, feedback: str) -> Task:
        """根据人工补充意见重新生成需求分析和技术方案。

        Args:
            task_id: 等待需求输入或技术方案审批的任务 ID。
            actor: 提出修改意见的用户标识。
            feedback: 需要纳入规划的新信息或方案调整意见。

        Returns:
            已重新规划、等待再次审批的任务。
        """
        task = self._require_task(task_id)
        if task.status not in {TaskStatus.WAITING_REQUIREMENT_APPROVAL, TaskStatus.WAITING_REQUIREMENT_INPUT}:
            raise ValueError("Only a task waiting for input or approval can be revised")
        self._transition(task, TaskStatus.REQUIREMENT_ANALYSIS)
        task.metadata.setdefault("plan_feedback", []).append({"actor": actor, "feedback": feedback})
        enriched_requirement = f"{task.requirement}\n\n用户补充意见：{feedback}"
        if task.repository_analysis is None:
            repository = self.store.get_repository(task.repository_id or "")
            if repository is None:
                raise ValueError("Repository not found")
            task.repository_analysis = self.repository_analyzer.analyze(Path(repository.local_path), enriched_requirement)
        task.analysis, task.technical_plan = self.requirement_planning_agent.plan(
            task.title, enriched_requirement, task.repository_analysis,
            task.design_reference.context if task.design_reference else "",
        )
        self._transition(task, TaskStatus.WAITING_REQUIREMENT_APPROVAL)
        self._event(task, "plan_revised", f"已根据 {actor} 的意见重新生成方案", {"feedback": feedback})
        return task

    def _transition(self, task: Task, target: TaskStatus) -> None:
        checkpoint = self.workflow_state.latest_checkpoint(task)
        self.workflow_state.transition(task, target, checkpoint=checkpoint)
        task.status = target
        task.updated_at = datetime.now(UTC)
        self.store.save_task(task)

    def latest_workflow_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
        """读取任务在 LangGraph 状态存储中的最新工作流检查点。

        Args:
            task_id: 任务唯一标识。

        Returns:
            最新工作流检查点；尚未保存时返回 ``None``。
        """
        task = self._require_task(task_id)
        return self.workflow_state.latest_checkpoint(task)

    def _event(self, task: Task, event_type: str, message: str, payload: dict | None = None) -> None:
        self.store.add_event(
            TaskEvent(task_id=task.id, event_type=event_type, message=message, payload=payload or {})
        )
        self.store.save_task(task)

    def _require_task(self, task_id: str) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    @staticmethod
    def _build_mr_description(task: Task, command: list[str], exit_code: int, output: str) -> str:
        criteria = "\n".join(f"- [x] {item}" for item in task.analysis.acceptance_criteria)
        tests = " ".join(command)
        return f"""## 变更说明

为示例任务服务增加 `priority` 字段，支持 `low`、`medium`、`high`，默认值为 `medium`。

## 验收标准

{criteria}

## 测试

- 命令：`{tests}`
- 退出码：`{exit_code}`
- 结果：{'通过' if exit_code == 0 else '失败'}

<details>
<summary>测试输出</summary>

```text
{output}
```
</details>

## 风险与回滚

- 任务返回对象增加了一个字段，依赖精确字段集合的调用方需要关注。
- 回滚方式：撤销本 MR，不涉及数据迁移。
"""
