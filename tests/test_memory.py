"""Behavioral memory evaluation: persistence, retrieval and prompt delivery."""

import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from agents.repository_exploration_agent import RepositoryExplorationToolRegistry
from agents.task_context import build_task_context
from agents.task_memory import query_execution_memory
from domain.models import Task, DevelopmentAttempt, Repository, RepositoryAnalysis, ExecutionResult
from infrastructure.store import SQLiteTaskStore


def task(id="first", repo="repo-a"):
    return Task(id=id, repository_id=repo, title="Your Videos", requirement="四个 Tab 都要有数据，保留上传入口",
                status="developing")


def attempt(output):
    return DevelopmentAttempt(attempt=1, summary="已创建 YourVideos.js", changed_files=["src/pages/YourVideos.js"],
                              test_command=["npm", "test"], exit_code=1, output=output)


def test_retry_after_restart_retains_failure_evidence_and_requirement(tmp_path):
    path = tmp_path / "agent.db"
    first = task()
    first.error = "Automatic repair exhausted after 3 attempts"
    first.development_attempts = [attempt("ReferenceError: YourVideos is not defined")]
    first.metadata["user_feedback_rounds"] = [
        {"feedback": "必须保留上传入口", "status": "failed", "agent_message": first.error},
        {"feedback": "重试一下", "status": "running"}]
    SQLiteTaskStore(path).save_task(first)
    restored = SQLiteTaskStore(path).get_task(first.id)
    context = build_task_context(restored)
    assert "重试一下" in json.dumps(context, ensure_ascii=False)
    assert "必须保留上传入口" in json.dumps(context, ensure_ascii=False)
    memory = context["working_memory"]
    assert memory["latest_execution"]["output_excerpt"] == "ReferenceError: YourVideos is not defined"
    assert memory["latest_failed_round"]["source_round"] == 1
    assert memory["latest_execution"]["test_exit_code"] == 1


def test_historical_failure_does_not_override_latest_success(tmp_path):
    current = task()
    current.metadata["user_feedback_rounds"] = [{"status": "failed", "agent_message": "old error"}]
    success = attempt("18 tests passed")
    success.exit_code = 0
    current.development_attempts = [success]
    memory = build_task_context(current)["working_memory"]
    assert memory["latest_execution"]["test_exit_code"] == 0
    assert memory["latest_failed_round"]["historical_failure_not_necessarily_current"]
    assert "不代表审查" in memory["retry_policy"]


def test_execution_evidence_pagination_is_lossless_and_tool_propagates():
    current = task()
    output = "测试错误：组件导入失败。" * 1000
    current.development_attempts = [attempt(output)]
    parts, position = [], 0
    while True:
        record = query_execution_memory(current, text_offset=position)["records"][0]
        parts.append(record["output"])
        position = record["next_text_offset"]
        if position is None:
            break
    assert "".join(parts) == output
    registry = RepositoryExplorationToolRegistry(Mock(), task=current)
    result = registry.invoke("query_execution_memory", {"offset": 0})
    assert result == build_task_context(current)["retrieved_history"][-1]


def test_cross_task_memory_is_persistent_scoped_and_reaches_context(tmp_path):
    path = tmp_path / "agent.db"
    store = SQLiteTaskStore(path)
    store.memory.put("repo-a", "component_location", "convention", "新组件放在 src/pages 或 src/components", "AGENTS.md")
    store.memory.put("repo-b", "secret_rule", "convention", "其他仓库专用规范", "AGENTS.md")
    second = task("second")
    store.save_task(second)
    restored = SQLiteTaskStore(path).get_task(second.id)
    records = build_task_context(restored)["project_memory"]
    assert [r["key"] for r in records] == ["component_location"]
    assert records[0]["requires_current_verification"]
    registry = RepositoryExplorationToolRegistry(Mock(), task=restored)
    result = registry.invoke("query_project_memory", {"query": "新组件", "kind": "convention"})
    assert result["records"][0]["source"] == "AGENTS.md"
    assert result == build_task_context(restored)["retrieved_history"][-1]


@pytest.mark.parametrize("query,kind,key", [
    ("组件目录", "convention", "components"),
    ("npm test", "test_command", "test"),
    ("React", "architecture", "framework"),
    ("src/pages", "convention", "components"),
])
def test_retrieval_effectiveness_top_one(tmp_path, query, kind, key):
    memory = SQLiteTaskStore(tmp_path / "agent.db").memory
    memory.put("a", "components", "convention", "组件目录：src/pages，通用组件：src/components", "guide")
    memory.put("a", "test", "test_command", "npm test -- --watch=false", "verified run")
    memory.put("a", "framework", "architecture", "React application", "package.json")
    memory.put("b", key, kind, query, "another repository")
    records = memory.search("a", query, kind)["records"]
    assert records[0]["key"] == key
    assert all(r["repository_id"] == "a" for r in records)


