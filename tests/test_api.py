import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api import main
from tests.support import build_orchestrator
from domain.models import ExecutionJob, TaskStatus


def test_development_chat_live_actions_summary_and_scroll():
    """执行前端纯渲染函数，避免把历史测试或已回滚文件误报为本轮结果。"""
    import shutil
    import subprocess
    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for frontend rendering assertions")
    script = r'''
const fs = require('fs');
const assert = require('assert/strict');
const source = fs.readFileSync('apps/web/assets/app.js', 'utf8');
function load(name) {
  const start = source.indexOf(`function ${name}(`);
  let end = source.indexOf('\nfunction ', start + 1);
  const asyncEnd = source.indexOf('\nasync function ', start + 1);
  if (end < 0 || (asyncEnd >= 0 && asyncEnd < end)) end = asyncEnd;
  return source.slice(start, end < 0 ? undefined : end);
}
eval(['escapeHtml', 'describeAgentActivity', 'analyzeAttemptFailure',
      'renderChatExecution', 'isChatNearBottom', 'scrollChatToLatest',
      'updateChatMessages'].map(load).join('\n'));
const statusLabels = {changes_requested: 'CR 要求修改'};
const jobStatusLabels = {running: '执行中', failed: '执行失败', paused: '已暂停'};
const task = {status: 'changes_requested', development_attempts: [
  {summary: '旧的开发记录', changed_files: ['reverted.js'], exit_code: 0}
], reviews: [{decision: 'changes_requested', summary: '缺少 tab 验证'}]};
const progress = {job: {id: 'job-1', status: 'running'}, spans: [
  {id: 'tool-1', kind: 'tool', name: 'tool.read_file', status: 'running',
   attributes: {path: 'src/YourVideos.js', reason: '<script>unsafe</script>'}}
]};
let html = renderChatExecution(task, progress, []);
assert.match(html, /实时执行/);
assert.match(html, /tool.read_file/);
assert.match(html, /src\/YourVideos.js/);
assert(!html.includes('<script>unsafe</script>'));
assert(html.includes('&lt;script&gt;'));
assert(!html.includes('执行总结'));
progress.job = {id: 'job-1', status: 'failed', error: 'Automatic repair exhausted after 3 attempts'};
html = renderChatExecution(task, progress, [{path: 'actual.js'}]);
assert.match(html, /执行总结/);
assert.match(html, /actual.js/);
assert(!html.includes('reverted.js'));
assert.match(html, /没有测试记录/);
assert.match(html, /自动修复次数已用尽/);
assert.match(html, /缺少 tab 验证/);
progress.spans.push({kind: 'tool', name: 'tool.run_tests', status: 'succeeded', attributes: {exit_code: 1}});
assert.match(renderChatExecution(task, progress, []), /退出码 1（未通过）/);
progress.job.status = 'paused';
assert.match(renderChatExecution(task, progress, null), /差异暂未加载成功/);
assert.match(renderChatExecution(task, progress, []), /执行已停止/);
let renderedChatTaskId = 'task', renderedChatRevision = 'same', renderedChatVisible = true;
const chatBottomThreshold = 48;
const requestAnimationFrame = (callback) => callback();
let writes = 0;
const openDetail = {dataset: {chatDetail: 'older-actions'}, open: true};
const element = {scrollTop: 100, scrollHeight: 1000, clientHeight: 200,
  querySelectorAll: () => [openDetail], set innerHTML(value) {writes++;}};
updateChatMessages(element, 'same', 'task', true);
assert.equal(writes, 0);
updateChatMessages(element, 'changed', 'task', true);
assert.equal(element.scrollTop, 100); // 阅读历史时不抢滚动位置。
assert.equal(openDetail.open, true);
element.scrollTop = 800;
updateChatMessages(element, 'latest', 'task', true);
assert.equal(element.scrollTop, 1000);
'''
    result = subprocess.run([node], input=script, text=True, encoding="utf-8", capture_output=True,
                            cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stdout + result.stderr


def test_initial_page_load_does_not_wait_for_lists():
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for frontend assertions")
    script = r'''
const fs = require('fs'), assert = require('assert/strict');
const source = fs.readFileSync('apps/web/assets/app.js', 'utf8');
function load(name) {
  const start = source.indexOf(`async function ${name}(`);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}
(async () => {
  const calls = [];
  let finishRepositories, finishTasks;
  const loadRepositories = () => { calls.push('repositories'); return new Promise(resolve => finishRepositories = resolve); };
  const refreshTaskList = () => { calls.push('list'); return new Promise(resolve => finishTasks = resolve); };
  const localStorage = {getItem: () => 'saved-task'};
  const selectTask = async id => { assert.equal(id, 'saved-task'); calls.push('selected'); };
  const notify = () => {}, setView = () => {}, renderTaskList = () => {};
  let currentTask = null;
  eval(load('initialize'));
  const pending = initialize();
  await Promise.resolve();
  assert.deepEqual(calls, ['repositories', 'list', 'selected']);
  finishRepositories(); finishTasks(); await pending;
})();
'''
    result = subprocess.run([node], input=script, text=True, capture_output=True,
                            cwd=Path(__file__).resolve().parents[1])
    assert result.returncode == 0, result.stdout + result.stderr


class ApiTests(unittest.TestCase):
    def test_page_reads_do_not_retrieve_memory_or_diagnose_entire_list(self):
        with tempfile.TemporaryDirectory() as directory:
            isolated = build_orchestrator(Path(directory) / "runtime")
            task = isolated.create_task("页面快速加载", "仅验证读取任务列表与详情时不执行额外的模型检索。")
            with patch.object(main, "orchestrator", isolated), \
                    patch.object(main.execution_worker, "start"), \
                    patch.object(main.execution_worker, "stop"):
                with TestClient(main.app) as client, \
                        patch.object(isolated.store.memory, "context_records", side_effect=AssertionError("页面不能检索记忆")), \
                        patch.object(main.RecoveryCoordinator, "diagnose", side_effect=AssertionError("列表不能深度诊断")):
                    response = client.get("/api/tasks")
                    self.assertEqual(response.status_code, 200)
                    summary = response.json()[0]
                    self.assertEqual(summary["id"], task.id)
                    self.assertNotIn("result", summary)
                    self.assertNotIn("development_attempts", summary)
                    self.assertNotIn("technical_plan", summary)
                    for suffix in ("", "/events", "/progress", "/jobs", "/checkpoints", "/traces", "/diff-files"):
                        self.assertEqual(client.get(f"/api/tasks/{task.id}{suffix}").status_code, 200)
                    self.assertIsNotNone(client.get(f"/api/tasks/{task.id}").json()["technical_plan"])
                with patch.object(isolated.store.memory, "context_records", return_value=[]) as memory:
                    isolated.store.get_task(task.id)
                    memory.assert_called_once()  # Agent 默认读取仍可取最新记忆。
                with patch.object(isolated.store.memory, "context_records", side_effect=AssertionError("诊断不能检索记忆")):
                    self.assertEqual(main.RecoveryCoordinator(isolated).diagnose(task.id).status, "healthy")

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
                    self.assertNotIn("workspace_diff", completed["metadata"])

                    diff_files = client.get(
                        f"/api/tasks/{task['id']}/diff-files"
                    )
                    self.assertEqual(diff_files.status_code, 200)
                    changed = {item["path"]: item for item in diff_files.json()["files"]}
                    self.assertEqual(changed["task_service.py"]["status"], "modified")
                    self.assertIn("ALLOWED_PRIORITIES", changed["task_service.py"]["modified"])
                    self.assertNotIn("ALLOWED_PRIORITIES", changed["task_service.py"]["original"])

                    untracked_path = Path(completed["workspace"]) / "agent-notes.txt"
                    untracked_path.write_text("new file from agent\n", encoding="utf-8")
                    changed = {
                        item["path"]: item
                        for item in client.get(
                            f"/api/tasks/{task['id']}/diff-files"
                        ).json()["files"]
                    }
                    self.assertEqual(changed["agent-notes.txt"]["status"], "added")
                    self.assertEqual(changed["agent-notes.txt"]["original"], "")
                    self.assertIn("new file from agent", changed["agent-notes.txt"]["modified"])

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
                    progress = client.get(f"/api/tasks/{task['id']}/progress")
                    self.assertEqual(progress.status_code, 200)
                    self.assertEqual(progress.json()["job"]["id"], job["id"])
                    self.assertEqual(
                        progress.json()["events"][-1]["event_type"],
                        "execution_finished",
                    )
                    self.assertTrue(progress.json()["trace"])
                    self.assertTrue(progress.json()["spans"])
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

    def test_rerun_review_returns_a_background_job(self):
        with tempfile.TemporaryDirectory() as directory:
            isolated = build_orchestrator(Path(directory) / "runtime")
            with patch.object(main, "orchestrator", isolated), \
                    patch.object(main.execution_worker, "start"):
                with TestClient(main.app) as client:
                    task = isolated.create_task(
                        "异步重新审查",
                        "重新审查应进入后台队列并由前端监听进度。",
                    )
                    task.status = TaskStatus.CHANGE_READY
                    isolated.store.save_task(task)

                    response = client.post(f"/api/tasks/{task.id}/review/run")

                    self.assertEqual(response.status_code, 202)
                    job = response.json()
                    self.assertEqual(job["action"], "run_review")
                    self.assertEqual(job["status"], "queued")
