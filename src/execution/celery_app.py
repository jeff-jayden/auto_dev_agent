"""Celery 应用、任务路由和定时维护任务。

Redis 只承担消息代理职责；ExecutionJob、LangGraph State、检查点和页面状态
仍由项目数据库持久化。Celery Worker 通过 Job ID 调用原有执行器。
"""

from __future__ import annotations

import os
from typing import Any

from celery import Celery

from domain.models import ExecutionJob


BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/1")

celery_app = Celery(
    "ai_dev_agent",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
)
celery_app.conf.update(
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    beat_schedule={
        "recover-stale-execution-jobs": {
            "task": "execution.recover_stale_jobs",
            "schedule": 30.0,
            "options": {"queue": "maintenance"},
        },
        "poll-github-agent-comments": {
            "task": "execution.poll_github_comments",
            "schedule": 30.0,
            "options": {"queue": "github"},
        },
    },
)


def queue_for_action(action: str) -> str:
    """按照 Job 职责选择队列，同一需求的先后顺序仍由 LangGraph 控制。"""
    if action == "run_review":
        return "review"
    if action == "github_comment_feedback":
        return "github"
    return "development"


def dispatch_execution_job(job: ExecutionJob) -> str:
    """向 Redis Broker 投递轻量消息，仅传递 Job ID。"""
    broker_task_id = f"execution-{job.id}"
    celery_app.send_task(
        "execution.execute_job",
        args=[job.id],
        task_id=broker_task_id,
        queue=queue_for_action(job.action),
    )
    return broker_task_id


def _runtime() -> tuple[Any, Any]:
    """延迟加载 API 装配结果，避免 Celery 模块导入时产生循环依赖。"""
    from apps.api.main import execution_worker, orchestrator

    return orchestrator, execution_worker


@celery_app.task(bind=True, name="execution.execute_job", max_retries=2)
def execute_job(self, job_id: str) -> dict[str, Any]:
    """领取并执行指定 Job；业务失败状态仍以 ExecutionJob 为准。"""
    from execution.worker import TaskExecutionWorker

    orchestrator, _ = _runtime()
    worker_id = f"celery-{self.request.hostname}-{self.request.id}"
    executor = TaskExecutionWorker(
        lambda: orchestrator,
        local_execution=False,
        worker_id=worker_id,
    )
    job = executor.execute_job(job_id)
    if job is None:
        return {"job_id": job_id, "status": "missing"}
    if job.status == "queued" and job.attempts < job.max_attempts:
        raise self.retry(countdown=10)
    return {
        "job_id": job.id,
        "task_id": job.task_id,
        "status": job.status,
        "result_status": job.result_status,
    }


@celery_app.task(name="execution.recover_stale_jobs")
def recover_stale_jobs() -> dict[str, Any]:
    """定时回收租约过期的 Job，并重新投递尚未进入 Broker 的任务。"""
    orchestrator, _ = _runtime()
    recovered = orchestrator.store.recover_incomplete_jobs(
        lambda task_id: orchestrator.latest_workflow_checkpoint(task_id) is not None
    )
    dispatched: list[str] = []
    for job in reversed(orchestrator.store.list_jobs()):
        if job.status != "queued" or job.dispatched_at is not None:
            continue
        broker_task_id = dispatch_execution_job(job)
        orchestrator.store.mark_job_dispatched(job.id, broker_task_id)
        dispatched.append(job.id)
    return {"recovered": recovered, "dispatched_job_ids": dispatched}


@celery_app.task(name="execution.poll_github_comments")
def poll_github_comments() -> dict[str, int]:
    """定时同步 GitHub Agent 指令；新修复任务仍通过 Redis 分发。"""
    orchestrator, execution_worker = _runtime()
    execution_worker.poll_github_comment_commands(orchestrator)
    return {"checked_tasks": len(orchestrator.store.list_tasks())}
