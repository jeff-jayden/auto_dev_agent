from __future__ import annotations

import subprocess
from pathlib import Path

from apps.api.main import build_orchestrator as build_production_orchestrator
from dev_agent.agents import DeveloperRunOutcome
from dev_agent.domain.models import DevelopmentAttempt, ExecutionResult


BASE_SERVICE = '''def create_task(title: str) -> dict[str, object]:
    return {"id": 1, "title": title.strip(), "completed": False}
'''

BASE_TEST = '''import unittest
from task_service import create_task

class TaskServiceTests(unittest.TestCase):
    def test_create_task(self):
        self.assertEqual(create_task("Write tests")["title"], "Write tests")
'''

UPDATED_SERVICE = '''ALLOWED_PRIORITIES = {"low", "medium", "high"}

def create_task(title: str, priority: str = "medium") -> dict[str, object]:
    if not title.strip():
        raise ValueError("title must not be empty")
    if priority not in ALLOWED_PRIORITIES:
        raise ValueError("priority must be one of: high, low, medium")
    return {"id": 1, "title": title.strip(), "completed": False, "priority": priority}
'''

UPDATED_TEST = '''import unittest
from task_service import create_task

class TaskServiceTests(unittest.TestCase):
    def test_create_task(self):
        self.assertEqual(create_task("Write tests")["title"], "Write tests")
    def test_default_priority_is_medium(self):
        self.assertEqual(create_task("Plan")["priority"], "medium")
    def test_accepts_high_priority(self):
        self.assertEqual(create_task("Fix", "high")["priority"], "high")
    def test_rejects_unknown_priority(self):
        with self.assertRaises(ValueError): create_task("Bad", "urgent")
    def test_rejects_empty_title(self):
        with self.assertRaises(ValueError): create_task("  ")
'''


class _EnabledGateway:
    enabled = True


class FixtureDeveloper:
    model_gateway = _EnabledGateway()

    def run(self, task, workspace, checkpoint_handler=None, **_kwargs):
        if checkpoint_handler and checkpoint_handler("proposal_ready", "apply_patch", {}):
            return DeveloperRunOutcome(kind="paused", paused_at="proposal_ready")
        (workspace / "task_service.py").write_text(UPDATED_SERVICE, encoding="utf-8")
        (workspace / "tests" / "test_task_service.py").write_text(UPDATED_TEST, encoding="utf-8")
        changed = ["task_service.py", "tests/test_task_service.py"]
        if checkpoint_handler and checkpoint_handler("patch_applied", "run_tests", {"changed_files": changed}):
            return DeveloperRunOutcome(kind="paused", paused_at="patch_applied")
        completed = subprocess.run(
            ["python", "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--", "."], cwd=workspace,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        ).stdout
        result = ExecutionResult(
            success=completed.returncode == 0,
            command=["python", "-m", "unittest", "discover", "-s", "tests", "-v"],
            exit_code=completed.returncode,
            output=(completed.stdout + completed.stderr).strip(),
            diff=diff,
            mr_title="feat: add priority to tasks",
            mr_description="fixture",
        )
        attempt = DevelopmentAttempt(
            attempt=1, summary="fixture development", changed_files=changed,
            test_command=result.command, exit_code=result.exit_code,
            output=result.output, diff=diff,
        )
        if checkpoint_handler and checkpoint_handler(
            "test_result_saved", "generate_mr", {"result": result.model_dump(mode="json"), "success": True}
        ):
            return DeveloperRunOutcome(kind="paused", attempts=[attempt], result=result, paused_at="test_result_saved")
        return DeveloperRunOutcome(kind="success", attempts=[attempt], result=result)


def build_orchestrator(runtime_root: Path, model_gateway=None):
    orchestrator = build_production_orchestrator(runtime_root, model_gateway)
    repository = runtime_root.parent / "fixture-repository"
    (repository / "tests").mkdir(parents=True, exist_ok=True)
    (repository / "task_service.py").write_text(BASE_SERVICE, encoding="utf-8")
    (repository / "tests" / "test_task_service.py").write_text(BASE_TEST, encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tests@example.local"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test Fixture"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "test: baseline"], cwd=repository, check=True, capture_output=True)
    orchestrator.repository_catalog.allowed_roots.append(runtime_root.parent.resolve())
    orchestrator.repository_catalog.register("测试仓库", repository, repository_id="test-repository")
    if model_gateway is None:
        orchestrator.generic_developer = FixtureDeveloper()
    return orchestrator
