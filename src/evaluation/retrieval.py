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
    rows, context_rows, results = [], [], []
    timings = {"baseline": [], "hybrid": []}
    context_timings = {"baseline": [], "hybrid": []}
    # 单独计入建库耗时，查询计时不混入冷启动建库；空字符串不触发建库。
    started = perf_counter()
    indexer.vector_store.search(index, labels[0]["query"], k=1)
    indexing_ms = (perf_counter() - started) * 1000
    for case in labels:
        ranked = {}
        context_ranked = {}
        full = {}
        for name, engine in (("baseline", baseline), ("hybrid", indexer)):
            started = perf_counter()
            raw = engine.rank_files(index, case["query"], strategy="hybrid")
            raw_elapsed = (perf_counter() - started) * 1000
            full[name] = raw[:k]
            ranked[name] = [item["path"] for item in full[name]]
            timings[name].append(raw_elapsed)
            context_started = perf_counter()
            context = engine.rank_context_files(index, case["query"], limit=k, ranked_hits=raw)
            context_ranked[name] = [item["path"] for item in context]
            context_timings[name].append(raw_elapsed + (perf_counter() - context_started) * 1000)
        rows.append((RetrievalCase(case.get("name", case["query"]), case["query"], tuple(case["expected_files"]), {}), ranked["baseline"], ranked["hybrid"]))
        context_rows.append((rows[-1][0], context_ranked["baseline"], context_ranked["hybrid"]))
        results.append({"name": case.get("name"), "query": case["query"], "expected_files": case["expected_files"],
                        "baseline_files": ranked["baseline"], "hybrid_files": ranked["hybrid"],
                        "baseline_context_files": context_ranked["baseline"],
                        "hybrid_context_files": context_ranked["hybrid"],
                        "retrieved_chunks": [{key: chunk[key] for key in ("path", "symbol", "start_line", "end_line", "score")}
                                             for item in full["hybrid"] for chunk in item.get("chunks", [])[:2]]})
    evaluator = RetrievalEvaluator(k=k)
    old = evaluator._metrics(rows, position=1).model_dump()
    new = evaluator._metrics(rows, position=2).model_dump()
    old_context = evaluator._metrics(context_rows, position=1).model_dump()
    new_context = evaluator._metrics(context_rows, position=2).model_dump()
    return {"dataset": dataset.get("name", "custom"), "dataset_kind": "real_repository", "case_count": len(rows), "k": k,
            "baseline_strategy": "bm25f_hash_hybrid_v2", "hybrid_strategy": "embedding_chroma_rrf_v1",
            "context_strategy": "model_rerank_v1" if indexer.rerank_strategy == "model" else "expanded_dependency_role_rerank_v1",
            "reranker": indexer.reranker.metadata() if indexer.rerank_strategy == "model" else None,
            "repository": str(repository.resolve()), "head_sha": head,
            "content_fingerprint": hashlib.sha256(json.dumps([(p, r["hash"]) for p, r in sorted(index["files"].items())]).encode()).hexdigest(),
            "embedding_model": indexer.vector_store.embedding.model,
            "embedding_signature": indexer.vector_store.embedding.signature,
            "index_mode": mode, "indexing_ms": round(indexing_ms, 2),
            "baseline": old, "hybrid": new,
            "delta": {key: round(new[key] - old[key], 2) for key in old},
            "baseline_context": old_context, "hybrid_context": new_context,
            "context_delta": {key: round(new_context[key] - old_context[key], 2) for key in old_context},
            "average_query_ms": {name: round(sum(values) / len(values), 2) for name, values in timings.items()},
            "average_final_context_ms": {name: round(sum(values) / len(values), 2) for name, values in context_timings.items()},
            "cases": results,
            "limitations": "文件级标注集；不是功能成功率或人工验收结论。首个查询预热成本记入 indexing_ms。小样本结果不能外推生产收益。"}


