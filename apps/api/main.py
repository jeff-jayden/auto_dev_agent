from __future__ import annotations

import asyncio
import sys
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from dev_agent.agents import (
    CodeReviewerAgent,
    DemoDeveloperAgent,
    GenericDeveloperAgent,
    LocalPlanningAgent,
    MergeRequestWriter,
)
from dev_agent.infrastructure.store import SQLiteTaskStore
from dev_agent.execution import TaskExecutionWorker
from dev_agent.llm import ModelGateway, build_model_gateway
from dev_agent.observability import TraceRecorder, TracingModelGateway
from dev_agent.evaluation import GoldenCaseEvaluator
from dev_agent.repository import RepositoryAnalyzer, RepositoryCatalog
from dev_agent.sandbox import WorkspaceManager
from dev_agent.scm import GitHubClient, GitHubDeliveryService
from dev_agent.workflows import RecoveryCoordinator, TaskOrchestrator
from dev_agent.ui_validation import FigmaMCPClient, UIAcceptanceService


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    requirement: str = Field(min_length=10, max_length=5000)
    repository_id: str = "demo"
    figma_url: str = Field(default="", max_length=2000)
    preview_url: str = Field(default="", max_length=2000)
    viewport_width: int = Field(default=1440, ge=320, le=3840)
    viewport_height: int = Field(default=900, ge=320, le=2160)


class DecisionRequest(BaseModel):
    actor: str = Field(default="demo-user", min_length=2, max_length=80)
    comment: str = Field(default="", max_length=1000)


class RepositoryRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    remote_url: str | None = Field(default=None, max_length=1000)
    local_path: str | None = Field(default=None, max_length=1000)


class RevisionRequest(BaseModel):
    actor: str = Field(default="demo-user", min_length=2, max_length=80)
    feedback: str = Field(min_length=3, max_length=2000)


class GitHubCommentProcessRequest(BaseModel):
    actor: str = Field(default="demo-user", min_length=2, max_length=80)
    comment_keys: list[str] = Field(min_length=1, max_length=20)


def build_orchestrator(
    runtime_root: Path | None = None,
    model_gateway: ModelGateway | None = None,
) -> TaskOrchestrator:
    runtime = runtime_root or PROJECT_ROOT / "runtime"
    store = SQLiteTaskStore(runtime / "agent.db")
    allowed_roots_env = os.getenv("AGENT_ALLOWED_REPOSITORY_ROOTS", "")
    allowed_roots = [PROJECT_ROOT.parent, *[Path(item) for item in allowed_roots_env.split(os.pathsep) if item]]
    catalog = RepositoryCatalog(
        store,
        allowed_roots,
        clone_root=runtime / "repositories",
        github_token=os.getenv("GITHUB_TOKEN", ""),
    )
    catalog.refresh_remotes()
    catalog.register(
        "内置任务服务 Demo",
        PROJECT_ROOT / "examples" / "demo_repository",
        execution_mode="demo",
        require_git=False,
        repository_id="demo",
    )
    workspace_manager = WorkspaceManager(
        PROJECT_ROOT / "examples" / "demo_repository",
        runtime / "tasks",
    )
    tracer = TraceRecorder(store)
    gateway = TracingModelGateway(model_gateway or build_model_gateway(), tracer)
    result = TaskOrchestrator(
        store,
        LocalPlanningAgent(gateway, tracer=tracer),
        DemoDeveloperAgent(),
        workspace_manager,
        RepositoryAnalyzer(runtime / "indexes"),
        GenericDeveloperAgent(gateway, tracer=tracer),
        MergeRequestWriter(),
        CodeReviewerAgent(gateway),
        GitHubDeliveryService(
            GitHubClient(
                os.getenv("GITHUB_TOKEN", ""),
                os.getenv("GITHUB_API_URL", "https://api.github.com"),
            )
        ),
        tracer,
        figma_client=FigmaMCPClient(
            os.getenv("FIGMA_MCP_URL", ""), runtime / "designs"
        ),
        ui_acceptance_service=UIAcceptanceService(runtime / "ui-acceptance"),
    )
    result.repository_catalog = catalog
    return result


orchestrator = build_orchestrator()
execution_worker = TaskExecutionWorker(lambda: orchestrator)
app = FastAPI(title="AI Dev Agent", version="0.1.0")
web_root = PROJECT_ROOT / "apps" / "web"
app.mount("/assets", StaticFiles(directory=web_root / "assets"), name="assets")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(web_root / "index.html")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "phase": 7,
        "model_enabled": orchestrator.planner.model_gateway.enabled,
        "github_enabled": bool(orchestrator.github_delivery and orchestrator.github_delivery.client.enabled),
        "figma_mcp_enabled": bool(orchestrator.figma_client and orchestrator.figma_client.enabled),
    }


@app.on_event("startup")
def start_execution_worker():
    execution_worker.start()


@app.on_event("shutdown")
def stop_execution_worker():
    execution_worker.stop()


