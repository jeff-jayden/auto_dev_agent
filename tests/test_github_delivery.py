import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import build_orchestrator
from dev_agent.domain.models import RemotePullRequest, TaskStatus
from dev_agent.execution import TaskExecutionWorker
from dev_agent.repository.catalog import RepositoryCatalog
from dev_agent.scm import GitHubClient, GitHubDeliveryService


class FakeGitHubTransport:
    def __init__(self):
        self.calls = []
        self.open_pull = None
        self.next_number = 17
        self.issue_comments = []
        self.review_comments = []
        self.reviews = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))
        if method == "GET" and "/pulls?" in path:
            return [self.open_pull] if self.open_pull else []
        if method == "GET" and "/issues/" in path and "/comments" in path:
            return self.issue_comments
        if method == "GET" and "/pulls/" in path and "/comments?" in path:
            return self.review_comments
        if method == "GET" and "/pulls/" in path and "/reviews?" in path:
            return self.reviews
        if method == "POST" and path.endswith("/pulls"):
            return {
                "number": self.next_number,
                "node_id": f"PR_node_{self.next_number}",
                "html_url": f"https://github.com/example/project/pull/{self.next_number}",
                "state": "open",
                "draft": True,
            }
        if path == "/graphql":
            return {"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}}
        if method == "PUT" and path.endswith("/merge"):
            return {"merged": True, "sha": "merged-sha"}
        return {"id": 1}


class GitHubDeliveryTests(unittest.TestCase):
    def test_github_remote_parser(self):
        self.assertEqual(
            RepositoryCatalog._parse_github_repository("git@github.com:owner/repo.git"),
            "owner/repo",
        )
        self.assertEqual(
            RepositoryCatalog._parse_github_repository("https://github.com/owner/repo.git"),
            "owner/repo",
        )
        self.assertIsNone(
            RepositoryCatalog._parse_github_repository("https://gitlab.com/owner/repo.git")
        )

    def test_publish_draft_pull_request_and_merge_after_user_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orchestrator = build_orchestrator(root / "runtime")
            task = orchestrator.create_task(
                "为任务增加优先级",
                "任务支持 low、medium、high 三种优先级，默认使用 medium。",
            )
            task = orchestrator.approve(task.id, "tester", "同意")
            self.assertEqual(task.status, TaskStatus.WAITING_RELEASE_APPROVAL)

            bare_remote = root / "remote.git"
            subprocess.run(
                ["git", "init", "--bare", str(bare_remote)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", str(bare_remote)],
                cwd=task.workspace,
                check=True,
                capture_output=True,
            )
            repository = orchestrator.store.get_repository("test-repository")
            repository.provider = "github"
            repository.remote_url = "git@github.com:example/project.git"
            repository.github_repository = "example/project"
            orchestrator.store.save_repository(repository)

            transport = FakeGitHubTransport()
            orchestrator.github_delivery = GitHubDeliveryService(
                GitHubClient("test-token", transport=transport)
            )
            published = orchestrator.publish_pull_request(task.id, "publisher")

            self.assertEqual(published.status, TaskStatus.WAITING_MERGE_APPROVAL)
            self.assertEqual(published.remote_pull_request.number, 17)
            self.assertTrue(published.remote_pull_request.draft)
            remote_branch = subprocess.run(
                ["git", "--git-dir", str(bare_remote), "rev-parse", "refs/heads/agent/" + task.id],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(remote_branch, published.remote_pull_request.head_sha)
            create_call = next(call for call in transport.calls if call[1].endswith("/pulls"))
            self.assertTrue(create_call[2]["draft"])

            merged = orchestrator.merge_pull_request(task.id, "owner", "确认合入")

            self.assertEqual(merged.status, TaskStatus.MERGED)
            self.assertEqual(merged.remote_pull_request.merged_sha, "merged-sha")
            self.assertTrue(any(call[1] == "/graphql" for call in transport.calls))
            self.assertTrue(any(call[0] == "PUT" and call[1].endswith("/merge") for call in transport.calls))

    def test_publish_reuses_open_pull_request_and_creates_replacement_for_closed_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            orchestrator = build_orchestrator(root / "runtime")
            task = orchestrator.create_task(
                "为任务增加优先级",
                "任务支持 low、medium、high 三种优先级，默认使用 medium。",
            )
            task = orchestrator.approve(task.id, "tester", "同意")

            bare_remote = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(bare_remote)], check=True, capture_output=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(bare_remote)],
                cwd=task.workspace,
                check=True,
                capture_output=True,
            )
            repository = orchestrator.store.get_repository("test-repository")
            repository.provider = "github"
            repository.remote_url = "git@github.com:example/project.git"
            repository.github_repository = "example/project"
            orchestrator.store.save_repository(repository)

            transport = FakeGitHubTransport()
            orchestrator.github_delivery = GitHubDeliveryService(GitHubClient("token", transport=transport))
            first = orchestrator.publish_pull_request(task.id, "publisher")
            self.assertEqual(first.remote_pull_request.number, 17)

            transport.open_pull = {
                "number": 17,
                "node_id": "PR_node_17",
                "html_url": "https://github.com/example/project/pull/17",
                "state": "open",
                "draft": True,
            }
            first.status = TaskStatus.WAITING_RELEASE_APPROVAL
            orchestrator.store.save_task(first)
            reused = orchestrator.publish_pull_request(task.id, "publisher")
            self.assertEqual(reused.remote_pull_request.number, 17)
            self.assertEqual(
                len([call for call in transport.calls if call[0] == "POST" and call[1].endswith("/pulls")]),
                1,
            )

            transport.open_pull = None
            transport.next_number = 18
            reused.status = TaskStatus.WAITING_RELEASE_APPROVAL
            orchestrator.store.save_task(reused)
            replaced = orchestrator.publish_pull_request(task.id, "publisher")
            self.assertEqual(replaced.remote_pull_request.number, 18)
            self.assertEqual(
                len([call for call in transport.calls if call[0] == "POST" and call[1].endswith("/pulls")]),
                2,
            )

    def test_syncs_review_comments_and_queues_selected_fix_once(self):
        with tempfile.TemporaryDirectory() as directory:
            orchestrator = build_orchestrator(Path(directory) / "runtime")
            task = orchestrator.create_task("PR 评论闭环", "同步 GitHub 评论并交给 Agent 修复。")
            task.status = TaskStatus.WAITING_MERGE_APPROVAL
            task.remote_pull_request = RemotePullRequest(
                repository="example/project",
                number=17,
                url="https://github.com/example/project/pull/17",
                state="open",
                draft=True,
                head_branch=f"agent/{task.id}",
                base_branch="main",
                head_sha="abc123",
            )
            orchestrator.store.save_task(task)
            transport = FakeGitHubTransport()
            transport.issue_comments = [
                {
                    "id": 101,
                    "body": "/agent fix 请补充空状态",
                    "user": {"login": "reviewer"},
                    "html_url": "https://github.com/example/project/pull/17#issuecomment-101",
                    "created_at": "2026-09-25T10:00:00Z",
                },
                {
                    "id": 102,
                    "body": "<!-- ai-dev-agent-comment-result:conversation:101 --> done",
                    "user": {"login": "bot"},
                },
            ]
            transport.review_comments = [
                {
                    "id": 201,
                    "body": "这里需要处理 null",
                    "user": {"login": "inline-reviewer"},
                    "path": "src/App.js",
                    "line": 12,
                    "created_at": "2026-09-25T10:01:00Z",
                }
            ]
            orchestrator.github_delivery = GitHubDeliveryService(
                GitHubClient("token", transport=transport)
            )

            synchronized = orchestrator.sync_pull_request_comments(task.id)
            comments = synchronized.metadata["github_review_comments"]
            self.assertEqual([item["key"] for item in comments], ["conversation:101", "inline:201"])
            self.assertTrue(comments[0]["command_requested"])
            self.assertFalse(comments[1]["command_requested"])

            worker = TaskExecutionWorker(lambda: orchestrator, poll_interval=60)
            worker.start = lambda: None
            job = worker.enqueue_github_comments(
                task.id, "tester", ["conversation:101", "inline:201"]
            )
            queued = orchestrator.store.get_task(task.id).metadata["github_review_comments"]
            self.assertEqual(job.action, "github_comment_feedback")
            self.assertTrue(all(item["status"] == "queued" for item in queued))
            with self.assertRaisesRegex(ValueError, "already running"):
                worker.enqueue_github_comments(task.id, "tester", ["conversation:101"])

            completed = orchestrator.complete_github_comment_feedback(
                task.id,
                ["conversation:101", "inline:201"],
                True,
                "测试通过，PR 已更新。",
            )
            self.assertTrue(all(
                item["status"] == "resolved" and item["reply_status"] == "replied"
                for item in completed.metadata["github_review_comments"]
            ))
            self.assertTrue(any(
                call[0] == "POST" and call[1].endswith("/issues/17/comments")
                for call in transport.calls
            ))
            self.assertTrue(any(
                call[0] == "POST" and call[1].endswith("/comments/201/replies")
                for call in transport.calls
            ))


if __name__ == "__main__":
    unittest.main()
