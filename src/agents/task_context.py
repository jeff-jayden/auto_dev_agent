"""Task summaries and paginated access to immutable conversation evidence."""

import hashlib
import json
import os
import re

from domain.models import Task
from llm.context_budget import token_upper_bound, ContextBudgetExceeded


def refresh_task_summary(task: Task) -> dict:
    rounds = task.metadata.get("user_feedback_rounds", [])
    summary = {
        "version": 2,
        "original_requirement": task.requirement,
        "acceptance_criteria": task.analysis.acceptance_criteria if task.analysis else [],
        "user_requirements": [],
        "actual_progress": [],
        "protected_requirements": [],
        "latest_development": [
            {"attempt": item.attempt, "summary": item.summary[:500],
             "changed_files": item.changed_files, "test_exit_code": item.exit_code}
            for item in task.development_attempts[-3:]
        ],
        "total_rounds": len(rounds),
    }
    for index, item in enumerate(rounds, 1):
        text = item.get("feedback", "")
        inactive = bool(item.get("rolled_back_at")) or item.get("operation") == "rollback"
        summary["user_requirements"].append({
            "source_round": index, "preview": text[:160], "preview_truncated": len(text) > 160,
            "status": "withdrawn_or_rollback" if inactive else "requested_not_assumed_complete",
        })
        summary["actual_progress"].append({
            "source_round": index, "status": item.get("status", "unknown"),
            "changed_files": item.get("changed_files", []),
            "test_exit_code": item.get("test_exit_code"), "rolled_back": inactive,
            "user_acceptance": "not_inferred_from_tests",
        })
        if not inactive:
            for line in re.split(r"[\n。；]", text):
                if re.search(r"必须|保留|不要|不得|不能|禁止|保持|must|never|preserve|do not", line, re.I):
                    summary["protected_requirements"].append({"source_round": index, "text": line})
    summary["source_digest"] = hashlib.sha256(json.dumps(
        [task.requirement, rounds, summary["latest_development"], summary["acceptance_criteria"]],
        ensure_ascii=False, sort_keys=True, default=str,
    ).encode()).hexdigest()
    task.metadata["task_summary"] = summary
    return summary


def query_task_history(task: Task, *, query: str = "", round_number: int | None = None,
                       file: str = "", offset: int = 0, limit: int = 2,
                       text_offset: int = 0, field: str = "feedback") -> dict:
    """Search complete persisted rounds, including failed and reverted changes.

    Results have separate pagination for records and long text. No model
    summary replaces the original source material.
    """
    if field not in {"feedback", "agent_message", "diff"}:
        raise ValueError("History field must be feedback, agent_message, or diff")
    if min(offset, text_offset) < 0 or not 1 <= limit <= 4:
        raise ValueError("Invalid history pagination")
    matches = []
    for index, item in enumerate(task.metadata.get("user_feedback_rounds", []), 1):
        if round_number is not None and index != round_number:
            continue
        if file and file not in item.get("changed_files", []) and file not in item.get("diff", ""):
            continue
        if query and query.casefold() not in json.dumps(item, ensure_ascii=False).casefold():
            continue
        matches.append((index, item))
    results = []
    for index, item in matches[offset:offset + limit]:
        text = str(item.get(field, ""))
        # 1200 UTF-8 bytes is a conservative per-record token cap.
        piece = text[text_offset:]
        while token_upper_bound(piece) > 1200:
            piece = piece[:max(1, len(piece) // 2)]
        results.append({"source_round": index, "field": field, "text": piece,
                        "status": item.get("status", "unknown"),
                        "rolled_back": bool(item.get("rolled_back_at")),
                        "operation": item.get("operation", "modify"),
                        "changed_files": item.get("changed_files", []),
                        "test_exit_code": item.get("test_exit_code"),
                        "next_text_offset": text_offset + len(piece) if text_offset + len(piece) < len(text) else None})
    return {"records": results, "total_matches": len(matches),
            "next_offset": offset + len(results) if offset + len(results) < len(matches) else None}


def build_task_context(task: Task) -> dict:
    """Send a bounded projection; full requirements stay durably queryable."""
    summary = refresh_task_summary(task)
    count = max(1, min(int(os.getenv("TASK_CONTEXT_RECENT_ROUNDS", "3")), 8))
    rounds = task.metadata.get("user_feedback_rounds", [])
    recent = []
    for index, item in list(enumerate(rounds, 1))[-count:]:
        recent.append({"round": index, "request": item.get("feedback", ""),
                       "status": item.get("status", "unknown"),
                       "operation": item.get("operation", "modify"),
                       "rolled_back": bool(item.get("rolled_back_at")),
                       "result": item.get("agent_message", "")[:500],
                       "changed_files": item.get("changed_files", []),
                       "test_exit_code": item.get("test_exit_code")})
    # Old requirements have source-linked previews. Protected constraints and
    # the complete latest requests are never silently shortened to fit.
    projection = dict(summary)
    projection["user_requirements"] = list(summary["user_requirements"])
    projection["actual_progress"] = summary["actual_progress"][-count:]
    context = {
        "instructions": (
            "task_summary 是带来源轮次的索引摘要，preview 不是完整要求。最近反馈保留原文。"
            "用户要求与实际完成状态分开；测试通过不等于用户验收，failed/running 不代表完成。"
            "撤销轮次不再作为当前目标。更正只覆盖明确冲突的旧要求，其他要求继续有效；不自行推断覆盖。"
            "历史不扩大 allowed_files。缺少历史细节时调用 query_task_history，按轮次/文件/关键词分页查询原文。"
            "涉及旧实现的修改或删除前必须核对相关原始要求；未查询到的要求不能视为不存在。"
            "历史 Diff 是过程证据，当前代码和累计 Diff 才是现状，不重放历史 Patch。"
        ),
        "task_summary": projection,
        "plan_feedback": task.metadata.get("plan_feedback", []),
        "feedback_history": recent,
        "history_access": {"tool": "query_task_history", "total_rounds": len(rounds),
                           "omitted_summary_rounds": [], "fields": ["feedback", "agent_message", "diff"]},
        "retrieved_history": task.metadata.get("retrieved_history", [])[-4:],
        "current_result": None if task.result is None else {
            "test_exit_code": task.result.exit_code, "has_changes": bool(task.result.diff)},
    }
    budget = int(os.getenv("TASK_CONTEXT_MAX_TOKENS", "8000"))
    while token_upper_bound(context) + 64 > budget and projection["user_requirements"]:
        removed = projection["user_requirements"].pop(0)
        context["history_access"]["omitted_summary_rounds"].append(removed["source_round"])
    if token_upper_bound(context) + 64 > budget:
        raise ContextBudgetExceeded("Task context exceeds budget; protected constraints and current requests were preserved")
    context["estimated_tokens_upper_bound"] = token_upper_bound(context) + 64
    return context
