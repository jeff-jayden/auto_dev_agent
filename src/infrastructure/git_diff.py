"""Capture working tree content, including new files, without touching its index."""

import os
import subprocess
import tempfile
from pathlib import Path


def workspace_diff(repository: Path, baseline: str = "HEAD") -> str:
    with tempfile.TemporaryDirectory(prefix="agent-diff-") as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"))

        def git(*args):
            return subprocess.run(
                ["git", "-c", "core.quotePath=false", *args], cwd=repository, env=env,
                check=True, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30,
            ).stdout

        git("read-tree", baseline)
        git("add", "-u", "--", ".")
        # Runtime artifacts must not become deliverables in repositories that
        # have incomplete .gitignore files. Track existing files normally.
        generated = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}
        for path in git("ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            if not path:
                continue
            relative = Path(path)
            if set(relative.parts) & generated or relative.suffix in {".pyc", ".pyo"}:
                continue
            if relative.name in {".env", ".env.local", "id_rsa", "id_ed25519", "credentials", "secrets.yml"}:
                continue
            git("add", "--", path)
        return git("diff", "--cached", "--no-ext-diff", "--no-renames", baseline, "--", ".").strip()
