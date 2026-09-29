"""代码仓库索引与混合检索实现。

增量提取文件、符号和依赖信息，并融合关键词与本地语义向量排名，为规划和开发提供相关上下文。
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import subprocess
from collections import Counter
from pathlib import Path

from domain.models import CodeEvidence, ContextFile, RepositoryContextPack


INDEX_VERSION = 2
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".scss"}
TEST_MARKERS = ("test", "spec", "__tests__")
SEMANTIC_ALIASES = {
    "身份": ["auth", "authentication", "identity", "session", "credential"],
    "校验": ["validate", "verify", "check", "guard"],
    "鉴权": ["auth", "authorization", "permission", "guard"],
    "登录": ["login", "signin", "auth", "session"],
    "导出": ["export", "download", "writer", "serialize"],
    "报表": ["report", "analytics", "summary"],
    "逗号分隔": ["csv", "comma", "delimiter"],
    "重试": ["retry", "backoff", "attempt"],
    "重新执行": ["retry", "rerun", "requeue"],
    "超时": ["timeout", "deadline", "expired"],
    "缓存": ["cache", "memo", "redis"],
    "弹窗": ["modal", "dialog", "alert", "popup"],
    "历史": ["history", "audit", "timeline", "record"],
}


class RepositoryCodeIndex:
    """Persistent, Git-aware code index used to build explainable task context."""

    def __init__(self, cache_root: Path | None = None):
        """初始化仓库代码索引器。

        Args:
            cache_root: 可选的持久化索引目录，用于按仓库及 HEAD 复用分析结果。
        """
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
        ranked_hits = self.rank_files(index, requirement, strategy="hybrid")
        scores = {item["path"]: int(round(item["score"] * 1000)) for item in ranked_hits}
        reasons: dict[str, list[str]] = {}
        for item in ranked_hits:
            reasons[item["path"]] = list(item["reasons"])
        ranked = [item["path"] for item in ranked_hits]
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
            retrieval_strategy="hybrid_rag_v1",
        ), evidence

    def rank_files(
        self,
        index: dict,
        requirement: str,
        *,
        strategy: str = "hybrid",
    ) -> list[dict]:
        """Rank indexed files while keeping the legacy scorer as an A/B baseline."""
        records: dict[str, dict] = index.get("files", {})
        lexical_scores, lexical_reasons = self._legacy_lexical_scores(records, requirement)
        if strategy == "lexical":
            return [
                {"path": path, "score": float(lexical_scores[path]), "reasons": lexical_reasons[path]}
                for path in sorted(lexical_scores, key=lambda value: (-lexical_scores[value], value))
            ]
        if strategy != "hybrid":
            raise ValueError(f"Unsupported retrieval strategy: {strategy}")

        semantic_scores = self._semantic_scores(records, requirement)
        lexical_rank = {
            path: rank for rank, path in enumerate(
                sorted(lexical_scores, key=lambda value: (-lexical_scores[value], value)), 1
            )
        }
        semantic_rank = {
            path: rank for rank, path in enumerate(
                sorted(semantic_scores, key=lambda value: (-semantic_scores[value], value)), 1
            )
        }
        candidates = set(lexical_scores) | set(semantic_scores)
        hits = []
        for path in candidates:
            # Weighted reciprocal-rank fusion is robust across the unrelated
            # score scales produced by exact matching and vector similarity.
            fused = 0.0
            reasons = list(lexical_reasons.get(path, []))
            if path in lexical_rank:
                fused += 0.55 / (20 + lexical_rank[path])
            if path in semantic_rank:
                fused += 0.45 / (20 + semantic_rank[path])
                reasons.append(f"语义向量相似度 {semantic_scores[path]:.3f}")
            hits.append({"path": path, "score": fused, "reasons": reasons})
        return sorted(hits, key=lambda item: (-item["score"], item["path"]))

    def _legacy_lexical_scores(
        self, records: dict[str, dict], requirement: str
    ) -> tuple[dict[str, int], dict[str, list[str]]]:
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
        return scores, reasons

    def _semantic_scores(self, records: dict[str, dict], requirement: str) -> dict[str, float]:
        query_tokens = self._semantic_query_tokens(requirement)
        if not query_tokens:
            return {}
        query_vector = self._hashed_vector(query_tokens)
        scores: dict[str, float] = {}
        for path, record in records.items():
            document = "\n".join([
                path,
                " ".join(record.get("symbols", [])),
                record.get("search_text", "")[:40_000],
            ])
            score = self._cosine(query_vector, self._hashed_vector(self._tokenize(document)))
            if score > 0:
                scores[path] = score
        return scores

    @classmethod
    def _semantic_query_tokens(cls, requirement: str) -> list[str]:
        tokens = cls._tokenize(requirement)
        lowered = requirement.lower()
        for phrase, aliases in SEMANTIC_ALIASES.items():
            if phrase in lowered:
                tokens.extend(aliases)
        return list(dict.fromkeys(tokens))

    @staticmethod
    def _tokenize(value: str) -> list[str]:
        value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
        english = re.findall(r"[A-Za-z_$][A-Za-z0-9_$-]{1,}", value.lower())
        chinese_segments = re.findall(r"[\u4e00-\u9fff]+", value)
        chinese = [
            segment[index:index + 2]
            for segment in chinese_segments
            for index in range(max(1, len(segment) - 1))
            if len(segment[index:index + 2]) >= 2
        ]
        return english + chinese

    @staticmethod
    def _hashed_vector(tokens: list[str], dimensions: int = 384) -> dict[int, float]:
        counts = Counter(tokens)
        vector: dict[int, float] = {}
        for token, frequency in counts.items():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % dimensions
            vector[bucket] = vector.get(bucket, 0.0) + 1 + math.log(frequency)
        return vector

    @staticmethod
    def _cosine(left: dict[int, float], right: dict[int, float]) -> float:
        left_norm = math.sqrt(sum(value * value for value in left.values()))
        right_norm = math.sqrt(sum(value * value for value in right.values()))
        if not left_norm or not right_norm:
            return 0.0
        dot = sum(value * right.get(key, 0.0) for key, value in left.items())
        return max(0.0, dot / (left_norm * right_norm))

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
