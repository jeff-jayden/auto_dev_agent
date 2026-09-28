import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.support import build_orchestrator
from dev_agent.execution import TaskExecutionWorker
from dev_agent.domain.models import ExecutionJob
from dev_agent.workflows import RecoveryCoordinator


class RecoveryCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.orchestrator = build_orchestrator(
            Path(self.temp_directory.name) / "runtime"
        )

    def tearDown(self):
        self.temp_directory.cleanup()

    def _interrupted_task(self):
        task = self.orchestrator.create_task(
            "恢复诊断示例",
            "验证执行中断后能够诊断最近检查点并安全创建恢复任务。",
        )
        return self.orchestrator.approve(
            task.id,
            "tester",
            "同意",
            checkpoint_job_id="interrupted-job",
            should_pause=lambda: True,
        )

    def test_diagnoses_interrupted_task_and_enqueues_one_recovery_job(self):
        task = self._interrupted_task()
        report = RecoveryCoordinator(self.orchestrator).diagnose(task.id)

        self.assertEqual(report.status, "recoverable")
        self.assertEqual(report.checkpoint_stage, "plan_approved")
        self.assertEqual(report.next_action, "prepare_workspace")

        worker = TaskExecutionWorker(lambda: self.orchestrator, poll_interval=60)
        worker.start = lambda: None
        first = worker.enqueue_recovery(task.id, "tester")
        second = worker.enqueue_recovery(task.id, "tester")

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.action, "resume")
        self.assertEqual(first.payload["checkpoint_id"], report.checkpoint_id)
        event_types = [
            event.event_type for event in self.orchestrator.store.list_events(task.id)
        ]
        self.assertEqual(event_types.count("recovery_queued"), 1)

    def test_blocks_recovery_when_plan_changed_after_checkpoint(self):
        task = self._interrupted_task()
        task.technical_plan.approach += " 已发生漂移"
        self.orchestrator.store.save_task(task)

        report = RecoveryCoordinator(self.orchestrator).diagnose(task.id)

        self.assertEqual(report.status, "blocked")
        self.assertIn("不一致", report.reason)
        self.assertTrue(any(check.status == "blocked" for check in report.checks))

    def test_stale_worker_is_recoverable_and_fenced_before_resume(self):
        task = self._interrupted_task()
        stale = ExecutionJob(
            id="stale-job",
            task_id=task.id,
            action="approve",
            status="running",
            attempts=1,
            worker_id="dead-worker",
            heartbeat_at=datetime.now(UTC) - timedelta(minutes=3),
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
            current_stage="testing",
        )
        self.orchestrator.store.enqueue_job(stale)

        report = RecoveryCoordinator(self.orchestrator).diagnose(task.id)

        self.assertEqual(report.status, "recoverable")
        self.assertTrue(report.active_job_stale)
        self.assertEqual(report.current_stage, "testing")
        self.assertTrue(report.requires_confirmation)

        worker = TaskExecutionWorker(lambda: self.orchestrator, poll_interval=60)
        worker.start = lambda: None
        recovery = worker.enqueue_recovery(task.id, "tester")

        self.assertEqual(recovery.action, "resume")
        self.assertEqual(self.orchestrator.store.get_job("stale-job").status, "abandoned")


if __name__ == "__main__":
    unittest.main()
