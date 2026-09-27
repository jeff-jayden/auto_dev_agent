from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from dev_agent.domain.models import ExecutionJob, TaskEvent, TaskStatus
from dev_agent.workflows import RecoveryCoordinator, TaskOrchestrator


TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "paused", "abandoned"}


class TaskExecutionWorker:
    def __init__(
        self,
        orchestrator_provider: Callable[[], TaskOrchestrator],
        poll_interval: float = 0.25,
        heartbeat_interval: float = 10.0,
        lease_seconds: float = 90.0,
    ):
        self._orchestrator_provider = orchestrator_provider
        self._poll_interval = poll_interval
        self._heartbeat_interval = heartbeat_interval
        self._lease_seconds = lease_seconds
        self._worker_id = f"worker-{uuid4().hex[:12]}"
        self._wake = Event()
        self._stop = Event()
        self._start_lock = Lock()
        self._thread: Thread | None = None
        self._github_poll_interval = 30.0
        self._next_github_poll_at = 0.0

    def start(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            orchestrator = self._orchestrator_provider()
            orchestrator.store.recover_incomplete_jobs(
                lambda task_id: orchestrator.latest_workflow_checkpoint(task_id) is not None
            )
            self._thread = Thread(target=self._run, name="task-execution-worker", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout)

    def enqueue_approval(self, task_id: str, actor: str, comment: str) -> ExecutionJob:
        orchestrator = self._orchestrator_provider()
        active = orchestrator.store.get_active_job(task_id, "approve")
        if active:
            self.start()
            self._wake.set()
            return active
        task = orchestrator.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status != TaskStatus.WAITING_REQUIREMENT_APPROVAL:
            raise ValueError("Only a task waiting for approval can be queued")
        candidate = ExecutionJob(
            id=uuid4().hex[:16],
            task_id=task_id,
            action="approve",
            payload={"actor": actor, "comment": comment},
        )
        job = orchestrator.store.enqueue_job(candidate)
        if job.id == candidate.id:
            orchestrator.store.add_event(
                TaskEvent(
                    task_id=task_id,
                    event_type="execution_queued",
                    message="审批已进入后台执行队列",
                    payload={"job_id": job.id},
                )
            )
        self.start()
        self._wake.set()
        return job

    def cancel(self, job_id: str) -> ExecutionJob:
        orchestrator = self._orchestrator_provider()
        job = orchestrator.store.request_job_cancel(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status in {"cancelled", "pause_requested"}:
            orchestrator.store.add_event(
                TaskEvent(
                    task_id=job.task_id,
                    event_type="execution_cancel_requested",
                    message=(
                        "后台执行已取消"
                        if job.status == "cancelled"
                        else "已请求暂停，将在下一个安全检查点生效"
                    ),
                    payload={"job_id": job.id, "job_status": job.status},
                )
            )
        self._wake.set()
        return job

    def resume(self, job_id: str, actor: str = "demo-user") -> ExecutionJob:
        orchestrator = self._orchestrator_provider()
        previous = orchestrator.store.get_job(job_id)
        if previous is None:
            raise KeyError(job_id)
        if previous.status not in {"cancelled", "paused"}:
            raise ValueError("Only a cancelled or paused execution job can be resumed")
        active_jobs = [
            item
            for item in orchestrator.store.list_jobs(previous.task_id)
            if item.status in {"queued", "running", "pause_requested"}
        ]
        if active_jobs:
            self.start()
            self._wake.set()
            return active_jobs[0]
        task = orchestrator.store.get_task(previous.task_id)
        if task is None:
            raise KeyError(previous.task_id)
        checkpoint = orchestrator.latest_workflow_checkpoint(previous.task_id)
        resumable = {
            TaskStatus.WAITING_REQUIREMENT_APPROVAL,
            TaskStatus.DEVELOPING,
            TaskStatus.TESTING,
            TaskStatus.CHANGE_READY,
            TaskStatus.REVIEWING,
            TaskStatus.CHANGES_REQUESTED,
            TaskStatus.REVIEW_REPAIRING,
        }
        if task.status not in resumable or (
            task.status != TaskStatus.WAITING_REQUIREMENT_APPROVAL and checkpoint is None
        ):
            raise ValueError(f"Task cannot resume from status: {task.status.value}")
        payload = {
            "actor": actor,
            "comment": f"Resume cancelled job {previous.id}",
            "previous_job_id": previous.id,
        }
        if previous.action == "approve":
            payload["original_actor"] = previous.payload.get("actor", actor)
            payload["original_comment"] = previous.payload.get("comment", "")
        candidate = ExecutionJob(
            id=uuid4().hex[:16],
            task_id=previous.task_id,
            action="resume",
            payload=payload,
        )
        job = orchestrator.store.enqueue_job(candidate)
        if job.id == candidate.id:
            orchestrator.store.add_event(
                TaskEvent(
                    task_id=job.task_id,
                    event_type="execution_resumed",
                    message="已从取消点创建新的后台执行任务",
                    payload={"job_id": job.id, "previous_job_id": previous.id},
                )
            )
        self.start()
        self._wake.set()
        return job

    def enqueue_recovery(self, task_id: str, actor: str = "demo-user") -> ExecutionJob:
        orchestrator = self._orchestrator_provider()
        active_jobs = [
            item for item in orchestrator.store.list_jobs(task_id)
            if item.status in {"queued", "running", "pause_requested"}
        ]
        active = active_jobs[0] if active_jobs else None
        if active and not orchestrator.store.job_lease_expired(active):
            self.start()
            self._wake.set()
            return active

        report = RecoveryCoordinator(orchestrator).diagnose(task_id)
        if report.status != "recoverable" or report.checkpoint_id is None:
            raise ValueError(report.reason)

        previous_jobs = orchestrator.store.list_jobs(task_id)
        candidate = ExecutionJob(
            id=uuid4().hex[:16],
            task_id=task_id,
            action="resume",
            payload={
                "actor": actor,
                "comment": f"Recover interrupted task from {report.checkpoint_stage}",
                "previous_job_id": previous_jobs[0].id if previous_jobs else None,
                "checkpoint_id": report.checkpoint_id,
                "recovery_reason": report.reason,
            },
        )
        job = (
            orchestrator.store.replace_stale_job_with_recovery(active.id, candidate)
            if active else orchestrator.store.enqueue_job(candidate)
        )
        if job.id == candidate.id:
            orchestrator.store.add_event(
                TaskEvent(
                    task_id=task_id,
                    event_type="recovery_queued",
                    message=f"已诊断中断并从 {report.checkpoint_stage} 检查点加入恢复队列",
                    payload={
                        "job_id": job.id,
                        "checkpoint_id": report.checkpoint_id,
                        "next_action": report.next_action,
                        "actor": actor,
                        "abandoned_job_id": active.id if active else None,
                    },
                )
            )
        self.start()
        self._wake.set()
        return job

    def retry_failed(self, task_id: str, actor: str = "demo-user") -> ExecutionJob:
        orchestrator = self._orchestrator_provider()
        active = orchestrator.store.get_active_job(task_id, "retry_failed")
        if active:
            self.start()
            self._wake.set()
            return active
        task = orchestrator.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status != TaskStatus.FAILED:
            raise ValueError("Only a failed task can be retried")
        candidate = ExecutionJob(
            id=uuid4().hex[:16],
            task_id=task_id,
            action="retry_failed",
            payload={"actor": actor},
        )
        job = orchestrator.store.enqueue_job(candidate)
        if job.id == candidate.id:
            orchestrator.store.add_event(
                TaskEvent(
                    task_id=task_id,
                    event_type="failure_retry_queued",
                    message="失败任务已进入重试队列",
                    payload={"job_id": job.id, "actor": actor},
                )
            )
        self.start()
        self._wake.set()
        return job

    def enqueue_user_feedback(
        self, task_id: str, actor: str, feedback: str
    ) -> ExecutionJob:
        orchestrator = self._orchestrator_provider()
        active = self._active_feedback_job(orchestrator, task_id)
        if active:
            self.start()
            self._wake.set()
            return active
        task = orchestrator.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if (
            task.status == TaskStatus.WAITING_MERGE_APPROVAL
            and task.remote_pull_request is not None
            and orchestrator.github_delivery is not None
        ):
            task = orchestrator.refresh_pull_request(task_id)
        if task.status == TaskStatus.MERGED:
            followup = orchestrator.create_followup_task(
                task_id, actor, feedback
            )
            return self.enqueue_approval(
                followup.id,
                actor,
                f"用户从已合入任务 {task_id} 继续对话并批准后续开发",
            )
        if task.status not in {
            TaskStatus.WAITING_RELEASE_APPROVAL,
            TaskStatus.CHANGES_REQUESTED,
            TaskStatus.WAITING_MERGE_APPROVAL,
        }:
            raise ValueError("Task is not waiting for user acceptance feedback")
        candidate = ExecutionJob(
            id=uuid4().hex[:16],
            task_id=task_id,
            action="user_feedback",
            payload={"actor": actor, "feedback": feedback},
        )
        job = orchestrator.store.enqueue_job(candidate)
        if job.id == candidate.id:
            orchestrator.store.add_event(TaskEvent(
                task_id=task_id,
                event_type="user_feedback_queued",
                message="用户修改意见已进入继续开发队列",
                payload={"job_id": job.id, "actor": actor},
            ))
        self.start()
        self._wake.set()
        return job

    def enqueue_github_comments(
        self, task_id: str, actor: str, comment_keys: list[str]
    ) -> ExecutionJob:
        orchestrator = self._orchestrator_provider()
        active = self._active_feedback_job(orchestrator, task_id)
        if active:
            raise ValueError("Another feedback repair is already running for this task")
        task = orchestrator.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if (
            task.status == TaskStatus.WAITING_MERGE_APPROVAL
            and task.remote_pull_request is not None
            and orchestrator.github_delivery is not None
        ):
            task = orchestrator.refresh_pull_request(task_id)
        if task.remote_pull_request is None or task.status != TaskStatus.WAITING_MERGE_APPROVAL:
            raise ValueError("GitHub comments can only be processed for an open pull request")
        requested = list(dict.fromkeys(comment_keys))
        if not requested:
            raise ValueError("Select at least one GitHub comment")
        comments = {
            str(item.get("key")): item
            for item in task.metadata.get("github_review_comments", [])
        }
        selected = []
        for key in requested:
            comment = comments.get(key)
            if comment is None:
                raise ValueError(f"GitHub comment is unavailable: {key}")
            if comment.get("status") not in {"pending", "failed"}:
                raise ValueError(f"GitHub comment cannot be processed again: {key}")
            selected.append(comment)
        feedback_lines = []
        for comment in selected:
            location = comment.get("path") or "PR conversation"
            if comment.get("line"):
                location += f":{comment['line']}"
            body = str(comment.get("body", "")).replace("/agent fix", "").strip()
            feedback_lines.append(
                f"- [{location}] @{comment.get('author', 'unknown')}: {body}"
            )
        feedback = "处理以下 GitHub PR 审查意见：\n" + "\n".join(feedback_lines)
        candidate = ExecutionJob(
            id=uuid4().hex[:16],
            task_id=task_id,
            action="github_comment_feedback",
            payload={
                "actor": actor,
                "feedback": feedback,
                "comment_keys": requested,
            },
        )
        job = orchestrator.store.enqueue_job(candidate)
        if job.id != candidate.id:
            raise ValueError("Another GitHub comment repair is already queued")
        orchestrator.update_github_comment_status(
            task_id, requested, "queued", job_id=job.id
        )
        orchestrator.store.add_event(TaskEvent(
            task_id=task_id,
            event_type="github_comment_fix_queued",
            message=f"{len(requested)} 条 GitHub 评论已进入 Agent 修复队列",
            payload={"job_id": job.id, "comment_keys": requested, "actor": actor},
        ))
        self.start()
        self._wake.set()
        return job

    @staticmethod
    def _active_feedback_job(
        orchestrator: TaskOrchestrator, task_id: str
    ) -> ExecutionJob | None:
        return next((
            job for job in orchestrator.store.list_jobs(task_id)
            if job.action in {"user_feedback", "github_comment_feedback"}
            and job.status in {"queued", "running", "pause_requested"}
        ), None)

    def _run(self) -> None:
        while not self._stop.is_set():
            orchestrator = self._orchestrator_provider()
            job = orchestrator.store.claim_next_job(
                self._worker_id, self._lease_seconds
            )
            if job is None:
                if monotonic() >= self._next_github_poll_at:
                    self._next_github_poll_at = monotonic() + self._github_poll_interval
                    self._poll_github_comment_commands(orchestrator)
                self._wake.wait(self._poll_interval)
                self._wake.clear()
                continue
            self._execute_with_heartbeat(orchestrator, job)

    def _execute_with_heartbeat(
        self, orchestrator: TaskOrchestrator, job: ExecutionJob
    ) -> None:
        stopped = Event()

        def beat() -> None:
            while not stopped.is_set():
                task = orchestrator.store.get_task(job.task_id)
                stage = task.status.value if task else (job.current_stage or "running")
                current = orchestrator.store.heartbeat_job(
                    job.id, self._worker_id, stage, self._lease_seconds
                )
                if current is None or current.status == "abandoned":
                    return
                stopped.wait(self._heartbeat_interval)

        heartbeat = Thread(
            target=beat,
            name=f"job-heartbeat-{job.id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            self._execute(orchestrator, job)
        finally:
            stopped.set()
            heartbeat.join(min(self._heartbeat_interval + 0.1, 1.0))

    def _poll_github_comment_commands(self, orchestrator: TaskOrchestrator) -> None:
        delivery = orchestrator.github_delivery
        if delivery is None or not delivery.client.enabled:
            return
        for task in orchestrator.store.list_tasks():
            if task.status != TaskStatus.WAITING_MERGE_APPROVAL or task.remote_pull_request is None:
                continue
            try:
                synchronized = orchestrator.sync_pull_request_comments(task.id)
                command_keys = [
                    str(item["key"])
                    for item in synchronized.metadata.get("github_review_comments", [])
                    if item.get("command_requested") and item.get("status") == "pending"
                ]
                if command_keys and self._active_feedback_job(orchestrator, task.id) is None:
                    self.enqueue_github_comments(task.id, "github-/agent-fix", command_keys)
            except (KeyError, ValueError, RuntimeError, OSError):
                continue

    def _execute(self, orchestrator: TaskOrchestrator, job: ExecutionJob) -> None:
        if getattr(orchestrator, "tracer", None):
            with orchestrator.tracer.trace(
                f"job.{job.action}", task_id=job.task_id, kind="execution",
                metadata={"job_id": job.id, "attempt": job.attempts},
            ) as trace:
                with orchestrator.tracer.span(f"agent.{job.action}", kind="agent"):
                    self._execute_traced(orchestrator, job)
                completed = orchestrator.store.get_job(job.id)
                if completed and completed.status == "failed":
                    trace.status = "failed"
            return
        self._execute_traced(orchestrator, job)

    def _execute_traced(self, orchestrator: TaskOrchestrator, job: ExecutionJob) -> None:
        orchestrator.store.add_event(
            TaskEvent(
                task_id=job.task_id,
                event_type="execution_started",
                message=f"后台 Worker 开始执行（第 {job.attempts} 次）",
                payload={"job_id": job.id, "attempt": job.attempts},
            )
        )
        try:
            if job.action == "approve":
                task = orchestrator.approve(
                    job.task_id,
                    str(job.payload.get("actor", "system")),
                    str(job.payload.get("comment", "")),
                    checkpoint_job_id=job.id,
                    should_pause=lambda: self._pause_requested(orchestrator, job.id),
                )
            elif job.action == "resume":
                task = self._resume_task(orchestrator, job)
            elif job.action == "retry_failed":
                task = orchestrator.retry_failed_task(
                    job.task_id,
                    job.id,
                    should_pause=lambda: self._pause_requested(orchestrator, job.id),
                )
            elif job.action == "user_feedback":
                task = orchestrator.apply_user_feedback(
                    job.task_id,
                    str(job.payload.get("actor", "demo-user")),
                    str(job.payload.get("feedback", "")),
                    job_id=job.id,
                    should_pause=lambda: self._pause_requested(orchestrator, job.id),
                )
            elif job.action == "github_comment_feedback":
                task = orchestrator.apply_user_feedback(
                    job.task_id,
                    str(job.payload.get("actor", "github-reviewer")),
                    str(job.payload.get("feedback", "")),
                    job_id=job.id,
                    should_pause=lambda: self._pause_requested(orchestrator, job.id),
                )
                comment_keys = [str(key) for key in job.payload.get("comment_keys", [])]
                if task.status == TaskStatus.WAITING_RELEASE_APPROVAL:
                    task = orchestrator.publish_pull_request(
                        job.task_id,
                        str(job.payload.get("actor", "github-reviewer")),
                    )
                    changed = ", ".join(task.merge_request.changed_files) if task.merge_request else ""
                    detail = (
                        f"修改文件：{changed or '见 PR Diff'}\n"
                        f"测试：{task.merge_request.test_summary if task.merge_request else '已通过'}\n"
                        f"Commit：`{task.remote_pull_request.head_sha[:12]}`"
                    )
                    task = orchestrator.complete_github_comment_feedback(
                        job.task_id, comment_keys, True, detail
                    )
                elif task.status == TaskStatus.WAITING_RISK_APPROVAL:
                    task.metadata["pending_github_comment_keys"] = comment_keys
                    orchestrator.store.save_task(task)
                    task = orchestrator.update_github_comment_status(
                        job.task_id, comment_keys, "waiting_risk_approval"
                    )
                elif task.status != TaskStatus.WAITING_RISK_APPROVAL:
                    task = orchestrator.complete_github_comment_feedback(
                        job.task_id,
                        comment_keys,
                        False,
                        task.error or "代码修改或 Code Review 未通过，请在 Agent 页面查看详情。",
                    )
            else:
                raise ValueError(f"Unsupported job action: {job.action}")
            latest = orchestrator.store.get_job(job.id) or job
            if latest.status == "abandoned" or latest.worker_id != self._worker_id:
                return
            now = datetime.now(UTC)
            latest.result_status = task.status.value
            latest.finished_at = now
            latest.updated_at = now
            paused_checkpoint = task.metadata.get("paused_checkpoint", {})
            pause_honored = (
                latest.status == "pause_requested"
                and paused_checkpoint.get("job_id") == job.id
            )
            latest.status = "paused" if pause_honored else (
                "failed" if task.status == TaskStatus.FAILED else "succeeded"
            )
            latest.error = task.error if latest.status == "failed" else None
            latest.lease_expires_at = None
            latest.current_stage = task.status.value
            orchestrator.store.save_job(latest)
            orchestrator.store.add_event(
                TaskEvent(
                    task_id=job.task_id,
                    event_type="execution_finished",
                    message=f"后台执行结束：{latest.status}",
                    payload={
                        "job_id": latest.id,
                        "job_status": latest.status,
                        "task_status": latest.result_status,
                    },
                )
            )
        except Exception as error:
            latest = orchestrator.store.get_job(job.id) or job
            if latest.status == "abandoned" or latest.worker_id != self._worker_id:
                return
            now = datetime.now(UTC)
            latest.error = str(error)
            latest.updated_at = now
            if latest.status == "pause_requested":
                task = orchestrator.store.get_task(job.task_id)
                paused_checkpoint = task.metadata.get("paused_checkpoint", {}) if task else {}
                latest.status = (
                    "paused" if paused_checkpoint.get("job_id") == job.id else "failed"
                )
                latest.finished_at = now
            elif latest.attempts < latest.max_attempts:
                latest.status = "queued"
                latest.worker_id = None
                latest.lease_expires_at = None
                orchestrator.store.add_event(
                    TaskEvent(
                        task_id=job.task_id,
                        event_type="execution_retrying",
                        message="后台执行异常，将自动重试",
                        payload={"job_id": job.id, "error": str(error)},
                    )
                )
                self._wake.set()
            else:
                latest.status = "failed"
                latest.finished_at = now
                latest.lease_expires_at = None
            orchestrator.store.save_job(latest)
            if latest.status == "failed" and job.action == "github_comment_feedback":
                try:
                    orchestrator.complete_github_comment_feedback(
                        job.task_id,
                        [str(key) for key in job.payload.get("comment_keys", [])],
                        False,
                        f"后台执行失败：{str(error)[:1000]}",
                    )
                except Exception:
                    pass

    @staticmethod
    def _resume_task(orchestrator: TaskOrchestrator, job: ExecutionJob):
        task = orchestrator.store.get_task(job.task_id)
        if task is None:
            raise KeyError(job.task_id)
        checkpoint = orchestrator.latest_workflow_checkpoint(job.task_id)
        if checkpoint is not None:
            return orchestrator.resume_from_checkpoint(
                job.task_id,
                job.id,
                should_pause=lambda: TaskExecutionWorker._pause_requested(orchestrator, job.id),
            )
        if task.status == TaskStatus.WAITING_REQUIREMENT_APPROVAL:
            return orchestrator.approve(
                job.task_id,
                str(job.payload.get("original_actor", job.payload.get("actor", "system"))),
                str(job.payload.get("original_comment", job.payload.get("comment", ""))),
                checkpoint_job_id=job.id,
                should_pause=lambda: TaskExecutionWorker._pause_requested(orchestrator, job.id),
            )
        if task.status in {TaskStatus.CHANGE_READY, TaskStatus.CHANGES_REQUESTED}:
            return orchestrator.run_review(job.task_id)
        raise ValueError(f"Task cannot resume from status: {task.status.value}")

    @staticmethod
    def _pause_requested(orchestrator: TaskOrchestrator, job_id: str) -> bool:
        current = orchestrator.store.get_job(job_id)
        return bool(current and current.status in {"pause_requested", "abandoned"})
