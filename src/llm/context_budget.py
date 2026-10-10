"""HTTP 与 LangChain 共用的保守上下文估算、软预算与窗口余量保护。"""

import copy
import json
import math
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

    @property
    def hard_input_limit(self):
        return self.window - self.output - self.safety

    @property
    def admission_limit(self):
        # 字节估算偏保守：允许软预算小幅浮动，但绝不挤占输出与安全余量。
        return min(math.ceil(self.max_input * 1.1), self.hard_input_limit)


def fit_messages(messages, *, extra=None, budget=None):
    """Compact encoding and optional evidence; never cut requirements or code.

    max_input is a soft target with a bounded 10% estimation margin. The
    configured window minus output/safety reservations is always respected.
    Keep tool-call/response pairs intact; fail only after fitting is exhausted.
    """
    budget = budget or ContextBudget.from_environment()
    fitted = copy.deepcopy(messages)
    removed = []

    def size():
        return token_upper_bound(fitted) + token_upper_bound(extra or {}) + 32 * len(fitted)

    before = size()
    if before > budget.input_limit:
        for message in fitted:
            if message.get("role") != "user":
                continue
            try:
                payload = json.loads(message["content"])
            except (ValueError, TypeError, KeyError):
                continue
            # 只压缩 JSON 编码空白，源码/需求字符串内的空白保持原样。
            compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if token_upper_bound(compact) < token_upper_bound(message["content"]):
                message["content"] = compact
        if size() < before:
            removed.append("json_encoding_whitespace")
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
        for key in ("recent_change_evidence", "development_history", "retrieved_history"):
            if context.get(key):
                context[key] = []
                removed.append("task_context." + key)
                message["content"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                if size() <= budget.input_limit:
                    break
        # Preview evidence is redundant with read_file; keep requests, tool
        # schemas, allowed_files, current code, and review Diff untouched.
        if size() > budget.input_limit and payload.get("initial_context_preview"):
            payload["initial_context_preview"] = {}
            removed.append("initial_context_preview")
            message["content"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    after = size()
    if after > budget.admission_limit:
        raise ContextBudgetExceeded(
            f"Context budget exceeded: 整理后保守输入估算 {after}，软预算 {budget.input_limit}，"
            f"弹性准入上限 {budget.admission_limit}，配置窗口输入上限 {budget.hard_input_limit}。"
            "已保留需求与当前源码，并预留输出空间；请检查上下文构成或模型窗口配置。"
        )
    return fitted, {"estimator": "utf8_byte_upper_bound", "input_before": before,
                    "input_estimate": after, "input_limit": budget.input_limit,
                    "admission_limit": budget.admission_limit, "hard_input_limit": budget.hard_input_limit,
                    "soft_budget_exceeded": after > budget.input_limit,
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
