"""GitHub 交付服务。

将已审查的工作区修改提交并推送到 Agent 分支，创建或更新 PR、同步评论并执行用户批准后的合并。
"""

from __future__ import annotations

import base64
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from domain.models import RemotePullRequest, Repository, Task

from .github import GitHubClient


class GitHubDeliveryService:
    def __init__(self, client: GitHubClient):
        """初始化 GitHub 交付服务。

        Args:
            client: 用于推送分支、创建或更新 Pull Request 的 GitHub 客户端。
        """
        self.client = client

    def publish(self, task: Task, repository: Repository) -> RemotePullRequest:
        if repository.provider != "github" or not repository.github_repository:
            raise ValueError("Repository origin is not a supported GitHub repository")
        if not self.client.enabled:
            raise ValueError("GITHUB_TOKEN is not configured")
        if task.merge_request is None or task.result is None or not task.result.success:
            raise ValueError("A reviewed MR draft and successful test result are required")
        if not task.workspace or not Path(task.workspace).is_dir():
            raise ValueError("Task Worktree is missing")

        workspace = Path(task.workspace)
        previous = task.remote_pull_request
        branch = previous.head_branch if previous is not None else f"agent/{task.id}"
        if not branch.startswith("agent/"):
            raise ValueError("Agent may only push agent/* branches")
        base = previous.base_branch if previous is not None else (
            task.repository_analysis.default_branch if task.repository_analysis else None
        )
        base = base or "main"
        current_diff = self._git(workspace, "diff", "--", ".")
        if current_diff.strip() and current_diff.strip() != task.result.diff.strip():
            raise ValueError("Worktree diff no longer matches the reviewed result")

        self._git(workspace, "config", "user.email", "ai-dev-agent@users.noreply.github.com")
        self._git(workspace, "config", "user.name", "AI Dev Agent")
        self._git(workspace, "checkout", "-B", branch)
        if current_diff.strip():
            changed_files = task.merge_request.changed_files
            if not changed_files:
                raise ValueError("Reviewed change contains no files")
            self._git(workspace, "add", "--", *changed_files)
            self._git(workspace, "commit", "-m", f"feat(agent): {task.title} [{task.id}]")
        head_sha = self._git(workspace, "rev-parse", "HEAD").strip()
        self._push(workspace, branch)

        owner = repository.github_repository.split("/", 1)[0]
        pull = self.client.find_open_pull_request(repository.github_repository, owner, branch)
        created = pull is None
        if pull is None:
            pull = self.client.create_pull_request(
                repository.github_repository,
                task.merge_request.title,
                task.merge_request.description,
                branch,
                base,
            )
        number = int(pull["number"])
        if created or current_diff.strip():
            self.client.add_comment(
                repository.github_repository,
                number,
                self._review_comment(task),
            )
        return RemotePullRequest(
            repository=repository.github_repository,
            number=number,
            node_id=pull.get("node_id"),
            url=str(pull["html_url"]),
            state=str(pull.get("state", "open")),
            draft=bool(pull.get("draft", True)),
            head_branch=branch,
            base_branch=base,
            head_sha=head_sha,
        )

    def refresh(self, record: RemotePullRequest) -> RemotePullRequest:
        pull = self.client.get_pull_request(record.repository, record.number)
        record.state = str(pull.get("state", record.state))
        record.draft = bool(pull.get("draft", record.draft))
        record.updated_at = datetime.now(UTC)
        if pull.get("merged"):
            record.state = "merged"
            record.merged_sha = pull.get("merge_commit_sha")
        return record

    def list_review_comments(self, record: RemotePullRequest) -> list[dict]:
        comments: list[dict] = []
        sources = (
            ("conversation", self.client.list_issue_comments(record.repository, record.number)),
            ("inline", self.client.list_review_comments(record.repository, record.number)),
            ("review", self.client.list_reviews(record.repository, record.number)),
        )
        for kind, items in sources:
            for item in items:
                body = str(item.get("body") or "").strip()
                if not body or "<!-- ai-dev-agent-" in body:
                    continue
                identifier = item.get("id")
                if identifier is None:
                    continue
                user = item.get("user") if isinstance(item.get("user"), dict) else {}
                comments.append({
                    "key": f"{kind}:{identifier}",
                    "github_id": int(identifier),
                    "kind": kind,
                    "author": str(user.get("login") or "unknown"),
                    "body": body,
                    "path": item.get("path"),
                    "line": item.get("line") or item.get("original_line"),
                    "url": str(item.get("html_url") or record.url),
                    "created_at": item.get("created_at") or item.get("submitted_at"),
                    "command_requested": "/agent fix" in body.lower(),
                })
        return sorted(comments, key=lambda item: str(item.get("created_at") or ""))

    def reply_to_review_comment(
        self, record: RemotePullRequest, comment: dict, body: str
    ) -> None:
        marked_body = (
            f"<!-- ai-dev-agent-comment-result:{comment['key']} -->\n{body}"
        )
        if comment.get("kind") == "inline":
            self.client.reply_to_review_comment(
                record.repository,
                record.number,
                int(comment["github_id"]),
                marked_body,
            )
            return
        self.client.add_comment(record.repository, record.number, marked_body)

    def merge(self, record: RemotePullRequest) -> RemotePullRequest:
        if record.draft:
            if not record.node_id:
                raise ValueError("Draft pull request node ID is missing")
            self.client.mark_pull_request_ready(record.node_id)
        result = self.client.merge_pull_request(record.repository, record.number, record.head_sha)
        record.state = "merged"
        record.draft = False
        record.merged_sha = result.get("sha")
        record.updated_at = datetime.now(UTC)
        return record

    @staticmethod
    def _review_comment(task: Task) -> str:
        latest = task.reviews[-1] if task.reviews else None
        findings = [] if latest is None else latest.findings
        finding_lines = [
            f"- **{item.severity}** `{item.file or 'general'}{':' + str(item.line) if item.line else ''}` — {item.message}"
            for item in findings
        ]
        detail = "\n".join(finding_lines) or "- 没有阻塞问题"
        decision = latest.decision if latest else "not_run"
        return (
            f"<!-- ai-dev-agent-review:{task.id} -->\n"
            "## AI Dev Agent Code Review\n\n"
            f"- Decision: **{decision}**\n"
            f"- Test: `{task.merge_request.test_summary}`\n\n"
            f"### Findings\n\n{detail}"
        )

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-2000:]
            raise ValueError(f"Git command failed ({' '.join(arguments[:2])}): {detail}")
        return completed.stdout

    def _push(self, repository: Path, branch: str) -> None:
        credentials = base64.b64encode(
            f"x-access-token:{self.client.token}".encode("utf-8")
        ).decode("ascii")
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        completed = subprocess.run(
            ["git", "push", "origin", f"HEAD:refs/heads/{branch}"],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-2000:]
            raise ValueError(f"Git push failed: {detail}")
