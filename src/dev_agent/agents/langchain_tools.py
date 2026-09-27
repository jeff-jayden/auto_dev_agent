from __future__ import annotations

import json
from contextlib import nullcontext
from typing import Any, Callable

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, model_validator

from dev_agent.domain.models import ToolCallAudit


class SearchInput(BaseModel):
    query: str = Field(min_length=2, max_length=300, description="要在仓库中查找的文本或符号")
    reason: str = Field(default="", max_length=500, description="为什么当前需要调用这个工具")


class ReadFileInput(BaseModel):
    path: str = Field(min_length=1, max_length=500, description="仓库相对路径")
    start_line: int = Field(default=1, ge=1, le=1_000_000, description="起始行，包含")
    end_line: int = Field(default=240, ge=1, le=1_000_000, description="结束行，包含")
    reason: str = Field(default="", max_length=500, description="为什么当前需要读取这个文件")

    @model_validator(mode="after")
    def validate_line_range(self):
        if self.end_line < self.start_line:
            raise ValueError("end_line must not be less than start_line")
        return self


class GitHistoryInput(BaseModel):
    path: str = Field(min_length=1, max_length=500, description="仓库相对路径")
    reason: str = Field(default="", max_length=500, description="为什么当前需要查看历史")


class DeveloperContextToolRegistry:
    """LangChain tool registry backed by the project's policy-aware toolkit."""

    def __init__(self, toolkit, *, tracer=None, max_calls: int = 8):
        self.toolkit = toolkit
        self.tracer = tracer
        self.max_calls = max(1, max_calls)
        self.calls: list[dict[str, Any]] = []
        self._signatures: set[str] = set()

        @tool("search_text", args_schema=SearchInput)
        def search_text_tool(query: str, reason: str = ""):
            """在仓库可读文件中搜索精确文本；不知道定义位置或页面文案位置时使用。"""
            return self._execute("search_text", {"query": query}, reason, lambda: toolkit.search_text(query))

        @tool("search_symbol", args_schema=SearchInput)
        def search_symbol_tool(query: str, reason: str = ""):
            """按完整标识符搜索符号定义；需要定位函数、类、组件或变量时使用。"""
            return self._execute("search_symbol", {"query": query}, reason, lambda: toolkit.search_symbol(query))

        @tool("find_references", args_schema=SearchInput)
        def find_references_tool(query: str, reason: str = ""):
            """查找符号在仓库中的引用位置；判断调用链和影响范围时使用。"""
            return self._execute("find_references", {"query": query}, reason, lambda: toolkit.find_references(query))

        @tool("read_file", args_schema=ReadFileInput)
        def read_file_tool(path: str, start_line: int = 1, end_line: int = 240, reason: str = ""):
            """读取仓库文件的指定行区间；已经定位文件并需要查看实现细节时使用。"""
            arguments = {"path": path, "start_line": start_line, "end_line": end_line}
            return self._execute(
                "read_file", arguments, reason,
                lambda: toolkit.read_file_slice(path, start_line, end_line),
            )

        @tool("git_history", args_schema=GitHistoryInput)
        def git_history_tool(path: str, reason: str = ""):
            """查看指定文件最近的 Git 提交历史；需要理解代码演进原因时使用。"""
            return self._execute("git_history", {"path": path}, reason, lambda: toolkit.git_history(path))

        tools = [
            search_text_tool,
            search_symbol_tool,
            find_references_tool,
            read_file_tool,
            git_history_tool,
        ]
        self._tools: dict[str, BaseTool] = {item.name: item for item in tools}

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    @property
    def tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def specifications(self) -> list[dict[str, Any]]:
        return [
            {
                "name": item.name,
                "description": item.description,
                "input_schema": item.args_schema.model_json_schema(),
            }
            for item in self._tools.values()
        ]

    def invoke(self, name: str, arguments: dict[str, Any]):
        selected = self._tools.get(name)
        if selected is None:
            raise ValueError(f"Unknown LangChain context tool: {name}")
        return selected.invoke(arguments)

    def _execute(
        self,
        name: str,
        arguments: dict[str, Any],
        reason: str,
        operation: Callable[[], Any],
    ) -> Any:
        signature = json.dumps({"tool": name, **arguments}, ensure_ascii=False, sort_keys=True)
        if len(self.calls) >= self.max_calls:
            self.toolkit.audit.append(ToolCallAudit(
                tool="agent.context_guard",
                arguments={"tool": name, "max_calls": self.max_calls},
                summary="上下文探索已达到工具调用上限",
                success=False,
            ))
            return {"error": "context tool call limit reached", "guarded": True}
        if signature in self._signatures:
            self.toolkit.audit.append(ToolCallAudit(
                tool="agent.context_guard",
                arguments={"tool": name, **arguments},
                summary="阻止重复的上下文工具调用",
                success=False,
            ))
            return {"error": "duplicate context tool call", "guarded": True}

        self._signatures.add(signature)
        call = {"tool": name, "arguments": arguments, "reason": reason}
        self.calls.append(call)
        self.toolkit.audit.append(ToolCallAudit(
            tool="agent.context_tool_call",
            arguments={"tool": name, "framework": "langchain", **arguments},
            summary=reason or f"Developer Agent 自主调用 {name}",
        ))
        scope = self.tracer.span(
            f"tool.{name}", kind="tool",
            attributes={"framework": "langchain", "reason": reason, **arguments},
            input_summary=reason or name,
        ) if self.tracer is not None else nullcontext(None)
        try:
            with scope as span:
                result = operation()
                call["result"] = result
                if span is not None:
                    span.output_summary = self._result_summary(name, arguments, result)
                return result
        except (OSError, ValueError) as error:
            call["error"] = str(error)[:1000]
            self.toolkit.audit.append(ToolCallAudit(
                tool=name,
                arguments=arguments,
                summary=f"执行失败：{str(error)[:500]}",
                success=False,
            ))
            return {"error": str(error)[:1000]}

    @staticmethod
    def _result_summary(name: str, arguments: dict[str, Any], result: Any) -> str:
        if isinstance(result, list):
            return f"{name} 命中 {len(result)} 处"
        if name == "read_file" and isinstance(result, dict):
            return f"读取 {result.get('path', arguments.get('path', ''))}"
        if name == "git_history" and isinstance(result, dict):
            return f"读取 {result.get('path', arguments.get('path', ''))} 的 {len(result.get('entries', []))} 条提交"
        return name
