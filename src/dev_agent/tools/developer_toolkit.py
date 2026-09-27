from __future__ import annotations

import os
import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dev_agent.domain.models import DevelopmentProposal, ToolCallAudit
from .policy import ToolPolicy


class PatchRejected(ValueError):
    pass


@dataclass
class TestExecution:
    command: list[str]
    exit_code: int
    output: str


class DeveloperToolkit:
    READABLE_SUFFIXES = {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".sass",
        ".less", ".java", ".go", ".rs", ".json", ".toml", ".yaml", ".yml",
        ".md", ".ini", ".cfg",
    }
    IGNORED_PARTS = {
        ".git", ".idea", ".venv", "venv", "node_modules", "dist", "build",
        "__pycache__", ".pytest_cache", "runtime", "coverage", ".next",
    }
    SENSITIVE_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
    SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}

    def __init__(self, repository: Path, policy: ToolPolicy, dependency_repository: Path | None = None):
        self.repository = repository.resolve()
        self.policy = policy
        self.dependency_repository = dependency_repository.resolve() if dependency_repository else None
        self.audit: list[ToolCallAudit] = []

    def read_context(self, paths: list[str], max_chars_per_file: int = 24_000) -> dict[str, str]:
        context: dict[str, str] = {}
        for relative in paths[:20]:
            target = (self.repository / relative).resolve()
            if self.repository not in target.parents or not target.is_file():
                continue
            content = target.read_text(encoding="utf-8", errors="replace")[:max_chars_per_file]
            context[relative] = content
            self.audit.append(ToolCallAudit(tool="read_file", arguments={"path": relative}, summary=f"读取 {relative}"))
        return context

    def list_readable_files(self, limit: int = 300) -> list[str]:
        files: list[str] = []
        for target in self.repository.rglob("*"):
            if len(files) >= limit:
                break
            if not target.is_file():
                continue
            relative = target.relative_to(self.repository)
            if any(part in self.IGNORED_PARTS for part in relative.parts):
                continue
            if self._is_sensitive_read_target(target):
                continue
            if target.suffix.lower() not in self.READABLE_SUFFIXES and target.name not in {"Dockerfile", "Makefile"}:
                continue
            files.append(relative.as_posix())
        return sorted(files)

    def _resolve_read_path(self, relative: str) -> Path:
        normalized = relative.replace("\\", "/").strip()
        target = (self.repository / normalized).resolve()
        if self.repository not in target.parents or not target.is_file():
            raise ValueError(f"Read target is outside the repository or missing: {relative}")
        repository_relative = target.relative_to(self.repository)
        if self._is_sensitive_read_target(target) or any(
            part in self.IGNORED_PARTS for part in repository_relative.parts
        ):
            raise ValueError(f"Read target is not available to the context explorer: {relative}")
        if target.suffix.lower() not in self.READABLE_SUFFIXES and target.name not in {"Dockerfile", "Makefile"}:
            raise ValueError(f"Read target type is not allowed: {relative}")
        return target

    def _is_sensitive_read_target(self, target: Path) -> bool:
        lowered = target.name.lower()
        return (
            lowered in self.SENSITIVE_NAMES
            or lowered.startswith(".env")
            or target.suffix.lower() in self.SENSITIVE_SUFFIXES
        )

    def read_file_slice(self, relative: str, start_line: int = 1, end_line: int = 240) -> dict:
        target = self._resolve_read_path(relative)
        start = max(1, start_line)
        end = min(max(start, end_line), start + 399)
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        content = "\n".join(lines[start - 1:end])
        if content and end <= len(lines):
            content += "\n"
        normalized = target.relative_to(self.repository).as_posix()
        self.audit.append(ToolCallAudit(
            tool="read_file",
            arguments={"path": normalized, "start_line": start, "end_line": end},
            summary=f"读取 {normalized} 第 {start}-{min(end, len(lines))} 行",
        ))
        return {
            "path": normalized,
            "start_line": start,
            "end_line": min(end, len(lines)),
            "content": content[:24_000],
        }

    def _search_repository(self, query: str, *, symbol: bool, limit: int = 20) -> list[dict]:
        query = query.strip()
        if len(query) < 2:
            raise ValueError("Search query must contain at least 2 characters")
        pattern = re.compile(rf"\b{re.escape(query)}\b" if symbol else re.escape(query), re.I)
        results: list[dict] = []
        for relative in self.list_readable_files(limit=1000):
            target = self.repository / relative
            for number, line in enumerate(target.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if pattern.search(line):
                    results.append({"path": relative, "line": number, "snippet": line.strip()[:300]})
                    if len(results) >= limit:
                        return results
        return results

    def search_text(self, query: str, limit: int = 12) -> list[dict]:
        results = self._search_repository(query, symbol=False, limit=limit)
        self.audit.append(ToolCallAudit(
            tool="search_text",
            arguments={"query": query, "limit": limit},
            summary=f"文本搜索“{query}”命中 {len(results)} 处",
        ))
        return results

    def search_symbol(self, symbol: str, limit: int = 12) -> list[dict]:
        results = self._search_repository(symbol, symbol=True, limit=limit)
        self.audit.append(ToolCallAudit(
            tool="search_symbol",
            arguments={"symbol": symbol, "limit": limit},
            summary=f"符号搜索“{symbol}”命中 {len(results)} 处",
        ))
        return results

    def find_references(self, symbol: str, limit: int = 16) -> list[dict]:
        results = self._search_repository(symbol, symbol=True, limit=limit)
        self.audit.append(ToolCallAudit(
            tool="find_references",
            arguments={"symbol": symbol, "limit": limit},
            summary=f"引用搜索“{symbol}”命中 {len(results)} 处",
        ))
        return results

    def git_history(self, relative: str, limit: int = 8) -> dict:
        target = self._resolve_read_path(relative)
        normalized = target.relative_to(self.repository).as_posix()
        completed = subprocess.run(
            ["git", "log", f"-{max(1, min(limit, 20))}", "--format=%h%x09%ad%x09%s", "--date=short", "--", normalized],
            cwd=self.repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or "Unable to read git history")
        entries = completed.stdout.splitlines()[:limit]
        self.audit.append(ToolCallAudit(
            tool="git_history",
            arguments={"path": normalized, "limit": limit},
            summary=f"读取 {normalized} 最近 {len(entries)} 条提交记录",
        ))
        return {"path": normalized, "entries": entries}

    def apply_proposal(self, proposal: DevelopmentProposal) -> list[str]:
        if proposal.replacements:
            if proposal.changes:
                raise PatchRejected("A proposal cannot mix text replacements and unified patches")
            return self._apply_replacements(proposal)
        changed: list[str] = []
        patch_parts: list[str] = []
        if not proposal.changes:
            raise PatchRejected("Proposal contains no changes")
        for change in proposal.changes:
            self.policy.resolve_write_path(change.path)
            patch_text = change.patch.strip() + "\n"
            declared_path = change.path.replace("\\", "/")
            diff_headers = [line for line in patch_text.splitlines() if line.startswith("diff --git ")]
            new_headers = [line[6:] for line in patch_text.splitlines() if line.startswith("+++ b/")]
            if len(diff_headers) != 1 or new_headers != [declared_path]:
                raise PatchRejected(
                    f"Patch headers do not match declared path {declared_path}"
                )
            if "/dev/null" in patch_text:
                raise PatchRejected("File deletion is not allowed in this phase")
            if len(patch_text) > 200_000:
                raise PatchRejected("A single patch exceeded the 200 KB limit")
            if change.path in changed:
                raise PatchRejected(f"A proposal may patch a file only once: {change.path}")
            changed.append(change.path)
            patch_parts.append(patch_text)
        combined_patch = "\n".join(patch_parts)
        check = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", "-"],
            cwd=self.repository, input=combined_patch, capture_output=True, text=True,
            timeout=20, check=False,
        )
        if check.returncode != 0:
            raise PatchRejected(check.stderr.strip() or "git apply --check rejected the patch")
        subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"], cwd=self.repository,
            input=combined_patch, capture_output=True, text=True, timeout=20, check=True,
        )
        for change in proposal.changes:
            self.audit.append(ToolCallAudit(tool="apply_patch", arguments={"path": change.path}, summary=change.reason))
        return changed

    def _apply_replacements(self, proposal: DevelopmentProposal) -> list[str]:
        if not proposal.replacements:
            raise PatchRejected("Proposal contains no changes or replacements")
        originals: dict[Path, str] = {}
        updated: dict[Path, str] = {}
        relative_paths: list[str] = []
        for replacement in proposal.replacements:
            search = self.normalize_model_text(replacement.search)
            replace = self.normalize_model_text(replacement.replace)
            if search == replace:
                raise PatchRejected(
                    f"Replacement in {replacement.path} does not change any content"
                )
            target = self.policy.resolve_write_path(replacement.path)
            if not target.is_file():
                raise PatchRejected(f"Text replacement target does not exist: {replacement.path}")
            current = updated.get(target)
            if current is None:
                current = target.read_text(encoding="utf-8", errors="strict")
                originals[target] = current
            self._validate_replacement_shape(replacement.path, search, replace)
            occurrences = current.count(search)
            if occurrences == 1:
                updated[target] = current.replace(search, replace, 1)
            elif occurrences == 0:
                parts = [part for part in re.split(r"\s+", search.strip()) if part]
                pattern = r"\s+".join(re.escape(part) for part in parts)
                matches = list(re.finditer(pattern, current)) if pattern else []
                if len(matches) != 1:
                    raise PatchRejected(
                        f"Search text must occur exactly once in {replacement.path}; "
                        f"found {occurrences} exact and {len(matches)} whitespace-normalized matches"
                    )
                match = matches[0]
                updated[target] = current[:match.start()] + replace + current[match.end():]
            else:
                raise PatchRejected(
                    f"Search text must occur exactly once in {replacement.path}; found {occurrences}"
                )
            if replacement.path not in relative_paths:
                relative_paths.append(replacement.path)
        for target, content in updated.items():
            self._validate_content(target, content)
        try:
            for target, content in updated.items():
                target.write_text(content, encoding="utf-8")
        except OSError:
            for target, content in originals.items():
                target.write_text(content, encoding="utf-8")
            raise
        for replacement in proposal.replacements:
            self.audit.append(
                ToolCallAudit(
                    tool="apply_text_replacement",
                    arguments={"path": replacement.path},
                    summary=replacement.reason,
                )
            )
        return relative_paths

    @staticmethod
    def normalize_model_text(value: str) -> str:
        """Recover snippets that a small model accidentally JSON-escapes twice."""
        if "\n" not in value and "\\n" in value:
            return (
                value.replace("\\r\\n", "\n")
                .replace("\\n", "\n")
                .replace('\\"', '"')
                .replace("\\'", "'")
                .replace("\\t", "\t")
            )
        return value

    @staticmethod
    def _validate_replacement_shape(path: str, search: str, replace: str) -> None:
        suffix = Path(path).suffix.lower()
        if suffix not in {".css", ".scss", ".js", ".jsx", ".ts", ".tsx"}:
            return
        search_balance = search.count("{") - search.count("}")
        replacement_balance = replace.count("{") - replace.count("}")
        if search_balance > 0 and replacement_balance <= 0:
            raise PatchRejected(
                f"Structural replacement in {path} is unsafe: search opens a block but does not "
                "include its closing brace. Replace the complete block instead."
            )

    @staticmethod
    def _validate_content(path: Path, content: str) -> None:
        if path.suffix.lower() not in {".css", ".scss"}:
            return
        masked = re.sub(r"/\*.*?\*/", "", content, flags=re.S)
        masked = re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", '""', masked)
        depth = 0
        for line_number, raw in enumerate(masked.splitlines(), 1):
            line = raw.strip()
            if depth == 0 and re.match(r"^(?:--)?[-A-Za-z_][\w-]*\s*:", line):
                raise PatchRejected(
                    f"CSS validation failed in {path.name}:{line_number}: property is outside a rule block"
                )
            depth += raw.count("{") - raw.count("}")
            if depth < 0:
                raise PatchRejected(
                    f"CSS validation failed in {path.name}:{line_number}: unexpected closing brace"
                )
        if depth != 0:
            raise PatchRejected(f"CSS validation failed in {path.name}: unbalanced braces")

    def run_tests(self, command: str, timeout: int = 120) -> TestExecution:
        self.policy.validate_test_command(command)
        arguments = shlex.split(command, posix=os.name != "nt")
        environment = os.environ.copy()
        dependency_link: Path | None = None
        if os.name == "nt" and arguments and arguments[0] == "npm" and shutil.which("npm.cmd"):
            arguments[0] = "npm.cmd"
        if arguments and arguments[0] in {"npm", "npm.cmd"}:
            environment["CI"] = "true"
            if self.dependency_repository:
                node_modules = self.dependency_repository / "node_modules"
                binary_directory = node_modules / ".bin"
                if node_modules.is_dir():
                    environment["NODE_PATH"] = str(node_modules)
                    environment["PATH"] = f"{binary_directory}{os.pathsep}{environment.get('PATH', '')}"
                    workspace_modules = self.repository / "node_modules"
                    if not workspace_modules.exists():
                        if os.name == "nt":
                            linked = subprocess.run(
                                ["cmd.exe", "/d", "/c", "mklink", "/J", str(workspace_modules), str(node_modules)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                            )
                            if linked.returncode == 0:
                                dependency_link = workspace_modules
                        else:
                            os.symlink(node_modules, workspace_modules, target_is_directory=True)
                            dependency_link = workspace_modules
                        if dependency_link:
                            self.audit.append(ToolCallAudit(
                                tool="reuse_dependencies",
                                arguments={"source": str(node_modules)},
                                summary="测试期间复用原仓库依赖",
                            ))
        try:
            completed = subprocess.run(
                arguments, cwd=self.repository, env=environment, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, check=False,
            )
            outputs = ["[test]\n" + ((completed.stdout or "") + (completed.stderr or "")).strip()]
            exit_code = completed.returncode
            package_path = self.repository / "package.json"
            if exit_code == 0 and arguments and arguments[0] in {"npm", "npm.cmd"} and package_path.is_file():
                try:
                    package = json.loads(package_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    package = {}
                if "build" in package.get("scripts", {}):
                    build_command = [arguments[0], "run", "build"]
                    environment["GENERATE_SOURCEMAP"] = "false"
                    build = subprocess.run(
                        build_command, cwd=self.repository, env=environment,
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=max(timeout, 180), check=False,
                    )
                    outputs.append("[build]\n" + ((build.stdout or "") + (build.stderr or "")).strip())
                    exit_code = build.returncode
                    self.audit.append(ToolCallAudit(
                        tool="run_build", arguments={"command": build_command},
                        summary=f"构建退出码 {build.returncode}", success=build.returncode == 0,
                    ))
        finally:
            if dependency_link is not None:
                dependency_link.rmdir()
        output = "\n\n".join(outputs)[-60_000:]
        self.audit.append(ToolCallAudit(tool="run_tests", arguments={"command": arguments}, summary=f"验证退出码 {exit_code}", success=exit_code == 0))
        return TestExecution(arguments, exit_code, output)

    def diff(self) -> str:
        completed = subprocess.run(
            ["git", "diff", "--", "."], cwd=self.repository,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, check=True,
        )
        result = (completed.stdout or "").strip()
        self.audit.append(ToolCallAudit(tool="git_diff", summary=f"Diff 共 {len(result.splitlines())} 行"))
        return result

    def repair_malformed_css(self, paths: list[str]) -> list[str]:
        """Remove only unambiguous top-level CSS debris from approved files."""
        repaired: list[str] = []
        property_pattern = re.compile(r"^(?:--)?[-A-Za-z_][\w-]*\s*:")
        for relative in paths:
            if Path(relative).suffix.lower() not in {".css", ".scss"}:
                continue
            target = self.policy.resolve_write_path(relative)
            if not target.is_file():
                continue
            original = target.read_text(encoding="utf-8", errors="strict")
            try:
                self._validate_content(target, original)
                continue
            except PatchRejected:
                pass

            depth = 0
            dropping_orphan = False
            changed = False
            output: list[str] = []
            for raw in original.splitlines(keepends=True):
                stripped = raw.strip()
                if depth == 0 and property_pattern.match(stripped):
                    dropping_orphan = True
                    changed = True
                    continue
                if dropping_orphan and depth == 0:
                    if not stripped or property_pattern.match(stripped):
                        changed = True
                        continue
                    if stripped == "}":
                        dropping_orphan = False
                        changed = True
                        continue
                    dropping_orphan = False
                next_depth = depth + raw.count("{") - raw.count("}")
                if next_depth < 0 and stripped == "}":
                    changed = True
                    continue
                output.append(raw)
                depth = next_depth

            candidate = "".join(output)
            if not changed:
                continue
            self._validate_content(target, candidate)
            target.write_text(candidate, encoding="utf-8")
            repaired.append(relative)
            self.audit.append(ToolCallAudit(
                tool="repair_css_structure",
                arguments={"path": relative},
                summary="清理顶层孤立 CSS 属性和多余闭合括号",
            ))
        return repaired

    def restore_files(self, originals: dict[str, str], paths: list[str]) -> None:
        for relative in dict.fromkeys(paths):
            if relative not in originals:
                continue
            target = self.policy.resolve_write_path(relative)
            target.write_text(originals[relative], encoding="utf-8")
            self.audit.append(ToolCallAudit(
                tool="restore_file",
                arguments={"path": relative},
                summary="测试失败后恢复本次尝试前快照",
            ))
