"""代码仓库索引与混合检索实现。

增量提取文件、符号和依赖信息，并融合关键词与本地语义向量排名，为规划和开发提供相关上下文。
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
from uuid import uuid4
from collections import Counter
from pathlib import Path

from domain.models import CodeEvidence, ContextFile, RepositoryContextPack
from code_intelligence.vector_store import CodeVectorStore, code_chunks


INDEX_VERSION = 4
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".css", ".scss"}
TEST_MARKERS = ("test", "spec", "__tests__")
LEXICAL_FIELD_WEIGHTS = {"path": 8.0, "symbols": 6.0, "imports": 3.0, "body": 1.0}
LEXICAL_MIN_SCORE = 1.5
SEMANTIC_CHUNK_CANDIDATES = 64
CONTEXT_CANDIDATE_LIMIT = 18
CONTEXT_DEPENDENCY_SEEDS = 8
GENERIC_ROLE_TERMS = {"video", "player", "watch", "channel", "page", "view", "screen", "file", "data"}
QUERY_STOP_TERMS = {
    "一个", "一些", "以及", "进行", "功能", "实现", "增加", "新增", "修改", "支持",
    "提供", "完成", "正确", "统一", "页面", "组件", "文件",
    "the", "and", "with", "from", "into", "page", "view", "screen", "component", "file", "data",
}
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
    "页面": ["page", "view", "screen"],
    "键盘快捷键": ["keyboard", "shortcut", "hotkey", "keydown", "keyup"],
    "键盘": ["keyboard", "keydown", "keyup", "key"],
    "快捷键": ["shortcut", "hotkey", "keydown"],
    "聚焦": ["focus", "activeelement"],
    "搜索": ["search", "query", "filter"],
    "返回": ["back", "navigate", "history"],
    "上传": ["upload", "publish", "file"],
    "错误边界": ["error", "boundary", "fallback", "recover"],
    "空状态": ["empty", "placeholder", "nothing", "noresults"],
    "内容目录": ["catalog", "dataset", "fixture", "videos", "channels"],
    "目录": ["catalog", "registry", "index"],
    "频道": ["channel", "subscription"],
    "视频": ["video", "player", "watch"],
    "工作台": ["studio", "dashboard", "workspace"],
    "全局状态": ["store", "context", "provider", "state"],
    "路由": ["route", "router", "navigate", "pathname"],
    "播放列表": ["playlist", "playlists", "collection"],
    "稍后观看": ["watchlater", "watch", "later"],
    "热门": ["trending", "popular"],
    "媒体库": ["library"],
    "统计面板": ["analytics", "studio", "dashboard"],
    "点赞": ["reaction", "reactions", "like"],
    "不喜欢": ["reaction", "reactions", "dislike"],
    "格式化": ["format", "formatter"],
    "侧边栏": ["sidebar", "navigation", "nav"],
    "过滤": ["filter", "filters", "select", "selector"],
    "删除": ["delete", "remove"],
    "编辑": ["edit", "update"],
    "评论": ["comment", "comments"],
    "网格": ["grid"],
    "顶部导航栏": ["header", "navbar", "topbar"],
    "响应式": ["responsive", "layout", "media"],
    "断点": ["breakpoint", "responsive", "media"],
    "个人资料": ["profile", "avatar", "account"],
    "首页": ["home", "landing"],
    "应用装配": ["app", "application", "bootstrap"],
}
CONTEXT_ROLE_ALIASES = {
    "键盘快捷键": ["keyboard", "usekeyboard"],
    "深浅主题": ["settings", "theme"],
    "隐私开关": ["settings", "privacy"],
    "订阅动态": ["subscriptions"],
    "频道主页": ["channel"],
    "相关推荐": ["recommendations"],
    "自动连播": ["recommendations", "autoplay"],
    "分页逻辑": ["pagination", "usepagination"],
    "搜索结果": ["filters", "selectvideos"],
    "侧边栏": ["sidebar"],
    "顶部导航栏": ["header"],
    "模态框": ["modal"],
    "视频评论": ["comments"],
}


class RepositoryCodeIndex:
    """Persistent, Git-aware code index used to build explainable task context."""

    def __init__(self, cache_root: Path | None = None, *, vector_store: CodeVectorStore | None = None):
        """初始化仓库代码索引器。

        Args:
            cache_root: 可选的持久化索引目录，用于按仓库及 HEAD 复用分析结果。
        """
        self.cache_root = cache_root.resolve() if cache_root else None
        self.vector_store = vector_store
        if self.cache_root:
            self.cache_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_environment(cls, cache_root: Path | None = None):
        """显式启用真实 RAG；服务错误直接报告，不能静默冒充向量检索。"""
        mode = os.getenv("RAG_RETRIEVAL_MODE", "legacy")
        if mode not in {"legacy", "embedding"}:
            raise ValueError("RAG_RETRIEVAL_MODE 只支持 legacy / embedding")
        if mode == "legacy":
            return cls(cache_root)
        from code_intelligence.embedding import EmbeddingClient
        root = Path(os.getenv("RAG_VECTOR_ROOT", str((cache_root.parent if cache_root else Path("runtime")) / "vectors")))
        embedding = EmbeddingClient.from_environment()
        server_url = os.getenv("RAG_CHROMA_URL")
        if os.getenv("EXECUTION_BACKEND") == "celery" and not server_url:
            raise ValueError("Celery 多进程使用真实 RAG 时必须配置 RAG_CHROMA_URL，不能共享内嵌 Chroma 目录")
        return cls(cache_root, vector_store=CodeVectorStore(root, embedding, server_url=server_url))

    @property
    def version(self) -> int:
        return INDEX_VERSION

    def build(
        self, repository: Path, files: list[str], head_sha: str | None
    ) -> tuple[dict, str, list[str]]:
        repository = repository.resolve()
        cached = self._load(repository)
        source_files = [relative for relative in files if Path(relative).suffix.lower() in SOURCE_SUFFIXES]
        hashes = {
            relative: hashlib.sha256((repository / relative).read_text(encoding="utf-8", errors="replace")[:512_000].encode()).hexdigest()
            for relative in source_files
        }
        if (
            cached
            and cached.get("version") == INDEX_VERSION
            and head_sha is not None
            and cached.get("head_sha") == head_sha
            and {path: record.get("hash") for path, record in cached.get("files", {}).items()} == hashes
        ):
            return cached, "cache_hit", []

        # HEAD 相同但工作区已修改/新增/删除时也必须重建相关块。
        changed = None
        if cached and cached.get("version") == INDEX_VERSION:
            old = cached.get("files", {})
            changed = sorted(path for path in set(old) | set(hashes) if old.get(path, {}).get("hash") != hashes.get(path))
        if cached and changed is not None:
            records = dict(cached.get("files", {}))
            for relative in changed:
                target = repository / relative
                if relative not in source_files or not target.is_file():
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
        raw_hits = self.rank_files(index, requirement, strategy="hybrid")
        ranked_hits = self.rank_context_files(index, requirement, ranked_hits=raw_hits)
        scores = {item["path"]: int(round(item["score"] * 1000)) for item in ranked_hits}
        reasons: dict[str, list[str]] = {}
        for item in ranked_hits:
            reasons[item["path"]] = list(item["reasons"])
        ranked = [item["path"] for item in ranked_hits]
        primary = self._primary_files(records, ranked, requirement)
        if not primary:
            primary = [path for path in entrypoints if path in records and not self._is_test(path)][:1]
        if not primary:
            primary = [path for path in sorted(records) if not self._is_test(path)][:1]
        for path in primary:
            reasons.setdefault(path, []).append("需求目标匹配的主要实现候选；不代表必须修改或已批准可写")

        dependencies: list[str] = []
        tests: list[str] = [path for path in ranked if self._is_test(path)][:6]
        for path in primary:
            record = records.get(path, {})
            for related in [*record.get("imports", []), *record.get("referenced_by", [])]:
                if related in records and related not in primary:
                    if self._is_test(related):
                        if related not in tests:
                            tests.append(related)
                    elif related not in dependencies:
                        dependencies.append(related)
                    reasons.setdefault(related, []).append(f"与 {path} 存在依赖关系")
            stem = Path(path).stem.lower().replace("index", "")
            for candidate in records:
                if self._is_test(candidate) and ((stem and stem in candidate.lower()) or Path(path).name.lower() in records[candidate].get("search_text", "")):
                    if candidate not in tests:
                        tests.append(candidate)
                        reasons.setdefault(candidate, []).append(f"覆盖 {path} 的相关测试")
            for suffix in (".css", ".scss"):
                style = str(Path(path).with_suffix(suffix)).replace("\\", "/")
                if style in records and style not in dependencies:
                    dependencies.append(style)
                    reasons.setdefault(style, []).append(f"{path} 的同名样式文件")

        # 检索候选不等于核心实现。保留其余召回作为辅助上下文，避免缩小可读范围。
        for path in entrypoints[:3]:
            if path in records and path not in primary and not self._is_test(path) and path not in dependencies:
                dependencies.append(path)
                reasons.setdefault(path, []).append("只读入口上下文；不默认修改入口")
        for path in ranked[:6]:
            if path not in primary and not self._is_test(path) and path not in dependencies:
                dependencies.append(path)
                reasons.setdefault(path, []).append("检索召回的辅助上下文，不默认纳入修改范围")
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
        raw_by_path = {item["path"]: item for item in raw_hits}
        chunk_evidence = [
            CodeEvidence(path=hit["path"], line=chunk["start_line"], snippet=chunk["text"],
                         reason=f"真实向量检索 {chunk['symbol']}，行 {chunk['start_line']}-{chunk['end_line']}，相似度 {chunk['score']:.3f}")
            for hit in (raw_by_path[path] for path in selected if path in raw_by_path)
            for chunk in hit.get("chunks", [])[:2]
        ]
        if chunk_evidence:
            evidence = (chunk_evidence + [item for item in evidence if item.path not in {chunk.path for chunk in chunk_evidence}])[:16]
        return RepositoryContextPack(
            baseline_sha=index.get("head_sha"),
            index_version=INDEX_VERSION,
            index_mode=mode,
            changed_files=changed_files[:100],
            primary_files=primary,
            dependency_files=dependencies,
            test_files=tests,
            files=context_files,
            retrieval_strategy="embedding_chroma_rrf_v1" if self.vector_store else "hybrid_rag_v1",
        ), evidence

    def _primary_files(self, records: dict, ranked: list[str], requirement: str) -> list[str]:
        """用需求指向区分主要实现和辅助文件；无直接命中时只保留最高排名实现候选。

        文件名指向优先于符号指向，避免父组件引用目标符号就一起被列为核心。
        这是可解释的启发式分类，不把 RRF 排名分数当作置信度，也不决定可写范围。
        """
        normalized = requirement.lower().replace("\\", "/")
        compact = re.sub(r"[^\w]", "", normalized)
        query_terms = {
            term for term, weight in self._lexical_query_terms(requirement).items()
            if weight > 1.0 and term not in GENERIC_ROLE_TERMS
        }
        candidates = [path for path in ranked if path in records and not self._is_test(path)]
        explicit = [path for path in candidates if re.search(
            r"(?<![\w./-])" + re.escape(path.lower()) + r"(?![\w./-])", normalized
        ) or re.search(r"(?<![\w.-])" + re.escape(Path(path).name.lower()) + r"(?![\w.-])", normalized)]
        styles = {".css", ".scss"}
        wants_styles = any(term in requirement for term in ("样式", "主题", "响应式", "断点", "布局"))
        generic = {"app", "index", "main", "server", "utils", "helpers", "common", "styles"}

        def matches_target(path: str) -> bool:
            stem = re.sub(r"[^\w]", "", Path(path).stem.lower())
            target = re.sub(r"(?:page|panel|view|component|service|controller|handler)$", "", stem)
            return any(len(value) >= 4 and value not in generic and value in compact for value in (stem, target))

        named = [path for path in candidates if (Path(path).suffix.lower() not in styles or wants_styles)
                 and (matches_target(path) or query_terms & set(self._tokenize(Path(path).stem))
                      or query_terms & {term for symbol in records[path].get("symbols", []) for term in self._tokenize(symbol)})]
        if explicit or named:
            return list(dict.fromkeys([*explicit, *named]))[:6]
        symbols = [path for path in candidates if Path(path).suffix.lower() not in styles and any(
            len(symbol) >= 4 and symbol.lower() not in generic
            and re.sub(r"[^\w]", "", symbol.lower()) in compact
            for symbol in records[path].get("symbols", [])
        )]
        if symbols:
            return symbols[:3]
        return candidates[:1]

    def rank_files(
        self,
        index: dict,
        requirement: str,
        *,
        strategy: str = "hybrid",
    ) -> list[dict]:
        """Rank indexed files with fielded lexical retrieval and optional semantic fusion."""
        records: dict[str, dict] = index.get("files", {})
        lexical_scores, lexical_reasons = self._lexical_scores(records, requirement)
        if strategy == "lexical":
            return [
                {"path": path, "score": float(lexical_scores[path]), "reasons": lexical_reasons[path]}
                for path in sorted(lexical_scores, key=lambda value: (-lexical_scores[value], value))
            ]
        if strategy != "hybrid":
            raise ValueError(f"Unsupported retrieval strategy: {strategy}")

        chunks_by_path: dict[str, list[dict]] = {}
        if self.vector_store:
            for chunk in self.vector_store.search(index, requirement, k=SEMANTIC_CHUNK_CANDIDATES):
                chunks_by_path.setdefault(chunk["path"], []).append(chunk)
            semantic_scores = {path: max(chunk["score"] for chunk in chunks) for path, chunks in chunks_by_path.items()}
        else:
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
                prefix = "Embedding 代码块语义向量" if self.vector_store else "本地哈希语义向量"
                reasons.append(f"{prefix}相似度 {semantic_scores[path]:.3f}")
            hits.append({"path": path, "score": fused, "reasons": reasons, "chunks": chunks_by_path.get(path, [])})
        return sorted(hits, key=lambda item: (-item["score"], item["path"]))

    def rank_context_files(
        self,
        index: dict,
        requirement: str,
        *,
        limit: int | None = None,
        ranked_hits: list[dict] | None = None,
    ) -> list[dict]:
        """扩大原始候选，沿代码关系补充文件，再按目标角色统一重排。

        词法/哈希基线和真实 Embedding 都调用这一阶段；传入 ``ranked_hits`` 可避免
        对远程向量服务发起第二次相同查询。返回结果仍保留原始 RRF 理由与代码块证据。
        """
        records: dict[str, dict] = index.get("files", {})
        raw = ranked_hits if ranked_hits is not None else self.rank_files(index, requirement, strategy="hybrid")
        if not raw:
            return []
        candidate_limit = max(CONTEXT_CANDIDATE_LIMIT, (limit or 0) * 4)
        raw = raw[:candidate_limit]
        by_path = {item["path"]: {**item, "reasons": list(item.get("reasons", []))} for item in raw}
        raw_rank = {item["path"]: rank for rank, item in enumerate(raw, 1)}
        relation_scores: dict[str, float] = {}

        for seed_rank, item in enumerate(raw[:max(CONTEXT_DEPENDENCY_SEEDS, (limit or 0) * 2)], 1):
            seed = item["path"]
            record = records.get(seed, {})
            for relation, weight in (("imports", 0.16), ("referenced_by", 0.12)):
                for related in record.get(relation, []):
                    if related not in records or related == seed:
                        continue
                    relation_scores[related] = max(relation_scores.get(related, 0.0), weight / seed_rank)
                    if related not in by_path:
                        by_path[related] = {"path": related, "score": 0.0, "reasons": [], "chunks": []}
                    label = "依赖" if relation == "imports" else "调用方"
                    reason = f"由候选 {seed} 扩展的{label}"
                    if reason not in by_path[related]["reasons"]:
                        by_path[related]["reasons"].append(reason)
            stem = Path(seed).stem.lower().replace("index", "")
            for related in records:
                if self._is_test(related) and ((stem and stem in related.lower()) or Path(seed).name.lower() in records[related].get("search_text", "")):
                    relation_scores[related] = max(relation_scores.get(related, 0.0), 0.1 / seed_rank)
                    by_path.setdefault(related, {"path": related, "score": 0.0, "reasons": [], "chunks": []})
                    reason = f"覆盖候选 {seed} 的测试"
                    if reason not in by_path[related]["reasons"]:
                        by_path[related]["reasons"].append(reason)
            for suffix in (".css", ".scss"):
                related = str(Path(seed).with_suffix(suffix)).replace("\\", "/")
                if related in records:
                    relation_scores[related] = max(relation_scores.get(related, 0.0), 0.1 / seed_rank)
                    by_path.setdefault(related, {"path": related, "score": 0.0, "reasons": [], "chunks": []})
                    reason = f"候选 {seed} 的同名样式"
                    if reason not in by_path[related]["reasons"]:
                        by_path[related]["reasons"].append(reason)

        paths = list(by_path)
        primary = set(self._primary_files(records, paths, requirement))
        query_terms = set(self._lexical_query_terms(requirement))
        wants_tests = any(term in requirement.lower() for term in ("测试", "回归", "test", "spec"))
        reranked = []
        for path, item in by_path.items():
            rank_score = 1.0 / (4 + raw_rank[path]) if path in raw_rank else 0.0
            path_terms = set(self._tokenize(path))
            symbol_terms = {term for symbol in records[path].get("symbols", []) for term in self._tokenize(symbol)}
            overlap = len(query_terms & (path_terms | symbol_terms))
            role_score = 0.3 if path in primary else 0.0
            # 多个近义动作词（如 delete/remove）只算一次，避免通用动作压过业务实体。
            role_score += 0.06 if overlap else 0.0
            role_score += min(0.03, self._lexical_role_bonus(path, requirement) * 0.01)
            role_score += self._context_role_bonus(path, records[path], requirement)
            if self._is_test(path):
                role_score += 0.3 if wants_tests else -0.08
            score = rank_score + relation_scores.get(path, 0.0) + role_score
            role = "primary" if path in primary else "test" if self._is_test(path) else "dependency" if path in relation_scores else "candidate"
            reasons = list(item.get("reasons", []))
            if path in primary:
                reasons.append("文件路径或符号与需求目标角色匹配")
            reranked.append({**item, "score": score, "reasons": list(dict.fromkeys(reasons)), "context_role": role})
        result = sorted(reranked, key=lambda item: (-item["score"], raw_rank.get(item["path"], 10_000), item["path"]))
        return result[:limit] if limit is not None else result

    def _lexical_scores(
        self, records: dict[str, dict], requirement: str
    ) -> tuple[dict[str, float], dict[str, list[str]]]:
        """字段化 BM25：精确路径/符号优先，正文命中降权，并过滤弱候选。"""
        query_terms = self._lexical_query_terms(requirement)
        if not query_terms:
            return {}, {}
        documents = {path: self._lexical_fields(path, record) for path, record in records.items()}
        field_lengths = {
            field: [sum(document[field].values()) for document in documents.values()]
            for field in LEXICAL_FIELD_WEIGHTS
        }
        average_lengths = {
            field: (sum(lengths) / len(lengths) if lengths else 1.0)
            for field, lengths in field_lengths.items()
        }
        document_frequency = {
            term: sum(any(term in document[field] for field in LEXICAL_FIELD_WEIGHTS)
                      for document in documents.values())
            for term in query_terms
        }
        total_documents = max(1, len(documents))
        scores: dict[str, float] = {}
        reasons: dict[str, list[str]] = {}
        normalized_requirement = requirement.lower().replace("\\", "/")
        for path, document in documents.items():
            score = 0.0
            matched_by_field: dict[str, list[str]] = {}
            for term, query_weight in query_terms.items():
                frequency = document_frequency[term]
                if not frequency:
                    continue
                inverse_frequency = math.log(1 + (total_documents - frequency + 0.5) / (frequency + 0.5))
                for field, field_weight in LEXICAL_FIELD_WEIGHTS.items():
                    term_frequency = document[field].get(term, 0)
                    if not term_frequency:
                        continue
                    length = max(1, sum(document[field].values()))
                    average = max(1.0, average_lengths[field])
                    normalized_frequency = (
                        term_frequency * 2.2
                        / (term_frequency + 1.2 * (0.25 + 0.75 * length / average))
                    )
                    score += query_weight * field_weight * inverse_frequency * normalized_frequency
                    matched_by_field.setdefault(field, []).append(term)

            filename = Path(path).name.lower()
            if path.lower() in normalized_requirement or filename in normalized_requirement:
                score += 12.0
                reasons.setdefault(path, []).append("需求明确指定文件路径或文件名")
            role_bonus = self._lexical_role_bonus(path, requirement) if score > 0 else 0.0
            if role_bonus:
                score += role_bonus
                reasons.setdefault(path, []).append("文件角色与需求类型匹配")
            strong_terms = set(matched_by_field.get("path", [])) | set(matched_by_field.get("symbols", []))
            if len(strong_terms) >= 2:
                score += 2.0
                reasons.setdefault(path, []).append("路径或符号命中多个组合词")
            if score < LEXICAL_MIN_SCORE:
                continue
            for field, label in (("path", "路径"), ("symbols", "代码符号"),
                                 ("imports", "导入"), ("body", "正文")):
                matched = list(dict.fromkeys(matched_by_field.get(field, [])))
                if matched:
                    reasons.setdefault(path, []).append(f"BM25 {label}命中：" + ", ".join(matched[:6]))
            scores[path] = round(score, 6)
        return scores, reasons

    @classmethod
    def _lexical_query_terms(cls, requirement: str) -> dict[str, float]:
        terms = {term: 1.0 for term in cls._tokenize(requirement)
                 if term not in QUERY_STOP_TERMS and len(term) >= 2}
        lowered = requirement.lower()
        for phrase, aliases in SEMANTIC_ALIASES.items():
            if phrase in lowered:
                for alias in aliases:
                    for term in cls._tokenize(alias):
                        terms[term] = max(terms.get(term, 0.0), 1.35)
        return dict(list(terms.items())[:80])

    @classmethod
    def _lexical_fields(cls, path: str, record: dict) -> dict[str, Counter]:
        cached = record.get("lexical_fields")
        if cached:
            return {field: Counter(cached.get(field, {})) for field in LEXICAL_FIELD_WEIGHTS}
        return {
            "path": Counter(cls._tokenize(path)),
            "symbols": Counter(term for symbol in record.get("symbols", []) for term in cls._tokenize(symbol)),
            "imports": Counter(term for imported in record.get("imports_raw", []) for term in cls._tokenize(imported)),
            "body": Counter(cls._tokenize(record.get("search_text", "")[:40_000])),
        }

    @staticmethod
    def _lexical_role_bonus(path: str, requirement: str) -> float:
        lowered = requirement.lower()
        name = Path(path).stem.lower()
        suffix = Path(path).suffix.lower()
        bonus = 0.0
        if "页面" in requirement and re.search(r"(?:page|view|screen)$", name):
            bonus += 2.5
        if any(term in requirement for term in ("测试", "回归")) and any(marker in path.lower() for marker in TEST_MARKERS):
            bonus += 3.0
        if any(term in requirement for term in ("样式", "主题", "响应式")) and suffix in {".css", ".scss"}:
            bonus += 3.0
        if "全局状态" in requirement and any(term in name for term in ("store", "context", "provider")):
            bonus += 3.0
        if "路由" in requirement and any(term in name for term in ("route", "router", "app", "application")):
            bonus += 2.5
        if "组件" in requirement and "/shared/" in path.lower():
            bonus += 1.5
        if "test" in lowered and any(marker in path.lower() for marker in TEST_MARKERS):
            bonus += 3.0
        return bonus

    @classmethod
    def _context_role_bonus(cls, path: str, record: dict, requirement: str) -> float:
        """只影响最终上下文排序的高精度复合意图，不改变原始召回。"""
        lowered = requirement.lower()
        file_terms = set(cls._tokenize(Path(path).stem))
        file_terms.update(
            term for symbol in record.get("symbols", []) for term in cls._tokenize(symbol)
        )
        bonus = 0.0
        for phrase, aliases in CONTEXT_ROLE_ALIASES.items():
            if phrase not in lowered:
                continue
            alias_terms = {term for alias in aliases for term in cls._tokenize(alias)}
            if file_terms & alias_terms:
                # 这些映射只包含高精度复合意图，可以安全地压过“搜索/删除”等泛化邻居。
                bonus = max(bonus, 0.34)
        return bonus

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
        value = value.replace("_", " ").replace("-", " ").replace("/", " ").replace("\\", " ")
        english = re.findall(r"[A-Za-z$][A-Za-z0-9$]{1,}", value.lower())
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
            "lexical_fields": {
                "path": dict(Counter(self._tokenize(relative))),
                "symbols": dict(Counter(term for symbol in symbols for term in self._tokenize(symbol))),
                "imports": dict(Counter(term for imported in imports for term in self._tokenize(imported))),
                "body": dict(Counter(self._tokenize(content[:40_000]))),
            },
            "chunks": code_chunks(relative, content),
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
        terms = RepositoryCodeIndex._tokenize(requirement)
        for phrase, aliases in SEMANTIC_ALIASES.items():
            if phrase in requirement.lower():
                terms.extend(term for alias in aliases for term in RepositoryCodeIndex._tokenize(alias))
        return [term for term in dict.fromkeys(terms) if term not in QUERY_STOP_TERMS][:80]

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
            temporary = path.with_suffix(f".{uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
