import unittest
from pathlib import Path
from types import SimpleNamespace

from dev_agent.agents.planner import LocalPlanningAgent
from dev_agent.agents.generic_developer import DeveloperRunOutcome
from dev_agent.domain.models import (
    DevelopmentAttempt,
    DevelopmentStep,
    ExecutionResult,
    Task,
    TaskStatus,
    TechnicalPlan,
)
from dev_agent.workflows.orchestrator import TaskOrchestrator


class StepwiseDevelopmentTests(unittest.TestCase):
    def test_plan_steps_are_scoped_ordered_and_cover_approved_files(self):
        plan = TechnicalPlan(
            approach="增加页面并补充测试",
            affected_files=["src/App.js", "src/App.css", "src/App.test.js"],
            implementation_steps=["实现页面", "增加样式", "补充测试"],
            test_plan=["npm test"],
            risks=[],
            development_steps=[DevelopmentStep(
                id="model-id",
                title="实现页面",
                objective="实现页面主体",
                allowed_files=["src/App.js", "outside.js"],
            )],
        )

        steps = LocalPlanningAgent.ensure_development_steps(plan, None)

        self.assertEqual([step.id for step in steps], ["step-1", "step-2", "step-3"])
        self.assertEqual(steps[0].allowed_files, ["src/App.js"])
        self.assertEqual(steps[1].allowed_files, ["src/App.css"])
        self.assertEqual(steps[2].allowed_files, ["src/App.test.js"])
        self.assertEqual(steps[1].depends_on, ["step-1"])
        self.assertEqual(
            {path for step in steps for path in step.allowed_files},
            set(plan.affected_files),
        )

    def test_old_task_json_defaults_to_legacy_single_flow(self):
        task = Task.model_validate({
            "id": "legacy",
            "title": "旧任务",
            "requirement": "保持兼容",
            "status": TaskStatus.WAITING_REQUIREMENT_APPROVAL,
            "technical_plan": {
                "approach": "最小修改",
                "affected_files": ["main.py"],
                "implementation_steps": ["修改 main.py"],
                "test_plan": ["python -m unittest"],
                "risks": [],
            },
        })

        self.assertEqual(task.technical_plan.development_steps, [])
        self.assertEqual(task.step_executions, [])

    def test_one_developer_session_shares_context_and_uses_global_attempt_numbers(self):
        steps = [DevelopmentStep(
            id=f"step-{index}", title=f"步骤 {index}", objective=f"完成 {index}",
            allowed_files=[f"file-{index}.txt"],
        ) for index in range(1, 4)]
        task = Task(
            id="session-task", title="连续开发", requirement="依次完成三个步骤",
            status=TaskStatus.DEVELOPING,
            technical_plan=TechnicalPlan(
                approach="连续执行", affected_files=[f"file-{index}.txt" for index in range(1, 4)],
                implementation_steps=[step.title for step in steps], test_plan=["test"], risks=[],
                development_steps=steps,
            ),
        )

        class Store:
            def __init__(self):
                self.events = []

            def save_task(self, saved_task):
                self.saved_task = saved_task

            def add_event(self, event):
                self.events.append(event)

        class Developer:
            def __init__(self):
                self.context_ids = []
                self.context_snapshots = []

            def run(self, current_task, workspace, **kwargs):
                context = kwargs["session_context"]
                self.context_ids.append(id(context))
                self.context_snapshots.append(set(context))
                step = kwargs["step_context"]
                context[step["allowed_files"][0]] = step["objective"]
                result = ExecutionResult(
                    success=True, command=["test"], exit_code=0, output="ok", diff="diff",
                    mr_title="title", mr_description="description",
                )
                return DeveloperRunOutcome(
                    kind="success",
                    attempts=[DevelopmentAttempt(
                        attempt=1, summary=step["objective"], changed_files=step["allowed_files"],
                        test_command=["test"], exit_code=0, output="ok", diff="diff",
                    )],
                    result=result,
                )

        orchestrator = TaskOrchestrator.__new__(TaskOrchestrator)
        orchestrator.store = Store()
        orchestrator.generic_developer = Developer()
        orchestrator.tracer = None
        outcome = orchestrator._run_development_steps(
            task, Path("."), SimpleNamespace(local_path="."),
        )

        self.assertEqual(outcome.kind, "success")
        self.assertEqual([attempt.attempt for attempt in outcome.attempts], [1, 2, 3])
        self.assertEqual([attempt.step_attempt for attempt in outcome.attempts], [1, 1, 1])
        self.assertEqual([attempt.step_id for attempt in outcome.attempts], ["step-1", "step-2", "step-3"])
        self.assertEqual(len(set(orchestrator.generic_developer.context_ids)), 1)
        self.assertEqual(orchestrator.generic_developer.context_snapshots[1], {"file-1.txt"})
        self.assertEqual(orchestrator.generic_developer.context_snapshots[2], {"file-1.txt", "file-2.txt"})
        self.assertTrue(task.metadata["developer_session_id"])
        self.assertEqual(task.metadata["developer_session_files"], ["file-1.txt", "file-2.txt", "file-3.txt"])


if __name__ == "__main__":
    unittest.main()
