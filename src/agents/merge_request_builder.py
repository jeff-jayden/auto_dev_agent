"""Merge Request 草稿构建器。

将任务结果、修改文件、测试证据和审查结论整理为可发布的 MR 标题与描述；本模块不调用模型。
"""

from __future__ import annotations

import re

from domain.models import MergeRequestDraft, Task


class MergeRequestBuilder:
    """Build an evidence-backed local MR draft from verified task artifacts."""

    def write(self, task: Task) -> MergeRequestDraft:
        if task.result is None or not task.result.success:
            raise ValueError("A successful execution result is required to generate an MR draft")

        changed_files = self._changed_files(task.result.diff)
        criteria = list(task.analysis.acceptance_criteria) if task.analysis else []
        risks = list(task.technical_plan.risks) if task.technical_plan else []
        changelog = [f"更新 {path}：实现并验证“{task.title}”" for path in changed_files]
        summary = task.development_attempts[-1].summary if task.development_attempts else task.title
        test_command = " ".join(task.result.command)
        description = self._description(task, summary, changed_files, criteria, test_command, risks)

        return MergeRequestDraft(
            title=task.result.mr_title or f"feat: {task.title}",
            summary=summary,
            description=description,
            changed_files=changed_files,
            acceptance_checklist=criteria,
            test_summary=f"{test_command} · exit {task.result.exit_code} · 通过",
            risks=risks,
            rollback_plan="撤销本 MR 或丢弃任务 Worktree；当前阶段不执行数据迁移和线上发布。",
            changelog=changelog,
        )

    @staticmethod
    def _changed_files(diff: str) -> list[str]:
        return list(dict.fromkeys(re.findall(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE)))

    @staticmethod
    def _description(
        task: Task,
        summary: str,
        files: list[str],
        criteria: list[str],
        test_command: str,
        risks: list[str],
    ) -> str:
        file_lines = "\n".join(f"- {path}" for path in files) or "- 无"
        checklist = "\n".join(f"- [x] {item}" for item in criteria) or "- [x] 已完成需求描述"
        risk_lines = "\n".join(f"- {item}" for item in risks) or "- 未发现额外风险"
        ui_section = ""
        if task.design_reference:
            report = task.ui_acceptance
            if report:
                ui_checks = "\n".join(
                    f"- [{'x' if item.status == 'passed' else ' '}] {item.name}：{item.detail}"
                    for item in report.checks
                )
                similarity = (
                    f"{report.similarity_score:.1f}%"
                    if report.similarity_score is not None else "未计算"
                )
                ui_section = f"""
## UI 验收

- Figma 节点：{task.design_reference.node_id}
- 视口：{task.design_reference.viewport_width}×{task.design_reference.viewport_height}
- 状态：{report.status}
- 视觉相似度：{similarity}

{ui_checks}
"""
            else:
                ui_section = "\n## UI 验收\n\n- 已绑定 Figma 设计，但尚未执行 UI 验收。\n"
        return f"""## 变更说明

{summary}

## 修改文件

{file_lines}

## 验收标准

{checklist}

## 自动验证

- 命令：{test_command}
- 退出码：{task.result.exit_code}
- 结果：通过

{ui_section}

## 风险

{risk_lines}

## 回滚

- 撤销本 MR 或丢弃隔离 Worktree。
"""
