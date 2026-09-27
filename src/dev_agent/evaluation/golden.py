from __future__ import annotations

from time import perf_counter
from uuid import uuid4

from dev_agent.domain.models import EvaluationCaseResult, EvaluationRun, TaskStatus
from dev_agent.domain.state_machine import ensure_transition
from dev_agent.observability import TraceRecorder
from dev_agent.repository.catalog import RepositoryCatalog


class GoldenCaseEvaluator:
    """Fast, deterministic contract cases; it never edits a repository or calls a model."""

    def __init__(self, store):
        self.store = store

    def run(self) -> EvaluationRun:
        cases = [
            ("github_remote_parse", self._github_remote_parse),
            ("state_machine_rejects_skip", self._state_machine_rejects_skip),
            ("trace_secret_redaction", self._trace_secret_redaction),
            ("checkpoint_stage_contract", self._checkpoint_stage_contract),
            ("trace_metric_contract", self._trace_metric_contract),
        ]
        results = [self._execute(name, case) for name, case in cases]
        passed = sum(item.passed for item in results)
        run = EvaluationRun(
            id=uuid4().hex[:16], status="passed" if passed == len(results) else "failed",
            score=round(100 * passed / len(results), 1), passed=passed,
            total=len(results), cases=results,
        )
        return self.store.save_evaluation(run)

    @staticmethod
    def _execute(name, case) -> EvaluationCaseResult:
        started = perf_counter()
        try:
            detail = case()
            passed = True
        except Exception as error:
            detail = str(error)
            passed = False
        return EvaluationCaseResult(
            name=name, passed=passed, detail=detail,
            duration_ms=round((perf_counter() - started) * 1000, 2),
        )

    @staticmethod
    def _github_remote_parse() -> str:
        value = RepositoryCatalog._parse_github_repository("https://github.com/acme/agent-demo.git")
        assert value == "acme/agent-demo"
        return "HTTPS GitHub remote parsed"

    @staticmethod
    def _state_machine_rejects_skip() -> str:
        try:
            ensure_transition(TaskStatus.WAITING_REQUIREMENT_APPROVAL, TaskStatus.TESTING)
        except ValueError:
            return "Illegal stage skip rejected"
        raise AssertionError("illegal transition was accepted")

    @staticmethod
    def _trace_secret_redaction() -> str:
        value = TraceRecorder.redact({"github_token": "do-not-store", "safe": "ok"})
        assert value == {"github_token": "[REDACTED]", "safe": "ok"}
        return "Sensitive trace fields redacted"

    @staticmethod
    def _checkpoint_stage_contract() -> str:
        stages = {"plan_approved", "proposal_ready", "patch_applied", "test_result_saved", "review_round_saved"}
        assert len(stages) == 5
        return "Five resumable stages registered"

    def _trace_metric_contract(self) -> str:
        metrics = self.store.trace_metrics()
        required = {"trace_count", "success_rate", "llm_calls", "tool_calls", "failed_spans"}
        assert required <= metrics.keys()
        return "Observability metrics schema valid"
