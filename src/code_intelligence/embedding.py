"""真实 Embedding HTTP 适配器：独立配置 Ollama / OpenAI-compatible，校验向量并隐藏密钥。"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class EmbeddingClient:
    provider: str
    model: str
    base_url: str
    api_key: str = field(default="", repr=False)
    timeout: float = 60
    dimensions: int | None = None

    def __post_init__(self):
        if self.provider not in {"ollama", "openai"}:
            raise ValueError("EMBEDDING_PROVIDER 只支持 ollama 或 openai")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Embedding 地址必须是不含凭证或查询参数的 HTTP(S) 地址")
        if not self.model or self.timeout <= 0 or (self.dimensions is not None and self.dimensions <= 0):
            raise ValueError("Embedding 模型、超时或维度配置无效")

    @classmethod
    def from_environment(cls) -> EmbeddingClient:
        provider = os.getenv("EMBEDDING_PROVIDER", "ollama")
        model = os.getenv("EMBEDDING_MODEL", "")
        if not model:
            raise ValueError("真实 RAG 需要配置 EMBEDDING_MODEL，不能使用对话模型代替")
        return cls(
            provider=provider, model=model,
            base_url=os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:11434" if provider == "ollama" else "https://api.openai.com/v1").rstrip("/"),
            api_key=os.getenv("EMBEDDING_API_KEY", ""),
            timeout=float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "60")),
            dimensions=int(os.environ["EMBEDDING_DIMENSIONS"]) if os.getenv("EMBEDDING_DIMENSIONS") else None,
        )

    @property
    def signature(self) -> str:
        # 不持久化凭证；切换模型/服务/维度后隔离向量空间。
        value = f"{self.provider}|{self.base_url}|{self.model}|{self.dimensions}|v1"
        return hashlib.sha256(value.encode()).hexdigest()[:24]

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": self.model, "input": texts}
        if self.provider == "ollama":
            payload["truncate"] = False
        if self.dimensions:
            payload["dimensions"] = self.dimensions
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        endpoint = "/api/embed" if self.provider == "ollama" else "/embeddings"
        request = Request(self.base_url.rstrip("/") + endpoint, json.dumps(payload).encode(), headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"Embedding 服务 HTTP {error.code}，请检查模型、权限及输入长度") from None
        except (URLError, TimeoutError, OSError):
            raise RuntimeError("Embedding 服务不可达或超时，请检查 EMBEDDING_BASE_URL 和模型服务") from None
        except (ValueError, UnicodeError):
            raise RuntimeError("Embedding 服务返回了无效 JSON") from None
        try:
            if self.provider == "ollama":
                vectors = data["embeddings"]
            else:
                rows = sorted(data["data"], key=lambda item: item["index"])
                if [row["index"] for row in rows] != list(range(len(texts))):
                    raise ValueError("invalid indices")
                vectors = [row["embedding"] for row in rows]
            if len(vectors) != len(texts) or not vectors:
                raise ValueError("invalid count")
            size = len(vectors[0])
            if not size or (self.dimensions and size != self.dimensions):
                raise ValueError("invalid dimensions")
            normalized = []
            for vector in vectors:
                if len(vector) != size:
                    raise ValueError("inconsistent dimensions")
                values = [float(value) for value in vector]
                norm = math.sqrt(sum(value * value for value in values))
                if not math.isfinite(norm) or norm == 0:
                    raise ValueError("invalid vector")
                normalized.append([value / norm for value in values])
            return normalized
        except (KeyError, TypeError, ValueError, OverflowError):
            raise RuntimeError("Embedding 返回的数量、维度或数值无效") from None
