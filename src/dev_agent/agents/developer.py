from pathlib import Path


SERVICE_IMPLEMENTATION = '''"""Small task service used by the development-agent demo."""

ALLOWED_PRIORITIES = {"low", "medium", "high"}


def create_task(title: str, priority: str = "medium") -> dict[str, object]:
    """Create a task with a validated priority."""
    if not title.strip():
        raise ValueError("title must not be empty")
    if priority not in ALLOWED_PRIORITIES:
        allowed = ", ".join(sorted(ALLOWED_PRIORITIES))
        raise ValueError(f"priority must be one of: {allowed}")
    return {
        "id": 1,
        "title": title.strip(),
        "completed": False,
        "priority": priority,
    }
'''


TEST_IMPLEMENTATION = '''import unittest

from task_service import create_task


class TaskServiceTests(unittest.TestCase):
    def test_create_task(self):
        task = create_task("Write tests")
        self.assertEqual(task["title"], "Write tests")
        self.assertFalse(task["completed"])

    def test_default_priority_is_medium(self):
        self.assertEqual(create_task("Plan release")["priority"], "medium")

    def test_accepts_high_priority(self):
        self.assertEqual(create_task("Fix incident", priority="high")["priority"], "high")

    def test_rejects_unknown_priority(self):
        with self.assertRaisesRegex(ValueError, "priority must be one of"):
            create_task("Mystery", priority="urgent")

    def test_rejects_empty_title(self):
        with self.assertRaisesRegex(ValueError, "title must not be empty"):
            create_task("  ")


if __name__ == "__main__":
    unittest.main()
'''


class DemoDeveloperAgent:
    """Applies the approved priority feature to the isolated demo repository."""

    def implement(self, repository: Path) -> list[str]:
        service_path = repository / "task_service.py"
        test_path = repository / "tests" / "test_task_service.py"
        if not service_path.exists() or not test_path.exists():
            raise FileNotFoundError("Demo repository is missing expected files")
        service_path.write_text(SERVICE_IMPLEMENTATION, encoding="utf-8")
        test_path.write_text(TEST_IMPLEMENTATION, encoding="utf-8")
        return [str(service_path), str(test_path)]

