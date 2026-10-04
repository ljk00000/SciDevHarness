from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
if os.name == "nt":
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if windows_fonts.is_dir():
        os.environ["QT_QPA_FONTDIR"] = str(windows_fonts)

from PySide6.QtCore import QPointF, QSize, QSettings, QTimer  # noqa: E402
from PySide6.QtGui import QFontInfo, QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from scidev_client import (  # noqa: E402
    CodeEditor,
    ClientWindow,
    DevelopmentTreeView,
    MAX_TREE_LAYOUT_BYTES,
    NotificationToast,
)


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


class UIProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_ui_profiles_update_editor_tree_layout_and_switcher(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-ui-profiles-") as temp:
            window = ClientWindow(Path(temp))
            try:
                window.resize(1500, 920)
                window.show()
                self.app.processEvents()

                window.set_ui_profile("paper", persist=False)
                self.app.processEvents()
                self.assertIn("#f4f6f9", window.styleSheet())
                self.assertTrue(window.ui_theme_actions["paper"].isChecked())
                self.assertEqual(window.git_tree.rootObject().property("visualTheme"), "paper")
                self.assertEqual(window.welcome_editor.ui_profile, "paper")
                editor_font = QFontInfo(window.welcome_editor.font())
                self.assertTrue(editor_font.fixedPitch())
                self.assertEqual(editor_font.pointSize(), 12)
                keyword_color = (
                    window._editor_highlighters[window.welcome_editor]
                    .rules[0][1]
                    .foreground()
                    .color()
                    .name()
                )
                self.assertEqual(keyword_color, "#7c3aed")

                window.set_ui_profile("focus", persist=False)
                self.app.processEvents()
                self.assertIn("#0b0d14", window.styleSheet())
                sizes = window.workbench.sizes()
                total = sum(sizes)
                self.assertAlmostEqual(sizes[0] / total, 0.22, delta=0.015)
                self.assertAlmostEqual(sizes[2] / total, 0.20, delta=0.015)
                self.assertEqual(window.git_tree.rootObject().property("visualTheme"), "focus")
            finally:
                window.close()
                self.app.processEvents()

    def test_narrow_layout_collapses_explorer_and_activity_button_toggles_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-responsive-explorer-") as temp:
            window = ClientWindow(Path(temp))
            try:
                window.resize(899, 620)
                window.show()
                self.app.processEvents()

                self.assertTrue(window.explorer.isHidden())
                self.assertEqual(window.workbench.sizes()[0], 58)
                self.assertGreaterEqual(window.workbench.sizes()[1], 480)
                self.assertEqual(window.activity_buttons[0].toolTip(), "显示资源管理器")
                self.assertEqual(window.activity_buttons[0].accessibleName(), "显示资源管理器")

                window.resize(900, 620)
                self.app.processEvents()
                self.assertFalse(window.explorer.isHidden())
                self.assertGreaterEqual(window.workbench.sizes()[0], 200)

                window.resize(899, 620)
                self.app.processEvents()
                self.assertTrue(window.explorer.isHidden())
                window.activity_buttons[0].click()
                self.app.processEvents()
                self.assertFalse(window.explorer.isHidden())
                self.assertTrue(window._explorer_visibility_override)
                self.assertEqual(window.activity_buttons[0].toolTip(), "隐藏资源管理器")
                self.assertEqual(window.activity_buttons[0].accessibleName(), "隐藏资源管理器")

                window.resize(820, 600)
                self.app.processEvents()
                self.assertFalse(window.explorer.isHidden())
                window.activity_buttons[0].click()
                self.app.processEvents()
                self.assertTrue(window.explorer.isHidden())
                window.resize(1024, 768)
                self.app.processEvents()
                self.assertTrue(window.explorer.isHidden())
            finally:
                window.close()
                self.app.processEvents()

    def test_activity_rail_icons_are_vector_based_accessible_and_theme_aware(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-activity-icons-") as temp:
            window = ClientWindow(Path(temp))
            try:
                icons = [button.icon().cacheKey() for button in window.activity_buttons]
                self.assertEqual(len(set(icons)), 3)
                for button in window.activity_buttons:
                    self.assertFalse(button.icon().isNull())
                    self.assertEqual(button.text(), "")
                    self.assertGreaterEqual(button.iconSize().width(), 18)
                    self.assertTrue(button.accessibleName())

                window.set_ui_profile("paper", persist=False)
                self.app.processEvents()
                self.assertIn("#f4f6f9", window.styleSheet())
                self.assertNotEqual(window.activity_buttons[0].icon().cacheKey(), icons[0])
                icon = window.activity_buttons[0].icon()
                self.assertFalse(icon.pixmap(QSize(24, 24)).isNull())
                self.assertFalse(
                    icon.pixmap(QSize(24, 24), QIcon.Mode.Normal, QIcon.State.On).isNull()
                )
            finally:
                window.close()
                self.app.processEvents()

    def test_keyboard_focus_ring_tracks_the_active_theme(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-focus-ring-") as temp:
            window = ClientWindow(Path(temp))
            try:
                window.resize(1100, 700)
                window.show()
                for profile_key, focus_color in (
                    ("studio", "#8bc1ff"),
                    ("paper", "#286bcf"),
                    ("focus", "#b9a8ff"),
                ):
                    with self.subTest(profile=profile_key):
                        window.set_ui_profile(profile_key, persist=False)
                        window.title_git_button.setFocus()
                        self.app.processEvents()
                        self.assertTrue(window.title_git_button.hasFocus())
                        self.assertIn("QPushButton#GitPrimary:focus", window.styleSheet())
                        self.assertIn(f"border: 1px solid {focus_color}", window.styleSheet())
                        self.assertIn("QToolButton#ActivityButton:focus", window.styleSheet())
            finally:
                window.close()
                self.app.processEvents()

    def test_theme_menu_persists_the_choice_and_restores_it_on_startup(self) -> None:
        settings = QSettings("SciDevHarness", "SciDevHarness")
        had_previous = settings.contains("uiProfile")
        previous = settings.value("uiProfile") if had_previous else None
        second_window = None
        with tempfile.TemporaryDirectory(prefix="scidev-ui-profile-persistence-") as temp:
            window = ClientWindow(Path(temp))
            try:
                window.ui_theme_actions["paper"].trigger()
                self.assertEqual(window.ui_profile, "paper")
                self.assertEqual(settings.value("uiProfile"), "paper")

                second_window = ClientWindow(Path(temp))
                self.assertEqual(second_window.ui_profile, "paper")
            finally:
                window.close()
                if second_window is not None:
                    second_window.close()
                if had_previous:
                    settings.setValue("uiProfile", previous)
                else:
                    settings.remove("uiProfile")
                settings.sync()
                self.app.processEvents()


class NotificationToastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_messages_are_non_modal_reuse_one_toast_and_fit_responsive_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-notification-toast-") as temp:
            window = ClientWindow(Path(temp))
            try:
                window.resize(1100, 700)
                window.show()
                self.app.processEvents()

                window._show_message("保存失败", "磁盘暂时不可写。")
                notification = window._active_notification
                self.assertIsInstance(notification, NotificationToast)
                self.assertIsNone(self.app.activeModalWidget())
                self.assertIs(notification.parentWidget(), window.centralWidget())
                self.assertTrue(notification._timer.isActive())

                window._show_message("Git 错误", "远程暂时不可用。")
                self.app.processEvents()
                self.assertIs(window._active_notification, notification)
                self.assertEqual(len(window.centralWidget().findChildren(NotificationToast)), 1)
                self.assertEqual(notification.title_label.text(), "Git 错误")
                self.assertEqual(notification.body_label.text(), "远程暂时不可用。")

                for profile, expected in (
                    ("studio", "#202832"),
                    ("paper", "#ffffff"),
                    ("focus", "#211d2b"),
                ):
                    window.set_ui_profile(profile, persist=False)
                    self.app.processEvents()
                    self.assertIn(expected, window.styleSheet())

                window.resize(820, 600)
                self.app.processEvents()
                toast_rect = notification.geometry()
                root_rect = window.centralWidget().rect()
                self.assertTrue(root_rect.contains(toast_rect))
                self.assertGreaterEqual(toast_rect.x(), 0)
                self.assertGreaterEqual(toast_rect.y(), 0)
                self.assertLessEqual(toast_rect.right(), root_rect.right())
                self.assertLessEqual(toast_rect.bottom(), root_rect.bottom())

                notification.close_button.click()
                self.app.processEvents()
                self.assertIsNone(window._active_notification)
            finally:
                window.close()
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


class StreamingAgentUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_streamed_text_updates_one_bubble_and_finalizes_without_duplication(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-streaming-chat-") as temp:
            window = ClientWindow(Path(temp))
            try:
                initial_count = window.chat_layout.count()
                partial = {"session_id": "session-1", "turn": 2, "text": "A pelican "}
                window._handle_agent_event("assistant_delta", partial)
                window._handle_agent_event("assistant_delta", {**partial, "text": "is drawing."})
                self.app.processEvents()

                self.assertEqual(len(window._streaming_bubbles), 1)
                bubble = window._streaming_bubbles[("session-1", 2)]
                self.assertEqual(bubble.body_label.text(), "A pelican is drawing.")
                self.assertEqual(bubble.role_label.text(), "Agent · 正在生成")

                window._handle_agent_event(
                    "assistant",
                    {"session_id": "session-1", "turn": 2, "text": "A pelican is drawing."},
                )
                self.app.processEvents()

                self.assertEqual(window.chat_layout.count(), initial_count + 1)
                self.assertEqual(bubble.body_label.text(), "A pelican is drawing.")
                self.assertEqual(bubble.role_label.text(), "Agent")
                self.assertEqual(window._streaming_bubbles, {})
            finally:
                window.close()
                self.app.processEvents()

    def test_interrupted_stream_is_marked_before_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-streaming-retry-") as temp:
            window = ClientWindow(Path(temp))
            try:
                window._handle_agent_event(
                    "assistant_delta",
                    {"session_id": "session-2", "turn": 1, "text": "Partial answer"},
                )
                bubble = window._streaming_bubbles[("session-2", 1)]
                window._interrupt_streaming_bubbles("Agent · 网络中断，等待自动重试")

                self.assertEqual(bubble.role_label.text(), "Agent · 网络中断，等待自动重试")
                self.assertEqual(bubble.body_label.text(), "Partial answer")
                self.assertEqual(window._streaming_bubbles, {})
            finally:
                window.close()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
