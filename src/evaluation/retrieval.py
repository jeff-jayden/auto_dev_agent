"""RAG 检索效果评测器。

使用固定数据集对比关键词检索与混合检索，计算 Recall@K、MRR、Hit@K 和无关结果比例。
"""

from __future__ import annotations

import re
import hashlib
import json
from pathlib import Path
from time import perf_counter
from dataclasses import dataclass

from code_intelligence import RepositoryCodeIndex
from domain.models import (
    RetrievalCaseResult,
    RetrievalEvaluationComparison,
    RetrievalMetrics,
)


@dataclass(frozen=True)
class RetrievalCase:
    name: str
    query: str
    expected_files: tuple[str, ...]
    documents: dict[str, str]


CASES = (
    RetrievalCase(
        name="react_history_page",
        query="实现 History 页面并展示历史记录",
        expected_files=("src/HistoryPage.jsx",),
        documents={
            "src/HistoryPage.jsx": "export function HistoryPage() { return <h1>History</h1>; }",
            "src/ProfilePage.jsx": "export function ProfilePage() { return <h1>Profile</h1>; }",
            "src/theme.css": ".page { display: grid; }",
        },
    ),
    RetrievalCase(
        name="authentication_guard",
        query="增加用户身份校验",
        expected_files=("auth/session_guard.py",),
        documents={
            "auth/session_guard.py": "class SessionGuard:\n def authenticate_credentials(self, credentials): return bool(credentials)",
            "users/profile.py": "def update_profile(data): return data",
            "orders/store.py": "def save_order(order): return order",
        },
    ),
    RetrievalCase(
        name="csv_report_export",
        query="把统计报表导出为逗号分隔文件",
        expected_files=("reports/csv_writer.py",),
        documents={
            "reports/csv_writer.py": "class CsvWriter:\n def serialize_rows(self, rows): return encode_csv(rows)",
            "reports/dashboard.py": "def render_dashboard(metrics): return metrics",
            "storage/blob.py": "def upload_blob(data): return data",
        },
    ),
    RetrievalCase(
        name="order_timeout_retry",
        query="订单超时后重新执行",
        expected_files=("orders/retry_policy.py",),
        documents={
            "orders/retry_policy.py": "class RetryPolicy:\n def backoff_after_deadline(self, attempt): return attempt * 2",
            "orders/repository.py": "def find_order(order_id): return order_id",
            "payments/gateway.py": "def charge(amount): return amount",
        },
    ),
    RetrievalCase(
        name="dialog_interaction",
        query="点击按钮打开弹窗",
        expected_files=("src/components/ConfirmDialog.tsx",),
        documents={
            "src/components/ConfirmDialog.tsx": "export const ConfirmDialog = () => <dialog aria-modal='true' />",
            "src/components/Table.tsx": "export const Table = () => <table />",
            "src/api/client.ts": "export const request = () => fetch('/api')",
        },
    ),
    RetrievalCase(
        name="audit_timeline",
        query="展示操作历史时间线",
        expected_files=("audit/timeline_service.py",),
        documents={
            "audit/timeline_service.py": "class TimelineService:\n def list_audit_records(self): return []",
            "notifications/email.py": "def send_email(message): return message",
            "settings/preferences.py": "def save_preferences(value): return value",
        },
    ),
)


