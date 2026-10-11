"""人工标注闭环契约测试：候选不计分、内置集导入、基线隔离与指标覆盖率。"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from domain.models import Repository, RepositoryAnalysis, Task, TaskStatus
from evaluation.dataset import (AnnotationRequest, BuiltinDatasetImportRequest, DatasetConflict,
                                DatasetEvaluationRequest, DatasetImportRequest, RagDatasetService)
from infrastructure.store import SQLiteTaskStore
from scm.github import GitHubClient


def git(repo, *args):
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=True)
    return result.stdout.strip()


@pytest.fixture
def dataset(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("def original():\n    return 'before'\n", encoding="utf-8")
    (repo / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("def modified():\n    return 'after'\n", encoding="utf-8")
    (repo / "new.py").write_text("answer = 42\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "merged changes")
    store = SQLiteTaskStore(tmp_path / "agent.db")
    store.save_repository(Repository(id="repo", name="Example", local_path=str(repo)))
    task = Task(id="task-1", title="历史需求", requirement="修改应用并添加功能", status=TaskStatus.MERGED, repository_id="repo",
                repository_analysis=RepositoryAnalysis(language="python", file_count=2, head_sha=base))
    task.development_attempts = [SimpleNamespace(changed_files=["app.py", "new.py"])]
    # 保持候选提取测试与 Task 序列化无关；真实存储由其他测试验证。
    service = RagDatasetService(store, runtime_root=tmp_path / "runtime", indexer_factory=lambda: object())
    with patch.object(store, "list_tasks", return_value=[task]):
        result = service.import_history(DatasetImportRequest(merged_only=False))
    assert len(result["created"]) == 1
    return service, store.list_rag_cases()[0], task, repo


def annotation(case, **overrides):
    values = dict(revision=case["revision"], query=case["query"], actor="测试用户", state="confirmed", split="dev",
                  labels={"app.py": "required", "new.py": "irrelevant"})
    values.update(overrides)
    return AnnotationRequest(**values)


def builtin_manifest(path, repo):
    path.write_text(json.dumps({
        "name": "test-builtin",
        "repository_head_sha": git(repo, "rev-parse", "HEAD"),
        "known_irrelevant_files": ["helper.py"],
        "cases": [{"name": "R02_app", "requirement_id": "R02", "source_commit": "abc",
                   "query": "找到应用功能的核心实现文件", "expected_files": ["app.py"]}],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def test_import_is_pending_idempotent_and_preserves_labels(dataset):
    service, case, task, _ = dataset
    assert case["state"] == "pending"
    assert all(item["label"] == "unreviewed" for item in case["candidates"])
    service.annotate(case["id"], annotation(case))
    with patch.object(service.store, "list_tasks", return_value=[task]):
        result = service.import_history(DatasetImportRequest(merged_only=False))
    assert not result["created"]
    assert service.overview()["cases"][0]["state"] == "confirmed"


def test_import_skips_existing_before_limit(dataset):
    service, _, task, _ = dataset
    another = task.model_copy(update={"id": "task-2"})
    with patch.object(service.store, "list_tasks", return_value=[task, another]):
        result = service.import_history(DatasetImportRequest(merged_only=False, limit=1))
    assert len(result["created"]) == 1


def test_builtin_dataset_imports_confirmed_test_cases_and_is_idempotent(dataset, tmp_path):
    service, _, _, repo = dataset
    service.builtin_dataset_path = builtin_manifest(tmp_path / "builtin.json", repo)

    result = service.import_builtin(BuiltinDatasetImportRequest(repository_id="repo"))
    assert len(result["created"]) == 1
    imported = next(case for case in service.store.list_rag_cases()
                    if case["source_task_id"].startswith("builtin:"))
    assert imported["state"] == "confirmed"
    assert imported["split"] == "test"
    assert imported["base_sha"] == git(repo, "rev-parse", "HEAD")
    assert {item["path"]: item["label"] for item in imported["candidates"]} == {
        "app.py": "required", "helper.py": "irrelevant"
    }
    repeated = service.import_builtin(BuiltinDatasetImportRequest(repository_id="repo"))
    assert not repeated["created"] and len(repeated["skipped"]) == 1


def test_pending_never_evaluated(dataset):
    service, _, _, _ = dataset
    with pytest.raises(ValueError, match="没有人工确认"):
        service.evaluate(DatasetEvaluationRequest())


@pytest.mark.parametrize("labels,message", [
    ({"app.py": "required", "new.py": "supporting"}, "开发前不存在"),
    ({"app.py": "required", "new.py": "unreviewed"}, "未审核"),
    ({"app.py": "required"}, "不能省略"),
    ({"app.py": "irrelevant", "new.py": "irrelevant"}, "至少"),
    ({"app.py": "required", "new.py": "irrelevant", "../secret.py": "required"}, "不合法"),
])
def test_annotation_rejects_invalid_labels(dataset, labels, message):
    service, case, _, _ = dataset
    with pytest.raises(ValueError, match=message):
        service.annotate(case["id"], annotation(case, labels=labels))


def test_add_missing_file_and_conflict(dataset):
    service, case, _, _ = dataset
    updated = service.annotate(case["id"], annotation(case, labels={"app.py": "required", "new.py": "irrelevant", "helper.py": "supporting"}))
    assert updated["revision"] == 2
    assert updated["actor"] == "测试用户"
    assert updated["candidates"][-1]["sources"] == ["human_added"]
    with pytest.raises(DatasetConflict):
        service.annotate(case["id"], annotation(case))
    assert service.baseline_files(case["id"]) == ["app.py", "helper.py"]


def test_missing_baseline_does_not_fall_back_to_head(dataset):
    service, _, task, _ = dataset
    task.repository_analysis.head_sha = None
    with pytest.raises(ValueError, match="开发前 Git 基线"):
        service._candidate(task, False)


def test_evaluation_uses_pre_development_snapshot_and_saves_report(dataset):
    service, case, _, repo = dataset
    service.annotate(case["id"], annotation(case))
    current_head = git(repo, "rev-parse", "HEAD")

    def evaluate(snapshot, data, engine, k, **kwargs):
        assert "before" in (snapshot / "app.py").read_text()
        assert not (snapshot / "new.py").exists()
        assert not (snapshot / ".git").exists()
        assert kwargs["baseline_sha"] == case["base_sha"]
        assert kwargs["repository_identity"].endswith(case["base_sha"])
        return {"content_fingerprint": "fingerprint", "embedding_model": "test-only", "embedding_signature": "contract",
                "average_query_ms": {"baseline": 1, "hybrid": 2},
                "cases": [{"name": case["id"], "expected_files": ["app.py"], "baseline_files": ["helper.py"], "hybrid_files": ["app.py"]}]}

    with patch("evaluation.dataset.evaluate_repository", side_effect=evaluate):
        report = service.evaluate(DatasetEvaluationRequest(k=1))
    assert report["evaluated_count"] == report["requested_count"] == 1
    assert report["baseline"]["recall_at_k"] == 0
    assert report["hybrid"]["required_recall_at_k"] == 100
    assert report["hybrid"]["precision_at_k"] == 100
    assert report["dataset_snapshot"][0]["revision"] == 2
    assert service.overview()["latest_run"]["id"] == report["id"]
    assert git(repo, "rev-parse", "HEAD") == current_head
    assert "after" in (repo / "app.py").read_text()


def test_failed_samples_not_scored_and_split_isolated(dataset):
    service, case, _, _ = dataset
    service.annotate(case["id"], annotation(case, split="test"))
    with pytest.raises(ValueError, match="没有人工确认"):
        service.evaluate(DatasetEvaluationRequest(split="dev"))
    with patch("evaluation.dataset.evaluate_repository", side_effect=RuntimeError("Embedding unavailable")):
        report = service.evaluate(DatasetEvaluationRequest(split="test"))
    assert report["status"] == "failed"
    assert report["baseline"] is None and report["hybrid"] is None
    assert report["evaluated_count"] == 0
    assert len(report["errors"]) == 1


def test_evaluation_lock(dataset):
    service, case, _, _ = dataset
    service.annotate(case["id"], annotation(case))
    service._evaluation_lock.acquire()
    try:
        with pytest.raises(DatasetConflict):
            service.evaluate(DatasetEvaluationRequest())
    finally:
        service._evaluation_lock.release()


def test_github_files_pagination():
    calls = []

    def transport(method, path, payload):
        calls.append(path)
        assert method == "GET"
        return [{"filename": f"file{i}.py"} for i in range(100)] if "&page=1" in path else [{"filename": "last.py"}]

    client = GitHubClient("contract-token", transport=transport)
    assert len(client.list_pull_request_files("owner/repo", 42)) == 101
    assert len(calls) == 2


def test_merged_pr_candidates_are_not_auto_confirmed(dataset):
    service, _, task, _ = dataset
    task.remote_pull_request = SimpleNamespace(repository="owner/repo", number=42, url="https://github.com/owner/repo/pull/42")
    service.github = SimpleNamespace(enabled=True, get_pull_request=lambda *args: {"merged": True},
                                     list_pull_request_files=lambda *args: [{"filename": "helper.py"}])
    result = service._candidate(task, True)
    assert result["state"] == "pending"
    assert next(item for item in result["candidates"] if item["path"] == "helper.py")["sources"] == ["merged_pr"]


def test_same_requirement_cannot_leak_across_splits(dataset):
    service, case, task, _ = dataset
    service.annotate(case["id"], annotation(case))
    another = task.model_copy(update={"id": "another"})
    with patch.object(service.store, "list_tasks", return_value=[another]):
        service.import_history(DatasetImportRequest(merged_only=False))
    candidate = next(item for item in service.store.list_rag_cases() if item["id"] != case["id"])
    with pytest.raises(ValueError, match="跨开发集/测试集"):
        service.annotate(candidate["id"], annotation(candidate, split="test"))


def test_api_annotation_and_errors(dataset, tmp_path):
    from fastapi.testclient import TestClient
    from apps.api import main

    service, case, _, repo = dataset
    service.builtin_dataset_path = builtin_manifest(tmp_path / "api-builtin.json", repo)
    # 不启动 lifespan/Worker，防止 API 测试触发真实后台任务。
    with patch.object(main, "rag_service", return_value=service):
        client = TestClient(main.app)
        assert client.get("/api/rag/dataset").json()["counts"]["pending"] == 1
        assert client.post("/api/rag/dataset/evaluate", json={}).status_code == 400
        payload = annotation(case).model_dump()
        assert client.put(f"/api/rag/dataset/cases/{case['id']}", json=payload).status_code == 200
        assert client.put(f"/api/rag/dataset/cases/{case['id']}", json=payload).status_code == 409
        assert client.get("/api/rag/dataset/cases/missing/files").status_code == 404
        assert client.post("/api/rag/dataset/evaluate", json={"k": 0}).status_code == 422
        imported = client.post("/api/rag/dataset/import-builtin", json={"repository_id": "repo"})
        assert imported.status_code == 200
        assert len(imported.json()["created"]) == 1


def test_real_evaluator_contract_integration(dataset, tmp_path):
    pytest.importorskip("chromadb")
    from code_intelligence.indexer import RepositoryCodeIndex
    from code_intelligence.vector_store import CodeVectorStore
    from tests.test_real_rag import ContractEmbedding

    service, case, _, _ = dataset
    service.annotate(case["id"], annotation(case))
    service.indexer_factory = lambda: RepositoryCodeIndex(vector_store=CodeVectorStore(tmp_path / "vectors", ContractEmbedding()))
    report = service.evaluate(DatasetEvaluationRequest(k=1))
    assert report["status"] == "completed"
    assert report["embedding_signature"] == ContractEmbedding.signature
    assert len(report["cases"]) == 1
    assert report["cases"][0]["base_sha"] == case["base_sha"]
    assert report["cases"][0]["hybrid_files"]
    assert report["cases"][0]["hybrid_context_files"]
    assert report["baseline_context"]
    assert report["hybrid_context"]
    assert report["context_strategy"] == "expanded_dependency_role_rerank_v1"
