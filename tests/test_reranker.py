"""重排协议、有限候选、排序集成与成对评测契约测试；假评分不作为效果数据。"""

import io
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from code_intelligence.reranker import RerankerClient
from code_intelligence.indexer import RepositoryCodeIndex
from evaluation.retrieval import evaluate_rerankers


def client(**kwargs):
    return RerankerClient(endpoint="https://example.com/rerank", model="test-rerank", **kwargs)


def response(rows):
    return io.BytesIO(json.dumps({"output": {"results": rows}}).encode())


def test_reranker_request_and_order():
    rows = [{"index": 0, "relevance_score": .2}, {"index": 1, "relevance_score": .8}]
    with patch("code_intelligence.reranker.urlopen", return_value=response(rows)) as http:
        service = client(api_key="private")
        assert service.rank("query", ["a", "b"]) == [(1, .8), (0, .2)]
        request = http.call_args.args[0]
        payload = json.loads(request.data)
        assert payload["input"] == {"query": "query", "documents": ["a", "b"]}
        assert payload["parameters"]["top_n"] == 2
        assert "private" not in repr(service)
        assert "private" not in json.dumps(service.metadata())


@pytest.mark.parametrize("rows", [
    [], [{"index": 0, "relevance_score": .2}],
    [{"index": 0, "relevance_score": .2}] * 2,
    [{"index": -1, "relevance_score": .2}, {"index": 1, "relevance_score": .8}],
    [{"index": 0, "relevance_score": float("nan")}, {"index": 1, "relevance_score": .8}],
    [{"index": True, "relevance_score": .2}, {"index": 0, "relevance_score": .8}],
])
def test_invalid_response_does_not_fallback(rows):
    with patch("code_intelligence.reranker.urlopen", return_value=response(rows)):
        with pytest.raises(RuntimeError, match="响应"):
            client().rank("query", ["a", "b"])


def test_limits_errors_and_compatible():
    with pytest.raises(ValueError):
        RerankerClient(endpoint="https://user:secret@example.com", model="test")
    with pytest.raises(ValueError, match="预算"):
        client(candidate_limit=1).rank("query", ["a", "b"])
    with patch("code_intelligence.reranker.urlopen", side_effect=HTTPError("url", 401, "private", {}, None)):
        with pytest.raises(RuntimeError, match="HTTP 401") as error:
            client().rank("query", ["a"])
        assert "private" not in str(error.value)
    with patch("code_intelligence.reranker.urlopen", return_value=io.BytesIO(b'{"results":[{"index":0,"relevance_score":1}]}')):
        assert client(provider="compatible").rank("query", ["a"]) == [(0, 1.)]


class FakeReranker:
    model = "contract-only"
    candidate_limit = 3
    document_chars = 300

    def rank(self, query, documents):
        self.query, self.documents = query, documents
        return [(i, float(i)) for i in reversed(range(len(documents)))]

    def metadata(self):
        return {"model": self.model, "candidate_limit": self.candidate_limit}


def test_same_pool_evidence_budget_and_selection():
    reranker = FakeReranker()
    engine = RepositoryCodeIndex(reranker=reranker, rerank_strategy="model")
    index = {"files": {f"src/{name}.js": {"symbols": [name], "imports": [], "referenced_by": [],
             "chunks": [{"text": "const ORIGINAL = 'Code';" * 100, "start_line": 1, "end_line": 20}]}
             for name in ("Video", "Login", "Other", "Extra")}}
    raw = [{"path": path, "score": 1, "reasons": [], "chunks": []} for path in index["files"]]
    pool = engine.rank_context_files(index, "video", limit=2, ranked_hits=raw, rerank_strategy="candidates")
    rule = engine.rank_context_files(index, "video", limit=2, ranked_hits=raw, rerank_strategy="rule")
    model = engine.rank_context_files(index, "video", limit=2, ranked_hits=raw)
    assert len(pool) == 3
    assert {x["path"] for x in rule + model} <= {x["path"] for x in pool}
    assert model[0]["path"] == pool[-1]["path"]
    assert all(len(doc) <= 300 and "ORIGINAL" in doc for doc in reranker.documents)
    assert "rerank_model" in model[0]


