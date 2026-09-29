"""任务隔离工作区管理器。

基于批准时的 Git 基线创建独立 Worktree，并提供测试执行、Diff 读取和默认分支同步能力。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any


class WorkspaceManager:
    """Creates task-scoped repositories and executes an allow-listed test command."""

    def __init__(self, runtime_root: Path):
        """初始化任务工作区管理器。

        Args:
            runtime_root: 所有任务隔离 Worktree 的父目录。
        """
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
        """Return the patch used by checkpoints, rollback and review internals."""
        completed = self._git(repository, "diff", "--", ".", capture=True)
        return completed.stdout.strip()

    def diff_files_from_baseline(
        self,
        repository: Path,
        baseline_sha: str | None,
        *,
        max_file_bytes: int = 1_000_000,
    ) -> list[dict[str, Any]]:
        """Return before/after file contents for a Monaco diff editor.

        Git is asked for paths only.  The API intentionally returns full file
        contents instead of a unified patch so the browser does not need to
        parse Git's presentation format.  Untracked files are included as
        additions; renames are represented as one deletion and one addition.
        """
        repository = repository.resolve()
        baseline = baseline_sha or self._git(
            repository, "rev-parse", "HEAD", capture=True
        ).stdout.strip()
        changed = self._git(
            repository,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            baseline,
            "--",
            ".",
            capture=True,
        ).stdout
        untracked = self._git(
            repository,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            capture=True,
        ).stdout
        paths = sorted({
            item
            for output in (changed, untracked)
            for item in output.split("\0")
            if item
        })

        files: list[dict[str, Any]] = []
        for relative_path in paths:
            target = (repository / relative_path).resolve()
            if target != repository and repository not in target.parents:
                continue
            original_bytes = self._git_blob(repository, baseline, relative_path)
            modified_bytes = target.read_bytes() if target.is_file() else None
            status = (
                "added" if original_bytes is None else
                "deleted" if modified_bytes is None else
                "modified"
            )
            contents = [item for item in (original_bytes, modified_bytes) if item is not None]
            binary = any(b"\0" in item for item in contents)
            too_large = any(len(item) > max_file_bytes for item in contents)
            available = not binary and not too_large
            reason = ""
            if binary:
                reason = "二进制文件不支持文本对比"
            elif too_large:
                reason = f"文件超过 {max_file_bytes // 1000} KB，未载入编辑器"
            files.append({
                "path": relative_path.replace("\\", "/"),
                "status": status,
                "original": self._decode_text(original_bytes) if available else "",
                "modified": self._decode_text(modified_bytes) if available else "",
                "available": available,
                "reason": reason,
            })
        return files

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

    @staticmethod
    def _git_blob(repository: Path, revision: str, relative_path: str) -> bytes | None:
        completed = subprocess.run(
            ["git", "show", f"{revision}:{relative_path}"],
            cwd=repository,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return completed.stdout if completed.returncode == 0 else None

    @staticmethod
    def _decode_text(content: bytes | None) -> str:
        return content.decode("utf-8", errors="replace") if content is not None else ""
