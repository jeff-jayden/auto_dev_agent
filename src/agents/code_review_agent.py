"""代码审查 Agent。

结合需求、技术方案、累计 Diff 和测试证据识别阻塞问题，并驱动审查与修复循环。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .task_context import build_task_context

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
        """初始化结合规则检查与模型判断的代码审查 Agent。

        Args:
            model_gateway: 生成结构化审查结论和问题列表的模型网关。
        """
        self.model_gateway = model_gateway

    def review(
        self,
        task: Task,
        round_number: int,
        baseline_deletion_candidates: list[dict] | None = None,
    ) -> ReviewRound:
        if task.result is None or task.merge_request is None:
            raise ValueError("MR draft and execution result are required before review")

        changed_lines = self._changed_lines(task.result.diff)
        findings, checks = self._deterministic_findings(task, changed_lines)
        model_findings, baseline_restore_ids = self._model_findings(
            task,
            changed_lines,
            baseline_deletion_candidates or [],
        )
        findings.extend(model_findings)

        normalized: list[ReviewFinding] = []
        seen: set[tuple] = set()
        for finding in findings:
            finding.severity = finding.severity.lower()
            category = finding.category.lower().strip()
            category_parts = {
                part for part in re.split(r"[/|,>\s]+", category) if part
            }
            finding.blocking = (
                finding.blocking
                or finding.severity in self.BLOCKING_SEVERITIES
                or (
                    finding.severity == "medium"
                    and bool(category_parts & self.CORE_BLOCKING_CATEGORIES)
                )
            )
            key = (finding.file, finding.line, finding.category, finding.message)
            if key not in seen:
                seen.add(key)
                normalized.append(finding)

        blocking = [item for item in normalized if item.blocking]
        if baseline_restore_ids and not blocking:
            selected_candidates = {
                item["id"]: item for item in (baseline_deletion_candidates or [])
                if item["id"] in baseline_restore_ids
            }
            files = sorted({item["file"] for item in selected_candidates.values()})
            restoration_finding = ReviewFinding(
                severity="medium",
                category="acceptance",
                file=files[0] if len(files) == 1 else None,
                message="Reviewer 已确认存在需求未授权的删除，必须先从 Git 基线恢复对应片段。",
                suggestion="应用已选择的基线恢复片段，然后重新测试并再次审查。",
                blocking=True,
            )
            normalized.append(restoration_finding)
            blocking.append(restoration_finding)
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
            baseline_restore_ids=baseline_restore_ids,
        )

    def run_loop(
        self,
        task: Task,
        review_node: Callable[[ReviewLoopState], dict],
        repair_node: Callable[[ReviewLoopState], dict],
    ) -> Task:
        """Run the bounded review/repair subgraph for this reviewer."""
        def reviewed(state):
            result = review_node(state)
            review = result.get("review")
            if result.get("route") == "repair" and review and any(
                finding.blocking and finding.category in {"context", "model"}
                for finding in review.findings
            ):
                # 平台/模型故障不是代码缺陷，禁止交给 Developer 制造无关修改。
                task.error = "审查未完成：上下文或模型调用失败，请修复审查服务后重新审查"
                result["route"] = "done"
            return result

        graph = StateGraph(ReviewLoopState)
        graph.add_node("review", reviewed)
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

    def _model_findings(
        self,
        task: Task,
        changed_lines: dict[str, list[tuple[int, str]]],
        baseline_deletion_candidates: list[dict],
    ) -> tuple[list[ReviewFinding], list[str]]:
        if not self.model_gateway.enabled:
            return [], []
        current_files: dict[str, str] = {}
        if task.workspace and task.technical_plan:
            workspace = Path(task.workspace).resolve()
            for relative in task.technical_plan.affected_files[:20]:
                target = (workspace / relative).resolve()
                if workspace in target.parents and target.is_file():
                    current_files[relative] = target.read_text(
                        encoding="utf-8", errors="replace"
                    )[:24_000]
        review_diff, review_candidates = self._review_evidence(
            task.result.diff, current_files, baseline_deletion_candidates,
        )
        response = self.model_gateway.generate_structured(
            "你是独立 Code Reviewer。只审查给定需求、方案、测试证据和 Diff。"
            "只报告能从 Diff 直接定位的问题；不确定内容不得标为 high/critical。"
            "重点审查删除行为：测试、编译或 Lint 通过不代表删除符合用户意图。若 Diff 删除或绕过"
            "既有入口、导出、路由、组件组合、公共接口或调用链，而 requirement/technical_plan 没有"
            "明确授权，即使新代码能运行，也必须报告 medium acceptance/functionality 问题。"
            "intentional_removals 只是 Developer 的声明，必须与需求和 Diff 独立核对，不能直接采信。"
            "baseline_deletion_candidates 是系统从 Git 基线与当前工作区精确计算出的删除/替换片段。"
            "只有当某个候选片段确属需求和技术方案未授权的误删时，才把它的 id 放入 baseline_restore_ids；"
            "系统会从 Git 基线确定性恢复该片段，禁止在 suggestion 中要求 Developer 凭记忆重写原内容。"
            "若删除已由需求、技术方案或有效 intentional_removals 明确授权，不得选择对应候选。"
            "current_files 是审查时的真实工作区快照；判断行为是否存在时必须同时检查它。"
            "Diff 中的新增内容引用标记表示对应源码已在 current_files 中提供，必须读取该文件指定行范围，"
            "不是省略了审查内容。候选片段的 original_text_reference/current_text_reference 指向"
            "Diff 删除行或 current_files 中完全相同的原文，需结合行号核对，不能将引用标记当代码。"
            "文件未出现在 Diff 只表示本任务没有修改它，不能据此推断其中的功能或文本不存在。"
            "输出严格 JSON。",
            json.dumps({
                "requirement": task.requirement,
                "task_context": build_task_context(task),
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
                "diff": review_diff,
                "current_files": current_files,
                "developer_semantic_contract": self._latest_semantic_contract(task),
                "baseline_deletion_candidates": review_candidates,
                "valid_changed_lines": {path: [line for line, _ in lines] for path, lines in changed_lines.items()},
                "schema": ReviewerModelOutput.model_json_schema(),
            }, ensure_ascii=False),
            ReviewerModelOutput,
        )
        if not isinstance(response, ReviewerModelOutput):
            error = getattr(self.model_gateway, "last_error", "")
            if isinstance(error, str) and "budget" in error.lower():
                return [ReviewFinding(
                    severity="high", category="context", blocking=True,
                    message="Review context exceeded its budget; review was not completed.",
                    suggestion="Narrow the review scope or increase the configured context budget before publishing.",
                )], []
            return [ReviewFinding(
                severity="high", category="model", blocking=True,
                message="独立审查模型未返回有效审查结果，不能视为审查通过。",
                suggestion="检查模型调用错误后重新审查；无需让 Developer 修改代码。",
            )], []
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
        valid_restore_ids = {item["id"] for item in baseline_deletion_candidates}
        restore_ids = [
            candidate_id
            for candidate_id in dict.fromkeys(response.baseline_restore_ids)
            if candidate_id in valid_restore_ids
        ]
        return result, restore_ids

    @staticmethod
    def _review_evidence(diff: str, current_files: dict[str, str], candidates: list[dict]):
        """只对已验证完全相同的源码去重；保留删除证据、行号和恢复候选 ID。"""
        snapshots = {path: text.splitlines() for path, text in current_files.items()}
        packed, deleted = [], {}
        pending = []
        path, new_line = "", 0

        def flush():
            if pending:
                reference = f"+ [新增内容见 current_files[{json.dumps(path)}] 第 {new_line - len(pending)}-{new_line - 1} 行]"
                original = "\n".join(pending)
                packed.extend([reference] if len(reference.encode()) < len(original.encode()) else pending)
                pending.clear()

        for line in diff.splitlines():
            if line.startswith("+++ ") or line.startswith("@@") or line.startswith("diff --git"):
                flush()
                if line.startswith("+++ b/"):
                    path = line[6:]
                elif line.startswith("@@"):
                    match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
                    new_line = int(match.group(1)) if match else 0
                packed.append(line)
            elif line.startswith("+") and not line.startswith("+++"):
                source = snapshots.get(path, [])
                if 0 < new_line <= len(source) and source[new_line - 1] == line[1:]:
                    pending.append(line)
                else:
                    flush()
                    packed.append(line)
                new_line += 1
            else:
                flush()
                packed.append(line)
                if line.startswith("-") and not line.startswith("---"):
                    deleted.setdefault(path, []).append(line[1:])
                elif line.startswith(" "):
                    new_line += 1
        flush()
        compact_candidates = []
        for item in candidates[:30]:
            entry = {"id": item["id"], "file": item["file"], "operation": item["operation"],
                     "baseline_lines": [item["baseline_start_line"], item["baseline_end_line"]],
                     "current_lines": [item["current_start_line"], item["current_end_line"]]}
            old = item["original_text"].replace("\r\n", "\n")
            current = item["current_text"].replace("\r\n", "\n")
            if old.strip() and old.rstrip("\r\n") in "\n".join(deleted.get(item["file"], [])):
                entry["original_text_reference"] = "Diff 中该文件 baseline_lines 对应的删除行"
            else:
                entry["original_text"] = old[:12000]
            if current.strip() and current.rstrip("\r\n") in current_files.get(item["file"], ""):
                entry["current_text_reference"] = "current_files 中该文件 current_lines 对应的源码"
            else:
                entry["current_text"] = current[:12000]
            compact_candidates.append(entry)
        return "\n".join(packed), compact_candidates

    @staticmethod
    def _latest_semantic_contract(task: Task) -> dict[str, list[str]]:
        for attempt in reversed(task.development_attempts):
            if attempt.proposal is not None:
                return {
                    "intentional_removals": attempt.proposal.intentional_removals,
                }
        return {"intentional_removals": []}

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
