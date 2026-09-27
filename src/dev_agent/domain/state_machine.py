from .models import TaskStatus


def ensure_transition(current: TaskStatus, target: TaskStatus) -> None:
    """Compatibility shim; workflow transitions are owned by LangGraph."""
    from dev_agent.workflows.workflow_graph import ensure_workflow_transition

    ensure_workflow_transition(current, target)
