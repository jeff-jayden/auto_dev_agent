from __future__ import annotations

from typing import Any

from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from dev_agent.domain.models import RepositoryAnalysis, ToolCallAudit


class PlanningReasonInput(BaseModel):
    reason: str = Field(default="", max_length=500, description="为什么需要查询这项仓库事实")


class ContextFilesInput(PlanningReasonInput):
    role: str = Field(
        default="all",
        description="文件角色：all、primary、dependency、test、entrypoint 或 important",
    )


class EvidenceInput(PlanningReasonInput):
    path: str = Field(default="", max_length=500, description="可选的仓库相对路径；为空时返回全部证据")


class PlanningToolRegistry:
    """Read-only LangChain tools over the verified repository analysis."""

    def __init__(self, repository: RepositoryAnalysis):
        self.repository = repository
        self.calls: list[dict[str, Any]] = []

        @tool("get_repository_overview", args_schema=PlanningReasonInput)
        def get_repository_overview(reason: str = ""):
            """查看技术栈、分支、HEAD、测试命令与入口等仓库概览。"""
            return self._record("get_repository_overview", {}, reason, {
                "language": repository.language,
                "framework": repository.framework,
                "default_branch": repository.default_branch,
                "head_sha": repository.head_sha,
                "file_count": repository.file_count,
                "entrypoints": repository.entrypoints,
                "test_directories": repository.test_directories,
                "test_command": repository.test_command,
            })

        @tool("list_planning_files", args_schema=ContextFilesInput)
        def list_planning_files(role: str = "all", reason: str = ""):
            """按角色列出经过仓库分析确认的候选文件；制定 affected_files 前使用。"""
            context = repository.context_pack
            groups = {
                "primary": list(context.primary_files if context else []),
                "dependency": list(context.dependency_files if context else []),
                "test": list(context.test_files if context else []),
                "entrypoint": list(repository.entrypoints),
                "important": list(repository.important_files),
            }
            normalized = role.strip().lower() or "all"
            result: Any
            if normalized == "all":
                result = groups
            elif normalized in groups:
                result = groups[normalized]
            else:
                result = {"error": f"unknown role: {role}", "allowed_roles": ["all", *groups]}
            return self._record("list_planning_files", {"role": normalized}, reason, result)

        @tool("get_code_evidence", args_schema=EvidenceInput)
        def get_code_evidence(path: str = "", reason: str = ""):
            """查看仓库分析采集的代码行证据；需要确认文件职责或需求命中位置时使用。"""
            normalized = path.replace("\\", "/").strip()
            evidence = [
                item.model_dump(mode="json") for item in repository.evidence
                if not normalized or item.path == normalized
            ][:30]
            return self._record("get_code_evidence", {"path": normalized}, reason, evidence)

        tools = [get_repository_overview, list_planning_files, get_code_evidence]
        self._tools = {item.name: item for item in tools}

    @property
    def tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def _record(self, name: str, arguments: dict[str, Any], reason: str, result: Any) -> Any:
        self.calls.append({
            "tool": name,
            "arguments": arguments,
            "reason": reason,
            "result": result,
        })
        self.repository.tool_calls.append(ToolCallAudit(
            tool=f"planner.{name}",
            arguments={"framework": "langchain.create_agent", **arguments},
            summary=reason or f"Plan Agent 自主调用 {name}",
        ))
        return result
