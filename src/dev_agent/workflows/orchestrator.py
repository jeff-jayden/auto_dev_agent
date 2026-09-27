from __future__ import annotations

import hashlib
import difflib
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dev_agent.agents import (
    CodeReviewerAgent,
    DemoDeveloperAgent,
    GenericDeveloperAgent,
    LocalPlanningAgent,
    MergeRequestWriter,
)
from dev_agent.domain.models import (
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
from dev_agent.domain.state_machine import ensure_transition
from dev_agent.infrastructure.store import SQLiteTaskStore
from dev_agent.repository import RepositoryAnalyzer
from dev_agent.sandbox import WorkspaceManager
from dev_agent.scm import GitHubDeliveryService
from dev_agent.ui_validation import FigmaMCPClient, UIAcceptanceService


class TaskOrchestrator:
    def __init__(
        self,
        store: SQLiteTaskStore,
        planner: LocalPlanningAgent,
        developer: DemoDeveloperAgent,
        workspace_manager: WorkspaceManager,
        repository_analyzer: RepositoryAnalyzer,
        generic_developer: GenericDeveloperAgent,
        mr_writer: MergeRequestWriter,
        code_reviewer: CodeReviewerAgent,
        github_delivery: GitHubDeliveryService | None = None,
        tracer=None,
        figma_client: FigmaMCPClient | None = None,
        ui_acceptance_service: UIAcceptanceService | None = None,
    ):
        self.store = store
        self.planner = planner
        self.developer = developer
        self.workspace_manager = workspace_manager
        self.repository_analyzer = repository_analyzer
        self.generic_developer = generic_developer
        self.mr_writer = mr_writer
        self.code_reviewer = code_reviewer
        self.github_delivery = github_delivery
        self.tracer = tracer
        self.figma_client = figma_client
        self.ui_acceptance_service = ui_acceptance_service

    def create_task(
        self,
        title: str,
        requirement: str,
        repository_id: str = "demo",
        *,
        figma_url: str = "",
        preview_url: str = "",
        viewport_width: int = 1440,
        viewport_height: int = 900,
    ) -> Task:
        if self.tracer is not None:
            with self.tracer.trace(
                "task.create", kind="planning", metadata={"repository_id": repository_id}
            ) as trace:
                task = self._create_task(
                    title, requirement, repository_id,
                    figma_url=figma_url, preview_url=preview_url,
                    viewport_width=viewport_width, viewport_height=viewport_height,
                )
                trace.task_id = task.id
                self.store.save_trace(trace)
                return task
        return self._create_task(
            title, requirement, repository_id,
            figma_url=figma_url, preview_url=preview_url,
            viewport_width=viewport_width, viewport_height=viewport_height,
        )

    def rerun_task(self, task_id: str) -> Task:
        original = self._require_task(task_id)
        rerunnable = {TaskStatus.MERGED, TaskStatus.FAILED, TaskStatus.REJECTED}
        if original.status not in rerunnable:
            raise ValueError(
                "Only merged, failed, or rejected tasks can be rerun as a new task"
            )
        task = self.create_task(
            original.title,
            original.requirement,
            original.repository_id or "demo",
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
        original = self._require_task(task_id)
        if original.status != TaskStatus.MERGED:
            raise ValueError("Only a merged task can create a follow-up task")
        repository = self.store.get_repository(original.repository_id or "demo")
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
            original.repository_id or "demo",
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
        task = self._require_task(task_id)
        if task.status != TaskStatus.FAILED:
            raise ValueError("Only a failed task can continue from its failure context")
        if not task.workspace or not Path(task.workspace).is_dir():
            raise ValueError("The failed task workspace is no longer available")
        repository = self.store.get_repository(task.repository_id or "demo")
        if repository is None:
            raise ValueError("Repository not found")
        if repository.execution_mode != "plan_only":
            raise ValueError("Failure-aware retry is only supported for model-driven repositories")
        if task.technical_plan is None:
            raise ValueError("Technical plan is missing")

        failure_context = self._failure_context(task)
        diagnosed_files = self._diagnose_failure_files(Path(task.workspace), failure_context)
        dependency_files = self.planner.ensure_dependency_scope(
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

    def _create_task(
        self,
        title: str,
        requirement: str,
        repository_id: str = "demo",
        *,
        figma_url: str = "",
        preview_url: str = "",
        viewport_width: int = 1440,
        viewport_height: int = 900,
    ) -> Task:
        repository = self.store.get_repository(repository_id)
        if repository is None:
            raise ValueError("Repository not found")
        task_id = uuid4().hex[:12]
        design_reference = None
        if figma_url.strip():
            if self.figma_client is None:
                raise ValueError("Figma MCP 尚未配置，请设置 FIGMA_MCP_URL")
            design_reference = self.figma_client.capture(
                task_id,
                figma_url.strip(),
                preview_url.strip() or None,
                viewport_width,
                viewport_height,
            )
        task = Task(
            id=task_id,
            title=title.strip(),
            requirement=requirement.strip(),
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
        span = self.tracer.span("agent.plan", kind="agent") if self.tracer else None
        if span:
            with span:
                task.analysis, task.technical_plan = self.planner.plan(
                    task.title, task.requirement, task.repository_analysis,
                    task.design_reference.context if task.design_reference else "",
                )
        else:
            task.analysis, task.technical_plan = self.planner.plan(
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
        return task

    def approve(
        self,
        task_id: str,
        actor: str,
        comment: str = "",
        *,
        checkpoint_job_id: str | None = None,
        should_pause: Callable[[], bool] | None = None,
    ) -> Task:
        task = self._require_task(task_id)
        if task.status != TaskStatus.WAITING_REQUIREMENT_APPROVAL:
            raise ValueError("Only a task waiting for approval can be approved")

        repository_config = self.store.get_repository(task.repository_id or "demo")
        if repository_config is None:
            raise ValueError("Repository not found")
        if self._replan_if_repository_changed(task, repository_config):
            return task

        task.approval = Approval(decision="approved", actor=actor, comment=comment)
        task.metadata["approved_plan_hash"] = self._technical_plan_hash(task)
        task.metadata["development_head_sha"] = (
            task.repository_analysis.head_sha if task.repository_analysis else None
        )
        task.metadata["approved_index_version"] = task.metadata.get("index_version")
        self.store.save_task(task)
        if repository_config.execution_mode == "plan_only":
            self._transition(task, TaskStatus.DEVELOPING)
            self._event(
                task,
                "approved",
                f"{actor} 已批准技术方案，开始创建隔离 Worktree",
                {"comment": comment},
            )
            handler = self._checkpoint_handler(task, checkpoint_job_id, should_pause)
            if handler and handler("plan_approved", "prepare_workspace", {}):
                return task
            return self._run_real_development(
                task, repository_config, checkpoint_handler=handler
            )
        self._transition(task, TaskStatus.DEVELOPING)
        self._event(task, "approved", f"{actor} 已批准技术方案", {"comment": comment})
        handler = self._checkpoint_handler(task, checkpoint_job_id, should_pause)
        if handler and handler("plan_approved", "prepare_workspace", {}):
            return task

        try:
            repository = self.workspace_manager.prepare(task.id)
            task.workspace = str(repository)
            self.store.save_task(task)
            if handler and handler(
                "proposal_ready",
                "apply_patch",
                {"mode": "demo", "implementation": "deterministic"},
            ):
                return task
            changed_files = self.developer.implement(repository)
            self._event(task, "implementation_complete", "代码修改已完成", {"files": changed_files})
            if handler and handler("patch_applied", "run_tests", {"changed_files": changed_files}):
                return task

            self._transition(task, TaskStatus.TESTING)
            command, exit_code, output = self.workspace_manager.run_tests(repository)
            diff = self.workspace_manager.diff(repository)
            success = exit_code == 0
            mr_title = "feat: add priority to tasks"
            mr_description = self._build_mr_description(task, command, exit_code, output)
            task.result = ExecutionResult(
                success=success,
                command=command,
                exit_code=exit_code,
                output=output,
                diff=diff,
                mr_title=mr_title,
                mr_description=mr_description,
            )
            if handler and handler(
                "test_result_saved",
                "generate_mr" if success else "stop_failed",
                {"result": task.result.model_dump(mode="json"), "success": success},
            ):
                return task
            if success:
                self._transition(task, TaskStatus.CHANGE_READY)
                self._event(task, "change_ready", "测试通过，Diff 和 MR 描述已生成")
                return self._finish_review_pipeline(
                    task, repository_config, checkpoint_handler=handler
                )
            else:
                task.error = "Automated tests failed"
                self._transition(task, TaskStatus.FAILED)
                self._event(task, "tests_failed", "自动化测试失败", {"exit_code": exit_code})
            return task
        except Exception as error:
            task.error = str(error)
            if task.status in {TaskStatus.DEVELOPING, TaskStatus.TESTING}:
                self._transition(task, TaskStatus.FAILED)
            self._event(task, "execution_failed", "执行过程中发生错误", {"error": str(error)})
            return task

    def reject(self, task_id: str, actor: str, comment: str) -> Task:
        task = self._require_task(task_id)
        if task.status != TaskStatus.WAITING_REQUIREMENT_APPROVAL:
            raise ValueError("Only a task waiting for approval can be rejected")
        task.approval = Approval(decision="rejected", actor=actor, comment=comment)
        self._transition(task, TaskStatus.REJECTED)
        self._event(task, "rejected", f"{actor} 已拒绝技术方案", {"comment": comment})
        return task

    def approve_risk(self, task_id: str, actor: str, comment: str = "") -> Task:
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
            outcome = self.generic_developer.run(
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
            if not self.generic_developer.model_gateway.enabled:
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
                and repository_config.id != "demo"
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
                outcome = self.generic_developer.run(
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
                return self._finish_review_pipeline(
                    task, repository_config, checkpoint_handler=checkpoint_handler
                )
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
            "agent.developer_session",
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
        from dev_agent.agents.generic_developer import DeveloperRunOutcome

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
                outcome = DeveloperRunOutcome(
                    kind="success",
                    result=ExecutionResult.model_validate(current_resume_payload["result"]),
                )
            else:
                outcome = self.generic_developer.run(
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
                return DeveloperRunOutcome(
                    kind="paused", attempts=all_attempts, result=latest_result,
                    paused_at=outcome.paused_at,
                )
            if outcome.kind != "success":
                execution.status = "failed"
                execution.error = outcome.error or "本步骤执行失败"
                self._event(task, "development_step_failed", f"步骤失败：{step.title}", {
                    "step_id": step.id, "step_index": index, "error": execution.error,
                })
                return DeveloperRunOutcome(
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
            return DeveloperRunOutcome(kind="failed", attempts=all_attempts, error="No development step produced a result")
        return DeveloperRunOutcome(kind="success", attempts=all_attempts, result=latest_result)

    def run_review(self, task_id: str) -> Task:
        task = self._require_task(task_id)
        if task.status == TaskStatus.REVIEW_REPAIRING:
            raise ValueError(
                "Code Review 修复正在执行或曾被中断，请通过任务恢复入口继续"
            )
        if task.status not in {
            TaskStatus.CHANGE_READY,
            TaskStatus.CHANGES_REQUESTED,
        }:
            raise ValueError("Task is not ready for code review")
        repository = self.store.get_repository(task.repository_id or "demo")
        if repository is None:
            raise ValueError("Repository not found")
        if task.status == TaskStatus.CHANGE_READY:
            return self._finish_review_pipeline(task, repository)
        return self._review_loop(task, repository)

    def apply_user_feedback(
        self,
        task_id: str,
        actor: str,
        feedback: str,
        job_id: str | None = None,
        should_pause: Callable[[], bool] | None = None,
    ) -> Task:
        task = self._require_task(task_id)
        if task.status not in {
            TaskStatus.WAITING_RELEASE_APPROVAL,
            TaskStatus.CHANGES_REQUESTED,
            TaskStatus.WAITING_MERGE_APPROVAL,
        }:
            raise ValueError("Task is not waiting for user acceptance feedback")
        repository = self.store.get_repository(task.repository_id or "demo")
        if repository is None:
            raise ValueError("Repository not found")
        if repository.execution_mode != "plan_only" or not self.generic_developer.model_gateway.enabled:
            raise ValueError("User feedback repair requires a configured Developer Agent")
        normalized_feedback = feedback.strip()
        if not normalized_feedback:
            raise ValueError("Feedback cannot be empty")

        before_snapshot = self._workspace_snapshot(task)
        feedback_round = {
            "actor": actor,
            "feedback": normalized_feedback,
            "submitted_at": datetime.now(UTC).isoformat(),
            "status": "running",
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
        self._transition(task, TaskStatus.REVIEW_REPAIRING)
        repair_feedback = (
            "用户在发布 PR 前验收代码后提出以下修改意见。必须基于当前工作区继续修改，"
            "保留此前正确实现，不要新建任务：\n- " + normalized_feedback
        )
        outcome = self.generic_developer.run(
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
            "已按意见完成修改，测试和 Code Review 均已通过，请查看本轮 Diff。"
            if completed.status == TaskStatus.WAITING_RELEASE_APPROVAL
            else "已完成代码修改和测试，但 Code Review 仍有阻塞问题，请查看本轮 Diff 和审查意见。"
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
        task.analysis, task.technical_plan = self.planner.plan(
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
        task = self._require_task(task_id)
        if task.status not in {TaskStatus.WAITING_RELEASE_APPROVAL, TaskStatus.WAITING_MERGE_APPROVAL}:
            raise ValueError("Task is not waiting for release or merge approval")
        task.approval = Approval(decision="release_rejected", actor=actor, comment=comment)
        self._transition(task, TaskStatus.REJECTED)
        self._event(task, "release_rejected", f"{actor} 已拒绝进入发布阶段", {"comment": comment})
        return task

    def publish_pull_request(self, task_id: str, actor: str) -> Task:
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
        self._transition(task, TaskStatus.GENERATING_MR)
        task.merge_request = self._write_merge_request(task)
        self._event(
            task,
            "merge_request_generated",
            "已根据最终 Diff、验收标准和测试结果生成 MR 草稿",
            {"changed_files": task.merge_request.changed_files},
        )
        return self._review_loop(task, repository_config, checkpoint_handler=checkpoint_handler)

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
        return self.mr_writer.write(task)

    def run_ui_acceptance(self, task_id: str) -> Task:
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
            task.merge_request = self.mr_writer.write(task)
            self.store.save_task(task)
        return task

    def _review_loop(self, task: Task, repository_config, checkpoint_handler=None) -> Task:
        cycle_start = int(task.metadata.get("review_cycle_start", 0))
        while len(task.reviews) - cycle_start < 3:
            self._transition(task, TaskStatus.REVIEWING)
            review = self.code_reviewer.review(task, len(task.reviews) + 1)
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
                return task
            if review.decision == "approved":
                task.error = None
                self._transition(task, TaskStatus.REVIEW_APPROVED)
                self._event(task, "review_approved", "Code Review 已通过")
                self._transition(task, TaskStatus.WAITING_RELEASE_APPROVAL)
                self._event(task, "release_approval_required", "MR 和 Code Review 已就绪，等待发布审批")
                return task

            self._transition(task, TaskStatus.CHANGES_REQUESTED)
            self._event(task, "review_changes_requested", "Reviewer 提出阻塞问题，准备自动修复")
            if len(task.reviews) - cycle_start >= 3:
                task.error = "Automatic review repair exhausted after 3 review rounds"
                self._event(task, "review_repair_exhausted", "已达到三轮 Code Review 上限，等待人工处理")
                return task
            if repository_config.execution_mode != "plan_only" or not self.generic_developer.model_gateway.enabled:
                task.error = "Blocking review findings require a configured Developer Agent"
                self._event(task, "review_repair_unavailable", "当前执行模式无法自动修复 CR 问题")
                return task

            self._transition(task, TaskStatus.REVIEW_REPAIRING)
            feedback = self._review_feedback(review)
            outcome = self.generic_developer.run(
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
                return task
            if outcome.kind == "risk_approval":
                task.pending_proposal = outcome.pending_proposal
                task.metadata["risk_reasons"] = outcome.risk_reasons
                task.metadata["review_repair_pending"] = True
                task.metadata["pending_repair_feedback"] = feedback
                self._transition(task, TaskStatus.WAITING_RISK_APPROVAL)
                self._event(task, "risk_approval_required", "CR 修复涉及高风险文件，等待人工审批")
                return task
            if outcome.kind != "success":
                task.error = outcome.error or "Review repair failed"
                self._transition(task, TaskStatus.CHANGES_REQUESTED)
                self._event(task, "review_repair_failed", "CR 自动修复失败", {"error": task.error})
                return task

            task.result = outcome.result
            task.error = None
            self._transition(task, TaskStatus.GENERATING_MR)
            task.merge_request = self._write_merge_request(task)
            self._event(task, "merge_request_regenerated", "CR 修复测试通过，已重新生成 MR 草稿")
        return task

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
    def _workspace_snapshot(task: Task) -> dict[str, str]:
        if not task.workspace or not task.technical_plan:
            return {}
        workspace = Path(task.workspace).resolve()
        snapshot: dict[str, str] = {}
        for relative in task.technical_plan.affected_files[:40]:
            target = (workspace / relative).resolve()
            if workspace in target.parents and target.is_file():
                snapshot[relative] = target.read_text(encoding="utf-8", errors="replace")
        return snapshot

    def current_workspace_diff(self, task: Task) -> str:
        """Return the cumulative diff that is actually present in the task worktree.

        This intentionally differs from ``task.result.diff`` after a failed repair:
        result remains the last verified result, while the worktree may contain useful
        unverified edits that the user still needs to inspect.
        """
        if not task.workspace:
            return task.result.diff if task.result else ""
        workspace = Path(task.workspace)
        if not workspace.is_dir():
            return task.result.diff if task.result else ""
        try:
            baseline = task.repository_analysis.head_sha if task.repository_analysis else None
            return self.workspace_manager.diff_from_baseline(workspace, baseline)[-100_000:]
        except (OSError, ValueError, subprocess.SubprocessError):
            return task.result.diff if task.result else ""

    def _finish_user_feedback_round(
        self,
        task: Task,
        feedback_round: dict,
        before_snapshot: dict[str, str],
        status: str,
        message: str,
        review_decision: str | None = None,
    ) -> None:
        after_snapshot = self._workspace_snapshot(task)
        changed_files: list[str] = []
        diff_parts: list[str] = []
        for path in sorted(set(before_snapshot) | set(after_snapshot)):
            before = before_snapshot.get(path, "")
            after = after_snapshot.get(path, "")
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
        task.metadata["workspace_diff"] = self.current_workspace_diff(task)
        task.metadata["user_feedback_rounds"] = list(
            task.metadata.get("user_feedback_rounds", [])
        )
        task.updated_at = datetime.now(UTC)
        self.store.save_task(task)

    @staticmethod
    def _review_feedback(review) -> str:
        lines = [
            f"- [{item.severity}] {item.file or 'general'}"
            f"{':' + str(item.line) if item.line else ''}: {item.message} {item.suggestion}"
            for item in review.findings if item.blocking
        ]
        return "Code Review 阻塞问题，必须全部修复：\n" + "\n".join(lines)

    def resume_from_checkpoint(
        self,
        task_id: str,
        job_id: str,
        should_pause: Callable[[], bool] | None = None,
    ) -> Task:
        task = self._require_task(task_id)
        checkpoint = self.store.latest_checkpoint(task_id)
        if checkpoint is None:
            raise ValueError("No durable checkpoint is available for this task")
        self._validate_checkpoint(task, checkpoint)
        task.metadata.pop("paused_checkpoint", None)
        self.store.save_task(task)
        repository = self.store.get_repository(task.repository_id or "demo")
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
            if repository.execution_mode == "demo":
                return self._resume_demo(task, repository, checkpoint, handler)
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
            if repository.execution_mode == "demo":
                return self._resume_demo(task, repository, checkpoint, handler)
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
            if repository.execution_mode != "plan_only":
                task.error = "Saved test result failed; demo execution cannot auto-repair"
                self._transition(task, TaskStatus.FAILED)
                return task
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
        outcome = self.generic_developer.run(
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

    def _resume_demo(self, task: Task, repository_config, checkpoint, handler) -> Task:
        try:
            repository = Path(task.workspace) if task.workspace else self.workspace_manager.prepare(task.id)
            if not task.workspace:
                task.workspace = str(repository)
                self.store.save_task(task)
            if checkpoint.stage == "plan_approved":
                if handler and handler(
                    "proposal_ready",
                    "apply_patch",
                    {"mode": "demo", "implementation": "deterministic"},
                ):
                    return task
            if checkpoint.stage in {"plan_approved", "proposal_ready"}:
                changed_files = self.developer.implement(repository)
                self._event(task, "implementation_complete", "代码修改已完成", {"files": changed_files})
                if handler and handler("patch_applied", "run_tests", {"changed_files": changed_files}):
                    return task
            if task.status == TaskStatus.DEVELOPING:
                self._transition(task, TaskStatus.TESTING)
            command, exit_code, output = self.workspace_manager.run_tests(repository)
            diff = self.workspace_manager.diff(repository)
            task.result = ExecutionResult(
                success=exit_code == 0,
                command=command,
                exit_code=exit_code,
                output=output,
                diff=diff,
                mr_title="feat: add priority to tasks",
                mr_description=self._build_mr_description(task, command, exit_code, output),
            )
            if handler and handler(
                "test_result_saved",
                "generate_mr" if exit_code == 0 else "stop_failed",
                {"result": task.result.model_dump(mode="json"), "success": exit_code == 0},
            ):
                self.store.save_task(task)
                return task
            if exit_code != 0:
                task.error = "Automated tests failed"
                self._transition(task, TaskStatus.FAILED)
                return task
            self._transition(task, TaskStatus.CHANGE_READY)
            self._event(task, "change_ready", "从检查点恢复后测试通过")
            return self._finish_review_pipeline(task, repository_config, checkpoint_handler=handler)
        except Exception as error:
            task.error = str(error)
            if task.status in {TaskStatus.DEVELOPING, TaskStatus.TESTING}:
                self._transition(task, TaskStatus.FAILED)
            self._event(task, "execution_failed", "检查点恢复失败", {"error": str(error)})
            return task

    def _resume_review_repair(self, task: Task, repository, review, handler) -> Task:
        self._transition(task, TaskStatus.CHANGES_REQUESTED)
        self._event(task, "review_changes_requested", "从审查检查点恢复，准备自动修复")
        if len(task.reviews) >= 3:
            task.error = "Automatic review repair exhausted after 3 review rounds"
            return task
        if repository.execution_mode != "plan_only" or not self.generic_developer.model_gateway.enabled:
            task.error = "Blocking review findings require a configured Developer Agent"
            return task
        self._transition(task, TaskStatus.REVIEW_REPAIRING)
        outcome = self.generic_developer.run(
            task,
            Path(task.workspace),
            dependency_repository=Path(repository.local_path),
            review_feedback=self._review_feedback(review),
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
        task = self._require_task(task_id)
        if task.status not in {TaskStatus.WAITING_REQUIREMENT_APPROVAL, TaskStatus.WAITING_REQUIREMENT_INPUT}:
            raise ValueError("Only a task waiting for input or approval can be revised")
        self._transition(task, TaskStatus.REQUIREMENT_ANALYSIS)
        task.metadata.setdefault("plan_feedback", []).append({"actor": actor, "feedback": feedback})
        enriched_requirement = f"{task.requirement}\n\n用户补充意见：{feedback}"
        if task.repository_analysis is None:
            repository = self.store.get_repository(task.repository_id or "demo")
            task.repository_analysis = self.repository_analyzer.analyze(Path(repository.local_path), enriched_requirement)
        task.analysis, task.technical_plan = self.planner.plan(
            task.title, enriched_requirement, task.repository_analysis,
            task.design_reference.context if task.design_reference else "",
        )
        self._transition(task, TaskStatus.WAITING_REQUIREMENT_APPROVAL)
        self._event(task, "plan_revised", f"已根据 {actor} 的意见重新生成方案", {"feedback": feedback})
        return task

    def _transition(self, task: Task, target: TaskStatus) -> None:
        ensure_transition(task.status, target)
        task.status = target
        task.updated_at = datetime.now(UTC)
        self.store.save_task(task)

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
