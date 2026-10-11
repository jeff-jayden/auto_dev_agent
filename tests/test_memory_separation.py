"""Requirements survive schema migration; only working memory projects state."""

from copy import deepcopy

from agents.task_context import build_task_context, query_task_history
from agents.task_memory import query_execution_memory
from domain.models import Task, DevelopmentAttempt, ExecutionResult
from infrastructure.store import SQLiteTaskStore


def sample():
    task = Task(id="separation", title="视频页面", requirement="四个 Tab 都要有数据", status="developing")
    task.metadata["user_feedback_rounds"] = [
        {"feedback": "必须保留上传入口", "status": "failed", "agent_message": "import error",
         "changed_files": ["src/App.js"], "test_exit_code": 1, "diff": "+old"},
        {"feedback": "撤销上轮", "operation": "rollback", "status": "waiting_release_approval"},
        {"feedback": "重试一下", "status": "running"}]
    task.development_attempts = [DevelopmentAttempt(
        attempt=i, summary=f"attempt {i}", changed_files=["src/App.js"], test_command=["npm", "test"],
        exit_code=0, output=f"output {i}") for i in range(1, 4)]
    task.result = ExecutionResult(success=True, exit_code=0, command=["npm", "test"], output="passed",
                                  diff="+new", mr_title="title", mr_description="description")
    return task


def test_state_has_one_context_owner_and_requirements_remain_complete():
    task = sample()
    context = build_task_context(task)
    summary = context["task_summary"]
    assert summary["version"] == 3
    assert "actual_progress" not in summary and "latest_development" not in summary
    assert "current_result" not in context
    assert all(not ({"status", "result", "changed_files", "test_exit_code"} & set(item))
               for item in context["feedback_history"])
    assert [item["request"] for item in context["feedback_history"]] == [
        "必须保留上传入口", "撤销上轮", "重试一下"]
    progress = context["working_memory"]
    assert progress["recent_rounds"][0]["status"] == "failed"
    assert progress["recent_rounds"][1]["rolled_back"]
    assert progress["recent_rounds"][2]["status"] == "running"
    assert progress["latest_execution"]["summary"] == "attempt 3"
    assert progress["current_result"] == {"test_exit_code": 0, "has_changes": True}


def test_execution_changes_do_not_invalidate_requirement_digest():
    task = sample()
    before = build_task_context(task)["task_summary"]["source_digest"]
    task.metadata["user_feedback_rounds"][0].update(status="completed", test_exit_code=0)
    task.development_attempts[-1].summary = "new execution result"
    assert build_task_context(task)["task_summary"]["source_digest"] == before
    task.metadata["user_feedback_rounds"][0]["rolled_back_at"] = "now"
    after = build_task_context(task)["task_summary"]
    assert after["source_digest"] != before
    assert after["protected_requirements"] == []


def test_version_two_migration_preserves_raw_history_and_attempt_queries(tmp_path):
    task = sample()
    task.metadata["task_summary"] = {"version": 2, "actual_progress": ["obsolete"],
                                     "latest_development": ["obsolete"]}
    original = deepcopy(task.metadata["user_feedback_rounds"])
    store = SQLiteTaskStore(tmp_path / "agent.db")
    store.save_task(task)
    restored = store.get_task(task.id)
    assert restored.metadata["task_summary"]["version"] == 3
    assert restored.metadata["user_feedback_rounds"] == original
    for index, item in enumerate(original, 1):
        for field in ("feedback", "agent_message", "diff"):
            assert query_task_history(restored, round_number=index, field=field)["records"][0]["text"] == item.get(field, "")
    assert [r["output"] for r in query_execution_memory(restored, limit=4)["records"]] == [
        "output 1", "output 2", "output 3"]


def test_recent_limit_is_shared_and_older_evidence_is_queryable(monkeypatch):
    monkeypatch.setenv("TASK_CONTEXT_RECENT_ROUNDS", "1")
    context = build_task_context(sample())
    assert [r["round"] for r in context["feedback_history"]] == [3]
    assert [r["source_round"] for r in context["working_memory"]["recent_rounds"]] == [3]
    assert context["task_summary"]["protected_requirements"] == [
        {"source_round": 1, "text": "必须保留上传入口"}]
    assert query_task_history(sample(), file="src/App.js", field="diff")["records"][0]["text"] == "+old"
