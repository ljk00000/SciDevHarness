from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from scidev_pr_ui import PullRequestDialog


class PullRequestDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_renders_a_diff_and_submits_a_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-pr-ui-", ignore_cleanup_errors=True) as temp:
            dialog = PullRequestDialog(Path(temp))
            try:
                dialog._render_loaded(
                    {
                        "number": 7,
                        "title": "Task",
                        "state": "OPEN",
                        "headRefName": "feature/x",
                        "baseRefName": "main",
                    },
                    {"diff": "diff --git a/x b/x\n+hello\n", "truncated": False},
                    "",
                )
                self.assertIn("#7 Task", dialog.metadata.text())
                self.assertIn("+hello", dialog.diff.toPlainText())
                self.assertTrue(dialog.comment_button.isEnabled())

                dialog.comment.setPlainText("Please add a test")
                calls: list[tuple] = []

                def fake_review(*args, **kwargs):
                    calls.append((args, kwargs))
                    return {"action": "comment", "url": "https://github.com/example/project/pull/7"}

                with patch("scidev_pr_ui.review_pull_request", side_effect=fake_review):
                    dialog.review("comment")
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0][1], 7)
                self.assertEqual(calls[0][1]["body"], "Please add a test")
                self.assertIn("已提交", dialog.metadata.text())
            finally:
                dialog.close()

    def test_missing_comment_blocks_review_and_errors_are_shown(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-pr-ui-error-", ignore_cleanup_errors=True) as temp:
            dialog = PullRequestDialog(Path(temp))
            try:
                dialog._render_loaded({"number": 3, "title": "T"}, {"diff": "x"}, "")
                dialog.review("request-changes")
                self.assertIn("需要填写备注", dialog.metadata.text())
                dialog._render_loaded({}, {}, "网络策略为 deny")
                self.assertIn("加载失败", dialog.metadata.text())
                self.assertFalse(dialog.approve_button.isEnabled())
            finally:
                dialog.close()
