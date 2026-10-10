import unittest
import json
import subprocess
from unittest.mock import patch

from agents.requirement_planning_agent import RequirementPlanningAgent, RequirementPlanningResponse, RequirementPlanningToolRegistry
from domain.models import (
    CodeEvidence,
    DevelopmentStep,
    RepositoryAnalysis,
    RepositoryContextPack,
    RequirementAnalysis,
    TechnicalPlan,
)


class PlanningCreateAgentTests(unittest.TestCase):
    def test_create_agent_uses_repository_tools_and_returns_structured_plan(self):
        class Gateway:
            enabled = True
            model = "fake-deepseek"

            def build_langchain_model(self):
                return object()

            def generate_structured(self, *_args, **_kwargs):
                raise AssertionError("create_agent path must not use legacy structured generation")

        repository = RepositoryAnalysis(
            language="javascript",
            framework="react",
            head_sha="abc123",
            file_count=3,
            entrypoints=["src/index.js"],
            test_command="npm test -- --watchAll=false",
            important_files=["src/App.js", "src/App.test.js", "src/index.js"],
            evidence=[CodeEvidence(
                path="src/App.js", line=1, snippet="function App()", reason="应用入口组件",
            )],
            context_pack=RepositoryContextPack(
                primary_files=["src/App.js"],
                dependency_files=["src/index.js"],
                test_files=["src/App.test.js"],
            ),
        )
        expected = RequirementPlanningResponse(
            analysis=RequirementAnalysis(
                summary="新增入口按钮",
                user_story="用户可以点击入口按钮",
                acceptance_criteria=["按钮可见", "点击生效"],
                assumptions=["复用现有 React 结构"],
            ),
            plan=TechnicalPlan(
                approach="在 App 中增加入口并补充测试",
                affected_files=["src/App.js", "src/App.test.js"],
                implementation_steps=["增加按钮", "补充交互测试"],
                test_plan=["npm test -- --watchAll=false"],
                risks=["保持现有首页行为"],
                development_steps=[
                    DevelopmentStep(
                        id="step-1", title="实现入口", objective="增加按钮",
                        allowed_files=["src/App.js"], acceptance_checks=["按钮可见"],
                    ),
                    DevelopmentStep(
                        id="step-2", title="验证交互", objective="补充测试",
                        allowed_files=["src/App.test.js"], acceptance_checks=["测试通过"],
                        depends_on=["step-1"],
                    ),
                ],
            ),
        )

        class FakeCompiledAgent:
            def __init__(self, tools):
                self.tools = {item.name: item for item in tools}

            def invoke(self, payload, config):
                self.tools["list_planning_files"].invoke({
                    "role": "all", "reason": "确定受影响源码和测试文件",
                })
                self.tools["get_code_evidence"].invoke({
                    "path": "src/App.js", "reason": "确认页面入口职责",
                })
                return {"messages": [], "structured_response": expected}

        with patch(
            "agents.requirement_planning_agent.create_agent",
            side_effect=lambda **kwargs: FakeCompiledAgent(kwargs["tools"]),
        ) as create:
            analysis, plan = RequirementPlanningAgent(Gateway()).plan(
                "新增入口按钮", "在首页增加按钮，点击后展示内容", repository,
            )

        self.assertTrue(create.called)
        self.assertEqual(analysis.summary, "新增入口按钮")
        self.assertEqual(
            plan.affected_files,
            ["src/App.js", "src/App.test.js"],
        )
        planner_calls = [item.tool for item in repository.tool_calls]
        self.assertIn("requirement_planning.list_planning_files", planner_calls)
        self.assertIn("requirement_planning.get_code_evidence", planner_calls)
        self.assertIn("requirement_planning.create_agent", planner_calls)


if __name__ == "__main__":
    unittest.main()


