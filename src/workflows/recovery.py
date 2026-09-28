from __future__ import annotations

from typing import TYPE_CHECKING

from domain.models import RecoveryCheck, RecoveryReport, TaskStatus

if TYPE_CHECKING:
    from workflows.orchestrator import TaskOrchestrator


ACTIVE_JOB_STATUSES = {"queued", "running", "pause_requested"}
CHECKPOINT_RECOVERABLE_STATUSES = {
    TaskStatus.PLAN_APPROVED,
    TaskStatus.DEVELOPING,
    TaskStatus.TESTING,
    TaskStatus.REPAIRING,
    TaskStatus.CHANGE_READY,
    TaskStatus.GENERATING_MR,
    TaskStatus.REVIEWING,
    TaskStatus.REVIEW_REPAIRING,
}
UNSAFE_INTERRUPTED_STATUSES = {
    TaskStatus.PUBLISHING_PULL_REQUEST,
    TaskStatus.MERGING,
}


class RecoveryCoordinator:
    """Diagnose interrupted workflows without changing task state."""

    def __init__(self, orchestrator: TaskOrchestrator):
        self.orchestrator = orchestrator

    def diagnose(self, task_id: str) -> RecoveryReport:
        task = self.orchestrator.store.get_task(task_id)
        if task is None:
            raise KeyError(task_id)

        jobs = self.orchestrator.store.list_jobs(task_id)
        active = next((job for job in jobs if job.status in ACTIVE_JOB_STATUSES), None)
        active_stale = bool(
            active
            and active.status != "queued"
            and self.orchestrator.store.job_lease_expired(active)
        )
        checks = [RecoveryCheck(
            name="后台执行",
            status="blocked" if active_stale else ("running" if active else "idle"),
            detail=(
                (
                    f"Job {active.id} 的 Worker 心跳已经过期，可以终止旧执行后恢复"
                    if active_stale else
                    f"Job {active.id} 正在{self._job_status_label(active.status)}"
                )
                if active else "当前没有活动 Job"
            ),
        )]
        if active and not active_stale:
            return RecoveryReport(
                task_id=task.id,
                status="running",
                reason="任务已有后台执行，暂时不需要恢复",
                active_job_id=active.id,
                worker_id=active.worker_id,
                current_stage=active.current_stage,
                heartbeat_at=active.heartbeat_at,
                lease_expires_at=active.lease_expires_at,
                checks=checks,
            )
        if active_stale:
            checks.append(RecoveryCheck(
                name="Worker 租约",
                status="blocked",
                detail=(
                    f"最后心跳 {active.heartbeat_at.isoformat() if active.heartbeat_at else '未知'}；"
                    f"租约已于 {active.lease_expires_at.isoformat() if active.lease_expires_at else '未知时间'} 失效"
                ),
            ))

        if task.status in UNSAFE_INTERRUPTED_STATUSES:
            checks.append(RecoveryCheck(
                name="外部操作",
                status="blocked",
                detail="发布或合入可能已经在 GitHub 生效，需要先同步远端状态",
            ))
            return RecoveryReport(
                task_id=task.id,
                status="blocked",
                reason="任务在外部 GitHub 操作期间中断，不能直接重放本地检查点",
                requires_confirmation=True,
                checks=checks,
            )

        if task.status not in CHECKPOINT_RECOVERABLE_STATUSES:
            return RecoveryReport(
                task_id=task.id,
                status="healthy",
                reason="任务当前处于正常等待或终态，无需恢复",
                checks=checks,
            )

        checkpoint = self.orchestrator.latest_workflow_checkpoint(task_id)
        if checkpoint is None:
            checks.append(RecoveryCheck(
                name="持久化检查点",
                status="blocked",
                detail="没有找到可用于安全续跑的检查点",
            ))
            return RecoveryReport(
                task_id=task.id,
                status="blocked",
                reason="任务执行已停止，但没有可恢复检查点",
                requires_confirmation=True,
                checks=checks,
            )

        checks.append(RecoveryCheck(
            name="持久化检查点",
            status="passed",
            detail=f"最近完成 {checkpoint.stage}，下一步为 {checkpoint.next_action}",
        ))
        try:
            self.orchestrator._validate_checkpoint(task, checkpoint)
        except ValueError as error:
            checks.append(RecoveryCheck(
                name="恢复一致性",
                status="blocked",
                detail=str(error),
            ))
            return RecoveryReport(
                task_id=task.id,
                status="blocked",
                reason="检查点与当前方案、基线或工作区不一致",
                checkpoint_id=checkpoint.id,
                checkpoint_stage=checkpoint.stage,
                next_action=checkpoint.next_action,
                requires_confirmation=True,
                checks=checks,
            )

        checks.append(RecoveryCheck(
            name="恢复一致性",
            status="passed",
            detail="技术方案、仓库基线、工作区与 Diff 均未漂移",
        ))
        return RecoveryReport(
            task_id=task.id,
            status="recoverable",
            reason=(
                f"Worker 心跳已过期，可以终止旧执行并从 {checkpoint.stage} 检查点继续"
                if active_stale else self._recovery_reason(task.status, checkpoint.stage)
            ),
            checkpoint_id=checkpoint.id,
            checkpoint_stage=checkpoint.stage,
            next_action=checkpoint.next_action,
            requires_confirmation=active_stale,
            active_job_id=active.id if active else None,
            active_job_stale=active_stale,
            worker_id=active.worker_id if active else None,
            current_stage=active.current_stage if active else None,
            heartbeat_at=active.heartbeat_at if active else None,
            lease_expires_at=active.lease_expires_at if active else None,
            checks=checks,
        )

    @staticmethod
    def _job_status_label(status: str) -> str:
        return {
            "queued": "排队",
            "running": "执行",
            "pause_requested": "等待安全暂停点",
        }.get(status, status)

    @staticmethod
    def _recovery_reason(status: TaskStatus, checkpoint_stage: str) -> str:
        labels = {
            TaskStatus.REVIEW_REPAIRING: "审查修复被中断",
            TaskStatus.REVIEWING: "Code Review 被中断",
            TaskStatus.GENERATING_MR: "MR 生成被中断",
            TaskStatus.TESTING: "测试执行被中断",
            TaskStatus.REPAIRING: "自动修复被中断",
            TaskStatus.DEVELOPING: "代码开发被中断",
        }
        prefix = labels.get(status, "任务执行被中断")
        return f"{prefix}，可以从 {checkpoint_stage} 检查点继续"
