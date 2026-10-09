"""代码切片与 Chroma 存储：真实向量缓存、不可变仓库快照隔离、带行号的相关代码检索。"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from code_intelligence.embedding import EmbeddingClient


def code_chunks(path: str, content: str, max_chars: int = 2400) -> list[dict]:
    """Python 按 AST 符号分段；其他语言按声明边界与有界行窗口分段，不伪称 AST。"""
    lines = content.splitlines(keepends=True)
    boundaries: dict[int, str] = {1: "module"}
    if path.endswith(".py"):
        try:
            def visit(node, prefix=""):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        name = prefix + child.name
                        start = min([child.lineno, *(item.lineno for item in child.decorator_list)])
                        boundaries[start] = name
                        boundaries[child.end_lineno + 1] = "module"
                        visit(child, name + ".")
                    else:
                        visit(child, prefix)
            visit(ast.parse(content))
        except SyntaxError:
            pass
    else:
        import re
        for number, line in enumerate(lines, 1):
            match = re.match(r"\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|const|let|var)\s+([\w$]+)", line)
            if match:
                boundaries[number] = match[1]
    chunks = []
    starts = sorted(number for number in boundaries if number <= len(lines)) + [len(lines) + 1]
    for start, stop in zip(starts, starts[1:]):
        cursor = start
        while cursor < stop:
            end, length = cursor, 0
            while end < stop and (length + len(lines[end - 1]) <= max_chars or end == cursor):
                length += len(lines[end - 1])
                end += 1
            text = "".join(lines[cursor - 1:end - 1])
            # 超长单行也切成有界片段，保持真实行号（同一行可有多个片段）。
            for offset in range(0, len(text), max_chars):
                part = text[offset:offset + max_chars]
                symbol = boundaries[start]
                identity = f"{path}:{cursor}:{end - 1}:{offset}:{symbol}:{part}"
                chunks.append({"id": hashlib.sha256(identity.encode()).hexdigest(), "path": path,
                               "start_line": cursor, "end_line": end - 1, "symbol": symbol, "text": part})
            cursor = end
    return chunks


class CodeVectorStore:
    """每个仓库/模型/内容快照一个 collection，避免并发任务互相覆盖或读到旧工作区。"""

    def __init__(self, root: Path, embedding: EmbeddingClient, *, server_url: str | None = None):
        try:
            import chromadb
            from chromadb.config import Settings
        except ImportError:
            raise RuntimeError('请先安装 RAG 依赖：python -m pip install -e ".[rag]"') from None
        self.embedding = embedding
        settings = Settings(anonymized_telemetry=False)
        if server_url:
            address = urlsplit(server_url)
            if address.scheme not in {"http", "https"} or not address.hostname or address.username or address.password or address.query or address.fragment or address.path not in {"", "/"}:
                raise ValueError("RAG_CHROMA_URL 必须是不含凭证的 HTTP(S) 服务根地址")
            try:
                self.client = chromadb.HttpClient(host=address.hostname, port=address.port or (443 if address.scheme == "https" else 80), ssl=address.scheme == "https", settings=settings)
            except Exception:
                raise RuntimeError("Chroma 服务无法连接，请先运行 scripts/start-chroma.ps1 并检查 RAG_CHROMA_URL") from None
        else:
            self.client = chromadb.PersistentClient(path=str(root.resolve()), settings=settings)

    def _collection(self, index: dict):
        identity = json.dumps([index.get("version"), index.get("repository"), self.embedding.signature,
                               [(path, record.get("hash")) for path, record in sorted(index["files"].items())]], ensure_ascii=False)
        name = "code_" + hashlib.sha256(identity.encode()).hexdigest()[:40]
        return self.client.get_or_create_collection(name, embedding_function=None, metadata={"hnsw:space": "cosine"})

    def search(self, index: dict, query: str, k: int = 24) -> list[dict]:
        chunks = [chunk for record in index.get("files", {}).values() for chunk in record.get("chunks", []) if chunk["text"].strip()]
        if not chunks or not query.strip():
            return []
        collection = self._collection(index)
        existing = set(collection.get(include=[])["ids"])
        missing = [chunk for chunk in chunks if chunk["id"] not in existing]
        # 内容缓存与快照分离：HEAD 改变时只给新增/变化的块请求模型。
        cache = self.client.get_or_create_collection("embedding_" + self.embedding.signature, embedding_function=None)
        for offset in range(0, len(missing), 16):
            batch = missing[offset:offset + 16]
            documents = [f"{item['path']}\n{item['symbol']}\n{item['text']}" for item in batch]
            keys = [hashlib.sha256(text.encode()).hexdigest() for text in documents]
            cached = cache.get(ids=list(dict.fromkeys(keys)), include=["embeddings"])
            # Chroma 读取返回 ndarray，新请求返回 list；混用会使 upsert 校验失败。
            values = {key: [float(value) for value in vector] for key, vector in zip(cached["ids"], cached["embeddings"])}
            new = list(dict.fromkeys(key for key in keys if key not in values))
            if new:
                texts = {key: text for key, text in zip(keys, documents)}
                vectors = self.embedding.embed([texts[key] for key in new])
                cache.upsert(ids=new, embeddings=vectors)
                values.update(zip(new, vectors))
            collection.upsert(ids=[item["id"] for item in batch], embeddings=[values[key] for key in keys],
                              documents=[item["text"] for item in batch],
                              metadatas=[{key: item[key] for key in ("path", "start_line", "end_line", "symbol")} for item in batch])
        result = collection.query(query_embeddings=self.embedding.embed([query]), n_results=min(k, len(chunks)),
                                  include=["documents", "metadatas", "distances"])
        return [{**metadata, "text": text, "score": 1 - distance}
                for metadata, text, distance in zip(result["metadatas"][0], result["documents"][0], result["distances"][0])]
