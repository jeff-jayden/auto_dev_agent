import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dev_agent.domain.models import ExecutionJob
from dev_agent.infrastructure.store import SQLiteTaskStore


class ExecutionJobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = SQLiteTaskStore(Path(self.temporary_directory.name) / "agent.db")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_enqueue_is_idempotent_for_same_active_action(self):
        first = self.store.enqueue_job(ExecutionJob(id="job-1", task_id="task-1", action="approve"))
        second = self.store.enqueue_job(ExecutionJob(id="job-2", task_id="task-1", action="approve"))

        self.assertEqual(first.id, "job-1")
        self.assertEqual(second.id, "job-1")
        self.assertEqual(len(self.store.list_jobs("task-1")), 1)

    def test_recover_running_job_requeues_within_retry_budget(self):
        self.store.enqueue_job(ExecutionJob(id="job-1", task_id="task-1", action="approve"))
        running = self.store.claim_next_job()
        self.assertEqual(running.status, "running")

        recovered = self.store.recover_incomplete_jobs()
        job = self.store.get_job("job-1")

        self.assertEqual(recovered, 1)
        self.assertEqual(job.status, "queued")
        self.assertIn("Recovered", job.error)

    def test_cancel_queued_job(self):
        self.store.enqueue_job(ExecutionJob(id="job-1", task_id="task-1", action="approve"))

        job = self.store.request_job_cancel("job-1")

        self.assertEqual(job.status, "cancelled")
        self.assertIsNotNone(job.finished_at)

    def test_cancel_running_job_requests_safe_pause(self):
        self.store.enqueue_job(ExecutionJob(id="job-1", task_id="task-1", action="approve"))
        self.store.claim_next_job()

        job = self.store.request_job_cancel("job-1")

        self.assertEqual(job.status, "pause_requested")
        self.assertIsNone(job.finished_at)

    def test_recovery_fails_job_after_retry_budget(self):
        job = ExecutionJob(
            id="job-1",
            task_id="task-1",
            action="approve",
            status="running",
            attempts=2,
            started_at=datetime.now(UTC),
        )
        self.store.enqueue_job(job)

        self.store.recover_incomplete_jobs()
        recovered = self.store.get_job("job-1")

        self.assertEqual(recovered.status, "failed")
        self.assertIsNotNone(recovered.finished_at)

    def test_recovery_preserves_requested_cancellation(self):
        job = ExecutionJob(
            id="job-1",
            task_id="task-1",
            action="approve",
            status="cancel_requested",
            attempts=1,
        )
        self.store.enqueue_job(job)

        self.store.recover_incomplete_jobs()
        recovered = self.store.get_job("job-1")

        self.assertEqual(recovered.status, "cancelled")
        self.assertIsNotNone(recovered.finished_at)

    def test_recovery_uses_injected_graph_checkpoint_lookup(self):
        job = ExecutionJob(
            id="job-graph-checkpoint",
            task_id="task-graph-checkpoint",
            action="approve",
            status="pause_requested",
            attempts=1,
        )
        self.store.enqueue_job(job)

        self.store.recover_incomplete_jobs(
            lambda task_id: task_id == "task-graph-checkpoint"
        )
        recovered = self.store.get_job(job.id)

        self.assertEqual(recovered.status, "paused")
        self.assertIsNotNone(recovered.finished_at)

    def test_valid_worker_lease_is_not_recovered_on_startup(self):
        self.store.enqueue_job(ExecutionJob(id="job-1", task_id="task-1", action="approve"))
        running = self.store.claim_next_job("worker-a", lease_seconds=90)

        recovered_count = self.store.recover_incomplete_jobs()
        persisted = self.store.get_job(running.id)

        self.assertEqual(recovered_count, 0)
        self.assertEqual(persisted.status, "running")
        self.assertEqual(persisted.worker_id, "worker-a")

    def test_heartbeat_only_extends_the_owning_workers_lease(self):
        self.store.enqueue_job(ExecutionJob(id="job-1", task_id="task-1", action="approve"))
        running = self.store.claim_next_job("worker-a", lease_seconds=1)
        original_expiry = running.lease_expires_at

        rejected = self.store.heartbeat_job("job-1", "worker-b", "testing", 90)
        accepted = self.store.heartbeat_job("job-1", "worker-a", "testing", 90)

        self.assertEqual(rejected.worker_id, "worker-a")
        self.assertEqual(accepted.current_stage, "testing")
        self.assertGreater(accepted.lease_expires_at, original_expiry)

    def test_stale_job_is_atomically_replaced_by_one_recovery_job(self):
        stale = ExecutionJob(
            id="job-old",
            task_id="task-1",
            action="approve",
            status="running",
            worker_id="worker-old",
            heartbeat_at=datetime.now(UTC) - timedelta(minutes=2),
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        self.store.enqueue_job(stale)
        replacement = ExecutionJob(
            id="job-new", task_id="task-1", action="resume"
        )

        created = self.store.replace_stale_job_with_recovery("job-old", replacement)
        duplicate = self.store.replace_stale_job_with_recovery(
            "job-old", ExecutionJob(id="job-other", task_id="task-1", action="resume")
        )

        self.assertEqual(created.id, "job-new")
        self.assertEqual(duplicate.id, "job-new")
        old = self.store.get_job("job-old")
        self.assertEqual(old.status, "abandoned")
        self.assertEqual(old.failure_kind, "worker_lease_expired")
        self.assertEqual(old.recovery_job_id, "job-new")


if __name__ == "__main__":
    unittest.main()
