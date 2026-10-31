from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from scidev_worktree import WorktreeError, WorktreeManager


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _make_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Worktree Test")
    _git(root, "config", "user.email", "worktree@example.invalid")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")


class WorktreeManagerTests(unittest.TestCase):
    def test_create_commit_list_and_remove(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-worktree-", ignore_cleanup_errors=True) as temp:
            repo = Path(temp)
            _make_repo(repo)
            manager = WorktreeManager(repo)
            created = manager.create("task one")
            self.assertEqual(created.branch, "scidev/agent-task-one")
            self.assertTrue((created.path / "base.txt").is_file())
            self.assertIn(created.path.resolve(), {item.path for item in manager.list()})

            (created.path / "new.txt").write_text("new\n", encoding="utf-8")
            _git(created.path, "add", "-A")
            _git(created.path, "commit", "-q", "-m", "worktree change")
            self.assertTrue(_git(created.path, "log", "-1", "--format=%s"))
            manager.remove(created)
            self.assertFalse(created.path.exists())

    def test_dirty_worktree_requires_force(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-worktree-dirty-", ignore_cleanup_errors=True) as temp:
            repo = Path(temp)
            _make_repo(repo)
            created = WorktreeManager(repo).create("dirty")
            (created.path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaises(WorktreeError):
                WorktreeManager(repo).remove(created)
            WorktreeManager(repo).remove(created, force=True)

    def test_non_repository_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-worktree-norepo-") as temp:
            with self.assertRaises(WorktreeError):
                WorktreeManager(Path(temp)).create("nope")


if __name__ == "__main__":
    unittest.main()
