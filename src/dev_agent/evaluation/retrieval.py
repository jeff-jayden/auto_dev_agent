from __future__ import annotations

import re
from dataclasses import dataclass

from dev_agent.code_intelligence import RepositoryCodeIndex
from dev_agent.domain.models import (
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
    """Deterministic A/B benchmark for legacy and hybrid repository retrieval."""

    def __init__(self, indexer: RepositoryCodeIndex | None = None, k: int = 3):
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
