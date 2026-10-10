import json
import tempfile
from pathlib import Path
from unittest.mock import Mock

from agents.code_development_agent import CodeDevelopmentAgent
from agents.task_context import build_task_context
from domain.models import Task
from infrastructure.store import SQLiteTaskStore


def test_history_survives_reload_and_reaches_developer_prompt():
    task = Task(id="context-test", status="developing", title="页面", requirement="增加按钮")
    task.metadata["user_feedback_rounds"] = [
        {"feedback": "按钮改蓝色", "status": "waiting_release_approval",
         "changed_files": ["app.js"], "agent_message": "已改为蓝色",
         "test_exit_code": 0, "diff": "+blue"},
        {"feedback": "调整布局", "status": "running"},
    ]
    # Exercise durable task storage, not an in-memory conversation object.
    with tempfile.TemporaryDirectory() as directory:
        store = SQLiteTaskStore(Path(directory) / "tasks.db")
        store.save_task(task)
        loaded = store.get_task(task.id)
    from domain.models import RepositoryAnalysis, TechnicalPlan
    # Use the real schema defaults for a small developer invocation.
    loaded.repository_analysis = RepositoryAnalysis(
        language="javascript", file_count=1, entrypoints=[],
    )
    loaded.technical_plan = TechnicalPlan(
        approach="修改页面", affected_files=["app.js"], implementation_steps=[],
        test_plan=[], risks=[],
    )
    gateway = Mock()
    gateway.generate_structured.return_value = None
    CodeDevelopmentAgent(gateway)._request_proposal(
        loaded, {"app.js": "blue"}, "test", 1, "", review_feedback="调整布局",
    )
    payload = json.loads(gateway.generate_structured.call_args.args[1])
    history = payload["task_context"]["feedback_history"]
    assert history[0]["request"] == "按钮改蓝色"
    assert history[0]["changed_files"] == ["app.js"]
    assert history[1]["status"] == "running"
    assert payload["repository_context"] == {"app.js": "blue"}


def test_rollback_failure_and_old_requests_are_not_lost():
    task = Task(id="context-test", status="developing", title="页面", requirement="增加按钮")
    task.metadata["user_feedback_rounds"] = [
        {"feedback": "按钮改蓝色", "rolled_back_at": "2026-10-10", "diff": "x" * 9000},
        {"feedback": "撤销上一轮修改", "operation": "rollback"},
        {"feedback": "恢复布局", "status": "failed", "test_exit_code": 1},
    ] + [{"feedback": f"保留功能{i}", "status": "running"} for i in range(12)]
    context = build_task_context(task)
    assert context["feedback_history"][0]["rolled_back"] is True
    assert context["feedback_history"][1]["operation"] == "rollback"
    assert context["feedback_history"][2]["status"] == "failed"
    assert len(context["feedback_history"]) == 15
    assert len(context["recent_change_evidence"]) == 4
    assert build_task_context(Task(id="context-test", status="developing", title="旧任务", requirement="原需求"))["feedback_history"] == []
