"""Small task service used by the development-agent demo."""


def create_task(title: str) -> dict[str, object]:
    """Create a task using the original minimal schema."""
    if not title.strip():
        raise ValueError("title must not be empty")
    return {"id": 1, "title": title.strip(), "completed": False}

