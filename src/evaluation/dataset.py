"""RAG 人工标注闭环：从任务/合入 PR 取候选，确认标签后在开发前只读快照上评测。"""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from code_intelligence.indexer import RepositoryCodeIndex, SOURCE_SUFFIXES
from evaluation.retrieval import RetrievalCase, RetrievalEvaluator, evaluate_repository


def now() -> str:
    return datetime.now(UTC).isoformat()


class DatasetImportRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    merged_only: bool = True
    repository_id: str | None = None


class BuiltinDatasetImportRequest(BaseModel):
    repository_id: str | None = None


class AnnotationRequest(BaseModel):
    revision: int = Field(ge=1)
    query: str = Field(min_length=3, max_length=8000)
    actor: str = Field(min_length=2, max_length=80)
    state: Literal["pending", "confirmed", "excluded"] = "confirmed"
    split: Literal["dev", "test"] = "dev"
    labels: dict[str, Literal["required", "supporting", "irrelevant", "unreviewed"]] = Field(max_length=200)
    note: str = Field(default="", max_length=2000)


class DatasetEvaluationRequest(BaseModel):
    split: Literal["dev", "test"] = "dev"
    k: int = Field(default=3, ge=1, le=20)
    repository_id: str | None = None


class DatasetConflict(ValueError):
    pass


