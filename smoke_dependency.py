"""Minimal dependency smoke test for SciDevHarness."""

import os
import tempfile
import threading
import time
from pathlib import Path
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"

import PySide6
from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QLabel, QMessageBox

from scidev_client import ClientWindow, CodeEditor, resolve_initial_workspace
from scidev_core import CodingToolbox, EventLedger


project_root = Path(__file__).resolve().parent
assert (project_root / "requirements.txt").is_file()
assert sys.prefix != sys.base_prefix, "not running inside the project virtual environment"
assert PySide6.__version__
assert ClientWindow.__name__ == "ClientWindow"
assert CodeEditor.__name__ == "CodeEditor"
assert ".research" in CodingToolbox.EXCLUDED_NAMES

app = QApplication([])
with tempfile.TemporaryDirectory(prefix="scidev-client-smoke-") as temp:
    smoke_root = Path(temp)
    settings_root = smoke_root / "settings"
    settings_root.mkdir()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(settings_root))
    ledger = EventLedger(smoke_root)
    assert ledger.events_path.parent == smoke_root / ".research"
    source_file = smoke_root / "smoke_edit.py"
    source_file.write_text("value = 1\n", encoding="utf-8")
    window = ClientWindow(smoke_root)
    try:
        window.show()
        app.processEvents()
        tree = window.git_tree
        assert tree.rootObject() is not None, "development tree QML did not load"
        assert tree.status() == QQuickWidget.Status.Ready, [error.toString() for error in tree.errors()]
        assert not tree.errors(), [error.toString() for error in tree.errors()]

        deadline = time.monotonic() + 3
        file_index = window.file_proxy.mapFromSource(window.file_model.index(str(source_file)))
        while (not file_index.isValid() or window.file_tree.visualRect(file_index).isEmpty()) and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
            file_index = window.file_proxy.mapFromSource(window.file_model.index(str(source_file)))
        assert file_index.isValid(), "workspace file was not available in the Explorer model"
        file_rect = window.file_tree.visualRect(file_index)
        assert not file_rect.isEmpty(), "workspace file has no visible Explorer row"
        QTest.mouseClick(window.file_tree.viewport(), Qt.MouseButton.LeftButton, pos=file_rect.center())
        app.processEvents()
        preview = window._preview_editor
        assert preview is not None and window._editor_paths.get(preview) == source_file.resolve()
        QTest.mouseDClick(window.file_tree.viewport(), Qt.MouseButton.LeftButton, pos=file_rect.center())
        app.processEvents()
        editor = window._active_editor()
        assert editor is preview and editor in window._pinned_editors, "Explorer double-click did not pin the file tab"
        QTest.keyClick(editor, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClicks(editor, "value = 2")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert editor.document().isModified()
        QTest.keyClick(editor, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        assert source_file.read_text(encoding="utf-8") == "value = 2\n", "Ctrl+S did not save the active file"
        assert not editor.document().isModified(), "saved editor remained marked dirty"

        assert resolve_initial_workspace(None, str(smoke_root), Path.cwd()) == smoke_root.resolve()
        invalid_workspace = smoke_root / "missing"
        try:
            resolve_initial_workspace(invalid_workspace, None, Path.cwd())
        except ValueError:
            pass
        else:
            raise AssertionError("invalid explicit workspaces must be rejected")

        second_root = smoke_root / "second-project"
        second_root.mkdir()
        second_window = window._open_workspace_root(second_root)
        assert second_window is not None and second_window.project_root == second_root.resolve()
        assert second_window in app._scidev_windows
        assert QSettings("SciDevHarness", "SciDevHarness").value("lastWorkspace") == str(second_root.resolve())
        assert window._open_workspace_root(second_root) is second_window, "opening an active workspace should focus its window"

        third_root = smoke_root / "third-project"
        third_root.mkdir()

        def choose_third_workspace() -> None:
            dialogs = [
                widget for widget in app.topLevelWidgets()
                if isinstance(widget, QFileDialog) and widget.isVisible()
            ]
            if not dialogs:
                QTimer.singleShot(25, choose_third_workspace)
                return
            dialogs[0].setDirectory(str(third_root))
            QTimer.singleShot(0, dialogs[0].accept)

        QTimer.singleShot(50, choose_third_workspace)
        window.open_workspace_button.click()
        assert any(
            item.project_root == third_root.resolve()
            for item in getattr(app, "_scidev_windows", [])
        ), "folder picker did not open the selected workspace"
        assert QSettings("SciDevHarness", "SciDevHarness").value("lastWorkspace") == str(third_root.resolve())

        window._handle_agent_event(
            "tool_started",
            {"name": "git_diff", "source": "harness_precommit", "arguments": {}},
        )
        started_bubble = window.chat_layout.itemAt(window.chat_layout.count() - 2).widget()
        started_role = started_bubble.findChild(QLabel, "BubbleRole")
        assert started_role is not None and started_role.text() == "Harness · 提交前 diff"
        window._handle_agent_event(
            "tool_result",
            {"name": "git_diff", "source": "harness_precommit", "result": "状态：clean\n\nDiff：\nhello.py"},
        )
        result_bubble = window.chat_layout.itemAt(window.chat_layout.count() - 2).widget()
        result_role = result_bubble.findChild(QLabel, "BubbleRole")
        result_text = result_bubble.findChild(QLabel, "ToolText")
        assert result_role is not None and result_role.text() == "Harness · 提交前 diff"
        assert result_text is not None and "hello.py" in result_text.text()

        warning_message = "Git 自动提交保护：model.gguf 超过 240,000 bytes，仍保留在工作区。"
        window._handle_agent_event(
            "git_auto_commit_skipped_paths",
            {"message": warning_message},
        )
        warning_bubble = window.chat_layout.itemAt(window.chat_layout.count() - 2).widget()
        warning_role = warning_bubble.findChild(QLabel, "BubbleRole")
        warning_text = warning_bubble.findChild(QLabel, "BubbleText")
        assert warning_role is not None and warning_role.text() == "Harness · Git 自动提交保护"
        assert warning_text is not None and "model.gguf" in warning_text.text()

        approval_result = []
        approval_thread = threading.Thread(
            target=lambda: approval_result.append(window._request_command_approval("python -V", 30)),
            daemon=True,
        )

        def reject_command() -> None:
            for widget in app.topLevelWidgets():
                if isinstance(widget, QMessageBox) and widget.isVisible():
                    widget.button(QMessageBox.StandardButton.No).click()
                    return
            QTimer.singleShot(25, reject_command)

        QTimer.singleShot(50, reject_command)
        approval_thread.start()
        deadline = time.monotonic() + 5
        while approval_thread.is_alive() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert not approval_thread.is_alive(), "command approval bridge did not return"
        assert approval_result == [False], "command rejection was not returned to the Agent thread"
    finally:
        for open_window in list(getattr(app, "_scidev_windows", [])):
            open_window.close()
        window.close()
        app.processEvents()
app.quit()

print("SciDevHarness dependency smoke test: OK")
print(f"python: {sys.executable}")
print(f"PySide6: {PySide6.__version__}")
print("client window/QML, Explorer double-click, editor Ctrl+S, workspace selection, and command-approval bridge: OK")
