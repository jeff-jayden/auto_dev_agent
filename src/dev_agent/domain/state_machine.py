from .models import TaskStatus


ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.REQUIREMENT_ANALYSIS: {
        TaskStatus.WAITING_REQUIREMENT_APPROVAL,
        TaskStatus.WAITING_REQUIREMENT_INPUT,
        TaskStatus.FAILED,
    },
    TaskStatus.WAITING_REQUIREMENT_INPUT: {TaskStatus.REQUIREMENT_ANALYSIS, TaskStatus.REJECTED},
    TaskStatus.WAITING_REQUIREMENT_APPROVAL: {
        TaskStatus.REQUIREMENT_ANALYSIS,
        TaskStatus.PLAN_APPROVED,
        TaskStatus.DEVELOPING,
        TaskStatus.REJECTED,
    },
    TaskStatus.PLAN_APPROVED: {TaskStatus.DEVELOPING},
    TaskStatus.DEVELOPING: {
        TaskStatus.WAITING_REQUIREMENT_APPROVAL,
        TaskStatus.WAITING_RISK_APPROVAL,
        TaskStatus.TESTING,
        TaskStatus.FAILED,
    },
    TaskStatus.WAITING_RISK_APPROVAL: {
        TaskStatus.DEVELOPING,
        TaskStatus.REVIEW_REPAIRING,
        TaskStatus.REJECTED,
    },
    TaskStatus.TESTING: {TaskStatus.REPAIRING, TaskStatus.CHANGE_READY, TaskStatus.FAILED},
    TaskStatus.REPAIRING: {
        TaskStatus.TESTING,
        TaskStatus.WAITING_RISK_APPROVAL,
        TaskStatus.FAILED,
    },
    TaskStatus.CHANGE_READY: {TaskStatus.GENERATING_MR},
    TaskStatus.GENERATING_MR: {TaskStatus.REVIEWING, TaskStatus.FAILED},
    TaskStatus.REVIEWING: {
        TaskStatus.CHANGES_REQUESTED,
        TaskStatus.REVIEW_APPROVED,
        TaskStatus.FAILED,
    },
    TaskStatus.CHANGES_REQUESTED: {
        TaskStatus.REVIEW_REPAIRING,
        TaskStatus.REVIEWING,
        TaskStatus.REVIEW_APPROVED,
        TaskStatus.REJECTED,
    },
    TaskStatus.REVIEW_REPAIRING: {
        TaskStatus.GENERATING_MR,
        TaskStatus.CHANGES_REQUESTED,
        TaskStatus.WAITING_RISK_APPROVAL,
        TaskStatus.FAILED,
    },
    TaskStatus.REVIEW_APPROVED: {TaskStatus.WAITING_RELEASE_APPROVAL},
    TaskStatus.WAITING_RELEASE_APPROVAL: {
        TaskStatus.CHANGES_REQUESTED,
        TaskStatus.PUBLISHING_PULL_REQUEST,
        TaskStatus.REJECTED,
    },
    TaskStatus.PUBLISHING_PULL_REQUEST: {
        TaskStatus.WAITING_MERGE_APPROVAL,
        TaskStatus.WAITING_RELEASE_APPROVAL,
        TaskStatus.FAILED,
    },
    TaskStatus.WAITING_MERGE_APPROVAL: {
        TaskStatus.CHANGES_REQUESTED,
        TaskStatus.MERGING,
        TaskStatus.REJECTED,
    },
    TaskStatus.MERGING: {TaskStatus.MERGED, TaskStatus.WAITING_MERGE_APPROVAL, TaskStatus.FAILED},
    TaskStatus.MERGED: set(),
    TaskStatus.REJECTED: set(),
    # A failed task may be retried in the same isolated workspace after the
    # failure has been diagnosed and the approved repair scope has been updated.
    TaskStatus.FAILED: {TaskStatus.DEVELOPING},
}


def ensure_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"Illegal task transition: {current.value} -> {target.value}")
