from __future__ import annotations

import json
import os
from contextlib import nullcontext
from typing import Any

from langchain.agents import create_agent
from pydantic import BaseModel, Field

from dev_agent.domain.models import ToolCallAudit
from .langchain_tools import DeveloperContextToolRegistry


class ContextToolDecision(BaseModel):
    """A model-selected LangChain tool call, or the terminal finish action."""

    tool: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    arguments: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(default="", max_length=1200)


class DeveloperContextExplorer:
    """Bounded observe-decide-act loop for read-only repository exploration.

    Writes, tests, shell commands and workflow routing intentionally remain in
    the deterministic Developer pipeline and its ToolPolicy.
    """

    def __init__(self, model_gateway, tracer=None, max_steps: int | None = None):
        self.model_gateway = model_gateway
        self.tracer = tracer
        configured = int(os.getenv("DEVELOPER_CONTEXT_MAX_STEPS", "4"))
        self.max_steps = max(1, min(max_steps or configured, 8))

    @property
    def enabled(self) -> bool:
        if os.getenv("DEVELOPER_CONTEXT_TOOLS_ENABLED", "true").lower() in {
            "0", "false", "no", "off",
        }:
            return False
        # Existing test doubles and custom gateways retain the legacy one-shot
        # behavior unless they explicitly opt in to the extra model calls.
        return bool(getattr(self.model_gateway, "supports_context_tool_loop", False))

    def explore(
        self,
        *,
        task,
        toolkit,
        initial_context: dict[str, str],
        write_scope: list[str],
        repair_context: str = "",
        review_feedback: str = "",
    ) -> dict[str, str]:
        if not self.enabled:
            return initial_context

        context = dict(initial_context)
        registry = DeveloperContextToolRegistry(
            toolkit, tracer=self.tracer, max_calls=self.max_steps,
        )
        if callable(getattr(self.model_gateway, "build_langchain_model", None)):
            try:
                return self._explore_with_create_agent(
                    task=task,
                    toolkit=toolkit,
                    registry=registry,
                    context=context,
                    write_scope=write_scope,
                    repair_context=repair_context,
                    review_feedback=review_feedback,
                )
            except Exception as error:
                toolkit.audit.append(ToolCallAudit(
                    tool="agent.langchain_fallback",
                    arguments={"framework": "langchain.create_agent"},
                    summary=f"create_agent 不可用，切回兼容决策循环：{str(error)[:500]}",
                    success=False,
                ))
                registry = DeveloperContextToolRegistry(
                    toolkit, tracer=self.tracer, max_calls=self.max_steps,
                )

        observations: list[dict] = []
        previous_actions: set[str] = set()
        available_files = toolkit.list_readable_files(limit=300)
        initial_files = list(context)
        initial_preview = {
            path: content[:1600] for path, content in list(context.items())[:10]
        }
        system_prompt = (
            "你是 Developer Agent 的只读上下文探索器。目标是在生成代码修改前，自主决定是否需要更多仓库证据。"
            "每轮只能选择一个工具，是否调用工具完全由你决定。优先使用已有上下文；"
            "只有存在明确的信息缺口时才搜索或读取，上下文充分时第一轮就可以 finish。"
            "不得请求写文件、执行测试、运行 shell、扩大 allowed_files 或发布代码。"
            "可用工具由 payload.tools 提供；tool 必须是其中一个 name，arguments 必须严格符合该工具的 input_schema。"
            "信息足够时使用 tool=finish 且 arguments={}。所有动作必须说明 reason，输出严格 JSON。"
        )

        for step in range(1, self.max_steps + 1):
            payload = {
                "goal": {"title": task.title, "requirement": task.requirement},
                "technical_plan": {
                    "approach": task.technical_plan.approach,
                    "allowed_files": write_scope,
                },
                "already_loaded_files": list(context),
                "initial_context_files": initial_files,
                "initial_context_preview": initial_preview,
                "repository_files": available_files,
                "recent_observations": observations[-6:],
                "previous_failure": repair_context[-4000:],
                "review_feedback": review_feedback[-3000:],
                "step": step,
                "remaining_steps": self.max_steps - step + 1,
                "tools": registry.specifications(),
                "output_schema": ContextToolDecision.model_json_schema(),
            }
            decision = self.model_gateway.generate_structured(
                system_prompt,
                json.dumps(payload, ensure_ascii=False),
                ContextToolDecision,
            )
            if not isinstance(decision, ContextToolDecision):
                toolkit.audit.append(ToolCallAudit(
                    tool="agent.context_decision",
                    summary="上下文探索器未返回有效动作，沿用已有上下文",
                    success=False,
                ))
                break

            signature = self._action_signature(decision)
            decision_arguments = self._tool_arguments(decision)
            decision_arguments.update({
                "step": step,
                "tool": decision.tool,
                "framework": "langchain",
            })
            toolkit.audit.append(ToolCallAudit(
                tool="agent.context_decision",
                arguments=decision_arguments,
                summary=decision.reason,
            ))
            if decision.tool == "finish":
                toolkit.audit.append(ToolCallAudit(
                    tool="agent.context_complete",
                    arguments={"step": step, "loaded_files": list(context)},
                    summary=decision.summary or decision.reason,
                ))
                break
            if signature in previous_actions:
                toolkit.audit.append(ToolCallAudit(
                    tool="agent.context_guard",
                    arguments={"step": step, "tool": decision.tool},
                    summary="阻止重复的上下文工具调用",
                    success=False,
                ))
                break
            previous_actions.add(signature)

            try:
                result = registry.invoke(
                    decision.tool,
                    {**decision.arguments, "reason": decision.reason},
                )
            except (OSError, ValueError) as error:
                result = {"error": str(error)[:1000]}
                toolkit.audit.append(ToolCallAudit(
                    tool=decision.tool,
                    arguments=self._tool_arguments(decision),
                    summary=f"执行失败：{str(error)[:500]}",
                    success=False,
                ))
            observations.append({
                "tool": decision.tool,
                "reason": decision.reason,
                "result": result,
            })
            if decision.tool == "read_file" and isinstance(result, dict):
                path = str(result.get("path", ""))
                content = result.get("content")
                if path and isinstance(content, str) and content:
                    # Evidence can expand read context, but never write_scope.
                    context[path] = content

        return context

    def _explore_with_create_agent(
        self,
        *,
        task,
        toolkit,
        registry: DeveloperContextToolRegistry,
        context: dict[str, str],
        write_scope: list[str],
        repair_context: str,
        review_feedback: str,
    ) -> dict[str, str]:
        model = self.model_gateway.build_langchain_model()
        system_prompt = (
            "你是 Developer Agent 的只读仓库探索子代理。你要自行判断信息是否足够，并自主选择零到多个工具。"
            "仅在存在明确证据缺口时调用工具；优先搜索定位，再读取必要片段，避免重复调用。"
            f"最多执行 {self.max_steps} 次工具调用。每次调用必须在 reason 参数中写明目的。"
            "禁止写文件、执行测试、运行 shell、扩大可修改文件或发布代码。"
            "信息充分后直接返回简短中文总结，不要输出代码修改方案。"
        )
        payload = {
            "goal": {"title": task.title, "requirement": task.requirement},
            "technical_plan": {
                "approach": task.technical_plan.approach,
                "allowed_files": write_scope,
            },
            "already_loaded_files": list(context),
            "initial_context_preview": {
                path: content[:1600] for path, content in list(context.items())[:10]
            },
            "repository_files": toolkit.list_readable_files(limit=300),
            "previous_failure": repair_context[-4000:],
            "review_feedback": review_feedback[-3000:],
        }
        agent = create_agent(
            model=model,
            tools=registry.tools,
            system_prompt=system_prompt,
        )
        scope = self.tracer.span(
            "agent.langchain_create_agent",
            kind="agent",
            attributes={
                "framework": "langchain.create_agent",
                "model": getattr(self.model_gateway, "model", model.__class__.__name__),
                "tools": registry.names,
                "max_tool_calls": self.max_steps,
            },
            input_summary=f"自主探索 {len(payload['repository_files'])} 个仓库文件",
        ) if self.tracer is not None else nullcontext(None)
        with scope as span:
            result = agent.invoke(
                {"messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]},
                config={"recursion_limit": self.max_steps * 2 + 4},
            )
            messages = result.get("messages", []) if isinstance(result, dict) else []
            summary = self._last_message_content(messages) or "自主上下文探索完成"
            usage = self._message_usage(messages)
            if span is not None:
                span.attributes.update(usage)
                span.attributes["tool_calls"] = len(registry.calls)
                span.output_summary = summary[:1000]

        for call in registry.calls:
            result_value = call.get("result")
            if call["tool"] == "read_file" and isinstance(result_value, dict):
                path = str(result_value.get("path", ""))
                content = result_value.get("content")
                if path and isinstance(content, str) and content:
                    context[path] = content

        toolkit.audit.append(ToolCallAudit(
            tool="agent.context_complete",
            arguments={
                "framework": "langchain.create_agent",
                "tool_calls": len(registry.calls),
                "loaded_files": list(context),
            },
            summary=summary[:1200],
        ))
        return context

    @staticmethod
    def _last_message_content(messages: list[Any]) -> str:
        for message in reversed(messages):
            content = getattr(message, "content", None)
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"].strip()
        return ""

    @staticmethod
    def _message_usage(messages: list[Any]) -> dict[str, int]:
        totals = {"prompt_tokens": 0, "completion_tokens": 0}
        for message in messages:
            usage = getattr(message, "usage_metadata", None) or {}
            totals["prompt_tokens"] += int(usage.get("input_tokens", 0) or 0)
            totals["completion_tokens"] += int(usage.get("output_tokens", 0) or 0)
        return totals

    @staticmethod
    def _tool_arguments(decision: ContextToolDecision) -> dict:
        return dict(decision.arguments)

    @classmethod
    def _action_signature(cls, decision: ContextToolDecision) -> str:
        return json.dumps(
            {"tool": decision.tool, **cls._tool_arguments(decision)},
            ensure_ascii=False,
            sort_keys=True,
        )

