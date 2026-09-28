from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from domain.models import Task, TaskCheckpoint, TaskStatus


ALLOWED_PHASE_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.REQUIREMENT_ANALYSIS: {
        TaskStatus.WAITING_REQUIREMENT_APPROVAL,
        TaskStatus.WAITING_REQUIREMENT_INPUT,
        TaskStatus.FAILED,
    },
    TaskStatus.WAITING_REQUIREMENT_INPUT: {
        TaskStatus.REQUIREMENT_ANALYSIS,
        TaskStatus.REJECTED,
    },
    TaskStatus.WAITING_REQUIREMENT_APPROVAL: {
        TaskStatus.REQUIREMENT_ANALYSIS,
        TaskStatus.PLAN_APPROVED,
        TaskStatus.DEVELOPING,
        TaskStatus.REJECTED,
    },
    TaskStatus.PLAN_APPROVED: {TaskStatus.DEVELOPING},
    TaskStatus.DEVELOPING: {
        TaskStatus.WAITING_REQUIREMENT_APPROVAL,
        TaskStatus.WAITING_RISK_APPROVAL,
        TaskStatus.TESTING,
        TaskStatus.FAILED,
    },
    TaskStatus.WAITING_RISK_APPROVAL: {
        TaskStatus.DEVELOPING,
        TaskStatus.REVIEW_REPAIRING,
        TaskStatus.REJECTED,
    },
    TaskStatus.TESTING: {
        TaskStatus.REPAIRING,
        TaskStatus.CHANGE_READY,
        TaskStatus.FAILED,
    },
    TaskStatus.REPAIRING: {
        TaskStatus.TESTING,
        TaskStatus.WAITING_RISK_APPROVAL,
        TaskStatus.FAILED,
    },
    TaskStatus.CHANGE_READY: {TaskStatus.GENERATING_MR},
    TaskStatus.GENERATING_MR: {TaskStatus.REVIEWING, TaskStatus.FAILED},
    TaskStatus.REVIEWING: {
        TaskStatus.CHANGES_REQUESTED,
        TaskStatus.REVIEW_APPROVED,
        TaskStatus.FAILED,
    },
    TaskStatus.CHANGES_REQUESTED: {
        TaskStatus.REVIEW_REPAIRING,
        TaskStatus.REVIEWING,
        TaskStatus.REVIEW_APPROVED,
        TaskStatus.REJECTED,
    },
    TaskStatus.REVIEW_REPAIRING: {
        TaskStatus.GENERATING_MR,
        TaskStatus.CHANGES_REQUESTED,
        TaskStatus.WAITING_RISK_APPROVAL,
        TaskStatus.FAILED,
    },
    TaskStatus.REVIEW_APPROVED: {TaskStatus.WAITING_RELEASE_APPROVAL},
    TaskStatus.WAITING_RELEASE_APPROVAL: {
        TaskStatus.CHANGES_REQUESTED,
        TaskStatus.PUBLISHING_PULL_REQUEST,
        TaskStatus.REJECTED,
    },
    TaskStatus.PUBLISHING_PULL_REQUEST: {
        TaskStatus.WAITING_MERGE_APPROVAL,
        TaskStatus.WAITING_RELEASE_APPROVAL,
        TaskStatus.FAILED,
    },
    TaskStatus.WAITING_MERGE_APPROVAL: {
        TaskStatus.CHANGES_REQUESTED,
        TaskStatus.MERGING,
        TaskStatus.REJECTED,
    },
    TaskStatus.MERGING: {
        TaskStatus.MERGED,
        TaskStatus.WAITING_MERGE_APPROVAL,
        TaskStatus.FAILED,
    },
    TaskStatus.MERGED: set(),
    TaskStatus.REJECTED: set(),
    TaskStatus.FAILED: {TaskStatus.DEVELOPING},
}


def ensure_workflow_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in ALLOWED_PHASE_TRANSITIONS[current]:
        raise ValueError(f"Illegal task transition: {current.value} -> {target.value}")


class WorkflowState(TypedDict, total=False):
    """Durable execution state. Task.status is only a UI projection of phase."""

    task_id: str
    phase: str
    revision: int
    repository_id: str
    baseline_sha: str | None
    plan_hash: str | None
    workspace: str | None
    resume_stage: str | None
    saved_checkpoint_id: str | None
    saved_checkpoint_job_id: str | None
    next_action: str | None
    diff_hash: str | None
    saved_checkpoint_created_at: str | None
    checkpoint_payload: dict[str, Any]
    pending_human_action: str | None
    updated_at: str
    migrated_from_legacy: bool
    projection_reconciliations: int
    last_human_decision: dict[str, Any]


