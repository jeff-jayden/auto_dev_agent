import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.api.main import build_orchestrator as build_production_orchestrator
from tests.support import build_orchestrator
from agents import CodeDevelopmentAgent, RequirementPlanningAgent
from code_intelligence.indexer import RepositoryCodeIndex
from domain.models import (
    DevelopmentProposal,
    PatchChange,
    RepositoryAnalysis,
    ReplacementDevelopmentProposal,
    RequirementAnalysis,
    Task,
    TaskStatus,
    TechnicalPlan,
    TextReplacement,
)
from repository import RepositoryAnalyzer


class RepositoryAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = self.root / "sample-repo"
        self.repository.mkdir()
        (self.repository / "pyproject.toml").write_text(
            '[project]\nname="sample"\ndependencies=["fastapi"]\n', encoding="utf-8"
        )
        (self.repository / "main.py").write_text(
            "from fastapi import FastAPI\n\napp = FastAPI()\n\ndef export_users():\n    return []\n",
            encoding="utf-8",
        )

        (self.repository / "tests").mkdir()
        (self.repository / "tests" / "test_main.py").write_text(
            "import unittest\n\nfrom main import export_users\n\n\nclass MainTests(unittest.TestCase):\n    def test_export_users(self):\n        self.assertEqual(export_users(), [])\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init"], cwd=self.repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.local"], cwd=self.repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repository, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repository, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=self.repository, check=True, capture_output=True)

    def test_semantic_import_removal_is_not_blocked_before_tests(self):
        class RemovalGateway:
            enabled = True

            def generate_structured(self, system_prompt, user_prompt, output_model):
                return ReplacementDevelopmentProposal(
                    summary="删除不再需要的导入",
                    replacements=[TextReplacement(
                        path="main.py",
                        search="from fastapi import FastAPI\n\napp = FastAPI()",
                        replace="app = object()",
                    )],
                    preserved_behaviors=["保留 export_users 接口"],
                    test_command="python -m py_compile main.py",
                )

        task = Task(
            id="semantic-removal",
            title="移除旧接线",
            requirement="移除不再需要的 FastAPI 接线",
            status=TaskStatus.DEVELOPING,
            analysis=RequirementAnalysis(
                summary="移除旧接线",
                user_story="移除未使用依赖",
                acceptance_criteria=["测试通过"],
                assumptions=[],
            ),
            repository_analysis=RepositoryAnalysis(
                language="python",
                file_count=1,
                test_command="python -m py_compile main.py",
                important_files=["main.py"],
            ),
            technical_plan=TechnicalPlan(
                approach="移除旧接线并运行测试",
                affected_files=["main.py"],
                implementation_steps=["移除旧接线"],
                test_plan=["python -m py_compile main.py"],
                risks=[],
            ),
        )

        outcome = CodeDevelopmentAgent(RemovalGateway()).run(task, self.repository)

        self.assertEqual(outcome.kind, "success")
        self.assertNotIn(
            "from fastapi import FastAPI",
            (self.repository / "main.py").read_text(encoding="utf-8"),
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_analyzer_returns_verified_repository_facts(self):
        analysis = RepositoryAnalyzer().analyze(self.repository, "增加 export_users CSV 导出")
        self.assertEqual(analysis.language, "python")
        self.assertEqual(analysis.framework, "fastapi")
        self.assertEqual(analysis.test_command, "python -m unittest discover -s tests -v")
        self.assertTrue(analysis.head_sha)
        self.assertIn("main.py", analysis.entrypoints)
        self.assertTrue(any(item.path == "main.py" and item.line > 0 for item in analysis.evidence))
        self.assertEqual(
            [call.tool for call in analysis.tool_calls],
            ["list_files", "detect_stack", "git_metadata", "code_index", "search_code"],
        )
        self.assertIn("main.py", analysis.context_pack.primary_files)
        self.assertIn("tests/test_main.py", analysis.context_pack.test_files)

    def test_mixed_chinese_and_english_requirement_terms_are_separated(self):
        terms = RepositoryCodeIndex._requirement_terms("实现History页面，增加一些示例")

        self.assertIn("history", terms)
        self.assertNotIn("history页面", terms)

    def test_keyword_matches_are_primary_and_unmatched_entrypoint_is_dependency(self):
        repository = self.root / "react-repo"
        source = repository / "src"
        source.mkdir(parents=True)
        files = {
            "src/index.js": "import App from './App';\nrender(<App />);\n",
            "src/App.js": (
                "import HistoryPanel from './HistoryPanel';\n"
                "import './App.css';\n"
                "const sidebarItems = ['Home', 'History'];\n"
                "export default function App() { return <HistoryPanel />; }\n"
            ),
            "src/HistoryPanel.js": (
                "export default function HistoryPanel() { return <h1>History</h1>; }\n"
            ),
            "src/App.test.js": (
                "import App from './App';\n"
                "test('renders History', () => expect(App).toBeDefined());\n"
            ),
            "src/App.css": ".history-panel { display: block; }\n",
        }
        for relative, content in files.items():
            target = repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        indexer = RepositoryCodeIndex()
        index, mode, changed = indexer.build(repository, list(files), None)
        context, _ = indexer.context_pack(
            repository,
            index,
            "实现History页面，增加一些示例",
            ["src/index.js"],
            mode,
            changed,
        )

        self.assertIn("src/App.js", context.primary_files)
        self.assertIn("src/HistoryPanel.js", context.primary_files)
        self.assertNotIn("src/index.js", context.primary_files)
        self.assertIn("src/index.js", context.dependency_files)
        self.assertIn("src/App.test.js", context.test_files)

    def test_hybrid_rag_recalls_semantic_code_file_missed_by_legacy_keywords(self):
        repository = self.root / "semantic-repo"
        repository.mkdir()
        files = {
            "auth/session_guard.py": (
                "class SessionGuard:\n"
                "    def authenticate_credentials(self, credentials):\n"
                "        return bool(credentials)\n"
            ),
            "reports/dashboard.py": "def render_dashboard():\n    return 'dashboard'\n",
            "orders/repository.py": "def save_order(order):\n    return order\n",
        }
        for relative, content in files.items():
            target = repository / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        indexer = RepositoryCodeIndex()
        index, _, _ = indexer.build(repository, list(files), None)

        baseline = indexer.rank_files(index, "增加用户身份校验", strategy="lexical")
        hybrid = indexer.rank_files(index, "增加用户身份校验", strategy="hybrid")

        self.assertNotIn("auth/session_guard.py", [item["path"] for item in baseline])
        self.assertEqual(hybrid[0]["path"], "auth/session_guard.py")
        self.assertTrue(any("语义向量" in reason for reason in hybrid[0]["reasons"]))

    def test_code_index_is_cached_and_incrementally_updated_by_git_head(self):
        analyzer = RepositoryAnalyzer(self.root / "indexes")

        first = analyzer.analyze(self.repository, "增加 export_users CSV 导出")
        second = analyzer.analyze(self.repository, "增加 export_users CSV 导出")
        self.assertEqual(first.context_pack.index_mode, "full")
        self.assertEqual(second.context_pack.index_mode, "cache_hit")

        with (self.repository / "main.py").open("a", encoding="utf-8") as handle:
            handle.write("\ndef export_users_csv():\n    return 'id,name\\n'\n")
        subprocess.run(["git", "add", "main.py"], cwd=self.repository, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add csv export"],
            cwd=self.repository,
            check=True,
            capture_output=True,
        )

        updated = analyzer.analyze(self.repository, "增加 export_users CSV 导出")
        self.assertEqual(updated.context_pack.index_mode, "incremental")
        self.assertEqual(updated.context_pack.changed_files, ["main.py"])
        self.assertNotEqual(first.head_sha, updated.head_sha)

    def test_head_change_regenerates_plan_and_requires_approval_again(self):
        class EnabledGateway:
            enabled = True

            def generate_structured(self, system_prompt, user_prompt, output_model):
                return None

        with patch.dict(os.environ, {"AGENT_ALLOWED_REPOSITORY_ROOTS": str(self.root)}):
            orchestrator = build_orchestrator(
                self.root / "runtime-head-drift", EnabledGateway()
            )
        repository = orchestrator.repository_catalog.register("Sample", self.repository)
        task = orchestrator.create_task(
            "导出用户", "管理员可以调用 export_users 导出 CSV 用户列表。", repository.id
        )
        original_head = task.repository_analysis.head_sha

        with (self.repository / "main.py").open("a", encoding="utf-8") as handle:
            handle.write("\n# upstream change\n")
        subprocess.run(["git", "add", "main.py"], cwd=self.repository, check=True)
        subprocess.run(
            ["git", "commit", "-m", "upstream change"],
            cwd=self.repository,
            check=True,
            capture_output=True,
        )

        refreshed = orchestrator.approve(task.id, "reviewer", "同意旧方案")

        self.assertEqual(refreshed.status, TaskStatus.WAITING_REQUIREMENT_APPROVAL)
        self.assertIsNone(refreshed.approval)
        self.assertIsNone(refreshed.workspace)
        self.assertNotEqual(original_head, refreshed.repository_analysis.head_sha)
        self.assertEqual(refreshed.repository_analysis.context_pack.index_mode, "incremental")
        self.assertEqual(refreshed.metadata["baseline_refresh_count"], 1)
        event_types = [event.event_type for event in orchestrator.store.list_events(task.id)]
        self.assertIn("repository_baseline_changed", event_types)

    def test_react_analysis_selects_app_files_and_non_watching_tests(self):
        react_repository = self.root / "react-repo"
        (react_repository / "src").mkdir(parents=True)
        (react_repository / "package.json").write_text(
            '{"dependencies":{"react":"^18.0.0"},"scripts":{"test":"react-scripts test"}}',
            encoding="utf-8",
        )
        for name, content in {
            "App.js": "export default function App() { return <main>Hello</main>; }\n",
            "App.test.js": "test('renders', () => {});\n",
            "App.css": "main { padding: 1rem; }\n",
            "index.js": "import App from './App';\n",
            "reportWebVitals.js": "export default function reportWebVitals() {}\n",
        }.items():
            (react_repository / "src" / name).write_text(content, encoding="utf-8")

        analysis = RepositoryAnalyzer().analyze(react_repository, "首页增加按钮，点击弹出你好呀")
        plan = RequirementPlanningAgent().design(analysis, "首页增加按钮，点击弹出你好呀")

        self.assertEqual(analysis.framework, "react")
        self.assertEqual(analysis.test_command, "npm test -- --watchAll=false")
        self.assertIn("src/App.js", plan.affected_files)
        self.assertIn("src/App.test.js", plan.affected_files)
        self.assertIn("src/App.css", plan.affected_files)
        self.assertNotIn("src/reportWebVitals.js", plan.affected_files)

    def test_wiring_requirement_includes_repository_entrypoint(self):
        react_repository = self.root / "wiring-react-repo"
        (react_repository / "src").mkdir(parents=True)
        (react_repository / "package.json").write_text(
            '{"dependencies":{"react":"^18.0.0"},"scripts":{"test":"react-scripts test"}}',
            encoding="utf-8",
        )
        (react_repository / "src" / "App.js").write_text(
            "export default function App() { return <main>Home</main>; }\n",
            encoding="utf-8",
        )
        (react_repository / "src" / "App.test.js").write_text("test('app', () => {});\n", encoding="utf-8")
        (react_repository / "src" / "index.js").write_text(
            "import App from './App';\nroot.render(<App />);\n",
            encoding="utf-8",
        )

        analysis = RepositoryAnalyzer().analyze(
            react_repository, "恢复 App 的导入和调用方式"
        )
        plan = RequirementPlanningAgent().design(analysis, "恢复 App 的导入和调用方式")

        self.assertIn("src/App.js", plan.affected_files)
        self.assertIn("src/index.js", plan.affected_files)

    def test_real_repository_without_model_fails_explicitly_and_revision_is_audited(self):
        runtime = self.root / "runtime"
        with patch.dict(os.environ, {"AGENT_ALLOWED_REPOSITORY_ROOTS": str(self.root)}):
            orchestrator = build_production_orchestrator(runtime)
        repository = orchestrator.repository_catalog.register("Sample", self.repository)
        task = orchestrator.create_task("导出用户", "管理员可以调用 export_users 导出 CSV 用户列表。", repository.id)
        self.assertEqual(task.status, TaskStatus.WAITING_REQUIREMENT_APPROVAL)
        self.assertEqual(task.repository_analysis.framework, "fastapi")

        revised = orchestrator.revise(task.id, "reviewer", "必须支持 UTF-8 BOM")
        self.assertEqual(revised.status, TaskStatus.WAITING_REQUIREMENT_APPROVAL)
        self.assertEqual(revised.metadata["plan_feedback"][0]["feedback"], "必须支持 UTF-8 BOM")
        self.assertTrue(any("UTF-8 BOM" in item for item in revised.analysis.acceptance_criteria))

        approved = orchestrator.approve(task.id, "reviewer", "只批准方案")
        self.assertEqual(approved.status, TaskStatus.FAILED)
        self.assertIsNone(approved.workspace)
        self.assertIsNone(approved.result)
        self.assertIn("requires an LLM", approved.error)
        event_types = [event.event_type for event in orchestrator.store.list_events(task.id)]
        self.assertIn("plan_revised", event_types)
        self.assertEqual(event_types[-1], "development_failed")

    def test_real_repository_is_modified_only_in_worktree(self):
        class FakeDeveloperGateway:
            enabled = True

            def generate_structured(self, system_prompt, user_prompt, output_model):
                if output_model.__name__ not in {"DevelopmentProposal", "ReplacementDevelopmentProposal"}:
                    return None
                return DevelopmentProposal(
                    summary="增加 CSV 导出函数并补充测试",
                    test_command="python -m unittest discover -s tests -v",
                    changes=[
                        PatchChange(
                            path="main.py",
                            reason="在现有用户查询模块增加 CSV 输出",
                            patch='''diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -4,3 +4,7 @@ app = FastAPI()
 
 def export_users():
     return []
+
+
+def export_users_csv():
+    return "id,name\\n"
''',
                        ),
                        PatchChange(
                            path="tests/test_main.py",
                            reason="验证 CSV 表头",
                            patch='''diff --git a/tests/test_main.py b/tests/test_main.py
--- a/tests/test_main.py
+++ b/tests/test_main.py
@@ -1,8 +1,11 @@
 import unittest
 
-from main import export_users
+from main import export_users, export_users_csv
 
 
 class MainTests(unittest.TestCase):
     def test_export_users(self):
         self.assertEqual(export_users(), [])
+
+    def test_export_users_csv(self):
+        self.assertEqual(export_users_csv(), "id,name\\n")
''',
                        ),
                    ],
                )

        runtime = self.root / "runtime-model"
        with patch.dict(os.environ, {"AGENT_ALLOWED_REPOSITORY_ROOTS": str(self.root)}):
            orchestrator = build_orchestrator(runtime, FakeDeveloperGateway())
        repository = orchestrator.repository_catalog.register("Sample", self.repository)
        task = orchestrator.create_task("导出用户", "管理员可以调用 export_users 导出 CSV 用户列表。", repository.id)
        completed = orchestrator.approve(task.id, "reviewer", "同意开发")

        self.assertEqual(completed.status, TaskStatus.WAITING_RELEASE_APPROVAL)
        self.assertTrue(completed.result.success)
        self.assertEqual(completed.result.exit_code, 0)
        self.assertEqual(len(completed.development_attempts), 1)
        self.assertIsNotNone(completed.development_attempts[0].proposal)
        self.assertIn("export_users_csv", completed.result.diff)
        self.assertNotIn("export_users_csv", (self.repository / "main.py").read_text(encoding="utf-8"))
        self.assertIn("export_users_csv", (Path(completed.workspace) / "main.py").read_text(encoding="utf-8"))

    def test_failed_test_triggers_a_repair_patch(self):
        class RepairGateway:
            enabled = True

            def __init__(self):
                self.development_calls = 0

            def generate_structured(self, system_prompt, user_prompt, output_model):
                if output_model.__name__ not in {"DevelopmentProposal", "ReplacementDevelopmentProposal"}:
                    return None
                self.development_calls += 1
                if self.development_calls == 1:
                    return DevelopmentProposal(
                        summary="首次实现包含一个可被测试发现的错误",
                        test_command="python -m unittest discover -s tests -v",
                        changes=[
                            PatchChange(path="main.py", reason="增加导出", patch='''diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -4,3 +4,7 @@ app = FastAPI()
 
 def export_users():
     return []
+
+
+def export_users_csv():
+    return "wrong\\n"
'''),
                            PatchChange(path="tests/test_main.py", reason="增加验收测试", patch='''diff --git a/tests/test_main.py b/tests/test_main.py
--- a/tests/test_main.py
+++ b/tests/test_main.py
@@ -1,8 +1,11 @@
 import unittest
 
-from main import export_users
+from main import export_users, export_users_csv
 
 
 class MainTests(unittest.TestCase):
     def test_export_users(self):
         self.assertEqual(export_users(), [])
+
+    def test_export_users_csv(self):
+        self.assertEqual(export_users_csv(), "id,name\\n")
'''),
                        ],
                    )
                return DevelopmentProposal(
                    summary="根据测试失败修复 CSV 表头",
                    test_command="python -m unittest discover -s tests -v",
                    changes=[PatchChange(path="main.py", reason="修正返回值", patch='''diff --git a/main.py b/main.py
--- a/main.py
+++ b/main.py
@@ -7,4 +7,4 @@ def export_users():
 
 
 def export_users_csv():
-    return "wrong\\n"
+    return "id,name\\n"
''')],
                )

        gateway = RepairGateway()
        with patch.dict(os.environ, {"AGENT_ALLOWED_REPOSITORY_ROOTS": str(self.root)}):
            orchestrator = build_orchestrator(self.root / "runtime-repair", gateway)
        repository = orchestrator.repository_catalog.register("Sample", self.repository)
        task = orchestrator.create_task("导出用户", "管理员可以调用 export_users 导出 CSV 用户列表。", repository.id)
        completed = orchestrator.approve(task.id, "reviewer", "同意开发")

        self.assertEqual(completed.status, TaskStatus.WAITING_RELEASE_APPROVAL)
        self.assertEqual(gateway.development_calls, 2)
        self.assertEqual([attempt.exit_code for attempt in completed.development_attempts], [1, 0])
        self.assertIn("id,name", completed.result.diff)

    def test_replacement_repairs_accumulate_when_each_attempt_reveals_next_error(self):
        repository = self.root / "progressive-repair"
        repository.mkdir()
        (repository / "first.txt").write_text("bad-first\n", encoding="utf-8")
        (repository / "second.txt").write_text("bad-second\n", encoding="utf-8")
        (repository / "verify.py").write_text(
            "from pathlib import Path\n"
            "assert Path('first.txt').read_text().strip() == 'good-first', 'first is broken'\n"
            "assert Path('second.txt').read_text().strip() == 'good-second', 'second is broken'\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.local"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
        subprocess.run(["git", "add", "."], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=repository, check=True, capture_output=True)

        class ProgressiveGateway:
            enabled = True

            def __init__(self):
                self.calls = 0

            def generate_structured(self, system_prompt, user_prompt, output_model):
                self.calls += 1
                if self.calls == 1:
                    return DevelopmentProposal(
                        replacements=[TextReplacement(
                            path="first.txt", search="bad-first\n", replace="good-first\n"
                        )],
                        test_command="python verify.py",
                    )
                return DevelopmentProposal(
                    replacements=[TextReplacement(
                        path="second.txt", search="bad-second\n", replace="good-second\n"
                    )],
                    test_command="python verify.py",
                )

        task = Task(
            id="progressive",
            title="连续修复",
            requirement="依次修复两个会串行暴露的错误。",
            status=TaskStatus.DEVELOPING,
            analysis=RequirementAnalysis(
                summary="连续修复", user_story="连续修复", acceptance_criteria=["验证通过"], assumptions=[]
            ),
            technical_plan=TechnicalPlan(
                approach="逐个修复", affected_files=["first.txt", "second.txt"],
                implementation_steps=["修复两个文件"], test_plan=["运行验证"], risks=[]
            ),
            repository_analysis=RepositoryAnalysis(
                language="text", file_count=3, test_command="python verify.py"
            ),
        )
        gateway = ProgressiveGateway()

        outcome = CodeDevelopmentAgent(gateway).run(task, repository)

        self.assertEqual(outcome.kind, "success")
        self.assertEqual(gateway.calls, 2)
        self.assertEqual((repository / "first.txt").read_text(encoding="utf-8"), "good-first\n")
        self.assertEqual((repository / "second.txt").read_text(encoding="utf-8"), "good-second\n")

    def test_retry_preflight_repairs_css_before_model_handles_next_failure(self):
        repository = self.root / "css-preflight"
        repository.mkdir()
        (repository / "App.css").write_text(
            ".App {\n  color: white;\n}\n  color: white;\n}\n",
            encoding="utf-8",
        )
        (repository / "index.js").write_text("unused-import\n", encoding="utf-8")
        (repository / "verify.py").write_text(
            "from pathlib import Path\n"
            "css = Path('App.css').read_text()\n"
            "assert css.count('{') == css.count('}'), 'css remains malformed'\n"
            "assert Path('index.js').read_text().strip() == 'fixed-import', 'index remains broken'\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.local"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
        subprocess.run(["git", "add", "."], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=repository, check=True, capture_output=True)

        class IndexGateway:
            enabled = True

            def generate_structured(self, system_prompt, user_prompt, output_model):
                return DevelopmentProposal(
                    replacements=[TextReplacement(
                        path="index.js", search="unused-import\n", replace="fixed-import\n"
                    )],
                    test_command="python verify.py",
                )

        task = Task(
            id="css-preflight", title="CSS 预修复", requirement="先修 CSS，再修入口文件。",
            status=TaskStatus.DEVELOPING,
            analysis=RequirementAnalysis(
                summary="CSS 预修复", user_story="修复构建", acceptance_criteria=["验证通过"], assumptions=[]
            ),
            technical_plan=TechnicalPlan(
                approach="连续修复", affected_files=["App.css", "index.js"],
                implementation_steps=["修复"], test_plan=["运行验证"], risks=[]
            ),
            repository_analysis=RepositoryAnalysis(
                language="javascript", framework="react", file_count=3,
                test_command="python verify.py",
            ),
        )

        outcome = CodeDevelopmentAgent(IndexGateway()).run(
            task,
            repository,
            resume_stage="test_result_saved",
            resume_payload={
                "next_attempt": 1,
                "repair_context": "CSS validation failed in App.css: unexpected closing brace",
            },
        )

        self.assertEqual(outcome.kind, "success")
        self.assertEqual([item.exit_code for item in outcome.attempts], [1, 0])
        self.assertEqual(
            (repository / "App.css").read_text(encoding="utf-8").count("color: white;"), 1
        )
        self.assertEqual((repository / "index.js").read_text(encoding="utf-8"), "fixed-import\n")

    def test_pause_at_proposal_checkpoint_and_resume(self):
        class CheckpointGateway:
            enabled = True

            def generate_structured(self, system_prompt, user_prompt, output_model):
                if output_model.__name__ not in {"DevelopmentProposal", "ReplacementDevelopmentProposal"}:
                    return None
                return DevelopmentProposal(
                    summary="增加 CSV 导出函数",
                    test_command="python -m unittest discover -s tests -v",
                    replacements=[
                        TextReplacement(
                            path="main.py",
                            search="def export_users():\n    return []\n",
                            replace=(
                                "def export_users():\n    return []\n\n\n"
                                "def export_users_csv():\n    return \"id,name\\n\"\n"
                            ),
                        )
                    ],
                )

        with patch.dict(os.environ, {"AGENT_ALLOWED_REPOSITORY_ROOTS": str(self.root)}):
            orchestrator = build_orchestrator(self.root / "runtime-checkpoint", CheckpointGateway())
        repository = orchestrator.repository_catalog.register("Sample", self.repository)
        task = orchestrator.create_task(
            "导出用户",
            "管理员可以调用 export_users_csv 导出 CSV 用户列表。",
            repository.id,
        )
        checkpoint_calls = 0

        def pause_on_proposal():
            nonlocal checkpoint_calls
            checkpoint_calls += 1
            return checkpoint_calls == 2

        paused = orchestrator.approve(
            task.id,
            "reviewer",
            "同意开发",
            checkpoint_job_id="job-pause",
            should_pause=pause_on_proposal,
        )

        self.assertEqual(paused.status, TaskStatus.DEVELOPING)
        checkpoint = orchestrator.store.latest_checkpoint(task.id)
        self.assertEqual(checkpoint.stage, "proposal_ready")
        self.assertEqual(checkpoint.next_action, "apply_patch")
        self.assertIn("proposal", checkpoint.payload)

        completed = orchestrator.resume_from_checkpoint(task.id, "job-resume")

        self.assertEqual(completed.status, TaskStatus.WAITING_RELEASE_APPROVAL)
        self.assertIn("export_users_csv", completed.result.diff)
        stages = [item.stage for item in orchestrator.store.list_checkpoints(task.id)]
        self.assertIn("patch_applied", stages)
        self.assertIn("test_result_saved", stages)
        self.assertIn("review_round_saved", stages)
