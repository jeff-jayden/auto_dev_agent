import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agents.task_context import build_task_context, query_task_history
from agents.repository_exploration_agent import RepositoryExplorationToolRegistry
from domain.models import Task
from infrastructure.store import SQLiteTaskStore
from llm.context_budget import ContextBudget, ContextBudgetExceeded, fit_messages, token_upper_bound, budget_middleware
from llm.gateway import OpenAICompatibleGateway, OllamaGateway
from tools import DeveloperToolkit, ToolPolicy


def task_with_history():
    task = Task(id="history", status="developing", title="page", requirement="original requirement")
    task.metadata["user_feedback_rounds"] = [
        {"feedback": "must preserve keyboard navigation", "status": "failed", "test_exit_code": 1},
        {"feedback": "blue button", "status": "waiting_release_approval", "test_exit_code": 0,
         "changed_files": ["src/App.js"], "rolled_back_at": "today"},
    ] + [{"feedback": f"request {i}", "status": "running", "diff": "old evidence" * 10000} for i in range(8)]
    return task


def test_summary_is_persisted_and_failed_requirement_stays_protected(tmp_path):
    task = task_with_history()
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    store.save_task(task)
    reloaded = store.get_task(task.id)
    context = build_task_context(reloaded)
    summary = reloaded.metadata["task_summary"]
    assert summary["user_requirements"][0]["status"] == "requested_not_assumed_complete"
    assert summary["actual_progress"][0]["status"] == "failed"
    assert summary["actual_progress"][1]["rolled_back"]
    assert summary["actual_progress"][1]["user_acceptance"] == "not_inferred_from_tests"
    assert summary["protected_requirements"] == [{"source_round": 1, "text": "must preserve keyboard navigation"}]
    assert len(context["feedback_history"]) == 3
    assert "old evidence" not in json.dumps(context)
    assert query_task_history(reloaded, round_number=1)["records"][0]["text"] == "must preserve keyboard navigation"


def test_history_search_pagination_and_long_text_are_lossless():
    task = task_with_history()
    text = "中文需求" * 1000
    task.metadata["user_feedback_rounds"][0]["feedback"] = text
    parts, position = [], 0
    while True:
        response = query_task_history(task, round_number=1, text_offset=position)
        record = response["records"][0]
        parts.append(record["text"])
        assert token_upper_bound(record["text"]) <= 1200
        position = record["next_text_offset"]
        if position is None:
            break
    assert "".join(parts) == text
    assert query_task_history(task, file="src/App.js")["records"][0]["source_round"] == 2
    first = query_task_history(task, query="request", limit=2)
    second = query_task_history(task, query="request", limit=2, offset=first["next_offset"])
    assert first["records"][-1]["source_round"] < second["records"][0]["source_round"]


def test_registered_history_tool_returns_evidence_to_next_developer_context(tmp_path):
    task = task_with_history()
    kit = DeveloperToolkit(tmp_path, ToolPolicy(tmp_path, ["src/App.js"], "test"))
    registry = RepositoryExplorationToolRegistry(kit, task=task)
    assert "query_task_history" in registry.names
    result = registry.invoke("query_task_history", {"round_number": 1})
    assert result["records"][0]["source_round"] == 1
    assert build_task_context(task)["retrieved_history"][0] == result
    assert any(item.tool == "agent.context_tool_call" for item in kit.audit)
    with pytest.raises(ValueError):
        registry.invoke("query_task_history", {"limit": 1000})


def test_budget_trims_optional_evidence_but_preserves_requests_and_code():
    payload = {"task_context": {"feedback_history": [{"request": "keep critical requirement"}],
                                "development_history": ["x" * 20000]},
               "repository_context": {"main.py": "current code"}, "allowed_files": ["main.py"]}
    messages = [{"role": "system", "content": "system"},
                {"role": "user", "content": json.dumps(payload)}]
    budget = ContextBudget(window=4000, output=1000, safety=100, max_input=2000)
    fitted, report = fit_messages(messages, budget=budget)
    parsed = json.loads(fitted[1]["content"])
    assert parsed["repository_context"] == payload["repository_context"]
    assert parsed["task_context"]["feedback_history"] == payload["task_context"]["feedback_history"]
    assert report["input_estimate"] <= 2000
    assert report["output_reserved"] == 1000
    assert messages[1]["content"] == json.dumps(payload)  # no source mutation


