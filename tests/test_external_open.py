"""External opening must use the right-clicked file and preserve editor drafts."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"

from PySide6.QtCore import QPoint, QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu
from scidev_client import ClientWindow


class ExternalFileOpenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scidev-external-open-")
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root / "ui.ini"), QSettings.Format.IniFormat)
        self.settings_patch = patch("scidev_client.QSettings", return_value=self.settings)
        self.settings_patch.start()
        self.window = ClientWindow(self.root)
        self.window.resize(1280, 720)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        for editor in self.window._editor_paths:
            editor.document().setModified(False)
        self.window.close()
        self.app.processEvents()
        self.settings_patch.stop()
        self.temp.cleanup()

    def point_for(self, path):
        QTest.qWait(100)  # Let QFileSystemModel publish and sort the new rows.
        source = self.window.file_model.index(str(path))
        for _ in range(100):
            self.app.processEvents()
            index = self.window.file_proxy.mapFromSource(source)
            if index.isValid():
                self.window.file_tree.scrollTo(index)
                rect = self.window.file_tree.visualRect(index)
                hit = self.window.file_tree.indexAt(rect.center())
                if (rect.isValid() and not rect.isEmpty() and hit.isValid()
                        and self.window._explorer_path(hit) == path.resolve()):
                    return rect.center()
            QTest.qWait(10)
            source = self.window.file_model.index(str(path))
        self.fail(f"File row did not load: {path}")

    @staticmethod
    def intercept_menu(label, captured):
        def create_menu(parent):
            menu = QMenu(parent)
            def choose(_point):
                actions = {action.text(): action for action in menu.actions() if not action.isSeparator()}
                captured.update(actions)
                if label is not None:
                    actions[label].trigger()
            # PySide retains native instance methods when the class method is
            # patched; replace this instance's exec instead of entering a loop.
            menu.exec = choose
            return menu
        return patch("scidev_client.QMenu", side_effect=create_menu)

    def test_html_context_menu_uses_clicked_file_encoded_url_and_preserves_draft(self):
        html = self.root / "俄罗斯 方块 #% INDEX.HTML"
        html.write_text("<!doctype html><title>disk version</title>", encoding="utf-8")
        other = self.root / "other.txt"
        other.write_text("other", encoding="utf-8")
        self.window._open_file(html)
        editor = self.window._active_editor()
        editor.insertPlainText("unsaved")
        before = editor.toPlainText()
        self.window._open_file(other)
        actions = {}
        with patch("scidev_client.webbrowser.open_new_tab", return_value=True) as browser:
            with self.intercept_menu("在浏览器中打开", actions):
                self.window._show_explorer_menu(self.point_for(html))
            browser.assert_called_once_with(html.resolve().as_uri())
            uri = browser.call_args.args[0]
            self.assertEqual(urlsplit(uri).fragment, "")
            self.assertIn(html.name, unquote(urlsplit(uri).path))
        self.assertIn("在编辑器中打开", actions)
        self.assertIn("使用默认程序打开", actions)
        self.assertEqual(editor.toPlainText(), before)
        self.assertTrue(editor.document().isModified())
        self.assertEqual(html.read_text(encoding="utf-8"), "<!doctype html><title>disk version</title>")
        self.assertIn("磁盘版本", self.window.statusBar().currentMessage())

    def test_tab_context_menu_targets_clicked_tab_and_text_uses_default_program(self):
        html = self.root / "index.htm"
        html.write_text("<title>tab target</title>", encoding="utf-8")
        text = self.root / "说明.txt"
        text.write_text("read me", encoding="utf-8")
        self.window._open_file(html)
        html_editor = self.window._active_editor()
        self.window._open_file(text)
        group = self.window.editor_tabs
        self.app.processEvents()
        point = group.tabBar().tabRect(group.indexOf(html_editor)).center()
        with patch("scidev_client.webbrowser.open_new_tab", return_value=True) as browser:
            with self.intercept_menu("在浏览器中打开", {}):
                self.window._show_editor_tab_menu(point, group)
            browser.assert_called_once_with(html.resolve().as_uri())
        actions = {}
        with patch("scidev_client.QDesktopServices.openUrl", return_value=True) as desktop:
            with self.intercept_menu("使用默认程序打开", actions):
                self.window._show_explorer_menu(self.point_for(text))
            self.assertEqual(Path(desktop.call_args.args[0].toLocalFile()), text.resolve())
        self.assertNotIn("在浏览器中打开", actions)

    def test_blank_area_and_folders_have_no_external_file_action(self):
        html = self.root / "index.html"
        html.write_text("<title>selected</title>", encoding="utf-8")
        folder = self.root / "folder"
        folder.mkdir()
        self.window.file_tree.setCurrentIndex(self.window.file_proxy.mapFromSource(
            self.window.file_model.index(str(html))))
        for point in (QPoint(5, self.window.file_tree.viewport().height() - 5), self.point_for(folder)):
            actions = {}
            with self.intercept_menu(None, actions):
                self.window._show_explorer_menu(point)
            self.assertNotIn("在浏览器中打开", actions)
            self.assertNotIn("使用默认程序打开", actions)

    def test_missing_outside_and_invalid_files_do_not_dispatch_and_failure_is_visible(self):
        text = self.root / "note.txt"
        text.write_text("keep", encoding="utf-8")
        with patch("scidev_client.webbrowser.open_new_tab") as browser:
            self.assertFalse(self.window._open_external_file(self.root / "missing.html", browser=True))
            self.assertFalse(self.window._open_external_file(self.root.parent, browser=True))
            self.assertFalse(self.window._open_external_file(text, browser=True))
            browser.assert_not_called()
        html = self.root / "index.html"
        html.write_text("<title>no browser</title>", encoding="utf-8")
        with patch("scidev_client.webbrowser.open_new_tab", return_value=False):
            self.assertFalse(self.window._open_external_file(html, browser=True))
        self.assertIn("未找到可用的浏览器", self.window.statusBar().currentMessage())
        with patch("scidev_client.QDesktopServices.openUrl", return_value=False):
            self.assertFalse(self.window._open_external_file(text))
        self.assertIn("未找到可用的默认程序", self.window.statusBar().currentMessage())


if __name__ == "__main__":
    unittest.main()
