import unittest
from unittest.mock import patch

from agents.requirement_planning_agent import RequirementPlanningAgent, RequirementPlanningResponse
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
            ["src/App.js", "src/App.test.js", "src/index.js"],
        )
        planner_calls = [item.tool for item in repository.tool_calls]
        self.assertIn("requirement_planning.list_planning_files", planner_calls)
        self.assertIn("requirement_planning.get_code_evidence", planner_calls)
        self.assertIn("requirement_planning.create_agent", planner_calls)


if __name__ == "__main__":
    unittest.main()
