from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess
from urllib.parse import urlparse
from uuid import uuid4

from dev_agent.domain.models import Repository
from dev_agent.infrastructure.store import SQLiteTaskStore


class RepositoryCatalog:
    def __init__(
        self,
        store: SQLiteTaskStore,
        allowed_roots: list[Path],
        clone_root: Path | None = None,
        github_token: str = "",
    ):
        self.store = store
        self.allowed_roots = list(dict.fromkeys(root.resolve() for root in allowed_roots))
        self.clone_root = (clone_root or self.allowed_roots[0] / ".agent-repositories").resolve()
        self.github_token = github_token

    def refresh_remotes(self) -> None:
        for repository in self.store.list_repositories():
            path = Path(repository.local_path)
            if not (path / ".git").exists():
                continue
            metadata = self._remote_metadata(path)
            repository.provider = str(metadata["provider"])
            repository.remote_url = metadata["remote_url"]
            repository.github_repository = metadata["github_repository"]
            self.store.save_repository(repository)

    def register(
        self,
        name: str,
        local_path: str | Path,
        *,
        require_git: bool = True,
        repository_id: str | None = None,
    ) -> Repository:
        path = Path(local_path).resolve()
        self._validate_path(path)
        if require_git and not (path / ".git").exists():
            raise ValueError("Repository path must contain a .git directory")
        for existing in self.store.list_repositories():
            if Path(existing.local_path).resolve() == path:
                if require_git:
                    metadata = self._remote_metadata(path)
                    existing.provider = str(metadata["provider"])
                    existing.remote_url = metadata["remote_url"]
                    existing.github_repository = metadata["github_repository"]
                    self.store.save_repository(existing)
                return existing
        remote_metadata = self._remote_metadata(path) if require_git else {}
        repository = Repository(
            id=repository_id or uuid4().hex[:12],
            name=name.strip(),
            local_path=str(path),
            execution_mode="plan_only",
            **remote_metadata,
        )
        return self.store.save_repository(repository)

    def register_remote(self, remote_url: str, name: str = "") -> Repository:
        github_repository = self._parse_github_repository(remote_url)
        if github_repository is None:
            raise ValueError("Only GitHub repository URLs are supported")
        canonical_url = f"https://github.com/{github_repository}.git"

        for existing in self.store.list_repositories():
            if existing.github_repository == github_repository:
                self._sync_remote_repository(Path(existing.local_path), canonical_url)
                return existing

        owner, repository_name = github_repository.split("/", 1)
        destination = self.clone_root / f"{owner}--{repository_name}"
        self._sync_remote_repository(destination, canonical_url)
        return self.register(name.strip() or repository_name, destination)

    def _sync_remote_repository(self, destination: Path, canonical_url: str) -> None:
        if destination.exists():
            if not (destination / ".git").is_dir():
                raise ValueError(f"Repository cache path is occupied: {destination}")
            metadata = self._remote_metadata(destination)
            if metadata["github_repository"] != self._parse_github_repository(canonical_url):
                raise ValueError("Repository cache points to a different GitHub repository")
            self._run_git(["git", "fetch", "origin", "--prune"], cwd=destination, timeout=180)
            return

        self.clone_root.mkdir(parents=True, exist_ok=True)
        self._run_git(
            ["git", "clone", "--origin", "origin", canonical_url, str(destination)],
            cwd=self.clone_root,
            timeout=300,
        )

    def _run_git(self, command: list[str], *, cwd: Path, timeout: int) -> None:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        if self.github_token:
            credential = base64.b64encode(f"x-access-token:{self.github_token}".encode()).decode()
            environment["GIT_CONFIG_COUNT"] = "1"
            environment["GIT_CONFIG_KEY_0"] = "http.extraHeader"
            environment["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {credential}"
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "Git command failed").strip()
            if self.github_token:
                detail = detail.replace(self.github_token, "***")
            raise ValueError(f"Unable to access GitHub repository: {detail[-800:]}")

    def browse_directories(self, path: str | Path | None = None) -> dict[str, object]:
        """Return a safe, shallow directory listing for the repository picker."""
        if path is None:
            roots = [root for root in self.allowed_roots if root.exists() and root.is_dir()]
            return {
                "path": None,
                "parent": None,
                "is_git_repository": False,
                "directories": [self._directory_entry(root) for root in roots],
            }

        current = Path(path).resolve()
        self._validate_browse_path(current)
        containing_root = max(
            (root for root in self.allowed_roots if current == root or root in current.parents),
            key=lambda root: len(root.parts),
        )
        parent = current.parent if current != containing_root else None
        try:
            children = sorted(
                (
                    child.resolve()
                    for child in current.iterdir()
                    if child.is_dir() and child.name not in {".git", "node_modules", ".venv", "__pycache__"}
                ),
                key=lambda child: child.name.casefold(),
            )
        except OSError as error:
            raise ValueError(f"Directory cannot be read: {current}") from error

        safe_children = [
            child
            for child in children
            if child == containing_root or containing_root in child.parents
        ]
        return {
            "path": str(current),
            "parent": str(parent) if parent is not None else None,
            "is_git_repository": (current / ".git").is_dir(),
            "directories": [self._directory_entry(child) for child in safe_children[:300]],
        }

    @staticmethod
    def _directory_entry(path: Path) -> dict[str, object]:
        return {
            "name": path.name or str(path),
            "path": str(path),
            "is_git_repository": (path / ".git").is_dir(),
        }

    def _validate_browse_path(self, path: Path) -> None:
        if not path.exists() or not path.is_dir():
            raise ValueError("Directory does not exist or cannot be opened")
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise ValueError("Directory is outside the allowed repository roots")

    @staticmethod
    def _remote_metadata(path: Path) -> dict[str, str | None]:
        completed = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        remote_url = completed.stdout.strip() if completed.returncode == 0 else None
        github_repository = RepositoryCatalog._parse_github_repository(remote_url or "")
        return {
            "provider": "github" if github_repository else "local",
            "remote_url": remote_url,
            "github_repository": github_repository,
        }

    @staticmethod
    def _parse_github_repository(remote_url: str) -> str | None:
        normalized = remote_url.strip()
        if normalized.startswith("git@github.com:"):
            slug = normalized.removeprefix("git@github.com:").removesuffix(".git")
        elif normalized.startswith("ssh://git@github.com/"):
            slug = normalized.removeprefix("ssh://git@github.com/").removesuffix(".git")
        else:
            parsed = urlparse(normalized)
            if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.query or parsed.fragment:
                return None
            slug = parsed.path.strip("/").removesuffix(".git")
        parts = slug.split("/")
        return "/".join(parts) if len(parts) == 2 and all(parts) else None

    def _validate_path(self, path: Path) -> None:
        if not path.exists() or not path.is_dir():
            raise ValueError("Repository path does not exist or is not a directory")
        if path == Path(path.anchor):
            raise ValueError("A filesystem root cannot be registered as a repository")
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            roots = ", ".join(str(root) for root in self.allowed_roots)
            raise ValueError(f"Repository path is outside allowed roots: {roots}")
