"""真实 RAG 的协议、切片、索引失效、Chroma 隔离与上下文集成测试；假向量仅用于契约测试。"""

import io
import json
from unittest.mock import patch

import pytest

from code_intelligence.embedding import EmbeddingClient
from code_intelligence.indexer import RepositoryCodeIndex
from code_intelligence.vector_store import CodeVectorStore, code_chunks
from evaluation.retrieval import evaluate_repository


class ContractEmbedding:
    signature = "contract_test_not_a_real_model"
    model = "contract-only"

    def __init__(self):
        self.inputs = []

    def embed(self, texts):
        self.inputs.extend(texts)
        return [[1.0, 0.0] if "authenticate" in text or "身份" in text else [0.0, 1.0] for text in texts]


def test_chunks_preserve_source_and_bound_size():
    content = "import os\n\nclass Guard:\n    def authenticate(self):\n        return True\n\n" + "x" * 6000 + "\n"
    chunks = code_chunks("auth.py", content)
    assert "Guard.authenticate" in {item["symbol"] for item in chunks}
    assert all(len(item["text"]) <= 2400 for item in chunks)
    assert "".join(item["text"] for item in chunks) == content
    assert all(item["start_line"] <= item["end_line"] for item in chunks)
    assert code_chunks("auth.py", content) == chunks


@pytest.mark.parametrize("path", ["view.jsx", "style.css", "broken.py"])
def test_chunks_cover_other_languages_and_invalid_python(path):
    content = "export function Example() {\n return <button>Click</button>;\n}\n"
    assert "".join(item["text"] for item in code_chunks(path, content)) == content


def test_embedding_protocol_and_order():
    result = {"data": [{"index": 1, "embedding": [0, 3]}, {"index": 0, "embedding": [4, 0]}]}
    with patch("code_intelligence.embedding.urlopen", return_value=io.BytesIO(json.dumps(result).encode())) as request:
        client = EmbeddingClient("openai", "test", "https://embedding.example/v1", "secret")
        assert client.embed(["first", "second"]) == [[1, 0], [0, 1]]
        assert request.call_args.args[0].full_url == "https://embedding.example/v1/embeddings"
        assert json.loads(request.call_args.args[0].data)["input"] == ["first", "second"]
        assert "secret" not in client.signature


def test_ollama_protocol():
    with patch("code_intelligence.embedding.urlopen", return_value=io.BytesIO(b'{"embeddings":[[3,4]]}')) as request:
        assert EmbeddingClient("ollama", "test", "http://localhost:11434").embed(["text"]) == [[0.6, 0.8]]
        assert request.call_args.args[0].full_url.endswith("/api/embed")
        assert json.loads(request.call_args.args[0].data)["truncate"] is False


@pytest.mark.parametrize("vectors", [[], [[0, 0]], [[float("nan"), 1]], [[1, 2], [1]]])
def test_invalid_embeddings_rejected(vectors):
    with patch("code_intelligence.embedding.urlopen", return_value=io.BytesIO(json.dumps({"embeddings": vectors}).encode())):
        with pytest.raises(RuntimeError, match="数量、维度或数值"):
            EmbeddingClient("ollama", "test", "http://localhost:11434").embed(["text"])


def test_embedding_failure_has_no_secret():
    from urllib.error import HTTPError
    with patch("code_intelligence.embedding.urlopen", side_effect=HTTPError("secret", 401, "secret", {}, None)):
        with pytest.raises(RuntimeError, match="HTTP 401") as failure:
            EmbeddingClient("openai", "test", "https://embedding.example/v1", "secret").embed(["text"])
        assert "secret" not in str(failure.value)


