from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dev_agent.infrastructure.store import SQLiteTaskStore
from dev_agent.repository.catalog import RepositoryCatalog


class RepositoryCatalogBrowseTests(unittest.TestCase):
    def test_browse_only_exposes_allowed_directories_and_marks_git_repositories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "sample-repository"
            repository.mkdir()
            (repository / ".git").mkdir()
            (root / "ordinary-folder").mkdir()
            catalog = RepositoryCatalog(SQLiteTaskStore(root / "agent.db"), [root, root])

            top_level = catalog.browse_directories()
            self.assertEqual(len(top_level["directories"]), 1)
            self.assertEqual(top_level["directories"][0]["path"], str(root.resolve()))

            listing = catalog.browse_directories(root)
            entries = {item["name"]: item for item in listing["directories"]}
            self.assertTrue(entries["sample-repository"]["is_git_repository"])
            self.assertFalse(entries["ordinary-folder"]["is_git_repository"])
            self.assertIsNone(listing["parent"])

            selected = catalog.browse_directories(repository)
            self.assertTrue(selected["is_git_repository"])
            self.assertEqual(selected["parent"], str(root.resolve()))

    def test_browse_rejects_paths_outside_allowed_roots(self):
        with TemporaryDirectory() as directory, TemporaryDirectory() as outside:
            root = Path(directory)
            catalog = RepositoryCatalog(SQLiteTaskStore(root / "agent.db"), [root])

            with self.assertRaisesRegex(ValueError, "outside the allowed"):
                catalog.browse_directories(outside)

    def test_register_remote_clones_to_cache_and_derives_name(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            catalog = RepositoryCatalog(SQLiteTaskStore(root / "agent.db"), [root], cache)

            def create_checkout(destination: Path, canonical_url: str) -> None:
                destination.mkdir(parents=True)
                (destination / ".git").mkdir()

            with patch.object(catalog, "_sync_remote_repository", side_effect=create_checkout), patch.object(
                catalog,
                "_remote_metadata",
                return_value={
                    "provider": "github",
                    "remote_url": "https://github.com/example/sample.git",
                    "github_repository": "example/sample",
                },
            ):
                repository = catalog.register_remote("https://github.com/example/sample")

            self.assertEqual(repository.name, "sample")
            self.assertEqual(Path(repository.local_path), (cache / "example--sample").resolve())
            self.assertEqual(repository.github_repository, "example/sample")

    def test_register_remote_rejects_non_github_urls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = RepositoryCatalog(SQLiteTaskStore(root / "agent.db"), [root])

            with self.assertRaisesRegex(ValueError, "Only GitHub"):
                catalog.register_remote("https://gitlab.com/example/sample.git")


if __name__ == "__main__":
    unittest.main()
