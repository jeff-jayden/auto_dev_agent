"""仓库事实分析器。

识别语言、框架、分支、测试命令和文件结构，并调用代码索引生成与当前需求相关的上下文包。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from code_intelligence import RepositoryCodeIndex
from domain.models import CodeEvidence, RepositoryAnalysis, ToolCallAudit


IGNORED_DIRECTORIES = {
    ".git", ".idea", ".venv", "venv", "node_modules", "dist", "build",
    "__pycache__", ".pytest_cache", "runtime", "coverage", ".next",
}
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs",
    ".md", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".css", ".scss",
}


class RepositoryAnalyzer:
    """Read-only repository inspection with an explicit tool-call audit trail."""

    def __init__(self, index_root: Path | None = None):
        """初始化仓库分析器。

        Args:
            index_root: 可选的代码索引缓存目录；未提供时仅在内存中构建索引。
        """
        self.code_index = RepositoryCodeIndex(index_root)

    def analyze(self, repository: Path, requirement: str = "") -> RepositoryAnalysis:
        repository = repository.resolve()
        tool_calls: list[ToolCallAudit] = []
        files = self._list_files(repository)
        tool_calls.append(ToolCallAudit(tool="list_files", arguments={"limit": 1000}, summary=f"发现 {len(files)} 个可分析文件"))

        language, framework = self._detect_stack(files, repository)
        tool_calls.append(ToolCallAudit(tool="detect_stack", summary=f"识别为 {language}{' / ' + framework if framework else ''}"))

        branch = self._git(repository, "branch", "--show-current")
        sha = self._git(repository, "rev-parse", "HEAD")
        tool_calls.append(ToolCallAudit(tool="git_metadata", summary=f"基线 {branch or 'unknown'}@{(sha or 'unknown')[:8]}"))

        important = self._important_files(files)
        entrypoints = self._entrypoints(files)
        test_directories = sorted({path.split("/")[0] for path in files if path.startswith(("tests/", "test/", "spec/"))})
        test_command = self._test_command(language, files, repository)
        index, index_mode, changed_files = self.code_index.build(repository, files, sha)
        context_pack, indexed_evidence = self.code_index.context_pack(
            repository, index, requirement, entrypoints, index_mode, changed_files
        )
        tool_calls.append(ToolCallAudit(
            tool="code_index",
            arguments={"mode": index_mode, "version": context_pack.index_version},
            summary=(
                f"代码索引 {index_mode}，选择 {len(context_pack.primary_files)} 个核心文件、"
                f"{len(context_pack.dependency_files)} 个依赖文件和 {len(context_pack.test_files)} 个测试文件"
            ),
        ))
        evidence = indexed_evidence or self._find_evidence(repository, files, requirement, important)
        tool_calls.append(ToolCallAudit(tool="search_code", arguments={"requirement": requirement[:160]}, summary=f"找到 {len(evidence)} 条代码证据"))

        return RepositoryAnalysis(
            language=language,
            framework=framework,
            default_branch=branch,
            head_sha=sha,
            file_count=len(files),
            entrypoints=entrypoints,
            test_directories=test_directories,
            test_command=test_command,
            important_files=important[:20],
            evidence=evidence,
            tool_calls=tool_calls,
            context_pack=context_pack,
        )

    def current_head(self, repository: Path) -> str | None:
        return self._git(repository.resolve(), "rev-parse", "HEAD")

    @property
    def index_version(self) -> int:
        return self.code_index.version

    def _list_files(self, repository: Path) -> list[str]:
        result: list[str] = []
        for path in repository.rglob("*"):
            if len(result) >= 1000:
                break
            if not path.is_file() or any(part in IGNORED_DIRECTORIES for part in path.parts):
                continue
            if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"Dockerfile", "Makefile"}:
                result.append(path.relative_to(repository).as_posix())
        return sorted(result)

    @staticmethod
    def _detect_stack(files: list[str], repository: Path) -> tuple[str, str | None]:
        names = set(files)
        if "pyproject.toml" in names or any(path.endswith(".py") for path in files):
            content = RepositoryAnalyzer._safe_read(repository / "pyproject.toml").lower()
            framework = "fastapi" if "fastapi" in content else "django" if "django" in content else None
            return "python", framework
        if "package.json" in names or any(path.endswith((".ts", ".tsx", ".js")) for path in files):
            content = RepositoryAnalyzer._safe_read(repository / "package.json").lower()
            framework = "react" if "react" in content else "next.js" if "next" in content else "node.js"
            return "typescript" if any(path.endswith((".ts", ".tsx")) for path in files) else "javascript", framework
        if "go.mod" in names:
            return "go", None
        if "Cargo.toml" in names:
            return "rust", None
        return "unknown", None

    @staticmethod
    def _important_files(files: list[str]) -> list[str]:
        preferred = (
            "README", "pyproject.toml", "package.json", "go.mod", "Cargo.toml",
            "Dockerfile", "main.py", "app.py", "App.js", "App.jsx", "App.tsx",
            "App.test.js", "App.test.jsx", "App.test.tsx", "App.css", "index.js", "index.tsx",
        )
        return [path for path in files if Path(path).name.startswith(preferred)][:20]

    @staticmethod
    def _entrypoints(files: list[str]) -> list[str]:
        names = {"main.py", "app.py", "server.py", "main.ts", "index.ts", "index.js", "main.go"}
        return [path for path in files if Path(path).name in names][:20]

    @staticmethod
    def _test_command(language: str, files: list[str], repository: Path) -> str | None:
        names = set(files)
        if language == "python":
            test_files = [path for path in files if path.startswith(("tests/", "test/")) and path.endswith(".py")]
            test_sample = "\n".join(RepositoryAnalyzer._safe_read(repository / path) for path in test_files[:10])
            uses_pytest = (
                "pytest.ini" in names
                or "conftest.py" in {Path(p).name for p in files}
                or "import pytest" in test_sample
                or ("def test_" in test_sample and "unittest" not in test_sample)
            )
            return "python -m pytest -q" if uses_pytest else "python -m unittest discover -s tests -v"
        if language in {"typescript", "javascript"} and "package.json" in names:
            package = RepositoryAnalyzer._safe_read(repository / "package.json").lower()
            return "npm test -- --watchAll=false" if "react" in package else "npm test"
        if language == "go":
            return "go test ./..."
        if language == "rust":
            return "cargo test"
        return None

    def _find_evidence(self, repository: Path, files: list[str], requirement: str, important: list[str]) -> list[CodeEvidence]:
        terms = {term.lower() for term in re.findall(r"[A-Za-z_][A-Za-z0-9_-]{2,}", requirement)}
        candidates: list[CodeEvidence] = []
        ranked_files = list(dict.fromkeys(important + [p for p in files if Path(p).suffix in {".py", ".ts", ".tsx", ".js"}]))
        for relative in ranked_files[:120]:
            content = self._safe_read(repository / relative)
            if not content:
                continue
            for line_number, line in enumerate(content.splitlines(), 1):
                normalized = line.lower()
                matched = sorted(term for term in terms if term in normalized)
                is_symbol = bool(re.search(r"\b(def|class|function|export|interface)\b", line))
                if matched or (is_symbol and len(candidates) < 4):
                    reason = f"匹配需求关键词：{', '.join(matched)}" if matched else "仓库中的主要代码符号"
                    candidates.append(CodeEvidence(path=relative, line=line_number, snippet=line.strip()[:240], reason=reason))
                    if len(candidates) >= 12:
                        return candidates
        return candidates

    @staticmethod
    def _safe_read(path: Path) -> str:
        try:
            if not path.exists() or path.stat().st_size > 512_000:
                return ""
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str | None:
        if not (repository / ".git").exists():
            return None
        completed = subprocess.run(
            ["git", *arguments], cwd=repository, capture_output=True, text=True,
            timeout=10, check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else None
