"""GitHub API 客户端。

封装 Pull Request、评论、审查和合并相关的 HTTP/GraphQL 请求，不包含任务层业务判断。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GitHubApiError(RuntimeError):
    pass


Transport = Callable[[str, str, dict | None], dict | list]


class GitHubClient:
    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        transport: Transport | None = None,
    ):
        """初始化 GitHub API 客户端。

        Args:
            token: GitHub 鉴权 Token，仅用于当前客户端请求。
            api_url: GitHub REST API 根地址，也可配置为企业版地址。
            transport: 可选的 HTTP 传输函数，便于测试或替换网络实现。
        """
        self.token = token.strip()
        self.api_url = api_url.rstrip("/")
        self._transport = transport or self._request

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def find_open_pull_request(self, repository: str, owner: str, branch: str) -> dict | None:
        head = quote(f"{owner}:{branch}", safe="")
        result = self._transport(
            "GET", f"/repos/{repository}/pulls?state=open&head={head}", None
        )
        return result[0] if isinstance(result, list) and result else None

    def create_pull_request(
        self,
        repository: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> dict:
        result = self._transport(
            "POST",
            f"/repos/{repository}/pulls",
            {"title": title, "body": body, "head": head, "base": base, "draft": True},
        )
        if not isinstance(result, dict):
            raise GitHubApiError("GitHub returned an invalid pull request response")
        return result

    def add_comment(self, repository: str, number: int, body: str) -> dict:
        result = self._transport(
            "POST", f"/repos/{repository}/issues/{number}/comments", {"body": body}
        )
        return result if isinstance(result, dict) else {}

    def list_issue_comments(self, repository: str, number: int) -> list[dict]:
        result = self._transport(
            "GET", f"/repos/{repository}/issues/{number}/comments?per_page=100", None
        )
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    def list_review_comments(self, repository: str, number: int) -> list[dict]:
        result = self._transport(
            "GET", f"/repos/{repository}/pulls/{number}/comments?per_page=100", None
        )
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    def list_reviews(self, repository: str, number: int) -> list[dict]:
        result = self._transport(
            "GET", f"/repos/{repository}/pulls/{number}/reviews?per_page=100", None
        )
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    def reply_to_review_comment(
        self, repository: str, number: int, comment_id: int, body: str
    ) -> dict:
        result = self._transport(
            "POST",
            f"/repos/{repository}/pulls/{number}/comments/{comment_id}/replies",
            {"body": body},
        )
        return result if isinstance(result, dict) else {}

    def get_pull_request(self, repository: str, number: int) -> dict:
        result = self._transport("GET", f"/repos/{repository}/pulls/{number}", None)
        if not isinstance(result, dict):
            raise GitHubApiError("GitHub returned an invalid pull request response")
        return result

    def merge_pull_request(self, repository: str, number: int, head_sha: str) -> dict:
        result = self._transport(
            "PUT",
            f"/repos/{repository}/pulls/{number}/merge",
            {"sha": head_sha, "merge_method": "squash"},
        )
        if not isinstance(result, dict) or not result.get("merged"):
            message = result.get("message", "GitHub refused to merge the pull request") if isinstance(result, dict) else "Invalid GitHub response"
            raise GitHubApiError(message)
        return result

    def mark_pull_request_ready(self, node_id: str) -> dict:
        result = self._transport(
            "POST",
            "/graphql",
            {
                "query": (
                    "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id})"
                    "{pullRequest{id isDraft}}}"
                ),
                "variables": {"id": node_id},
            },
        )
        if not isinstance(result, dict) or result.get("errors"):
            raise GitHubApiError("GitHub could not mark the draft pull request ready")
        return result

    def _request(self, method: str, path: str, payload: dict | None) -> dict | list:
        if not self.token:
            raise GitHubApiError("GITHUB_TOKEN is not configured")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.api_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ai-dev-agent",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:2000]
            raise GitHubApiError(f"GitHub API {error.code}: {detail}") from error
        except (URLError, TimeoutError) as error:
            raise GitHubApiError(f"GitHub API request failed: {error}") from error
