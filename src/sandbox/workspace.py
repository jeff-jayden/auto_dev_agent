from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class WorkspaceManager:
    """Creates task-scoped repositories and executes an allow-listed test command."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = runtime_root.resolve()

    def prepare_worktree(self, task_id: str, source_repository: Path, baseline_sha: str) -> Path:
        source_repository = source_repository.resolve()
        if not (source_repository / ".git").exists():
            raise ValueError("Source repository is not a Git repository")
        status = self._git(source_repository, "status", "--porcelain", capture=True).stdout.strip()
        if status:
            raise ValueError("Source repository has uncommitted changes; development was not started")
        verified_sha = self._git(source_repository, "rev-parse", "HEAD", capture=True).stdout.strip()
        if verified_sha != baseline_sha:
            raise ValueError("Repository HEAD changed after planning; regenerate the technical plan")
        task_root = (self.runtime_root / task_id).resolve()
        if self.runtime_root not in task_root.parents:
            raise ValueError("Task workspace escaped runtime root")
        repository = task_root / "repo"
        if repository.exists():
            raise ValueError("Task worktree already exists")
        task_root.mkdir(parents=True, exist_ok=True)
        self._git(source_repository, "worktree", "add", "--detach", str(repository), baseline_sha)
        return repository

    def run_tests(self, repository: Path) -> tuple[list[str], int, str]:
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(repository)
        completed = subprocess.run(
            command,
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        output = (completed.stdout + completed.stderr).strip()
        return command, completed.returncode, output

    def diff(self, repository: Path) -> str:
        completed = self._git(repository, "diff", "--", ".", capture=True)
        return completed.stdout.strip()

    def diff_from_baseline(self, repository: Path, baseline_sha: str | None) -> str:
        if not baseline_sha:
            return self.diff(repository)
        completed = self._git(repository, "diff", baseline_sha, "--", ".", capture=True)
        return completed.stdout.strip()

    def sync_default_branch(self, repository: Path, branch: str) -> None:
        status = self._git(repository, "status", "--porcelain", capture=True).stdout.strip()
        if status:
            raise ValueError("Source repository has uncommitted changes; cannot update its default branch")
        self._git(repository, "fetch", "origin", branch)
        self._git(repository, "checkout", branch)
        self._git(repository, "merge", "--ff-only", f"origin/{branch}")

    @staticmethod
    def _git(repository: Path, *arguments: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=True,
        )
