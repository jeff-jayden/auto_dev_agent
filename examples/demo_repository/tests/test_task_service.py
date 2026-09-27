import unittest

from task_service import create_task


class TaskServiceTests(unittest.TestCase):
    def test_create_task(self):
        task = create_task("Write tests")
        self.assertEqual(task["title"], "Write tests")
        self.assertFalse(task["completed"])

    def test_rejects_empty_title(self):
        with self.assertRaisesRegex(ValueError, "title must not be empty"):
            create_task("  ")


if __name__ == "__main__":
    unittest.main()

