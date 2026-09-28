from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from dev_agent.domain.models import Task, TaskStatus


class TaskDeliveryState(TypedDict, total=False):
    action: str
    task_id: str
    actor: str
    comment: str
    checkpoint_job_id: str | None
    should_pause: Callable[[], bool] | None
    checkpoint_handler: Callable[[str, str, dict], bool] | None
    task: Task
    repository: Any
    route: str


class TaskDeliveryGraph:
    """Top-level LangGraph for the plan-to-reviewed-change delivery path.

    The orchestrator is injected as an application-service adapter. It exposes
    storage, Git and agent operations, while this graph owns which agent or
    delivery node runs next.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        builder = StateGraph(TaskDeliveryState)
        builder.add_node("dispatch", lambda state: state)
        builder.add_node("approve_plan", self.adapter._graph_approve_plan)
        builder.add_node("load_review", self.adapter._graph_load_review)
        builder.add_node("develop", self.adapter._graph_develop)
        builder.add_node("generate_mr", self.adapter._graph_generate_mr)
        builder.add_node("review", self.adapter._graph_review)
        builder.add_edge(START, "dispatch")
        builder.add_conditional_edges(
            "dispatch",
            lambda state: state["action"],
            {"approve": "approve_plan", "review": "load_review"},
        )
        builder.add_conditional_edges(
            "approve_plan",
            self._route_after_approval,
            {"develop": "develop", "done": END},
        )
        builder.add_conditional_edges(
            "load_review",
            self._route_review_entry,
            {"generate_mr": "generate_mr", "review": "review"},
        )
        builder.add_conditional_edges(
            "develop",
            self._route_after_development,
            {"generate_mr": "generate_mr", "done": END},
        )
        builder.add_edge("generate_mr", "review")
        builder.add_edge("review", END)
        self.graph = builder.compile(name="task-delivery-workflow")

    def approve(
        self,
        task_id: str,
        actor: str,
        comment: str = "",
        *,
        checkpoint_job_id: str | None = None,
        should_pause: Callable[[], bool] | None = None,
    ) -> Task:
        result = self.graph.invoke({
            "action": "approve",
            "task_id": task_id,
            "actor": actor,
            "comment": comment,
            "checkpoint_job_id": checkpoint_job_id,
            "should_pause": should_pause,
        })
        return result["task"]

    def review(self, task_id: str) -> Task:
        result = self.graph.invoke({"action": "review", "task_id": task_id})
        return result["task"]

    @staticmethod
    def _route_after_approval(state: TaskDeliveryState) -> str:
        if state.get("route") == "done":
            return "done"
        return "develop" if state["task"].status == TaskStatus.DEVELOPING else "done"

    @staticmethod
    def _route_after_development(state: TaskDeliveryState) -> str:
        return "generate_mr" if state["task"].status == TaskStatus.CHANGE_READY else "done"

    @staticmethod
    def _route_review_entry(state: TaskDeliveryState) -> str:
        return (
            "generate_mr"
            if state["task"].status == TaskStatus.CHANGE_READY
            else "review"
        )