@app.post("/api/tasks", status_code=201)
def create_task(request: CreateTaskRequest):
    try:
        return orchestrator.create_task(
            request.title,
            request.requirement,
            request.repository_id,
            figma_url=request.figma_url,
            preview_url=request.preview_url,
            viewport_width=request.viewport_width,
            viewport_height=request.viewport_height,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/rerun", status_code=201)
def rerun_task(task_id: str):
    try:
        return orchestrator.rerun_task(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/repositories")
def list_repositories():
    return orchestrator.store.list_repositories()


@app.get("/api/repository-folders")
def browse_repository_folders(path: str | None = None):
    try:
        return orchestrator.repository_catalog.browse_directories(path)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/api/repositories", status_code=201)
def register_repository(request: RepositoryRequest):
    try:
        if request.remote_url:
            return orchestrator.repository_catalog.register_remote(request.remote_url, request.name)
        if not request.local_path or len(request.name.strip()) < 2:
            raise ValueError("Local repository registration requires a name and folder")
        return orchestrator.repository_catalog.register(request.name, request.local_path)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/repositories/{repository_id}/analysis")
def analyze_repository(repository_id: str, requirement: str = ""):
    repository = orchestrator.store.get_repository(repository_id)
    if repository is None:
        raise HTTPException(status_code=404, detail="Repository not found")
    return orchestrator.repository_analyzer.analyze(Path(repository.local_path), requirement)


@app.get("/api/tasks")
def list_tasks():
    coordinator = RecoveryCoordinator(orchestrator)
    result = []
    for task in orchestrator.store.list_tasks():
        item = task.model_dump(mode="json")
        item["recovery"] = coordinator.diagnose(task.id).model_dump(mode="json")
        result.append(item)
    return result


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    task = orchestrator.store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    # Keep the reviewed result immutable while exposing edits left in the
    # worktree by later (including failed) feedback rounds.
    task.metadata["workspace_diff"] = orchestrator.current_workspace_diff(task)
    return task


@app.get("/api/tasks/{task_id}/events")
def get_events(task_id: str):
    if orchestrator.store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return orchestrator.store.list_events(task_id)


@app.get("/api/tasks/{task_id}/recovery")
def diagnose_task_recovery(task_id: str):
    try:
        return RecoveryCoordinator(orchestrator).diagnose(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None


@app.post("/api/tasks/{task_id}/recovery/resume", status_code=202)
def resume_interrupted_task(task_id: str, request: DecisionRequest):
    try:
        return execution_worker.enqueue_recovery(task_id, request.actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/approve", status_code=202)
def approve_task(task_id: str, request: DecisionRequest):
    try:
        return execution_worker.enqueue_approval(task_id, request.actor, request.comment)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/retry", status_code=202)
def retry_failed_task(task_id: str, request: DecisionRequest):
    try:
        return execution_worker.retry_failed(task_id, request.actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/feedback", status_code=202)
def continue_with_user_feedback(task_id: str, request: RevisionRequest):
    try:
        return execution_worker.enqueue_user_feedback(
            task_id, request.actor, request.feedback
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = orchestrator.store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Execution job not found")
    return job


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    try:
        return execution_worker.cancel(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Execution job not found") from None


@app.post("/api/jobs/{job_id}/resume", status_code=202)
def resume_job(job_id: str, request: DecisionRequest):
    try:
        return execution_worker.resume(job_id, request.actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="Execution job or task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/tasks/{task_id}/jobs")
def list_task_jobs(task_id: str):
    if orchestrator.store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return orchestrator.store.list_jobs(task_id)


@app.get("/api/tasks/{task_id}/checkpoints")
def list_task_checkpoints(task_id: str):
    if orchestrator.store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return orchestrator.store.list_checkpoints(task_id)


@app.post("/api/tasks/{task_id}/checkpoints/{checkpoint_id}/replay")
def replay_checkpoint(task_id: str, checkpoint_id: str):
    try:
        return orchestrator.dry_run_checkpoint(task_id, checkpoint_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task or checkpoint not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/tasks/{task_id}/traces")
def list_task_traces(task_id: str):
    if orchestrator.store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return orchestrator.store.list_traces(task_id)


@app.get("/api/traces/{trace_id}")
def get_trace(trace_id: str):
    trace = orchestrator.store.get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"trace": trace, "spans": orchestrator.store.list_spans(trace_id)}


@app.get("/api/metrics")
def get_metrics():
    return orchestrator.store.trace_metrics()


@app.post("/api/evaluations/run")
def run_evaluation():
    with orchestrator.tracer.trace("golden.evaluate", kind="evaluation"):
        return GoldenCaseEvaluator(orchestrator.store).run()


@app.get("/api/evaluations/latest")
def get_latest_evaluation():
    result = orchestrator.store.latest_evaluation()
    if result is None:
        raise HTTPException(status_code=404, detail="No evaluation has been run")
    return result


@app.get("/api/tasks/{task_id}/stream")
async def stream_task_events(task_id: str, after: int = 0):
    if orchestrator.store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Task not found")

    async def event_generator():
        cursor = max(after, 0)
        idle_ticks = 0
        while True:
            events = [event for event in orchestrator.store.list_events(task_id) if (event.id or 0) > cursor]
            if events:
                idle_ticks = 0
                for event in events:
                    cursor = event.id or cursor
                    yield (
                        f"id: {cursor}\n"
                        "event: task_event\n"
                        f"data: {event.model_dump_json()}\n\n"
                    )
            else:
                idle_ticks += 1
                if idle_ticks >= 10:
                    yield ": heartbeat\n\n"
                    idle_ticks = 0
            await asyncio.sleep(0.25)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/tasks/{task_id}/reject")
def reject_task(task_id: str, request: DecisionRequest):
    try:
        return orchestrator.reject(task_id, request.actor, request.comment)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/revise")
def revise_task(task_id: str, request: RevisionRequest):
    try:
        return orchestrator.revise(task_id, request.actor, request.feedback)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/risk/approve")
def approve_risk(task_id: str, request: DecisionRequest):
    try:
        return orchestrator.approve_risk(task_id, request.actor, request.comment)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/risk/reject")
def reject_risk(task_id: str, request: DecisionRequest):
    try:
        return orchestrator.reject_risk(task_id, request.actor, request.comment)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/tasks/{task_id}/merge-request")
def get_merge_request(task_id: str):
    task = orchestrator.store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.merge_request is None:
        raise HTTPException(status_code=409, detail="MR draft is not ready")
    return task.merge_request


@app.post("/api/tasks/{task_id}/ui-acceptance")
def run_ui_acceptance(task_id: str):
    try:
        return orchestrator.run_ui_acceptance(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/tasks/{task_id}/ui-acceptance/image/{kind}")
def get_ui_acceptance_image(task_id: str, kind: str):
    task = orchestrator.store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    paths = {
        "figma": task.design_reference.screenshot_path if task.design_reference else None,
        "implementation": (
            task.ui_acceptance.implementation_screenshot_path if task.ui_acceptance else None
        ),
        "diff": task.ui_acceptance.diff_screenshot_path if task.ui_acceptance else None,
    }
    path = paths.get(kind)
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=404, detail="UI acceptance image not found")
    return FileResponse(path, media_type="image/png")


@app.post("/api/tasks/{task_id}/pull-request/publish")
def publish_pull_request(task_id: str, request: DecisionRequest):
    try:
        return orchestrator.publish_pull_request(task_id, request.actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/tasks/{task_id}/pull-request")
def get_pull_request(task_id: str):
    task = orchestrator.store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.remote_pull_request is None:
        raise HTTPException(status_code=409, detail="GitHub pull request is not ready")
    return task.remote_pull_request


@app.post("/api/tasks/{task_id}/pull-request/refresh")
def refresh_pull_request(task_id: str):
    try:
        return orchestrator.refresh_pull_request(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/pull-request/comments/sync")
def sync_pull_request_comments(task_id: str):
    try:
        task = orchestrator.sync_pull_request_comments(task_id)
        automatic_keys = [
            str(item["key"])
            for item in task.metadata.get("github_review_comments", [])
            if item.get("command_requested") and item.get("status") == "pending"
        ]
        job = None
        if automatic_keys:
            try:
                job = execution_worker.enqueue_github_comments(
                    task_id, "github-/agent-fix", automatic_keys
                )
            except ValueError as error:
                if "already" not in str(error).lower():
                    raise
        return {
            "task": orchestrator.store.get_task(task_id),
            "job": job,
            "automatic_comment_keys": automatic_keys,
        }
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/pull-request/comments/process", status_code=202)
def process_pull_request_comments(task_id: str, request: GitHubCommentProcessRequest):
    try:
        return execution_worker.enqueue_github_comments(
            task_id, request.actor, request.comment_keys
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/pull-request/merge")
def merge_pull_request(task_id: str, request: DecisionRequest):
    try:
        return orchestrator.merge_pull_request(task_id, request.actor, request.comment)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/tasks/{task_id}/reviews")
def get_reviews(task_id: str):
    task = orchestrator.store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.reviews


@app.post("/api/tasks/{task_id}/review/run")
def run_review(task_id: str):
    try:
        active_job = next(
            (
                job for job in orchestrator.store.list_jobs(task_id)
                if job.status in {"queued", "running", "pause_requested"}
            ),
            None,
        )
        if active_job:
            raise ValueError(
                f"Code Review 正在后台执行（Job {active_job.id}），请等待当前执行结束"
            )
        return orchestrator.run_review(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/review/approve")
def approve_review(task_id: str, request: DecisionRequest):
    try:
        active_job = next(
            (
                job for job in orchestrator.store.list_jobs(task_id)
                if job.status in {"queued", "running", "pause_requested"}
            ),
            None,
        )
        if active_job:
            raise ValueError(
                f"Code Review 正在后台执行（Job {active_job.id}），不能同时人工批准"
            )
        return orchestrator.approve_review(task_id, request.actor, request.comment)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/tasks/{task_id}/release/reject")
def reject_release(task_id: str, request: DecisionRequest):
    try:
        return orchestrator.reject_release(task_id, request.actor, request.comment)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
