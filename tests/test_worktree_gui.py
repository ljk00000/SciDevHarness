from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from PySide6.QtWidgets import QApplication

from scidev_client import ClientWindow
from scidev_worktree import WorktreeManager


class WorktreeGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_command_creates_a_worktree_and_opens_a_window(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-gui-worktree-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "GUI Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "gui@example.invalid"], cwd=root, check=True)
            (root / "base.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True)

            window = ClientWindow(root)
            self.app._scidev_windows = [window]
            window.show()
            child = None
            try:
                child = window._create_isolated_worktree()
                self.assertIsNotNone(child)
                self.assertIsNot(child, window)
                assert child is not None
                self.assertTrue(child.project_root.is_dir())
                self.assertIn(".research", str(child.project_root))
                self.assertTrue(child.project_root.name.startswith("gui-"))
            finally:
                for candidate in list(getattr(self.app, "_scidev_windows", [])):
                    candidate.close()
                self.app.processEvents()
                manager = WorktreeManager(root)
                for worktree in manager.list():
                    try:
                        manager.remove(worktree, force=True)
                    except Exception:
                        pass


if __name__ == "__main__":
    unittest.main()
