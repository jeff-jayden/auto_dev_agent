"""Chroma is a rebuildable index, never the authority for memory contents."""

import hashlib
import json
import os
from dataclasses import replace
from functools import lru_cache
from pathlib import Path


def record_id(record):
    value = [record[k] for k in ("repository_id", "key", "content", "source", "source_sha", "updated_at")]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


class SemanticMemoryIndex:
    def __init__(self, database_path, *, embedding=None, client=None):
        from code_intelligence.embedding import EmbeddingClient
        from code_intelligence.vector_store import CodeVectorStore
        self.embedding = embedding or replace(EmbeddingClient.from_environment(),
                                               timeout=float(os.getenv("MEMORY_EMBEDDING_TIMEOUT_SECONDS", "8")))
        self.client = client or CodeVectorStore(Path(database_path).parent / "memory_vectors", self.embedding,
            server_url=os.getenv("MEMORY_CHROMA_URL") or os.getenv("RAG_CHROMA_URL") or "http://127.0.0.1:8001").client
        self.namespace = hashlib.sha256(str(Path(database_path).resolve()).encode()).hexdigest()[:16]

    def search(self, repository_id, query, records, kind=""):
        if not records:
            return {}
        identity = hashlib.sha256((self.namespace + repository_id + self.embedding.signature).encode()).hexdigest()[:40]
        collection = self.client.get_or_create_collection("memory_" + identity, embedding_function=None,
                                                         metadata={"hnsw:space": "cosine"})
        authoritative = {record_id(record): record for record in records}
        existing = set(collection.get(include=[])["ids"])
        missing = [key for key in authoritative if key not in existing]
        for offset in range(0, len(missing), 16):
            ids = missing[offset:offset + 16]
            batch = [authoritative[key] for key in ids]
            documents = [record["key"] + "\n" + record["content"] for record in batch]
            collection.upsert(ids=ids, documents=documents, embeddings=self.embedding.embed(documents),
                              metadatas=[{"key": record["key"], "kind": record["kind"]} for record in batch])
        # Lazy reconciliation also removes invalidated records, including after outages.
        stale = list(existing - set(authoritative))
        if stale:
            collection.delete(ids=stale)
        result = collection.query(query_embeddings=self.embedding.embed([query]),
                                  n_results=min(32, len(records)), include=["distances"],
                                  **({"where": {"kind": kind}} if kind else {}))
        threshold = float(os.getenv("MEMORY_MIN_SIMILARITY", "0.45"))
        hits = {}
        for key, distance in zip(result["ids"][0], result["distances"][0]):
            # SQLite membership/version check rejects stale/racing/foreign vectors.
            if key in authoritative and 1 - distance >= threshold:
                hits[authoritative[key]["key"]] = max(0.0, 1 - distance)
        return hits


@lru_cache(maxsize=8)
def configured_index(database_path, configuration):
    return SemanticMemoryIndex(database_path)


def semantic_scores(database_path, repository_id, query, records, kind=""):
    if os.getenv("MEMORY_SEMANTIC_ENABLED", "false").lower() not in {"true", "1", "yes"}:
        return {}, "disabled"
    configuration = tuple(os.getenv(name, "") for name in (
        "MEMORY_CHROMA_URL", "RAG_CHROMA_URL", "EMBEDDING_PROVIDER", "EMBEDDING_MODEL",
        "EMBEDDING_BASE_URL", "EMBEDDING_DIMENSIONS", "MEMORY_EMBEDDING_TIMEOUT_SECONDS"))
    try:
        return configured_index(str(database_path), configuration).search(repository_id, query, records, kind), "ok"
    except Exception:
        # Do not expose service URLs, credentials or source content in errors.
        return {}, "unavailable"
