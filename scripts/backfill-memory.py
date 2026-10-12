"""Populate historical experience from verified tasks without rewriting tasks."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from infrastructure.store import SQLiteTaskStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=ROOT / "runtime" / "agent.db")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("Existing SQLite task database required")
    store = SQLiteTaskStore(args.database)
    with store.memory.connect() as db:
        before = db.execute("SELECT COUNT(*) FROM repository_memories WHERE kind='experience'").fetchone()[0]
    for task in store.list_tasks():
        store.memory.observe_experience(task)
    with store.memory.connect() as db:
        after = db.execute("SELECT COUNT(*) FROM repository_memories WHERE kind='experience'").fetchone()[0]
    print(f"Historical experiences added: {after - before}; total: {after}")


if __name__ == "__main__":
    main()