def test_update_invalidate_and_no_match(tmp_path):
    store = SQLiteTaskStore(tmp_path / "agent.db")
    store.memory.put("a", "test", "test_command", "npm test", "run1")
    store.memory.put("a", "test", "test_command", "pnpm test", "run2")
    assert store.memory.search("a")["total_matches"] == 1
    assert store.memory.search("a", "pnpm")["records"][0]["source"] == "run2"
    assert store.memory.search("a", "不存在的部署约定")["records"] == []
    assert store.memory.invalidate("a", "test")
    assert store.memory.search("a")["records"] == []


def test_only_observed_facts_are_promoted_not_user_demands(tmp_path):
    store = SQLiteTaskStore(tmp_path / "agent.db")
    current = task()
    current.repository_analysis = RepositoryAnalysis(language="JavaScript", framework="React", file_count=10,
                                                       head_sha="abc123", test_command="invented command")
    store.save_task(current)
    records = store.memory.search("repo-a")["records"]
    assert len(records) == 1 and records[0]["kind"] == "architecture"
    assert records[0]["source_sha"] == "abc123"
    assert "上传" not in records[0]["content"]
    current.result = ExecutionResult(success=True, command=["npm", "test"], exit_code=0, output="passed",
                                     diff="", mr_title="test", mr_description="test")
    store.save_task(current)
    assert store.memory.search("repo-a", kind="test_command")["records"][0]["content"] == '["npm", "test"]'


def test_long_memory_preview_and_lossless_tool_read(tmp_path):
    store = SQLiteTaskStore(tmp_path / "agent.db")
    text = "组件规范" * 400
    store.memory.put("repo-a", "guide", "convention", text, "AGENTS.md")
    current = task()
    store.save_task(current)
    assert build_task_context(current)["project_memory"][0]["preview_truncated"]
    registry = RepositoryExplorationToolRegistry(Mock(), task=current, max_calls=8)
    parts, position = [], 0
    while True:
        record = registry.invoke("query_project_memory", {"query": "guide", "text_offset": position})["records"][0]
        parts.append(record["content"])
        position = record["next_text_offset"]
        if position is None:
            break
    assert "".join(parts) == text


def test_memory_api_lifecycle_and_missing_repository(tmp_path, monkeypatch):
    from apps.api import main
    store = SQLiteTaskStore(tmp_path / "agent.db")
    store.save_repository(Repository(id="a", name="a", local_path=str(tmp_path)))
    monkeypatch.setattr(main.orchestrator, "store", store)
    client = TestClient(main.app)
    url = "/api/repositories/a/memories"
    assert client.post(url, json={"key": "components", "kind": "convention", "content": "组件放在 src/pages", "source": "用户登记"}).status_code == 200
    assert client.get(url, params={"query": "组件"}).json()["total_matches"] == 1
    assert client.get(url, params={"limit": 100}).status_code == 422
    assert client.get("/api/repositories/missing/memories").status_code == 404
    assert client.delete(url + "/components").status_code == 200
    assert client.get(url).json()["records"] == []


def test_invalidated_observation_is_not_revived_by_routine_task_save(tmp_path):
    store = SQLiteTaskStore(tmp_path / "agent.db")
    current = task()
    current.repository_analysis = RepositoryAnalysis(language="Python", file_count=1, head_sha="a")
    store.save_task(current)
    store.memory.invalidate("repo-a", "observed_architecture")
    store.save_task(current)
    assert store.memory.search("repo-a")["records"] == []


def test_project_memory_is_delivered_to_planning_model():
    from agents.requirement_planning_agent import RequirementPlanningAgent
    class Gateway:
        enabled = True
        def generate_structured(self, system, user, schema):
            self.payload = json.loads(user)
            return None
    gateway = Gateway()
    planner = RequirementPlanningAgent(gateway)
    records = [{"key": "components", "content": "组件放在 src/pages", "source": "AGENTS.md"}]
    planner._request_model_plan("new task", "新增设置页", RepositoryAnalysis(language="JavaScript", file_count=1),
                                "", project_memory=records)
    assert gateway.payload["project_memory"] == records
    assert "当前用户需求优先" in gateway.payload["project_memory_policy"]