class RagDatasetService:
    def __init__(self, store, github=None, *, runtime_root: Path, indexer_factory=None,
                 builtin_dataset_path: Path | None = None):
        self.store = store
        self.github = github
        self.runtime = runtime_root.resolve()
        self.indexer_factory = indexer_factory or (lambda: RepositoryCodeIndex.from_environment(self.runtime / "indexes"))
        self.builtin_dataset_path = builtin_dataset_path or (
            Path(__file__).resolve().parent / "datasets" / "cfe_unit_commit_rag_cases.json"
        )
        self._evaluation_lock = threading.Lock()

    @staticmethod
    def _git(repository: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=repository, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
        if result.returncode:
            raise ValueError("本地 Git 历史中找不到对应基线，请恢复仓库历史后再标注/评测")
        return result.stdout.strip()

    def _base_files(self, repository: Path, sha: str) -> tuple[str, list[str]]:
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha or ""):
            raise ValueError("缺少有效的开发前 Git 基线，不能使用当前 HEAD 代替")
        full = self._git(repository, "rev-parse", "--verify", f"{sha}^{{commit}}")
        files = [path for path in self._git(repository, "ls-tree", "-rz", "--name-only", full).split("\0") if path]
        return full, files

    @staticmethod
    def _safe_path(path: str) -> bool:
        value = PurePosixPath(path)
        return bool(path) and not any(ord(char) < 32 for char in path) and not value.is_absolute() and ".." not in value.parts and "\\" not in path and ":" not in path and not any(part.startswith(".env") or part == ".git" for part in value.parts)

    def overview(self) -> dict:
        cases = self.store.list_rag_cases()
        return {"cases": cases, "counts": {state: sum(case["state"] == state for case in cases)
                                          for state in ("pending", "confirmed", "excluded")},
                "latest_run": self.store.latest_rag_dataset_run()}

    @staticmethod
    def _indexable(path: str) -> bool:
        from repository.analyzer import IGNORED_DIRECTORIES
        return Path(path).suffix.lower() in SOURCE_SUFFIXES and not any(part in IGNORED_DIRECTORIES for part in PurePosixPath(path).parts)

    def import_history(self, request: DatasetImportRequest) -> dict:
        created, skipped, errors = [], [], []
        existing = {case["source_task_id"] for case in self.store.list_rag_cases()}
        tasks = [task for task in self.store.list_tasks() if not request.repository_id or task.repository_id == request.repository_id]
        if request.merged_only:
            tasks = [task for task in tasks if task.remote_pull_request and (
                task.remote_pull_request.state == "merged" or task.remote_pull_request.merged_sha or task.status == "merged")]
        skipped = [task.id for task in tasks if task.id in existing]
        tasks = [task for task in tasks if task.id not in existing][:request.limit]
        for task in tasks:
            if task.id in existing:
                skipped.append(task.id)
                continue
            try:
                case = self._candidate(task, request.merged_only)
                if self.store.save_rag_case(case):
                    created.append(case["id"])
                else:
                    skipped.append(task.id)
            except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                errors.append({"task_id": task.id, "title": task.title, "reason": str(error)[:600]})
        return {"created": created, "skipped": skipped, "errors": errors,
                "message": "候选仅供人工审核；重复导入不会覆盖已有标注"}

    def import_builtin(self, request: BuiltinDatasetImportRequest) -> dict:
        """将随项目提供的 cfe-unit 提交数据集导入人工标注表。"""
        try:
            manifest = json.loads(self.builtin_dataset_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("内置 cfe-unit RAG 数据集不可用") from error

        pinned_sha = str(manifest.get("repository_head_sha", ""))
        repositories = ([self.store.get_repository(request.repository_id)] if request.repository_id
                        else self.store.list_repositories())
        repositories = [repository for repository in repositories
                        if repository is not None and Path(repository.local_path).is_dir()]
        matches = []
        for repository in repositories:
            try:
                full_sha, files = self._base_files(Path(repository.local_path), pinned_sha)
            except ValueError:
                continue
            matches.append((repository, full_sha, set(files)))
        if not matches:
            detail = "所选仓库" if request.repository_id else "已注册仓库"
            raise ValueError(f"{detail}不包含内置数据集固定的 cfe-unit 代码快照")
        if len(matches) > 1:
            raise ValueError("多个仓库包含该快照，请先在页面选择要评测的 cfe-unit 仓库")

        repository, full_sha, available = matches[0]
        dataset_name = str(manifest.get("name", "cfe-unit-commit-file-recall-v1"))
        irrelevant = [path for path in manifest.get("known_irrelevant_files", [])
                      if self._safe_path(path) and path in available and self._indexable(path)]
        existing = {case["source_task_id"] for case in self.store.list_rag_cases()}
        created, skipped, errors = [], [], []
        for item in manifest.get("cases", []):
            requirement_id = str(item.get("requirement_id", ""))
            query = requirement_id
            source_id = f"builtin:{dataset_name}:{requirement_id}:{repository.id}"
            if source_id in existing:
                skipped.append(source_id)
                continue
            try:
                query = str(item["query"]).strip()
                expected = list(dict.fromkeys(item["expected_files"]))
                invalid = [path for path in expected
                           if not self._safe_path(path) or path not in available or not self._indexable(path)]
                if not query or not expected or invalid:
                    raise ValueError(f"固定快照中缺少有效答案文件：{', '.join(invalid) or '未提供答案'}")
                labels = [(path, "required", "builtin_commit_gold") for path in expected]
                labels.extend((path, "irrelevant", "builtin_negative") for path in irrelevant if path not in expected)
                timestamp = now()
                case = {
                    "id": hashlib.sha256(source_id.encode()).hexdigest()[:24],
                    "source_task_id": source_id,
                    "group_id": source_id,
                    "title": f"{requirement_id} {query}",
                    "original_requirement": query,
                    "query": query,
                    "repository_id": repository.id,
                    "repository_name": repository.name,
                    "base_sha": full_sha,
                    "pr_url": None,
                    "pr_number": None,
                    "sources": ["builtin_commit_dataset"],
                    "warnings": ["内置提交召回集使用最终完整代码快照，不代表单条需求的开发前基线。"],
                    "candidates": [{"path": path, "sources": [source], "exists_at_base": True,
                                    "indexable": True, "label": label}
                                   for path, label, source in labels],
                    "state": "confirmed",
                    "split": "test",
                    "revision": 1,
                    "actor": "内置提交数据集",
                    "reviewed_at": timestamp,
                    "note": f"来源提交 {item.get('source_commit', '')}",
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
                if self.store.save_rag_case(case):
                    created.append(case["id"])
                else:
                    skipped.append(source_id)
            except (KeyError, TypeError, ValueError) as error:
                errors.append({"requirement_id": requirement_id, "title": query,
                               "reason": str(error)[:600]})
        return {"created": created, "skipped": skipped, "errors": errors,
                "repository_id": repository.id,
                "message": "内置样本已确认并归入测试集；重复导入不会覆盖页面中的现有标注"}

    def _candidate(self, task, merged_only: bool) -> dict:
        repository = self.store.get_repository(task.repository_id) if task.repository_id else None
        if repository is None or not Path(repository.local_path).is_dir():
            raise ValueError("关联的本地仓库不存在")
        pull = task.remote_pull_request
        warnings, provenance, sources = [], {}, []

        def add(path, source):
            if self._safe_path(path):
                provenance.setdefault(path, set()).add(source)

        baseline = task.repository_analysis.head_sha if task.repository_analysis else None
        if pull:
            if repository.github_repository and repository.github_repository != pull.repository:
                raise ValueError("任务 PR 与注册仓库不一致")
            if self.github and self.github.enabled:
                remote = self.github.get_pull_request(pull.repository, pull.number)
                if remote.get("merged"):
                    for item in self.github.list_pull_request_files(pull.repository, pull.number):
                        add(item["filename"], "merged_pr")
                        if item.get("previous_filename"):
                            add(item["previous_filename"], "renamed_from")
                    sources.append("github_merged_pr")
                    # PR 合入后的 base.sha 可能已包含答案，不可代替任务开发前基线。
                elif merged_only:
                    raise ValueError("GitHub 确认该 PR 尚未合入，未导入")
            else:
                warnings.append("GitHub 未配置：仅从本地历史生成候选，尚未核实远端 PR 文件")
        if task.merge_request:
            for path in task.merge_request.changed_files:
                add(path, "local_mr")
        if task.technical_plan:
            for path in task.technical_plan.affected_files:
                add(path, "technical_plan")
        for attempt in task.development_attempts:
            for path in attempt.changed_files:
                add(path, "development_attempt")
        full_sha, base_files = self._base_files(Path(repository.local_path), baseline or "")
        available = set(base_files)
        if not provenance:
            raise ValueError("任务没有可提取的候选文件")
        candidates = [{"path": path, "sources": sorted(origin), "exists_at_base": path in available,
                       "indexable": self._indexable(path),
                       "label": "unreviewed"} for path, origin in sorted(provenance.items())]
        if len(candidates) > 200:
            warnings.append("候选超过 200 个，已截断；请补充必要文件或排除此样本")
        timestamp = now()
        return {"id": hashlib.sha256(task.id.encode()).hexdigest()[:24], "source_task_id": task.id,
                "group_id": task.metadata.get("followup_of") or task.id, "title": task.title, "original_requirement": task.requirement,
                "query": task.requirement, "repository_id": repository.id, "repository_name": repository.name,
                "base_sha": full_sha, "pr_url": pull.url if pull else None,
                "pr_number": pull.number if pull else None, "sources": sources or ["local_history"],
                "warnings": warnings, "candidates": candidates[:200], "state": "pending", "split": "dev",
                "revision": 1, "actor": None, "reviewed_at": None, "note": "",
                "created_at": timestamp, "updated_at": timestamp}

    def annotate(self, case_id: str, request: AnnotationRequest) -> dict:
        case = next((case for case in self.store.list_rag_cases() if case["id"] == case_id), None)
        if case is None:
            raise KeyError("找不到该标注样本")
        if case["revision"] != request.revision:
            raise DatasetConflict("标注已被其他页面更新，请刷新后再保存")
        if not request.actor.strip() or not request.query.strip():
            raise ValueError("需求描述和标注人不能为空")
        if request.state == "confirmed":
            for other in self.store.list_rag_cases():
                if other["id"] != case_id and other["state"] == "confirmed" and other["split"] != request.split and other["repository_id"] == case["repository_id"] and (other["query"].strip() == request.query.strip() or other["group_id"] == case["group_id"]):
                    raise ValueError("同仓库相同需求及关联后续需求不能跨开发集/测试集重复，避免数据泄漏")
        repository = self.store.get_repository(case["repository_id"])
        if repository is None:
            raise ValueError("仓库已不存在")
        _, base_files = self._base_files(Path(repository.local_path), case["base_sha"])
        available = set(base_files)
        if request.state == "confirmed" and "required" not in request.labels.values():
            raise ValueError("确认样本至少需要标注一个必要文件")
        if request.state == "confirmed" and "unreviewed" in request.labels.values():
            raise ValueError("请先处理所有候选文件的标注，不能带未审核项确认")
        known = {item["path"]: item for item in case["candidates"]}
        if not set(known) <= set(request.labels):
            raise ValueError("不能省略已有候选；不相关文件请明确标记无关")
        for path, label in request.labels.items():
            if not self._safe_path(path):
                raise ValueError("文件路径不合法")
            if label in {"required", "supporting"} and (path not in available or not self._indexable(path)):
                raise ValueError(f"{path} 在开发前不存在或不在当前代码索引范围内，不能标记为相关")
            known.setdefault(path, {"path": path, "sources": ["human_added"], "exists_at_base": path in available,
                                    "indexable": self._indexable(path)})
            known[path]["label"] = label
        case.update(query=request.query.strip(), state=request.state, split=request.split,
                    candidates=list(known.values()), actor=request.actor.strip(), note=request.note,
                    reviewed_at=now() if request.state == "confirmed" else None,
                    revision=case["revision"] + 1, updated_at=now())
        if not self.store.save_rag_case(case, request.revision):
            raise DatasetConflict("标注保存冲突，请刷新后重试")
        return case

    def baseline_files(self, case_id: str) -> list[str]:
        case = next((case for case in self.store.list_rag_cases() if case["id"] == case_id), None)
        if case is None:
            raise KeyError("样本不存在")
        repository = self.store.get_repository(case["repository_id"])
        if repository is None:
            raise ValueError("仓库不存在")
        _, files = self._base_files(Path(repository.local_path), case["base_sha"])
        return [path for path in files if self._safe_path(path) and self._indexable(path)]

    def evaluate(self, request: DatasetEvaluationRequest) -> dict:
        cases = [case for case in self.store.list_rag_cases()
                 if case["state"] == "confirmed" and case["split"] == request.split
                 and (not request.repository_id or case["repository_id"] == request.repository_id)]
        if not cases:
            raise ValueError("所选分组没有人工确认的样本，请先完成标注")
        if not self._evaluation_lock.acquire(blocking=False):
            raise DatasetConflict("已有数据集评测正在执行，请等待完成")
        try:
            return self._evaluate(cases, request)
        finally:
            self._evaluation_lock.release()

    def _evaluate(self, cases: list[dict], request) -> dict:
        engine = self.indexer_factory()
        rows, details, errors, latencies = [], [], [], {"baseline": [], "hybrid": []}
        report = {"id": uuid4().hex, "created_at": now(), "split": request.split, "k": request.k,
                  "requested_count": len(cases), "dataset_snapshot": cases,
                  "baseline_strategy": "bm25f_hash_hybrid_v2", "hybrid_strategy": "embedding_chroma_rrf_v1"}
        groups = {}
        for case in cases:
            groups.setdefault((case["repository_id"], case["base_sha"]), []).append(case)
        for (repository_id, sha), group in groups.items():
            repository = self.store.get_repository(repository_id)
            try:
                if repository is None:
                    raise ValueError("注册仓库不存在")
                source = Path(repository.local_path)
                full_sha, _ = self._base_files(source, sha)
                # 绝不 checkout/reset 原仓库；导出开发前基线到临时只读用途快照。
                archive = subprocess.run(["git", "archive", "--format=tar", full_sha], cwd=source,
                                         capture_output=True, timeout=60, check=False)
                if archive.returncode:
                    raise ValueError("导出开发前仓库快照失败")
                with tempfile.TemporaryDirectory(prefix="rag-baseline-") as directory:
                    snapshot = Path(directory)
                    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
                        members = [member for member in tar.getmembers() if member.isfile() or member.isdir()]
                        tar.extractall(snapshot, members=members, filter="data")
                    labels = [{"name": case["id"], "query": case["query"],
                               "expected_files": [item["path"] for item in case["candidates"] if item["label"] in {"required", "supporting"}]}
                              for case in group]
                    result = evaluate_repository(snapshot, {"name": "human_confirmed", "cases": labels}, engine, request.k,
                                                 repository_identity=f"rag-evaluation:{repository_id}:{full_sha}", baseline_sha=full_sha)
                for case, detail in zip(group, result["cases"]):
                    required = {item["path"] for item in case["candidates"] if item["label"] == "required"}
                    detail.update(title=case["title"], base_sha=full_sha, repository_id=repository_id,
                                  annotation_revision=case["revision"], content_fingerprint=result["content_fingerprint"])
                    for name in ("baseline", "hybrid"):
                        ranked = detail[name + "_files"]
                        detail[name + "_required_recall"] = round(100 * len(required & set(ranked)) / len(required), 2)
                    details.append(detail)
                    rows.append((RetrievalCase(case["id"], case["query"], tuple(detail["expected_files"]), {}), detail["baseline_files"], detail["hybrid_files"]))
                for name in latencies:
                    latencies[name].extend([result["average_query_ms"][name]] * len(group))
                report["embedding_model"] = result["embedding_model"]
                report["embedding_signature"] = result["embedding_signature"]
            except Exception as error:
                # Chroma/Embedding 各自异常基类不同，均需保留失败覆盖数，不能退回 HTML 500。
                errors.extend({"case_id": case["id"], "title": case["title"], "reason": str(error)[:600]} for case in group)
        evaluator = RetrievalEvaluator(k=request.k)
        metrics = {}
        for name, position in (("baseline", 1), ("hybrid", 2)):
            if not rows:
                metrics[name] = None
                continue
            metrics[name] = evaluator._metrics(rows, position=position).model_dump()
            metrics[name]["precision_at_k"] = round(100 * sum(len(set(row[position]) & set(row[0].expected_files)) / request.k for row in rows) / len(rows), 2)
            metrics[name]["required_recall_at_k"] = round(sum(detail[name + "_required_recall"] for detail in details) / len(details), 2)
        report.update(status="failed" if not rows else "partial" if errors else "completed", evaluated_count=len(rows),
                      cases=details, errors=errors, **metrics,
                      delta={key: round(metrics["hybrid"][key] - metrics["baseline"][key], 2) for key in metrics["baseline"]} if rows else {},
                      average_query_ms={name: round(sum(values) / len(values), 2) if values else None for name, values in latencies.items()},
                      limitations="仅人工确认样本；Recall/MRR/Hit 的相关集合为必要+辅助，另报必要文件 Recall。未知相关文件可能被当作无关。失败样本不计入指标，必须结合覆盖数和错误查看。")
        return self.store.save_rag_dataset_run(report)
