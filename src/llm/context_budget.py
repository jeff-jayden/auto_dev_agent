"""Conservative token budgets shared by HTTP gateways and LangChain agents."""

import copy
import json
import os
from dataclasses import dataclass


class ContextBudgetExceeded(ValueError):
    pass


def token_upper_bound(value) -> int:
    """UTF-8 bytes upper-bound byte-based tokenizer tokens; no remote tokenizer.

    This intentionally overestimates common code and Chinese text. It is a
    conservative estimate, not the provider's measured token usage.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class ContextBudget:
    window: int = 65536
    output: int = 8192
    safety: int = 1024
    max_input: int = 32768

    @classmethod
    def from_environment(cls):
        budget = cls(*(int(os.getenv(name, default)) for name, default in (
            ("MODEL_CONTEXT_WINDOW_TOKENS", "65536"),
            ("MODEL_OUTPUT_RESERVE_TOKENS", "8192"),
            ("MODEL_CONTEXT_SAFETY_TOKENS", "1024"),
            ("MODEL_MAX_INPUT_TOKENS", "32768"),
        )))
        if min(budget.window, budget.output, budget.max_input) <= 0 or budget.safety < 0 or budget.input_limit <= 0:
            raise ContextBudgetExceeded("Invalid model context budget configuration")
        return budget

    @property
    def input_limit(self):
        return min(self.max_input, self.window - self.output - self.safety)


def fit_messages(messages, *, extra=None, budget=None):
    """Drop optional evidence only; never silently cut requirements or code.

    Keep tool-call/response pairs intact. If essential input cannot fit, stop
    before making the request instead of sacrificing task constraints.
    """
    budget = budget or ContextBudget.from_environment()
    fitted = copy.deepcopy(messages)
    removed = []

    def size():
        return token_upper_bound(fitted) + token_upper_bound(extra or {}) + 32 * len(fitted)

    before = size()
    for message in fitted:
        if size() <= budget.input_limit:
            break
        if message.get("role") != "user":
            continue
        try:
            payload = json.loads(message["content"])
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        context = payload.get("task_context", {})
        for key in ("recent_change_evidence", "development_history"):
            if context.get(key):
                context[key] = []
                removed.append("task_context." + key)
                message["content"] = json.dumps(payload, ensure_ascii=False)
                if size() <= budget.input_limit:
                    break
        # Preview evidence is redundant with read_file; keep requests, tool
        # schemas, allowed_files, current code, and review Diff untouched.
        if size() > budget.input_limit and payload.get("initial_context_preview"):
            payload["initial_context_preview"] = {}
            removed.append("initial_context_preview")
            message["content"] = json.dumps(payload, ensure_ascii=False)
    after = size()
    if after > budget.input_limit:
        raise ContextBudgetExceeded(
            f"Context budget exceeded: conservative input estimate {after} > {budget.input_limit}; "
            "critical requirements/current code were preserved. Narrow the task scope or increase the configured budget."
        )
    return fitted, {"estimator": "utf8_byte_upper_bound", "input_before": before,
                    "input_estimate": after, "input_limit": budget.input_limit,
                    "output_reserved": budget.output, "removed_optional_evidence": removed}


def budget_middleware():
    from langchain.agents.middleware import AgentMiddleware

    class BudgetMiddleware(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            from langchain_core.messages import HumanMessage

            messages = [{"role": "system", "content": request.system_message.content}] if request.system_message else []
            offset = len(messages)
            for message in request.messages:
                messages.append({"role": "user" if message.type == "human" else message.type,
                                 "content": message.content,
                                 "tool_calls": getattr(message, "tool_calls", [])})
            tools = [{"name": tool.name, "description": tool.description,
                      "schema": tool.args_schema.model_json_schema() if tool.args_schema else {}}
                     if hasattr(tool, "args_schema") else tool for tool in (request.tools or [])]
            schema = getattr(request.response_format, "schema", request.response_format)
            if hasattr(schema, "model_json_schema"):
                schema = schema.model_json_schema()
            fitted, _ = fit_messages(messages, extra={"tools": tools, "response_format": schema})
            updated = list(request.messages)
            for index, message in enumerate(updated):
                if message.type == "human" and fitted[index + offset]["content"] != message.content:
                    updated[index] = HumanMessage(content=fitted[index + offset]["content"], id=message.id)
            settings = dict(request.model_settings, max_tokens=ContextBudget.from_environment().output)
            return handler(request.override(messages=updated, model_settings=settings))

    return BudgetMiddleware()
