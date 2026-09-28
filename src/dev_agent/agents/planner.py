import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain.tools import tool
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from dev_agent.domain.models import (
    DevelopmentStep,
    RepositoryAnalysis,
    RequirementAnalysis,
    TechnicalPlan,
    ToolCallAudit,
)
from dev_agent.llm.gateway import DisabledModelGateway, ModelGateway


class PlanningResponse(BaseModel):
    analysis: RequirementAnalysis
    plan: TechnicalPlan


class LocalPlanningAgent:
    """Planning agent with a deterministic fallback when no model is configured."""

    def __init__(self, model_gateway: ModelGateway | None = None, tracer=None):
        self.model_gateway = model_gateway or DisabledModelGateway()
        self.tracer = tracer

    def plan(
        self,
        title: str,
        requirement: str,
        repository: RepositoryAnalysis,
        design_context: str = "",
    ) -> tuple[RequirementAnalysis, TechnicalPlan]:
        if self.model_gateway.enabled:
            response = self._request_model_plan(
                title, requirement, repository, design_context,
            )
            if isinstance(response, PlanningResponse):
                context_paths = {
                    item.path for item in repository.context_pack.files
                } if repository.context_pack else set()
                known = set(repository.important_files) | {item.path for item in repository.evidence} | context_paths
                if all(path in known for path in response.plan.affected_files):
                    if repository.framework == "react":
                        test_files = [
                            path for path in repository.important_files
                            if Path(path).name in {"App.test.js", "App.test.jsx", "App.test.tsx"}
                        ]
                        response.plan.affected_files = list(dict.fromkeys([
                            *response.plan.affected_files, *test_files,
                        ]))
                    self.ensure_dependency_scope(response.plan, repository, requirement)
                    self.ensure_development_steps(response.plan, repository, generate_when_missing=False)
                    return response.analysis, response.plan
        return self.analyze(title, requirement), self.design(repository, requirement)

    def _request_model_plan(
        self,
        title: str,
        requirement: str,
        repository: RepositoryAnalysis,
        design_context: str,
    ) -> PlanningResponse | None:
        system_prompt = (
            "你是资深软件架构师组成的 Plan Agent。只能根据已验证的仓库事实生成需求分析和技术方案，"
            "不得虚构文件。你可以自主决定是否调用只读规划工具补充仓库概览、候选文件和代码证据。"
            "affected_files 只能使用工具或输入中出现的真实路径。用户可见行为变更必须把仓库中已有的"
            "对应测试文件加入 affected_files 和 test_plan。development_steps 必须提供 2 到 4 个可串行"
            "执行的步骤；每步 allowed_files 只能取自 affected_files，并写清 objective、"
            "acceptance_checks 和前置 depends_on。信息充分时直接返回最终结构化方案。"
        )
        payload = {
            "title": title,
            "requirement": requirement,
            "verified_repository_summary": {
                "language": repository.language,
                "framework": repository.framework,
                "head_sha": repository.head_sha,
                "test_command": repository.test_command,
                "entrypoints": repository.entrypoints,
                "important_files": repository.important_files,
                "context_pack": (
                    repository.context_pack.model_dump(mode="json")
                    if repository.context_pack else None
                ),
            },
            "figma_design_context": design_context[:30000],
        }
        if callable(getattr(self.model_gateway, "build_langchain_model", None)):
            registry = PlanningToolRegistry(repository)
            try:
                model = self.model_gateway.build_langchain_model()
                agent = create_agent(
                    model=model,
                    tools=registry.tools,
                    system_prompt=system_prompt,
                    response_format=ToolStrategy(PlanningResponse, handle_errors=True),
                )
                scope = self.tracer.span(
                    "agent.langchain_plan_agent",
                    kind="agent",
                    attributes={
                        "framework": "langchain.create_agent",
                        "model": getattr(self.model_gateway, "model", model.__class__.__name__),
                        "tools": registry.names,
                    },
                    input_summary=f"为 {title} 生成需求分析和技术方案",
                ) if self.tracer is not None else nullcontext(None)
                with scope as span:
                    result = agent.invoke(
                        {"messages": [{
                            "role": "user",
                            "content": json.dumps(payload, ensure_ascii=False),
                        }]},
                        config={"recursion_limit": 12},
                    )
                    response = result.get("structured_response") if isinstance(result, dict) else None
                    if span is not None:
                        span.attributes["tool_calls"] = len(registry.calls)
                        span.output_summary = (
                            f"PlanningResponse，影响 {len(response.plan.affected_files)} 个文件"
                            if isinstance(response, PlanningResponse)
                            else "未返回有效 PlanningResponse"
                        )
                repository.tool_calls.append(ToolCallAudit(
                    tool="planner.create_agent",
                    arguments={
                        "framework": "langchain.create_agent",
                        "tool_calls": len(registry.calls),
                    },
                    summary="Plan Agent 已完成需求分析和技术方案生成",
                    success=isinstance(response, PlanningResponse),
                ))
                return response if isinstance(response, PlanningResponse) else None
            except Exception as error:
                repository.tool_calls.append(ToolCallAudit(
                    tool="planner.langchain_fallback",
                    arguments={"framework": "langchain.create_agent"},
                    summary=f"create_agent 不可用，切回兼容规划调用：{str(error)[:500]}",
                    success=False,
                ))

        return self.model_gateway.generate_structured(
            system_prompt + "输出严格 JSON。",
            json.dumps({
                **payload,
                "repository": repository.model_dump(mode="json"),
                "output_schema": PlanningResponse.model_json_schema(),
            }, ensure_ascii=False),
            PlanningResponse,
        )

    def analyze(self, title: str, requirement: str) -> RequirementAnalysis:
        base_requirement, _, feedback = requirement.partition("用户补充意见：")
        normalized = base_requirement.strip().rstrip("。")
        is_priority_feature = any(token in requirement.lower() for token in ("priority", "优先级", "low、medium、high"))
        if is_priority_feature:
            criteria = [
                "创建任务时可以传入 priority 字段",
                "priority 仅允许 low、medium、high 三个值",
                "未传 priority 时默认使用 medium",
                "非法 priority 会得到明确的 ValueError",
                "现有创建任务行为保持兼容",
            ]
            assumptions = ["优先级暂时使用字符串枚举，不引入数据库迁移"]
        else:
            criteria = [
                normalized,
                "正常路径的输入、输出和权限行为符合需求描述",
                "非法输入或依赖失败时返回明确且可诊断的错误",
                "新增行为有自动化测试，且现有测试保持通过",
            ]
            assumptions = ["方案仅依据当前仓库快照和需求文本生成，外部系统契约需要单独确认"]
        if feedback.strip():
            criteria.append(f"用户补充要求：{feedback.strip()}")
        return RequirementAnalysis(
            summary=f"为目标仓库实现：{title}",
            user_story=f"作为该系统的用户，我希望{normalized}，从而完成对应业务目标。",
            acceptance_criteria=criteria,
            assumptions=["本阶段只读分析真实仓库，不会直接修改工作区", *assumptions],
            open_questions=[],
        )

    def design(self, repository: RepositoryAnalysis | None = None, requirement: str = "") -> TechnicalPlan:
        if repository is not None:
            evidence_paths = list(dict.fromkeys(item.path for item in repository.evidence[:6]))
            context = repository.context_pack
            if repository.framework == "react":
                known = list(dict.fromkeys([
                    *(context.primary_files if context else []),
                    *(context.dependency_files if context else []),
                    *(context.test_files if context else []),
                    *evidence_paths,
                    *repository.important_files,
                ]))
                app_files = [
                    path for path in known
                    if Path(path).name in {"App.js", "App.jsx", "App.tsx"}
                ]
                test_files = [
                    path for path in known
                    if Path(path).name in {"App.test.js", "App.test.jsx", "App.test.tsx"}
                ]
                style_files = [path for path in known if Path(path).name == "App.css"]
                affected = list(dict.fromkeys([
                    *(context.primary_files if context else []),
                    *app_files,
                    *test_files,
                    *style_files,
                ]))
            else:
                affected = list(dict.fromkeys([
                    *(context.primary_files if context else []),
                    *(context.test_files if context else []),
                ])) or evidence_paths or repository.important_files[:4]
            plan = TechnicalPlan(
                approach=f"先沿现有 {repository.framework or repository.language} 结构定位需求入口，在最小影响范围内实现并使用仓库现有测试命令验证。",
                affected_files=affected,
                implementation_steps=[
                    "根据代码证据确认入口、数据模型和调用链",
                    "在现有抽象内完成最小代码变更",
                    "补充覆盖正常、默认和异常路径的测试",
                    f"执行 {repository.test_command or '仓库测试命令'} 并检查回归",
                ],
                test_plan=[
                    repository.test_command or "补充并执行仓库对应语言的自动化测试",
                    "验证原有行为保持兼容",
                    "验证需求描述中的异常输入",
                ],
                risks=[
                    "当前方案基于静态只读分析，动态调用关系需要在开发阶段再次验证",
                    "若需求涉及未纳入仓库的外部服务，需要补充接口契约",
                ],
                evidence=repository.evidence,
            )
            self.ensure_dependency_scope(plan, repository, requirement)
            self.ensure_development_steps(
                plan, repository, generate_when_missing=not self.model_gateway.enabled
            )
            return plan
        plan = TechnicalPlan(
            approach="在任务创建入口增加带默认值的 priority 参数，并在写入任务前执行白名单校验。",
            affected_files=["task_service.py", "tests/test_task_service.py"],
            implementation_steps=[
                "定义允许的优先级集合",
                "扩展 create_task 函数签名并校验输入",
                "将 priority 写入返回的任务对象",
                "增加默认值、合法值和非法值测试",
            ],
            test_plan=[
                "执行 python -m unittest discover -s tests -v",
                "验证旧调用不传 priority 时仍然成功",
                "验证 high 优先级被正确保存",
                "验证未知优先级被拒绝",
            ],
            risks=[
                "调用方可能依赖任务对象的精确字段集合",
                "未来若优先级来自配置，需要将白名单移出代码",
            ],
        )
        self.ensure_development_steps(
            plan, None, generate_when_missing=not self.model_gateway.enabled
        )
        return plan

    @staticmethod
    def ensure_development_steps(
        plan: TechnicalPlan,
        repository: RepositoryAnalysis | None,
        generate_when_missing: bool = True,
    ) -> list[DevelopmentStep]:
        """Normalize model output into small, ordered and enforceable write scopes."""
        approved = list(dict.fromkeys(plan.affected_files))
        approved_set = set(approved)
        normalized: list[DevelopmentStep] = []
        assigned: set[str] = set()

        for raw in plan.development_steps[:5]:
            files = [path for path in dict.fromkeys(raw.allowed_files)
                     if path in approved_set and path not in assigned]
            if not files:
                continue
            step_id = f"step-{len(normalized) + 1}"
            normalized.append(DevelopmentStep(
                id=step_id,
                title=raw.title.strip() or f"开发步骤 {len(normalized) + 1}",
                objective=raw.objective.strip() or plan.approach,
                allowed_files=files,
                acceptance_checks=list(dict.fromkeys(raw.acceptance_checks or plan.test_plan[:1])),
                depends_on=[normalized[-1].id] if normalized else [],
            ))
            assigned.update(files)

        if not plan.development_steps and not generate_when_missing:
            plan.development_steps = []
            return []

        remaining = [path for path in approved if path not in assigned]
        if remaining:
            groups: list[tuple[str, list[str]]] = []
            tests = [path for path in remaining if LocalPlanningAgent._is_test_file(path)]
            styles = [path for path in remaining if Path(path).suffix.lower() in {".css", ".scss", ".sass", ".less"}]
            sources = [path for path in remaining if path not in set(tests + styles)]
            if sources:
                groups.append(("实现核心功能", sources))
            if styles:
                groups.append(("完善界面样式", styles))
            if tests:
                groups.append(("补充并验证自动化测试", tests))
            for title, files in groups:
                step_id = f"step-{len(normalized) + 1}"
                normalized.append(DevelopmentStep(
                    id=step_id,
                    title=title,
                    objective=(
                        f"围绕“{plan.approach}”完成本步骤，仅修改列出的文件；"
                        "保持此前步骤的有效改动和既有行为。"
                    ),
                    allowed_files=files,
                    acceptance_checks=list(dict.fromkeys(plan.test_plan)),
                    depends_on=[normalized[-1].id] if normalized else [],
                ))

        if not normalized and approved:
            normalized = [DevelopmentStep(
                id="step-1",
                title="完成批准方案",
                objective=plan.approach,
                allowed_files=approved,
                acceptance_checks=list(dict.fromkeys(plan.test_plan)),
            )]
        plan.development_steps = normalized
        return normalized

    @staticmethod
    def _is_test_file(path: str) -> bool:
        name = Path(path).name.lower()
        parts = {part.lower() for part in Path(path).parts}
        return (
            "tests" in parts or "test" in parts or "__tests__" in parts
            or ".test." in name or ".spec." in name or name.startswith("test_")
            or name in {"setuptests.js", "setuptests.ts"}
        )

    @staticmethod
    def ensure_dependency_scope(
        plan: TechnicalPlan,
        repository: RepositoryAnalysis,
        requirement: str,
    ) -> list[str]:
        """Include callers/entrypoints for requirements that change code wiring."""
        combined = " ".join([
            requirement,
            plan.approach,
            *plan.implementation_steps,
        ]).lower()
        wiring_terms = (
            "导入", "引用", "调用", "使用方式", "接入", "注册", "路由", "挂载",
            "入口", "调用链", "恢复使用", "恢复原来", "连接",
            "import", "reference", "invoke", "call", "wire", "route", "register",
            "mount", "entrypoint", "integrate", "restore", "dependency",
        )
        if not any(term in combined for term in wiring_terms):
            return []
        contextual_dependencies = (
            repository.context_pack.dependency_files
            if repository.context_pack else []
        )
        added = [path for path in [*repository.entrypoints, *contextual_dependencies]
                 if path not in plan.affected_files]
        if added:
            plan.affected_files = list(dict.fromkeys([*plan.affected_files, *added]))
            plan.implementation_steps.insert(
                0,
                "检查并更新入口或调用方，确保目标实现仍通过原有调用链生效",
            )
            plan.risks.append("接线类变更可能绕过既有实现，需在 CR 中核对入口和调用链")
        return added

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
