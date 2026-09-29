"""SQLite 持久化仓储。

保存任务、事件、执行 Job、检查点、Trace 和评测结果，并提供队列领取、心跳及恢复所需的原子操作。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from collections.abc import Callable, Iterator

from domain.models import (
    AgentTrace,
    EvaluationRun,
    ExecutionJob,
    Repository,
    Task,
    TaskCheckpoint,
    TaskEvent,
    TraceSpan,
)


class SQLiteTaskStore:
    def __init__(self, database_path: Path):
        """初始化任务持久化存储。

        Args:
            database_path: SQLite 数据库文件路径，用于保存任务、Job、事件、检查点和追踪数据。
        """
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """创建一次数据库连接，并在上下文成功结束时提交事务。

        Yields:
            已启用 ``sqlite3.Row`` 行工厂的 SQLite 连接。
        """
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        """创建任务运行所需的数据表和查询索引；已存在的结构保持不变。"""
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repositories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    local_path TEXT NOT NULL UNIQUE,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_jobs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_execution_jobs_status
                    ON execution_jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_execution_jobs_task
                    ON execution_jobs(task_id, action, status);
                CREATE TABLE IF NOT EXISTS task_checkpoints (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_checkpoints_task
                    ON task_checkpoints(task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_task_checkpoints_job
                    ON task_checkpoints(job_id, created_at);
                CREATE TABLE IF NOT EXISTS agent_traces (
                    id TEXT PRIMARY KEY,
                    task_id TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    started_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_traces_task
                    ON agent_traces(task_id, started_at);
                CREATE TABLE IF NOT EXISTS trace_spans (
                    id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    task_id TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    started_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trace_spans_trace
                    ON trace_spans(trace_id, started_at);
                CREATE TABLE IF NOT EXISTS evaluation_runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    score REAL NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def save_repository(self, repository: Repository) -> Repository:
        """新增或更新一个已注册仓库。

        Args:
            repository: 待持久化的仓库领域对象。

        Returns:
            原仓库对象，便于调用方继续使用。
        """
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO repositories (id, name, local_path, data, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    local_path = excluded.local_path,
                    data = excluded.data
                """,
                (
                    repository.id,
                    repository.name,
                    repository.local_path,
                    repository.model_dump_json(),
                    repository.created_at.isoformat(),
                ),
            )
        return repository

    def get_repository(self, repository_id: str) -> Repository | None:
        """按仓库 ID 查询仓库。

        Args:
            repository_id: 仓库唯一标识。

        Returns:
            找到的仓库；不存在时返回 ``None``。
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM repositories WHERE id = ?", (repository_id,)
            ).fetchone()
        return Repository.model_validate_json(row["data"]) if row else None

    def list_repositories(self) -> list[Repository]:
        """按创建时间升序返回所有已注册仓库。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM repositories ORDER BY created_at"
            ).fetchall()
        return [Repository.model_validate_json(row["data"]) for row in rows]

    def save_task(self, task: Task) -> Task:
        """新增或更新任务快照。

        Args:
            task: 包含当前状态及完整业务数据的任务对象。

        Returns:
            原任务对象，便于调用方继续编排。
        """
        serialized = task.model_dump_json()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (id, status, title, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    title = excluded.title,
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (
                    task.id,
                    task.status.value,
                    task.title,
                    serialized,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ),
            )
        return task

    def get_task(self, task_id: str) -> Task | None:
        """按任务 ID 查询最新任务快照。

        Args:
            task_id: 任务唯一标识。

        Returns:
            找到的任务；不存在时返回 ``None``。
        """
        with self._connect() as connection:
            row = connection.execute("SELECT data FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return Task.model_validate_json(row["data"]) if row else None

    def list_tasks(self) -> list[Task]:
        """按创建时间倒序返回所有任务，最新任务排在最前。"""
        with self._connect() as connection:
            rows = connection.execute("SELECT data FROM tasks ORDER BY created_at DESC").fetchall()
        return [Task.model_validate_json(row["data"]) for row in rows]

    def add_event(self, event: TaskEvent) -> TaskEvent:
        """追加一条不可变的任务事件。

        Args:
            event: 待保存的时间线事件。

        Returns:
            已回填数据库自增 ID 的事件对象。
        """
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO task_events (task_id, event_type, message, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.task_id,
                    event.event_type,
                    event.message,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.created_at.isoformat(),
                ),
            )
            event.id = cursor.lastrowid
        return event

    def list_events(self, task_id: str) -> list[TaskEvent]:
        """按产生顺序返回指定任务的全部时间线事件。

        Args:
            task_id: 需要查询事件的任务 ID。

        Returns:
            按事件自增 ID 升序排列的事件列表。
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        return [
            TaskEvent(
                id=row["id"],
                task_id=row["task_id"],
                event_type=row["event_type"],
                message=row["message"],
                payload=json.loads(row["payload"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def enqueue_job(self, job: ExecutionJob) -> ExecutionJob:
        """将执行 Job 原子加入队列，并避免相同任务和动作重复入队。

        Args:
            job: 状态通常为 ``queued`` 的待执行 Job。

        Returns:
            新入队的 Job；若已有活动 Job，则返回已有 Job。
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT data FROM execution_jobs
                WHERE task_id = ? AND action = ? AND status IN ('queued', 'running', 'pause_requested')
                ORDER BY created_at LIMIT 1
                """,
                (job.task_id, job.action),
            ).fetchone()
            if existing:
                return ExecutionJob.model_validate_json(existing["data"])
            connection.execute(
                """
                INSERT INTO execution_jobs (id, task_id, action, status, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id, job.task_id, job.action, job.status, job.model_dump_json(),
                    job.created_at.isoformat(), job.updated_at.isoformat(),
                ),
            )
        return job

    def get_active_job(self, task_id: str, action: str) -> ExecutionJob | None:
        """查询指定任务动作当前尚未结束的 Job。

        Args:
            task_id: 任务唯一标识。
            action: Job 动作类型，例如开发、审查或恢复。

        Returns:
            最早创建的活动 Job；不存在时返回 ``None``。
        """
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT data FROM execution_jobs
                WHERE task_id = ? AND action = ?
                  AND status IN ('queued', 'running', 'pause_requested')
                ORDER BY created_at LIMIT 1
                """,
                (task_id, action),
            ).fetchone()
        return ExecutionJob.model_validate_json(row["data"]) if row else None

    def get_job(self, job_id: str) -> ExecutionJob | None:
        """按 Job ID 查询执行记录。

        Args:
            job_id: 执行 Job 唯一标识。

        Returns:
            找到的 Job；不存在时返回 ``None``。
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM execution_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return ExecutionJob.model_validate_json(row["data"]) if row else None

    def list_jobs(self, task_id: str | None = None) -> list[ExecutionJob]:
        """按创建时间倒序查询执行 Job。

        Args:
            task_id: 可选任务 ID；提供时仅返回该任务的 Job。

        Returns:
            符合条件的 Job 列表，最新记录排在最前。
        """
        with self._connect() as connection:
            if task_id:
                rows = connection.execute(
                    "SELECT data FROM execution_jobs WHERE task_id = ? ORDER BY created_at DESC",
                    (task_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT data FROM execution_jobs ORDER BY created_at DESC"
                ).fetchall()
        return [ExecutionJob.model_validate_json(row["data"]) for row in rows]

    def save_job(self, job: ExecutionJob) -> ExecutionJob:
        """更新一个已有 Job 的状态和完整数据快照。

        Args:
            job: 已包含最新执行状态的 Job。

        Returns:
            原 Job 对象。
        """
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE execution_jobs SET status = ?, data = ?, updated_at = ?
                WHERE id = ?
                """,
                (job.status, job.model_dump_json(), job.updated_at.isoformat(), job.id),
            )
        return job

    def claim_next_job(
        self,
        worker_id: str | None = None,
        lease_seconds: float = 90.0,
    ) -> ExecutionJob | None:
        """原子领取队列中最早的 Job，并为 Worker 建立租约。

        Args:
            worker_id: 可选的 Worker 标识；提供后会记录心跳和租约过期时间。
            lease_seconds: 本次 Worker 租约有效期，单位为秒。

        Returns:
            已切换为 ``running`` 的 Job；队列为空时返回 ``None``。
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM execution_jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            job = ExecutionJob.model_validate_json(row["data"])
            job.status = "running"
            job.attempts += 1
            job.started_at = job.started_at or datetime.now(UTC)
            job.updated_at = datetime.now(UTC)
            if worker_id:
                job.worker_id = worker_id
                job.heartbeat_at = job.updated_at
                job.lease_expires_at = job.updated_at + timedelta(seconds=lease_seconds)
                job.current_stage = job.current_stage or "starting"
            connection.execute(
                "UPDATE execution_jobs SET status = ?, data = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
                (job.status, job.model_dump_json(), job.updated_at.isoformat(), job.id),
            )
        return job

    @staticmethod
    def job_lease_expired(job: ExecutionJob, now: datetime | None = None) -> bool:
        """判断活动 Job 的 Worker 租约是否已经过期。

        Args:
            job: 待判断的执行 Job。
            now: 可选的比较时间，主要用于测试；默认使用当前 UTC 时间。

        Returns:
            活动 Job 无租约或租约到期时返回 ``True``；非活动状态返回 ``False``。
        """
        if job.status not in {"running", "pause_requested", "cancel_requested"}:
            return False
        if job.lease_expires_at is None:
            return True
        return job.lease_expires_at <= (now or datetime.now(UTC))

    def heartbeat_job(
        self,
        job_id: str,
        worker_id: str,
        current_stage: str,
        lease_seconds: float = 90.0,
    ) -> ExecutionJob | None:
        """刷新 Worker 心跳和租约，但不会复活或覆盖已被接管的 Job。

        Args:
            job_id: 正在执行的 Job ID。
            worker_id: 发送心跳的 Worker ID，必须与当前租约持有者一致。
            current_stage: Worker 当前执行阶段，用于恢复诊断和页面展示。
            lease_seconds: 从本次心跳起延长的租约秒数。

        Returns:
            更新后的 Job；Job 不存在时返回 ``None``。Worker 不匹配或状态已结束时返回原快照。
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM execution_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            job = ExecutionJob.model_validate_json(row["data"])
            if job.worker_id != worker_id or job.status not in {
                "running", "pause_requested", "cancel_requested"
            }:
                return job
            now = datetime.now(UTC)
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.current_stage = current_stage
            job.updated_at = now
            connection.execute(
                "UPDATE execution_jobs SET data = ?, updated_at = ? WHERE id = ?",
                (job.model_dump_json(), now.isoformat(), job.id),
            )
        return job

    def replace_stale_job_with_recovery(
        self, stale_job_id: str, recovery_job: ExecutionJob
    ) -> ExecutionJob:
        """原子隔离心跳过期的 Worker，并创建唯一的恢复 Job。

        Args:
            stale_job_id: 已过期、需要标记为 ``abandoned`` 的原 Job ID。
            recovery_job: 接续检查点执行的新恢复 Job。

        Returns:
            新建的恢复 Job；若任务已有活动恢复 Job，则返回已有记录。

        Raises:
            ValueError: 原 Job 不存在，或其 Worker 租约仍然有效。
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT data FROM execution_jobs
                WHERE task_id = ? AND action = 'resume'
                  AND status IN ('queued', 'running', 'pause_requested')
                ORDER BY created_at LIMIT 1
                """,
                (recovery_job.task_id,),
            ).fetchone()
            if existing:
                return ExecutionJob.model_validate_json(existing["data"])
            row = connection.execute(
                "SELECT data FROM execution_jobs WHERE id = ?", (stale_job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Interrupted execution job no longer exists")
            stale = ExecutionJob.model_validate_json(row["data"])
            if not self.job_lease_expired(stale):
                raise ValueError("Execution heartbeat is active; recovery is not allowed")
            now = datetime.now(UTC)
            stale.status = "abandoned"
            stale.failure_kind = "worker_lease_expired"
            stale.error = "Worker heartbeat expired; execution was fenced for checkpoint recovery"
            stale.finished_at = now
            stale.updated_at = now
            stale.recovery_job_id = recovery_job.id
            connection.execute(
                "UPDATE execution_jobs SET status = ?, data = ?, updated_at = ? WHERE id = ?",
                (stale.status, stale.model_dump_json(), now.isoformat(), stale.id),
            )
            connection.execute(
                """
                INSERT INTO execution_jobs (id, task_id, action, status, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recovery_job.id, recovery_job.task_id, recovery_job.action,
                    recovery_job.status, recovery_job.model_dump_json(),
                    recovery_job.created_at.isoformat(), recovery_job.updated_at.isoformat(),
                ),
            )
        return recovery_job

    def recover_incomplete_jobs(
        self,
        has_checkpoint: Callable[[str], bool] | None = None,
    ) -> int:
        """扫描并恢复因进程退出而遗留且租约已过期的 Job。

        根据暂停请求、检查点和重试预算，将 Job 转为 ``paused``、``cancelled``、
        ``failed`` 或重新放回 ``queued``。

        Args:
            has_checkpoint: 可选的检查点判定函数；默认查询任务的最新检查点。

        Returns:
            本次被恢复或终结的 Job 数量。
        """
        checkpoint_exists = has_checkpoint or (
            lambda task_id: self.latest_checkpoint(task_id) is not None
        )
        recovered = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM execution_jobs WHERE status IN ('running', 'pause_requested', 'cancel_requested')"
            ).fetchall()
            for row in rows:
                job = ExecutionJob.model_validate_json(row["data"])
                if not self.job_lease_expired(job):
                    continue
                if job.status in {"pause_requested", "cancel_requested"}:
                    job.status = "paused" if checkpoint_exists(job.task_id) else "cancelled"
                    job.error = "Pause completed during worker restart"
                    job.finished_at = datetime.now(UTC)
                elif job.attempts >= job.max_attempts:
                    job.status = "failed"
                    job.error = "Worker stopped before completion and retry budget was exhausted"
                    job.finished_at = datetime.now(UTC)
                else:
                    job.status = "queued"
                    job.error = "Recovered after worker restart"
                job.worker_id = None
                job.lease_expires_at = None
                job.updated_at = datetime.now(UTC)
                connection.execute(
                    "UPDATE execution_jobs SET status = ?, data = ?, updated_at = ? WHERE id = ?",
                    (job.status, job.model_dump_json(), job.updated_at.isoformat(), job.id),
                )
                recovered += 1
        return recovered

    def request_job_cancel(self, job_id: str) -> ExecutionJob | None:
        """请求取消或暂停一个尚未结束的 Job。

        排队中的 Job 会直接取消；运行中的 Job 会进入 ``pause_requested``，等待
        Worker 在安全检查点停止。

        Args:
            job_id: 目标执行 Job ID。

        Returns:
            更新后的 Job；不存在时返回 ``None``。
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM execution_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            job = ExecutionJob.model_validate_json(row["data"])
            now = datetime.now(UTC)
            if job.status == "queued":
                job.status = "cancelled"
                job.finished_at = now
            elif job.status == "running":
                job.status = "pause_requested"
            job.updated_at = now
            connection.execute(
                "UPDATE execution_jobs SET status = ?, data = ?, updated_at = ? WHERE id = ?",
                (job.status, job.model_dump_json(), job.updated_at.isoformat(), job.id),
            )
        return job

    def save_checkpoint(self, checkpoint: TaskCheckpoint) -> TaskCheckpoint:
        """保存一个可用于暂停恢复的任务检查点。

        Args:
            checkpoint: 包含任务、Job、阶段及恢复数据的检查点。

        Returns:
            原检查点对象。
        """
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO task_checkpoints (id, task_id, job_id, stage, data, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.id,
                    checkpoint.task_id,
                    checkpoint.job_id,
                    checkpoint.stage,
                    checkpoint.model_dump_json(),
                    checkpoint.created_at.isoformat(),
                ),
            )
        return checkpoint

    def list_checkpoints(self, task_id: str) -> list[TaskCheckpoint]:
        """按创建顺序返回指定任务的所有检查点。

        Args:
            task_id: 任务唯一标识。

        Returns:
            从最早到最新排列的检查点列表。
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM task_checkpoints WHERE task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
        return [TaskCheckpoint.model_validate_json(row["data"]) for row in rows]

    def latest_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
        """读取指定任务最近保存的检查点。

        Args:
            task_id: 任务唯一标识。

        Returns:
            最新检查点；任务尚无检查点时返回 ``None``。
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM task_checkpoints WHERE task_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return TaskCheckpoint.model_validate_json(row["data"]) if row else None

    def get_checkpoint(self, checkpoint_id: str) -> TaskCheckpoint | None:
        """按检查点 ID 查询检查点。

        Args:
            checkpoint_id: 检查点唯一标识。

        Returns:
            找到的检查点；不存在时返回 ``None``。
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM task_checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
        return TaskCheckpoint.model_validate_json(row["data"]) if row else None

    def save_trace(self, trace: AgentTrace) -> AgentTrace:
        """新增或更新一次 Agent 执行 Trace。

        Args:
            trace: 聚合一次 Agent 或工作流执行信息的 Trace。

        Returns:
            原 Trace 对象。
        """
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_traces (id, task_id, kind, status, data, started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    task_id = excluded.task_id,
                    kind = excluded.kind,
                    status = excluded.status,
                    data = excluded.data
                """,
                (trace.id, trace.task_id, trace.kind, trace.status,
                 trace.model_dump_json(), trace.started_at.isoformat()),
            )
        return trace

    def get_trace(self, trace_id: str) -> AgentTrace | None:
        """按 Trace ID 查询执行追踪。

        Args:
            trace_id: Trace 唯一标识。

        Returns:
            找到的 Trace；不存在时返回 ``None``。
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM agent_traces WHERE id = ?", (trace_id,)
            ).fetchone()
        return AgentTrace.model_validate_json(row["data"]) if row else None

    def list_traces(self, task_id: str | None = None, limit: int = 50) -> list[AgentTrace]:
        """查询最近的 Agent 执行 Trace。

        Args:
            task_id: 可选任务 ID；提供时仅查询该任务的 Trace。
            limit: 最多返回的 Trace 数量。

        Returns:
            按开始时间倒序排列的 Trace 列表。
        """
        with self._connect() as connection:
            if task_id:
                rows = connection.execute(
                    "SELECT data FROM agent_traces WHERE task_id = ? ORDER BY started_at DESC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT data FROM agent_traces ORDER BY started_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [AgentTrace.model_validate_json(row["data"]) for row in rows]

    def save_span(self, span: TraceSpan) -> TraceSpan:
        """新增或更新 Trace 中的一个调用 Span。

        Args:
            span: Agent、模型或工具调用的可观测性 Span。

        Returns:
            原 Span 对象。
        """
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO trace_spans (id, trace_id, task_id, kind, status, data, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET status = excluded.status, data = excluded.data
                """,
                (span.id, span.trace_id, span.task_id, span.kind, span.status,
                 span.model_dump_json(), span.started_at.isoformat()),
            )
        return span

    def list_spans(self, trace_id: str) -> list[TraceSpan]:
        """按执行顺序返回指定 Trace 的所有 Span。

        Args:
            trace_id: 所属 Trace 的唯一标识。

        Returns:
            从最早到最新排列的 Span 列表。
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM trace_spans WHERE trace_id = ? ORDER BY started_at, rowid",
                (trace_id,),
            ).fetchall()
        return [TraceSpan.model_validate_json(row["data"]) for row in rows]

    def trace_metrics(self) -> dict:
        """聚合可观测性首页使用的 Trace、调用量、Token 和评测指标。

        Returns:
            包含成功率、平均耗时、LLM/工具调用量、Token 用量及最近评测的字典。
        """
        def numeric_attribute(span: TraceSpan, key: str) -> int:
            """将 Span 属性安全转换为整数，非法值按零处理。"""
            value = span.attributes.get(key, 0)
            if isinstance(value, bool):
                return 0
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        traces = self.list_traces(limit=1000)
        with self._connect() as connection:
            rows = connection.execute("SELECT data FROM trace_spans").fetchall()
            evaluation = connection.execute(
                "SELECT data FROM evaluation_runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        spans = [TraceSpan.model_validate_json(row["data"]) for row in rows]
        llm = [item for item in spans if item.kind == "llm"]
        tools = [item for item in spans if item.kind == "tool"]
        completed = [item for item in traces if item.status in {"succeeded", "failed"}]
        durations = [item.duration_ms for item in completed if item.duration_ms is not None]
        return {
            "trace_count": len(traces),
            "success_rate": round(
                100 * sum(item.status == "succeeded" for item in completed) / len(completed), 1
            ) if completed else 0,
            "average_duration_ms": round(sum(durations) / len(durations), 1) if durations else 0,
            "llm_calls": len(llm),
            "tool_calls": len(tools),
            "failed_spans": sum(item.status == "failed" for item in spans),
            "prompt_tokens": sum(numeric_attribute(item, "prompt_tokens") for item in llm),
            "completion_tokens": sum(numeric_attribute(item, "completion_tokens") for item in llm),
            "latest_evaluation": (
                EvaluationRun.model_validate_json(evaluation["data"]).model_dump(mode="json")
                if evaluation else None
            ),
        }

    def save_evaluation(self, run: EvaluationRun) -> EvaluationRun:
        """保存一次 Golden Cases 或检索评测结果。

        Args:
            run: 包含总分和各评测用例结果的评测运行对象。

        Returns:
            原评测运行对象。
        """
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO evaluation_runs (id, status, score, data, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run.id, run.status, run.score, run.model_dump_json(), run.created_at.isoformat()),
            )
        return run

    def latest_evaluation(self) -> EvaluationRun | None:
        """读取最近一次评测运行结果。

        Returns:
            最新评测；尚未执行过评测时返回 ``None``。
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data FROM evaluation_runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return EvaluationRun.model_validate_json(row["data"]) if row else None