class RetrievalEvaluator:
    """Deterministic A/B benchmark for fielded lexical and hybrid retrieval."""

    def __init__(self, indexer: RepositoryCodeIndex | None = None, k: int = 3):
        """初始化代码检索评测器。

        Args:
            indexer: 待评测的仓库代码索引器；未提供时创建默认实现。
            k: 每个评测用例取前多少个检索结果计算指标。
        """
        self.indexer = indexer or RepositoryCodeIndex()
        self.k = k

    def run(self) -> RetrievalEvaluationComparison:
        rows: list[tuple[RetrievalCase, list[str], list[str]]] = []
        case_results: list[RetrievalCaseResult] = []
        for case in CASES:
            index = self._index(case.documents)
            baseline = [
                item["path"] for item in self.indexer.rank_files(
                    index, case.query, strategy="lexical"
                )
            ][:self.k]
            hybrid = [
                item["path"] for item in self.indexer.rank_files(
                    index, case.query, strategy="hybrid"
                )
            ][:self.k]
            rows.append((case, baseline, hybrid))
            case_results.append(RetrievalCaseResult(
                name=case.name,
                query=case.query,
                expected_files=list(case.expected_files),
                baseline_files=baseline,
                hybrid_files=hybrid,
            ))
        baseline_metrics = self._metrics(rows, position=1)
        hybrid_metrics = self._metrics(rows, position=2)
        return RetrievalEvaluationComparison(
            case_count=len(rows),
            k=self.k,
            baseline=baseline_metrics,
            hybrid=hybrid_metrics,
            delta={
                "recall_at_k": round(hybrid_metrics.recall_at_k - baseline_metrics.recall_at_k, 2),
                "mrr": round(hybrid_metrics.mrr - baseline_metrics.mrr, 2),
                "hit_at_k": round(hybrid_metrics.hit_at_k - baseline_metrics.hit_at_k, 2),
                "irrelevant_rate": round(
                    hybrid_metrics.irrelevant_rate - baseline_metrics.irrelevant_rate, 2
                ),
                "average_context_files": round(
                    hybrid_metrics.average_context_files - baseline_metrics.average_context_files, 2
                ),
            },
            cases=case_results,
        )

    def _metrics(
        self,
        rows: list[tuple[RetrievalCase, list[str], list[str]]],
        *,
        position: int,
    ) -> RetrievalMetrics:
        recalls: list[float] = []
        reciprocal_ranks: list[float] = []
        hits: list[float] = []
        irrelevant_rates: list[float] = []
        context_sizes: list[int] = []
        for row in rows:
            case = row[0]
            ranked = row[position]
            expected = set(case.expected_files)
            relevant = [path for path in ranked if path in expected]
            recalls.append(len(set(relevant)) / len(expected))
            first_rank = next(
                (rank for rank, path in enumerate(ranked, 1) if path in expected), None
            )
            reciprocal_ranks.append(1 / first_rank if first_rank else 0.0)
            hits.append(1.0 if relevant else 0.0)
            irrelevant_rates.append(
                (len(ranked) - len(relevant)) / len(ranked) if ranked else 0.0
            )
            context_sizes.append(len(ranked))
        return RetrievalMetrics(
            recall_at_k=round(100 * sum(recalls) / len(recalls), 2),
            mrr=round(100 * sum(reciprocal_ranks) / len(reciprocal_ranks), 2),
            hit_at_k=round(100 * sum(hits) / len(hits), 2),
            irrelevant_rate=round(100 * sum(irrelevant_rates) / len(irrelevant_rates), 2),
            average_context_files=round(sum(context_sizes) / len(context_sizes), 2),
        )

    @staticmethod
    def _index(documents: dict[str, str]) -> dict:
        records = {}
        for path, content in documents.items():
            symbols = re.findall(
                r"\b(?:class|def|function|const|let|var)\s+([A-Za-z_$][\w$]*)",
                content,
            )
            records[path] = {
                "symbols": symbols,
                "imports": [],
                "referenced_by": [],
                "search_text": f"{path}\n{content}".lower(),
            }
        return {"version": 2, "files": records}


