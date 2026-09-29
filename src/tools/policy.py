"""Agent 工具调用策略。

校验可写文件范围、禁止访问的敏感路径、修改风险等级以及允许执行的测试命令。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


FORBIDDEN_NAMES = {".env", ".env.local", "id_rsa", "id_ed25519", "credentials", "secrets.yml"}
FORBIDDEN_PARTS = {".git", "node_modules", ".venv", "venv", "__pycache__"}
HIGH_RISK_PARTS = {"migrations", "alembic", ".github", ".gitlab", "deploy", "helm", "terraform"}
HIGH_RISK_NAMES = {".gitlab-ci.yml", "Dockerfile", "docker-compose.yml", "docker-compose.yaml"}
MEDIUM_RISK_NAMES = {"pyproject.toml", "package.json", "package-lock.json", "requirements.txt", "poetry.lock"}


@dataclass(frozen=True)
class RiskAssessment:
    level: str
    reasons: list[str]


class ToolPolicy:
    def __init__(self, repository: Path, allowed_paths: list[str], test_command: str):
        """初始化 Developer Agent 的工具调用策略。

        Args:
            repository: 当前任务隔离 Worktree 的根目录。
            allowed_paths: 技术方案批准的可写文件相对路径列表。
            test_command: 允许 Agent 执行的项目验证命令。
        """
        self.repository = repository.resolve()
        self.allowed_paths = {PurePosixPath(path).as_posix() for path in allowed_paths}
        self.test_command = test_command.strip()

    def resolve_write_path(self, relative_path: str) -> Path:
        normalized = PurePosixPath(relative_path).as_posix().lstrip("/")
        parts = set(PurePosixPath(normalized).parts)
        if parts & FORBIDDEN_PARTS or PurePosixPath(normalized).name in FORBIDDEN_NAMES:
            raise ValueError(f"Path is forbidden: {normalized}")
        target = (self.repository / Path(normalized)).resolve()
        if self.repository not in target.parents:
            raise ValueError("Path escaped repository root")
        if self.allowed_paths and normalized not in self.allowed_paths:
            raise ValueError(f"Path is outside the approved technical plan: {normalized}")
        return target

    def assess_paths(self, paths: list[str]) -> RiskAssessment:
        reasons: list[str] = []
        level = "low"
        for raw in paths:
            path = PurePosixPath(raw)
            parts = set(path.parts)
            if path.name in FORBIDDEN_NAMES or parts & FORBIDDEN_PARTS:
                return RiskAssessment("forbidden", [f"禁止修改敏感路径：{raw}"])
            if path.name in HIGH_RISK_NAMES or parts & HIGH_RISK_PARTS:
                level = "high"
                reasons.append(f"涉及部署、CI 或数据库结构：{raw}")
            elif path.name in MEDIUM_RISK_NAMES and level == "low":
                level = "medium"
                reasons.append(f"涉及依赖或项目配置：{raw}")
        return RiskAssessment(level, reasons)

    def validate_test_command(self, command: str) -> None:
        if command.strip() != self.test_command:
            raise ValueError("Agent requested a command that was not approved by repository analysis")
