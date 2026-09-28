"""Agent 可观测性与调用追踪。

记录 Agent、LLM 和工具调用的嵌套 Span、耗时与 Token 指标，同时对敏感字段进行脱敏。
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Iterator, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from domain.models import AgentTrace, TraceSpan
from infrastructure.store import SQLiteTaskStore

T = TypeVar("T", bound=BaseModel)
_trace_id: ContextVar[str | None] = ContextVar("agent_trace_id", default=None)
_task_id: ContextVar[str | None] = ContextVar("agent_task_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("agent_span_id", default=None)


class TraceRecorder:
    def __init__(self, store: SQLiteTaskStore):
        self.store = store

    @staticmethod
    def redact(value: Any) -> Any:
        def is_sensitive_key(key: Any) -> bool:
            normalized = str(key).lower().replace("-", "_")
            exact = {
                "token", "access_token", "refresh_token", "github_token", "auth_token",
                "secret", "client_secret", "password", "authorization", "api_key", "apikey",
            }
            return normalized in exact or normalized.endswith(("_password", "_secret", "_api_key"))

        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]" if is_sensitive_key(key)
                else TraceRecorder.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [TraceRecorder.redact(item) for item in value]
        if isinstance(value, str) and len(value) > 1000:
            return value[:1000] + "…"
        return value

    @contextmanager
    def trace(self, name: str, *, task_id: str | None = None, kind: str = "execution",
              metadata: dict[str, Any] | None = None) -> Iterator[AgentTrace]:
        trace = AgentTrace(
            id=uuid4().hex[:20], task_id=task_id, kind=kind, name=name,
            metadata=self.redact(metadata or {}),
        )
        self.store.save_trace(trace)
        trace_token = _trace_id.set(trace.id)
        task_token = _task_id.set(task_id)
        try:
            yield trace
        except Exception:
            trace.status = "failed"
            raise
        else:
            if trace.status == "running":
                trace.status = "succeeded"
        finally:
            trace.ended_at = datetime.now(UTC)
            trace.duration_ms = round((trace.ended_at - trace.started_at).total_seconds() * 1000, 2)
            self.store.save_trace(trace)
            _task_id.reset(task_token)
            _trace_id.reset(trace_token)

    @contextmanager
    def span(self, name: str, *, kind: str = "workflow",
             attributes: dict[str, Any] | None = None,
             input_summary: str | None = None) -> Iterator[TraceSpan | None]:
        trace_id = _trace_id.get()
        if trace_id is None:
            yield None
            return
        span = TraceSpan(
            id=uuid4().hex[:20], trace_id=trace_id, task_id=_task_id.get(),
            parent_span_id=_span_id.get(), kind=kind, name=name,
            attributes=self.redact(attributes or {}), input_summary=input_summary,
        )
        self.store.save_span(span)
        token = _span_id.set(span.id)
        try:
            yield span
        except Exception as error:
            span.status = "failed"
            span.error = str(error)[:1000]
            raise
        else:
            span.status = "succeeded"
        finally:
            span.ended_at = datetime.now(UTC)
            span.duration_ms = round((span.ended_at - span.started_at).total_seconds() * 1000, 2)
            span.attributes = self.redact(span.attributes)
            self.store.save_span(span)
            _span_id.reset(token)


class TracingModelGateway:
    def __init__(self, delegate, recorder: TraceRecorder):
        self.delegate = delegate
        self.recorder = recorder

    @property
    def enabled(self) -> bool:
        return self.delegate.enabled

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    def generate_structured(self, system_prompt: str, user_prompt: str,
                            output_model: type[T], temperature: float = 0.1) -> T:
        prompt = system_prompt + "\n" + user_prompt
        attrs = {
            "model": getattr(self.delegate, "model", self.delegate.__class__.__name__),
            "output_model": output_model.__name__,
            "prompt_version": "v1",
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
            "prompt_chars": len(prompt),
        }
        with self.recorder.span(
            f"llm.{output_model.__name__}", kind="llm", attributes=attrs,
            input_summary=f"structured prompt ({len(prompt)} chars)",
        ) as span:
            result = self.delegate.generate_structured(system_prompt, user_prompt, output_model)
            if span is not None:
                usage = getattr(self.delegate, "last_usage", {}) or {}
                span.attributes["prompt_tokens"] = int(usage.get("prompt_tokens", len(prompt) // 4))
                serialized = result.model_dump_json() if result is not None else "null"
                span.attributes["completion_tokens"] = int(usage.get("completion_tokens", len(serialized) // 4))
                if output_model.__name__ == "RepositoryToolDecision" and result is not None:
                    decision = result.model_dump()
                    selected_tool = str(decision.get("tool", "unknown"))
                    reason = str(decision.get("reason", ""))[:500]
                    span.attributes["selected_tool"] = selected_tool
                    span.attributes["reason"] = reason
                    arguments = decision.get("arguments", {})
                    if arguments:
                        span.attributes["tool_arguments"] = arguments
                    span.output_summary = f"选择 {selected_tool}：{reason}"
                else:
                    span.output_summary = f"{output_model.__name__} ({len(serialized)} chars)"
            return result