def evaluate_repository(repository: Path, dataset: dict, indexer: RepositoryCodeIndex, k: int = 3, *, repository_identity: str | None = None, baseline_sha: str | None = None) -> dict:
    """同一工作区/同一标签对比 BM25F 混合基线与真实 RAG。"""
    from repository.analyzer import RepositoryAnalyzer
    if indexer.vector_store is None:
        raise ValueError("真实仓库评测必须启用 RAG_RETRIEVAL_MODE=embedding")
    if k < 1 or not dataset.get("cases"):
        raise ValueError("评测需要正整数 k 和非空 cases")
    files = RepositoryAnalyzer._list_files(repository)
    head = baseline_sha or RepositoryAnalyzer._git(repository, "rev-parse", "HEAD")
    index, mode, _ = indexer.build(repository, files, head)
    if repository_identity:
        # 临时导出目录不是仓库身份，避免重复评测生成无数向量集合。
        index = {**index, "repository": repository_identity}
    labels = dataset["cases"]
    for case in labels:
        if not case.get("query", "").strip() or not case.get("expected_files"):
            raise ValueError("每个 case 必须有 query 和 expected_files")
        if not set(case["expected_files"]) <= set(index["files"]):
            raise ValueError(f"评测标签含不存在或未被索引的文件：{case.get('name', '')}")
    baseline = RepositoryCodeIndex()
    rows, timings, results = [], {"baseline": [], "hybrid": []}, []
    # 单独计入建库耗时，查询计时不混入冷启动建库；空字符串不触发建库。
    started = perf_counter()
    indexer.vector_store.search(index, labels[0]["query"], k=1)
    indexing_ms = (perf_counter() - started) * 1000
    for case in labels:
        ranked = {}
        full = {}
        for name, engine in (("baseline", baseline), ("hybrid", indexer)):
            started = perf_counter()
            full[name] = engine.rank_files(index, case["query"], strategy="hybrid")[:k]
            ranked[name] = [item["path"] for item in full[name]]
            timings[name].append((perf_counter() - started) * 1000)
        rows.append((RetrievalCase(case.get("name", case["query"]), case["query"], tuple(case["expected_files"]), {}), ranked["baseline"], ranked["hybrid"]))
        results.append({"name": case.get("name"), "query": case["query"], "expected_files": case["expected_files"],
                        "baseline_files": ranked["baseline"], "hybrid_files": ranked["hybrid"],
                        "retrieved_chunks": [{key: chunk[key] for key in ("path", "symbol", "start_line", "end_line", "score")}
                                             for item in full["hybrid"] for chunk in item.get("chunks", [])[:2]]})
    evaluator = RetrievalEvaluator(k=k)
    old = evaluator._metrics(rows, position=1).model_dump()
    new = evaluator._metrics(rows, position=2).model_dump()
    return {"dataset": dataset.get("name", "custom"), "dataset_kind": "real_repository", "case_count": len(rows), "k": k,
            "baseline_strategy": "bm25f_hash_hybrid_v2", "hybrid_strategy": "embedding_chroma_rrf_v1",
            "repository": str(repository.resolve()), "head_sha": head,
            "content_fingerprint": hashlib.sha256(json.dumps([(p, r["hash"]) for p, r in sorted(index["files"].items())]).encode()).hexdigest(),
            "embedding_model": indexer.vector_store.embedding.model,
            "embedding_signature": indexer.vector_store.embedding.signature,
            "index_mode": mode, "indexing_ms": round(indexing_ms, 2),
            "baseline": old, "hybrid": new,
            "delta": {key: round(new[key] - old[key], 2) for key in old},
            "average_query_ms": {name: round(sum(values) / len(values), 2) for name, values in timings.items()},
            "cases": results,
            "limitations": "文件级标注集；不是功能成功率或人工验收结论。首个查询预热成本记入 indexing_ms。小样本结果不能外推生产收益。"}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="真实代码仓库 RAG A/B 评测（调用 Embedding 服务）")
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("runtime/evaluations/rag-ab.json"))
    parser.add_argument("--k", type=int, default=3)
    arguments = parser.parse_args()
    data = json.loads(arguments.dataset.read_text(encoding="utf-8"))
    engine = RepositoryCodeIndex.from_environment(Path("runtime/indexes"))
    report = evaluate_repository(arguments.repository.resolve(), data, engine, arguments.k)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("case_count", "baseline", "hybrid", "delta", "average_query_ms")}, ensure_ascii=False, indent=2))
    print(f"评测报告：{arguments.output.resolve()}")
