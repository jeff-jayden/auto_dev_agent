"""Cache/version/failure contracts; real model evaluation lives in scripts."""

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from agents.task_memory import working_memory
from domain.models import Task
from infrastructure.memory import RepositoryMemory
from infrastructure.semantic_memory import SemanticMemoryIndex
from infrastructure.store import SQLiteTaskStore
from infrastructure.working_memory_cache import WorkingMemoryCache


class Cache:
    def __init__(self):
        self.values = {}
        self.ttls = {}
    def get(self, key):
        return self.values.get(key)
    def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl
    def scan_iter(self, match, count):
        return [key for key in self.values if key.startswith(match[:-1])]
    def delete(self, key):
        self.values.pop(key, None)


def task():
    return Task(id="cache", title="test", requirement="preserve upload", status="developing")


def test_cache_hit_digest_miss_expiration_and_revision_validation(tmp_path):
    client = Cache()
    cache = WorkingMemoryCache(str(tmp_path), client)
    current = task()
    current.metadata["task_revision"] = 1
    build = Mock(return_value={"task_status": "developing"})
    cache.get_or_build(current, 3, build)
    cache.get_or_build(current, 3, build)
    assert build.call_count == 1 and cache.status == "hit"
    assert list(client.ttls.values()) == [1800]
    # A wrong envelope cannot be reused even if stored under the expected key.
    key = next(iter(client.values))
    value = json.loads(client.values[key])
    value["revision"] = 0
    client.values[key] = json.dumps(value)
    cache.get_or_build(current, 3, build)
    assert build.call_count == 2
    current.error = "unsaved failure"
    cache.get_or_build(current, 3, build)
    assert build.call_count == 3
    current.metadata["task_revision"] = 2
    cache.get_or_build(current, 3, build)
    assert build.call_count == 4
    cache.invalidate(current.id, 1)
    assert len(client.values) == 1  # Newer revision survived invalidation.
    client.values.clear()  # Simulate eviction or expiration.
    cache.get_or_build(current, 3, build)
    assert build.call_count == 5


@pytest.mark.parametrize("operation", ["get", "setex", "scan_iter"])
def test_redis_failures_fall_back_without_losing_state(tmp_path, operation):
    client = Cache()
    setattr(client, operation, Mock(side_effect=TimeoutError))
    cache = WorkingMemoryCache(str(tmp_path), client)
    current = task()
    expected = {"current_error": "real sqlite failure"}
    assert cache.get_or_build(current, 3, lambda: expected) == expected
    cache.invalidate(current.id, 0)


def test_store_versions_are_monotonic_with_concurrent_writers(tmp_path):
    path = tmp_path / "agent.db"
    store = SQLiteTaskStore(path)
    store.save_task(task())
    def save(_):
        another = SQLiteTaskStore(path)
        another.save_task(another.get_task("cache"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save, range(4)))
    assert store.get_task("cache").metadata["task_revision"] == 5


def test_real_working_memory_rejects_unsaved_changed_state(tmp_path, monkeypatch):
    import infrastructure.working_memory_cache as module
    client = Cache()
    monkeypatch.setattr(module, "WorkingMemoryCache", lambda path: WorkingMemoryCache(path, client))
    store = SQLiteTaskStore(tmp_path / "agent.db")
    current = task()
    store.save_task(current)
    working_memory(current)
    working_memory(current)
    assert current.metadata["working_memory_cache_status"] == "hit"
    current.error = "new error"
    assert working_memory(current)["current_error"] == "new error"
    assert current.metadata["working_memory_cache_status"] == "miss"
    store.save_task(current)
    restored = store.get_task(current.id)
    assert working_memory(restored)["current_error"] == "new error"


class ContractEmbedding:
    signature = "memory_contract_not_real_model"
    def embed(self, texts):
        return [[1.0, 0.0] if any(word in text for word in ("credential", "登录")) else [0.0, 1.0]
                for text in texts]


