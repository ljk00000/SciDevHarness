from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"

from PySide6.QtCore import QPointF, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from scidev_client import CodeEditor, ClientWindow, MAX_TREE_LAYOUT_BYTES, DevelopmentTreeView  # noqa: E402


class DevelopmentTreeLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_invalid_saved_offsets_do_not_break_tree_loading(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-invalid-tree-layout-") as temp:
            layout = Path(temp) / "tree_layout.json"
            layout.write_text(
                '{"nan":{"x":0,"y":NaN},'
                '"infinite":{"x":Infinity,"y":0},'
                '"huge_y":{"x":0,"y":1e300},'
                '"huge_x":{"x":-1e300,"y":0},'
                '"valid":{"x":22.5,"y":18}}',
                encoding="utf-8",
            )
            tree = DevelopmentTreeView(layout)
            nodes = [
                {"id": "main", "lane": "main", "title": "Main", "status": "active"},
                *(
                    {
                        "id": node_id,
                        "lane": "attempt",
                        "parent_id": "main",
                        "title": node_id,
                        "status": "failed",
                    }
                    for node_id in ("nan", "infinite", "huge_y", "huge_x", "valid")
                ),
            ]
            try:
                tree.set_nodes(nodes)
                self.assertEqual(tree._node_offsets, {"valid": QPointF(22.5, 18.0)})
                self.assertLess(tree.minimumHeight(), 1000)
            finally:
                tree.close()
                self.app.processEvents()

    def test_corrupt_or_oversized_layout_files_fall_back_to_default_positions(self) -> None:
        payloads = (
            b"\xff invalid utf-8",
            b"{ truncated json",
            b"[]",
            b'{"node":{"x":' + b"9" * 5000 + b',"y":0}}',
            b" " * (MAX_TREE_LAYOUT_BYTES + 1),
        )
        for payload in payloads:
            with self.subTest(payload_size=len(payload)), tempfile.TemporaryDirectory(
                prefix="scidev-corrupt-tree-layout-"
            ) as temp:
                layout = Path(temp) / "tree_layout.json"
                layout.write_bytes(payload)
                tree = DevelopmentTreeView(layout)
                try:
                    tree.set_nodes(
                        [
                            {"id": "main", "lane": "main", "title": "Main", "status": "active"},
                            {
                                "id": "node",
                                "lane": "attempt",
                                "parent_id": "main",
                                "title": "Attempt",
                                "status": "failed",
                            },
                        ]
                    )
                    self.assertEqual(tree._node_offsets, {})
                    self.assertLess(tree.minimumHeight(), 1000)
                finally:
                    tree.close()
                    self.app.processEvents()


class AgentWorkspaceRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_retry_and_failure_refresh_clean_editors_but_preserve_unsaved_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-agent-refresh-") as temp:
            root = Path(temp)
            source = root / "module.py"
            source.write_text("value = 1\n", encoding="utf-8")
            window = ClientWindow(root)
            try:
                window._open_file(source, preview=False)
                self.app.processEvents()
                editor = window._active_editor()
                self.assertIsNotNone(editor)

                source.write_text("value = 2\n", encoding="utf-8")
                window.active_task_id = "retry-task"
                window._handle_worker_event(
                    "task_retry",
                    {"task_id": "retry-task", "attempts": 2},
                )
                self.assertEqual(editor.toPlainText(), "value = 2\n")
                self.assertFalse(editor.document().isModified())

                editor.selectAll()
                editor.insertPlainText("local unsaved edit\n")
                self.assertTrue(editor.document().isModified())
                source.write_text("value = 3\n", encoding="utf-8")
                window.active_task_id = "failed-task"
                window._handle_worker_event(
                    "task_failed",
                    {"task_id": "failed-task", "error": "network unavailable"},
                )
                self.assertEqual(editor.toPlainText(), "local unsaved edit\n")
                self.assertTrue(editor.document().isModified())
            finally:
                # The assertions above verify that the dirty buffer survived
                # the simulated task failure; discard it only for test cleanup.
                for test_editor in window._editor_paths:
                    if isinstance(test_editor, CodeEditor):
                        test_editor.document().setModified(False)
                window.close()
                self.app.processEvents()


class UnsavedCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _close_with_choice(self, window: ClientWindow, label: str) -> bool:
        clicked: list[str] = []
        deadline = time.monotonic() + 3

        def choose_button() -> None:
            for widget in self.app.topLevelWidgets():
                if isinstance(widget, QMessageBox) and widget.isVisible():
                    button = next((item for item in widget.buttons() if item.text() == label), None)
                    if button is not None:
                        clicked.append(label)
                        button.click()
                    return
            if window.isVisible() and time.monotonic() < deadline:
                QTimer.singleShot(10, choose_button)

        QTimer.singleShot(0, choose_button)
        closed = window.close()
        self.app.processEvents()
        self.assertEqual(clicked, [label], f"close confirmation did not offer {label!r}")
        return closed

    def _open_window_with_file(self, root: Path, name: str = "module.py") -> tuple[ClientWindow, Path, CodeEditor]:
        source = root / name
        source.write_text("value = 1\n", encoding="utf-8")
        window = ClientWindow(root)
        window._open_file(source, preview=False)
        window.show()
        self.app.processEvents()
        editor = window._active_editor()
        self.assertIsInstance(editor, CodeEditor)
        return window, source, editor

    @staticmethod
    def _make_dirty(editor: CodeEditor, text: str) -> None:
        editor.selectAll()
        editor.insertPlainText(text)
        if not editor.document().isModified():
            editor.document().setModified(True)

    def test_close_saves_all_dirty_files_once_across_editor_groups(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-close-save-all-") as temp:
            root = Path(temp)
            window, first_path, first_editor = self._open_window_with_file(root)
            second_path = root / "second.py"
            second_path.write_text("value = 10\n", encoding="utf-8")
            try:
                self._make_dirty(first_editor, "value = 2\n")
                window._open_editor_to_side(second_path)
                second_editor = window._active_editor()
                self.assertIsInstance(second_editor, CodeEditor)
                self._make_dirty(second_editor, "value = 20\n")

                self.assertTrue(self._close_with_choice(window, "保存全部"))

                self.assertEqual(first_path.read_text(encoding="utf-8"), "value = 2\n")
                self.assertEqual(second_path.read_text(encoding="utf-8"), "value = 20\n")
                self.assertFalse(first_editor.document().isModified())
                self.assertFalse(second_editor.document().isModified())
            finally:
                if not window._closing:
                    window.close()
                self.app.processEvents()

    def test_close_discard_leaves_disk_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-close-discard-") as temp:
            root = Path(temp)
            window, source, editor = self._open_window_with_file(root)
            try:
                self._make_dirty(editor, "unsaved content\n")

                self.assertTrue(self._close_with_choice(window, "放弃修改"))

                self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
            finally:
                if not window._closing:
                    self._close_with_choice(window, "放弃修改")
                self.app.processEvents()

    def test_close_cancel_keeps_window_and_unsaved_buffer_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-close-cancel-") as temp:
            root = Path(temp)
            window, source, editor = self._open_window_with_file(root)
            try:
                self._make_dirty(editor, "unsaved content\n")

                self.assertFalse(self._close_with_choice(window, "继续编辑"))

                self.assertTrue(window.isVisible())
                self.assertTrue(editor.document().isModified())
                self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
            finally:
                if not window._closing:
                    self._close_with_choice(window, "放弃修改")
                self.app.processEvents()

    def test_close_save_failure_keeps_unsaved_buffer_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-close-save-failure-") as temp:
            root = Path(temp)
            window, source, editor = self._open_window_with_file(root)
            try:
                self._make_dirty(editor, "unsaved content\n")
                with patch.object(window.toolbox, "write_file", side_effect=OSError("disk full")):
                    self.assertFalse(self._close_with_choice(window, "保存全部"))

                self.assertTrue(window.isVisible())
                self.assertTrue(editor.document().isModified())
                self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
            finally:
                if not window._closing:
                    self._close_with_choice(window, "放弃修改")
                self.app.processEvents()

    def test_clean_window_closes_without_showing_confirmation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-close-clean-") as temp:
            root = Path(temp)
            window, _source, _editor = self._open_window_with_file(root)
            try:
                with patch("scidev_client.QMessageBox.exec") as show_dialog:
                    self.assertTrue(window.close())
                show_dialog.assert_not_called()
            finally:
                if not window._closing:
                    window.close()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
