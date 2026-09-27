from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from dev_agent.domain.models import Task, TaskCheckpoint, TaskStatus
from dev_agent.domain.state_machine import ensure_transition


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
    next_action: str | None
    checkpoint_payload: dict[str, Any]
    pending_human_action: str | None
    updated_at: str
    migrated_from_legacy: bool
    projection_reconciliations: int


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


class WorkflowStateStore:
    """LangGraph-backed source of truth for task execution phase.

    ExecutionJob remains an infrastructure lease/queue record. The Task model
    keeps a projected status for API compatibility, but transitions are first
    persisted here and only then copied to Task.status.
    """

    def __init__(self, checkpointer, connection: sqlite3.Connection | None = None):
        builder = StateGraph(WorkflowState)
        builder.add_node("project_state", _project_state)
        builder.add_edge(START, "project_state")
        builder.add_edge("project_state", END)
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
            "repository_id": task.repository_id or "demo",
            "baseline_sha": (
                task.repository_analysis.head_sha if task.repository_analysis else None
            ),
            "plan_hash": task.metadata.get("plan_hash"),
            "workspace": task.workspace,
            "resume_stage": checkpoint.stage if checkpoint else None,
            "next_action": checkpoint.next_action if checkpoint else None,
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
        ensure_transition(current_phase, target)
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