def evaluate_rerankers(repository: Path, dataset: dict, indexer: RepositoryCodeIndex,
                       k: int = 5, *, progress=None, baseline_sha: str | None = None,
                       repository_identity: str | None = None) -> dict:
    """固定真实混合召回及候选池，成对对比规则与模型重排；标签只用于计分。"""
    from repository.analyzer import RepositoryAnalyzer
    if indexer.vector_store is None or indexer.reranker is None:
        raise ValueError("重排评测需要真实 Embedding、Chroma 和 Reranker")
    if not dataset.get("cases") or not 1 <= k <= indexer.reranker.candidate_limit:
        raise ValueError("重排评测需要非空标签且 Top K 不能大于候选数")
    expected_head = dataset.get("repository_head_sha")
    if expected_head and baseline_sha is None:
        # 即便当前 HEAD 相同也导出，排除未提交文件污染；不 checkout/reset 用户仓库。
        import io
        import subprocess
        import tarfile
        import tempfile
        if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_head):
            raise ValueError("标注快照必须使用完整 Git SHA")
        archive = subprocess.run(["git", "archive", "--format=tar", expected_head], cwd=repository,
                                 capture_output=True, timeout=60, check=False)
        if archive.returncode:
            raise ValueError("无法导出标注对应 Git 快照")
        with tempfile.TemporaryDirectory(prefix="rag-rerank-") as directory:
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
                tar.extractall(directory, members=[m for m in tar.getmembers() if m.isfile() or m.isdir()], filter="data")
            return evaluate_rerankers(Path(directory), dataset, indexer, k, progress=progress,
                                     baseline_sha=expected_head, repository_identity=str(repository.resolve()))
    head = baseline_sha or RepositoryAnalyzer._git(repository, "rev-parse", "HEAD")
    if expected_head and head != expected_head:
        raise ValueError("评测快照与标注不一致")
    index, mode, _ = indexer.build(repository, RepositoryAnalyzer._list_files(repository), head)
    if repository_identity:
        index = {**index, "repository": repository_identity}
    for case in dataset["cases"]:
        if (not case.get("query", "").strip() or not case.get("expected_files")
                or not set(case["expected_files"]) <= set(index["files"])):
            raise ValueError(f"标注无效或文件不在当前快照：{case.get('name', '')}")
    started = perf_counter()
    indexer.vector_store.search(index, dataset["cases"][0]["query"], k=1)
    indexing_ms = (perf_counter() - started) * 1000
    rows, raw_rows, pool_rows, details, errors = [], [], [], [], []
    timing = {"retrieval": [], "rule": [], "model": []}
    for number, case in enumerate(dataset["cases"], 1):
        try:
            started = perf_counter()
            raw = indexer.rank_files(index, case["query"], strategy="hybrid")
            raw_ms = (perf_counter() - started) * 1000
            started = perf_counter()
            pool = indexer.rank_context_files(index, case["query"], limit=k, ranked_hits=raw,
                                             rerank_strategy="candidates")
            pool_ms = (perf_counter() - started) * 1000
            started = perf_counter()
            rule = indexer.rank_context_files(index, case["query"], limit=k, ranked_hits=raw,
                                             rerank_strategy="rule")
            rule_ms = (perf_counter() - started) * 1000
            started = perf_counter()
            model = indexer.rerank_candidates(index, case["query"], pool)[:k]
            model_ms = pool_ms + (perf_counter() - started) * 1000
            label = RetrievalCase(case.get("name", case["query"]), case["query"],
                                  tuple(case["expected_files"]), {})
            paths = lambda hits: [item["path"] for item in hits]
            rows.append((label, paths(rule), paths(model)))
            raw_rows.append((label, paths(raw[:k]), paths(raw[:k])))
            pool_rows.append((label, paths(pool), paths(pool)))
            for name, value in (("retrieval", raw_ms), ("rule", rule_ms), ("model", model_ms)):
                timing[name].append(value)
            details.append({"name": label.name, "query": case["query"],
                            "expected_files": case["expected_files"], "candidate_files": paths(pool),
                            "raw_files": paths(raw[:k]), "rule_files": paths(rule), "model_files": paths(model),
                            "model_scores": [{"path": item["path"], "score": item["score"]} for item in model],
                            "timing_ms": {"retrieval": round(raw_ms, 2), "rule": round(rule_ms, 2), "model": round(model_ms, 2)}})
        except (RuntimeError, ValueError) as error:
            errors.append({"name": case.get("name"), "error": str(error)})
        if progress:
            progress(number, len(dataset["cases"]), len(errors))
    evaluator = RetrievalEvaluator(k=k)
    rule_metrics = evaluator._metrics(rows, position=1).model_dump() if rows else None
    model_metrics = evaluator._metrics(rows, position=2).model_dump() if rows else None
    return {"dataset": dataset.get("name"), "dataset_kind": "reranker_paired_comparison",
            "status": "completed" if not errors else "partial" if rows else "failed",
            "requested_count": len(dataset["cases"]), "evaluated_count": len(rows), "k": k,
            "repository": repository_identity or str(repository.resolve()), "head_sha": head, "index_mode": mode,
            "content_fingerprint": hashlib.sha256(json.dumps(
                [(p, r["hash"]) for p, r in sorted(index["files"].items())]).encode()).hexdigest(),
            "embedding_model": indexer.vector_store.embedding.model,
            "embedding_signature": indexer.vector_store.embedding.signature,
            "reranker": indexer.reranker.metadata(),
            "rule_strategy": "expanded_dependency_role_rerank_v1", "model_strategy": "model_rerank_v1",
            "raw": evaluator._metrics(raw_rows, position=1).model_dump() if rows else None,
            "candidate_pool_recall": RetrievalEvaluator(k=indexer.reranker.candidate_limit)._metrics(pool_rows, position=1).recall_at_k if rows else None,
            "rule": rule_metrics, "model": model_metrics,
            "delta": {key: round(model_metrics[key] - rule_metrics[key], 2) for key in rule_metrics} if rows else None,
            "indexing_ms": round(indexing_ms, 2),
            "average_ms": {key: round(sum(values) / len(values), 2) if values else None for key, values in timing.items()},
            "cases": details, "errors": errors,
            "limitations": "同一候选池的成对宏平均；失败样本同时排除两组并报告覆盖数。标签未传给模型。最终代码快照检索不代表开发前召回或需求交付成功率；非独立留出集不得作为泛化结论。"}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="真实代码仓库 RAG A/B 评测（调用 Embedding 服务）")
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("runtime/evaluations/rag-ab.json"))
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--compare-rerank", action="store_true", help="固定混合召回，对比规则与 Reranker 模型")
    arguments = parser.parse_args()
    data = json.loads(arguments.dataset.read_text(encoding="utf-8"))
    engine = RepositoryCodeIndex.from_environment(Path("runtime/indexes"))
    if arguments.compare_rerank:
        from code_intelligence.reranker import RerankerClient
        engine.reranker = RerankerClient.from_environment()
        report = evaluate_rerankers(arguments.repository.resolve(), data, engine, arguments.k,
                                   progress=lambda done, total, errors: print(f"评测 {done}/{total}，失败 {errors}", flush=True))
    else:
        report = evaluate_repository(arguments.repository.resolve(), data, engine, arguments.k)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_keys = ("status", "evaluated_count", "rule", "model", "delta", "average_ms") if arguments.compare_rerank else ("case_count", "baseline", "hybrid", "delta", "average_query_ms")
    print(json.dumps({key: report[key] for key in summary_keys}, ensure_ascii=False, indent=2))
    print(f"评测报告：{arguments.output.resolve()}")
    if arguments.compare_rerank and report["status"] != "completed":
        raise SystemExit(1)
