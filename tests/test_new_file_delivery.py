import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from agents.merge_request_builder import MergeRequestBuilder
from domain.models import Repository, RepositoryAnalysis, Task, ExecutionResult, TechnicalPlan
from infrastructure.git_diff import workspace_diff
from scm.publisher import GitHubDeliveryService


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, encoding="utf-8").strip()


def setup_delivery(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "src/pages").mkdir(parents=True)
    (tmp_path / "src/App.js").write_text("export default null;\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "baseline")
    baseline = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "src/pages/YourVideos.js").write_text("export default function YourVideos() { return null; }\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__/generated.pyc").write_bytes(b"cache")
    task = Task(id="delivery", title="page", requirement="create page", status="waiting_release_approval",
        workspace=str(tmp_path), repository_analysis=RepositoryAnalysis(language="javascript", file_count=1, head_sha=baseline),
        technical_plan=TechnicalPlan(approach="create page", affected_files=["src/App.js", "src/pages/YourVideos.js"],
                                    implementation_steps=[], test_plan=[], risks=[]),
        result=ExecutionResult(success=True, command=["test"], exit_code=0, output="passed",
                               diff=workspace_diff(tmp_path, baseline), mr_title="page", mr_description="page"))
    task.merge_request = MergeRequestBuilder().write(task)
    repo = Repository(id="repo", name="repo", local_path=str(tmp_path), provider="github", github_repository="test/repo")
    client = Mock(enabled=True)
    client.find_open_pull_request.return_value = {"number": 9, "html_url": "https://github.com/test/repo/pull/9", "state": "open"}
    service = GitHubDeliveryService(client)
    service._push = Mock()
    return task, repo, service


def test_new_component_reaches_diff_mr_and_pushed_commit(tmp_path):
    task, repo, service = setup_delivery(tmp_path)
    assert "+++ b/src/pages/YourVideos.js" in task.result.diff
    assert "__pycache__" not in task.result.diff
    assert task.merge_request.changed_files == ["src/pages/YourVideos.js"]
    assert git(tmp_path, "diff", "--cached") == ""  # diff collection never stages files
    published = service.publish(task, repo)
    assert "function YourVideos" in git(tmp_path, "show", published.head_sha + ":src/pages/YourVideos.js")
    service._push.assert_called_once()
    assert workspace_diff(tmp_path) == ""
    # Later edits retain the first published component in cumulative review.
    (tmp_path / "src/App.js").write_text("import YourVideos from './pages/YourVideos';\nexport default YourVideos;\n", encoding="utf-8")
    task.result.diff = workspace_diff(tmp_path, task.repository_analysis.head_sha)
    task.merge_request = MergeRequestBuilder().write(task)
    assert set(task.merge_request.changed_files) == {"src/App.js", "src/pages/YourVideos.js"}
    service.publish(task, repo)


def test_stale_mr_file_list_cannot_silently_skip_new_file(tmp_path):
    task, repo, service = setup_delivery(tmp_path)
    task.merge_request.changed_files = ["src/App.js"]
    with pytest.raises(ValueError, match="Staged files"):
        service.publish(task, repo)
    service._push.assert_not_called()


def test_new_file_changed_after_review_is_rejected(tmp_path):
    task, repo, service = setup_delivery(tmp_path)
    (tmp_path / "src/pages/YourVideos.js").write_text("unreviewed edit", encoding="utf-8")
    with pytest.raises(ValueError, match="reviewed result"):
        service.publish(task, repo)
    service._push.assert_not_called()
