"""代码开发 Agent。

根据技术方案自主探索仓库、生成并应用修改、运行测试和修复失败，返回可恢复的开发结果。
"""

from __future__ import annotations

import json
import re
from contextlib import nullcontext
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from domain.models import (
    DevelopmentAttempt,
    DevelopmentProposal,
    ExecutionResult,
    ReplacementDevelopmentProposal,
    Task,
    ToolCallAudit,
)
from llm import ModelGateway
from tools import DeveloperToolkit, PatchRejected, ToolPolicy

from .repository_exploration_agent import RepositoryExplorationAgent


@dataclass
class DevelopmentRunOutcome:
    kind: str
    attempts: list[DevelopmentAttempt] = field(default_factory=list)
    result: ExecutionResult | None = None
    pending_proposal: DevelopmentProposal | None = None
    risk_reasons: list[str] = field(default_factory=list)
    error: str | None = None
    paused_at: str | None = None


class DevelopmentLoopState(TypedDict, total=False):
    attempt_number: int
    approved_proposal: DevelopmentProposal | None
    repair_context: str
    review_feedback: str
    attempts: list[DevelopmentAttempt]
    last_failure_signature: str | None
    skip_apply: bool
    resumed_changed_files: list[str]
    audit_start: int
    context: dict[str, str]
    proposal: DevelopmentProposal
    paths: list[str]
    risk_level: str
    changed_files: list[str]
    execution: Any
    diff: str
    route: str
    outcome: DevelopmentRunOutcome


