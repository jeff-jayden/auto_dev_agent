"""Bounded working memory and lossless paginated execution evidence."""

from llm.context_budget import token_upper_bound


def excerpt(text, budget=900):
    piece = text
    while token_upper_bound(piece) > budget:
        piece = piece[:max(1, len(piece) // 2)]
    return piece


def query_execution_memory(task, offset=0, text_offset=0, limit=2):
    if min(offset, text_offset) < 0 or not 1 <= limit <= 4:
        raise ValueError("Invalid execution memory pagination")
    attempts = task.development_attempts
    records = []
    for item in attempts[offset:offset + limit]:
        piece = excerpt(item.output[text_offset:], 1200)
        records.append({"attempt": item.attempt, "summary": item.summary,
                        "test_exit_code": item.exit_code, "changed_files": item.changed_files,
                        "output": piece, "next_text_offset": text_offset + len(piece)
                        if text_offset + len(piece) < len(item.output) else None})
    return {"records": records, "total_matches": len(attempts),
            "next_offset": offset + len(records) if offset + len(records) < len(attempts) else None}


def working_memory(task):
    rounds = task.metadata.get("user_feedback_rounds", [])
    failed = next(((i, r) for i, r in reversed(list(enumerate(rounds, 1)))
                   if r.get("status") == "failed" and not r.get("rolled_back_at")), None)
    latest = task.development_attempts[-1] if task.development_attempts else None
    review = task.reviews[-1] if task.reviews else None
    return {"task_status": task.status.value, "current_error": excerpt(task.error or ""),
            "latest_failed_round": None if not failed else {
                "source_round": failed[0], "error": excerpt(failed[1].get("agent_message", "")),
                "historical_failure_not_necessarily_current": True},
            "latest_execution": None if latest is None else {
                "attempt": latest.attempt, "test_exit_code": latest.exit_code,
                "output_excerpt": excerpt(latest.output), "changed_files": latest.changed_files,
                "history_offset": len(task.development_attempts) - 1},
            "steps": [{"step_id": s.step_id, "status": s.status, "error": excerpt(s.error or "")}
                      for s in task.step_executions if s.status != "completed"][-4:],
            "latest_review": None if review is None else {
                "round": review.round, "decision": review.decision, "summary": excerpt(review.summary),
                "blocking_findings": [dict(path=f.path, message=excerpt(f.message, 300))
                                      for f in review.findings if f.blocking][:4]},
            "delivery": {"pr_number": task.remote_pull_request.number if task.remote_pull_request else None,
                         "user_acceptance": "not_inferred_from_tests"},
            "retry_policy": "重试先核对当前失败阶段和最新执行证据；历史失败可能已解决。测试通过不代表审查、发布或用户验收完成。",
            "evidence_tool": "query_execution_memory"}
