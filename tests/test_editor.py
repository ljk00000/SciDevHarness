"""Inline editor diff highlighting tests."""
from __future__ import annotations

import os
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication

from scidev_editor import CodeEditor


class EditorDiffHighlightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_highlights_added_lines(self) -> None:
        editor = CodeEditor()
        editor.setPlainText("alpha\nbeta\ngamma")
        added, modified = editor.highlight_unsaved_diff("alpha\ngamma")
        self.assertEqual((added, modified), (1, 0))
        self.assertEqual(editor._diff_added, {2})
        self.assertGreaterEqual(len(editor.extraSelections()), 2)
        editor.deleteLater()

    def test_highlights_modified_lines(self) -> None:
        editor = CodeEditor()
        editor.setPlainText("alpha\nchanged\ngamma")
        added, modified = editor.highlight_unsaved_diff("alpha\nbeta\ngamma")
        self.assertEqual((added, modified), (0, 1))
        self.assertEqual(editor._diff_modified, {2})
        editor.deleteLater()

    def test_clear_diff_removes_highlights(self) -> None:
        editor = CodeEditor()
        editor.setPlainText("one\ntwo")
        editor.set_diff_lines({1}, {2})
        editor.clear_diff_lines()
        self.assertEqual(editor._diff_added, set())
        self.assertEqual(editor._diff_modified, set())
        editor.deleteLater()

    def test_unchanged_buffer_has_no_diff_markers(self) -> None:
        editor = CodeEditor()
        editor.setPlainText("same\ntext")
        added, modified = editor.highlight_unsaved_diff("same\ntext")
        self.assertEqual((added, modified), (0, 0))
        self.assertEqual(editor._diff_added, set())
        self.assertEqual(editor._diff_modified, set())
        editor.deleteLater()


if __name__ == "__main__":
    unittest.main()
