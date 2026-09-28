"""系统核心领域模型。

集中定义任务、方案、开发结果、审查、检查点、可观测性和远端 PR 等跨层数据契约。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    REQUIREMENT_ANALYSIS = "requirement_analysis"
    WAITING_REQUIREMENT_APPROVAL = "waiting_requirement_approval"
    WAITING_REQUIREMENT_INPUT = "waiting_requirement_input"
    PLAN_APPROVED = "plan_approved"
    DEVELOPING = "developing"
    WAITING_RISK_APPROVAL = "waiting_risk_approval"
    TESTING = "testing"
    REPAIRING = "repairing"
    CHANGE_READY = "change_ready"
    GENERATING_MR = "generating_mr"
    REVIEWING = "reviewing"
    CHANGES_REQUESTED = "changes_requested"
    REVIEW_REPAIRING = "review_repairing"
    REVIEW_APPROVED = "review_approved"
    WAITING_RELEASE_APPROVAL = "waiting_release_approval"
    PUBLISHING_PULL_REQUEST = "publishing_pull_request"
    WAITING_MERGE_APPROVAL = "waiting_merge_approval"
    MERGING = "merging"
    MERGED = "merged"
    REJECTED = "rejected"
    FAILED = "failed"


class RequirementAnalysis(BaseModel):
    summary: str
    user_story: str
    acceptance_criteria: list[str]
    assumptions: list[str]
    open_questions: list[str] = Field(default_factory=list)


class DevelopmentStep(BaseModel):
    id: str
    title: str
    objective: str
    allowed_files: list[str]
    acceptance_checks: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class TechnicalPlan(BaseModel):
    approach: str
    affected_files: list[str]
    implementation_steps: list[str]
    test_plan: list[str]
    risks: list[str]
    evidence: list[CodeEvidence] = Field(default_factory=list)
    development_steps: list[DevelopmentStep] = Field(default_factory=list)


class CodeEvidence(BaseModel):
    path: str
    line: int
    snippet: str
    reason: str


class ToolCallAudit(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    summary: str
    success: bool = True


class ContextFile(BaseModel):
    path: str
    role: str
    score: int = 0
    reason: str
    symbols: list[str] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    referenced_by: list[str] = Field(default_factory=list)


class RepositoryContextPack(BaseModel):
    baseline_sha: str | None = None
    index_version: int = 1
    index_mode: str = "full"
    changed_files: list[str] = Field(default_factory=list)
    primary_files: list[str] = Field(default_factory=list)
    dependency_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    files: list[ContextFile] = Field(default_factory=list)
    retrieval_strategy: str = "hybrid_rag"


class RepositoryAnalysis(BaseModel):
    language: str
    framework: str | None = None
    default_branch: str | None = None
    head_sha: str | None = None
    file_count: int
    entrypoints: list[str] = Field(default_factory=list)
    test_directories: list[str] = Field(default_factory=list)
    test_command: str | None = None
    important_files: list[str] = Field(default_factory=list)
    evidence: list[CodeEvidence] = Field(default_factory=list)
    tool_calls: list[ToolCallAudit] = Field(default_factory=list)
    context_pack: RepositoryContextPack | None = None


class Repository(BaseModel):
    id: str
    name: str
    local_path: str
    execution_mode: str = "plan_only"
    provider: str = "local"
    remote_url: str | None = None
    github_repository: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Approval(BaseModel):
    decision: str
    actor: str
    comment: str = ""
    decided_at: datetime = Field(default_factory=utc_now)


class ExecutionResult(BaseModel):
    success: bool
    command: list[str]
    exit_code: int
    output: str
    diff: str
    mr_title: str
    mr_description: str


class PatchChange(BaseModel):
    path: str
    patch: str
    reason: str = "按批准方案修改"


class TextReplacement(BaseModel):
    path: str
    search: str
    replace: str
    reason: str = "按批准方案修改"


class DevelopmentProposal(BaseModel):
    summary: str = "按批准方案实现需求"
    changes: list[PatchChange] = Field(default_factory=list)
    replacements: list[TextReplacement] = Field(default_factory=list)
    preserved_behaviors: list[str] = Field(default_factory=list)
    intentional_removals: list[str] = Field(default_factory=list)
    test_command: str


class ReplacementDevelopmentProposal(BaseModel):
    summary: str = "按批准方案实现需求"
    replacements: list[TextReplacement] = Field(min_length=1, max_length=8)
    preserved_behaviors: list[str] = Field(min_length=1, max_length=12)
    intentional_removals: list[str] = Field(default_factory=list, max_length=12)
    test_command: str


class DevelopmentAttempt(BaseModel):
    attempt: int
    step_attempt: int | None = None
    summary: str
    changed_files: list[str]
    test_command: list[str]
    exit_code: int | None = None
    output: str = ""
    diff: str = ""
    risk_level: str = "low"
    proposal: DevelopmentProposal | None = None
    tool_calls: list[ToolCallAudit] = Field(default_factory=list)
    step_id: str | None = None
    step_title: str | None = None


class StepExecution(BaseModel):
    step_id: str
    title: str
    status: str = "pending"
    attempt_count: int = 0
    changed_files: list[str] = Field(default_factory=list)
    diff: str = ""
    test_output: str = ""
    error: str | None = None


class MergeRequestDraft(BaseModel):
    title: str
    summary: str
    description: str
    changed_files: list[str]
    acceptance_checklist: list[str]
    test_summary: str
    risks: list[str]
    rollback_plan: str
    changelog: list[str]


class ReviewFinding(BaseModel):
    severity: str
    category: str
    file: str | None = None
    line: int | None = None
    message: str
    suggestion: str = ""
    blocking: bool = False


class ReviewRound(BaseModel):
    round: int
    decision: str
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    deterministic_checks: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ReviewerModelOutput(BaseModel):
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=12)


class DesignReference(BaseModel):
    provider: str = "figma"
    url: str
    file_key: str
    node_id: str
    viewport_width: int = 1440
    viewport_height: int = 900
    preview_url: str | None = None
    context: str = ""
    variables: str = ""
    screenshot_path: str | None = None
    snapshot_hash: str
    captured_at: datetime = Field(default_factory=utc_now)


class UIAcceptanceCheck(BaseModel):
    category: str
    name: str
    status: str
    detail: str
    blocking: bool = False


class UIAcceptanceReport(BaseModel):
    status: str
    summary: str
    similarity_score: float | None = None
    checks: list[UIAcceptanceCheck] = Field(default_factory=list)
    implementation_screenshot_path: str | None = None
    diff_screenshot_path: str | None = None
    design_snapshot_hash: str
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def blocking(self) -> bool:
        return any(check.blocking and check.status == "failed" for check in self.checks)


class ExecutionJob(BaseModel):
    id: str
    task_id: str
    action: str
    status: str = "queued"
    payload: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    max_attempts: int = 2
    error: str | None = None
    result_status: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    worker_id: str | None = None
    heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None
    current_stage: str | None = None
    recovery_job_id: str | None = None
    failure_kind: str | None = None


class TaskCheckpoint(BaseModel):
    id: str
    task_id: str
    job_id: str
    stage: str
    version: int = 1
    next_action: str
    workspace: str | None = None
    baseline_sha: str | None = None
    plan_hash: str
    diff_hash: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class AgentTrace(BaseModel):
    id: str
    task_id: str | None = None
    kind: str = "execution"
    name: str
    status: str = "running"
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    duration_ms: float | None = None


class TraceSpan(BaseModel):
    id: str
    trace_id: str
    task_id: str | None = None
    parent_span_id: str | None = None
    kind: str
    name: str
    status: str = "running"
    attributes: dict[str, Any] = Field(default_factory=dict)
    input_summary: str | None = None
    output_summary: str | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    duration_ms: float | None = None


class ReplayResult(BaseModel):
    task_id: str
    checkpoint_id: str
    stage: str
    valid: bool
    next_action: str
    checks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    trace_id: str


class RecoveryCheck(BaseModel):
    name: str
    status: str
    detail: str


class RecoveryReport(BaseModel):
    task_id: str
    status: str
    reason: str
    checkpoint_id: str | None = None
    checkpoint_stage: str | None = None
    next_action: str | None = None
    requires_confirmation: bool = False
    active_job_id: str | None = None
    active_job_stale: bool = False
    worker_id: str | None = None
    current_stage: str | None = None
    heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None
    checks: list[RecoveryCheck] = Field(default_factory=list)


class EvaluationCaseResult(BaseModel):
    name: str
    passed: bool
    detail: str
    duration_ms: float


class RetrievalMetrics(BaseModel):
    recall_at_k: float
    mrr: float
    hit_at_k: float
    irrelevant_rate: float
    average_context_files: float


class RetrievalCaseResult(BaseModel):
    name: str
    query: str
    expected_files: list[str]
    baseline_files: list[str]
    hybrid_files: list[str]


class RetrievalEvaluationComparison(BaseModel):
    case_count: int
    k: int
    baseline: RetrievalMetrics
    hybrid: RetrievalMetrics
    delta: dict[str, float]
    cases: list[RetrievalCaseResult] = Field(default_factory=list)


class EvaluationRun(BaseModel):
    id: str
    status: str
    score: float
    passed: int
    total: int
    cases: list[EvaluationCaseResult] = Field(default_factory=list)
    retrieval_comparison: RetrievalEvaluationComparison | None = None
    created_at: datetime = Field(default_factory=utc_now)


class RemotePullRequest(BaseModel):
    provider: str = "github"
    repository: str
    number: int
    node_id: str | None = None
    url: str
    state: str = "open"
    draft: bool = True
    head_branch: str
    base_branch: str
    head_sha: str
    merged_sha: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Task(BaseModel):
    id: str
    title: str
    requirement: str
    repository_id: str | None = None
    status: TaskStatus
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    analysis: RequirementAnalysis | None = None
    technical_plan: TechnicalPlan | None = None
    repository_analysis: RepositoryAnalysis | None = None
    approval: Approval | None = None
    workspace: str | None = None
    result: ExecutionResult | None = None
    development_attempts: list[DevelopmentAttempt] = Field(default_factory=list)
    step_executions: list[StepExecution] = Field(default_factory=list)
    merge_request: MergeRequestDraft | None = None
    remote_pull_request: RemotePullRequest | None = None
    reviews: list[ReviewRound] = Field(default_factory=list)
    design_reference: DesignReference | None = None
    ui_acceptance: UIAcceptanceReport | None = None
    pending_proposal: DevelopmentProposal | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskEvent(BaseModel):
    id: int | None = None
    task_id: str
    event_type: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
