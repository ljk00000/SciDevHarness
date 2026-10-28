"""Live settings must remain searchable, persistent and non-destructive."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
if os.name == "nt":
    os.environ["QT_QPA_FONTDIR"] = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QFontInfo
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit

from scidev_client import ClientWindow
from scidev_preferences import AppearancePreferences
from scidev_ui import accent_text_color, create_ui_font


class PreferenceValidationTests(unittest.TestCase):
    def test_invalid_values_are_bounded_and_unknown_fields_rejected(self):
        p = AppearancePreferences()
        self.assertEqual(p.with_value("font_size", 999).font_size, 16)
        self.assertEqual(p.with_value("zoom", "nan").zoom, 0)
        self.assertEqual(p.with_value("zoom", 999).zoom, 0)
        self.assertEqual(p.with_value("font_weight", 950).font_weight, 400)
        self.assertEqual(p.with_value("font_family", 'bad"; color:red;').font_family, "")
        self.assertEqual(p.with_value("accent_color", "#ffffff").accent_color, "#ffffff")
        self.assertEqual(p.with_value("accent_color", "red").accent_color, "")
        self.assertFalse(p.with_value("editor_wrap", "false").editor_wrap)
        with self.assertRaises(ValueError):
            p.with_value("save", "anything")

    def test_custom_accent_selects_legible_foreground(self):
        self.assertEqual(accent_text_color("#ffffff"), "#10151b")
        self.assertEqual(accent_text_color("#ffff00"), "#10151b")
        self.assertEqual(accent_text_color("#000000"), "#ffffff")


class WorkbenchSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setFont(create_ui_font())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scidev-settings-test-")
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root / "ui.ini"), QSettings.Format.IniFormat)
        self.settings_patch = patch("scidev_client.QSettings", return_value=self.settings)
        self.settings_patch.start()
        self.window = ClientWindow(self.root)
        self.window.show()
        self.settle()

    def settle(self):
        self.app.processEvents()
        QTest.qWait(35)
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.settle()
        self.settings_patch.stop()
        self.temp.cleanup()

    def control(self, key):
        return self.window.settings_page.controls[key]

    def choose(self, key, value):
        box = self.control(key)
        box.setCurrentIndex(box.findData(value))
        self.settle()

    def test_gear_shortcut_and_command_open_same_nonmodal_page(self):
        self.window.settings_button.click()
        self.settle()
        self.assertIs(self.window.workspace_stack.currentWidget(), self.window.settings_page)
        self.assertTrue(self.window.settings_button.isChecked())
        self.window.show_workspace()
        QTest.keyClick(self.window, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
        self.settle()
        self.assertIs(self.window.workspace_stack.currentWidget(), self.window.settings_page)
        self.window.show_workspace()
        self.window._run_command_completion("> settings")
        self.settle()
        self.assertIs(self.window.workspace_stack.currentWidget(), self.window.settings_page)
        self.assertFalse(self.window.settings_page.isWindow())
        self.assertFalse(any(widget.isVisible() and widget.isModal() for widget in self.app.topLevelWidgets() if hasattr(widget, "isModal")))

    def test_search_categories_modified_filter_and_empty_state(self):
        self.window.show_settings()
        page = self.window.settings_page
        page.search.setText("editor font")
        self.settle()
        visible = {key for key, row in page.rows.items() if row.isVisible()}
        self.assertEqual(visible, {"editor_font_family", "editor_font_size"})
        page.search.clear()
        page.categories.setCurrentRow(3)
        self.settle()
        self.assertTrue(page.rows["rail_width"].isVisible())
        self.assertFalse(page.rows["font_family"].isVisible())
        page.categories.setCurrentRow(0)
        page.modified_only.setChecked(True)
        self.settle()
        self.assertTrue(page.empty.isVisible())
        self.window._on_appearance_setting_changed("rail_width", 88)
        self.settle()
        self.assertTrue(page.rows["rail_width"].isVisible())
        self.assertFalse(page.rows["font_size"].isVisible())
        page.search.setText("no-such-setting")
        self.settle()
        self.assertTrue(page.empty.isVisible())

    def test_live_font_icon_zoom_theme_and_restart_persistence(self):
        self.window.show_settings()
        self.control("font_size").setValue(13)
        self.control("rail_width").setValue(88)
        self.control("icon_size").setValue(36)
        self.choose("zoom", 125)
        self.choose("control_weight", 700)
        self.choose("profile", "paper")
        self.assertEqual(self.window.ui_profile, "paper")
        self.assertAlmostEqual(self.window._ui_scale, 1.25)
        self.assertEqual(self.window.activity_rail.width(), 110)
        self.assertEqual(self.window.settings_button.iconSize().width(), 45)
        self.assertEqual(self.window.title_new_button.font().weight(), 700)
        self.assertAlmostEqual(self.window.title_new_button.font().pointSizeF(), 16.25, delta=0.1)
        sibling = ClientWindow(self.root)
        try:
            self.assertEqual(sibling.appearance_preferences, self.window.appearance_preferences)
            self.assertEqual(sibling.ui_profile, "paper")
        finally:
            sibling.close()

    def test_editor_preferences_preserve_unsaved_text_cursor_and_undo(self):
        source = self.root / "source.py"
        source.write_text("value = 1\n", encoding="utf-8")
        self.window._open_file(source)
        editor = self.window.code_editor
        editor.moveCursor(editor.textCursor().MoveOperation.End)
        editor.insertPlainText("# unsaved\n")
        before = editor.toPlainText(), editor.textCursor().position(), editor.document().isModified()
        self.window.show_settings()
        self.control("editor_font_size").setValue(18)
        self.control("editor_tab_size").setValue(8)
        self.choose("editor_wrap", True)
        self.choose("line_numbers", False)
        self.choose("highlight_line", False)
        self.assertEqual((editor.toPlainText(), editor.textCursor().position(), editor.document().isModified()), before)
        self.assertTrue(editor.document().isUndoAvailable())
        self.assertTrue(QFontInfo(editor.font()).fixedPitch())
        self.assertEqual(editor.font().pointSizeF(), 18)
        self.assertEqual(editor.lineWrapMode(), QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.assertEqual(editor.line_number_area_width(), 0)
        self.assertTrue(editor.line_number_area.isHidden())
        self.assertFalse(editor.extraSelections())
        self.assertIsNone(self.window._active_editor())
        self.window._close_settings()
        self.assertIs(self.window._active_editor(), editor)
        self.assertEqual(source.read_text(encoding="utf-8"), "value = 1\n")
        editor.undo()
        self.assertEqual(editor.toPlainText(), "value = 1\n")
        editor.document().setModified(False)

    def test_custom_accent_validation_and_reset_do_not_touch_agent_settings(self):
        self.window.show_settings()
        field = self.control("accent_color")
        field.setText("not-a-color")
        field.editingFinished.emit()
        self.assertTrue(field.property("invalid"))
        self.assertEqual(self.window.appearance_preferences.accent_color, "")
        field.setText("#ffff00")
        field.editingFinished.emit()
        self.settle()
        self.assertEqual(self.settings.value("appearance/accent_color"), "#ffff00")
        self.assertIn("color: #10151b", self.window.styleSheet())
        self.settings.setValue("planningMode", "required")
        self.window._reset_appearance()
        self.settle()
        self.assertEqual(self.window.appearance_preferences, AppearancePreferences())
        self.assertEqual(self.settings.value("planningMode"), "required")

    def test_layout_visibility_restart_and_agent_entry(self):
        self.window.show_settings()
        self.choose("explorer_mode", "hide")
        self.assertTrue(self.window.explorer.isHidden())
        sibling = ClientWindow(self.root)
        try:
            sibling.show()
            self.settle()
            self.assertTrue(sibling.explorer.isHidden())
        finally:
            sibling.close()
        self.window._open_agent_settings_from_preferences()
        self.assertTrue(self.window.summary_settings_panel.isVisible())
        self.assertIs(self.window.workspace_stack.currentWidget(), self.window.settings_page)

    def test_default_and_large_settings_remain_scrollable_at_narrow_sizes(self):
        self.window.show_settings()
        page = self.window.settings_page
        for size in ((1500, 920), (940, 620), (780, 480)):
            self.window.resize(*size)
            self.settle()
            self.assertEqual(
                page.categories.isVisible(),
                page.width() >= page.COMPACT_BREAKPOINT,
            )
            self.assertEqual(
                page.category_combo.isVisible(),
                page.width() < page.COMPACT_BREAKPOINT,
            )
            self.assertLessEqual(page.scroll.widget().width(), page.scroll.viewport().width())
            self.assertGreater(page.scroll.verticalScrollBar().maximum(), 0)
        self.window._on_appearance_setting_changed("font_size", 16)
        self.choose("zoom", 150)
        self.settle()
        self.assertLessEqual(page.scroll.widget().width(), page.scroll.viewport().width())
        for control in page.controls.values():
            self.assertGreater(control.height(), control.fontMetrics().height())

    def test_settings_titles_are_legible_in_all_themes_and_reset_clears_errors(self):
        self.window.show_settings()
        for theme, foreground in (("studio", "#dce4ed"), ("paper", "#263346"), ("focus", "#eeedf5")):
            self.window.set_ui_profile(theme, persist=False)
            self.settle()
            for label in self.window.settings_page.findChildren(QLabel):
                if label.objectName() in ("PreferencesTitle", "PreferenceName"):
                    self.assertEqual(label.palette().color(label.foregroundRole()).name(), foreground)
        field = self.control("accent_color")
        field.setText("invalid")
        field.editingFinished.emit()
        self.assertTrue(field.property("invalid"))
        self.window._reset_appearance()
        self.assertFalse(field.property("invalid"))

    def test_settings_return_to_tree_and_sync_between_windows(self):
        self.window.show_git()
        self.window.show_settings()
        self.window._close_settings()
        self.assertIs(self.window.workspace_stack.currentWidget(), self.window.git_page)
        sibling = ClientWindow(self.root)
        original = getattr(self.app, "_scidev_windows", [])
        self.app._scidev_windows = [self.window, sibling]
        try:
            self.window._on_appearance_setting_changed("font_size", 12)
            self.assertEqual(sibling.appearance_preferences.font_size, 12)
            self.assertEqual(sibling.settings_page.controls["font_size"].value(), 12)
        finally:
            self.app._scidev_windows = original
            sibling.close()


if __name__ == "__main__":
    unittest.main()
