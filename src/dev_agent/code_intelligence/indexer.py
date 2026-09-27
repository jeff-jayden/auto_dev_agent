from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

from dev_agent.domain.models import CodeEvidence, ContextFile, RepositoryContextPack


INDEX_VERSION = 1
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".scss"}
TEST_MARKERS = ("test", "spec", "__tests__")


class RepositoryCodeIndex:
    """Persistent, Git-aware code index used to build explainable task context."""

    def __init__(self, cache_root: Path | None = None):
        self.cache_root = cache_root.resolve() if cache_root else None
        if self.cache_root:
            self.cache_root.mkdir(parents=True, exist_ok=True)

    @property
    def version(self) -> int:
        return INDEX_VERSION

    def build(
        self, repository: Path, files: list[str], head_sha: str | None
    ) -> tuple[dict, str, list[str]]:
        repository = repository.resolve()
        cached = self._load(repository)
        if (
            cached
            and cached.get("version") == INDEX_VERSION
            and head_sha is not None
            and cached.get("head_sha") == head_sha
        ):
            return cached, "cache_hit", []

        changed = self._changed_files(repository, cached, head_sha)
        if cached and changed is not None:
            records = dict(cached.get("files", {}))
            for relative in changed:
                target = repository / relative
                if relative not in files or not target.is_file():
                    records.pop(relative, None)
                else:
                    records[relative] = self._parse_file(repository, relative)
            mode = "incremental"
            changed_files = changed
        else:
            records = {
                relative: self._parse_file(repository, relative)
                for relative in files
                if Path(relative).suffix.lower() in SOURCE_SUFFIXES
            }
            mode = "full"
            changed_files = sorted(records)

        self._resolve_dependencies(records)
        result = {
            "version": INDEX_VERSION,
            "head_sha": head_sha,
            "repository": str(repository),
            "files": records,
        }
        self._save(repository, result)
        return result, mode, changed_files

    def context_pack(
        self,
        repository: Path,
        index: dict,
        requirement: str,
        entrypoints: list[str],
        mode: str,
        changed_files: list[str],
    ) -> tuple[RepositoryContextPack, list[CodeEvidence]]:
        records: dict[str, dict] = index.get("files", {})
        terms = self._requirement_terms(requirement)
        scores: dict[str, int] = {}
        reasons: dict[str, list[str]] = {}
        for path, record in records.items():
            score = 0
            matched = [term for term in terms if term in record.get("search_text", "")]
            if matched:
                score += min(20, len(matched) * 5)
                reasons.setdefault(path, []).append("匹配需求关键词：" + ", ".join(matched[:5]))
            symbol_matches = [
                symbol for symbol in record.get("symbols", [])
                if any(term in symbol.lower() for term in terms)
            ]
            if symbol_matches:
                score += min(15, len(symbol_matches) * 5)
                reasons.setdefault(path, []).append("命中代码符号：" + ", ".join(symbol_matches[:4]))
            name = Path(path).stem.lower()
            if any(term in name for term in terms):
                score += 6
                reasons.setdefault(path, []).append("文件名与需求相关")
            if score:
                scores[path] = score

        ranked = sorted(scores, key=lambda path: (-scores[path], path))
        primary = [path for path in ranked if not self._is_test(path)][:6]
        if not primary:
            primary = [path for path in entrypoints if path in records][:3]
        if not primary:
            primary = sorted(records)[:3]

        dependencies: list[str] = []
        tests: list[str] = [path for path in ranked if self._is_test(path)][:6]
        for path in primary:
            record = records.get(path, {})
            for related in [*record.get("imports", []), *record.get("referenced_by", [])]:
                if related in records and related not in primary and related not in dependencies:
                    dependencies.append(related)
                    reasons.setdefault(related, []).append(f"与 {path} 存在依赖关系")
            stem = Path(path).stem.lower().replace("index", "")
            for candidate in records:
                if self._is_test(candidate) and (stem in candidate.lower() or Path(path).name.lower() in records[candidate].get("search_text", "")):
                    if candidate not in tests:
                        tests.append(candidate)
                        reasons.setdefault(candidate, []).append(f"覆盖 {path} 的相关测试")
            for suffix in (".css", ".scss"):
                style = str(Path(path).with_suffix(suffix)).replace("\\", "/")
                if style in records and style not in dependencies:
                    dependencies.append(style)
                    reasons.setdefault(style, []).append(f"{path} 的同名样式文件")

        dependencies = [
            path for path in dependencies
            if not self._is_test(path) and path not in primary
        ][:8]
        tests = list(dict.fromkeys(tests + [p for p in dependencies if self._is_test(p)]))[:6]
        dependencies = [path for path in dependencies if path not in tests]
        selected = list(dict.fromkeys([*primary, *dependencies, *tests]))
        context_files = []
        for path in selected:
            record = records[path]
            role = "primary" if path in primary else "test" if path in tests else "dependency"
            context_files.append(ContextFile(
                path=path,
                role=role,
                score=scores.get(path, 0),
                reason="；".join(reasons.get(path, ["由仓库结构关联得到"])),
                symbols=record.get("symbols", [])[:20],
                imports=record.get("imports", [])[:20],
                referenced_by=record.get("referenced_by", [])[:20],
            ))
        evidence = self._evidence(repository, selected, terms, reasons)
        return RepositoryContextPack(
            baseline_sha=index.get("head_sha"),
            index_version=INDEX_VERSION,
            index_mode=mode,
            changed_files=changed_files[:100],
            primary_files=primary,
            dependency_files=dependencies,
            test_files=tests,
            files=context_files,
        ), evidence

    def _parse_file(self, repository: Path, relative: str) -> dict:
        target = repository / relative
        content = target.read_text(encoding="utf-8", errors="replace")[:512_000]
        suffix = target.suffix.lower()
        imports: list[str] = []
        symbols: list[str] = []
        if suffix == ".py":
            try:
                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        symbols.append(node.name)
                    elif isinstance(node, ast.Import):
                        imports.extend(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.append(node.module)
            except SyntaxError:
                pass
        else:
            imports.extend(re.findall(r"(?:from\s+|require\s*\(\s*)['\"]([^'\"]+)", content))
            imports.extend(re.findall(r"^\s*import\s+['\"]([^'\"]+)", content, flags=re.M))
            symbols.extend(re.findall(
                r"\b(?:function|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
                content,
            ))
            symbols.extend(re.findall(
                r"\b(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
                content,
            ))
        return {
            "hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "imports_raw": list(dict.fromkeys(imports))[:100],
            "imports": [],
            "referenced_by": [],
            "symbols": list(dict.fromkeys(symbols))[:200],
            "search_text": (relative + "\n" + content).lower()[:80_000],
        }

    def _resolve_dependencies(self, records: dict[str, dict]) -> None:
        for record in records.values():
            record["imports"] = []
            record["referenced_by"] = []
        for path, record in records.items():
            resolved = []
            for imported in record.get("imports_raw", []):
                candidate = self._resolve_import(path, imported, records)
                if candidate and candidate not in resolved:
                    resolved.append(candidate)
                    records[candidate]["referenced_by"].append(path)
            record["imports"] = resolved

    @staticmethod
    def _resolve_import(source: str, imported: str, records: dict[str, dict]) -> str | None:
        if imported.startswith("."):
            base = (Path(source).parent / imported).as_posix()
        else:
            base = imported.replace(".", "/")
        candidates = [
            base,
            *(base + suffix for suffix in (".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".scss")),
            *(f"{base}/index{suffix}" for suffix in (".js", ".jsx", ".ts", ".tsx")),
        ]
        return next((candidate for candidate in candidates if candidate in records), None)

    @staticmethod
    def _requirement_terms(requirement: str) -> list[str]:
        # Do not use ``\w`` for identifiers here: in Python it includes
        # Unicode letters, so text such as ``History页面`` was previously
        # indexed as one term and failed to match the standalone ``History``
        # identifier in source code.
        terms = [
            term.lower()
            for term in re.findall(
                r"[A-Za-z_$][A-Za-z0-9_$-]{2,}|[\u4e00-\u9fff]{2,}",
                requirement,
            )
        ]
        aliases = {
            "首页": ["app", "home", "index"], "按钮": ["button", "btn"],
            "登录": ["login", "auth"], "样式": ["css", "style"],
            "测试": ["test", "spec"], "弹窗": ["alert", "modal", "dialog"],
        }
        for key, values in aliases.items():
            if key in requirement:
                terms.extend(values)
        return list(dict.fromkeys(terms))[:30]

    @staticmethod
    def _is_test(path: str) -> bool:
        lowered = path.lower()
        return any(marker in lowered for marker in TEST_MARKERS)

    @staticmethod
    def _evidence(
        repository: Path,
        selected: list[str],
        terms: list[str],
        reasons: dict[str, list[str]],
    ) -> list[CodeEvidence]:
        result: list[CodeEvidence] = []
        for relative in selected:
            target = repository / relative
            if not target.is_file():
                continue
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            chosen = next((
                (number, line) for number, line in enumerate(lines, 1)
                if any(term in line.lower() for term in terms)
            ), (1, lines[0] if lines else ""))
            result.append(CodeEvidence(
                path=relative,
                line=chosen[0],
                snippet=chosen[1].strip()[:240],
                reason="；".join(reasons.get(relative, ["仓库依赖关系中的相关文件"])),
            ))
        return result[:16]

    def _changed_files(
        self, repository: Path, cached: dict | None, head_sha: str | None
    ) -> list[str] | None:
        if not cached or not cached.get("head_sha") or not head_sha:
            return None
        completed = subprocess.run(
            ["git", "diff", "--name-only", str(cached["head_sha"]), head_sha, "--"],
            cwd=repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            return None
        return sorted({line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()})

    def _cache_path(self, repository: Path) -> Path | None:
        if not self.cache_root:
            return None
        key = hashlib.sha256(str(repository.resolve()).lower().encode("utf-8")).hexdigest()[:20]
        return self.cache_root / f"{key}.json"

    def _load(self, repository: Path) -> dict | None:
        path = self._cache_path(repository)
        if not path or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _save(self, repository: Path, index: dict) -> None:
        path = self._cache_path(repository)
        if path:
            path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
