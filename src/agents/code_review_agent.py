from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from domain.models import ReviewFinding, ReviewerModelOutput, ReviewRound, Task
from llm import ModelGateway


class ReviewLoopState(TypedDict, total=False):
    route: str
    review: object
    feedback: str


class CodeReviewAgent:
    """Independent reviewer combining deterministic gates with bounded model feedback."""

    BLOCKING_SEVERITIES = {"high", "critical"}
    CORE_BLOCKING_CATEGORIES = {"acceptance", "functionality", "ui/ux", "user authentication"}

    def __init__(self, model_gateway: ModelGateway):
        self.model_gateway = model_gateway

    def review(self, task: Task, round_number: int) -> ReviewRound:
        if task.result is None or task.merge_request is None:
            raise ValueError("MR draft and execution result are required before review")

        changed_lines = self._changed_lines(task.result.diff)
        findings, checks = self._deterministic_findings(task, changed_lines)
        findings.extend(self._model_findings(task, changed_lines))

        normalized: list[ReviewFinding] = []
        seen: set[tuple] = set()
        for finding in findings:
            finding.severity = finding.severity.lower()
            category = finding.category.lower().strip()
            finding.blocking = (
                finding.blocking
                or finding.severity in self.BLOCKING_SEVERITIES
                or (finding.severity == "medium" and category in self.CORE_BLOCKING_CATEGORIES)
            )
            key = (finding.file, finding.line, finding.category, finding.message)
            if key not in seen:
                seen.add(key)
                normalized.append(finding)

        blocking = [item for item in normalized if item.blocking]
        decision = "changes_requested" if blocking else "approved"
        summary = (
            f"发现 {len(blocking)} 个阻塞问题，需要修复后重新审查。"
            if blocking
            else f"确定性规则与独立 Reviewer 均未发现阻塞问题；记录 {len(normalized)} 条审查意见。"
        )
        return ReviewRound(
            round=round_number,
            decision=decision,
            summary=summary,
            findings=normalized,
            deterministic_checks=checks,
        )

    def run_loop(
        self,
        task: Task,
        review_node: Callable[[ReviewLoopState], dict],
        repair_node: Callable[[ReviewLoopState], dict],
    ) -> Task:
        """Run the bounded review/repair subgraph for this reviewer."""
        graph = StateGraph(ReviewLoopState)
        graph.add_node("review", review_node)
        graph.add_node("repair", repair_node)
        graph.add_edge(START, "review")
        graph.add_conditional_edges(
            "review",
            lambda state: state["route"],
            {"repair": "repair", "done": END},
        )
        graph.add_conditional_edges(
            "repair",
            lambda state: state["route"],
            {"review": "review", "done": END},
        )
        graph.compile(name="code-review-repair-loop").invoke({})
        return task

    def _deterministic_findings(
        self, task: Task, changed_lines: dict[str, list[tuple[int, str]]]
    ) -> tuple[list[ReviewFinding], list[str]]:
        findings: list[ReviewFinding] = []
        changed_files = set(changed_lines)
        allowed = set(task.technical_plan.affected_files if task.technical_plan else [])
        outside = sorted(changed_files - allowed)
        for path in outside:
            findings.append(ReviewFinding(
                severity="critical", category="scope", file=path,
                message="文件不在已批准技术方案范围内。",
                suggestion="移除该修改，或重新走技术方案审批。", blocking=True,
            ))

        sensitive_names = {".env", "id_rsa", "credentials.json", "secrets.yml"}
        debug_patterns = ("console.log(", "debugger;", "pdb.set_trace(", "breakpoint()")
        secret_pattern = re.compile(
            r"(AKIA[0-9A-Z]{16}|BEGIN (?:RSA |OPENSSH )?PRIVATE KEY|api[_-]?key\s*[:=])", re.I
        )
        for path, lines in changed_lines.items():
            source_text = ""
            if task.workspace:
                source_path = (Path(task.workspace) / path).resolve()
                workspace = Path(task.workspace).resolve()
                if workspace in source_path.parents and source_path.is_file():
                    source_text = source_path.read_text(encoding="utf-8", errors="replace")
            if any(part.lower() in sensitive_names for part in path.replace("\\", "/").split("/")):
                findings.append(ReviewFinding(
                    severity="critical", category="security", file=path,
                    message="Diff 包含敏感配置文件。",
                    suggestion="移除敏感文件并轮换可能暴露的凭据。", blocking=True,
                ))
            if path.lower().endswith((".css", ".scss")) and source_text:
                findings.extend(self._css_structure_findings(path, source_text))
            if Path(path).name in {"index.js", "index.jsx", "index.tsx"} and source_text:
                imports_app = bool(re.search(r"import\s+App\s+from\s+['\"]", source_text))
                renders_app = bool(re.search(r"<App(?:\s|/|>)", source_text))
                if imports_app and not renders_app:
                    findings.append(ReviewFinding(
                        severity="high", category="acceptance", file=path,
                        message="应用入口仍导入 App，但已经不再渲染 App 组件。",
                        suggestion="保持入口渲染 <App />，在 App 组件内完成首页需求并由测试覆盖。",
                        blocking=True,
                    ))
            for line_number, source in lines:
                if secret_pattern.search(source):
                    findings.append(ReviewFinding(
                        severity="critical", category="security", file=path, line=line_number,
                        message="新增代码疑似包含密钥或私钥。",
                        suggestion="改用安全的配置或密钥管理服务。", blocking=True,
                    ))
                if any(pattern in source for pattern in debug_patterns):
                    findings.append(ReviewFinding(
                        severity="high", category="quality", file=path, line=line_number,
                        message="新增代码包含调试语句。",
                        suggestion="合入前移除调试代码。", blocking=True,
                    ))
                if re.search(r"</\s+\w", source):
                    findings.append(ReviewFinding(
                        severity="high", category="syntax", file=path, line=line_number,
                        message="JSX 闭合标签包含非法空白。",
                        suggestion="将闭合标签恢复为标准形式，例如 </p>。", blocking=True,
                    ))
                handler = re.search(r"onClick=\{\(\)\s*=>\s*([A-Za-z_$][\w$]*)\s*\(", source)
                if handler and source_text:
                    name = handler.group(1)
                    declaration = re.search(
                        rf"(?:function\s+{re.escape(name)}\b|(?:const|let|var)\s+{re.escape(name)}\s*=)",
                        source_text,
                    )
                    hook_setter = re.search(
                        rf"(?:const|let|var)\s*\[\s*[A-Za-z_$][\w$]*\s*,\s*"
                        rf"{re.escape(name)}\s*\]\s*=\s*(?:React\.)?useState\s*\(",
                        source_text,
                    )
                    if declaration is None and hook_setter is None:
                        findings.append(ReviewFinding(
                            severity="high", category="runtime", file=path, line=line_number,
                            message=f"点击事件引用了未定义的处理器 {name}。",
                            suggestion="定义该处理器，或将完整点击逻辑直接放入 onClick。", blocking=True,
                        ))

        normalized_requirement = task.requirement.lower()
        if (
            task.repository_analysis
            and task.repository_analysis.framework == "react"
            and "按钮" in task.requirement
            and any(token in normalized_requirement for token in ("弹窗", "提示", "alert"))
        ):
            changed_source = "\n".join(
                (Path(task.workspace) / path).read_text(encoding="utf-8", errors="replace")
                for path in changed_files
                if task.workspace and (Path(task.workspace) / path).is_file()
            )
            has_popup = "alert(" in changed_source or 'role="dialog"' in changed_source or "aria-modal" in changed_source
            if "<button" not in changed_source or not has_popup:
                findings.append(ReviewFinding(
                    severity="high", category="acceptance",
                    message="React 变更没有形成可执行的“按钮点击后显示提示”链路。",
                    suggestion="加入 button，并在 onClick 中直接调用 window.alert，或实现可识别的 dialog。",
                    blocking=True,
                ))

        total_added = sum(len(lines) for lines in changed_lines.values())
        if total_added > 800:
            findings.append(ReviewFinding(
                severity="high", category="maintainability",
                message=f"单次变更新增 {total_added} 行，超过 800 行审查阈值。",
                suggestion="拆分为更小、可独立验证的 MR。", blocking=True,
            ))
        test_changed = any(re.search(r"(^|/)(test|tests|spec)|\.test\.", path, re.I) for path in changed_files)
        if not test_changed:
            react_ui_change = bool(
                task.repository_analysis
                and task.repository_analysis.framework == "react"
                and any(path.lower().endswith((".js", ".jsx", ".ts", ".tsx", ".css", ".scss")) for path in changed_files)
            )
            findings.append(ReviewFinding(
                severity="high" if react_ui_change else "low", category="acceptance" if react_ui_change else "test",
                message=("React 用户可见行为发生变化，但本次 Diff 未修改测试文件。" if react_ui_change else "本次 Diff 未修改测试文件。"),
                suggestion="补充覆盖核心验收标准的交互测试。" if react_ui_change else "确认现有测试已覆盖新增行为，必要时补充交互或边界测试。",
                blocking=react_ui_change,
            ))
        checks = [
            "修改文件均在批准范围内" if not outside else "发现方案外文件",
            "未发现疑似密钥" if not any(item.category == "security" for item in findings) else "发现安全问题",
            "未发现调试语句" if not any(item.category == "quality" for item in findings) else "发现调试语句",
            f"测试命令退出码为 {task.result.exit_code}",
            f"Diff 新增行数为 {total_added}",
        ]
        return findings, checks

    @staticmethod
    def _css_structure_findings(path: str, source: str) -> list[ReviewFinding]:
        masked = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        masked = re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", '""', masked)
        depth = 0
        findings: list[ReviewFinding] = []
        for line_number, raw in enumerate(masked.splitlines(), 1):
            line = raw.strip()
            if depth == 0 and re.match(r"^(?:--)?[-A-Za-z_][\w-]*\s*:", line):
                findings.append(ReviewFinding(
                    severity="high", category="syntax", file=path, line=line_number,
                    message="CSS 属性位于规则块之外，浏览器会忽略或错误解析该段样式。",
                    suggestion="删除重复声明，或把属性放回完整的选择器花括号内。", blocking=True,
                ))
                break
            depth += raw.count("{") - raw.count("}")
            if depth < 0:
                findings.append(ReviewFinding(
                    severity="high", category="syntax", file=path, line=line_number,
                    message="CSS 出现多余的闭合花括号。",
                    suggestion="修正 CSS 块边界并重新执行构建。", blocking=True,
                ))
                break
        if depth > 0 and not findings:
            findings.append(ReviewFinding(
                severity="high", category="syntax", file=path,
                message="CSS 存在未闭合的花括号。",
                suggestion="补全 CSS 规则块后重新执行构建。", blocking=True,
            ))
        return findings

    def _model_findings(self, task: Task, changed_lines: dict[str, list[tuple[int, str]]]) -> list[ReviewFinding]:
        if not self.model_gateway.enabled:
            return []
        current_files: dict[str, str] = {}
        if task.workspace and task.technical_plan:
            workspace = Path(task.workspace).resolve()
            for relative in task.technical_plan.affected_files[:20]:
                target = (workspace / relative).resolve()
                if workspace in target.parents and target.is_file():
                    current_files[relative] = target.read_text(
                        encoding="utf-8", errors="replace"
                    )[:24_000]
        response = self.model_gateway.generate_structured(
            "你是独立 Code Reviewer。只审查给定需求、方案、测试证据和 Diff。"
            "只报告能从 Diff 直接定位的问题；不确定内容不得标为 high/critical。"
            "重点审查删除行为：测试、编译或 Lint 通过不代表删除符合用户意图。若 Diff 删除或绕过"
            "既有入口、导出、路由、组件组合、公共接口或调用链，而 requirement/technical_plan 没有"
            "明确授权，即使新代码能运行，也必须报告 medium acceptance/functionality 问题。"
            "intentional_removals 只是 Developer 的声明，必须与需求和 Diff 独立核对，不能直接采信。"
            "current_files 是审查时的真实工作区快照；判断行为是否存在时必须同时检查它。"
            "文件未出现在 Diff 只表示本任务没有修改它，不能据此推断其中的功能或文本不存在。"
            "输出严格 JSON。",
            json.dumps({
                "requirement": task.requirement,
                "figma_design": (
                    {
                        "node_id": task.design_reference.node_id,
                        "context": task.design_reference.context[:20000],
                        "variables": task.design_reference.variables[:8000],
                    }
                    if task.design_reference else None
                ),
                "ui_acceptance": (
                    task.ui_acceptance.model_dump(mode="json")
                    if task.ui_acceptance else None
                ),
                "acceptance_criteria": task.analysis.acceptance_criteria if task.analysis else [],
                "approved_files": task.technical_plan.affected_files if task.technical_plan else [],
                "test": {"command": task.result.command, "exit_code": task.result.exit_code},
                "diff": task.result.diff,
                "current_files": current_files,
                "developer_semantic_contract": self._latest_semantic_contract(task),
                "valid_changed_lines": {path: [line for line, _ in lines] for path, lines in changed_lines.items()},
                "schema": ReviewerModelOutput.model_json_schema(),
            }, ensure_ascii=False),
            ReviewerModelOutput,
        )
        if not isinstance(response, ReviewerModelOutput):
            return []
        valid_paths = set(changed_lines)
        result: list[ReviewFinding] = []
        for finding in response.findings:
            if finding.file and finding.file not in valid_paths:
                continue
            if finding.file and finding.line:
                valid_lines = {line for line, _ in changed_lines[finding.file]}
                if finding.line not in valid_lines:
                    continue
            blocking_categories = ("syntax", "security", "runtime", "scope", "crash", "data loss")
            if (
                finding.severity.lower() in self.BLOCKING_SEVERITIES
                and not any(token in finding.category.lower() for token in blocking_categories)
            ):
                finding.severity = "medium"
                finding.blocking = False
            result.append(finding)
        return result

    @staticmethod
    def _latest_semantic_contract(task: Task) -> dict[str, list[str]]:
        for attempt in reversed(task.development_attempts):
            if attempt.proposal is not None:
                return {
                    "preserved_behaviors": attempt.proposal.preserved_behaviors,
                    "intentional_removals": attempt.proposal.intentional_removals,
                }
        return {"preserved_behaviors": [], "intentional_removals": []}

    @staticmethod
    def _changed_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
        result: dict[str, list[tuple[int, str]]] = {}
        current_file: str | None = None
        new_line = 0
        for raw in diff.splitlines():
            if raw.startswith("+++ b/"):
                current_file = raw[6:]
                result.setdefault(current_file, [])
                continue
            if raw.startswith("@@"):
                match = re.search(r"\+(\d+)", raw)
                if match:
                    new_line = int(match.group(1))
                continue
            if current_file is None or raw.startswith(("diff --git", "--- ")):
                continue
            if raw.startswith("+"):
                result[current_file].append((new_line, raw[1:]))
                new_line += 1
            elif not raw.startswith("-"):
                new_line += 1
        return result