WAITING_ACTIONS = {
    TaskStatus.WAITING_REQUIREMENT_APPROVAL: "requirement_approval",
    TaskStatus.WAITING_REQUIREMENT_INPUT: "requirement_input",
    TaskStatus.WAITING_RISK_APPROVAL: "risk_approval",
    TaskStatus.WAITING_RELEASE_APPROVAL: "publish_approval",
    TaskStatus.WAITING_MERGE_APPROVAL: "merge_approval",
}


def _project_state(state: WorkflowState) -> WorkflowState:
    # A real graph node gives every seed a LangGraph checkpoint. Business
    # execution nodes will replace this compatibility projection incrementally.
    return state


def _await_human_action(state: WorkflowState) -> WorkflowState:
    decision = interrupt({
        "task_id": state["task_id"],
        "phase": state["phase"],
        "action": state.get("pending_human_action"),
        "revision": state.get("revision", 0),
    })
    return {
        "last_human_decision": dict(decision),
        "pending_human_action": None,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _route_after_projection(state: WorkflowState) -> str:
    return "await_human_action" if state.get("pending_human_action") else "done"


class WorkflowStateStore:
    """LangGraph-backed source of truth for task execution phase.

    ExecutionJob remains an infrastructure lease/queue record. The Task model
    keeps a projected status for API compatibility, but transitions are first
    persisted here and only then copied to Task.status.
    """

    def __init__(self, checkpointer, connection: sqlite3.Connection | None = None):
        builder = StateGraph(WorkflowState)
        builder.add_node("project_state", _project_state)
        builder.add_node("await_human_action", _await_human_action)
        builder.add_edge(START, "project_state")
        builder.add_conditional_edges(
            "project_state",
            _route_after_projection,
            {"await_human_action": "await_human_action", "done": END},
        )
        builder.add_edge("await_human_action", END)
        self.graph = builder.compile(checkpointer=checkpointer, name="task-workflow")
        self._connection = connection
        self._lock = RLock()

    @classmethod
    def sqlite(cls, database_path: Path) -> "WorkflowStateStore":
        connection = sqlite3.connect(database_path, check_same_thread=False)
        saver = SqliteSaver(connection)
        saver.setup()
        return cls(saver, connection)

    @classmethod
    def memory(cls) -> "WorkflowStateStore":
        return cls(InMemorySaver())

    @staticmethod
    def _config(task_id: str) -> dict:
        return {"configurable": {"thread_id": task_id}}

    def get(self, task_id: str) -> WorkflowState | None:
        with self._lock:
            snapshot = self.graph.get_state(self._config(task_id))
        values = dict(snapshot.values) if snapshot and snapshot.values else {}
        return values or None

    def seed(
        self,
        task: Task,
        checkpoint: TaskCheckpoint | None = None,
        *,
        migrated_from_legacy: bool = False,
    ) -> WorkflowState:
        existing = self.get(task.id)
        if existing is not None:
            return existing
        state: WorkflowState = {
            "task_id": task.id,
            "phase": task.status.value,
            "revision": 1,
            "repository_id": task.repository_id or "",
            "baseline_sha": (
                task.repository_analysis.head_sha if task.repository_analysis else None
            ),
            "plan_hash": task.metadata.get("plan_hash"),
            "workspace": task.workspace,
            "resume_stage": checkpoint.stage if checkpoint else None,
            "saved_checkpoint_id": checkpoint.id if checkpoint else None,
            "saved_checkpoint_job_id": checkpoint.job_id if checkpoint else None,
            "next_action": checkpoint.next_action if checkpoint else None,
            "diff_hash": checkpoint.diff_hash if checkpoint else None,
            "saved_checkpoint_created_at": (
                checkpoint.created_at.isoformat() if checkpoint else None
            ),
            "checkpoint_payload": dict(checkpoint.payload) if checkpoint else {},
            "pending_human_action": WAITING_ACTIONS.get(task.status),
            "updated_at": datetime.now(UTC).isoformat(),
            "migrated_from_legacy": migrated_from_legacy,
            "projection_reconciliations": 0,
        }
        with self._lock:
            self.graph.invoke(state, self._config(task.id))
        return self.get(task.id) or state

    def transition(
        self,
        task: Task,
        target: TaskStatus,
        *,
        checkpoint: TaskCheckpoint | None = None,
    ) -> WorkflowState:
        current = self.get(task.id) or self.seed(task, checkpoint)
        current_phase = TaskStatus(current["phase"])
        # Compatibility bridge for legacy code/tests that still write the
        # projected Task.status directly. Record the divergence in LangGraph
        # before validating the next edge; these writers are removed in the
        # later cut-over slices.
        if current_phase != task.status:
            reconciliations = int(current.get("projection_reconciliations", 0)) + 1
            with self._lock:
                self.graph.update_state(
                    self._config(task.id),
                    {
                        "phase": task.status.value,
                        "projection_reconciliations": reconciliations,
                        "updated_at": datetime.now(UTC).isoformat(),
                    },
                    as_node="project_state",
                )
            current = self.get(task.id) or current
            current_phase = task.status
        ensure_workflow_transition(current_phase, target)
        if current.get("pending_human_action"):
            with self._lock:
                self.graph.invoke(
                    Command(resume={
                        "target": target.value,
                        "action": current["pending_human_action"],
                        "resumed_at": datetime.now(UTC).isoformat(),
                    }),
                    self._config(task.id),
                )
            current = self.get(task.id) or current
        update: WorkflowState = {
            "phase": target.value,
            "revision": int(current.get("revision", 0)) + 1,
            "baseline_sha": (
                task.repository_analysis.head_sha if task.repository_analysis else None
            ),
            "plan_hash": task.metadata.get("plan_hash"),
            "workspace": task.workspace,
            "resume_stage": checkpoint.stage if checkpoint else current.get("resume_stage"),
            "next_action": checkpoint.next_action if checkpoint else current.get("next_action"),
            "checkpoint_payload": (
                dict(checkpoint.payload) if checkpoint else current.get("checkpoint_payload", {})
            ),
            "pending_human_action": WAITING_ACTIONS.get(target),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        with self._lock:
            self.graph.update_state(
                self._config(task.id), update, as_node="project_state"
            )
        return self.get(task.id) or {**current, **update}

    def pending_human_action(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self.graph.get_state(self._config(task_id))
        if not snapshot or "await_human_action" not in snapshot.next:
            return None
        interrupts = getattr(snapshot, "interrupts", ())
        if not interrupts:
            return None
        return dict(interrupts[0].value)

    def record_checkpoint(self, task: Task, checkpoint: TaskCheckpoint) -> WorkflowState:
        current = self.get(task.id) or self.seed(task)
        update: WorkflowState = {
            "revision": int(current.get("revision", 0)) + 1,
            "baseline_sha": checkpoint.baseline_sha,
            "plan_hash": checkpoint.plan_hash,
            "workspace": checkpoint.workspace,
            "resume_stage": checkpoint.stage,
            "saved_checkpoint_id": checkpoint.id,
            "saved_checkpoint_job_id": checkpoint.job_id,
            "next_action": checkpoint.next_action,
            "diff_hash": checkpoint.diff_hash,
            "saved_checkpoint_created_at": checkpoint.created_at.isoformat(),
            "checkpoint_payload": dict(checkpoint.payload),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        with self._lock:
            self.graph.update_state(
                self._config(task.id), update, as_node="project_state"
            )
        return self.get(task.id) or {**current, **update}

    def latest_checkpoint(self, task: Task) -> TaskCheckpoint | None:
        state = self.get(task.id)
        if not state or not state.get("saved_checkpoint_id") or not state.get("resume_stage"):
            return None
        created_at = state.get("saved_checkpoint_created_at")
        return TaskCheckpoint(
            id=str(state["saved_checkpoint_id"]),
            task_id=task.id,
            job_id=str(state.get("saved_checkpoint_job_id") or "graph-migration"),
            stage=str(state["resume_stage"]),
            next_action=str(state.get("next_action") or "resume"),
            workspace=state.get("workspace"),
            baseline_sha=state.get("baseline_sha"),
            plan_hash=str(state.get("plan_hash") or ""),
            diff_hash=str(state.get("diff_hash") or ""),
            payload=dict(state.get("checkpoint_payload") or {}),
            **({"created_at": datetime.fromisoformat(created_at)} if created_at else {}),
        )

    def migrate(self, tasks: list[Task], checkpoint_loader) -> int:
        migrated = 0
        for task in tasks:
            if self.get(task.id) is not None:
                continue
            self.seed(
                task,
                checkpoint_loader(task.id),
                migrated_from_legacy=True,
            )
            migrated += 1
        return migrated

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()


class TaskDeliveryState(TypedDict, total=False):
    action: str
    task_id: str
    title: str
    requirement: str
    repository_id: str
    figma_url: str
    preview_url: str
    viewport_width: int
    viewport_height: int
    actor: str
    comment: str
    checkpoint_job_id: str | None
    should_pause: Callable[[], bool] | None
    checkpoint_handler: Callable[[str, str, dict], bool] | None
    task: Task
    repository: Any
    route: str


class TaskDeliveryGraph:
    """Top-level graph deciding which delivery or agent node runs next."""

    def __init__(self, adapter):
        self.adapter = adapter
        builder = StateGraph(TaskDeliveryState)
        builder.add_node("dispatch", lambda state: state)
        builder.add_node("create_task", self.adapter._graph_create_task)
        builder.add_node("analyze_repository", self.adapter._graph_analyze_repository)
        builder.add_node("plan", self.adapter._graph_plan)
        builder.add_node("approve_plan", self.adapter._graph_approve_plan)
        builder.add_node("load_review", self.adapter._graph_load_review)
        builder.add_node("develop", self.adapter._graph_develop)
        builder.add_node("generate_mr", self.adapter._graph_generate_mr)
        builder.add_node("review", self.adapter._graph_review)
        builder.add_edge(START, "dispatch")
        builder.add_conditional_edges(
            "dispatch",
            lambda state: state["action"],
            {
                "create": "create_task",
                "approve": "approve_plan",
                "review": "load_review",
            },
        )
        builder.add_edge("create_task", "analyze_repository")
        builder.add_edge("analyze_repository", "plan")
        builder.add_edge("plan", END)
        builder.add_conditional_edges(
            "approve_plan",
            self._route_after_approval,
            {"develop": "develop", "done": END},
        )
        builder.add_conditional_edges(
            "load_review",
            self._route_review_entry,
            {"generate_mr": "generate_mr", "review": "review"},
        )
        builder.add_conditional_edges(
            "develop",
            self._route_after_development,
            {"generate_mr": "generate_mr", "done": END},
        )
        builder.add_edge("generate_mr", "review")
        builder.add_edge("review", END)
        self.graph = builder.compile(name="task-delivery-workflow")

    def create(
        self,
        title: str,
        requirement: str,
        repository_id: str,
        *,
        figma_url: str = "",
        preview_url: str = "",
        viewport_width: int = 1440,
        viewport_height: int = 900,
    ) -> Task:
        result = self.graph.invoke({
            "action": "create",
            "title": title,
            "requirement": requirement,
            "repository_id": repository_id,
            "figma_url": figma_url,
            "preview_url": preview_url,
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
        })
        return result["task"]

    def approve(
        self,
        task_id: str,
        actor: str,
        comment: str = "",
        *,
        checkpoint_job_id: str | None = None,
        should_pause: Callable[[], bool] | None = None,
    ) -> Task:
        result = self.graph.invoke({
            "action": "approve",
            "task_id": task_id,
            "actor": actor,
            "comment": comment,
            "checkpoint_job_id": checkpoint_job_id,
            "should_pause": should_pause,
        })
        return result["task"]

    def review(self, task_id: str) -> Task:
        result = self.graph.invoke({"action": "review", "task_id": task_id})
        return result["task"]

    @staticmethod
    def _route_after_approval(state: TaskDeliveryState) -> str:
        if state.get("route") == "done":
            return "done"
        return "develop" if state["task"].status == TaskStatus.DEVELOPING else "done"

    @staticmethod
    def _route_after_development(state: TaskDeliveryState) -> str:
        return "generate_mr" if state["task"].status == TaskStatus.CHANGE_READY else "done"

    @staticmethod
    def _route_review_entry(state: TaskDeliveryState) -> str:
        return (
            "generate_mr"
            if state["task"].status == TaskStatus.CHANGE_READY
            else "review"
        )
