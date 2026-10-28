"""Regression checks for the screenshot-driven desktop UI cleanup."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
if os.name == "nt":
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if fonts.is_dir():
        os.environ["QT_QPA_FONTDIR"] = str(fonts)

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QFontInfo, QTextDocument, QTextLayout
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from scidev_client import ClientWindow, ElidedLabel
from scidev_highlighting import LanguageHighlighter
from scidev_planning import TaskPlan
from scidev_ui import (
    ACTIVITY_BUTTON_SIZE, ACTIVITY_ICON_SIZE, ACTIVITY_RAIL_WIDTH, UI_PROFILES,
    create_ui_font, ui_font_family,
)


class HighlightingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def highlight(self, text, suffix):
        document = QTextDocument(text)
        highlighter = LanguageHighlighter(document, "paper", Path("source" + suffix))
        highlighter.rehighlight()
        return document, highlighter

    @staticmethod
    def color_at(document, block_number, offset):
        block = document.findBlockByNumber(block_number)
        for item in block.layout().formats():
            if item.start <= offset < item.start + item.length:
                return item.format.foreground().color().name()
        return None

    def test_css_id_hex_and_javascript_keywords_are_not_python_comments(self):
        document, highlighter = self.highlight("#board { color: #ffffff; } /* note */", ".css")
        self.assertNotEqual(self.color_at(document, 0, 0), UI_PROFILES["paper"].syntax_colors[3])
        self.assertEqual(self.color_at(document, 0, 27), UI_PROFILES["paper"].syntax_colors[3])
        document, highlighter = self.highlight("const value = true; // note", ".js")
        self.assertEqual(self.color_at(document, 0, 0), UI_PROFILES["paper"].syntax_colors[0])
        self.assertEqual(self.color_at(document, 0, 20), UI_PROFILES["paper"].syntax_colors[3])

    def test_html_embedded_languages_multiline_comments_and_return_to_markup(self):
        document, highlighter = self.highlight(
            '<style>\n#board { color: #fff; }\n/* comment\ncontinued */\n</style>\n'
            '<script>\nconst value = 3;\n</script>\n<div id="board"></div>', ".html"
        )
        self.assertEqual(self.color_at(document, 1, 0), UI_PROFILES["paper"].syntax_colors[1])
        self.assertEqual(self.color_at(document, 3, 0), UI_PROFILES["paper"].syntax_colors[3])
        self.assertEqual(self.color_at(document, 6, 0), UI_PROFILES["paper"].syntax_colors[0])
        self.assertEqual(document.findBlockByNumber(8).userState(), 0)

    def test_plain_files_stay_plain_and_reused_preview_changes_language(self):
        document, highlighter = self.highlight("# not a comment", ".txt")
        self.assertIsNone(self.color_at(document, 0, 0))
        highlighter.set_path(Path("script.py"))
        self.assertEqual(self.color_at(document, 0, 0), UI_PROFILES["paper"].syntax_colors[3])

    def test_emoji_does_not_shift_formats_and_comments_do_not_override_strings(self):
        text = 'const icon = "🧪"; const value = "#literal";'
        document, highlighter = self.highlight(text, ".js")
        index = len(text[:text.index("const", 1)].encode("utf-16-le")) // 2
        self.assertEqual(self.color_at(document, 0, index), UI_PROFILES["paper"].syntax_colors[0])
        document, highlighter = self.highlight('value = "#literal" # real', ".py")
        self.assertEqual(self.color_at(document, 0, 9), UI_PROFILES["paper"].syntax_colors[4])


class DesktopPolishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setFont(create_ui_font())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scidev-ui-polish-test-")
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root / "ui.ini"), QSettings.Format.IniFormat)
        self.settings_patch = patch("scidev_client.QSettings", return_value=self.settings)
        self.settings_patch.start()
        self.window = ClientWindow(self.root)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.settings_patch.stop()
        self.temp.cleanup()

    def settle(self):
        self.app.processEvents()
        QTest.qWait(40)
        self.app.processEvents()

    def test_settings_scroll_without_horizontal_clipping_in_all_themes(self):
        self.window.toggle_summary_settings()
        for theme in UI_PROFILES:
            self.window.set_ui_profile(theme, persist=False)
            for size in ((1500, 920), (1120, 700), (780, 480)):
                self.window.resize(*size)
                self.settle()
                panel, scroll = self.window.summary_settings_panel, self.window.summary_settings_scroll
                # The Agent settings panel lives in the settings workspace now;
                # fall back to the chat-side scroll only if it is still hosted there.
                settings = self.window.settings_page
                hosted_by_settings = panel.parentWidget() is settings.scroll.widget()
                container = settings.scroll if hosted_by_settings else scroll
                self.assertLessEqual(panel.width(), container.viewport().width())
                fields = [self.window.summary_tokens_spin, self.window.summary_context_spin,
                          self.window.summary_model_edit, self.window.acceptance_mode_combo]
                for field in fields:
                    self.assertGreaterEqual(field.height(), 32)
                    self.assertTrue(panel.rect().contains(field.geometry()))
                if panel.height() > container.viewport().height():
                    self.assertGreater(container.verticalScrollBar().maximum(), 0)
                if theme == "paper":
                    color = self.window.summary_settings_title.palette().color(self.window.summary_settings_title.foregroundRole())
                    self.assertEqual(color.name(), "#263346")
        self.window.toggle_summary_settings()
        self.assertTrue(self.window.chat_input.isVisible())

    def test_minimum_composer_controls_fit_and_usage_has_its_own_row(self):
        self.window.resize(780, 480)
        self.settle()
        # The usage chip only appears once a request was actually made, so give
        # it a counted attempt before measuring its row.
        self.window._display_model_usage({"usage": {"request_attempts": 1}, "record": None})
        self.settle()
        for chip in (self.window.chat_mode_chip, self.window.retry_chip, self.window.summary_chip):
            self.assertLessEqual(chip.fontMetrics().horizontalAdvance(chip.text()), chip.contentsRect().width())
        self.assertTrue(self.window.usage_chip.isVisible())
        self.assertGreater(self.window.usage_chip.geometry().top(), self.window.summary_chip.geometry().bottom())

    def test_activity_hit_areas_and_rasters_scale_without_shrinking_when_collapsed(self):
        for scale, resolution in ((1.0, (1280, 720)), (1.3, (3840, 2160))):
            self.window._apply_screen_resolution(*resolution)
            for size in ((1500, 920), (780, 480)):
                self.window.resize(*size)
                self.settle()
                self.assertEqual(self.window.activity_rail.width(), round(ACTIVITY_RAIL_WIDTH * scale))
                for button in self.window.activity_buttons:
                    self.assertEqual(button.width(), round(ACTIVITY_BUTTON_SIZE * scale))
                    self.assertEqual(button.height(), round(ACTIVITY_BUTTON_SIZE * scale))
                    self.assertEqual(button.iconSize().width(), round(ACTIVITY_ICON_SIZE * scale))
                    self.assertTrue(self.window.activity_rail.rect().contains(button.geometry()))
                    raster = button.icon().pixmap(button.iconSize())
                    self.assertGreaterEqual(raster.width() / raster.devicePixelRatio(), button.iconSize().width())
                if self.window.explorer.isHidden():
                    self.assertEqual(self.window.workbench.sizes()[0], self.window.activity_rail.width())

    def test_ui_font_prefers_installed_cjk_sans_without_light_faces(self):
        self.assertEqual(ui_font_family(["", "Microsoft YaHei UI"]), "Microsoft YaHei UI")
        self.assertEqual(ui_font_family(["Segoe UI", "SimSun", "Noto Sans SC"]), "Noto Sans SC")
        self.assertEqual(ui_font_family(["Noto Sans SC", "Microsoft YaHei UI"]), "Microsoft YaHei UI")
        self.assertEqual(ui_font_family(["Noto Sans SC Thin", "Segoe UI"]), "Segoe UI")
        self.assertEqual(ui_font_family(["microsoft yahei ui"]), "microsoft yahei ui")

    def test_long_label_reelides_on_font_change_without_resize(self):
        full = "这是较长的项目名称 Long workspace name"
        label = ElidedLabel(full)
        try:
            label.resize(200, 60)
            label.setFont(create_ui_font(11))
            label.show()
            self.settle()
            original = label.text()
            self.assertGreaterEqual(label.sizeHint().width(), label.fontMetrics().horizontalAdvance(full))
            label.setFont(create_ui_font(22))
            self.settle()
            self.assertNotEqual(label.text(), original)
            self.assertLessEqual(label.fontMetrics().horizontalAdvance(label.text()), label.contentsRect().width())
            self.assertEqual(label.full_text, full)
            self.assertEqual(label.accessibleName(), full)
        finally:
            label.close()

    def test_navigation_uses_real_sans_glyphs_and_semibold_controls_in_all_themes(self):
        controls = [self.window.title_new_button, self.window.title_git_button,
                    self.window.title_refresh_button, self.window.agent_title,
                    self.window.task_plan_panel.title_label]
        controls += self.window.welcome_content.findChildren(QPushButton)
        for theme in UI_PROFILES:
            self.window.set_ui_profile(theme, persist=False)
            self.settle()
            for control in controls:
                self.assertEqual(QFontInfo(control.font()).family(), ui_font_family())
                self.assertGreaterEqual(control.font().weight(), 600)
                layout = QTextLayout("打开文件 新会话 任务计划", control.font())
                layout.beginLayout()
                layout.createLine()
                layout.endLayout()
                self.assertTrue(layout.glyphRuns())
                for run in layout.glyphRuns():
                    self.assertEqual(run.rawFont().familyName(), ui_font_family())
                    self.assertNotIn("Light", run.rawFont().styleName())
                    self.assertNotIn("Thin", run.rawFont().styleName())
            self.assertTrue(QFontInfo(self.window.welcome_editor.font()).fixedPitch())
            self.assertEqual(self.window.welcome_editor.font().weight(), 400)
            self.assertEqual(self.window.git_tree.rootObject().property("uiFontFamily"), ui_font_family())

    def test_git_focus_releases_canvas_and_restores_workbench(self):
        before = self.window.workbench.sizes()
        self.window.show_git()
        self.settle()
        self.assertTrue(self.window.explorer.isHidden())
        self.assertTrue(self.window.chat_panel.isHidden())
        self.assertGreater(self.window.git_tree.width(), 800)
        self.window._toggle_git_details()
        self.settle()
        self.assertTrue(self.window.git_details_scroll.isHidden())
        self.assertGreater(self.window.git_tree.width(), 1300)
        self.window._toggle_git_chat()
        self.settle()
        self.assertTrue(self.window.chat_panel.isVisible())
        self.window.show_workspace()
        self.settle()
        self.assertFalse(self.window.explorer.isHidden())
        self.assertEqual(self.window.workbench.sizes(), before)

    def test_splitter_ratios_persist_and_ignore_tree_focus(self):
        self.window.workbench.setSizes([320, 760, 410])
        self.window._on_workbench_splitter_moved(320, 1)
        saved = self.window._workbench_user_ratios
        self.assertEqual(tuple(map(float, self.settings.value(f"workbenchRatios/{self.window.ui_profile}"))), saved)
        self.window.show_git()
        self.window._on_workbench_splitter_moved(58, 1)
        self.assertEqual(self.window._workbench_user_ratios, saved)
        self.assertEqual(self.window._saved_workbench_ratios(self.window.ui_profile), saved)

    def test_welcome_is_clean_and_disappears_when_a_file_opens(self):
        self.assertEqual(self.window.welcome_editor.toPlainText(), "")
        self.assertTrue(self.window.welcome_editor.line_number_area.isHidden())
        self.assertTrue(self.window.welcome_content.isVisible())
        self.assertTrue(self.window.workspace_toolbar.isHidden())
        self.window.resize(780, 480)
        self.settle()
        for label in self.window._welcome_text_labels:
            self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))
        source = self.root / "demo.js"
        source.write_text("const value = 1;", encoding="utf-8")
        self.window._open_file(source)
        self.settle()
        welcome_index = self.window.editor_tabs.indexOf(self.window.welcome_editor)
        self.assertFalse(self.window.editor_tabs.tabBar().isTabVisible(welcome_index))
        self.assertEqual(self.window._editor_highlighters[self.window.code_editor].language, "javascript")
        self.window._close_editor_tab(self.window.editor_tabs.currentIndex())
        self.settle()
        self.assertTrue(self.window.welcome_content.isVisible())

    def test_plan_showing_three_steps_wide_and_current_step_narrow(self):
        plan = TaskPlan.update(None, [
            {"step": "分析实现", "status": "completed"},
            {"step": "修改代码", "status": "in_progress"},
            {"step": "核验结果", "status": "pending"},
        ])
        panel = self.window.task_plan_panel
        panel.collapsed = False
        panel.set_plan(plan.to_record(), run_state="running")
        self.settle()
        self.assertGreaterEqual(panel.scroll.height(), 120)
        self.assertEqual(panel.scroll.verticalScrollBar().maximum(), 0)
        self.window.resize(780, 480)
        self.settle()
        current = panel.body_layout.itemAt(1).widget()
        rect = current.geometry().translated(panel.body.pos())
        self.assertTrue(panel.scroll.viewport().rect().contains(rect))

    def test_model_identity_and_missing_configuration_color(self):
        with patch.dict(os.environ, {"SCIDEV_API_KEY": "", "OPENAI_API_KEY": ""}):
            self.window.refresh_all()
            self.assertEqual(self.window.connection_label.objectName(), "StateWorking")
            self.assertEqual(self.window.agent_title.full_text, "Coding Agent")
            QTest.mouseClick(self.window.connection_label, Qt.MouseButton.LeftButton)
            self.assertTrue(self.window.summary_settings_panel.isVisible())
        with patch.dict(os.environ, {"SCIDEV_API_KEY": "test-only", "SCIDEV_MODEL": "Qwen2.5-Coder-7B"}):
            self.window.refresh_all()
            self.assertEqual(self.window.agent_title.full_text, "Qwen2.5-Coder-7B")

    def test_branch_can_move_right_even_when_its_default_card_is_at_edge(self):
        tree = self.window.git_tree
        tree.resize(420, 600)
        tree.set_nodes([{"id": "root", "lane": "main", "status": "main"},
                        {"id": "attempt", "lane": "attempt", "parent_id": "root", "status": "failed"}])
        first = tree._positions()[0]["attempt"][2]
        tree._node_offsets["attempt"] = QPoint(35, 20)
        second = tree._positions()[0]["attempt"][2]
        self.assertEqual(second.x() - first.x(), 35)
        self.assertEqual(second.y() - first.y(), 20)


if __name__ == "__main__":
    unittest.main()