def test_planning_read_file_is_paged_and_fixed_to_git_baseline(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "Page.js"
    source.write_text("first\nsecond\nthird\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "baseline"], check=True)
    sha = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    source.write_text("working tree changed", encoding="utf-8")
    repository = RepositoryAnalysis(language="javascript", file_count=1, head_sha=sha, important_files=["Page.js"])
    registry = RequirementPlanningToolRegistry(repository, tmp_path)
    reader = next(tool for tool in registry.tools if tool.name == "read_file")
    result = reader.invoke({"path": "Page.js", "start_line": 2, "end_line": 2})
    assert result["content"] == "2: second"
    assert result["baseline_sha"] == sha
    assert result["has_more"]
    duplicate = reader.invoke({"path": "Page.js", "start_line": 2, "end_line": 2})
    assert "相同查询" in duplicate["error"]
    assert repository.tool_calls[-1].success is False


def test_planning_reader_rejects_unknown_sensitive_large_and_external_files(tmp_path):
    from pathlib import Path
    import pytest
    source = tmp_path / "Page.js"
    source.write_text("page", encoding="utf-8")
    repository = RepositoryAnalysis(language="javascript", file_count=1, important_files=["Page.js", ".env", "../outside.js"])
    registry = RequirementPlanningToolRegistry(repository, tmp_path)
    reader = next(tool for tool in registry.tools if tool.name == "read_file")
    for path in (".env", "../outside.js", str(Path(tmp_path).parent / "outside.js"), "unknown.js"):
        assert "error" in reader.invoke({"path": path})
    assert "error" in reader.invoke({"path": "Page.js", "end_line": 121})
    source.write_bytes(b"x" * 256001)
    assert "error" in reader.invoke({"path": "Page.js", "end_line": 10})
    for _ in range(2):
        reader.invoke({"path": "Page.js", "end_line": 10})
    with pytest.raises(ValueError, match="8 次"):
        reader.invoke({"path": "Page.js"})


def test_finalization_retains_source_read_before_exploration_failure(tmp_path):
    class Gateway:
        enabled = True
        def build_langchain_model(self):
            return object()
        def generate_structured(self, system, prompt, model):
            payload = json.loads(prompt)
            assert payload["exploration_results"][0]["result"]["content"] == "1: actual source"
            return RequirementPlanningResponse(
                analysis=RequirementAnalysis(summary="具体方案", user_story="user", acceptance_criteria=["works"], assumptions=[]),
                plan=TechnicalPlan(approach="修改真实源码", affected_files=["Page.js"], implementation_steps=["implement"], test_plan=[], risks=[]),
            )
    (tmp_path / "Page.js").write_text("actual source", encoding="utf-8")
    repository = RepositoryAnalysis(language="javascript", file_count=1, important_files=["Page.js"])
    def compile_agent(**kwargs):
        class Agent:
            def invoke(self, *_args, **_kwargs):
                next(tool for tool in kwargs["tools"] if tool.name == "read_file").invoke({"path": "Page.js"})
                raise ValueError("exploration limit")
        return Agent()
    with patch("agents.requirement_planning_agent.create_agent", side_effect=compile_agent):
        analysis, _ = RequirementPlanningAgent(Gateway()).plan("page", "change page", repository, repository_root=tmp_path)
    assert analysis.summary == "具体方案"


def test_model_failure_is_not_replaced_with_template():
    import pytest
    class Gateway:
        enabled = True
        last_error = "JSON 字段校验失败"
        def generate_structured(self, *_args):
            return None
    repository = RepositoryAnalysis(language="javascript", file_count=0)
    with pytest.raises(ValueError, match="JSON 字段校验失败"):
        RequirementPlanningAgent(Gateway()).plan("page", "change page", repository)


def test_planning_failure_saved_for_revision(tmp_path):
    import pytest
    from tests.support import build_orchestrator
    from domain.models import TaskStatus
    orchestrator = build_orchestrator(tmp_path / "runtime")
    with patch.object(orchestrator.requirement_planning_agent, "plan", side_effect=ValueError("规划失败：字段校验失败")):
        with pytest.raises(ValueError, match="规划失败"):
            orchestrator.create_task("planning", "change page")
    task = orchestrator.store.list_tasks()[0]
    assert task.status == TaskStatus.WAITING_REQUIREMENT_INPUT
    assert task.analysis is None and task.technical_plan is None
    assert "字段校验失败" in task.error
    assert orchestrator.store.list_events(task.id)[-1].event_type == "planning_failed"
    revised = orchestrator.revise(task.id, "user", "重新生成方案")
    assert revised.status == TaskStatus.WAITING_REQUIREMENT_APPROVAL
    assert revised.error is None
