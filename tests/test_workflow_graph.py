import sqlite3
import tempfile
import unittest
from pathlib import Path

from domain.models import Task, TaskCheckpoint, TaskStatus
from workflows.workflow_graph import WorkflowStateStore


class WorkflowStateStoreTests(unittest.TestCase):
    def test_sqlite_graph_is_transition_source_and_task_status_is_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.db"
            graph = WorkflowStateStore.sqlite(database)
            task = Task(
                id="graph-task",
                title="迁移状态",
                requirement="使用 LangGraph 保存唯一的工作流执行状态",
                status=TaskStatus.REQUIREMENT_ANALYSIS,
            )

            seeded = graph.seed(task)
            transitioned = graph.transition(task, TaskStatus.WAITING_REQUIREMENT_APPROVAL)

            self.assertEqual(seeded["phase"], "requirement_analysis")
            self.assertEqual(transitioned["phase"], "waiting_requirement_approval")
            self.assertEqual(transitioned["pending_human_action"], "requirement_approval")
            # Projection is intentionally updated by TaskOrchestrator only.
            self.assertEqual(task.status, TaskStatus.REQUIREMENT_ANALYSIS)
            graph.close()

            connection = sqlite3.connect(database)
            try:
                checkpoint_count = connection.execute(
                    "SELECT COUNT(*) FROM checkpoints"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertGreaterEqual(checkpoint_count, 2)

    def test_legacy_checkpoint_is_imported_once(self):
        graph = WorkflowStateStore.memory()
        task = Task(
            id="legacy-task",
            title="恢复旧任务",
            requirement="把旧检查点迁移到 LangGraph State",
            status=TaskStatus.DEVELOPING,
            workspace="C:/worktree",
        )
        checkpoint = TaskCheckpoint(
            id="checkpoint-1",
            task_id=task.id,
            job_id="job-1",
            stage="patch_applied",
            next_action="run_tests",
            plan_hash="plan",
            diff_hash="diff",
            payload={"changed_files": ["src/App.js"]},
        )

        first = graph.migrate([task], lambda _task_id: checkpoint)
        second = graph.migrate([task], lambda _task_id: checkpoint)
        state = graph.get(task.id)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(state["resume_stage"], "patch_applied")
        self.assertEqual(state["next_action"], "run_tests")
        self.assertEqual(state["checkpoint_payload"]["changed_files"], ["src/App.js"])
        self.assertTrue(state["migrated_from_legacy"])

    def test_new_checkpoint_is_written_to_graph_before_projection(self):
        graph = WorkflowStateStore.memory()
        task = Task(
            id="checkpoint-task",
            title="保存图检查点",
            requirement="使用 LangGraph State 保存恢复信息",
            status=TaskStatus.DEVELOPING,
        )
        graph.seed(task)
        checkpoint = TaskCheckpoint(
            id="checkpoint-graph",
            task_id=task.id,
            job_id="job-graph",
            stage="test_result_saved",
            next_action="generate_mr",
            plan_hash="plan-hash",
            diff_hash="diff-hash",
            payload={"success": True},
        )

        graph.record_checkpoint(task, checkpoint)
        restored = graph.latest_checkpoint(task)

        self.assertEqual(restored.id, checkpoint.id)
        self.assertEqual(restored.stage, "test_result_saved")
        self.assertEqual(restored.payload, {"success": True})

    def test_waiting_phase_is_durable_interrupt_and_transition_resumes_it(self):
        graph = WorkflowStateStore.memory()
        task = Task(
            id="human-action-task",
            title="等待人工审批",
            requirement="审批后继续执行",
            status=TaskStatus.WAITING_REQUIREMENT_APPROVAL,
        )

        graph.seed(task)

        pending = graph.pending_human_action(task.id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["action"], "requirement_approval")
        self.assertEqual(pending["phase"], "waiting_requirement_approval")

        state = graph.transition(task, TaskStatus.PLAN_APPROVED)

        self.assertEqual(state["phase"], "plan_approved")
        self.assertIsNone(state["pending_human_action"])
        self.assertEqual(state["last_human_decision"]["target"], "plan_approved")
        self.assertIsNone(graph.pending_human_action(task.id))


if __name__ == "__main__":
    unittest.main()
