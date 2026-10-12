"""Live Redis + Chroma + Embedding evaluation on isolated synthetic task history."""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
if (ROOT / ".env").exists():
    for raw in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        if raw.strip() and not raw.lstrip().startswith("#") and "=" in raw:
            name, value = raw.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip())

from agents.task_memory import working_memory
from domain.models import Task
from infrastructure.store import SQLiteTaskStore
from infrastructure.semantic_memory import SemanticMemoryIndex
from code_intelligence.embedding import EmbeddingClient

CASES = [
    ("episode1", "用户登录后立即退出。原因是会话凭证过期，刷新令牌后重新请求接口。",
     "账号刚进去又被踢回认证界面，怎样处理？"),
    ("episode2", "新增页面使用了未声明的 React 组件，运行时报 ReferenceError。将页面模块导出并补齐导入路径。",
     "浏览器提示某个视图标识符找不到，但新页面源码已经写了"),
    ("episode3", "界面正常但远端分支缺少新增文件。提交前将未跟踪文件加入暂存区，核对最终 Git diff。",
     "本地做好了按钮，代码托管网站却没有那个新模块"),
    ("episode4", "审查阶段输入超过模型上下文窗口。压缩重复日志，按需读取文件并预留输出额度。",
     "给大模型看的材料太长，检查代码这一步总是过不了"),
]


def evaluate():
    with tempfile.TemporaryDirectory(prefix="agent-memory-eval-") as directory:
        store = SQLiteTaskStore(Path(directory) / "agent.db")
        task = Task(id="current", title="new task", requirement="继续之前未完成的修改", status="developing", repository_id="eval")
        os.environ["WORKING_MEMORY_REDIS_URL"] = os.getenv("WORKING_MEMORY_REDIS_URL", "redis://127.0.0.1:6379/2")
        os.environ["MEMORY_SEMANTIC_ENABLED"] = "false"
        store.save_task(task)
        first = working_memory(task)
        working_memory(task)
        assert task.metadata["working_memory_cache_status"] == "hit"
        task.error = "new unsaved error"
        assert working_memory(task)["current_error"] == task.error
        assert task.metadata["working_memory_cache_status"] == "miss"
        store.save_task(task)
        assert working_memory(store.get_task(task.id))["current_error"] == task.error
        healthy_url = os.environ["WORKING_MEMORY_REDIS_URL"]
        os.environ["WORKING_MEMORY_REDIS_URL"] = "redis://127.0.0.1:1/2"
        assert working_memory(task)["current_error"] == task.error
        assert task.metadata["working_memory_cache_status"] == "unavailable"
        os.environ["WORKING_MEMORY_REDIS_URL"] = healthy_url
        for key, content, _ in CASES:
            store.memory.put("eval", key, "experience", content, "task:past_" + key)
        store.memory.put("foreign", "episode1", "experience", CASES[0][1], "task:foreign")
        report = {"fixture": "synthetic cross-task history; real embedding model", "redis": "hit/version/mutation/outage passed"}
        embedding = EmbeddingClient.from_environment()
        report.update(embedding_model=embedding.model, embedding_provider=embedding.provider,
                      minimum_similarity=float(os.getenv("MEMORY_MIN_SIMILARITY", "0.45")))
        for mode in ("lexical", "semantic"):
            os.environ["MEMORY_SEMANTIC_ENABLED"] = "true" if mode == "semantic" else "false"
            ranks, samples = [], []
            for expected, _, query in CASES:
                result = store.memory.search("eval", query, "experience", limit=4)
                if mode == "semantic":
                    assert result["semantic_status"] == "ok", "Semantic backend unavailable"
                keys = [r["key"] for r in result["records"]]
                assert all(r["repository_id"] == "eval" for r in result["records"])
                rank = keys.index(expected) + 1 if expected in keys else None
                ranks.append(rank)
                samples.append({"query": query, "expected": expected, "rank": rank, "returned": keys})
            report[mode] = {"hit_at_1": sum(r == 1 for r in ranks) / len(ranks),
                            "hit_at_3": sum(r is not None and r <= 3 for r in ranks) / len(ranks),
                            "mrr": sum(1 / r if r else 0 for r in ranks) / len(ranks), "samples": samples}
        store.memory.invalidate("eval", "episode1")
        assert "episode1" not in [r["key"] for r in store.memory.search("eval", CASES[0][2])["records"]]
        report["invalidated_and_foreign_excluded"] = True
        chroma_url = os.environ.get("MEMORY_CHROMA_URL", "http://127.0.0.1:8001")
        os.environ["MEMORY_CHROMA_URL"] = "http://127.0.0.1:1"
        degraded = store.memory.search("eval", "ReferenceError")
        assert degraded["semantic_status"] == "unavailable" and degraded["records"][0]["key"] == "episode2"
        os.environ["MEMORY_CHROMA_URL"] = chroma_url
        embedding_url = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11434")
        os.environ["EMBEDDING_BASE_URL"] = "http://127.0.0.1:1"
        degraded = store.memory.search("eval", "ReferenceError")
        assert degraded["semantic_status"] == "unavailable" and degraded["records"][0]["key"] == "episode2"
        os.environ["EMBEDDING_BASE_URL"] = embedding_url
        report["chroma_and_embedding_outage_fallback"] = "passed"
        # Delete only this evaluation's isolated vector collection.
        index = SemanticMemoryIndex(store.database_path)
        identity = hashlib.sha256((index.namespace + "eval" + index.embedding.signature).encode()).hexdigest()[:40]
        index.client.delete_collection("memory_" + identity)
        return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.dumps(evaluate(), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(report + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(report)
