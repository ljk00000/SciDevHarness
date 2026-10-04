from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
if os.name == "nt":
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if windows_fonts.is_dir():
        os.environ["QT_QPA_FONTDIR"] = str(windows_fonts)

from PySide6.QtCore import QPointF, QSize, QSettings, QTimer, Qt  # noqa: E402
from PySide6.QtGui import QFontInfo, QIcon  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from scidev_client import (  # noqa: E402
    CodeEditor,
    ClientWindow,
    DevelopmentTreeView,
    MAX_TREE_LAYOUT_BYTES,
    NotificationToast,
    ProjectFilterProxy,
    TerminalOutputDecoder,
)
from scidev_core import CodingToolbox, SummarySettings  # noqa: E402


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


class DevelopmentTreeInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_clicking_attempt_node_updates_details_and_retry_availability(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-tree-node-selection-") as temp:
            window = ClientWindow(Path(temp))
            try:
                window.resize(1500, 920)
                window.show()
                window.show_git()
                window.git_tree.set_nodes(
                    [
                        {"id": "root", "lane": "main", "status": "main", "title": "主线"},
                        {
                            "id": "cancelled-direction",
                            "lane": "attempt",
                            "status": "failed",
                            "parent_id": "root",
                            "title": "失败的优化方向",
                            "description": "验证结果未改善。",
                            "meta": "失败 · 2 次尝试",
                        },
                    ]
                )
                self.app.processEvents()
                QTest.qWait(40)

                branch_rect = window.git_tree._positions()[0]["cancelled-direction"][2]
                QTest.mouseClick(
                    window.git_tree,
                    Qt.MouseButton.LeftButton,
                    pos=branch_rect.center(),
                )
                self.app.processEvents()

                self.assertEqual(window.git_tree.selected_id, "cancelled-direction")
                self.assertEqual(window.git_detail_title.text(), "失败的优化方向")
                self.assertEqual(window.git_detail_description.text(), "验证结果未改善。")
                self.assertEqual(window.git_retry_button.property("task_id"), "cancelled-direction")
                self.assertTrue(window.git_retry_button.isEnabled())
            finally:
                window.close()
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
                window.set_ui_profile("studio", persist=False)
                self.app.processEvents()
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


class EditorPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_save_preserves_existing_line_ending_style(self) -> None:
        for label, newline in (("CRLF", b"\r\n"), ("LF", b"\n"), ("CR", b"\r")):
            with self.subTest(style=label), tempfile.TemporaryDirectory(
                prefix="scidev-editor-eol-"
            ) as temp:
                root = Path(temp)
                source = root / "module.py"
                source.write_bytes(b"first = 1" + newline + b"second = 2" + newline)
                window = ClientWindow(root)
                try:
                    window._open_file(source, preview=False)
                    editor = window._active_editor()
                    self.assertIsNotNone(editor)
                    editor.setPlainText("first = 3\nsecond = 2\n")
                    editor.document().setModified(True)

                    window.save_current_file()

                    self.assertEqual(
                        source.read_bytes(),
                        b"first = 3" + newline + b"second = 2" + newline,
                    )
                    self.assertFalse(editor.document().isModified())
                finally:
                    window.close()
                    self.app.processEvents()

    def test_clean_agent_refresh_updates_the_editor_eol_style(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-editor-refresh-eol-") as temp:
            root = Path(temp)
            source = root / "module.py"
            source.write_bytes(b"value = 1\r\n")
            window = ClientWindow(root)
            try:
                window._open_file(source, preview=False)
                editor = window._active_editor()
                self.assertIsNotNone(editor)
                source.write_bytes(b"value = 2\n")
                window._refresh_open_file_after_task()
                editor.selectAll()
                editor.insertPlainText("value = 3\n")
                window.save_current_file()

                self.assertEqual(source.read_bytes(), b"value = 3\n")
            finally:
                window.close()
                self.app.processEvents()

    def test_ctrl_s_saves_the_active_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-editor-shortcut-") as temp:
            root = Path(temp)
            source = root / "module.py"
            source.write_bytes(b"value = 1\n")
            window = ClientWindow(root)
            try:
                window._open_file(source, preview=False)
                window.show()
                self.app.processEvents()
                editor = window._active_editor()
                self.assertIsNotNone(editor)
                editor.setPlainText("value = 2\n")
                editor.document().setModified(True)
                editor.setFocus()
                self.app.processEvents()
                self.assertTrue(editor.hasFocus())

                QTest.keyClick(editor, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
                self.app.processEvents()

                self.assertEqual(source.read_bytes(), b"value = 2\n")
                self.assertFalse(editor.document().isModified())
            finally:
                window.close()
                self.app.processEvents()


class GitDiffEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_same_named_files_keep_separate_git_diff_tabs_and_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-same-name-diff-") as temp:
            root = Path(temp)
            first_path = root / "src" / "module.py"
            second_path = root / "tests" / "module.py"
            first_path.parent.mkdir()
            second_path.parent.mkdir()
            first_path.write_text("value = 1\n", encoding="utf-8")
            second_path.write_text("value = 2\n", encoding="utf-8")
            window = ClientWindow(root)
            try:
                window.git.init()
                window.git.stage_all()
                window.git.commit_changes("baseline")
                first_path.write_text("value = 10\n", encoding="utf-8")
                second_path.write_text("value = 20\n", encoding="utf-8")

                window._show_diff_for_path(first_path)
                first_editor = window._active_editor()
                window._show_diff_for_path(second_path)
                second_editor = window._active_editor()

                self.assertIsNot(first_editor, second_editor)
                self.assertEqual(Path(first_editor.property("diff_path")).resolve(), first_path.resolve())
                self.assertEqual(Path(second_editor.property("diff_path")).resolve(), second_path.resolve())
                self.assertIn("src/module.py", first_editor.toPlainText())
                self.assertIn("tests/module.py", second_editor.toPlainText())
            finally:
                window.close()
                self.app.processEvents()


class GitWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_git_panel_stages_unstages_and_commits_workspace_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-git-panel-") as temp:
            root = Path(temp)
            source = root / "src" / "module.py"
            source.parent.mkdir()
            window = ClientWindow(root)
            try:
                window.init_git()
                source.write_text("value = 1\n", encoding="utf-8")
                window.refresh_git_status()

                item = next(
                    window.git_changes_tree.topLevelItem(index)
                    for index in range(window.git_changes_tree.topLevelItemCount())
                    if window.git_changes_tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole)
                    == "src/module.py"
                )
                window.git_changes_tree.setCurrentItem(item)
                window.stage_selected_change()
                self.assertEqual(window.git.status_entries()[0]["code"], "A ")

                window.unstage_selected_change()
                self.assertEqual(window.git.status_entries()[0]["code"], "??")

                window.stage_all_changes()
                self.assertEqual(window.git.status_entries()[0]["code"], "A ")
                window.git_commit_input.setText("test: commit from Git panel")
                window.commit_workspace()

                self.assertTrue(window.git.head_sha())
                self.assertEqual(window.git.status_entries(), [])
                self.assertIn("test: commit from Git panel", window.git.log())
            finally:
                window.close()
                self.app.processEvents()


class EditorTabBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_preview_click_does_not_unpin_an_open_tab_or_discard_dirty_preview(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-preview-tabs-") as temp:
            root = Path(temp)
            paths = [root / f"file-{index}.py" for index in range(1, 4)]
            for index, path in enumerate(paths, start=1):
                path.write_text(f"value = {index}\n", encoding="utf-8")

            window = ClientWindow(root)
            try:
                window._open_file(paths[0], preview=False)
                pinned_editor = window._active_editor()

                window._open_file(paths[0], preview=True)
                self.assertIsNone(window._preview_editor)

                window._open_file(paths[1], preview=True)
                preview_editor = window._active_editor()
                self.assertIsNot(preview_editor, pinned_editor)
                self.assertIs(window._preview_editor, preview_editor)
                preview_editor.insertPlainText("# keep unsaved preview\n")

                window._open_file(paths[2], preview=True)
                self.assertIsNot(window._active_editor(), preview_editor)
                self.assertTrue(preview_editor.document().isModified())
                self.assertIn("# keep unsaved preview", preview_editor.toPlainText())
                self.assertEqual(window._editor_paths[pinned_editor].resolve(), paths[0].resolve())
                preview_editor.document().setModified(False)
            finally:
                window.close()
                self.app.processEvents()


class ExplorerInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_double_clicking_a_file_pins_it_and_shows_its_contents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-explorer-double-click-") as temp:
            root = Path(temp)
            source = root / "src" / "hello.py"
            source.parent.mkdir()
            content = 'print("opened from explorer")\n'
            source.write_text(content, encoding="utf-8")
            window = ClientWindow(root)
            try:
                window.resize(1280, 820)
                window.show()
                self.app.processEvents()
                QTest.qWait(100)

                source_index = window.file_model.index(str(source))
                index = window.file_proxy.mapFromSource(source_index)
                self.assertTrue(index.isValid())
                window.file_tree.expand(index.parent())
                self.app.processEvents()
                QTest.qWait(80)
                rect = window.file_tree.visualRect(index)
                self.assertTrue(rect.isValid())

                QTest.mouseDClick(
                    window.file_tree.viewport(),
                    Qt.MouseButton.LeftButton,
                    pos=rect.center(),
                )
                self.app.processEvents()

                editor = window._active_editor()
                self.assertEqual(window.current_file.resolve(), source.resolve())
                self.assertEqual(editor.toPlainText(), content)
                self.assertIn(editor, window._pinned_editors)
                self.assertIsNot(window._preview_editor, editor)
            finally:
                window.close()
                self.app.processEvents()


class FindReplaceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_ctrl_f_and_ctrl_h_open_the_expected_find_modes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-find-shortcuts-") as temp:
            root = Path(temp)
            source = root / "module.py"
            source.write_text("needle\n", encoding="utf-8")
            window = ClientWindow(root)
            try:
                window.show()
                window._open_file(source, preview=False)
                editor = window._active_editor()
                window.activateWindow()
                editor.setFocus()
                self.app.processEvents()

                QTest.keyClick(editor, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
                self.app.processEvents()
                self.assertTrue(window.find_bar.isVisible())
                self.assertFalse(window.replace_input.isVisible())

                window.find_input.setText("needle")
                QTest.keyClick(window.find_input, Qt.Key.Key_H, Qt.KeyboardModifier.ControlModifier)
                self.app.processEvents()
                self.assertTrue(window.replace_input.isVisible())
                self.assertTrue(window.replace_button.isVisible())
                self.assertTrue(window.replace_all_button.isVisible())
                self.assertEqual(window.find_input.text(), "needle")

                modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
                QTest.keyClick(window.find_input, Qt.Key.Key_F, modifiers)
                self.app.processEvents()
                self.assertTrue(window.bottom_tabs.isVisible())
                self.assertIs(window.workspace_stack.currentWidget(), window.workspace_page)
                self.assertTrue(window.workspace_search_input.hasFocus())
            finally:
                window.close()
                self.app.processEvents()

    def test_whole_word_navigation_and_replace_buttons_update_the_document(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-find-replace-") as temp:
            root = Path(temp)
            source = root / "module.py"
            source.write_text("cat scatter CAT catapult\n", encoding="utf-8")
            window = ClientWindow(root)
            try:
                window._open_file(source, preview=False)
                editor = window._active_editor()
                window._show_find_bar(replace=True)
                window.find_word_checkbox.setChecked(True)
                window.find_input.setText("cat")
                self.assertEqual(editor.textCursor().selectedText(), "cat")

                QTest.keyClick(window.find_input, Qt.Key.Key_Return)
                self.assertEqual(editor.textCursor().selectedText(), "CAT")

                window.replace_input.setText("dog")
                QTest.mouseClick(window.replace_button, Qt.MouseButton.LeftButton)
                self.assertIn("scatter dog catapult", editor.toPlainText())

                cursor = editor.textCursor()
                cursor.movePosition(cursor.MoveOperation.Start)
                editor.setTextCursor(cursor)
                QTest.mouseClick(window.replace_all_button, Qt.MouseButton.LeftButton)
                self.assertEqual(editor.toPlainText(), "dog scatter dog catapult\n")
                editor.document().setModified(False)
            finally:
                window.close()
                self.app.processEvents()


class SymbolNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_shift_f12_shows_case_sensitive_python_references(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-symbol-references-") as temp:
            root = Path(temp)
            source = root / "module.py"
            content = "value = 1\nprint(value)\nprint(VALUE)\n"
            source.write_text(content, encoding="utf-8")
            window = ClientWindow(root)
            try:
                window.show()
                window._open_file(source, preview=False)
                editor = window._active_editor()
                editor.setFocus()
                cursor = editor.textCursor()
                cursor.setPosition(content.index("value", content.index("print")))
                editor.setTextCursor(cursor)
                window.show_git()
                window.bottom_tabs.hide()
                self.app.processEvents()

                QTest.keyClick(
                    editor,
                    Qt.Key.Key_F12,
                    Qt.KeyboardModifier.ShiftModifier,
                )
                self.app.processEvents()

                self.assertIs(window.workspace_stack.currentWidget(), window.workspace_page)
                self.assertTrue(window.bottom_tabs.isVisible())
                self.assertEqual(window.bottom_tabs.currentIndex(), 0)
                self.assertEqual(window.workspace_search_results.topLevelItemCount(), 2)
                result_text = "\n".join(
                    window.workspace_search_results.topLevelItem(index).text(0)
                    for index in range(window.workspace_search_results.topLevelItemCount())
                )
                self.assertNotIn("VALUE", result_text)
            finally:
                window.close()
                self.app.processEvents()

    def test_f12_definition_navigation_round_trips_with_alt_left_and_right(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-definition-history-") as temp:
            root = Path(temp)
            library = root / "library.py"
            library.write_text("def target():\n    return 7\n", encoding="utf-8")
            caller = root / "caller.py"
            caller_text = "def run():\n    return target()\n"
            caller.write_text(caller_text, encoding="utf-8")
            window = ClientWindow(root)
            try:
                window.show()
                window._open_file(caller, preview=False)
                editor = window._active_editor()
                cursor = editor.textCursor()
                cursor.setPosition(caller_text.rindex("target"))
                editor.setTextCursor(cursor)
                editor.setFocus()
                self.app.processEvents()

                QTest.keyClick(editor, Qt.Key.Key_F12)
                self.app.processEvents()
                self.assertEqual(window.current_file.resolve(), library.resolve())
                self.assertEqual(window._active_editor().textCursor().blockNumber(), 0)

                QTest.keyClick(
                    window._active_editor(),
                    Qt.Key.Key_Left,
                    Qt.KeyboardModifier.AltModifier,
                )
                self.app.processEvents()
                self.assertEqual(window.current_file.resolve(), caller.resolve())
                self.assertEqual(window._active_editor().textCursor().blockNumber(), 1)

                QTest.keyClick(
                    window._active_editor(),
                    Qt.Key.Key_Right,
                    Qt.KeyboardModifier.AltModifier,
                )
                self.app.processEvents()
                self.assertEqual(window.current_file.resolve(), library.resolve())
                self.assertEqual(window._active_editor().textCursor().blockNumber(), 0)
            finally:
                window.close()
                self.app.processEvents()

    def test_f2_renames_python_name_tokens_without_changing_strings_or_comments(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-symbol-rename-") as temp:
            root = Path(temp)
            source = root / "module.py"
            original = 'value = 1\ncopy = value\nlabel = f"{value}"\nmessage = "value"\n# value comment\n'
            source.write_text(original, encoding="utf-8")
            window = ClientWindow(root)
            try:
                window._open_file(source, preview=False)
                editor = window._active_editor()
                cursor = editor.textCursor()
                cursor.setPosition(original.index("value"))
                editor.setTextCursor(cursor)

                with patch("scidev_client.QInputDialog.getText", return_value=("count", True)):
                    window._rename_current_symbol()

                self.assertEqual(
                    editor.toPlainText(),
                    'count = 1\ncopy = count\nlabel = f"{count}"\nmessage = "value"\n# value comment\n',
                )
            finally:
                window.close()
                self.app.processEvents()

    def test_f2_accepts_unicode_identifiers_and_rejects_python_keywords(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-unicode-symbol-rename-") as temp:
            root = Path(temp)
            source = root / "module.py"
            original = "变量 = 1\nprint(变量)\n文本 = '变量'\n"
            source.write_text(original, encoding="utf-8")
            window = ClientWindow(root)
            try:
                window._open_file(source, preview=False)
                editor = window._active_editor()
                cursor = editor.textCursor()
                cursor.setPosition(original.index("变量"))
                editor.setTextCursor(cursor)

                with patch("scidev_client.QInputDialog.getText", return_value=("计数", True)):
                    window._rename_current_symbol()

                renamed = "计数 = 1\nprint(计数)\n文本 = '变量'\n"
                self.assertEqual(editor.toPlainText(), renamed)
                editor.document().setModified(False)
                cursor = editor.textCursor()
                cursor.setPosition(renamed.index("计数"))
                editor.setTextCursor(cursor)

                with patch("scidev_client.QInputDialog.getText", return_value=("class", True)):
                    window._rename_current_symbol()

                self.assertEqual(editor.toPlainText(), renamed)
            finally:
                window.close()
                self.app.processEvents()

    def test_f2_refuses_an_incomplete_python_string_without_partial_edits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-incomplete-symbol-rename-") as temp:
            root = Path(temp)
            source = root / "module.py"
            original = 'value = 1\ntext = """unfinished\nvalue\n'
            source.write_text(original, encoding="utf-8")
            window = ClientWindow(root)
            try:
                window._open_file(source, preview=False)
                editor = window._active_editor()
                cursor = editor.textCursor()
                cursor.setPosition(original.index("value"))
                editor.setTextCursor(cursor)

                with patch("scidev_client.QInputDialog.getText", return_value=("count", True)):
                    window._rename_current_symbol()

                self.assertEqual(editor.toPlainText(), original)
                self.assertIn("无法安全分词", window.statusBar().currentMessage())
            finally:
                window.close()
                self.app.processEvents()


class QuickOpenUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_line_and_column_only_navigation_uses_the_active_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-quick-open-location-") as temp:
            root = Path(temp)
            active_file = root / "src" / "long_module.py"
            active_file.parent.mkdir()
            active_file.write_text("first\nsecond\nthird line\n", encoding="utf-8")
            (root / "a.py").write_text("short file\n", encoding="utf-8")
            window = ClientWindow(root)
            try:
                window.show()
                window._open_file(active_file, preview=False)
                editor = window._active_editor()
                window.activateWindow()
                editor.setFocus()
                self.app.processEvents()

                QTest.keyClick(editor, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier)
                self.assertTrue(window.command_search.hasFocus())
                window.command_search.setText(":3:2")
                QTest.keyClick(window.command_search, Qt.Key.Key_Return)
                self.app.processEvents()

                self.assertEqual(window.current_file.resolve(), active_file.resolve())
                self.assertEqual(window._active_editor().textCursor().blockNumber(), 2)
                self.assertEqual(window._active_editor().textCursor().columnNumber(), 1)
            finally:
                window.close()
                self.app.processEvents()

    def test_line_navigation_without_an_active_file_does_not_open_an_arbitrary_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-quick-open-no-current-") as temp:
            root = Path(temp)
            unrelated = root / "a.py"
            unrelated.write_text("unrelated\n", encoding="utf-8")
            window = ClientWindow(root)
            try:
                window.command_search.setText(":3")
                window._open_quick_search()

                self.assertIsNone(window.current_file)
                self.assertEqual(window.command_search.text(), ":3")
                self.assertEqual(unrelated.read_text(encoding="utf-8"), "unrelated\n")
                self.assertIn("先打开文件", window.statusBar().currentMessage())
            finally:
                window.close()
                self.app.processEvents()


class ExplorerInlineEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_switching_create_mode_to_folder_keeps_entry_open_and_creates_folder(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-explorer-entry-") as temp:
            root = Path(temp)
            source_dir = root / "src"
            source_dir.mkdir()
            window = ClientWindow(root)
            try:
                window.show()
                self.app.processEvents()

                window.start_new_file_entry(source_dir)
                self.assertTrue(window.new_file_entry.isVisible())
                window.start_new_folder_entry(source_dir)

                self.assertTrue(window.new_file_entry.isVisible())
                self.assertEqual(window._explorer_entry_mode, "folder")
                self.assertIn("文件夹", window.new_file_entry.placeholderText())
                window.new_file_entry.setText("analysis")
                window.create_new_file()

                analysis_dir = source_dir / "analysis"
                self.assertTrue(analysis_dir.is_dir())
                self.assertFalse(window.new_file_entry.isVisible())

                window.start_new_file_entry(analysis_dir)
                self.assertTrue(window.new_file_entry.isVisible())
                window.new_file_entry.setText("module.py")
                window.create_new_file()
                source = analysis_dir / "module.py"
                self.assertTrue(source.is_file())

                editor = window._active_editor()
                self.assertIsNotNone(editor)
                window.start_rename_entry(source)
                window.new_file_entry.setText("renamed.py")
                window.create_new_file()
                renamed = analysis_dir / "renamed.py"
                self.assertTrue(renamed.is_file())
                self.assertFalse(source.exists())
                self.assertEqual(window._editor_paths[editor].resolve(), renamed.resolve())
            finally:
                window.close()
                self.app.processEvents()

    def test_new_file_cannot_be_created_beneath_nested_internal_directories(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-explorer-internal-path-") as temp:
            root = Path(temp)
            source_dir = root / "src"
            source_dir.mkdir()
            window = ClientWindow(root)
            try:
                window.show()
                self.app.processEvents()
                window.start_new_file_entry(source_dir)
                window.new_file_entry.setText(".research/hidden.py")
                window.create_new_file()

                self.assertFalse((source_dir / ".research").exists())
                self.assertTrue(window.new_file_entry.isVisible())
                self.assertIn("内部目录", window.statusBar().currentMessage())
            finally:
                window.close()
                self.app.processEvents()

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior is platform-specific")
    def test_junction_is_not_exposed_as_a_deletable_project_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-explorer-junction-") as temp:
            root = Path(temp) / "project"
            actual = root / "actual_data"
            actual.mkdir(parents=True)
            protected = actual / "keep.txt"
            protected.write_text("junction-only-search-sentinel\n", encoding="utf-8")
            junction = root / "linked_data"
            result = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(junction), str(actual)],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertTrue(junction.is_junction())

            window = ClientWindow(root)
            try:
                window.show()
                deadline = time.monotonic() + 3
                source_index = window.file_model.index(str(junction))
                while time.monotonic() < deadline and not source_index.isValid():
                    self.app.processEvents()
                    QTest.qWait(10)
                    source_index = window.file_model.index(str(junction))
                self.assertTrue(source_index.isValid())

                listed_paths = {
                    path.resolve().relative_to(root.resolve()).as_posix()
                    for path in window._project_files()
                }
                self.assertIn("actual_data/keep.txt", listed_paths)
                self.assertNotIn("linked_data/keep.txt", listed_paths)
                window.workspace_search_input.setText("junction-only-search-sentinel")
                window._search_workspace()
                self.assertEqual(window.workspace_search_results.topLevelItemCount(), 1)
                search_result = window.workspace_search_results.topLevelItem(0)
                self.assertEqual(
                    Path(str(search_result.data(0, Qt.ItemDataRole.UserRole)))
                    .resolve()
                    .relative_to(root.resolve())
                    .as_posix(),
                    "actual_data/keep.txt",
                )

                # The link must not be navigable as a normal project folder.
                self.assertFalse(window.file_proxy.mapFromSource(source_index).isValid())
                with patch.object(QMessageBox, "question") as confirm_delete:
                    window.delete_explorer_path(junction)
                confirm_delete.assert_not_called()
                self.assertTrue(junction.is_junction())
                self.assertEqual(
                    protected.read_text(encoding="utf-8"),
                    "junction-only-search-sentinel\n",
                )
            finally:
                window.close()
                self.app.processEvents()
                junction.rmdir()

    @unittest.skipUnless(os.name == "nt", "Windows short-path aliases are platform-specific")
    def test_rename_accepts_a_windows_short_path_alias(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-short-path-rename-") as temp:
            root = Path(temp)
            source = root / "src" / "module.py"
            source.parent.mkdir()
            source.write_text("value = 1\n", encoding="utf-8")

            short_path_buffer = ctypes.create_unicode_buffer(32768)
            get_short_path_name = ctypes.windll.kernel32.GetShortPathNameW
            get_short_path_name.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            get_short_path_name.restype = ctypes.c_uint
            length = get_short_path_name(str(root), short_path_buffer, len(short_path_buffer))
            self.assertGreater(length, 0)
            short_root = Path(short_path_buffer.value)
            self.assertNotEqual(short_root, root)

            window = ClientWindow(root)
            try:
                window.start_rename_entry(short_root / "src" / "module.py")
                window.new_file_entry.setText("renamed.py")
                window.create_new_file()

                renamed = root / "src" / "renamed.py"
                self.assertTrue(renamed.is_file())
                self.assertFalse(source.exists())
            finally:
                window.close()
                self.app.processEvents()


class WorkspaceSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_typing_quick_open_filters_project_files_without_qt_deprecation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-live-file-filter-") as temp:
            root = Path(temp)
            source_dir = root / "src"
            source_dir.mkdir()
            match = source_dir / "needle_module.py"
            match.write_text("value = 1\n", encoding="utf-8")
            miss = source_dir / "unrelated.py"
            miss.write_text("value = 2\n", encoding="utf-8")
            window = ClientWindow(root)
            try:
                window.show()
                window.command_search.setFocus()
                self.app.processEvents()
                with warnings.catch_warnings(record=True) as emitted:
                    warnings.simplefilter("always", DeprecationWarning)
                    QTest.keyClicks(window.command_search, "needle")
                    self.app.processEvents()

                self.assertEqual(window.file_proxy._filter_text, "needle")
                match_index = window.file_model.index(str(match))
                miss_index = window.file_model.index(str(miss))
                self.assertTrue(window.file_proxy.mapFromSource(match_index).isValid())
                self.assertFalse(window.file_proxy.mapFromSource(miss_index).isValid())
                self.assertFalse(
                    any("invalidateFilter" in str(warning.message) for warning in emitted)
                )
            finally:
                window.close()
                self.app.processEvents()

    def test_all_live_search_entries_accept_typing_without_qt_slot_errors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-search-black-box-") as temp:
            root = Path(temp)
            source = root / "example.py"
            source.write_text("def target():\n    return 1\nprint(target())\n", encoding="utf-8")
            window = ClientWindow(root)
            slot_errors: list[str] = []
            original_excepthook = sys.excepthook
            try:
                window.show()
                window._open_file(source, preview=False)
                self.app.processEvents()
                sys.excepthook = lambda kind, error, _traceback: slot_errors.append(
                    f"{kind.__name__}: {error}"
                )

                editor = window._active_editor()
                QTest.keyClick(editor, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier)
                QTest.keyClicks(window.command_search, "example")
                self.app.processEvents()
                self.assertEqual(window.file_proxy._filter_text, "example")

                QTest.keyClick(
                    window.command_search,
                    Qt.Key.Key_P,
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
                )
                QTest.keyClicks(window.command_search, "search")
                self.app.processEvents()
                self.assertEqual(window.command_completer.completionCount(), 1)

                QTest.keyClick(window.command_search, Qt.Key.Key_Escape)
                QTest.keyClick(
                    window.command_search,
                    Qt.Key.Key_F,
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
                )
                QTest.keyClicks(window.workspace_search_input, "target")
                QTest.keyClick(window.workspace_search_input, Qt.Key.Key_Return)
                self.app.processEvents()
                self.assertEqual(window.workspace_search_results.topLevelItemCount(), 2)

                QTest.keyClick(window.workspace_search_input, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
                QTest.keyClicks(window.find_input, "target")
                self.app.processEvents()
                self.assertEqual(window._active_editor().textCursor().selectedText(), "target")
                self.assertEqual(slot_errors, [])
            finally:
                sys.excepthook = original_excepthook
                window.close()
                self.app.processEvents()

    def test_quick_open_filter_falls_back_when_new_qt_api_is_unavailable(self) -> None:
        proxy = ProjectFilterProxy()
        with (
            patch.object(proxy, "beginFilterChange", None),
            patch.object(proxy, "endFilterChange", None),
            patch.object(proxy, "invalidateRowsFilter") as invalidate_rows,
        ):
            proxy.set_filter_text("Ex")

        self.assertEqual(proxy._filter_text, "ex")
        invalidate_rows.assert_called_once_with()

    def test_search_skips_oversized_files_without_reading_them_and_opens_results(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-workspace-search-") as temp:
            root = Path(temp)
            source = root / "src" / "module.py"
            source.parent.mkdir()
            source.write_text("ignore this\nNeedle found here\n", encoding="utf-8")
            oversized = root / "checkpoint.bin"
            with oversized.open("wb") as stream:
                stream.truncate(CodingToolbox.MAX_READ_BYTES + 1)

            original_open = Path.open

            def reject_oversized_open(path: Path, *args, **kwargs):
                if path == oversized:
                    raise AssertionError("workspace search must not open an oversized file")
                return original_open(path, *args, **kwargs)

            window = ClientWindow(root)
            try:
                window.workspace_search_input.setFocus()
                with patch.object(Path, "open", reject_oversized_open):
                    QTest.keyClicks(window.workspace_search_input, "needle")
                    QTest.keyClick(window.workspace_search_input, Qt.Key.Key_Return)
                    self.app.processEvents()

                self.assertEqual(window.workspace_search_results.topLevelItemCount(), 1)
                result = window.workspace_search_results.topLevelItem(0)
                result_path = Path(str(result.data(0, Qt.ItemDataRole.UserRole)))
                self.assertEqual(result_path.resolve(), source.resolve())
                self.assertEqual(result.data(0, Qt.ItemDataRole.UserRole + 1), 2)

                window._open_search_result(result)
                self.assertEqual(window.current_file.resolve(), source.resolve())
                self.assertEqual(window._active_editor().textCursor().blockNumber(), 1)
            finally:
                window.close()
                self.app.processEvents()

    def test_case_variant_internal_directory_is_hidden_from_tree_search_and_delete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-case-variant-internal-") as temp:
            root = Path(temp)
            internal_dir = root / ".RESEARCH"
            internal_dir.mkdir()
            secret = internal_dir / "private.txt"
            secret.write_text("case-variant-search-sentinel\n", encoding="utf-8")
            visible = root / "visible.py"
            visible.write_text("ordinary project source\n", encoding="utf-8")

            window = ClientWindow(root)
            try:
                window.show()
                self.app.processEvents()

                project_files = window._project_files()
                resolved_project_files = {path.resolve() for path in project_files}
                self.assertIn(visible.resolve(), resolved_project_files)
                self.assertNotIn(secret.resolve(), resolved_project_files)

                source_index = window.file_model.index(str(internal_dir))
                self.assertTrue(source_index.isValid())
                self.assertFalse(window.file_proxy.mapFromSource(source_index).isValid())

                window.workspace_search_input.setText("case-variant-search-sentinel")
                window._search_workspace()
                self.assertEqual(window.workspace_search_results.topLevelItemCount(), 0)

                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.No,
                ) as confirm_delete:
                    window.delete_explorer_path(internal_dir)
                confirm_delete.assert_not_called()
                self.assertTrue(secret.is_file())
            finally:
                window.close()
                self.app.processEvents()


class WorkspaceLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_reselecting_an_open_workspace_updates_startup_restore_path(self) -> None:
        settings = QSettings("SciDevHarness", "SciDevHarness")
        had_previous = settings.contains("lastWorkspace")
        previous = settings.value("lastWorkspace") if had_previous else None
        had_windows = hasattr(self.app, "_scidev_windows")
        previous_windows = getattr(self.app, "_scidev_windows", None)
        with tempfile.TemporaryDirectory(prefix="scidev-workspace-lifecycle-") as temp:
            root = Path(temp)
            workspace_a = root / "workspace-a"
            workspace_b = root / "workspace-b"
            workspace_a.mkdir()
            workspace_b.mkdir()
            first = ClientWindow(workspace_a)
            second = ClientWindow(workspace_b)
            try:
                self.app._scidev_windows = [first, second]
                settings.setValue("lastWorkspace", str(workspace_b.resolve()))
                selected = first._open_workspace_root(workspace_a)

                self.assertIs(selected, first)
                self.assertEqual(
                    Path(str(settings.value("lastWorkspace"))).resolve(),
                    workspace_a.resolve(),
                )
            finally:
                first.close()
                second.close()
                self.app.processEvents()
                if had_windows:
                    self.app._scidev_windows = previous_windows
                elif hasattr(self.app, "_scidev_windows"):
                    del self.app._scidev_windows
                if had_previous:
                    settings.setValue("lastWorkspace", previous)
                else:
                    settings.remove("lastWorkspace")
                settings.sync()


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


class SummarySettingsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_summary_frequency_and_enabled_state_persist_from_the_chat_controls(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-summary-settings-ui-") as temp:
            root = Path(temp)
            window = ClientWindow(root)
            try:
                window.show()
                window.toggle_summary_settings()
                self.app.processEvents()
                self.assertTrue(window.summary_settings_panel.isVisible())

                interval = 7 if window.summary_interval_spin.value() != 7 else 8
                window.summary_interval_spin.setValue(interval)
                settings_path = SummarySettings.config_path(root)
                saved = json.loads(settings_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["summary_interval_turns"], interval)
                self.assertIs(window.agent.summary_settings, window.summary_settings)
                self.assertIn(f"{interval}轮", window.summary_chip.text())

                window.summary_enabled_checkbox.setChecked(False)
                saved = json.loads(settings_path.read_text(encoding="utf-8"))
                self.assertFalse(saved["summary_enabled"])
                self.assertEqual(window.summary_chip.text(), "总结关")

                window.resize(820, 600)
                self.app.processEvents()
                QTest.qWait(30)
                self.assertTrue(window._summary_settings_compact)
                self.assertEqual(window.summary_enabled_checkbox.text(), "对话结束自动总结")
                self.assertEqual(window.summary_state_label.text(), "已关闭")
                self.assertFalse(window.summary_failure_note.isVisible())
                self.assertFalse(window.summary_frequency_note.isVisible())
                for label, control in (
                    (window.summary_model_label, window.summary_model_edit),
                    (window.summary_tokens_label, window.summary_tokens_spin),
                    (window.summary_context_label, window.summary_context_spin),
                    (window.summary_retry_label, window.summary_retry_spin),
                    (window.summary_interval_label, window.summary_interval_spin),
                    (window.summary_instruction_label, window.summary_instruction_edit),
                ):
                    self.assertLess(label.geometry().right(), control.geometry().left())
                    self.assertLessEqual(control.geometry().right(), window.summary_settings_panel.rect().right())

                window.resize(1120, 700)
                self.app.processEvents()
                QTest.qWait(30)
                self.assertTrue(window._summary_settings_compact)
                self.assertEqual(window.summary_chip.text(), "关")

                window.resize(1500, 920)
                self.app.processEvents()
                QTest.qWait(30)
                self.assertFalse(window._summary_settings_compact)
                self.assertEqual(window.summary_chip.text(), "总结关")
            finally:
                window.close()
                self.app.processEvents()


class StreamingAgentUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_prompt_is_preserved_when_another_task_is_still_running(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-chat-busy-input-") as temp:
            window = ClientWindow(Path(temp))
            try:
                window.active_task_id = "task_in_progress"
                prompt = "请接着检查第二个文件"
                window.chat_input.setPlainText(prompt)
                window.chat_input.setFocus()
                self.app.processEvents()

                with (
                    patch.object(window.worker, "submit") as submit_task,
                    patch.object(window, "_append_chat", wraps=window._append_chat) as append_chat,
                ):
                    QTest.keyClick(
                        window.chat_input,
                        Qt.Key.Key_Return,
                        Qt.KeyboardModifier.ControlModifier,
                    )
                    self.app.processEvents()

                self.assertEqual(window.chat_input.toPlainText(), prompt)
                submit_task.assert_not_called()
                append_chat.assert_any_call(
                    "系统",
                    "当前任务仍在执行，请等待 Agent 完成后继续。",
                    "meta",
                )
            finally:
                window.close()
                self.app.processEvents()

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


class TerminalUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_real_terminal_collects_quoted_command_output_and_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-terminal-ui-") as temp:
            window = ClientWindow(Path(temp))
            try:
                window.show()
                self.app.processEvents()
                script = (
                    "import sys; print('stdout-sentinel'); "
                    "print('stderr-sentinel', file=sys.stderr); "
                    "print('argument=' + sys.argv[1]); sys.exit(7)"
                )
                window.terminal_input.setText(
                    subprocess.list2cmdline([sys.executable, "-c", script, "argument with spaces"])
                )
                window._run_terminal_command()

                deadline = time.monotonic() + 10
                while window.terminal_process is not None and time.monotonic() < deadline:
                    self.app.processEvents()
                    QTest.qWait(10)

                self.assertIsNone(window.terminal_process, "terminal process did not finish")
                terminal_text = window.terminal_output.toPlainText()
                self.assertIn("stdout-sentinel", terminal_text)
                self.assertIn("stderr-sentinel", terminal_text)
                self.assertIn("argument=argument with spaces", terminal_text)
                self.assertIn("[退出码 7]", terminal_text)
                self.assertIn("stderr-sentinel", window.problems_output.toPlainText())
            finally:
                window.close()
                self.app.processEvents()

    def test_terminal_keeps_utf8_characters_split_across_output_events(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-terminal-utf8-split-") as temp:
            window = ClientWindow(Path(temp))
            try:
                window.show()
                self.app.processEvents()
                script = (
                    "import sys,time; "
                    "sys.stdout.buffer.write(bytes([0xe4])); sys.stdout.buffer.flush(); "
                    "sys.stderr.buffer.write(bytes([0xe5])); sys.stderr.buffer.flush(); "
                    "time.sleep(0.2); "
                    "sys.stdout.buffer.write(bytes([0xb8,0xad])); sys.stdout.buffer.flush(); "
                    "sys.stderr.buffer.write(bytes([0xad,0x97])); sys.stderr.buffer.flush()"
                )
                window.terminal_input.setText(subprocess.list2cmdline([sys.executable, "-c", script]))
                window._run_terminal_command()

                deadline = time.monotonic() + 10
                while window.terminal_process is not None and time.monotonic() < deadline:
                    self.app.processEvents()
                    QTest.qWait(10)

                self.assertIsNone(window.terminal_process, "terminal process did not finish")
                terminal_text = window.terminal_output.toPlainText()
                self.assertIn("中", terminal_text)
                self.assertIn("字", terminal_text)
            finally:
                window.close()
                self.app.processEvents()

    def test_terminal_decoder_preserves_split_legacy_codepage_text(self) -> None:
        decoder = TerminalOutputDecoder("gbk")
        self.assertEqual(decoder.decode(bytes([0xd7])), "")
        self.assertEqual(decoder.decode(bytes([0xd6]), final=True), "字")


if __name__ == "__main__":
    unittest.main()
