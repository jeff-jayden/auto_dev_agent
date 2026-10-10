"""Shared task history for model calls, rebuilt from persisted task state."""

from domain.models import Task


def build_task_context(task: Task) -> dict:
    """Keep requests lossless; bound bulky evidence and use live code as truth."""
    rounds = task.metadata.get("user_feedback_rounds", [])
    history = []
    for index, item in enumerate(rounds, 1):
        rolled_back = bool(item.get("rolled_back_at"))
        history.append({
            "round": index,
            "request": item.get("feedback", ""),
            "status": item.get("status", "unknown"),
            "operation": item.get("operation", "modify"),
            "rolled_back": rolled_back,
            "result": item.get("agent_message", "")[:1500],
            "changed_files": item.get("changed_files", []),
            "test_exit_code": item.get("test_exit_code"),
        })
    recent_evidence = []
    for index, item in list(enumerate(rounds, 1))[-4:]:
        diff = item.get("diff", "")
        recent_evidence.append({
            "round": index,
            "diff": diff[:4000],
            "diff_truncated": len(diff) > 4000,
        })
    result = task.result
    return {
        "instructions": (
            "以下为同一任务的历史数据。按时间理解需求；后续明确更正覆盖冲突的旧要求，"
            "未冲突的要求继续保留，不得重做或删除此前正确实现。历史不扩大 allowed_files。"
            "rolled_back=true 的轮次及 rollback 操作只用于解释撤销，不作为当前实现目标。"
            "failed、paused、running 等状态不代表需求已完成，测试通过也不等于用户验收。"
            "历史 Diff 是过程证据，当前 repository_context/current_files 和累计 Diff 才是代码现状；"
            "发生冲突时读取当前代码确认，不直接重放历史 Patch。"
        ),
        "original_requirement": task.requirement,
        "plan_feedback": task.metadata.get("plan_feedback", []),
        "feedback_history": history,
        "recent_change_evidence": recent_evidence,
        "development_history": [
            {"attempt": item.attempt, "summary": item.summary[:1500],
             "changed_files": item.changed_files, "test_exit_code": item.exit_code}
            for item in task.development_attempts[-8:]
        ],
        "current_result": None if result is None else {
            "test_exit_code": result.exit_code,
            "cumulative_diff": result.diff[:12000],
            "diff_truncated": len(result.diff) > 12000,
        },
    }
