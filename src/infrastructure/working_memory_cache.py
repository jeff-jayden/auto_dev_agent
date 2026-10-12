"""Disposable Redis cache: immutable task revision + input digest keys."""

import hashlib
import json
import os
from functools import lru_cache


@lru_cache(maxsize=4)
def redis_client(url):
    import redis
    return redis.Redis.from_url(url, socket_connect_timeout=0.3,
                                socket_timeout=0.3, decode_responses=True)


class WorkingMemoryCache:
    schema = 1

    def __init__(self, database_path, client=None):
        self.namespace = hashlib.sha256(str(database_path).encode()).hexdigest()[:24]
        self.client = client
        self.status = "disabled"
        if client is None and os.getenv("WORKING_MEMORY_REDIS_URL"):
            try:
                self.client = redis_client(os.environ["WORKING_MEMORY_REDIS_URL"])
            except Exception:
                self.status = "unavailable"

    def prefix(self, task_id, revision):
        identity = hashlib.sha256(task_id.encode()).hexdigest()
        return f"agent:working:{self.namespace}:{identity}:{revision}:"

    def invalidate(self, task_id, revision):
        if self.client is None:
            return
        try:
            # Only the superseded revision; a racing newer writer cannot be removed.
            for key in self.client.scan_iter(match=self.prefix(task_id, revision) + "*", count=50):
                self.client.delete(key)
        except Exception:
            self.status = "unavailable"

    def get_or_build(self, task, recent_rounds, build):
        if self.client is None:
            return build()
        # Includes unsaved in-round mutations; revision alone would reuse stale state.
        source = task.model_dump(mode="json", exclude={"metadata"})
        source["rounds"] = task.metadata.get("user_feedback_rounds", [])
        digest = hashlib.sha256(json.dumps([self.schema, recent_rounds, source],
                                 sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        revision = task.metadata.get("task_revision", 0)
        key = self.prefix(task.id, revision) + digest
        try:
            raw = self.client.get(key)
            if raw:
                cached = json.loads(raw)
                if cached.get("revision") == revision and cached.get("digest") == digest and isinstance(cached.get("memory"), dict):
                    self.status = "hit"
                    return cached["memory"]
        except Exception:
            self.status = "unavailable"
            return build()
        result = build()
        try:
            ttl = max(1, int(os.getenv("WORKING_MEMORY_TTL_SECONDS", "1800")))
            self.client.setex(key, ttl, json.dumps({"revision": revision, "digest": digest, "memory": result}, ensure_ascii=False))
            self.status = "miss"
        except Exception:
            self.status = "unavailable"
        return result