def test_index_updates_uncommitted_added_and_deleted_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "main.py"
    source.write_text("def old(): return 1\n", encoding="utf-8")
    indexer = RepositoryCodeIndex(tmp_path / "indexes")
    first, _, _ = indexer.build(repo, ["main.py"], "same-head")
    assert indexer.build(repo, ["main.py"], "same-head")[1] == "cache_hit"
    source.write_text("def new(): return 2\n", encoding="utf-8")
    updated, mode, changed = indexer.build(repo, ["main.py"], "same-head")
    assert mode == "incremental" and changed == ["main.py"]
    assert first["files"]["main.py"]["hash"] != updated["files"]["main.py"]["hash"]
    source.unlink()
    (repo / "added.py").write_text("x = 2\n", encoding="utf-8")
    updated, _, changed = indexer.build(repo, ["added.py"], "same-head")
    assert set(updated["files"]) == {"added.py"}
    assert changed == ["added.py", "main.py"]


def test_chroma_real_storage_cache_snapshot_isolation_and_evidence(tmp_path):
    pytest.importorskip("chromadb")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "guard.py").write_text("def authenticate():\n    return True\n", encoding="utf-8")
    (repo / "unrelated.py").write_text("def wallpaper():\n    return 'blue'\n", encoding="utf-8")
    embedding = ContractEmbedding()
    store = CodeVectorStore(tmp_path / "vectors", embedding)
    indexer = RepositoryCodeIndex(tmp_path / "indexes", vector_store=store)
    index, mode, changed = indexer.build(repo, ["guard.py", "unrelated.py"], "head")
    context, evidence = indexer.context_pack(repo, index, "身份校验", [], mode, changed)
    assert context.retrieval_strategy == "embedding_chroma_rrf_v1"
    assert context.primary_files[0] == "guard.py"
    assert any(item.path == "guard.py" and "def authenticate" in item.snippet and "行 1-2" in item.reason for item in evidence)
    embedded = len(embedding.inputs)
    indexer.rank_files(index, "身份校验")
    assert len(embedding.inputs) == embedded + 1  # 只请求查询，不重复嵌入代码
    (repo / "unrelated.py").write_text("def wallpaper():\n    return 'red'\n", encoding="utf-8")
    mixed, _, _ = indexer.build(repo, ["guard.py", "unrelated.py"], "head")
    assert store.search(mixed, "身份校验")[0]["path"] == "guard.py"  # 同批次混合命中缓存与新模型向量
    (repo / "guard.py").unlink()
    updated, _, _ = indexer.build(repo, ["unrelated.py"], "head")
    assert {hit["path"] for hit in store.search(updated, "身份校验")} == {"unrelated.py"}
    assert "guard.py" in {hit["path"] for hit in store.search(index, "身份校验")}  # 老任务快照仍可用
    assert {hit["path"] for hit in CodeVectorStore(tmp_path / "vectors", embedding).search(updated, "身份校验")} == {"unrelated.py"}
    report = evaluate_repository(repo, {"cases": [{"query": "wallpaper", "expected_files": ["unrelated.py"]}]}, indexer, 1)
    assert report["case_count"] == 1 and report["hybrid"]["recall_at_k"] == 100
    assert report["embedding_model"] == "contract-only"
    with pytest.raises(ValueError, match="不存在"):
        evaluate_repository(repo, {"cases": [{"query": "x", "expected_files": ["missing.py"]}]}, indexer)


def test_real_mode_does_not_silently_use_legacy(monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_RETRIEVAL_MODE", "embedding")
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    with pytest.raises(ValueError, match="EMBEDDING_MODEL"):
        RepositoryCodeIndex.from_environment(tmp_path)


def test_celery_cannot_share_embedded_chroma(monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_RETRIEVAL_MODE", "embedding")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("EMBEDDING_MODEL", "test")
    monkeypatch.setenv("EXECUTION_BACKEND", "celery")
    monkeypatch.delenv("RAG_CHROMA_URL", raising=False)
    with pytest.raises(ValueError, match="RAG_CHROMA_URL"):
        RepositoryCodeIndex.from_environment(tmp_path)


def test_chroma_http_mode_is_selected(tmp_path):
    pytest.importorskip("chromadb")
    with patch("chromadb.HttpClient") as client, patch("chromadb.PersistentClient") as embedded:
        CodeVectorStore(tmp_path, ContractEmbedding(), server_url="http://127.0.0.1:8001")
        assert client.call_args.kwargs["port"] == 8001
        embedded.assert_not_called()
