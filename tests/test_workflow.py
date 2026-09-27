import tempfile
import unittest
from pathlib import Path

from apps.api.main import build_orchestrator
from dev_agent.agents import DeveloperRunOutcome
from dev_agent.domain.models import (
    DevelopmentAttempt,
    DevelopmentProposal,
    ReviewFinding,
    ReviewRound,
    ReviewerModelOutput,
    TaskStatus,
    TextReplacement,
)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp_directory.name) / "runtime"

    def tearDown(self):
        self.temp_directory.cleanup()

    def test_vertical_slice_creates_verified_change(self):
        orchestrator = build_orchestrator(self.runtime)
        task = orchestrator.create_task(
            "为任务增加优先级",
            "任务需要支持 low、medium、high 三种优先级，默认使用 medium。",
        )
        self.assertEqual(task.status, TaskStatus.WAITING_REQUIREMENT_APPROVAL)
        self.assertGreaterEqual(len(task.analysis.acceptance_criteria), 4)
        self.assertEqual(task.technical_plan.affected_files, ["task_service.py", "tests/test_task_service.py"])

        completed = orchestrator.approve(task.id, "tester", "方案可执行")
        self.assertEqual(completed.status, TaskStatus.WAITING_RELEASE_APPROVAL)
        self.assertTrue(completed.result.success)
        self.assertEqual(completed.result.exit_code, 0)
        self.assertIn("Ran 5 tests", completed.result.output)
        self.assertIn("+ALLOWED_PRIORITIES", completed.result.diff)
        self.assertEqual(completed.result.mr_title, "feat: add priority to tasks")
        self.assertIsNotNone(completed.merge_request)
        self.assertIn("task_service.py", completed.merge_request.changed_files)
        self.assertEqual(completed.reviews[-1].decision, "approved")
        self.assertTrue(Path(completed.workspace).exists())
        self.assertEqual(
            [event.event_type for event in orchestrator.store.list_events(task.id)],
            [
                "task_created", "repository_analyzed", "plan_ready", "approved",
                "implementation_complete", "change_ready", "merge_request_generated",
                "code_review_completed", "review_approved", "release_approval_required",
            ],
        )

    def test_reviewer_blocks_debug_code_at_real_diff_line(self):
        orchestrator = build_orchestrator(self.runtime)
        task = orchestrator.create_task(
            "为任务增加优先级",
            "任务需要支持 low、medium、high 三种优先级，默认使用 medium。",
        )
        completed = orchestrator.approve(task.id, "tester", "方案可执行")
        completed.result.diff = """diff --git a/task_service.py b/task_service.py
--- a/task_service.py
+++ b/task_service.py
@@ -1,1 +1,2 @@
 VALUE = 1
+breakpoint()
"""
        completed.merge_request.changed_files = ["task_service.py"]

        review = orchestrator.code_reviewer.review(completed, 2)

        self.assertEqual(review.decision, "changes_requested")
        self.assertTrue(review.findings[0].blocking)
        self.assertEqual(review.findings[0].file, "task_service.py")
        self.assertEqual(review.findings[0].line, 2)

    def test_reviewer_blocks_undefined_react_handler_and_missing_popup(self):
        orchestrator = build_orchestrator(self.runtime)
        task = orchestrator.create_task(
            "为任务增加优先级",
            "任务需要支持 low、medium、high 三种优先级，默认使用 medium。",
        )
        completed = orchestrator.approve(task.id, "tester", "方案可执行")
        completed.requirement = "在首页添加按钮，点击弹窗提示你好呀"
        completed.repository_analysis.framework = "react"
        target = Path(completed.workspace) / "task_service.py"
        target.write_text(
            "function App() { return <button onClick={() => missingHandler()}>点击</button>; }\n",
            encoding="utf-8",
        )
        completed.result.diff = """diff --git a/task_service.py b/task_service.py
--- a/task_service.py
+++ b/task_service.py
@@ -1,0 +1,1 @@
+function App() { return <button onClick={() => missingHandler()}>点击</button>; }
"""

        review = orchestrator.code_reviewer.review(completed, 2)
        messages = [item.message for item in review.findings if item.blocking]

        self.assertEqual(review.decision, "changes_requested")
        self.assertTrue(any("未定义" in message for message in messages))
        self.assertTrue(any("显示提示" in message for message in messages))

    def test_reviewer_blocks_malformed_css_and_entry_bypass(self):
        orchestrator = build_orchestrator(self.runtime)
        task = orchestrator.create_task(
            "为任务增加优先级",
            "任务需要支持 low、medium、high 三种优先级，默认使用 medium。",
        )
        completed = orchestrator.approve(task.id, "tester", "方案可执行")
        completed.repository_analysis.framework = "react"
        completed.technical_plan.affected_files.extend(["App.css", "index.js"])
        workspace = Path(completed.workspace)
        (workspace / "App.css").write_text(
            ".App-header {\n  color: white;\n}\n  min-height: 100vh;\n}\n",
            encoding="utf-8",
        )
        (workspace / "index.js").write_text(
            "import App from './App';\nroot.render(<main>replacement</main>);\n",
            encoding="utf-8",
        )
        completed.result.diff = """diff --git a/App.css b/App.css
--- a/App.css
+++ b/App.css
@@ -1,1 +1,5 @@
+.App-header {
+  color: white;
+}
+  min-height: 100vh;
+}
diff --git a/index.js b/index.js
--- a/index.js
+++ b/index.js
@@ -1,1 +1,2 @@
+import App from './App';
+root.render(<main>replacement</main>);
"""

        review = orchestrator.code_reviewer.review(completed, 2)
        messages = [item.message for item in review.findings if item.blocking]

        self.assertEqual(review.decision, "changes_requested")
        self.assertTrue(any("CSS" in message for message in messages))
        self.assertTrue(any("不再渲染 App" in message for message in messages))

    def test_reviewer_blocks_medium_core_acceptance_finding(self):
        orchestrator = build_orchestrator(self.runtime)
        task = orchestrator.create_task(
            "为任务增加优先级",
            "任务需要支持 low、medium、high 三种优先级，默认使用 medium。",
        )
        completed = orchestrator.approve(task.id, "tester", "方案可执行")

        class AcceptanceGateway:
            enabled = True

            def generate_structured(self, system_prompt, user_prompt, output_model):
                return ReviewerModelOutput(
                    summary="核心功能缺失",
                    findings=[ReviewFinding(
                        severity="medium", category="Functionality",
                        file="task_service.py", line=6,
                        message="核心验收功能没有实现。",
                        suggestion="按验收标准补全实现。",
                    )],
                )

        orchestrator.code_reviewer.model_gateway = AcceptanceGateway()
        review = orchestrator.code_reviewer.review(completed, 2)

        self.assertEqual(review.decision, "changes_requested")
        self.assertTrue(any(item.blocking for item in review.findings if item.category == "Functionality"))

    def test_reject_stops_before_development(self):
        orchestrator = build_orchestrator(self.runtime)
        task = orchestrator.create_task("拒绝示例", "这是一个足够长但将被拒绝的需求描述。")
        rejected = orchestrator.reject(task.id, "reviewer", "验收标准需要调整")
        self.assertEqual(rejected.status, TaskStatus.REJECTED)
        self.assertEqual(rejected.approval.decision, "rejected")
        self.assertIsNone(rejected.workspace)

    def test_rerun_creates_new_task_with_fresh_audit_lineage(self):
        orchestrator = build_orchestrator(self.runtime)
        original = orchestrator.create_task(
            "重跑示例", "这是一个用于验证重跑任务继承需求并重新规划的完整需求描述。"
        )
        original = orchestrator.reject(original.id, "reviewer", "暂时拒绝")

        rerun = orchestrator.rerun_task(original.id)

        self.assertNotEqual(rerun.id, original.id)
        self.assertEqual(rerun.title, original.title)
        self.assertEqual(rerun.requirement, original.requirement)
        self.assertEqual(rerun.repository_id, original.repository_id)
        self.assertEqual(rerun.status, TaskStatus.WAITING_REQUIREMENT_APPROVAL)
        self.assertEqual(rerun.metadata["rerun_of"], original.id)
        event_types = [item.event_type for item in orchestrator.store.list_events(rerun.id)]
        self.assertIn("task_rerun_created", event_types)

    def test_direct_review_rejects_interrupted_review_repair(self):
        orchestrator = build_orchestrator(self.runtime)
        task = orchestrator.create_task(
            "恢复审查修复", "验证重新审查可以继续此前被服务重启中断的自动修复流程。"
        )
        task.status = TaskStatus.REVIEW_REPAIRING
        task.reviews.append(ReviewRound(
            round=2,
            decision="changes_requested",
            summary="仍有阻塞问题需要修复",
        ))
        orchestrator.store.save_task(task)

        with self.assertRaisesRegex(ValueError, "任务恢复入口"):
            orchestrator.run_review(task.id)

        event_types = [item.event_type for item in orchestrator.store.list_events(task.id)]
        self.assertNotIn("review_repair_resumed", event_types)

    def test_reviewer_recognizes_use_state_setter_as_defined_click_handler(self):
        orchestrator = build_orchestrator(self.runtime)
        task = orchestrator.create_task(
            "状态切换", "点击导航项后切换当前页面，并保留原有页面内容。"
        )
        completed = orchestrator.approve(task.id, "tester", "方案可执行")
        completed.repository_analysis.framework = "react"
        target = Path(completed.workspace) / "task_service.py"
        target.write_text(
            "import { useState } from 'react';\n"
            "function App() {\n"
            "  const [activePage, setActivePage] = useState('Home');\n"
            "  return <button onClick={() => setActivePage('Subscriptions')}>切换</button>;\n"
            "}\n",
            encoding="utf-8",
        )
        completed.result.diff = """diff --git a/task_service.py b/task_service.py
--- a/task_service.py
+++ b/task_service.py
@@ -1,0 +1,5 @@
+import { useState } from 'react';
+function App() {
+  const [activePage, setActivePage] = useState('Home');
+  return <button onClick={() => setActivePage('Subscriptions')}>切换</button>;
+}
diff --git a/tests/task_service.test.js b/tests/task_service.test.js
--- a/tests/task_service.test.js
+++ b/tests/task_service.test.js
@@ -1,0 +1,1 @@
+test('switches page', () => {});
"""
        completed.technical_plan.affected_files.append("tests/task_service.test.js")

        review = orchestrator.code_reviewer.review(completed, 2)

        messages = [item.message for item in review.findings]
        self.assertFalse(any("setActivePage" in message and "未定义" in message for message in messages))

    def test_failed_retry_expands_scope_from_build_error_and_keeps_same_task(self):
        orchestrator = build_orchestrator(self.runtime)
        task = orchestrator.create_task(
            "失败续跑示例", "验证失败任务能够诊断报错文件并在相同工作区继续开发。"
        )
        task = orchestrator.approve(task.id, "tester", "方案可执行")
        workspace = Path(task.workspace)
        (workspace / "broken.css").write_text("main { color: red; }}\n", encoding="utf-8")
        task.status = TaskStatus.FAILED
        task.error = "Automatic repair exhausted after 3 attempts"
        task.technical_plan.affected_files = ["task_service.py"]
        task.development_attempts.append(DevelopmentAttempt(
            attempt=1,
            summary="初次修改",
            changed_files=["task_service.py"],
            test_command=["npm", "run", "build"],
            exit_code=1,
            output=f"Syntax error: {workspace / 'broken.css'} Unexpected }} (1:21)",
        ))
        orchestrator.store.save_task(task)
        repository = orchestrator.store.get_repository("demo")
        repository.execution_mode = "plan_only"
        orchestrator.store.save_repository(repository)

        class RetryDeveloper:
            model_gateway = type("Gateway", (), {"enabled": True})()

            def run(self, retried_task, repository_path, **kwargs):
                self.scope = list(retried_task.technical_plan.affected_files)
                self.repair_context = kwargs["resume_payload"]["repair_context"]
                return DeveloperRunOutcome(
                    kind="risk_approval",
                    pending_proposal=DevelopmentProposal(
                        replacements=[TextReplacement(
                            path="broken.css",
                            search="main { color: red; }}",
                            replace="main { color: red; }",
                        )],
                        test_command="npm run build",
                    ),
                    risk_reasons=["test stop"],
                )

        retry_developer = RetryDeveloper()
        orchestrator.generic_developer = retry_developer

        retried = orchestrator.retry_failed_task(task.id, "retry-job")

        self.assertEqual(retried.id, task.id)
        self.assertEqual(retried.status, TaskStatus.WAITING_RISK_APPROVAL)
        self.assertIn("broken.css", retry_developer.scope)
        self.assertIn("Unexpected", retry_developer.repair_context)
        self.assertEqual(retried.metadata["failure_retry_count"], 1)
        self.assertEqual(retried.metadata["last_retry_added_files"], ["broken.css"])
        event_types = [item.event_type for item in orchestrator.store.list_events(task.id)]
        self.assertIn("failure_retry_started", event_types)

    def test_failed_retry_adds_entrypoint_for_wiring_requirement(self):
        orchestrator = build_orchestrator(self.runtime)
        task = orchestrator.create_task(
            "恢复调用", "恢复原来的导入和调用方式。"
        )
        task.status = TaskStatus.FAILED
        task.workspace = str(self.runtime / "tasks" / task.id / "repo")
        Path(task.workspace).mkdir(parents=True)
        (Path(task.workspace) / "main.py").write_text("print('ok')\n", encoding="utf-8")
        task.repository_analysis.entrypoints = ["main.py"]
        task.technical_plan.affected_files = ["task_service.py"]
        task.error = "No-op proposal"
        orchestrator.store.save_task(task)
        repository = orchestrator.store.get_repository("demo")
        repository.execution_mode = "plan_only"
        orchestrator.store.save_repository(repository)

        class RetryDeveloper:
            model_gateway = type("Gateway", (), {"enabled": True})()

            def run(self, retried_task, repository_path, **kwargs):
                self.scope = list(retried_task.technical_plan.affected_files)
                return DeveloperRunOutcome(kind="failed", error="stop")

        developer = RetryDeveloper()
        orchestrator.generic_developer = developer

        retried = orchestrator.retry_failed_task(task.id, "retry-wiring")

        self.assertIn("main.py", developer.scope)
        self.assertEqual(retried.metadata["last_retry_added_files"], ["main.py"])

    def test_user_feedback_continues_same_task_before_pull_request(self):
        orchestrator = build_orchestrator(self.runtime)
        task = orchestrator.create_task(
            "持续验收", "任务支持 low、medium、high，默认使用 medium，非法值必须拒绝。"
        )
        task = orchestrator.approve(task.id, "tester", "同意")
        repository = orchestrator.store.get_repository("demo")
        repository.execution_mode = "plan_only"
        orchestrator.store.save_repository(repository)

        class FeedbackDeveloper:
            model_gateway = type("Gateway", (), {"enabled": True})()

            def run(self, current_task, repository_path, **kwargs):
                self.feedback = kwargs["review_feedback"]
                target = Path(repository_path) / "task_service.py"
                target.write_text(
                    target.read_text(encoding="utf-8") + "\n# clearer priority documentation\n",
                    encoding="utf-8",
                )
                return DeveloperRunOutcome(kind="success", result=current_task.result)

        developer = FeedbackDeveloper()
        orchestrator.generic_developer = developer

        updated = orchestrator.apply_user_feedback(
            task.id,
            "tester",
            "保留已有接口，把默认优先级说明写得更清楚。",
        )

        self.assertEqual(updated.id, task.id)
        self.assertEqual(updated.status, TaskStatus.WAITING_RELEASE_APPROVAL)
        self.assertIn("默认优先级", developer.feedback)
        self.assertEqual(len(updated.metadata["user_feedback_rounds"]), 1)
        feedback_round = updated.metadata["user_feedback_rounds"][0]
        self.assertIn("task_service.py", feedback_round["changed_files"])
        self.assertIn("clearer priority documentation", feedback_round["diff"])
        self.assertIn("clearer priority documentation", updated.metadata["workspace_diff"])
        self.assertIn("查看本轮 Diff", feedback_round["agent_message"])
        self.assertEqual(updated.reviews[-1].round, 2)
        event_types = [item.event_type for item in orchestrator.store.list_events(task.id)]
        self.assertIn("user_feedback_submitted", event_types)