class CodeDevelopmentAgent:
    def __init__(self, model_gateway: ModelGateway, max_attempts: int = 3, tracer=None):
        """初始化负责生成 Patch、测试和自动修复的 Developer Agent。

        Args:
            model_gateway: 生成结构化代码修改方案和失败修复方案的模型网关。
            max_attempts: 单次开发或修复允许的最大尝试次数。
            tracer: 可选的 Trace 记录器，用于记录 Agent、模型和工具 Span。
        """
        self.model_gateway = model_gateway
        self.max_attempts = max_attempts
        self.tracer = tracer
        self.repository_exploration_agent = RepositoryExplorationAgent(model_gateway, tracer=tracer)

    def _span(self, name: str, kind: str = "tool", **attributes):
        return self.tracer.span(name, kind=kind, attributes=attributes) if self.tracer else nullcontext(None)

    def run(
        self,
        task: Task,
        repository: Path,
        approved_proposal: DevelopmentProposal | None = None,
        high_risk_approved: bool = False,
        dependency_repository: Path | None = None,
        review_feedback: str = "",
        checkpoint_handler: Callable[[str, str, dict], bool] | None = None,
        resume_stage: str | None = None,
        resume_payload: dict | None = None,
        allowed_files: list[str] | None = None,
        step_context: dict | None = None,
        session_context: dict[str, str] | None = None,
        session_files: list[str] | None = None,
    ) -> DevelopmentRunOutcome:
        if not self.model_gateway.enabled:
            return DevelopmentRunOutcome(
                kind="failed",
                error="No model provider is configured. Real-repository development requires an LLM.",
            )
        if task.repository_analysis is None or task.technical_plan is None:
            return DevelopmentRunOutcome(kind="failed", error="Repository analysis or technical plan is missing")
        test_command = task.repository_analysis.test_command
        if not test_command:
            return DevelopmentRunOutcome(kind="failed", error="No approved test command was detected for this repository")

        write_scope = list(dict.fromkeys(allowed_files or task.technical_plan.affected_files))
        approved_scope = set(task.technical_plan.affected_files)
        if not write_scope or any(path not in approved_scope for path in write_scope):
            return DevelopmentRunOutcome(kind="failed", error="Development step contains files outside the approved plan")
        policy = ToolPolicy(repository, write_scope, test_command)
        toolkit = DeveloperToolkit(repository, policy, dependency_repository)
        session_context = session_context if session_context is not None else {}
        session_files = session_files or []
        attempts: list[DevelopmentAttempt] = []
        last_failure_signature: str | None = None
        repair_context = ""
        start_attempt = 1
        skip_apply = False
        resumed_changed_files: list[str] = []
        resume_payload = resume_payload or {}
        if resume_stage == "proposal_ready":
            approved_proposal = DevelopmentProposal.model_validate(resume_payload["proposal"])
            start_attempt = int(resume_payload.get("attempt", 1))
            repair_context = str(resume_payload.get("repair_context", ""))
            review_feedback = str(resume_payload.get("review_feedback", review_feedback))
        elif resume_stage == "patch_applied":
            approved_proposal = DevelopmentProposal.model_validate(resume_payload["proposal"])
            start_attempt = int(resume_payload.get("attempt", 1))
            review_feedback = str(resume_payload.get("review_feedback", review_feedback))
            resumed_changed_files = list(resume_payload.get("changed_files", []))
            skip_apply = True
        elif resume_stage == "test_result_saved":
            start_attempt = int(resume_payload.get("next_attempt", 1))
            repair_context = str(resume_payload.get("repair_context", ""))
            review_feedback = str(resume_payload.get("review_feedback", review_feedback))

        if repair_context:
            pending_failure = repair_context
            existing_diff = toolkit.diff()
            if existing_diff:
                audit_start = len(toolkit.audit)
                execution = toolkit.run_tests(test_command)
                attempts.append(DevelopmentAttempt(
                    attempt=0,
                    summary="失败续跑前验证工作区中已累积的修改",
                    changed_files=list(write_scope),
                    test_command=execution.command,
                    exit_code=execution.exit_code,
                    output=execution.output,
                    diff=existing_diff,
                    tool_calls=toolkit.audit[audit_start:],
                ))
                if execution.exit_code == 0:
                    return DevelopmentRunOutcome(
                        kind="success",
                        attempts=attempts,
                        result=ExecutionResult(
                            success=True,
                            command=execution.command,
                            exit_code=0,
                            output=execution.output,
                            diff=existing_diff,
                            mr_title=f"feat: {task.title}",
                            mr_description=self._mr_description(task, attempts),
                        ),
                    )
                pending_failure = execution.output
            for _ in range(3):
                audit_start = len(toolkit.audit)
                # Only apply transformations whose correctness follows from
                # syntax alone. Compiler/linter diagnostics about symbols do
                # not tell us whether to remove the symbol or restore its use.
                repaired_files = toolkit.repair_malformed_css(
                    write_scope
                )
                if not repaired_files:
                    break
                execution = toolkit.run_tests(test_command)
                diff = toolkit.diff()
                attempts.append(DevelopmentAttempt(
                    attempt=0,
                    summary="在模型修复前处理构建器已明确诊断的机械性错误",
                    changed_files=repaired_files,
                    test_command=execution.command,
                    exit_code=execution.exit_code,
                    output=execution.output,
                    diff=diff,
                    tool_calls=toolkit.audit[audit_start:],
                ))
                if execution.exit_code == 0 and diff:
                    return DevelopmentRunOutcome(
                        kind="success",
                        attempts=attempts,
                        result=ExecutionResult(
                            success=True,
                            command=execution.command,
                            exit_code=0,
                            output=execution.output,
                            diff=diff,
                            mr_title=f"feat: {task.title}",
                            mr_description=self._mr_description(task, attempts),
                        ),
                    )
                pending_failure = execution.output
            if attempts:
                repair_context = (
                    "Deterministic build repairs were applied and retained. "
                    "Do not revert them. Fix only the current remaining error.\n"
                    f"Current test/build failure:\n{pending_failure[-8000:]}"
                )

        return self._run_attempt_graph(
            task=task,
            toolkit=toolkit,
            policy=policy,
            test_command=test_command,
            write_scope=write_scope,
            session_context=session_context,
            session_files=session_files,
            step_context=step_context,
            checkpoint_handler=checkpoint_handler,
            high_risk_approved=high_risk_approved,
            initial_state={
                "attempt_number": start_attempt,
                "approved_proposal": approved_proposal,
                "repair_context": repair_context,
                "review_feedback": review_feedback,
                "attempts": attempts,
                "last_failure_signature": last_failure_signature,
                "skip_apply": skip_apply,
                "resumed_changed_files": resumed_changed_files,
            },
        )

    def _run_attempt_graph(
        self,
        *,
        task: Task,
        toolkit: DeveloperToolkit,
        policy: ToolPolicy,
        test_command: str,
        write_scope: list[str],
        session_context: dict[str, str],
        session_files: list[str],
        step_context: dict | None,
        checkpoint_handler: Callable[[str, str, dict], bool] | None,
        high_risk_approved: bool,
        initial_state: DevelopmentLoopState,
    ) -> DevelopmentRunOutcome:
        """Run the bounded Developer repair loop as explicit LangGraph nodes."""
        if initial_state["attempt_number"] > self.max_attempts:
            return DevelopmentRunOutcome(
                kind="failed",
                attempts=initial_state["attempts"],
                error=f"Automatic repair exhausted after {self.max_attempts} attempts",
            )

        def prepare(state: DevelopmentLoopState) -> dict:
            attempt_number = state["attempt_number"]
            attempts = state["attempts"]
            repair_context = state.get("repair_context", "")
            review_feedback = state.get("review_feedback", "")
            audit_start = len(toolkit.audit)
            context_pack = task.repository_analysis.context_pack
            context_paths = list(dict.fromkeys([
                *write_scope,
                *session_files,
                *session_context.keys(),
                *(context_pack.primary_files if context_pack else []),
                *(context_pack.dependency_files if context_pack else []),
                *(context_pack.test_files if context_pack else []),
            ]))
            with self._span("tool.read_context", file_count=len(context_paths)):
                context = dict(session_context)
                context.update(toolkit.read_context(context_paths))
            with self._span("agent.context_exploration", kind="agent", initial_file_count=len(context)):
                context = self.repository_exploration_agent.explore(
                    task=task,
                    toolkit=toolkit,
                    initial_context=context,
                    write_scope=write_scope,
                    repair_context=repair_context,
                    review_feedback=review_feedback,
                )
            session_context.update(context)
            approved_proposal = state.get("approved_proposal")
            using_approved_proposal = approved_proposal is not None
            proposal = approved_proposal if using_approved_proposal else self._request_proposal(
                task,
                context,
                test_command,
                attempt_number,
                repair_context,
                review_feedback,
                allowed_files=write_scope,
                step_context=step_context,
            )
            if proposal is None:
                detail = getattr(self.model_gateway, "last_error", "")
                suffix = f": {detail}" if detail else ""
                return {
                    "route": "done",
                    "outcome": DevelopmentRunOutcome(
                        kind="failed",
                        attempts=attempts,
                        error=f"Model did not return a valid structured patch proposal{suffix}",
                    ),
                }
            paths = [change.path for change in proposal.changes] + [
                replacement.path for replacement in proposal.replacements
            ]
            risk = policy.assess_paths(paths)
            if risk.level == "forbidden":
                return {
                    "route": "done",
                    "outcome": DevelopmentRunOutcome(
                        kind="failed", attempts=attempts, error="; ".join(risk.reasons)
                    ),
                }
            if risk.level == "high" and not (using_approved_proposal and high_risk_approved):
                return {
                    "route": "done",
                    "outcome": DevelopmentRunOutcome(
                        kind="risk_approval",
                        attempts=attempts,
                        pending_proposal=proposal,
                        risk_reasons=risk.reasons,
                    ),
                }
            return {
                "approved_proposal": None,
                "audit_start": audit_start,
                "context": context,
                "proposal": proposal,
                "paths": paths,
                "risk_level": risk.level,
                "route": "apply",
            }

        def apply_and_test(state: DevelopmentLoopState) -> dict:
            attempt_number = state["attempt_number"]
            attempts = state["attempts"]
            proposal = state["proposal"]
            repair_context = state.get("repair_context", "")
            review_feedback = state.get("review_feedback", "")
            skip_apply = state.get("skip_apply", False)
            if checkpoint_handler and not skip_apply and checkpoint_handler(
                "proposal_ready",
                "apply_patch",
                {
                    "proposal": proposal.model_dump(mode="json"),
                    "attempt": attempt_number,
                    "repair_context": repair_context,
                    "review_feedback": review_feedback,
                },
            ):
                return {
                    "route": "done",
                    "outcome": DevelopmentRunOutcome(
                        kind="paused", attempts=attempts, paused_at="proposal_ready"
                    ),
                }
            try:
                with self._span("tool.apply_proposal", attempt=attempt_number):
                    changed_files = (
                        state.get("resumed_changed_files", [])
                        if skip_apply
                        else toolkit.apply_proposal(proposal)
                    )
                if checkpoint_handler and checkpoint_handler(
                    "patch_applied",
                    "run_tests",
                    {
                        "proposal": proposal.model_dump(mode="json"),
                        "attempt": attempt_number,
                        "changed_files": changed_files,
                        "review_feedback": review_feedback,
                    },
                ):
                    return {
                        "skip_apply": False,
                        "resumed_changed_files": [],
                        "route": "done",
                        "outcome": DevelopmentRunOutcome(
                            kind="paused", attempts=attempts, paused_at="patch_applied"
                        ),
                    }
                with self._span("tool.run_tests", command=proposal.test_command) as test_span:
                    execution = toolkit.run_tests(proposal.test_command)
                    if test_span is not None:
                        test_span.attributes["exit_code"] = execution.exit_code
                with self._span("tool.git_diff"):
                    diff = toolkit.diff()
                session_context.update(toolkit.read_context(changed_files))
                return {
                    "skip_apply": False,
                    "resumed_changed_files": [],
                    "changed_files": changed_files,
                    "execution": execution,
                    "diff": diff,
                    "route": "evaluate",
                }
            except (PatchRejected, ValueError, OSError) as error:
                paths = state["paths"]
                failed_paths = list(dict.fromkeys(paths))
                repair_context = (
                    "The previous proposal was rejected before application; none of its edits were written.\n"
                    f"Rejected target files: {', '.join(failed_paths)}\n"
                    f"Application failed: {error}\n"
                    "Do not repeat or cosmetically reformat the rejected proposal. Re-read the current "
                    "repository_context and choose the file that actually owns the missing behavior."
                )
                attempts.append(DevelopmentAttempt(
                    attempt=attempt_number,
                    summary=proposal.summary,
                    changed_files=paths,
                    test_command=[],
                    exit_code=-1,
                    output=repair_context,
                    risk_level=state["risk_level"],
                    proposal=proposal,
                    tool_calls=[
                        *toolkit.audit[state["audit_start"]:],
                        ToolCallAudit(
                            tool="model_generate_patch",
                            summary=f"生成第 {attempt_number} 次 Patch",
                        ),
                    ],
                ))
                next_attempt = attempt_number + 1
                if next_attempt > self.max_attempts:
                    return {
                        "route": "done",
                        "outcome": DevelopmentRunOutcome(
                            kind="failed",
                            attempts=attempts,
                            error=f"Automatic repair exhausted after {self.max_attempts} attempts",
                        ),
                    }
                return {
                    "attempt_number": next_attempt,
                    "repair_context": repair_context,
                    "skip_apply": False,
                    "resumed_changed_files": [],
                    "route": "retry",
                }

        def evaluate(state: DevelopmentLoopState) -> dict:
            attempt_number = state["attempt_number"]
            attempts = state["attempts"]
            proposal = state["proposal"]
            execution = state["execution"]
            diff = state["diff"]
            changed_files = state["changed_files"]
            review_feedback = state.get("review_feedback", "")
            attempt = DevelopmentAttempt(
                attempt=attempt_number,
                summary=proposal.summary,
                changed_files=changed_files,
                test_command=execution.command,
                exit_code=execution.exit_code,
                output=execution.output,
                diff=diff,
                risk_level=state["risk_level"],
                proposal=proposal,
                tool_calls=[
                    *toolkit.audit[state["audit_start"]:],
                    ToolCallAudit(
                        tool="model_generate_patch",
                        summary=f"生成第 {attempt_number} 次 Patch",
                    ),
                ],
            )
            attempts.append(attempt)
            if execution.exit_code == 0 and diff:
                result = ExecutionResult(
                    success=True,
                    command=execution.command,
                    exit_code=0,
                    output=execution.output,
                    diff=diff,
                    mr_title=f"feat: {task.title}",
                    mr_description=self._mr_description(task, attempts),
                )
                if checkpoint_handler and checkpoint_handler(
                    "test_result_saved",
                    "generate_mr",
                    {
                        "attempt": attempt.model_dump(mode="json"),
                        "result": result.model_dump(mode="json"),
                        "success": True,
                        "review_feedback": review_feedback,
                    },
                ):
                    outcome = DevelopmentRunOutcome(
                        kind="paused",
                        attempts=attempts,
                        result=result,
                        paused_at="test_result_saved",
                    )
                else:
                    outcome = DevelopmentRunOutcome(
                        kind="success", attempts=attempts, result=result
                    )
                return {"route": "done", "outcome": outcome}

            output = execution.output
            signature = (
                output
                if len(output) <= 6_000
                else output[:4_000] + "\n... output truncated ...\n" + output[-2_000:]
            )
            if signature == state.get("last_failure_signature"):
                if proposal.replacements:
                    toolkit.restore_files(state["context"], changed_files)
                return {
                    "route": "done",
                    "outcome": DevelopmentRunOutcome(
                        kind="failed",
                        attempts=attempts,
                        error="The same test failure repeated; automatic repair stopped early",
                    ),
                }
            repair_context = (
                f"Previous proposal:\n{proposal.model_dump_json(indent=2)}\n"
                f"Test command failed with exit code {execution.exit_code}:\n{signature}"
            )
            if checkpoint_handler and checkpoint_handler(
                "test_result_saved",
                "generate_repair_proposal",
                {
                    "attempt": attempt.model_dump(mode="json"),
                    "success": False,
                    "repair_context": repair_context,
                    "next_attempt": attempt_number + 1,
                    "review_feedback": review_feedback,
                },
            ):
                return {
                    "route": "done",
                    "outcome": DevelopmentRunOutcome(
                        kind="paused", attempts=attempts, paused_at="test_result_saved"
                    ),
                }
            next_attempt = attempt_number + 1
            if next_attempt > self.max_attempts:
                return {
                    "route": "done",
                    "outcome": DevelopmentRunOutcome(
                        kind="failed",
                        attempts=attempts,
                        error=f"Automatic repair exhausted after {self.max_attempts} attempts",
                    ),
                }
            return {
                "attempt_number": next_attempt,
                "last_failure_signature": signature,
                "repair_context": repair_context,
                "route": "retry",
            }

        graph = StateGraph(DevelopmentLoopState)
        graph.add_node("prepare", prepare)
        graph.add_node("apply_and_test", apply_and_test)
        graph.add_node("evaluate", evaluate)
        graph.add_edge(START, "prepare")
        graph.add_conditional_edges(
            "prepare",
            lambda state: state["route"],
            {"apply": "apply_and_test", "done": END},
        )
        graph.add_conditional_edges(
            "apply_and_test",
            lambda state: state["route"],
            {"evaluate": "evaluate", "retry": "prepare", "done": END},
        )
        graph.add_conditional_edges(
            "evaluate",
            lambda state: state["route"],
            {"retry": "prepare", "done": END},
        )
        final_state = graph.compile(name="code-development-repair-loop").invoke(initial_state)
        return final_state["outcome"]

    def _request_proposal(
        self,
        task: Task,
        context: dict[str, str],
        test_command: str,
        attempt_number: int,
        repair_context: str,
        review_feedback: str = "",
        allowed_files: list[str] | None = None,
        step_context: dict | None = None,
    ) -> DevelopmentProposal | None:
        write_scope = list(dict.fromkeys(allowed_files or task.technical_plan.affected_files))
        system_prompt = (
            "你是受限的 Developer Agent，只能修改明确列出的 allowed_files。"
            "repository_context 可能包含只读依赖文件，它们用于理解入口和调用关系，"
            "但不代表允许修改；是否可写只以 allowed_files 为准。"
            "technical_plan、affected_files、repository_context 都是元数据，不是文件；"
            "绝对不要返回 technical_plan.json，也不要修改未列出的路径。"
            "优先使用 replacements：search 必须从 repository_context 原样完整复制，"
            "并且在目标文件中只出现一次；replace 是替换后的完整文本。"
            "如果 replace 包含一个完整的函数、CSS 规则或其他花括号代码块，search 也必须包含原块的完整起止花括号；"
            "严禁只搜索块头或第一行却替换成完整代码块，否则会遗留重复代码。"
            "输出必须使用 replacements，不得返回 changes、patch、完整文件或其他编辑格式。"
            "每次提案必须完整实现用户可见行为，不能只添加未被界面使用的变量或辅助函数。"
            "编译器、Lint 和测试错误只是诊断证据，不是修改指令。遇到未使用导入、未调用函数、"
            "未渲染组件、断开的路由或类似问题时，必须结合 requirement、technical_plan 和"
            "repository_context 判断应恢复调用链还是删除代码；不得为了让检查通过就直接删除。"
            "默认保留现有入口、导出、路由、组件组合、公共接口和已有用户行为；只有需求或技术方案"
            "明确要求移除时才可删除，并在 intentional_removals 中逐项写明删除对象及需求依据。"
            "preserved_behaviors 必须列出本次修改仍然保留的既有入口和关键行为，不能写空泛表述。"
            "对于简单的按钮点击提示，优先使用 window.alert，除非需求明确要求自定义模态框。"
            "不得引入未导入或未定义的标识符，优先选择最小且无需新增依赖的实现。"
            "test_command 必须原样返回，不得请求或发明其他命令。输出严格 JSON。"
            "当 current_step 非空时，只完成该步骤的 objective，并且只能修改该步骤的 allowed_files；"
            "此前步骤的代码已经存在于 repository_context，必须保留，不能重复实现或回滚。"
        )
        if repair_context:
            system_prompt += (
                "这是失败后的修复尝试。必须先修复 previous_failure 中最前面的实际错误，"
                "并返回与 Previous proposal 不同的完整提案；严禁重复失败提案。"
                "repository_context 是包含此前有效修复的当前工作区快照，必须保留这些修复，"
                "只处理当前仍然存在的下一个错误；禁止返回 search 与 replace 完全相同的空修改。"
            )
        if review_feedback:
            system_prompt += (
                "这是 Code Review 修复任务。必须逐条处理 review_feedback 中的阻塞问题，"
                "保持原需求行为并返回完整、可测试的修复提案。"
            )
        entrypoints = [
            path for path in task.repository_analysis.entrypoints
            if path in context
        ]
        rejected_paths = set(re.findall(
            r"Rejected target files:\s*([^\n]+)",
            repair_context,
            flags=re.I,
        ))
        rejected_paths = {
            path.strip()
            for group in rejected_paths
            for path in group.split(",")
            if path.strip()
        }
        can_deprioritize = bool(
            rejected_paths
            and any(
                marker in repair_context.lower()
                for marker in (
                    "does not change any content",
                    "structural replacement",
                )
            )
            and any(path not in rejected_paths for path in context)
        )
        diagnostic_files = self._diagnostic_target_files(
            repair_context, write_scope
        )
        candidate_files = diagnostic_files or [
            path for path in write_scope
            if not can_deprioritize or path not in rejected_paths
        ]
        writable_files = list(dict.fromkeys([
            *entrypoints,
            *candidate_files,
        ]))
        if diagnostic_files:
            writable_files = diagnostic_files
        if can_deprioritize and not diagnostic_files:
            writable_files = [path for path in writable_files if path not in rejected_paths]
        writable_files = [
            path for path in writable_files
            if path in write_scope
        ]
        ordered_files = list(dict.fromkeys([
            *writable_files,
            *context.keys(),
        ]))
        ordered_context = {
            path: context[path] for path in ordered_files if path in context
        }
        payload = {
            "task": {"title": task.title, "requirement": task.requirement},
            "figma_design": (
                {
                    "node_id": task.design_reference.node_id,
                    "context": task.design_reference.context[:30000],
                    "variables": task.design_reference.variables[:12000],
                    "viewport": [
                        task.design_reference.viewport_width,
                        task.design_reference.viewport_height,
                    ],
                }
                if task.design_reference else None
            ),
            "approved_scope": {"affected_files": task.technical_plan.affected_files},
            "allowed_files": writable_files,
            "current_step": step_context or {},
            "repository_context": ordered_context,
            "test_command": test_command,
            "attempt": attempt_number,
            "previous_failure": repair_context,
            "review_feedback": review_feedback,
            "output_schema": ReplacementDevelopmentProposal.model_json_schema(),
        }
        guidance: list[str] = []
        wiring_terms = (
            "导入", "引用", "调用", "使用方式", "接入", "注册", "路由", "挂载",
            "入口", "调用链", "恢复使用", "恢复原来", "连接",
            "import", "reference", "invoke", "call", "wire", "route", "register",
            "mount", "entrypoint", "integrate", "restore", "dependency",
        )
        planning_text = " ".join([
            task.requirement,
            task.technical_plan.approach,
            *task.technical_plan.implementation_steps,
        ]).lower()
        if any(term in planning_text for term in wiring_terms):
            guidance.append(
                "这是调用链或接线变更。优先检查 repository_entrypoints 中的调用方，确认目标模块"
                "是否被正确导入、注册、挂载或调用；如果被调用模块已经满足需求，不要原地重写它。"
            )
        if diagnostic_files:
            guidance.append(
                "编译器/测试已把当前错误定位到这些文件，本轮只能修改它们："
                f"{', '.join(diagnostic_files)}。对于未定义符号或模块找不到错误，优先从"
                "repository_context 中找到现有定义并恢复 import、引用、注册或调用；不要改定义文件来规避错误。"
            )
        if "already been declared" in repair_context.lower() or "duplicate" in repair_context.lower():
            guidance.append(
                "诊断表明存在重复声明。保留一份原有声明及必要调用，精确删除重复插入的声明/测试块；"
                "不要通过改名制造两份等价实现，也不要重复写回相同内容。删除重复声明时，"
                "在 intentional_removals 中明确写出被删除声明及重复诊断依据。"
            )
            payload["repository_entrypoints"] = entrypoints
        if "does not change any content" in repair_context.lower():
            no_op_paths = list(dict.fromkeys(re.findall(
                r"Replacement in ([^\s]+) does not change any content",
                repair_context,
                flags=re.I,
            )))
            guidance.append(
                "之前的提案是 no-op，说明目标片段已经处于 replace 所描述的状态。"
                f"不要再次提交相同替换；重新检查其他 allowed_files，尤其是调用方和入口。"
                f"此前 no-op 文件：{', '.join(no_op_paths) or '见 previous_failure'}。"
            )
        if can_deprioritize:
            guidance.append(
                "以下文件的上一份修改已确认是 no-op 或结构不安全，本轮不要修改它们："
                f"{', '.join(sorted(rejected_paths))}。请从当前 allowed_files 中选择真正的调用方。"
            )
        normalized_requirement = task.requirement.lower()
        if "按钮" in task.requirement and any(token in normalized_requirement for token in ("弹窗", "提示", "alert")):
            guidance.append(
                "这是简单按钮提示需求：在现有 JSX 的唯一位置加入 button，"
                "onClick 直接调用 window.alert('需求中的提示内容')；不要使用 useState 或自定义模态框。"
            )
        if guidance:
            payload["implementation_guidance"] = guidance
        response = self.model_gateway.generate_structured(
            system_prompt,
            json.dumps(payload, ensure_ascii=False),
            ReplacementDevelopmentProposal,
        )
        if isinstance(response, DevelopmentProposal):
            return response
        if not isinstance(response, ReplacementDevelopmentProposal):
            return None
        return DevelopmentProposal(
            summary=response.summary,
            replacements=response.replacements,
            preserved_behaviors=response.preserved_behaviors,
            intentional_removals=response.intentional_removals,
            test_command=response.test_command,
        )

    @staticmethod
    def _diagnostic_target_files(
        failure_context: str, allowed_files: list[str]
    ) -> list[str]:
        if not failure_context:
            return []
        normalized = "\n".join(
            line for line in failure_context.replace("\\", "/").splitlines()
            if not line.lower().startswith((
                "rejected target files:",
                "previous proposal",
            ))
        )
        strong_markers = (
            "is not defined", "cannot find", "not found", "undefined",
            "unresolved", "no module named", "cannot resolve",
            "already been declared", "duplicate declaration", "redeclared",
        )
        marker_positions = [
            match.start()
            for marker in strong_markers
            for match in re.finditer(re.escape(marker), normalized, flags=re.I)
        ]
        if not marker_positions:
            return []
        scored: list[tuple[int, str]] = []
        for path in allowed_files:
            positions = [
                match.start()
                for match in re.finditer(re.escape(path), normalized, flags=re.I)
            ]
            if not positions:
                continue
            distance = min(abs(file_pos - marker_pos) for file_pos in positions for marker_pos in marker_positions)
            if distance <= 600:
                scored.append((distance, path))
        if not scored:
            return []
        best_distance = min(distance for distance, _ in scored)
        return [path for distance, path in scored if distance <= best_distance + 80]

    @staticmethod
    def _mr_description(task: Task, attempts: list[DevelopmentAttempt]) -> str:
        criteria = "\n".join(f"- [x] {item}" for item in task.analysis.acceptance_criteria)
        final = attempts[-1]
        files = "\n".join(f"- `{path}`" for path in final.changed_files)
        return f"""## 变更说明

{final.summary}

## 修改文件

{files}

## 验收标准

{criteria}

## 自动验证

- 尝试次数：{len(attempts)}
- 命令：`{' '.join(final.test_command)}`
- 退出码：`{final.exit_code}`
- 结果：通过

## 风险与回滚

- 变更基于规划时锁定的 Commit 和隔离 Git Worktree。
- 回滚方式：丢弃任务 Worktree，原始工作目录未被修改。
"""
