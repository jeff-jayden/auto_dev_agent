import tempfile
import time
import unittest
from pathlib import Path
from threading import Event

from tests.support import build_orchestrator
from dev_agent.execution import TaskExecutionWorker
from dev_agent.domain.models import TaskStatus


class ExecutionWorkerCheckpointTests(unittest.TestCase):
    def test_user_feedback_is_queued_for_same_task(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = build_orchestrator(Path(directory) / "runtime")
            task = orchestrator.create_task(
                "反馈队列", "任务支持 low、medium、high，默认使用 medium，非法值必须拒绝。"
            )
            task.status = TaskStatus.WAITING_RELEASE_APPROVAL
            orchestrator.store.save_task(task)
            worker = TaskExecutionWorker(lambda: orchestrator, poll_interval=60)

            job = worker.enqueue_user_feedback(task.id, "tester", "按钮文案需要调整")

            self.assertEqual(job.task_id, task.id)
            self.assertEqual(job.action, "user_feedback")
            self.assertEqual(job.payload["feedback"], "按钮文案需要调整")
            worker.stop()

    def test_merged_task_feedback_creates_and_queues_followup_task(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = build_orchestrator(Path(directory) / "runtime")
            original = orchestrator.create_task(
                "已合入任务", "实现首页按钮并在点击后展示提示。"
            )
            original.status = TaskStatus.MERGED
            original.metadata["user_feedback_rounds"] = [
                {"feedback": "把按钮文案改得更清楚"}
            ]
            orchestrator.store.save_task(original)
            worker = TaskExecutionWorker(lambda: orchestrator, poll_interval=60)
            worker.start = lambda: None

            job = worker.enqueue_user_feedback(
                original.id, "tester", "合入后再增加一个关闭按钮"
            )

            followup = orchestrator.store.get_task(job.task_id)
            self.assertNotEqual(job.task_id, original.id)
            self.assertEqual(job.action, "approve")
            self.assertEqual(followup.metadata["followup_of"], original.id)
            self.assertEqual(
                followup.metadata["inherited_conversation"],
                ["把按钮文案改得更清楚"],
            )
            self.assertIn("合入后再增加一个关闭按钮", followup.requirement)

    def test_feedback_refreshes_remote_pull_request_before_choosing_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = build_orchestrator(Path(directory) / "runtime")
            task = orchestrator.create_task("远端状态刷新", "继续修改前检查 PR 状态。")
            task.status = TaskStatus.WAITING_MERGE_APPROVAL
            from dev_agent.domain.models import RemotePullRequest
            task.remote_pull_request = RemotePullRequest(
                repository="example/project",
                number=7,
                url="https://github.com/example/project/pull/7",
                state="open",
                draft=False,
                head_branch=f"agent/{task.id}",
                base_branch="main",
                head_sha="abc123",
            )
            orchestrator.store.save_task(task)

            class MergedDelivery:
                @staticmethod
                def refresh(record):
                    record.state = "merged"
                    record.merged_sha = "merged-sha"
                    return record

            orchestrator.github_delivery = MergedDelivery()
            worker = TaskExecutionWorker(lambda: orchestrator, poll_interval=60)
            worker.start = lambda: None

            job = worker.enqueue_user_feedback(task.id, "tester", "合入后继续修改")

            self.assertNotEqual(job.task_id, task.id)
            self.assertEqual(orchestrator.store.get_task(task.id).status, TaskStatus.MERGED)
            self.assertEqual(
                orchestrator.store.get_task(job.task_id).metadata["followup_of"], task.id
            )

    def test_running_job_pauses_at_patch_checkpoint_and_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = build_orchestrator(Path(directory) / "runtime")
            original_developer = orchestrator.generic_developer
            implementation_started = Event()
            allow_implementation_to_finish = Event()

            class BlockingDeveloper:
                model_gateway = original_developer.model_gateway

                def run(self, *args, **kwargs):
                    implementation_started.set()
                    allow_implementation_to_finish.wait(2)
                    checkpoint_handler = kwargs.get("checkpoint_handler")
                    if checkpoint_handler:
                        kwargs["checkpoint_handler"] = lambda stage, next_action, payload: (
                            False if stage == "proposal_ready"
                            else checkpoint_handler(stage, next_action, payload)
                        )
                    return original_developer.run(*args, **kwargs)

            orchestrator.generic_developer = BlockingDeveloper()
            task = orchestrator.create_task(
                "暂停恢复回归",
                "任务支持 low、medium、high，默认使用 medium，非法值必须拒绝。",
            )
            worker = TaskExecutionWorker(
                lambda: orchestrator,
                poll_interval=0.01,
                heartbeat_interval=0.02,
                lease_seconds=0.25,
            )
            try:
                job = worker.enqueue_approval(task.id, "tester", "同意")
                self.assertTrue(implementation_started.wait(2))
                time.sleep(0.04)
                running = orchestrator.store.get_job(job.id)
                self.assertEqual(running.status, "running")
                self.assertIsNotNone(running.worker_id)
                self.assertIsNotNone(running.heartbeat_at)
                self.assertEqual(running.current_stage, "developing")
                requested = worker.cancel(job.id)
                self.assertEqual(requested.status, "pause_requested")
                allow_implementation_to_finish.set()

                paused = self._wait_for_job(orchestrator, job.id, {"paused"})
                self.assertEqual(paused.status, "paused")
                checkpoint = orchestrator.store.latest_checkpoint(task.id)
                self.assertEqual(checkpoint.stage, "patch_applied")
                self.assertEqual(checkpoint.next_action, "run_tests")

                resumed = worker.resume(job.id, "tester")
                completed = self._wait_for_job(orchestrator, resumed.id, {"succeeded", "failed"})
                self.assertEqual(completed.status, "succeeded", completed.error)
                final_task = orchestrator.store.get_task(task.id)
                self.assertEqual(final_task.status.value, "waiting_release_approval")
            finally:
                allow_implementation_to_finish.set()
                worker.stop()

    @staticmethod
    def _wait_for_job(orchestrator, job_id, terminal_statuses):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = orchestrator.store.get_job(job_id)
            if job and job.status in terminal_statuses:
                return job
            time.sleep(0.02)
        raise AssertionError(f"Job {job_id} did not reach {terminal_statuses}")


if __name__ == "__main__":
    unittest.main()
