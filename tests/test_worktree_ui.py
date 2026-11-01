from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from PySide6.QtWidgets import QApplication

from scidev_worktree import WorktreeManager
from scidev_worktree_ui import WorktreeDialog


class WorktreeDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_lists_opens_and_removes_a_worktree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-worktree-ui-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "UI Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "ui@example.invalid"], cwd=root, check=True)
            (root / "base.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True)
            manager = WorktreeManager(root)
            worktree = manager.create("dialog-test")
            dialog = WorktreeDialog(manager, root)
            try:
                rows = dialog.table.rowCount()
                branches = [dialog.table.item(row, 0).text() for row in range(rows)]
                self.assertIn(worktree.branch, branches)
                row = branches.index(worktree.branch)
                dialog.table.selectRow(row)
                opened: list[str] = []
                dialog.open_requested.connect(opened.append)
                dialog._open_selected()
                self.assertEqual(opened, [str(worktree.path)])
            finally:
                dialog.close()
            dialog = WorktreeDialog(manager, root)
            try:
                branches = [dialog.table.item(row, 0).text() for row in range(dialog.table.rowCount())]
                dialog.table.selectRow(branches.index(worktree.branch))
                self.assertTrue(dialog.remove_selected(force=True))
                self.assertFalse(worktree.path.exists())
            finally:
                dialog.close()


if __name__ == "__main__":
    unittest.main()
