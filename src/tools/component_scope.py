"""Validate exact new component paths without granting directory access."""

import re
from pathlib import Path, PurePosixPath


def validate_component_requests(requests, root: Path | None = None) -> list[str]:
    if len(requests) > 8:
        raise ValueError("At most eight new components may be requested")
    paths = []
    for request in requests:
        path = request.path
        if not request.reason.strip():
            raise ValueError("New component request requires a reason")
        if not re.fullmatch(r"src/(?:pages|components)/(?:[A-Za-z][A-Za-z0-9_-]*/)*[A-Za-z][A-Za-z0-9_-]*\.(?:js|jsx|ts|tsx)", path):
            raise ValueError(f"Invalid new component path: {path}")
        if root is not None:
            base = root.resolve()
            target = base.joinpath(*PurePosixPath(path).parts)
            if base not in target.resolve().parents:
                raise ValueError("New component escapes workspace")
            # Do not grant access through symlinks/junctions, even within root.
            if any(part.is_symlink() or part.is_junction() for part in [target, *target.parents] if part != base and base in part.parents):
                raise ValueError("New component path traverses a link")
            if target.exists():
                raise ValueError(f"New component already exists: {path}")
        if path not in paths:
            paths.append(path)
    return paths


def approve_component_requests(task, requests, root: Path, actor: str) -> list[str]:
    """Validate the complete batch before expanding task and step write scopes."""
    from datetime import UTC, datetime

    paths = validate_component_requests(requests, root)
    if task.technical_plan is None:
        raise ValueError("A technical plan is required")
    added = [path for path in paths if path not in task.technical_plan.affected_files]
    task.technical_plan.affected_files.extend(added)
    if added:
        task.metadata.setdefault("scope_expansion_requests", []).append({
            "actor": actor, "approved_files": added,
            "reasons": {request.path: request.reason for request in requests},
            "created_at": datetime.now(UTC).isoformat(),
        })
    return paths


def approve_feedback_components(task, feedback: str, actor: str) -> list[str]:
    """Treat explicit new source paths in user feedback as exact requests."""
    from domain.models import NewComponentRequest

    if not task.workspace or task.technical_plan is None:
        return []
    root = Path(task.workspace)
    candidates = re.findall(
        r"(?<![\w.-])(src/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:js|jsx|ts|tsx|css|scss|py))",
        feedback.replace("\\", "/"),
    )
    requests = [NewComponentRequest(path=path, reason=feedback[:1000])
                for path in dict.fromkeys(candidates)
                if path not in task.technical_plan.affected_files and not (root / path).exists()]
    return approve_component_requests(task, requests, root, actor)
