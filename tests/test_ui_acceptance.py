import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.api.main import build_orchestrator
from dev_agent.agents.mr_writer import MergeRequestWriter
from dev_agent.domain.models import (
    DesignReference,
    ExecutionResult,
    Task,
    TaskStatus,
    UIAcceptanceCheck,
    UIAcceptanceReport,
)
from dev_agent.ui_validation import FigmaMCPClient, UIAcceptanceService


class UIAcceptanceTests(unittest.TestCase):
    def test_parses_specific_figma_frame_url(self):
        file_key, node_id = FigmaMCPClient.parse_url(
            "https://www.figma.com/design/abc123/Product?node-id=12-34"
        )

        self.assertEqual(file_key, "abc123")
        self.assertEqual(node_id, "12:34")

    def test_rejects_figma_url_without_node_id(self):
        with self.assertRaisesRegex(ValueError, "node-id"):
            FigmaMCPClient.parse_url("https://www.figma.com/design/abc123/Product")

    def test_design_without_preview_is_explicitly_not_run(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Task(
                id="task-1",
                title="UI task",
                requirement="Match the design",
                status=TaskStatus.CHANGE_READY,
                design_reference=DesignReference(
                    url="https://www.figma.com/design/abc/Product?node-id=1-2",
                    file_key="abc",
                    node_id="1:2",
                    snapshot_hash="a" * 64,
                ),
            )

            report = UIAcceptanceService(Path(directory)).validate(task)

            self.assertEqual(report.status, "not_run")
            self.assertFalse(report.blocking)
            self.assertEqual(report.checks[-1].status, "skipped")

    def test_missing_browser_is_a_blocking_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Task(
                id="task-1",
                title="UI task",
                requirement="Match the design",
                status=TaskStatus.CHANGE_READY,
                design_reference=DesignReference(
                    url="https://www.figma.com/design/abc/Product?node-id=1-2",
                    file_key="abc",
                    node_id="1:2",
                    preview_url="http://127.0.0.1:3000/",
                    snapshot_hash="a" * 64,
                ),
            )
            service = UIAcceptanceService(Path(directory))

            with patch.object(service, "_find_browser", return_value=None):
                report = service.validate(task)

            self.assertEqual(report.status, "failed")
            self.assertTrue(report.blocking)

    def test_task_creation_captures_figma_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = build_orchestrator(Path(directory) / "runtime")

            class FakeFigmaClient:
                def capture(self, task_id, figma_url, preview_url, width, height):
                    return DesignReference(
                        url=figma_url,
                        file_key="file-key",
                        node_id="7:8",
                        preview_url=preview_url,
                        viewport_width=width,
                        viewport_height=height,
                        context="<button>保存</button>",
                        variables='{"primary":"#2563eb"}',
                        snapshot_hash="b" * 64,
                    )

            orchestrator.figma_client = FakeFigmaClient()
            task = orchestrator.create_task(
                "实现设计稿",
                "按照指定设计稿实现保存按钮和页面布局。",
                figma_url="https://www.figma.com/design/file-key/Product?node-id=7-8",
                preview_url="http://127.0.0.1:3000/",
                viewport_width=1280,
                viewport_height=720,
            )

            self.assertEqual(task.design_reference.node_id, "7:8")
            self.assertEqual(task.design_reference.viewport_width, 1280)
            event_types = [
                event.event_type for event in orchestrator.store.list_events(task.id)
            ]
            self.assertIn("figma_baseline_captured", event_types)

    def test_mr_description_contains_ui_evidence(self):
        task = Task(
            id="task-1",
            title="UI task",
            requirement="Match the design",
            status=TaskStatus.CHANGE_READY,
            design_reference=DesignReference(
                url="https://www.figma.com/design/abc/Product?node-id=1-2",
                file_key="abc",
                node_id="1:2",
                snapshot_hash="a" * 64,
            ),
            ui_acceptance=UIAcceptanceReport(
                status="warning",
                summary="UI acceptance completed",
                similarity_score=82.5,
                design_snapshot_hash="a" * 64,
                checks=[
                    UIAcceptanceCheck(
                        category="visual",
                        name="截图视觉一致性",
                        status="warning",
                        detail="相似度 82.5%",
                    )
                ],
            ),
            result=ExecutionResult(
                success=True,
                command=["npm", "test"],
                exit_code=0,
                output="passed",
                diff="diff --git a/src/App.js b/src/App.js\n+++ b/src/App.js\n",
                mr_title="feat: ui",
                mr_description="",
            ),
        )

        draft = MergeRequestWriter().write(task)

        self.assertIn("## UI 验收", draft.description)
        self.assertIn("82.5%", draft.description)


if __name__ == "__main__":
    unittest.main()
