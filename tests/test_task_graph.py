import unittest

from dev_agent.domain.models import Task, TaskStatus
from dev_agent.workflows.task_graph import TaskDeliveryGraph


class FakeDeliveryAdapter:
    def __init__(self, stop_after_development: TaskStatus | None = None):
        self.calls = []
        self.stop_after_development = stop_after_development
        self.task = Task(
            id="graph-delivery",
            title="Graph delivery",
            requirement="Route agents with LangGraph",
            status=TaskStatus.WAITING_REQUIREMENT_APPROVAL,
        )
        self.repository = object()

    def _graph_approve_plan(self, state):
        self.calls.append("approve_plan")
        self.task.status = TaskStatus.DEVELOPING
        return {"task": self.task, "repository": self.repository}

    def _graph_load_review(self, state):
        self.calls.append("load_review")
        return {"task": self.task, "repository": self.repository}

    def _graph_develop(self, state):
        self.calls.append("develop")
        self.task.status = self.stop_after_development or TaskStatus.CHANGE_READY
        return {"task": self.task}

    def _graph_generate_mr(self, state):
        self.calls.append("generate_mr")
        self.task.status = TaskStatus.GENERATING_MR
        return {"task": self.task}

    def _graph_review(self, state):
        self.calls.append("review")
        self.task.status = TaskStatus.WAITING_RELEASE_APPROVAL
        return {"task": self.task}


class TaskDeliveryGraphTests(unittest.TestCase):
    def test_approval_routes_through_developer_mr_and_reviewer(self):
        adapter = FakeDeliveryAdapter()

        task = TaskDeliveryGraph(adapter).approve(task_id=adapter.task.id, actor="tester")

        self.assertEqual(
            adapter.calls,
            ["approve_plan", "develop", "generate_mr", "review"],
        )
        self.assertEqual(task.status, TaskStatus.WAITING_RELEASE_APPROVAL)

    def test_risk_gate_stops_graph_before_mr_and_review(self):
        adapter = FakeDeliveryAdapter(TaskStatus.WAITING_RISK_APPROVAL)

        task = TaskDeliveryGraph(adapter).approve(task_id=adapter.task.id, actor="tester")

        self.assertEqual(adapter.calls, ["approve_plan", "develop"])
        self.assertEqual(task.status, TaskStatus.WAITING_RISK_APPROVAL)

    def test_manual_review_of_ready_change_generates_mr_before_review(self):
        adapter = FakeDeliveryAdapter()
        adapter.task.status = TaskStatus.CHANGE_READY

        task = TaskDeliveryGraph(adapter).review(adapter.task.id)

        self.assertEqual(adapter.calls, ["load_review", "generate_mr", "review"])
        self.assertEqual(task.status, TaskStatus.WAITING_RELEASE_APPROVAL)


if __name__ == "__main__":
    unittest.main()
