"""Repository-scoped durable facts; task requirements never become global policy."""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime


def terms(text):
    """ASCII words and Chinese bigrams support Chinese without external services."""
    text = text.casefold()
    words = re.findall(r"[a-z0-9_./-]+", text)
    for chunk in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(chunk[i:i + 2] for i in range(max(1, len(chunk) - 1)))
    return set(words)


class RepositoryMemory:
    def __init__(self, database_path):
        self.path = str(database_path)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS repository_memories (
                repository_id TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,
                content TEXT NOT NULL, source TEXT NOT NULL, source_sha TEXT,
                updated_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(repository_id, key))""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, repository_id, key, kind, content, source, source_sha=None, *, observed=False):
        if not repository_id or not key or not source or not content.strip():
            raise ValueError("Memory requires repository, key, content and source")
        if kind not in {"architecture", "convention", "test_command"}:
            raise ValueError("Unsupported project memory kind")
        if len(content.encode()) > 6000:
            raise ValueError("Project memory exceeds 6000 bytes")
        with self.connect() as db:
            previous = db.execute("SELECT content,source_sha FROM repository_memories WHERE repository_id=? AND key=?",
                                  (repository_id, key)).fetchone()
            if observed and previous and previous["content"] == content and previous["source_sha"] == source_sha:
                return
            db.execute("""INSERT INTO repository_memories VALUES(?,?,?,?,?,?,?,1)
                ON CONFLICT(repository_id,key) DO UPDATE SET kind=excluded.kind,
                content=excluded.content,source=excluded.source,source_sha=excluded.source_sha,
                updated_at=excluded.updated_at,active=1""",
                (repository_id, key, kind, content, source, source_sha,
                 datetime.now(UTC).isoformat()))

    def invalidate(self, repository_id, key):
        with self.connect() as db:
            return db.execute("UPDATE repository_memories SET active=0 WHERE repository_id=? AND key=?",
                              (repository_id, key)).rowcount > 0

    def search(self, repository_id, query="", kind="", limit=4, offset=0):
        if not 1 <= limit <= 8 or offset < 0 or len(query) > 300:
            raise ValueError("Invalid memory query")
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM repository_memories
                WHERE repository_id=? AND active=1 AND (?='' OR kind=?)
                ORDER BY updated_at DESC,key""", (repository_id, kind, kind)).fetchall()
        needles = terms(query)
        ranked = []
        for row in rows:
            record = dict(row)
            haystack = record["key"] + " " + record["content"]
            score = len(needles & terms(haystack)) / max(1, len(needles))
            if query and query.casefold() in haystack.casefold():
                score += 1
            if query and score == 0:
                continue
            record["score"] = score
            record["requires_current_verification"] = True
            ranked.append(record)
        ranked.sort(key=lambda item: item["score"], reverse=True)
        page = ranked[offset:offset + limit]
        return {"records": page, "total_matches": len(ranked),
                "next_offset": offset + len(page) if offset + len(page) < len(ranked) else None}

    def observe(self, task):
        """Only repository observations and actually successful commands are promoted."""
        if not task.repository_id:
            return
        analysis = task.repository_analysis
        sha = analysis.head_sha if analysis else None
        if analysis:
            content = json.dumps({"language": analysis.language, "framework": analysis.framework,
                                  "entrypoints": analysis.entrypoints}, ensure_ascii=False)
            self.put(task.repository_id, "observed_architecture", "architecture", content,
                     f"task:{task.id}:repository_analysis", sha, observed=True)
        # A suggested command in a plan is not a proven working command.
        result = task.result
        if result and result.success and result.exit_code == 0 and result.command:
            self.put(task.repository_id, "verified_test_command", "test_command",
                     json.dumps(result.command, ensure_ascii=False),
                     f"task:{task.id}:result:exit_code=0", sha, observed=True)

    def context_records(self, repository_id, query=""):
        records = self.search(repository_id, query[:300], limit=2)["records"] if query else []
        for kind in ("convention", "architecture", "test_command"):
            records.extend(self.search(repository_id, kind=kind, limit=1)["records"])
        unique = {record["key"]: record for record in records}
        previews = list(unique.values())[:4]
        for item in previews:
            original = item["content"]
            while len(item["content"].encode()) > 600:
                item["content"] = item["content"][:max(1, len(item["content"]) // 2)]
            item["preview_truncated"] = original != item["content"]
        return previews


def attach_project_memory(task, memory):
    task.metadata["memory_database"] = memory.path
    task.metadata["project_memory"] = memory.context_records(task.repository_id or "", task.requirement)
