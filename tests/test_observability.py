import tempfile
import unittest
from pathlib import Path

from infrastructure.store import SQLiteTaskStore
from observability import TraceRecorder
from evaluation import GoldenCaseEvaluator
from evaluation import RetrievalEvaluator
from domain.models import TraceSpan


class ObservabilityTests(unittest.TestCase):
    def test_nested_spans_are_persisted_and_secrets_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteTaskStore(Path(directory) / "agent.db")
            recorder = TraceRecorder(store)
            with recorder.trace("test", task_id="task-1") as trace:
                with recorder.span("agent.developer", kind="agent") as parent:
                    with recorder.span(
                        "tool.test", kind="tool",
                        attributes={"github_token": "never-store", "exit_code": 0},
                    ) as child:
                        child.output_summary = "passed"
            spans = store.list_spans(trace.id)
            self.assertEqual(len(spans), 2)
            self.assertEqual(spans[1].parent_span_id, parent.id)
            self.assertEqual(spans[1].attributes["github_token"], "[REDACTED]")
            self.assertEqual(store.get_trace(trace.id).status, "succeeded")

    def test_token_metrics_are_not_secrets_and_legacy_redaction_is_tolerated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteTaskStore(Path(directory) / "agent.db")
            recorder = TraceRecorder(store)
            redacted = recorder.redact({
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "access_token": "secret-value",
            })
            self.assertEqual(redacted["prompt_tokens"], 120)
            self.assertEqual(redacted["completion_tokens"], 30)
            self.assertEqual(redacted["access_token"], "[REDACTED]")

            store.save_span(TraceSpan(
                id="legacy-span", trace_id="legacy-trace", kind="llm", name="legacy",
                status="succeeded",
                attributes={"prompt_tokens": "[REDACTED]", "completion_tokens": "[REDACTED]"},
            ))
            metrics = store.trace_metrics()
            self.assertEqual(metrics["prompt_tokens"], 0)
            self.assertEqual(metrics["completion_tokens"], 0)

    def test_golden_cases_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteTaskStore(Path(directory) / "agent.db")
            result = GoldenCaseEvaluator(store).run()
            self.assertEqual(result.score, 100.0)
            self.assertEqual(result.passed, 5)
            self.assertEqual(store.latest_evaluation().id, result.id)

    def test_hybrid_rag_metrics_compare_against_same_case_baseline(self):
        comparison = RetrievalEvaluator().run()

        self.assertEqual(comparison.case_count, 6)
        self.assertEqual(comparison.k, 3)
        self.assertGreater(
            comparison.hybrid.recall_at_k, comparison.baseline.recall_at_k
        )
        self.assertGreater(comparison.hybrid.mrr, comparison.baseline.mrr)
        self.assertGreater(comparison.hybrid.hit_at_k, comparison.baseline.hit_at_k)
        self.assertEqual(comparison.delta["recall_at_k"], 66.67)
        self.assertTrue(all(case.expected_files for case in comparison.cases))


if __name__ == "__main__":
    unittest.main()