@pytest.mark.parametrize("gateway_type", [OpenAICompatibleGateway, OllamaGateway])
def test_gateway_refuses_oversized_essential_input_before_http(gateway_type, monkeypatch):
    monkeypatch.setenv("MODEL_MAX_INPUT_TOKENS", "200")
    gateway = gateway_type("http://example.test", "test")
    with patch("urllib.request.urlopen") as http:
        assert gateway.generate_structured("system", "critical" * 1000, Task) is None
        http.assert_not_called()
    assert "budget exceeded" in gateway.last_error


def test_oversized_protected_constraints_are_not_silently_dropped(monkeypatch):
    task = task_with_history()
    monkeypatch.setenv("TASK_CONTEXT_MAX_TOKENS", "100")
    with pytest.raises(ContextBudgetExceeded):
        build_task_context(task)
    assert task.metadata["user_feedback_rounds"][0]["feedback"] == "must preserve keyboard navigation"


def test_summary_refreshes_when_a_prior_round_is_rolled_back():
    task = task_with_history()
    before = build_task_context(task)["task_summary"]
    task.metadata["user_feedback_rounds"][0]["rolled_back_at"] = "now"
    after = build_task_context(task)["task_summary"]
    assert before["source_digest"] != after["source_digest"]
    assert after["protected_requirements"] == []


def test_langchain_budget_counts_tool_results_and_schema(monkeypatch):
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.messages import HumanMessage, ToolMessage
    monkeypatch.setenv("MODEL_MAX_INPUT_TOKENS", "300")
    request = ModelRequest(model=Mock(), system_prompt="system",
        messages=[HumanMessage(content="request"), ToolMessage(content="tool result" * 1000, tool_call_id="tool")],
        tools=[])
    handler = Mock()
    with pytest.raises(ContextBudgetExceeded):
        budget_middleware().wrap_model_call(request, handler)
    handler.assert_not_called()


def test_review_budget_failure_is_blocking():
    from agents.code_review_agent import CodeReviewAgent
    from domain.models import ExecutionResult
    task = task_with_history()
    task.result = ExecutionResult(success=True, command=["test"], exit_code=0, output="passed",
                                  diff="", mr_title="page", mr_description="page")
    gateway = Mock(enabled=True, last_error="Context budget exceeded")
    gateway.generate_structured.return_value = None
    findings, _ = CodeReviewAgent(gateway)._model_findings(task, {}, [])
    assert findings[0].blocking
    assert findings[0].category == "context"


def test_large_history_is_bounded_but_original_round_remains_queryable():
    task = task_with_history()
    task.metadata["user_feedback_rounds"] = [
        {"feedback": f"change {i}: " + "details " * 40, "status": "running"} for i in range(200)
    ]
    context = build_task_context(task)
    assert token_upper_bound(context) <= 8000
    assert context["history_access"]["omitted_summary_rounds"]
    assert query_task_history(task, round_number=1)["records"][0]["text"] == task.metadata["user_feedback_rounds"][0]["feedback"]


def test_http_retry_is_rebudgeted_and_output_is_reserved(monkeypatch):
    from pydantic import BaseModel
    class Output(BaseModel):
        value: int
    monkeypatch.setenv("MODEL_MAX_INPUT_TOKENS", "3000")
    monkeypatch.setenv("MODEL_OUTPUT_RESERVE_TOKENS", "1000")
    gateway = OpenAICompatibleGateway("http://example.test", "test")
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps({"choices": [{"message": {"content": "invalid" * 1000}}]}).encode()
    with patch("urllib.request.urlopen", return_value=response) as http:
        assert gateway.generate_structured("system", "request", Output) is None
        # 失败输出不再回灌，请求始终有界，允许完成三次 JSON 修正尝试。
        assert http.call_count == 3
        for call in http.call_args_list:
            sent = json.loads(call.args[0].data)
            assert sent["max_tokens"] == 1000
            assert "invalid" * 1000 not in json.dumps(sent["messages"])
        assert len(sent["messages"]) == 3
    assert "budget exceeded" not in gateway.last_error
    assert gateway.last_error
