import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api import main
from tests.support import build_orchestrator
from dev_agent.domain.models import ExecutionJob, TaskStatus


class ApiTests(unittest.TestCase):
    def test_api_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            isolated = build_orchestrator(Path(directory) / "runtime")
            with patch.object(main, "orchestrator", isolated):
                with TestClient(main.app) as client:
                    health = client.get("/health")
                    self.assertEqual(health.status_code, 200)
                    self.assertEqual(health.json()["phase"], 7)
                    self.assertIn("github_enabled", health.json())
                    repositories = client.get("/api/repositories")
                    self.assertEqual(repositories.status_code, 200)
                    self.assertEqual(repositories.json()[0]["id"], "test-repository")
                    repository_analysis = client.get(
                        "/api/repositories/test-repository/analysis",
                        params={"requirement": "增加 priority 优先级"},
                    )
                    self.assertEqual(repository_analysis.status_code, 200)
                    self.assertEqual(repository_analysis.json()["language"], "python")
                    response = client.post(
                        "/api/tasks",
                        json={
                            "title": "为任务增加优先级",
                            "requirement": "任务支持 low、medium、high，未提供时默认使用 medium。",
                            "repository_id": "test-repository",
                        },
                    )
                    self.assertEqual(response.status_code, 201)
                    task = response.json()
                    self.assertEqual(task["status"], "waiting_requirement_approval")

                    started = time.monotonic()
                    approval = client.post(
                        f"/api/tasks/{task['id']}/approve",
                        json={"actor": "api-tester", "comment": "同意"},
                    )
                    self.assertEqual(approval.status_code, 202)
                    self.assertLess(time.monotonic() - started, 1.0)
                    job = approval.json()
                    self.assertIn(job["status"], {"queued", "running"})

                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        job = client.get(f"/api/jobs/{job['id']}").json()
                        if job["status"] in {"succeeded", "failed", "cancelled"}:
                            break
                        time.sleep(0.05)
                    self.assertEqual(job["status"], "succeeded")
                    completed = client.get(f"/api/tasks/{task['id']}").json()
                    self.assertEqual(completed["status"], "waiting_release_approval")
                    self.assertIsNotNone(completed["merge_request"])
                    self.assertEqual(completed["reviews"][-1]["decision"], "approved")

                    merge_request = client.get(f"/api/tasks/{task['id']}/merge-request")
                    self.assertEqual(merge_request.status_code, 200)
                    self.assertIn("acceptance_checklist", merge_request.json())
                    reviews = client.get(f"/api/tasks/{task['id']}/reviews")
                    self.assertEqual(reviews.status_code, 200)
                    self.assertEqual(reviews.json()[0]["decision"], "approved")

                    jobs = client.get(f"/api/tasks/{task['id']}/jobs")
                    self.assertEqual(jobs.status_code, 200)
                    self.assertEqual(jobs.json()[0]["id"], job["id"])
                    events = client.get(f"/api/tasks/{task['id']}/events")
                    self.assertEqual(events.status_code, 200)
                    event_types = [event["event_type"] for event in events.json()]
                    self.assertIn("execution_queued", event_types)
                    self.assertIn("execution_started", event_types)
                    self.assertEqual(event_types[-1], "execution_finished")
                    checkpoints = client.get(
                        f"/api/tasks/{task['id']}/checkpoints"
                    ).json()
                    checkpoint_stages = [item["stage"] for item in checkpoints]
                    self.assertIn("plan_approved", checkpoint_stages)
                    self.assertIn("proposal_ready", checkpoint_stages)
                    self.assertIn("patch_applied", checkpoint_stages)
                    self.assertIn("test_result_saved", checkpoint_stages)
                    self.assertIn("review_round_saved", checkpoint_stages)

                    traces = client.get(f"/api/tasks/{task['id']}/traces")
                    self.assertEqual(traces.status_code, 200)
                    self.assertGreaterEqual(len(traces.json()), 2)
                    trace_detail = client.get(f"/api/traces/{traces.json()[0]['id']}")
                    self.assertEqual(trace_detail.status_code, 200)
                    self.assertIn("spans", trace_detail.json())
                    replay = client.post(
                        f"/api/tasks/{task['id']}/checkpoints/{checkpoints[-1]['id']}/replay"
                    )
                    self.assertEqual(replay.status_code, 200)
                    self.assertTrue(replay.json()["valid"])
                    evaluation = client.post("/api/evaluations/run")
                    self.assertEqual(evaluation.status_code, 200)
                    self.assertEqual(evaluation.json()["score"], 100.0)
                    metrics = client.get("/api/metrics")
                    self.assertEqual(metrics.status_code, 200)
                    self.assertGreaterEqual(metrics.json()["trace_count"], 3)

                    resumable_task = client.post(
                        "/api/tasks",
                        json={
                            "title": "恢复被取消的执行",
                            "requirement": "任务支持 low、medium、high，默认使用 medium，非法值必须拒绝。",
                            "repository_id": "test-repository",
                        },
                    ).json()
                    cancelled = isolated.store.enqueue_job(
                        ExecutionJob(
                            id="cancelled-job",
                            task_id=resumable_task["id"],
                            action="approve",
                            status="cancelled",
                            payload={"actor": "api-tester", "comment": "原审批"},
                        )
                    )
                    resumed_response = client.post(
                        f"/api/jobs/{cancelled.id}/resume",
                        json={"actor": "api-tester", "comment": "继续执行"},
                    )
                    self.assertEqual(resumed_response.status_code, 202)
                    resumed = resumed_response.json()
                    self.assertEqual(resumed["action"], "resume")
                    self.assertEqual(resumed["payload"]["previous_job_id"], cancelled.id)

                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        resumed = client.get(f"/api/jobs/{resumed['id']}").json()
                        if resumed["status"] in {"succeeded", "failed", "cancelled"}:
                            break
                        time.sleep(0.05)
                    self.assertEqual(resumed["status"], "succeeded")
                    resumed_task = client.get(f"/api/tasks/{resumable_task['id']}").json()
                    self.assertEqual(resumed_task["status"], "waiting_release_approval")
                    resumed_events = client.get(
                        f"/api/tasks/{resumable_task['id']}/events"
                    ).json()
                    self.assertIn(
                        "execution_resumed",
                        [event["event_type"] for event in resumed_events],
                    )

    def test_review_actions_are_blocked_while_background_job_is_active(self):
        with tempfile.TemporaryDirectory() as directory:
            isolated = build_orchestrator(Path(directory) / "runtime")
            with patch.object(main, "orchestrator", isolated), \
                    patch.object(main.execution_worker, "start"), \
                    patch.object(main.execution_worker, "stop"):
                with TestClient(main.app) as client:
                    task = isolated.create_task(
                        "审查并发保护",
                        "验证后台恢复执行期间不能并发启动重新审查或人工批准。",
                    )
                    task.status = TaskStatus.CHANGES_REQUESTED
                    isolated.store.save_task(task)
                    isolated.store.enqueue_job(ExecutionJob(
                        id="active-review-job",
                        task_id=task.id,
                        action="resume",
                        status="running",
                    ))

                    rerun = client.post(f"/api/tasks/{task.id}/review/run")
                    approve = client.post(
                        f"/api/tasks/{task.id}/review/approve",
                        json={"actor": "api-tester", "comment": "人工批准"},
                    )

                    self.assertEqual(rerun.status_code, 409)
                    self.assertIn("正在后台执行", rerun.json()["detail"])
                    self.assertEqual(approve.status_code, 409)
                    self.assertIn("不能同时人工批准", approve.json()["detail"])
