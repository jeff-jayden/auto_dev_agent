"""Celery 消息路由和 API 调度适配测试。"""

import unittest
from pathlib import Path
from unittest.mock import patch

from domain.models import ExecutionJob
from execution.celery_app import dispatch_execution_job, queue_for_action


class CeleryExecutionTests(unittest.TestCase):
    def test_beat_schedule_is_kept_in_runtime_directory(self):
        script = (Path(__file__).resolve().parents[1] / "scripts" / "start-celery-beat.ps1").read_text(encoding="utf-8")
        self.assertIn('Join-Path $projectRoot "runtime\\celery"', script)
        self.assertIn('New-Item -ItemType Directory -Path $scheduleDirectory -Force', script)
        self.assertIn('Join-Path $scheduleDirectory "celerybeat-schedule"', script)
        self.assertIn('--schedule $scheduleFile', script)

    def test_actions_are_routed_to_role_specific_queues(self):
        self.assertEqual(queue_for_action("approve"), "development")
        self.assertEqual(queue_for_action("retry_failed"), "development")
        self.assertEqual(queue_for_action("run_review"), "review")
        self.assertEqual(queue_for_action("github_comment_feedback"), "github")

    @patch("execution.celery_app.celery_app.send_task")
    def test_dispatch_sends_only_job_id_with_deterministic_task_id(self, send_task):
        job = ExecutionJob(id="job-123", task_id="task-456", action="run_review")

        broker_task_id = dispatch_execution_job(job)

        self.assertEqual(broker_task_id, "execution-job-123")
        send_task.assert_called_once_with(
            "execution.execute_job",
            args=["job-123"],
            task_id="execution-job-123",
            queue="review",
        )


if __name__ == "__main__":
    unittest.main()
