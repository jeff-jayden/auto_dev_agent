"""Reranker HTTP 适配器：对有限代码候选评分，校验完整响应，不静默退回规则重排。"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class RerankerClient:
    endpoint: str
    model: str
    api_key: str = field(default="", repr=False)
    timeout: float = 60
    candidate_limit: int = 24
    document_chars: int = 3000
    provider: str = "dashscope"

    def __post_init__(self):
        address = urlsplit(self.endpoint)
        if (address.scheme not in {"http", "https"} or not address.hostname
                or address.username or address.password or address.query or address.fragment):
            raise ValueError("RERANK_ENDPOINT 必须是不含凭证和查询参数的 HTTP(S) 地址")
        if self.provider not in {"dashscope", "compatible"}:
            raise ValueError("RERANK_PROVIDER 只支持 dashscope / compatible")
        if (not self.model or not math.isfinite(self.timeout) or self.timeout <= 0
                or not 1 <= self.candidate_limit <= 100 or not 256 <= self.document_chars <= 8000):
            raise ValueError("Reranker 模型、超时、候选数或文档长度配置无效")

    @classmethod
    def from_environment(cls):
        # 只在用户显式指定时复用已有密钥变量；默认不继承 Embedding/对话凭证。
        return cls(
            endpoint=os.getenv("RERANK_ENDPOINT", ""), model=os.getenv("RERANK_MODEL", ""),
            api_key=os.getenv(os.getenv("RERANK_API_KEY_ENV", "RERANK_API_KEY"), ""),
            provider=os.getenv("RERANK_PROVIDER", "dashscope"),
            timeout=float(os.getenv("RERANK_TIMEOUT_SECONDS", "60")),
            candidate_limit=int(os.getenv("RERANK_CANDIDATE_LIMIT", "24")),
            document_chars=int(os.getenv("RERANK_DOCUMENT_CHARS", "3000")),
        )

    def rank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        if not documents:
            return []
        if (not query.strip() or len(query.encode("utf-8")) > 8000
                or len(documents) > self.candidate_limit
                or any(not isinstance(doc, str) or len(doc) > self.document_chars for doc in documents)):
            raise ValueError("Reranker 查询或候选文档超出预算")
        payload = {"model": self.model, "query": query, "documents": documents,
                   "top_n": len(documents), "return_documents": False}
        if self.provider == "dashscope":
            payload = {"model": self.model, "input": {"query": query, "documents": documents},
                       "parameters": {"top_n": len(documents), "return_documents": False}}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.endpoint, json.dumps(payload).encode("utf-8"), headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"Reranker 服务 HTTP {error.code}，请检查模型、权限和输入预算") from None
        except (URLError, TimeoutError, OSError):
            raise RuntimeError("Reranker 服务不可达或超时") from None
        except (ValueError, UnicodeError):
            raise RuntimeError("Reranker 返回无效 JSON") from None
        try:
            rows = data["output"]["results"] if self.provider == "dashscope" else data["results"]
            scores = []
            for row in rows:
                index, score = row["index"], row["relevance_score"]
                if type(index) is not int or not 0 <= index < len(documents):
                    raise ValueError
                if type(score) not in {int, float} or not math.isfinite(score):
                    raise ValueError
                scores.append((index, float(score)))
            if len(scores) != len(documents) or len({i for i, _ in scores}) != len(documents):
                raise ValueError
            return sorted(scores, key=lambda item: (-item[1], item[0]))
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("Reranker 响应缺失候选、索引重复或评分无效") from None

    def metadata(self) -> dict:
        return {"provider": self.provider, "model": self.model,
                "candidate_limit": self.candidate_limit, "document_chars": self.document_chars}
