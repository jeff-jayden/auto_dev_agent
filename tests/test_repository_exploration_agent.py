import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from agents.repository_exploration_agent import RepositoryToolDecision
from agents.repository_exploration_agent import RepositoryExplorationAgent
from agents.repository_exploration_agent import RepositoryExplorationToolRegistry
from agents.code_development_agent import CodeDevelopmentAgent
from domain.models import (
    RepositoryAnalysis,
    RepositoryContextPack,
    ReplacementDevelopmentProposal,
    RequirementAnalysis,
    Task,
    TaskStatus,
    TechnicalPlan,
    TextReplacement,
)


class DeveloperContextToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        # Production worktrees live below the application's runtime directory.
        # The explorer must ignore a runtime folder inside a repo, not this parent.
        self.repository = Path(self.temporary_directory.name) / "runtime" / "tasks" / "demo" / "repo"
        self.repository.mkdir(parents=True)
        (self.repository / "main.py").write_text('VALUE = "old"\n', encoding="utf-8")
        (self.repository / "helper.py").write_text(
            'class HiddenHelper:\n    """Evidence found outside the initial context."""\n',
            encoding="utf-8",
        )
        (self.repository / ".env").write_text("SECRET=do-not-read\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=self.repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.local"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repository, check=True)
        subprocess.run(["git", "add", "main.py", "helper.py"], cwd=self.repository, check=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=self.repository, check=True, capture_output=True)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_developer_selects_read_only_tools_before_proposal(self):
        class ToolLoopGateway:
            enabled = True
            supports_context_tool_loop = True

            def __init__(self):
                self.decisions = 0
                self.proposal_prompt = ""

            def generate_structured(self, system_prompt, user_prompt, output_model):
                if output_model is RepositoryToolDecision:
                    self.decisions += 1
                    if self.decisions == 1:
                        return RepositoryToolDecision(
                            tool="search_symbol", arguments={"query": "HiddenHelper"},
                            reason="需要定位需求涉及的辅助类型",
                        )
                    if self.decisions == 2:
                        observation = json.loads(user_prompt)["recent_observations"][-1]["result"]
                        self.assert_search_result = observation
                        return RepositoryToolDecision(
                            tool="read_file",
                            arguments={"path": "helper.py", "start_line": 1, "end_line": 20},
                            reason="需要阅读搜索命中的实现",
                        )
                    return RepositoryToolDecision(
                        tool="finish", reason="上下文已经足够", summary="已确认辅助类型定义",
                    )
                if output_model is ReplacementDevelopmentProposal:
                    self.proposal_prompt = user_prompt
                    return ReplacementDevelopmentProposal(
                        summary="使用检索到的仓库证据完成修改",
                        replacements=[TextReplacement(
                            path="main.py", search='VALUE = "old"', replace='VALUE = "new"',
                        )],
                        preserved_behaviors=["保留 main.py 的 VALUE 接口"],
                        test_command="python -m py_compile main.py",
                    )
                return None

        task = Task(
            id="context-tools",
            title="使用辅助类型",
            requirement="修改主模块前确认 HiddenHelper 的定义。",
            status=TaskStatus.DEVELOPING,
            analysis=RequirementAnalysis(
                summary="确认辅助类型", user_story="读取相关实现", acceptance_criteria=["编译通过"], assumptions=[],
            ),
            repository_analysis=RepositoryAnalysis(
                language="python", file_count=2, test_command="python -m py_compile main.py",
                context_pack=RepositoryContextPack(primary_files=["main.py"]),
            ),
            technical_plan=TechnicalPlan(
                approach="确认依赖后修改主模块", affected_files=["main.py"],
                implementation_steps=["修改主模块"], test_plan=["python -m py_compile main.py"], risks=[],
            ),
        )
        gateway = ToolLoopGateway()

        outcome = CodeDevelopmentAgent(gateway).run(task, self.repository)

        self.assertEqual(outcome.kind, "success")
        self.assertEqual(gateway.decisions, 3)
        self.assertEqual(gateway.assert_search_result[0]["path"], "helper.py")
        self.assertIn("HiddenHelper", gateway.proposal_prompt)
        self.assertNotIn("do-not-read", gateway.proposal_prompt)
        tool_names = [item.tool for item in outcome.attempts[0].tool_calls]
        self.assertIn("search_symbol", tool_names)
        self.assertIn("read_file", tool_names)
        self.assertIn("agent.context_complete", tool_names)

    def test_langgraph_developer_loop_routes_failed_test_to_next_attempt(self):
        class RepairGateway:
            enabled = True
            supports_context_tool_loop = False

            def __init__(self):
                self.proposals = 0

            def generate_structured(self, _system_prompt, _user_prompt, output_model):
                if output_model is not ReplacementDevelopmentProposal:
                    return None
                self.proposals += 1
                if self.proposals == 1:
                    return ReplacementDevelopmentProposal(
                        summary="先产生一个可诊断的语法错误",
                        replacements=[TextReplacement(
                            path="main.py", search='VALUE = "old"', replace="VALUE =",
                        )],
                        preserved_behaviors=["保留 VALUE 常量"],
                        test_command="python -m py_compile main.py",
                    )
                return ReplacementDevelopmentProposal(
                    summary="根据测试结果修复语法错误",
                    replacements=[TextReplacement(
                        path="main.py", search="VALUE =", replace='VALUE = "new"',
                    )],
                    preserved_behaviors=["保留 VALUE 常量"],
                    test_command="python -m py_compile main.py",
                )

        task = Task(
            id="langgraph-repair-loop",
            title="修复主模块",
            requirement="更新 VALUE 并保持模块可编译。",
            status=TaskStatus.DEVELOPING,
            analysis=RequirementAnalysis(
                summary="更新常量", user_story="修改主模块",
                acceptance_criteria=["编译通过"], assumptions=[],
            ),
            repository_analysis=RepositoryAnalysis(
                language="python", file_count=2,
                test_command="python -m py_compile main.py",
                context_pack=RepositoryContextPack(primary_files=["main.py"]),
            ),
            technical_plan=TechnicalPlan(
                approach="修改并验证", affected_files=["main.py"],
                implementation_steps=["修改常量"],
                test_plan=["python -m py_compile main.py"], risks=[],
            ),
        )
        gateway = RepairGateway()

        outcome = CodeDevelopmentAgent(gateway).run(task, self.repository)

        self.assertEqual(outcome.kind, "success")
        self.assertEqual(gateway.proposals, 2)
        self.assertEqual([attempt.exit_code for attempt in outcome.attempts], [1, 0])
        self.assertEqual(
            (self.repository / "main.py").read_text(encoding="utf-8"),
            'VALUE = "new"\n',
        )

    def test_context_tool_requires_arguments_for_selected_tool(self):
        from tools import DeveloperToolkit, ToolPolicy

        toolkit = DeveloperToolkit(
            self.repository,
            ToolPolicy(self.repository, ["main.py"], "python -m py_compile main.py"),
        )
        registry = RepositoryExplorationToolRegistry(toolkit)
        with self.assertRaises(ValueError):
            registry.invoke("read_file", {})
        with self.assertRaises(ValueError):
            registry.invoke("search_text", {})
        decision = RepositoryToolDecision(tool="finish", reason="证据充分")
        self.assertEqual(decision.tool, "finish")

    def test_langchain_registry_exposes_schemas_and_invokes_tools(self):
        from tools import DeveloperToolkit, ToolPolicy

        toolkit = DeveloperToolkit(
            self.repository,
            ToolPolicy(self.repository, ["main.py"], "python -m py_compile main.py"),
        )
        registry = RepositoryExplorationToolRegistry(toolkit)
        specifications = {item["name"]: item for item in registry.specifications()}

        self.assertEqual(
            set(specifications),
            {"search_text", "search_symbol", "find_references", "read_file", "git_history"},
        )
        self.assertIn("query", specifications["search_text"]["input_schema"]["properties"])
        result = registry.invoke("read_file", {"path": "helper.py", "start_line": 1, "end_line": 5})
        self.assertIn("HiddenHelper", result["content"])

    def test_create_agent_autonomously_calls_tools_and_expands_context(self):
        from tools import DeveloperToolkit, ToolPolicy

        class CreateAgentGateway:
            enabled = True
            supports_context_tool_loop = True
            model = "fake-deepseek"

            def build_langchain_model(self):
                return object()

            def generate_structured(self, *_args, **_kwargs):
                raise AssertionError("create_agent path must not use the legacy decision model")

        class FakeCompiledAgent:
            def __init__(self, tools):
                self.tools = {item.name: item for item in tools}

            def invoke(self, payload, config):
                self.tools["search_symbol"].invoke({
                    "query": "HiddenHelper", "reason": "定位辅助类型",
                })
                self.tools["read_file"].invoke({
                    "path": "helper.py", "start_line": 1, "end_line": 20,
                    "reason": "读取搜索命中的实现",
                })
                return {"messages": [AIMessage(content="已确认辅助类型定义。")]}

        task = Task(
            id="create-agent-context",
            title="使用辅助类型",
            requirement="修改主模块前确认 HiddenHelper 的定义。",
            status=TaskStatus.DEVELOPING,
            analysis=RequirementAnalysis(
                summary="确认辅助类型", user_story="读取相关实现",
                acceptance_criteria=["编译通过"], assumptions=[],
            ),
            repository_analysis=RepositoryAnalysis(
                language="python", file_count=2,
                test_command="python -m py_compile main.py",
                context_pack=RepositoryContextPack(primary_files=["main.py"]),
            ),
            technical_plan=TechnicalPlan(
                approach="确认依赖后修改主模块", affected_files=["main.py"],
                implementation_steps=["修改主模块"],
                test_plan=["python -m py_compile main.py"], risks=[],
            ),
        )
        toolkit = DeveloperToolkit(
            self.repository,
            ToolPolicy(self.repository, ["main.py"], "python -m py_compile main.py"),
        )

        with patch(
            "agents.repository_exploration_agent.create_agent",
            side_effect=lambda *, model, tools, system_prompt: FakeCompiledAgent(tools),
        ) as create:
            context = RepositoryExplorationAgent(CreateAgentGateway(), max_steps=4).explore(
                task=task,
                toolkit=toolkit,
                initial_context={"main.py": 'VALUE = "old"\n'},
                write_scope=["main.py"],
            )

        self.assertTrue(create.called)
        self.assertIn("HiddenHelper", context["helper.py"])
        calls = [item for item in toolkit.audit if item.tool == "agent.context_tool_call"]
        self.assertEqual([item.arguments["tool"] for item in calls], ["search_symbol", "read_file"])
        completion = next(item for item in toolkit.audit if item.tool == "agent.context_complete")
        self.assertEqual(completion.arguments["framework"], "langchain.create_agent")


if __name__ == "__main__":
    unittest.main()