def test_paired_evaluation_failure_coverage(tmp_path):
    (tmp_path / "Video.js").write_text("const Video = 1;", encoding="utf-8")
    (tmp_path / "Login.js").write_text("const Login = 1;", encoding="utf-8")
    class FakeVector:
        class embedding:
            model, signature = "fake", "fake-signature"
        def search(self, *args, **kwargs):
            return []
    engine = RepositoryCodeIndex(vector_store=FakeVector(), reranker=FakeReranker())
    dataset = {"cases": [{"name": "one", "query": "Video", "expected_files": ["Video.js"]},
                         {"name": "two", "query": "Login", "expected_files": ["Login.js"]}]}
    with patch.object(engine, "rank_files", return_value=[{"path": "Video.js", "score": 1, "reasons": [], "chunks": []}]), \
         patch.object(engine, "rerank_candidates", side_effect=[RuntimeError("unavailable"), [{"path": "Video.js", "score": 1}]]):
        report = evaluate_rerankers(tmp_path, dataset, engine, k=1)
    assert report["status"] == "partial" and report["evaluated_count"] == 1
    assert len(report["errors"]) == 1
    assert report["rule"]["recall_at_k"] == report["model"]["recall_at_k"] == 0


def test_reranker_environment_explicit_key(monkeypatch):
    monkeypatch.setenv("RERANK_ENDPOINT", "https://example.com/rerank")
    monkeypatch.setenv("RERANK_MODEL", "model")
    monkeypatch.setenv("EMBEDDING_API_KEY", "private")
    monkeypatch.delenv("RERANK_API_KEY", raising=False)
    monkeypatch.delenv("RERANK_API_KEY_ENV", raising=False)
    assert RerankerClient.from_environment().api_key == ""
    monkeypatch.setenv("RERANK_API_KEY_ENV", "EMBEDDING_API_KEY")
    assert RerankerClient.from_environment().api_key == "private"


def test_environment_selects_strategy(monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_RETRIEVAL_MODE", "legacy")
    monkeypatch.setenv("RAG_RERANK_STRATEGY", "model")
    monkeypatch.setenv("RERANK_ENDPOINT", "https://example.com/rerank")
    monkeypatch.setenv("RERANK_MODEL", "actual-rerank")
    engine = RepositoryCodeIndex.from_environment(tmp_path)
    assert engine.rerank_strategy == "model" and engine.reranker.model == "actual-rerank"
    monkeypatch.setenv("RAG_RERANK_STRATEGY", "invalid")
    with pytest.raises(ValueError):
        RepositoryCodeIndex.from_environment(tmp_path)


def test_snapshot_export_ignores_dirty_worktree(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (repo / "Video.js").write_text("const original = 1;", encoding="utf-8")
    git("add", "Video.js")
    git("commit", "-m", "snapshot")
    sha = git("rev-parse", "HEAD")
    (repo / "Video.js").write_text("const dirty = 2;", encoding="utf-8")
    engine = RepositoryCodeIndex(vector_store=object(), reranker=FakeReranker())
    dataset = {"repository_head_sha": sha, "cases": [{"query": "Video", "expected_files": ["Video.js"]}]}
    def build(snapshot, files, head):
        assert (snapshot / "Video.js").read_text() == "const original = 1;"
        assert head == sha
        # 中止后续网络阶段；验证导出快照正确，且临时资源会清理。
        raise RuntimeError("snapshot verified")
    with patch.object(engine, "build", side_effect=build):
        with pytest.raises(RuntimeError, match="snapshot verified"):
            evaluate_rerankers(repo, dataset, engine, k=1)
    assert (repo / "Video.js").read_text() == "const dirty = 2;"