def test_chroma_semantic_recall_sqlite_truth_update_invalidation_and_scope(tmp_path, monkeypatch):
    chromadb = pytest.importorskip("chromadb")
    from infrastructure import semantic_memory as module
    monkeypatch.setenv("MEMORY_SEMANTIC_ENABLED", "true")
    client = chromadb.EphemeralClient()
    path = tmp_path / "agent.db"
    index = SemanticMemoryIndex(path, embedding=ContractEmbedding(), client=client)
    monkeypatch.setattr(module, "configured_index", lambda *args: index)
    memory = RepositoryMemory(path)
    memory.put("repo-a", "episode1", "experience", "credential expires", "task:first")
    memory.put("repo-a", "episode2", "experience", "upload file missing", "task:second")
    memory.put("repo-b", "episode3", "experience", "credential expires", "task:foreign")
    result = memory.search("repo-a", "登录失败", "experience")
    assert result["retrieval_mode"] == "hybrid_chroma"
    assert [r["key"] for r in result["records"]] == ["episode1"]
    assert result["records"][0]["source"] == "task:first"
    memory.put("repo-a", "episode1", "experience", "credential renewed", "task:updated")
    assert memory.search("repo-a", "登录失败")["records"][0]["content"] == "credential renewed"
    memory.invalidate("repo-a", "episode1")
    assert memory.search("repo-a", "登录失败")["records"] == []


def test_chroma_or_embedding_failure_uses_sqlite_and_reports_degradation(tmp_path, monkeypatch):
    from infrastructure import semantic_memory as module
    monkeypatch.setenv("MEMORY_SEMANTIC_ENABLED", "true")
    monkeypatch.setattr(module, "configured_index", Mock(side_effect=TimeoutError))
    memory = RepositoryMemory(tmp_path / "agent.db")
    memory.put("a", "rule", "convention", "组件放在 src/pages", "guide")
    result = memory.search("a", "组件")
    assert result["records"][0]["source"] == "guide"
    assert result["retrieval_mode"] == "sqlite_lexical"
    assert result["semantic_status"] == "unavailable"


def test_existing_sqlite_schema_migrates_without_dropping_records(tmp_path):
    import sqlite3
    path = tmp_path / "agent.db"
    current = task()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY,status TEXT,title TEXT,data TEXT,created_at TEXT,updated_at TEXT)")
        db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?)", (current.id, "developing", "test", current.model_dump_json(), "now", "now"))
    store = SQLiteTaskStore(path)
    assert store.get_task(current.id).requirement == current.requirement
    store.save_task(store.get_task(current.id))
    assert store.get_task(current.id).metadata["task_revision"] == 1


def test_sqlite_invalidation_racing_vector_query_is_respected(tmp_path, monkeypatch):
    from infrastructure import semantic_memory as module
    memory = RepositoryMemory(tmp_path / "agent.db")
    memory.put("a", "episode", "experience", "old solution", "task:old")
    def query(*args):
        memory.invalidate("a", "episode")
        return {"episode": 1.0}, "ok"
    monkeypatch.setattr(module, "semantic_scores", query)
    assert memory.search("a", "similar problem")["records"] == []


def test_only_verified_reviewed_task_becomes_cross_task_experience(tmp_path):
    from domain.models import ExecutionResult, ReviewRound, TaskStatus
    store = SQLiteTaskStore(tmp_path / "agent.db")
    current = task()
    current.repository_id = "a"
    current.result = ExecutionResult(success=True, exit_code=0, command=["npm", "test"], output="passed",
                                     diff="+fixed", mr_title="fixed", mr_description="fixed")
    store.save_task(current)
    assert store.memory.search("a", kind="experience")["records"] == []
    current.status = TaskStatus.WAITING_RELEASE_APPROVAL
    current.reviews = [ReviewRound(round=1, decision="approved", summary="review passed")]
    store.save_task(current)
    another = task()
    another.id = "next-task"
    another.repository_id = "a"
    store.save_task(another)
    experience = store.memory.search("a", kind="experience")["records"][0]
    assert experience["source"] == "task:cache:review:1"
    assert json.loads(experience["content"])["solution_is_model_report_not_independent_fact"]
