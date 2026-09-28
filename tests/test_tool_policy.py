import tempfile
import unittest
from pathlib import Path

from domain.models import DevelopmentProposal, PatchChange, TextReplacement
from tools import DeveloperToolkit, PatchRejected, ToolPolicy


class ToolPolicyTests(unittest.TestCase):
    def test_classifies_high_risk_and_forbidden_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = ToolPolicy(
                Path(directory),
                ["deploy/rollout.yaml", ".env", "src/service.py"],
                "python -m unittest",
            )
            self.assertEqual(policy.assess_paths(["deploy/rollout.yaml"]).level, "high")
            self.assertEqual(policy.assess_paths([".env"]).level, "forbidden")
            self.assertEqual(policy.assess_paths(["src/service.py"]).level, "low")

    def test_rejects_paths_outside_plan_and_unapproved_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = ToolPolicy(Path(directory), ["src/service.py"], "python -m unittest")
            with self.assertRaisesRegex(ValueError, "outside the approved technical plan"):
                policy.resolve_write_path("src/other.py")
            with self.assertRaisesRegex(ValueError, "not approved"):
                policy.validate_test_command("powershell -Command whoami")

    def test_rejects_patch_that_spoofs_its_declared_path(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            (repository / "safe.py").write_text("value = 1\n", encoding="utf-8")
            (repository / "other.py").write_text("secret = 1\n", encoding="utf-8")
            policy = ToolPolicy(repository, ["safe.py"], "python -m unittest")
            toolkit = DeveloperToolkit(repository, policy)
            proposal = DevelopmentProposal(
                summary="伪造路径",
                test_command="python -m unittest",
                changes=[PatchChange(
                    path="safe.py",
                    reason="测试",
                    patch="""diff --git a/other.py b/other.py
--- a/other.py
+++ b/other.py
@@ -1 +1 @@
-secret = 1
+secret = 2
""",
                )],
            )
            with self.assertRaisesRegex(PatchRejected, "do not match"):
                toolkit.apply_proposal(proposal)

    def test_applies_exact_text_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            target = repository / "src" / "App.js"
            target.parent.mkdir()
            target.write_text("function App() {\n  return <main>Hello</main>;\n}\n", encoding="utf-8")
            policy = ToolPolicy(repository, ["src/App.js"], "npm test -- --watchAll=false")
            toolkit = DeveloperToolkit(repository, policy)
            proposal = DevelopmentProposal(
                summary="增加按钮",
                test_command="npm test -- --watchAll=false",
                replacements=[TextReplacement(
                    path="src/App.js",
                    search="  return <main>Hello</main>;",
                    replace='  return <main>Hello<button onClick={() => alert("你好呀~")}>点击</button></main>;',
                    reason="增加弹窗按钮",
                )],
            )

            self.assertEqual(toolkit.apply_proposal(proposal), ["src/App.js"])
            self.assertIn("你好呀~", target.read_text(encoding="utf-8"))

    def test_replacement_must_match_exactly_once_without_partial_write(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            target = repository / "App.js"
            original = "const value = 1;\nconst value = 1;\n"
            target.write_text(original, encoding="utf-8")
            policy = ToolPolicy(repository, ["App.js"], "npm test")
            toolkit = DeveloperToolkit(repository, policy)
            proposal = DevelopmentProposal(
                summary="含糊替换",
                test_command="npm test",
                replacements=[TextReplacement(
                    path="App.js",
                    search="const value = 1;",
                    replace="const value = 2;",
                    reason="测试唯一匹配保护",
                )],
            )

            with self.assertRaisesRegex(PatchRejected, "exactly once"):
                toolkit.apply_proposal(proposal)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_rejects_noop_replacement_without_running_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            target = repository / "App.js"
            original = "export default function App() { return null; }\n"
            target.write_text(original, encoding="utf-8")
            toolkit = DeveloperToolkit(
                repository, ToolPolicy(repository, ["App.js"], "npm test")
            )
            proposal = DevelopmentProposal(
                test_command="npm test",
                replacements=[TextReplacement(
                    path="App.js", search=original, replace=original
                )],
            )

            with self.assertRaisesRegex(PatchRejected, "does not change any content"):
                toolkit.apply_proposal(proposal)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_small_model_proposal_can_omit_optional_summary(self):
        proposal = DevelopmentProposal.model_validate({
            "changes": [],
            "replacements": [{
                "path": "App.js",
                "search": "old",
                "replace": "new",
                "reason": "实现需求",
            }],
            "test_command": "npm test",
            "allowed_files": ["App.js"],
        })

        self.assertEqual(proposal.summary, "按批准方案实现需求")
        self.assertEqual(proposal.replacements[0].path, "App.js")

    def test_double_escaped_model_snippet_is_normalized_before_validation(self):
        from agents.code_development_agent import CodeDevelopmentAgent

        proposal = DevelopmentProposal(
            test_command="npm test",
            replacements=[TextReplacement(
                path="src/index.js",
                search="import App from './App';\\nroot.render(<main>old</main>);",
                replace="import App from './App';\\nroot.render(<App />);",
            )],
        )

        self.assertEqual(
            DeveloperToolkit.normalize_model_text(proposal.replacements[0].replace),
            "import App from './App';\nroot.render(<App />);",
        )

    def test_compiler_error_targets_the_referencing_file(self):
        from agents.code_development_agent import CodeDevelopmentAgent

        failure = """[build]\nFailed to compile.\n[eslint]\nsrc\\index.js\n  Line 9:6: 'App' is not defined react/jsx-no-undef\nRejected target files: src/App.js"""

        targets = CodeDevelopmentAgent._diagnostic_target_files(
            failure,
            ["src/App.js", "src/App.test.js", "src/index.js"],
        )

        self.assertEqual(targets, ["src/index.js"])

    def test_replacement_allows_one_unique_whitespace_only_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            target = repository / "App.js"
            target.write_text("function App() {\n  return null;\n}\n\nexport default App;\n", encoding="utf-8")
            policy = ToolPolicy(repository, ["App.js"], "npm test")
            proposal = DevelopmentProposal(
                test_command="npm test",
                replacements=[TextReplacement(
                    path="App.js",
                    search="function App() {\n  return null;\n}\nexport default App;",
                    replace="function App() {\n  return <button>你好</button>;\n}\n\nexport default App;",
                )],
            )

            DeveloperToolkit(repository, policy).apply_proposal(proposal)
            self.assertIn("<button>你好</button>", target.read_text(encoding="utf-8"))

    def test_rejects_partial_css_block_expansion_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            target = repository / "App.css"
            original = ".App-header {\n  color: white;\n  min-height: 100vh;\n}\n"
            target.write_text(original, encoding="utf-8")
            policy = ToolPolicy(repository, ["App.css"], "npm test")
            proposal = DevelopmentProposal(
                test_command="npm test",
                replacements=[TextReplacement(
                    path="App.css",
                    search=".App-header {\n  color: white;",
                    replace=".App-header {\n  color: black;\n  min-height: 100vh;\n}",
                )],
            )

            with self.assertRaisesRegex(PatchRejected, "complete block"):
                DeveloperToolkit(repository, policy).apply_proposal(proposal)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_rejects_css_property_outside_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            target = repository / "App.css"
            original = ".App-header {\n  color: white;\n}\n"
            target.write_text(original, encoding="utf-8")
            policy = ToolPolicy(repository, ["App.css"], "npm test")
            proposal = DevelopmentProposal(
                test_command="npm test",
                replacements=[TextReplacement(
                    path="App.css", search=original,
                    replace=".App-header {\n  color: black;\n}\n  min-height: 100vh;\n",
                )],
            )

            with self.assertRaisesRegex(PatchRejected, "outside a rule block"):
                DeveloperToolkit(repository, policy).apply_proposal(proposal)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_structural_css_repair_removes_only_orphan_declarations(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            target = repository / "App.css"
            target.write_text(
                ".App {\n  color: white;\n}\n"
                "  min-height: 100vh;\n  display: flex;\n}\n\n"
                ".App-link {\n  color: blue;\n}\n",
                encoding="utf-8",
            )
            toolkit = DeveloperToolkit(
                repository, ToolPolicy(repository, ["App.css"], "npm test")
            )

            repaired = toolkit.repair_malformed_css(["App.css"])

            self.assertEqual(repaired, ["App.css"])
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                ".App {\n  color: white;\n}\n\n.App-link {\n  color: blue;\n}\n",
            )
            self.assertEqual(toolkit.audit[-1].tool, "repair_css_structure")
