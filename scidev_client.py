"""SciDevHarness desktop coding client.

The shell intentionally follows the information hierarchy of a modern code
editor: activity bar, project explorer, editor workbench, and an Agent panel.
The coding Agent and its retry/audit services remain UI-independent in
``scidev_core.py``.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QDir,
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QPointF,
    QProcess,
    QProcessEnvironment,
    QRect,
    QRegularExpression,
    QSize,
    QSettings,
    QStringListModel,
    Qt,
    QSortFilterProxyModel,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QFont, QKeyEvent, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient, QShortcut, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QCompleter,
    QDialog,
    QFileDialog,
    QFileSystemModel,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSizePolicy,
    QSizeGrip,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabBar,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtQuickWidgets import QQuickWidget

from scidev_core import (
    CodingAgent,
    CodingToolbox,
    EventLedger,
    GitManager,
    RetryQueue,
    SummarySettings,
    new_id,
)
from scidev_icons import activity_icon
from scidev_ui import DEFAULT_UI_PROFILE, UI_PROFILES, stylesheet_for_profile, ui_profile_for_key


THEME = """
* {
    font-family: "Segoe UI", "Noto Sans SC", "DengXian", "Microsoft YaHei", sans-serif;
    font-size: 11pt;
}
QMainWindow, QWidget#Root {
    background: #10151b;
    color: #dce4ed;
}
QFrame#TitleBar {
    background: #11171e;
    border-bottom: 1px solid #26313d;
}
QFrame#ActivityRail {
    background: #11171e;
    border-right: 1px solid #26313d;
}
QFrame#ExplorerPane {
    background: #11171e;
    border-right: 1px solid #26313d;
}
QFrame#WorkspacePane {
    background: #0d1218;
}
QFrame#ChatPane {
    background: #11171e;
    border-left: 1px solid #26313d;
}
QFrame#WorkspaceToolbar, QFrame#EditorToolbar {
    background: #0d1218;
    border-bottom: 1px solid #222d38;
}
QFrame#ChatHeader {
    background: #11171e;
    border-bottom: 1px solid #26313d;
}
QFrame#Composer {
    background: #11171e;
    border-top: 1px solid #26313d;
}
QFrame#SummarySettings {
    background: #17212b;
    border-bottom: 1px solid #2b3947;
}
QLabel#SummarySettingsTitle { color: #e6e6e6; font-weight: 600; }
QLabel#SummaryState { color: #73c991; }
QCheckBox { color: #cccccc; spacing: 6px; }
QCheckBox::indicator { width: 14px; height: 14px; }
QCheckBox::indicator:unchecked { background: #2b2b2b; border: 1px solid #5a5a5a; border-radius: 3px; }
QCheckBox::indicator:checked { background: #2d7d72; border: 1px solid #4ec9b0; border-radius: 3px; }
QFrame#SummarySettings QLabel { color: #c8c8c8; }
QFrame#SummarySettings QLabel#SummarySettingsTitle { color: #e6e6e6; }
QFrame#SummarySettings QLabel#SummaryState { color: #73c991; }
QFrame#SummarySettings QLabel#Hint { color: #9d9d9d; }
QSpinBox {
    min-height: 26px;
    padding: 2px 5px;
    background: #18212b;
    color: #dce4ed;
    border: 1px solid #2d3946;
    border-radius: 6px;
}
QSpinBox:focus { border: 1px solid #5a9cf5; }
QFrame#GitPage {
    background: #0d1218;
}
QFrame#DevelopmentTree {
    background: #101820;
    border: none;
}
QScrollArea#TreeScroll {
    background: #101820;
    border: none;
}
QScrollArea#GitDetailsScroll {
    background: #141b23;
    border: none;
}
QScrollArea#GitDetailsScroll > QWidget {
    background: #141b23;
}
QFrame#GitPageHeader {
    background: #11171e;
    border-bottom: 1px solid #26313d;
}
QFrame#GitDetails {
    background: #141b23;
    border-left: 1px solid #273441;
}
QFrame#MetricCard {
    background: #171f28;
    border: 1px solid #293643;
    border-radius: 8px;
}
QFrame#GitEntry {
    background: #18212b;
    border: 1px solid #2d3a47;
    border-left: 3px solid #53cbb7;
    border-radius: 7px;
}
QFrame#GitEntry:hover {
    background: #1d2934;
    border-color: #435565;
}
QLabel#AppTitle {
    color: #f2f2f2;
    font-size: 11pt;
    font-weight: 600;
}
QLabel#WindowTitle {
    color: #e6e6e6;
    font-size: 10pt;
    font-weight: 600;
}
QLabel#Subtle, QLabel#Hint, QLabel#StatusText {
    color: #a2afbd;
}
QLabel#Overline {
    color: #a9b6c4;
    font-size: 9pt;
    font-weight: 600;
}
QLabel#SectionTitle {
    color: #edf3f8;
    font-size: 12pt;
    font-weight: 600;
}
QLabel#Logo {
    background: #3278df;
    color: #ffffff;
    border-radius: 5px;
    font-size: 12pt;
    font-weight: 700;
}
QLabel#AgentLogo {
    background: #247f93;
    color: #ffffff;
    border-radius: 5px;
    font-size: 12pt;
    font-weight: 700;
}
QLabel#StateReady { color: #73c991; font-weight: 600; }
QLabel#StateWorking { color: #e5c07b; font-weight: 600; }
QLabel#StateError { color: #f48771; font-weight: 600; }
QLineEdit, QTextEdit, QPlainTextEdit {
    background: #18212b;
    color: #dce4ed;
    border: 1px solid #2d3946;
    border-radius: 7px;
    padding: 8px 10px;
    selection-background-color: #264f78;
    selection-color: #ffffff;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border: 1px solid #5a9cf5;
}
QLineEdit#CommandSearch {
    background: #19232e;
    border: 1px solid #2d3a47;
    color: #dce4ed;
    padding: 7px 10px;
}
QTextEdit#ChatInput {
    background: #18212b;
    border: 1px solid #344353;
    border-radius: 9px;
    padding: 8px 9px;
}
QPlainTextEdit#CodeEditor {
    background: #0d1218;
    color: #dce4ed;
    border: none;
    border-radius: 0px;
    padding: 14px 18px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12pt;
}
QFrame#FindBar {
    background: #17212b;
    border-bottom: 1px solid #2b3947;
}
QFrame#FindBar QLineEdit {
    min-height: 25px;
    padding: 4px 7px;
    border-radius: 3px;
}
QLabel#FindStatus { color: #9d9d9d; min-width: 72px; }
QTabWidget#BottomTabs::pane {
    border-top: 1px solid #273441;
    background: #11171e;
}
QTabWidget#BottomTabs QTabBar::tab {
    min-width: 0px;
    padding: 7px 14px;
    background: #11171e;
}
QPlainTextEdit#TerminalOutput, QPlainTextEdit#ProblemsOutput {
    background: #0d1218;
    color: #dce4ed;
    border: none;
    border-radius: 0px;
    padding: 9px 12px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 9pt;
}
QLineEdit#TerminalInput {
    background: #17212b;
    border: none;
    border-top: 1px solid #2b3947;
    border-radius: 0px;
    padding: 7px 10px;
    font-family: "Consolas", "Courier New", monospace;
}
QMenu {
    background: #17212b;
    color: #dce4ed;
    border: 1px solid #334252;
    padding: 4px;
}
QMenu::item { padding: 6px 24px 6px 10px; border-radius: 3px; }
QMenu::item:selected { background: #213c5b; color: #ffffff; }
QMenu::separator { height: 1px; background: #2b3947; margin: 4px 8px; }
QPushButton, QToolButton {
    background: #1d2732;
    color: #dce4ed;
    border: 1px solid #2e3b49;
    border-radius: 6px;
    padding: 6px 11px;
}
QPushButton:hover, QToolButton:hover {
    background: #263442;
    border-color: #405365;
}
QPushButton:pressed, QToolButton:pressed {
    background: #202b36;
}
QPushButton:disabled, QToolButton:disabled {
    background: #161c22;
    color: #65717d;
    border-color: #222c35;
}
QPushButton#Primary {
    background: #3278df;
    color: #ffffff;
    border: 1px solid #4b91f2;
    font-weight: 600;
}
QPushButton#Primary:hover { background: #4389ed; }
QPushButton#Primary:disabled, QPushButton#GitPrimary:disabled {
    background: #161c22;
    color: #65717d;
    border-color: #222c35;
}
QPushButton#GitPrimary {
    background: #168f80;
    color: #ffffff;
    border: 1px solid #40bea9;
    font-weight: 600;
}
QPushButton#GitPrimary:hover { background: #20a291; }
QPushButton#GitEntryButton {
    background: transparent;
    color: #dce9f4;
    border: none;
    border-radius: 6px;
    text-align: left;
    padding: 8px 10px;
}
QPushButton#GitEntryButton:hover { background: #222d38; }
QPushButton#Quiet, QToolButton#Quiet {
    background: transparent;
    border: 1px solid transparent;
}
QPushButton#Quiet:hover, QToolButton#Quiet:hover { background: #202a35; }
QToolButton#ActivityButton {
    background: transparent;
    color: #8492a1;
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 0px;
}
QToolButton#ActivityButton:hover {
    background: #1d2935;
    color: #ffffff;
}
QToolButton#ActivityButton:checked {
    color: #79b4ff;
    background: #1a2734;
    border-left: 2px solid #5a9cf5;
}
QToolButton#IconButton {
    background: transparent;
    color: #9eacba;
    border: 1px solid transparent;
    padding: 2px 5px;
}
QToolButton#IconButton:hover { background: #202a35; color: #ffffff; }
QToolButton#ThemeSelector {
    background: #19232e;
    color: #b8d4f2;
    border: 1px solid #2d3c4b;
    font-weight: 600;
    padding: 4px 8px;
}
QToolButton#ThemeSelector:hover { background: #243448; border-color: #405771; }
QToolButton#TabCloseButton {
    background: transparent;
    color: #8492a1;
    border: none;
    border-radius: 3px;
    padding: 0px;
    font-size: 12pt;
}
QToolButton#TabCloseButton:hover { background: #293744; color: #ffffff; }
QToolButton#TabCloseButton:pressed { background: #354554; color: #ffffff; }
QToolButton#WindowButton {
    background: transparent;
    color: #a6b2be;
    border: none;
    border-radius: 0px;
    font-size: 11pt;
    padding: 0px;
}
QToolButton#WindowButton:hover { background: #26313c; color: #ffffff; }
QToolButton#WindowClose:hover { background: #c42b1c; color: #ffffff; }
QTreeView {
    background: #11171e;
    alternate-background-color: #151c24;
    color: #d2dbe5;
    border: none;
    outline: none;
    padding: 5px 0px;
    font-size: 10pt;
}
QTreeView::item { padding: 5px 6px; border-radius: 3px; }
QTreeView::item:hover { background: #1d2935; }
QTreeView::item:selected { background: #20364e; color: #ffffff; }
QTabWidget::pane {
    border: none;
    background: #0d1218;
}
QTabBar {
    background: #11171e;
}
QTabBar::tab {
    background: #11171e;
    color: #8f9dab;
    border: none;
    border-right: 1px solid #26313d;
    padding: 10px 15px;
    min-width: 88px;
}
QTabBar::tab:selected {
    background: #0d1218;
    color: #eef4fa;
    border-top: 2px solid #5a9cf5;
}
QSplitter::handle { background: #202a35; }
QSplitter::handle:hover { background: #41586c; }
QSplitter#Workbench::handle { background: #202a35; }
QSplitter#Workbench::handle:hover { background: #5a9cf5; }
QScrollArea, QScrollArea#ChatScroll, QScrollArea#ChatScroll > QWidget, QWidget#ChatContent {
    background: #11171e;
    border: none;
}
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 2px;
}
QScrollBar::handle:vertical { background: #344251; min-height: 26px; border-radius: 4px; }
QScrollBar::handle:vertical:hover { background: #506477; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; border: none; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 2px; }
QScrollBar::handle:horizontal { background: #344251; min-width: 26px; border-radius: 4px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; border: none; }
QStatusBar {
    background: #11171e;
    color: #a2afbd;
    border: none;
    border-top: 1px solid #26313d;
    min-height: 26px;
}
QStatusBar::item { border: none; }
QStatusBar QLabel { color: #a2afbd; padding: 0 5px; }
QStatusBar QLabel#StatusText { color: #bdc8d3; }
QFrame#UserBubble { background: #172d43; border: 1px solid #294866; border-radius: 9px; }
QFrame#AgentBubble { background: #18212b; border: 1px solid #2a3744; border-radius: 9px; }
QFrame#ToolBubble { background: #172720; border: 1px solid #2e493a; border-radius: 8px; }
QFrame#MetaBubble { background: transparent; border: none; }
QFrame#SummaryBubble { background: #172934; border: 1px solid #2a4c5b; border-radius: 9px; }
QFrame#NotificationToast { background: #202832; border: 1px solid #493b40; border-left: 3px solid #df7478; border-radius: 10px; }
QLabel#BubbleRole { color: #91a0af; font-size: 9pt; font-weight: 600; }
QLabel#BubbleText { color: #e2e9f0; font-size: 11pt; }
QFrame#SummaryBubble QLabel#BubbleRole { color: #77cbed; }
QFrame#SummaryBubble QLabel#BubbleText { color: #e5f7ff; }
QLabel#NotificationBadge { background: #492b30; color: #ffb4b7; border-radius: 12px; font-size: 12pt; font-weight: 700; }
QLabel#NotificationTitle { color: #f3f5f7; font-size: 11pt; font-weight: 650; }
QLabel#NotificationBody { color: #c2cbd5; font-size: 10pt; }
QToolButton#NotificationClose { background: transparent; color: #aab6c2; border: none; font-size: 16pt; }
QToolButton#NotificationClose:hover { background: #303943; color: #ffffff; }
QLabel#ToolText { color: #b9d4c3; font-family: "Consolas", "Courier New", monospace; font-size: 9pt; }
QLabel#Chip { color: #a9d4ff; background: #19232e; border: 1px solid #2d3c4b; border-radius: 6px; padding: 4px 8px; }
QLabel#MetricValue { color: #eef4fa; font-size: 16pt; font-weight: 600; }
QLabel#GitDetailTitle { color: #eef4fa; font-size: 14pt; font-weight: 600; }
QLabel#GitDetailStatus { color: #5bd5bd; font-size: 11pt; font-weight: 600; }
QLabel#TreeLegend { color: #a2afbd; }
QDialog { background: #141b23; color: #dce4ed; }
QTreeWidget { background: #10161d; color: #dce4ed; border: 1px solid #2d3946; border-radius: 6px; }
QHeaderView::section { background: #18212b; color: #dce4ed; border: none; border-bottom: 1px solid #2d3946; padding: 8px; }
"""


def resolve_initial_workspace(
    explicit_workspace: str | Path | None,
    last_workspace: str | Path | None,
    fallback: str | Path,
) -> Path:
    """Resolve a requested, remembered, or development workspace in priority order."""
    if explicit_workspace is not None:
        requested = Path(explicit_workspace).expanduser().resolve()
        if not requested.is_dir():
            raise ValueError(f"工作区目录不存在：{requested}")
        return requested

    if last_workspace:
        remembered = Path(last_workspace).expanduser()
        if remembered.is_dir():
            return remembered.resolve()

    default = Path(fallback).expanduser().resolve()
    if not default.is_dir():
        raise ValueError(f"默认工作区目录不存在：{default}")
    return default


def application_base_dir() -> Path:
    """Return the source or deployed app directory for bundled UI resources."""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parent


MAX_TREE_LAYOUT_BYTES = 1_000_000
MAX_TREE_LAYOUT_OFFSET = 50_000.0


def _finite_tree_coordinate(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        coordinate = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(coordinate) or abs(coordinate) > MAX_TREE_LAYOUT_OFFSET:
        return None
    return coordinate


def _load_tree_layout_offsets(layout_path: Path | None) -> dict[str, QPointF]:
    if layout_path is None:
        return {}
    try:
        if layout_path.stat().st_size > MAX_TREE_LAYOUT_BYTES:
            return {}
        raw = json.loads(layout_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return {}
    if not isinstance(raw, dict):
        return {}

    offsets: dict[str, QPointF] = {}
    for node_id, value in raw.items():
        if not isinstance(node_id, str) or not node_id or len(node_id) > 256 or not isinstance(value, dict):
            continue
        x = _finite_tree_coordinate(value.get("x", 0))
        y = _finite_tree_coordinate(value.get("y", 0))
        if x is not None and y is not None:
            offsets[node_id] = QPointF(x, y)
    return offsets


def _choose_workspace_directory(initial_directory: Path, parent: QWidget | None = None) -> Path | None:
    """Show a Qt directory picker without the platform-native title bar."""
    dialog = QFileDialog(parent)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
    dialog.setWindowTitle("打开文件夹")
    dialog.setDirectory(str(initial_directory))
    profile_key = getattr(parent, "ui_profile", DEFAULT_UI_PROFILE)
    dialog.setStyleSheet(stylesheet_for_profile(THEME, profile_key))
    dialog.resize(860, 600)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    selections = dialog.selectedFiles()
    if not selections:
        return None
    selected = Path(selections[0]).expanduser().resolve()
    return selected if selected.is_dir() else None


class AppSignals(QObject):
    agent_event = Signal(str, object)
    worker_event = Signal(str, object)
    command_approval_requested = Signal(object)


class ElidedLabel(QLabel):
    """Keep long workspace names readable without letting them push out controls."""

    def __init__(self, full_text: str, parent: QWidget | None = None):
        super().__init__(full_text, parent)
        self.full_text = str(full_text)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setAccessibleName(self.full_text)

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        super().resizeEvent(event)
        available_width = max(0, self.contentsRect().width())
        if available_width == 0:
            return
        visible_text = self.fontMetrics().elidedText(
            self.full_text,
            Qt.TextElideMode.ElideMiddle,
            available_width,
        )
        if visible_text != self.text():
            QLabel.setText(self, visible_text)


class PromptEdit(QTextEdit):
    send_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.send_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):  # noqa: ANN201 - Qt API has no useful narrow type here.
        return self.editor.line_number_area_width(), 0

    def paintEvent(self, event):  # noqa: ANN001 - Qt event signature.
        self.editor.line_number_area_paint_event(event)


class CodeEditor(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setObjectName("CodeEditor")
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        editor_font = QFont("Consolas", 12)
        editor_font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(editor_font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.completer = QCompleter(self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completion_model = QStringListModel(self)
        self.completer.setModel(self.completion_model)
        self.completer.setWidget(self)
        self.completer.activated.connect(self._insert_completion)
        self.line_number_area = LineNumberArea(self)
        self.ui_profile = DEFAULT_UI_PROFILE
        self._line_number_background = QColor("#1e1e1e")
        self._line_number_foreground = QColor("#858585")
        self._current_line_background = QColor("#252526")
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(self.highlight_current_line)
        self.update_line_number_area_width(0)
        self.highlight_current_line()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.show_completions()
            event.accept()
            return
        if self.completer.popup().isVisible():
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                completion = self.completer.currentCompletion()
                if completion:
                    self._insert_completion(completion)
                    event.accept()
                    return
            if event.key() == Qt.Key.Key_Escape:
                self.completer.popup().hide()
                event.accept()
                return
        super().keyPressEvent(event)
        if not self.isReadOnly() and (event.text().isalnum() or event.text() == "_"):
            self.show_completions()

    def show_completions(self) -> None:
        if self.isReadOnly():
            return
        cursor = self.textCursor()
        before_cursor = cursor.block().text()[:cursor.positionInBlock()]
        match = re.search(r"[A-Za-z_]\w*$", before_cursor)
        prefix = match.group(0) if match else ""
        if not prefix:
            self.completer.popup().hide()
            return
        keywords = {
            "and", "as", "assert", "async", "await", "break", "case", "class", "continue",
            "def", "elif", "else", "except", "False", "finally", "for", "from", "global",
            "if", "import", "in", "is", "lambda", "None", "not", "or", "pass", "raise",
            "return", "True", "try", "while", "with", "yield",
        }
        words = set(re.findall(r"\b[A-Za-z_]\w*\b", self.toPlainText())) | keywords
        suggestions = sorted(word for word in words if word.casefold().startswith(prefix.casefold()) and word != prefix)
        self.completion_model.setStringList(suggestions[:100])
        if not suggestions:
            self.completer.popup().hide()
            return
        self.completer.setCompletionPrefix(prefix)
        popup = self.completer.popup()
        popup.setCurrentIndex(self.completion_model.index(0, 0))
        rect = self.cursorRect()
        rect.setWidth(min(360, max(180, popup.sizeHintForColumn(0) + 30)))
        self.completer.complete(rect)

    def _insert_completion(self, completion: str) -> None:
        if self.isReadOnly() or not completion:
            return
        cursor = self.textCursor()
        before_cursor = cursor.block().text()[:cursor.positionInBlock()]
        match = re.search(r"[A-Za-z_]\w*$", before_cursor)
        prefix = match.group(0) if match else ""
        if prefix:
            cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor, len(prefix))
        cursor.insertText(completion)
        self.setTextCursor(cursor)
        self.completer.popup().hide()

    def line_number_area_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 16 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, _new_block_count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event):  # noqa: ANN001 - Qt event signature.
        super().resizeEvent(event)
        content_rect = self.contentsRect()
        self.line_number_area.setGeometry(QRect(content_rect.left(), content_rect.top(), self.line_number_area_width(), content_rect.height()))

    def line_number_area_paint_event(self, event) -> None:
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), self._line_number_background)
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(self._line_number_foreground)
                painter.drawText(0, top, self.line_number_area.width() - 8, self.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, str(block_number + 1))
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1

    def highlight_current_line(self) -> None:
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(self._current_line_background)
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        self.setExtraSelections([selection])

    def set_ui_profile(self, profile_key: str) -> None:
        profile = ui_profile_for_key(profile_key)
        self.ui_profile = profile.key
        self._line_number_background = QColor(profile.line_number_background)
        self._line_number_foreground = QColor(profile.line_number_foreground)
        self._current_line_background = QColor(profile.current_line_background)
        self.line_number_area.update()
        self.highlight_current_line()


class PythonHighlighter(QSyntaxHighlighter):
    _PATTERNS = (
        r"\b(and|as|assert|async|await|break|case|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|match|not|or|pass|raise|return|try|while|with|yield)\b",
        r"\b(True|False|None|self)\b",
        r"\b\d+(\.\d+)?\b",
        r"#[^\n]*",
        r'"[^"\n]*"|\'[^\'\n]*\'',
    )

    def __init__(self, document, profile_key: str = DEFAULT_UI_PROFILE):  # noqa: ANN001 - document type varies by binding.
        super().__init__(document)
        self.rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        self.profile_key = DEFAULT_UI_PROFILE
        self.set_ui_profile(profile_key)

    def set_ui_profile(self, profile_key: str) -> None:
        profile = ui_profile_for_key(profile_key)
        self.profile_key = profile.key
        self.rules.clear()
        for index, (pattern, color) in enumerate(zip(self._PATTERNS, profile.syntax_colors, strict=True)):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if index == 0:
                fmt.setFontWeight(QFont.Weight.Bold)
            self.rules.append((QRegularExpression(pattern), fmt))
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        for expression, fmt in self.rules:
            iterator = expression.globalMatch(text)
            while iterator.hasNext():
                match = iterator.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


class ProjectFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._filter_text = ""

    def set_filter_text(self, text: str) -> None:
        self._filter_text = text.strip().casefold()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        index = model.index(source_row, 0, source_parent)
        name = model.fileName(index)
        if name in CodingToolbox.EXCLUDED_NAMES:
            return False
        return not self._filter_text or model.isDir(index) or self._filter_text in name.casefold()


class WindowTitleBar(QFrame):
    """A small native-free title bar shared by the main window and dialogs."""

    def __init__(self, host: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self.host = host
        self._drag_offset: QPoint | None = None
        self.setObjectName("TitleBar")

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        if event.button() == Qt.MouseButton.LeftButton and not self.host.isMaximized():
            self._drag_offset = event.globalPosition().toPoint() - self.host.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.host.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        if event.button() == Qt.MouseButton.LeftButton and isinstance(self.host, QMainWindow):
            self.host.showNormal() if self.host.isMaximized() else self.host.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class _LegacyDevelopmentTreeView(QFrame):
    """Paint a readable development-attempt tree without pretending it is Git branches."""

    node_selected = Signal(object)

    def __init__(self, layout_path: Path | None = None):
        super().__init__()
        self.setObjectName("DevelopmentTree")
        self.setMinimumHeight(420)
        self.setMouseTracking(True)
        self.nodes: list[dict[str, Any]] = []
        self.selected_id: str | None = None
        self._hit_boxes: list[tuple[QRect, dict[str, Any]]] = []
        self._layout_path = Path(layout_path).resolve() if layout_path is not None else None
        self._node_offsets: dict[str, QPointF] = {}
        self._pan = QPointF(0, 0)
        self._zoom = 1.0
        self._press_pos = QPointF()
        self._press_pan = QPointF()
        self._press_node: dict[str, Any] | None = None
        self._press_node_offset = QPointF()
        self._drag_mode = "pan"
        self._dragging = False
        self._hover_node_id: str | None = None
        self._pulse_phase = 0.0
        self._animation_timer = QTimer(self)
        self._animation_timer.setInterval(34)
        self._animation_timer.timeout.connect(self._tick_animation)
        self._animation_timer.start()
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._load_layout()

    def _tick_animation(self) -> None:
        if not self.isVisible():
            return
        self._pulse_phase = (self._pulse_phase + 0.075) % math.tau
        self.update()

    def _load_layout(self) -> None:
        self._node_offsets = _load_tree_layout_offsets(self._layout_path)

    def _save_layout(self) -> None:
        if self._layout_path is None:
            return
        try:
            self._layout_path.parent.mkdir(parents=True, exist_ok=True)
            data = {node_id: {"x": round(offset.x(), 1), "y": round(offset.y(), 1)} for node_id, offset in self._node_offsets.items()}
            self._layout_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            return

    def set_nodes(self, nodes: list[dict[str, Any]]) -> None:
        self.nodes = nodes
        main_count = max(1, sum(node.get("lane") == "main" for node in nodes))
        attempts_by_parent: dict[str, int] = {}
        for node in nodes:
            if node.get("lane") == "attempt":
                parent_id = str(node.get("parent_id") or "root")
                attempts_by_parent[parent_id] = attempts_by_parent.get(parent_id, 0) + 1
        extra_height = sum(max(0, count - 1) * 72 for count in attempts_by_parent.values())
        self.setMinimumHeight(max(420, 150 + main_count * 118 + extra_height))
        ids = {node["id"] for node in nodes}
        self._node_offsets = {node_id: offset for node_id, offset in self._node_offsets.items() if node_id in ids}
        offset_bottom = max(
            (max(0, int(offset.y())) for node_id, offset in self._node_offsets.items() if node_id in ids),
            default=0,
        )
        self.setMinimumHeight(max(self.minimumHeight(), 150 + main_count * 118 + extra_height + offset_bottom))
        if self.selected_id not in ids:
            self.selected_id = nodes[0]["id"] if nodes else None
        self.update()
        selected = next((node for node in nodes if node["id"] == self.selected_id), None)
        if selected is not None:
            self.node_selected.emit(selected)

    def _positions(self) -> tuple[dict[str, tuple[int, int, QRect]], dict[str, tuple[int, int]]]:
        main_nodes = [node for node in self.nodes if node.get("lane") == "main"]
        if not main_nodes:
            return {}, {}
        card_width = min(270, max(180, int(self.width() * 0.31)))
        if self.width() < 620:
            # Two cards must still fit on opposite sides of the trunk in the
            # narrow vertical-split layout; otherwise their 2.5D shadows can
            # visually collide even though the graph geometry is valid.
            card_width = min(card_width, max(132, int((self.width() - 48) / 2)))
        trunk_x = self.width() // 2
        top = 78
        row_height = 118
        cards: dict[str, tuple[int, int, QRect]] = {}
        dots: dict[str, tuple[int, int]] = {}
        attempts_by_parent: dict[str, int] = {}
        for node in (item for item in self.nodes if item.get("lane") == "attempt"):
            parent_id = str(node.get("parent_id") or main_nodes[-1]["id"])
            attempts_by_parent[parent_id] = attempts_by_parent.get(parent_id, 0) + 1
        main_y: dict[str, int] = {}
        y = top
        for node in main_nodes:
            main_y[node["id"]] = y
            y += row_height + max(0, attempts_by_parent.get(node["id"], 0) - 1) * 72
        for node in main_nodes:
            y = main_y[node["id"]]
            dots[node["id"]] = (trunk_x, y)
            card_x = max(12, trunk_x - card_width - 58)
            cards[node["id"]] = (trunk_x, y, QRect(card_x, y - 31, card_width, 62))

        children_count: dict[str, int] = {}
        for node in (item for item in self.nodes if item.get("lane") == "attempt"):
            parent_id = node.get("parent_id") or main_nodes[-1]["id"]
            parent_x, parent_y = dots.get(parent_id, dots[main_nodes[-1]["id"]])
            slot = children_count.get(parent_id, 0)
            children_count[parent_id] = slot + 1
            side = 1
            offset = self._node_offsets.get(str(node["id"]), QPointF())
            branch_y = int(parent_y + 49 + slot * 72 + offset.y())
            branch_x = int(parent_x + side * 78 + offset.x())
            if side > 0:
                card_x = min(self.width() - card_width - 12, branch_x + 24)
            else:
                card_x = max(12, branch_x - card_width - 24)
            cards[node["id"]] = (branch_x, branch_y, QRect(card_x, branch_y - 31, card_width, 62))
            dots[node["id"]] = (branch_x, branch_y)
        return cards, dots

    @staticmethod
    def _accent(node: dict[str, Any]) -> QColor:
        if node.get("lane") == "main":
            return QColor("#4ec9b0")
        return {
            "failed": QColor("#f48771"),
            "retry": QColor("#e5c07b"),
            "active": QColor("#569cd6"),
        }.get(node.get("status"), QColor("#9d9d9d"))

    @staticmethod
    def _status_text(node: dict[str, Any]) -> str:
        if node.get("lane") == "main":
            return "当前主线"
        return {
            "failed": "已取消 / 失败",
            "retry": "等待重试",
            "active": "进行中",
        }.get(node.get("status"), "尝试方向")

    def _screen_rect(self, rect: QRect) -> QRect:
        return QRect(
            int(rect.left() * self._zoom + self._pan.x()),
            int(rect.top() * self._zoom + self._pan.y()),
            max(1, int(rect.width() * self._zoom)),
            max(1, int(rect.height() * self._zoom)),
        )

    def _node_at(self, point: QPoint) -> dict[str, Any] | None:
        for rect, node in reversed(self._hit_boxes):
            if rect.contains(point):
                return node
        return None

    @staticmethod
    def _lerp(start: QPointF, end: QPointF, amount: float) -> QPointF:
        return QPointF(
            start.x() + (end.x() - start.x()) * amount,
            start.y() + (end.y() - start.y()) * amount,
        )

    def _draw_energy_pulse(self, painter: QPainter, point: QPointF, accent: QColor, radius: float = 3.0) -> None:
        """Draw a small animated light that makes the graph feel alive."""
        halo_radius = radius * 4.5
        halo = QRadialGradient(point, halo_radius)
        halo.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 175))
        halo.setColorAt(0.45, QColor(accent.red(), accent.green(), accent.blue(), 52))
        halo.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(halo))
        painter.drawEllipse(point, halo_radius, halo_radius)
        painter.setBrush(accent)
        painter.drawEllipse(point, radius, radius)

    def _draw_node_halo(self, painter: QPainter, point: QPointF, accent: QColor, selected: bool, hovered: bool) -> None:
        if not selected and not hovered:
            return
        phase = (math.sin(self._pulse_phase * 1.35) + 1.0) / 2.0
        radius = 16.0 + phase * 7.0 if selected else 14.0 + phase * 4.0
        alpha = int(48 + phase * 32) if selected else int(34 + phase * 22)
        halo = QRadialGradient(point, radius)
        halo.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), alpha))
        halo.setColorAt(0.55, QColor(accent.red(), accent.green(), accent.blue(), alpha // 3))
        halo.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(halo))
        painter.drawEllipse(point, radius, radius)

    def paintEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        background = QLinearGradient(0, 0, 0, self.height())
        background.setColorAt(0.0, QColor("#202a31"))
        background.setColorAt(0.16, QColor("#1b2329"))
        background.setColorAt(0.48, QColor("#151a1f"))
        background.setColorAt(1.0, QColor("#111519"))
        painter.fillRect(event.rect(), background)

        # A faint horizon glow and perspective grid add depth without competing
        # with the actual development graph.
        horizon = 54
        vanishing_x = self.width() * 0.52
        horizon_glow = QRadialGradient(QPointF(vanishing_x, horizon), max(220.0, self.width() * 0.62))
        horizon_glow.setColorAt(0.0, QColor(71, 143, 139, 30))
        horizon_glow.setColorAt(0.55, QColor(35, 72, 78, 12))
        horizon_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(horizon_glow))
        painter.drawEllipse(
            QPointF(vanishing_x, horizon),
            max(220.0, self.width() * 0.62),
            max(220.0, self.width() * 0.62),
        )

        for x in range(-self.width(), self.width() * 2 + 1, 36):
            end_x = vanishing_x + (x - vanishing_x) * 0.42
            painter.setPen(QPen(QColor(107, 131, 139, 20), 1))
            painter.drawLine(x, horizon, int(end_x), self.height())
        for index in range(13):
            ratio = (index / 12.0) ** 1.7
            y = int(horizon + (self.height() - horizon) * ratio)
            painter.setPen(QPen(QColor(107, 131, 139, 16 + index), 1))
            painter.drawLine(0, y, self.width(), y)
        painter.setPen(QPen(QColor(91, 218, 191, 24), 1))
        painter.drawLine(0, horizon, self.width(), horizon)

        cards, dots = self._positions()
        self._hit_boxes = []
        main_nodes = [node for node in self.nodes if node.get("lane") == "main"]
        painter.save()
        painter.translate(self._pan)
        painter.scale(self._zoom, self._zoom)
        if not main_nodes:
            painter.restore()
            painter.setPen(QColor("#9d9d9d"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "还没有开发记录")
            return

        trunk_x = self.width() // 2
        first_y = dots[main_nodes[0]["id"]][1]
        last_y = dots[main_nodes[-1]["id"]][1]
        # The trunk is layered to look like a slim illuminated rail floating
        # above the canvas instead of a flat diagram line.
        painter.setPen(QPen(QColor(0, 0, 0, 130), 13, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(trunk_x + 5, first_y + 8, trunk_x + 5, last_y + 8)
        painter.setPen(QPen(QColor("#0b1115"), 11, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(trunk_x, first_y, trunk_x, last_y)
        painter.setPen(QPen(QColor("#284a45"), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(trunk_x, first_y, trunk_x, last_y)
        painter.setPen(QPen(QColor("#5bd9bd"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(trunk_x, first_y, trunk_x, last_y)
        for node in main_nodes:
            y = dots[node["id"]][1]
            painter.setPen(QPen(QColor(126, 237, 212, 80), 1))
            painter.drawLine(trunk_x - 14, y, trunk_x + 14, y)

        for node in (item for item in self.nodes if item.get("lane") == "attempt"):
            parent = dots.get(node.get("parent_id"), dots[main_nodes[-1]["id"]])
            current = dots[node["id"]]
            side = 1 if current[0] > parent[0] else -1
            path = QPainterPath()
            path.moveTo(parent[0], parent[1])
            path.cubicTo(parent[0] + side * 46, parent[1], current[0] - side * 36, current[1], current[0], current[1])
            accent = self._accent(node)
            hovered = str(node["id"]) == self._hover_node_id
            painter.save()
            painter.translate(5, 7)
            painter.setPen(QPen(QColor(0, 0, 0, 120), 10, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawPath(path)
            painter.restore()
            glow_alpha = 70 if hovered else 32
            painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), glow_alpha), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawPath(path)
            painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 150 if hovered else 92), 2.2))
            painter.drawPath(path)
            if node.get("status") in {"active", "retry"} or hovered:
                seed = (sum(ord(char) for char in str(node["id"])) % 100) / 100.0
                progress = (self._pulse_phase / math.tau * 0.65 + seed) % 1.0
                self._draw_energy_pulse(painter, path.pointAtPercent(progress), accent, 2.3 if hovered else 1.8)

        for node in self.nodes:
            if node["id"] not in dots:
                continue
            x, y = dots[node["id"]]
            rect = cards[node["id"]][2]
            accent = self._accent(node)
            selected = node["id"] == self.selected_id
            hovered = str(node["id"]) == self._hover_node_id
            self._draw_node_halo(painter, QPointF(x, y), accent, selected, hovered)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 110))
            painter.drawEllipse(x - 12, y - 8, 24, 24)
            painter.setBrush(QColor("#10171a"))
            painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 230), 3))
            painter.drawEllipse(x - 9, y - 9, 18, 18)
            painter.setBrush(accent)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(x - 4, y - 4, 8, 8)

            painter.setPen(Qt.PenStyle.NoPen)
            # Offset layers are the visible extrusion under each floating card.
            for depth in range(10, 0, -1):
                shade = max(18, 105 - depth * 7)
                painter.setBrush(QColor(accent.red(), accent.green(), accent.blue(), shade))
                painter.drawRoundedRect(rect.translated(int(depth * 0.65), depth), 8, 8)
            painter.setBrush(QColor(0, 0, 0, 135))
            painter.drawRoundedRect(rect.translated(6, 8), 8, 8)
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            if selected:
                gradient.setColorAt(0.0, QColor("#263f42"))
                gradient.setColorAt(0.48, QColor("#25363b"))
                gradient.setColorAt(1.0, QColor("#1b262c"))
            elif hovered:
                gradient.setColorAt(0.0, QColor("#303c43"))
                gradient.setColorAt(0.48, QColor("#2a343a"))
                gradient.setColorAt(1.0, QColor("#20272d"))
            else:
                gradient.setColorAt(0.0, QColor("#2b343b"))
                gradient.setColorAt(0.48, QColor("#272f35"))
                gradient.setColorAt(1.0, QColor("#1b2025"))
            painter.setBrush(gradient)
            border = accent if selected else QColor("#8bd8d0" if hovered else "#53606a")
            painter.setPen(QPen(border, 1.4 if selected or hovered else 1.0))
            painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(QPen(QColor(255, 255, 255, 38), 1))
            painter.drawLine(rect.left() + 10, rect.top() + 1, rect.right() - 10, rect.top() + 1)
            painter.setBrush(accent)
            painter.drawRoundedRect(QRect(rect.left(), rect.top(), 4, rect.height()), 2, 2)
            if node.get("lane") == "attempt":
                painter.setBrush(QColor(210, 220, 225, 90))
                for grip_y in (rect.top() + 23, rect.top() + 29, rect.top() + 35):
                    painter.drawEllipse(rect.right() - 14, grip_y, 3, 3)

            status_label = "主线" if node.get("lane") == "main" else {
                "failed": "失败",
                "retry": "重试",
                "active": "进行中",
            }.get(node.get("status"), "尝试")
            badge_font = QFont("Segoe UI", 9)
            painter.setFont(badge_font)
            badge_width = max(34, painter.fontMetrics().horizontalAdvance(status_label) + 14)
            badge_rect = QRect(rect.right() - badge_width - 10, rect.top() + 8, badge_width, 18)
            painter.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 38 if not selected else 62))
            painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 120), 1))
            painter.drawRoundedRect(badge_rect, 8, 8)
            painter.setPen(accent)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, status_label)

            text_rect = QRect(rect.left() + 13, rect.top() + 7, max(40, badge_rect.left() - rect.left() - 20), rect.height() - 14)
            painter.setPen(QColor("#f2f2f2"))
            title_font = QFont("Segoe UI", 10)
            title_font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(title_font)
            title = painter.fontMetrics().elidedText(str(node.get("title", "")), Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(text_rect.left(), text_rect.top() + 14, title)
            painter.setPen(accent)
            status_font = QFont("Segoe UI", 9)
            painter.setFont(status_font)
            painter.drawText(text_rect.left(), text_rect.top() + 33, self._status_text(node))
            painter.setPen(QColor("#aab5ba"))
            meta = str(node.get("meta", ""))
            meta = painter.fontMetrics().elidedText(meta, Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(text_rect.left(), text_rect.top() + 49, meta)
            self._hit_boxes.append((self._screen_rect(rect), node))

        trunk_progress = (self._pulse_phase / math.tau * 0.55 + 0.08) % 1.0
        trunk_point = self._lerp(QPointF(trunk_x, first_y), QPointF(trunk_x, last_y), trunk_progress)
        self._draw_energy_pulse(painter, trunk_point, QColor("#75f2d2"), 2.7)

        painter.restore()
        painter.setPen(QColor("#d8f5ed"))
        header_font = QFont("Segoe UI", 11)
        header_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(header_font)
        painter.drawText(18, 24, "开发尝试树")
        painter.setPen(QColor("#858585"))
        painter.setFont(QFont("Segoe UI", 10))
        helper_rect = QRect(18, 10, max(100, self.width() - 36), 20)
        painter.drawText(
            helper_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{int(self._zoom * 100)}%  ·  拖动分支 / 平移画布",
        )
        legend = "主干 = 当前编码主线  ·  分支 = 已取消、失败或等待中的尝试方向"
        legend_rect = QRect(18, 31, max(100, self.width() - 36), 18)
        legend = painter.fontMetrics().elidedText(legend, Qt.TextElideMode.ElideRight, legend_rect.width())
        painter.drawText(legend_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, legend)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position()
            self._press_pan = QPointF(self._pan)
            self._press_node = self._node_at(event.position().toPoint())
            self._drag_mode = "node" if self._press_node and self._press_node.get("lane") == "attempt" else "pan"
            self._press_node_offset = QPointF(
                self._node_offsets.get(str(self._press_node["id"]), QPointF())
                if self._press_node is not None
                else QPointF()
            )
            self._dragging = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        if event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.position() - self._press_pos
            if not self._dragging and (abs(delta.x()) > 5 or abs(delta.y()) > 5):
                self._dragging = True
            if self._dragging:
                if self._drag_mode == "node" and self._press_node is not None:
                    node_id = str(self._press_node["id"])
                    self._node_offsets[node_id] = QPointF(
                        self._press_node_offset.x() + delta.x() / self._zoom,
                        self._press_node_offset.y() + delta.y() / self._zoom,
                    )
                    self.setMinimumHeight(max(self.minimumHeight(), int(self.height() + delta.y())))
                else:
                    self._pan = QPointF(self._press_pan.x() + delta.x(), self._press_pan.y() + delta.y())
                self.update()
            event.accept()
            return
        node = self._node_at(event.position().toPoint())
        hover_id = str(node["id"]) if node is not None else None
        if hover_id != self._hover_node_id:
            self._hover_node_id = hover_id
            self.update()
        if node and node.get("lane") == "attempt":
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        elif node:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        if self._hover_node_id is not None:
            self._hover_node_id = None
            self.update()
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        if event.button() == Qt.MouseButton.LeftButton:
            if self._press_node is not None and (not self._dragging or self._drag_mode == "node"):
                self.selected_id = self._press_node["id"]
                self.node_selected.emit(self._press_node)
                self.update()
            if self._dragging and self._drag_mode == "node":
                self._save_layout()
            self._press_node = None
            self._drag_mode = "pan"
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        delta = event.angleDelta().y()
        if not delta:
            event.ignore()
            return
        old_zoom = self._zoom
        factor = 1.12 if delta > 0 else 1 / 1.12
        self._zoom = max(0.65, min(1.8, old_zoom * factor))
        cursor = event.position()
        logical_x = (cursor.x() - self._pan.x()) / old_zoom
        logical_y = (cursor.y() - self._pan.y()) / old_zoom
        self._pan = QPointF(cursor.x() - logical_x * self._zoom, cursor.y() - logical_y * self._zoom)
        self.update()
        event.accept()


class DevelopmentTreeView(QQuickWidget):
    """QML-backed development tree with a Python persistence/compatibility shell."""

    node_selected = Signal(object)

    def __init__(
        self,
        layout_path: Path | None = None,
        visual_theme: str = DEFAULT_UI_PROFILE,
    ):
        super().__init__()
        self.setObjectName("DevelopmentTree")
        self.setMinimumHeight(420)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._visual_theme = ui_profile_for_key(visual_theme).tree_mode
        self.setClearColor(QColor("#101419"))
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.nodes: list[dict[str, Any]] = []
        self.selected_id: str | None = None
        self._layout_path = Path(layout_path).resolve() if layout_path is not None else None
        self._node_offsets: dict[str, QPointF] = {}
        self._root_item = None
        self._load_layout()

        qml_path = application_base_dir() / "ui" / "qml" / "DevelopmentTree.qml"
        self.setSource(QUrl.fromLocalFile(str(qml_path)))
        root = self.rootObject()
        if root is not None:
            self._root_item = root
            root.nodeSelected.connect(self._on_qml_node_selected)
            root.nodeMoved.connect(self._on_qml_node_moved)
            root.setProperty("layoutOffsets", self._qml_offsets())
            root.setProperty("visualTheme", self._visual_theme)

    def set_visual_theme(self, profile_key: str) -> None:
        """Keep the QML canvas aligned with the selected workbench profile."""
        self._visual_theme = ui_profile_for_key(profile_key).tree_mode
        clear_color = {
            "paper": "#f5f8fb",
            "focus": "#11131b",
            "studio": "#101419",
        }[self._visual_theme]
        self.setClearColor(QColor(clear_color))
        if self._root_item is not None:
            self._root_item.setProperty("visualTheme", self._visual_theme)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if self._root_item is None:
                super().wheelEvent(event)
                return
            delta = event.pixelDelta().y() or event.angleDelta().y()
            if delta == 0:
                event.ignore()
                return
            old_zoom = float(self._root_item.property("zoom"))
            factor = 1.12 if delta > 0 else 1 / 1.12
            new_zoom = max(0.65, min(1.8, old_zoom * factor))
            position = event.position()
            pan_x = float(self._root_item.property("panX"))
            pan_y = float(self._root_item.property("panY"))
            logical_x = (position.x() - pan_x) / old_zoom
            logical_y = (position.y() - pan_y) / old_zoom
            self._root_item.setProperty("zoom", new_zoom)
            self._root_item.setProperty("panX", position.x() - logical_x * new_zoom)
            self._root_item.setProperty("panY", position.y() - logical_y * new_zoom)
            event.accept()
            return

        scroll_area = self.parentWidget()
        while scroll_area is not None and not isinstance(scroll_area, QScrollArea):
            scroll_area = scroll_area.parentWidget()
        if scroll_area is None:
            super().wheelEvent(event)
            return

        scroll_bar = scroll_area.verticalScrollBar()
        delta = event.pixelDelta().y()
        if delta == 0:
            wheel_steps = event.angleDelta().y() / 120
            delta = wheel_steps * max(1, scroll_bar.singleStep()) * 3
        if delta == 0:
            super().wheelEvent(event)
            return
        scroll_bar.setValue(scroll_bar.value() - round(delta))
        event.accept()

    @staticmethod
    def _accent(node: dict[str, Any]) -> QColor:
        if node.get("lane") == "main":
            return QColor("#55e3c1")
        return {
            "failed": QColor("#ff8978"),
            "retry": QColor("#f2ca78"),
            "active": QColor("#70b8ff"),
        }.get(node.get("status"), QColor("#a7b1b8"))

    @staticmethod
    def _status_text(node: dict[str, Any]) -> str:
        if node.get("lane") == "main":
            return "当前主线"
        return {
            "failed": "已取消 / 失败",
            "retry": "等待重试",
            "active": "进行中",
        }.get(node.get("status"), "尝试方向")

    def _load_layout(self) -> None:
        self._node_offsets = _load_tree_layout_offsets(self._layout_path)

    def _save_layout(self) -> None:
        if self._layout_path is None:
            return
        try:
            self._layout_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                node_id: {"x": round(offset.x(), 1), "y": round(offset.y(), 1)}
                for node_id, offset in self._node_offsets.items()
            }
            self._layout_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            return

    def _qml_offsets(self) -> dict[str, dict[str, float]]:
        return {
            node_id: {"x": float(offset.x()), "y": float(offset.y())}
            for node_id, offset in self._node_offsets.items()
        }

    def _on_qml_node_selected(self, node_id: str) -> None:
        self.selected_id = str(node_id)
        selected = next((node for node in self.nodes if str(node.get("id")) == self.selected_id), None)
        if selected is not None:
            self.node_selected.emit(selected)

    def _on_qml_node_moved(self, node_id: str, offset_x: float, offset_y: float) -> None:
        x = _finite_tree_coordinate(offset_x)
        y = _finite_tree_coordinate(offset_y)
        if x is None or y is None:
            return
        self._node_offsets[str(node_id)] = QPointF(x, y)
        self._save_layout()

    def set_nodes(self, nodes: list[dict[str, Any]]) -> None:
        self.nodes = nodes
        ids = {str(node["id"]) for node in nodes}
        self._node_offsets = {node_id: offset for node_id, offset in self._node_offsets.items() if node_id in ids}
        if self.selected_id not in ids:
            self.selected_id = str(nodes[0]["id"]) if nodes else None

        main_count = max(1, sum(node.get("lane") == "main" for node in nodes))
        attempts_by_parent: dict[str, int] = {}
        for node in nodes:
            if node.get("lane") == "attempt":
                parent_id = str(node.get("parent_id") or "root")
                attempts_by_parent[parent_id] = attempts_by_parent.get(parent_id, 0) + 1
        extra_height = sum(max(0, count - 1) * 90 for count in attempts_by_parent.values())
        offset_bottom = max((max(0, int(offset.y())) for offset in self._node_offsets.values()), default=0)
        self.setMinimumHeight(max(420, 140 + main_count * 122 + extra_height + offset_bottom))

        if self._root_item is not None:
            qml_nodes = []
            for node in nodes:
                item = dict(node)
                offset = self._node_offsets.get(str(node["id"]), QPointF())
                item["offset_x"] = float(offset.x())
                item["offset_y"] = float(offset.y())
                qml_nodes.append(item)
            self._root_item.setProperty("nodes", qml_nodes)
            self._root_item.setProperty("layoutOffsets", self._qml_offsets())
            self._root_item.setProperty("selectedId", self.selected_id or "")

        selected = next((node for node in nodes if str(node.get("id")) == self.selected_id), None)
        if selected is not None:
            self.node_selected.emit(selected)

    def _positions(self) -> tuple[dict[str, tuple[int, int, QRect]], dict[str, tuple[int, int]]]:
        """Return the logical geometry used by the QML scene and UI smoke checks."""
        main_nodes = [node for node in self.nodes if node.get("lane") == "main"]
        if not main_nodes:
            return {}, {}
        card_width = min(270, max(180, int(self.width() * 0.31)))
        if self.width() < 620:
            card_width = min(card_width, max(132, int((self.width() - 48) / 2)))
        trunk_x = self.width() // 2
        card_height = 68
        compact_layout = self.width() < 520 or self.height() < 360
        top = 48 if compact_layout else 88
        row_height = 80 if compact_layout else 122
        attempt_offset = 40 if compact_layout else 54
        attempt_slot_height = 90
        cards: dict[str, tuple[int, int, QRect]] = {}
        dots: dict[str, tuple[int, int]] = {}
        attempts_by_parent: dict[str, int] = {}
        for node in (item for item in self.nodes if item.get("lane") == "attempt"):
            parent_id = str(node.get("parent_id") or "root")
            attempts_by_parent[parent_id] = attempts_by_parent.get(parent_id, 0) + 1
        main_y: dict[str, int] = {}
        y = top
        for node in main_nodes:
            main_y[node["id"]] = y
            y += row_height + max(0, attempts_by_parent.get(str(node["id"]), 0) - 1) * attempt_slot_height
        for node in main_nodes:
            y = main_y[node["id"]]
            dots[node["id"]] = (trunk_x, y)
            card_x = max(12, trunk_x - card_width - 58)
            cards[node["id"]] = (trunk_x, y, QRect(card_x, y - card_height // 2, card_width, card_height))

        children_count: dict[str, int] = {}
        for node in (item for item in self.nodes if item.get("lane") == "attempt"):
            parent_id = str(node.get("parent_id") or main_nodes[-1]["id"])
            parent_x, parent_y = dots.get(parent_id, dots[main_nodes[-1]["id"]])
            slot = children_count.get(parent_id, 0)
            children_count[parent_id] = slot + 1
            offset = self._node_offsets.get(str(node["id"]), QPointF())
            dot_x = int(parent_x + 78 + offset.x())
            dot_y = max(90, int(parent_y + attempt_offset + slot * attempt_slot_height + offset.y()))
            card_x = min(self.width() - card_width - 12, dot_x + 24)
            cards[node["id"]] = (dot_x, dot_y, QRect(card_x, dot_y - card_height // 2, card_width, card_height))
            dots[node["id"]] = (dot_x, dot_y)
        return cards, dots


class MessageBubble(QFrame):
    def __init__(self, speaker: str, message: str, kind: str):
        super().__init__()
        self.setObjectName(
            {
                "user": "UserBubble",
                "agent": "AgentBubble",
                "tool": "ToolBubble",
                "meta": "MetaBubble",
                "summary": "SummaryBubble",
            }.get(kind, "AgentBubble")
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 9)
        layout.setSpacing(4)
        self.role_label = QLabel(speaker)
        self.role_label.setObjectName("BubbleRole")
        layout.addWidget(self.role_label)
        self.body_label = QLabel(message)
        self.body_label.setObjectName("ToolText" if kind == "tool" else "BubbleText")
        self.body_label.setTextFormat(Qt.TextFormat.PlainText)
        self.body_label.setWordWrap(True)
        self.body_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.body_label)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

    def set_speaker(self, speaker: str) -> None:
        self.role_label.setText(speaker)

    def set_message(self, message: str) -> None:
        self.body_label.setText(message)

    def append_message(self, text: str) -> None:
        self.body_label.setText(self.body_label.text() + text)


class NotificationToast(QFrame):
    """A compact, non-blocking workspace notification."""

    dismissed = Signal()
    DISPLAY_MS = 9000

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("NotificationToast")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumWidth(0)
        self.setMaximumWidth(440)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 11, 9, 11)
        layout.setSpacing(10)

        badge = QLabel("!")
        badge.setObjectName("NotificationBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(24, 24)
        badge.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(3)
        self.title_label = QLabel()
        self.title_label.setObjectName("NotificationTitle")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        self.title_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        content.addWidget(self.title_label)
        self.body_label = QLabel()
        self.body_label.setObjectName("NotificationBody")
        self.body_label.setTextFormat(Qt.TextFormat.PlainText)
        self.body_label.setWordWrap(True)
        self.body_label.setMaximumHeight(96)
        self.body_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content.addWidget(self.body_label)
        layout.addLayout(content, 1)

        self.close_button = QToolButton()
        self.close_button.setObjectName("NotificationClose")
        self.close_button.setText("×")
        self.close_button.setToolTip("关闭通知")
        self.close_button.setAccessibleName("关闭通知")
        self.close_button.setFixedSize(26, 26)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.clicked.connect(self.dismiss)
        layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

    def show_message(self, title: str, message: str) -> None:
        self.title_label.setText(title)
        self.body_label.setText(message)
        self.body_label.setToolTip(message)
        self.adjustSize()
        self._timer.start(self.DISPLAY_MS)

    def dismiss(self) -> None:
        self._timer.stop()
        self.hide()
        self.dismissed.emit()


class ClientWindow(QMainWindow):
    WORKBENCH_COMPACT_BREAKPOINT = 1000
    EXPLORER_COLLAPSE_BREAKPOINT = 900
    WORKBENCH_WIDE_MINIMUMS = (240, 360, 280)
    WORKBENCH_COMPACT_MINIMUMS = (200, 320, 232)

    def __init__(self, project_root: Path):
        super().__init__()
        self.project_root = Path(project_root).resolve()
        self.setWindowTitle("SciDevHarness — Coding Workspace")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.resize(1500, 920)
        self.setMinimumSize(780, 480)
        self.ui_settings = QSettings("SciDevHarness", "SciDevHarness")
        self.ui_profile = ui_profile_for_key(
            self.ui_settings.value("uiProfile", DEFAULT_UI_PROFILE)
        ).key
        self.setStyleSheet(stylesheet_for_profile(THEME, self.ui_profile))
        self.ledger = EventLedger(self.project_root)
        self.git = GitManager(self.project_root)
        self.toolbox = CodingToolbox(self.project_root, self.ledger)
        self.summary_settings = SummarySettings.load(self.project_root)
        self.signals = AppSignals()
        self.current_session_id: str | None = None
        self.active_task_id: str | None = None
        self._streaming_bubbles: dict[tuple[str, int], MessageBubble] = {}
        self._active_notification: NotificationToast | None = None
        self.current_file: Path | None = None
        self._editor_paths: dict[QWidget, Path] = {}
        self._editor_titles: dict[QWidget, str] = {}
        self._editor_highlighters: dict[QWidget, PythonHighlighter] = {}
        self._pinned_editors: set[QWidget] = set()
        self._preview_editor: QWidget | None = None
        self.welcome_editor: CodeEditor | None = None
        self._closing = False
        self.task_dialog: QDialog | None = None
        self.task_history_tree: QTreeWidget | None = None
        self.window_max_button: QToolButton | None = None
        self.terminal_process: QProcess | None = None
        self._terminal_buffer = ""
        self.terminal_cwd = self.project_root
        self._entry_parent = self.project_root
        self._explorer_entry_mode = "create"
        self._rename_target: Path | None = None
        self._navigation_back: list[tuple[Path, int, int]] = []
        self._navigation_forward: list[tuple[Path, int, int]] = []
        self._workspace_search_whole_word = False
        self._workbench_user_ratios: tuple[float, float] | None = UI_PROFILES[
            self.ui_profile
        ].layout_ratios
        self._workbench_adapting = False
        self._workbench_adapt_pending = False
        self._explorer_visibility_override: bool | None = None
        self._titlebar_compact: bool | None = None
        self._git_tree_compact_restore: tuple[float, float, float] | None = None
        self._git_tree_compact_auto_view: tuple[float, float, float] | None = None
        self._git_tree_short_layout: bool | None = None

        self.agent = CodingAgent(
            self.project_root,
            self.ledger,
            self.git,
            event_callback=self._emit_agent,
            summary_settings=self.summary_settings,
            command_approval=self._request_command_approval,
        )
        self.worker = RetryQueue(
            self.ledger,
            {"coding": self._handle_coding},
            event_callback=self._emit_worker,
            retry_base_seconds=5.0,
        )
        self.signals.agent_event.connect(self._handle_agent_event)
        self.signals.worker_event.connect(self._handle_worker_event)
        self.signals.command_approval_requested.connect(self._handle_command_approval_request)
        self._build_ui()
        self.set_ui_profile(self.ui_profile, persist=False)
        self._install_shortcuts()
        self.worker.start()
        self.refresh_all()
        self._append_chat("系统", "准备好了。打开项目文件开始编辑，或直接在右侧告诉 Agent 要修改什么。", "meta")

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_title_bar())

        workbench = QSplitter(Qt.Orientation.Horizontal)
        self.workbench = workbench
        workbench.setObjectName("Workbench")
        workbench.setChildrenCollapsible(False)
        workbench.setHandleWidth(5)
        workbench.addWidget(self._build_left_panel())
        self.workspace_stack = QStackedWidget()
        self.workspace_stack.setMinimumWidth(self.WORKBENCH_COMPACT_MINIMUMS[1])
        self.workspace_page = self._build_workspace()
        self.git_page = self._build_git_page()
        self.workspace_stack.addWidget(self.workspace_page)
        self.workspace_stack.addWidget(self.git_page)
        workbench.addWidget(self.workspace_stack)
        workbench.addWidget(self._build_chat_panel())
        profile = UI_PROFILES[self.ui_profile]
        workbench.setSizes(
            [
                round(profile.layout_ratios[0] * 1000),
                1,
                round(profile.layout_ratios[1] * 1000),
            ]
        )
        workbench.setStretchFactor(0, 0)
        workbench.setStretchFactor(1, 1)
        workbench.setStretchFactor(2, 0)
        workbench.splitterMoved.connect(self._on_workbench_splitter_moved)
        root_layout.addWidget(workbench, 1)
        self.setCentralWidget(root)
        self.command_search.textChanged.connect(self.file_proxy.set_filter_text)
        self.command_search.textChanged.connect(self._update_command_suggestions)
        self.command_search.returnPressed.connect(self._open_quick_search)

        status = QStatusBar()
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("StatusText")
        status.addWidget(self.status_label, 1)
        self.status_branch = QLabel("Git · 未初始化")
        self.status_branch.setObjectName("StatusBranch")
        self.status_branch.setMinimumWidth(0)
        self.status_branch.setMaximumWidth(300)
        self.status_branch.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        status.addPermanentWidget(self.status_branch)
        self.status_position = QLabel("Ln 1, Col 1")
        status.addPermanentWidget(self.status_position)
        status.addPermanentWidget(QLabel("UTF-8"))
        self.status_language = QLabel("Plain Text")
        status.addPermanentWidget(self.status_language)
        status.addPermanentWidget(QSizeGrip(self))
        self.setStatusBar(status)

    def _install_shortcuts(self) -> None:
        """Install workbench-wide shortcuts expected from a code editor."""
        self.quick_open_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        self.quick_open_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.quick_open_shortcut.activated.connect(self._focus_quick_search)

        self.open_workspace_shortcut = QShortcut(QKeySequence("Ctrl+K, Ctrl+O"), self)
        self.open_workspace_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.open_workspace_shortcut.activated.connect(self._choose_workspace)

        self.command_palette_shortcut = QShortcut(QKeySequence("Ctrl+Shift+P"), self)
        self.command_palette_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.command_palette_shortcut.activated.connect(self._focus_command_palette)

        self.find_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self.find_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.find_shortcut.activated.connect(lambda: self._show_find_bar(False))

        self.workspace_search_shortcut = QShortcut(QKeySequence("Ctrl+Shift+F"), self)
        self.workspace_search_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.workspace_search_shortcut.activated.connect(self._show_workspace_search)

        self.outline_shortcut = QShortcut(QKeySequence("Ctrl+Shift+O"), self)
        self.outline_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.outline_shortcut.activated.connect(self._show_outline)

        self.definition_shortcut = QShortcut(QKeySequence("F12"), self)
        self.definition_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.definition_shortcut.activated.connect(self._go_to_definition)

        self.references_shortcut = QShortcut(QKeySequence("Shift+F12"), self)
        self.references_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.references_shortcut.activated.connect(self._find_symbol_references)

        self.back_navigation_shortcut = QShortcut(QKeySequence("Alt+Left"), self)
        self.back_navigation_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.back_navigation_shortcut.activated.connect(self._navigate_back)

        self.forward_navigation_shortcut = QShortcut(QKeySequence("Alt+Right"), self)
        self.forward_navigation_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.forward_navigation_shortcut.activated.connect(self._navigate_forward)

        self.format_shortcut = QShortcut(QKeySequence("Shift+Alt+F"), self)
        self.format_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.format_shortcut.activated.connect(self._format_current_document)

        self.replace_shortcut = QShortcut(QKeySequence("Ctrl+H"), self)
        self.replace_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.replace_shortcut.activated.connect(lambda: self._show_find_bar(True))

        self.close_find_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.close_find_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.close_find_shortcut.activated.connect(
            lambda: self._close_find_bar() if self.find_bar.isVisible() else None
        )

        self.terminal_shortcut = QShortcut(QKeySequence("Ctrl+`"), self)
        self.terminal_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.terminal_shortcut.activated.connect(lambda: self._toggle_bottom_panel(1))

    def _build_title_bar(self) -> QFrame:
        title_bar = WindowTitleBar(self)
        title_bar.setFixedHeight(42)
        layout = QHBoxLayout(title_bar)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(9)

        logo = QLabel("S")
        logo.setObjectName("Logo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(25, 25)
        layout.addWidget(logo)
        title = QLabel("SciDevHarness")
        title.setObjectName("WindowTitle")
        layout.addWidget(title)
        layout.addWidget(self._vertical_rule())
        project = ElidedLabel(self.project_root.name)
        project.setObjectName("Subtle")
        project.setToolTip(str(self.project_root))
        project.setMinimumWidth(96)
        project.setMaximumWidth(220)
        project.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.title_project_label = project
        layout.addWidget(project)

        search = QLineEdit()
        self.command_search = search
        search.setObjectName("CommandSearch")
        search.setPlaceholderText("搜索文件、命令或跳转到…")
        search.setMinimumWidth(190)
        search.setMaximumWidth(340)
        search.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.command_completer = QCompleter(self)
        self.command_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.command_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.command_completer.setModel(
            QStringListModel(
                [
                    "> open folder",
                    "> terminal",
                    "> problems",
                    "> search",
                    "> replace",
                    "> outline",
                    "> git",
                    "> explorer",
                    "> appearance",
                ],
                self.command_completer,
            )
        )
        self.command_completer.setWidget(search)
        self.command_completer.activated.connect(self._run_command_completion)
        layout.addStretch(1)
        layout.addWidget(search)
        layout.addStretch(1)

        new_button = QPushButton("新会话")
        new_button.setObjectName("Primary")
        self.title_new_button = new_button
        new_button.clicked.connect(self.new_coding_task)
        layout.addWidget(new_button)
        git_button = QPushButton("版本树")
        git_button.setObjectName("GitPrimary")
        self.title_git_button = git_button
        git_button.clicked.connect(self.show_git)
        layout.addWidget(git_button)
        for text, command in (("刷新", self.refresh_all),):
            button = QPushButton(text)
            button.setObjectName("Quiet")
            button.setAccessibleName("刷新工作区")
            self.title_refresh_button = button
            button.clicked.connect(command)
            layout.addWidget(button)

        self.ui_theme_button = QToolButton()
        self.ui_theme_button.setObjectName("ThemeSelector")
        self.ui_theme_button.setText("主题")
        self.ui_theme_button.setAccessibleName("界面方案")
        self.ui_theme_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.ui_theme_button.setFixedHeight(28)
        self.ui_theme_menu = QMenu(self.ui_theme_button)
        self.ui_theme_actions = {}
        for profile_key, profile in UI_PROFILES.items():
            action = self.ui_theme_menu.addAction(profile.label)
            action.setCheckable(True)
            action.setChecked(profile_key == self.ui_profile)
            action.setToolTip(profile.description)
            action.triggered.connect(
                lambda _checked=False, selected=profile_key: self.set_ui_profile(selected)
            )
            self.ui_theme_actions[profile_key] = action
        self.ui_theme_button.setMenu(self.ui_theme_menu)
        layout.addWidget(self.ui_theme_button)

        self.connection_label = QLabel(self._provider_state())
        self.connection_label.setObjectName("StateReady")
        layout.addSpacing(4)
        layout.addWidget(self.connection_label)

        for symbol, tip, object_name, command in (
            ("—", "最小化", "WindowButton", lambda: self.showMinimized()),
            ("□", "最大化 / 还原", "WindowButton", self._toggle_maximize),
            ("×", "关闭", "WindowClose", lambda: self.close()),
        ):
            button = QToolButton()
            button.setObjectName(object_name)
            button.setText(symbol)
            button.setToolTip(tip)
            button.setFixedSize(34, 28)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(command)
            layout.addWidget(button)
            if symbol == "□":
                self.window_max_button = button
        return title_bar

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def set_ui_profile(self, profile_key: str, *, persist: bool = True) -> None:
        """Apply and optionally persist a complete, switchable workbench style."""
        profile = ui_profile_for_key(profile_key)
        changed = profile.key != self.ui_profile
        self.ui_profile = profile.key
        self.setStyleSheet(stylesheet_for_profile(THEME, profile))

        if persist:
            self.ui_settings.setValue("uiProfile", profile.key)
            self.ui_settings.sync()

        for editor in self.findChildren(CodeEditor):
            editor.set_ui_profile(profile.key)
        for highlighter in self._editor_highlighters.values():
            highlighter.set_ui_profile(profile.key)
        if hasattr(self, "git_tree"):
            self.git_tree.set_visual_theme(profile.key)

        if hasattr(self, "ui_theme_actions"):
            for key, action in self.ui_theme_actions.items():
                action.setChecked(key == profile.key)
        if hasattr(self, "ui_theme_button"):
            self.ui_theme_button.setToolTip(f"界面方案：{profile.label}\n{profile.description}")
            self.ui_theme_button.setAccessibleDescription(profile.description)
        for button, icon_name in zip(
            getattr(self, "activity_buttons", ()),
            ("explorer", "search", "git"),
        ):
            button.setIcon(activity_icon(icon_name, *profile.activity_icon_colors))

        if changed:
            self._workbench_user_ratios = profile.layout_ratios
            if hasattr(self, "workbench"):
                QTimer.singleShot(0, self._adapt_workbench_layout)

        if persist:
            app = QApplication.instance()
            sibling_windows = getattr(app, "_scidev_windows", []) if app is not None else ()
            for sibling in sibling_windows:
                if sibling is not self:
                    sibling.set_ui_profile(profile.key, persist=False)

    def _choose_workspace(self) -> None:
        selected = _choose_workspace_directory(self.project_root, self)
        if selected is not None:
            self._open_workspace_root(selected)

    def _open_workspace_root(self, workspace: str | Path) -> "ClientWindow | None":
        target = Path(workspace).expanduser().resolve()
        if not target.is_dir():
            self._show_message("无法打开文件夹", f"所选工作区不存在或不是文件夹：\n{target}")
            return None

        app = QApplication.instance()
        if app is None:
            return None
        windows: list[ClientWindow] = getattr(app, "_scidev_windows", [])
        if self not in windows:
            windows.append(self)
        app._scidev_windows = windows

        for window in windows:
            if window.project_root == target:
                if window.isMinimized():
                    window.showNormal()
                window.raise_()
                window.activateWindow()
                return window

        settings = QSettings("SciDevHarness", "SciDevHarness")
        settings.setValue("lastWorkspace", str(target))
        new_window = ClientWindow(target)
        windows.append(new_window)
        new_window.show()
        return new_window

    @staticmethod
    def _vertical_rule() -> QFrame:
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.VLine)
        rule.setFrameShadow(QFrame.Shadow.Plain)
        rule.setStyleSheet("color: #3a3a3a;")
        rule.setFixedHeight(18)
        return rule

    def _dialog_title_bar(self, dialog: QDialog, title: str) -> WindowTitleBar:
        bar = WindowTitleBar(dialog, dialog)
        bar.setFixedHeight(42)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 0, 8, 0)
        label = QLabel(title)
        label.setObjectName("WindowTitle")
        layout.addWidget(label)
        layout.addStretch(1)
        close = QToolButton()
        close.setObjectName("WindowClose")
        close.setText("×")
        close.setToolTip("关闭")
        close.setFixedSize(34, 28)
        close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close.clicked.connect(dialog.close)
        layout.addWidget(close)
        return bar

    def _show_message(self, title: str, message: str) -> None:
        if self._active_notification is None:
            notification = NotificationToast(self.centralWidget())
            notification.dismissed.connect(self._clear_active_notification)
            self._active_notification = notification
        else:
            notification = self._active_notification
        notification.show_message(title, message)
        self._position_notification()
        notification.show()
        notification.raise_()

    def _clear_active_notification(self) -> None:
        notification = self.sender()
        if notification is not self._active_notification:
            return
        self._active_notification = None
        notification.deleteLater()

    def _position_notification(self) -> None:
        notification = self._active_notification
        if notification is None or not hasattr(self, "workspace_stack"):
            return
        root = self.centralWidget()
        if root is None:
            return
        anchor = self.workspace_stack.mapTo(root, QPoint(0, 0))
        anchor_rect = QRect(anchor, self.workspace_stack.size())
        width = max(1, min(440, anchor_rect.width() - 24))
        notification.setFixedWidth(width)
        notification.adjustSize()
        height = notification.sizeHint().height()
        x = anchor_rect.x() + max(8, anchor_rect.width() - width - 12)
        y = max(8, root.height() - height - 14)
        notification.setGeometry(x, y, width, height)

    def _build_left_panel(self) -> QWidget:
        shell = QWidget()
        shell.setMinimumWidth(58)
        shell.setMaximumWidth(480)
        shell.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QHBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        rail = QFrame()
        rail.setObjectName("ActivityRail")
        rail.setFixedWidth(58)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 10, 0, 8)
        rail_layout.setSpacing(2)
        self.activity_buttons: list[QToolButton] = []
        for index, (icon_name, tip, command) in enumerate((
            ("explorer", "资源管理器", self.focus_explorer),
            ("search", "搜索项目", self._show_workspace_search),
            ("git", "开发版本树", self.show_git),
        )):
            button = QToolButton()
            button.setObjectName("ActivityButton")
            button.setIcon(
                activity_icon(icon_name, *UI_PROFILES[self.ui_profile].activity_icon_colors)
            )
            button.setIconSize(QSize(20, 20))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setText("")
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setChecked(index == 0)
            button.setFixedHeight(46)
            button.clicked.connect(command)
            rail_layout.addWidget(button)
            self.activity_buttons.append(button)
        rail_layout.addStretch(1)
        logo_bottom = QLabel("S")
        logo_bottom.setObjectName("Logo")
        logo_bottom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_bottom.setFixedSize(30, 30)
        rail_layout.addWidget(logo_bottom, 0, Qt.AlignmentFlag.AlignHCenter)

        explorer = QFrame()
        explorer.setObjectName("ExplorerPane")
        explorer_layout = QVBoxLayout(explorer)
        explorer_layout.setContentsMargins(12, 12, 8, 8)
        explorer_layout.setSpacing(7)
        heading = QHBoxLayout()
        overline = QLabel("资源管理器")
        overline.setObjectName("Overline")
        heading.addWidget(overline)
        heading.addStretch(1)
        for symbol, tip in (("+", "新建文件"), ("…", "刷新资源管理器")):
            action = QToolButton()
            action.setObjectName("IconButton")
            action.setText(symbol)
            action.setToolTip(tip)
            action.clicked.connect(self.start_new_file_entry if symbol == "+" else self.refresh_file_tree)
            heading.addWidget(action)
        explorer_layout.addLayout(heading)

        self.new_file_entry = QLineEdit()
        self.new_file_entry.setPlaceholderText("输入相对路径，例如 src/new_module.py，回车创建")
        self.new_file_entry.setVisible(False)
        self.new_file_entry.returnPressed.connect(self.create_new_file)
        explorer_layout.addWidget(self.new_file_entry)

        project_row = QHBoxLayout()
        project = ElidedLabel(f"⌄  {self.project_root.name}")
        project.setToolTip(str(self.project_root))
        project.setObjectName("AppTitle")
        self.project_label = project
        project_row.addWidget(project, 1)
        project_row.addStretch(1)
        open_workspace = QToolButton()
        open_workspace.setObjectName("IconButton")
        open_workspace.setText("⋯")
        open_workspace.setToolTip("打开文件夹…  (Ctrl+K, Ctrl+O)")
        open_workspace.setAccessibleName("打开文件夹")
        open_workspace.clicked.connect(self._choose_workspace)
        self.open_workspace_button = open_workspace
        project_row.addWidget(open_workspace)
        explorer_layout.addLayout(project_row)

        git_entry = QFrame()
        git_entry.setObjectName("GitEntry")
        git_entry_layout = QHBoxLayout(git_entry)
        git_entry_layout.setContentsMargins(0, 0, 0, 0)
        git_entry_button = QPushButton("开发版本树\n查看主线与尝试方向")
        git_entry_button.setObjectName("GitEntryButton")
        git_entry_button.setToolTip("查看主线与成功、失败、取消中的尝试方向；这是 Harness 开发树，不是 Git 分支")
        git_entry_button.setAccessibleName("开发版本树：查看主线与尝试方向")
        git_entry_button.clicked.connect(self.show_git)
        self.git_entry_button = git_entry_button
        git_entry_layout.addWidget(git_entry_button)
        explorer_layout.addWidget(git_entry)

        self.file_model = QFileSystemModel(self)
        self.file_model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        source_root = self.file_model.setRootPath(str(self.project_root))
        self.file_proxy = ProjectFilterProxy(self)
        self.file_proxy.setSourceModel(self.file_model)
        self.file_tree = QTreeView()
        self.file_tree.setObjectName("FileTree")
        self.file_tree.setModel(self.file_proxy)
        self.file_tree.setRootIndex(self.file_proxy.mapFromSource(source_root))
        self.file_tree.setHeaderHidden(True)
        self.file_tree.setAnimated(True)
        self.file_tree.setIndentation(14)
        self.file_tree.setUniformRowHeights(True)
        self.file_tree.setSelectionMode(QTreeView.SelectionMode.SingleSelection)
        for column in (1, 2, 3):
            self.file_tree.hideColumn(column)
        self.file_tree.clicked.connect(self._on_file_preview)
        self.file_tree.doubleClicked.connect(self._on_file_selected)
        self.file_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_tree.customContextMenuRequested.connect(self._show_explorer_menu)
        self.explorer_rename_shortcut = QShortcut(QKeySequence("F2"), self.file_tree)
        self.explorer_rename_shortcut.activated.connect(self.start_rename_entry)
        self.explorer_delete_shortcut = QShortcut(QKeySequence("Delete"), self.file_tree)
        self.explorer_delete_shortcut.activated.connect(lambda: self.delete_explorer_path(self._explorer_path()))
        self.file_tree.viewport().installEventFilter(self)
        explorer_layout.addWidget(self.file_tree, 1)

        hint = QLabel("本地 · .research")
        hint.setObjectName("Hint")
        hint.setToolTip("本地工作区 · .research 已启用")
        self.workspace_hint = hint
        explorer_layout.addWidget(hint)
        layout.addWidget(rail)
        layout.addWidget(explorer, 1)
        self.left_panel = shell
        self.explorer = explorer
        return shell

    def _create_editor_group(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.setTabsClosable(True)
        tabs.setMovable(True)
        tabs.setUsesScrollButtons(True)
        tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tabs.tabBar().customContextMenuRequested.connect(
            lambda point, group=tabs: self._show_editor_tab_menu(point, group)
        )
        tabs.tabCloseRequested.connect(
            lambda index, group=tabs: self._close_editor_tab(index, group)
        )
        tabs.currentChanged.connect(
            lambda index, group=tabs: self._on_editor_tab_changed(index, group)
        )
        return tabs

    def _install_tab_close_button(self, group: QTabWidget, index: int, editor: QWidget) -> None:
        close = QToolButton(group.tabBar())
        close.setObjectName("TabCloseButton")
        close.setText("×")
        close.setToolTip("关闭标签")
        close.setFixedSize(20, 20)
        close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close.clicked.connect(
            lambda _checked=False, target_group=group, target_editor=editor: self._close_editor_tab(
                target_group.indexOf(target_editor), target_group
            )
        )
        group.tabBar().setTabButton(index, QTabBar.ButtonPosition.RightSide, close)

    def _editor_groups(self) -> list[QTabWidget]:
        groups = [self.editor_tabs]
        if self.secondary_editor_tabs is not None:
            groups.append(self.secondary_editor_tabs)
        return groups

    def _editor_group_for(self, editor: QWidget | None) -> QTabWidget | None:
        if editor is None:
            return None
        for group in self._editor_groups():
            if group.indexOf(editor) >= 0:
                return group
        return None

    def _split_current_editor(self) -> None:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if path is None:
            self._append_log("请先打开一个项目文件，再进行编辑器分栏")
            return
        if self.secondary_editor_tabs is None:
            self.secondary_editor_tabs = self._create_editor_group()
            self.editor_splitter.addWidget(self.secondary_editor_tabs)
            self.editor_splitter.setSizes([1, 1])
        self.secondary_editor_tabs.setVisible(True)
        self._open_file(path, preview=False, group=self.secondary_editor_tabs)
        self._append_log(f"已在右侧编辑器组打开 {path.name}")

    def _toggle_editor_split(self) -> None:
        if self.secondary_editor_tabs is not None and self.secondary_editor_tabs.isVisible():
            self.secondary_editor_tabs.setVisible(False)
            self._active_editor_group = self.editor_tabs
            self.editor_tabs.setFocus()
            self._append_log("已关闭编辑器分栏")
            return
        self._split_current_editor()

    def _build_workspace(self) -> QWidget:
        workspace = QFrame()
        workspace.setObjectName("WorkspacePane")
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        workspace_toolbar = QFrame()
        workspace_toolbar.setObjectName("WorkspaceToolbar")
        workspace_toolbar.setFixedHeight(40)
        toolbar_layout = QHBoxLayout(workspace_toolbar)
        toolbar_layout.setContentsMargins(14, 0, 12, 0)
        self.breadcrumb = QLabel("项目  /  欢迎页")
        self.breadcrumb.setObjectName("Subtle")
        toolbar_layout.addWidget(self.breadcrumb)
        toolbar_layout.addStretch(1)
        self.workspace_state = QLabel("● 就绪")
        self.workspace_state.setObjectName("StateReady")
        toolbar_layout.addWidget(self.workspace_state)
        layout.addWidget(workspace_toolbar)

        editor_toolbar = QFrame()
        editor_toolbar.setObjectName("EditorToolbar")
        editor_toolbar.setFixedHeight(42)
        editor_layout = QHBoxLayout(editor_toolbar)
        editor_layout.setContentsMargins(14, 0, 8, 0)
        editor_label = QLabel("编辑器")
        editor_label.setObjectName("Overline")
        editor_layout.addWidget(editor_label)
        editor_layout.addStretch(1)
        save = QPushButton("保存")
        save.setObjectName("Quiet")
        save.setToolTip("保存当前文件（Ctrl+S）")
        save.clicked.connect(self.save_current_file)
        editor_layout.addWidget(save)
        find = QPushButton("查找")
        find.setObjectName("Quiet")
        find.clicked.connect(lambda: self._show_find_bar(False))
        editor_layout.addWidget(find)
        terminal = QPushButton("终端")
        terminal.setObjectName("Quiet")
        terminal.clicked.connect(lambda: self._toggle_bottom_panel(1))
        editor_layout.addWidget(terminal)
        more = QToolButton()
        more.setObjectName("Quiet")
        more.setText("更多")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        editor_menu = QMenu(more)
        editor_menu.addAction("搜索整个项目", self._show_workspace_search)
        editor_menu.addAction("文件大纲", self._show_outline)
        editor_menu.addAction("问题", lambda: self._toggle_bottom_panel(2))
        editor_menu.addAction("查看当前文件差异", self._show_current_diff)
        editor_menu.addSeparator()
        editor_menu.addAction("在右侧分栏打开", self._toggle_editor_split)
        more.setMenu(editor_menu)
        editor_layout.addWidget(more)
        layout.addWidget(editor_toolbar)

        self.find_bar = self._build_find_bar()
        layout.addWidget(self.find_bar)

        self.editor_tabs = self._create_editor_group()
        self._active_editor_group = self.editor_tabs
        self.secondary_editor_tabs: QTabWidget | None = None
        self.welcome_editor = CodeEditor()
        self.welcome_editor.setReadOnly(True)
        self.welcome_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.welcome_editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._configure_editor(self.welcome_editor)
        self.code_editor = self.welcome_editor
        welcome_index = self.editor_tabs.addTab(self.welcome_editor, "欢迎页")
        self.editor_tabs.tabBar().setTabButton(welcome_index, QTabBar.ButtonPosition.RightSide, None)
        self.editor_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.editor_splitter.setObjectName("EditorSplitter")
        self.editor_splitter.setChildrenCollapsible(False)
        self.editor_splitter.setHandleWidth(1)
        self.editor_splitter.addWidget(self.editor_tabs)
        layout.addWidget(self.editor_splitter, 1)
        self.bottom_tabs = self._build_bottom_panel()
        layout.addWidget(self.bottom_tabs)
        self._show_welcome()
        return workspace

    def _on_workbench_splitter_moved(self, _position: int, _index: int) -> None:
        if not self._workbench_adapting:
            sizes = self.workbench.sizes()
            total = sum(sizes)
            if total > 0 and len(sizes) == 3:
                if self.explorer.isHidden():
                    left_ratio = (
                        self._workbench_user_ratios[0]
                        if self._workbench_user_ratios is not None
                        else UI_PROFILES[self.ui_profile].layout_ratios[0]
                    )
                else:
                    left_ratio = sizes[0] / total
                self._workbench_user_ratios = (left_ratio, sizes[2] / total)

    def _workbench_minimum_widths(self) -> tuple[int, int, int]:
        minimums = (
            self.WORKBENCH_COMPACT_MINIMUMS
            if self.width() < self.WORKBENCH_COMPACT_BREAKPOINT
            else self.WORKBENCH_WIDE_MINIMUMS
        )
        if hasattr(self, "explorer") and self.explorer.isHidden():
            return (58, minimums[1], minimums[2])
        return minimums

    def _apply_responsive_explorer_visibility(self) -> bool:
        if not hasattr(self, "explorer"):
            return True
        should_show = self._explorer_visibility_override
        if should_show is None:
            should_show = self.width() >= self.EXPLORER_COLLAPSE_BREAKPOINT
        if (not self.explorer.isHidden()) != should_show:
            self.explorer.setVisible(should_show)
        if hasattr(self, "left_panel"):
            if should_show:
                minimum = (
                    self.WORKBENCH_COMPACT_MINIMUMS[0]
                    if self.width() < self.WORKBENCH_COMPACT_BREAKPOINT
                    else self.WORKBENCH_WIDE_MINIMUMS[0]
                )
            else:
                minimum = 58
            self.left_panel.setMinimumWidth(minimum)
        if getattr(self, "activity_buttons", None):
            label = "隐藏资源管理器" if should_show else "显示资源管理器"
            self.activity_buttons[0].setToolTip(label)
            self.activity_buttons[0].setAccessibleName(label)
            self.activity_buttons[0].setAccessibleDescription(label)
        return should_show

    def _adapt_workbench_layout(self) -> None:
        self._workbench_adapt_pending = False
        if not hasattr(self, "workbench"):
            return
        explorer_visible = self._apply_responsive_explorer_visibility()
        total = self.workbench.width() - self.workbench.handleWidth() * 2
        if total <= 0:
            return
        if self._workbench_user_ratios is None:
            left = int(total * 0.245)
            chat = int(total * 0.275)
        else:
            left_ratio, chat_ratio = self._workbench_user_ratios
            left = int(total * left_ratio)
            chat = int(total * chat_ratio)
        left_minimum, center_minimum, chat_minimum = self._workbench_minimum_widths()
        if explorer_visible:
            left = min(440, max(left_minimum, left))
        else:
            left = 58
        chat = min(460, max(chat_minimum, chat))
        center = total - left - chat
        if center < center_minimum:
            deficit = center_minimum - center
            chat_reduction = min(deficit, max(0, chat - chat_minimum))
            chat -= chat_reduction
            deficit -= chat_reduction
            if explorer_visible:
                left -= min(deficit, max(0, left - left_minimum))
            center = total - left - chat
        self._workbench_adapting = True
        try:
            self.workbench.setSizes([left, max(1, center), chat])
        finally:
            self._workbench_adapting = False
        self._adapt_explorer_density()
        self._adapt_chat_composer_density()

    def _adapt_explorer_density(self) -> None:
        git_entry = getattr(self, "git_entry_button", None)
        if git_entry is None:
            return
        compact = self.width() < self.WORKBENCH_COMPACT_BREAKPOINT
        git_entry.setText("开发版本树\n主线 · 尝试" if compact else "开发版本树\n查看主线与尝试方向")

    def _adapt_chat_composer_density(self) -> None:
        composer_layout = getattr(self, "chat_composer_layout", None)
        composer_top = getattr(self, "chat_composer_top", None)
        if composer_layout is None or composer_top is None:
            return
        compact = self.width() < self.WORKBENCH_COMPACT_BREAKPOINT
        horizontal_margin = 8 if compact else 12
        composer_layout.setContentsMargins(horizontal_margin, 10, horizontal_margin, 10)
        composer_top.setSpacing(2 if compact else 6)
        if hasattr(self, "summary_chip"):
            self.summary_chip.setText(self._summary_chip_text())
            self.summary_chip.setToolTip(self._summary_status_text())
            self.summary_chip.setAccessibleName(self._summary_status_text())

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        super().resizeEvent(event)
        if hasattr(self, "command_search"):
            self._adapt_title_bar_layout()
        if hasattr(self, "workbench") and not self._workbench_adapt_pending:
            self._workbench_adapt_pending = True
            QTimer.singleShot(0, self._adapt_workbench_layout)
        if hasattr(self, "git_tree_splitter"):
            QTimer.singleShot(0, self._adapt_git_layout)
        if self._active_notification is not None:
            QTimer.singleShot(0, self._position_notification)

    def _adapt_title_bar_layout(self) -> None:
        compact = self.width() < 1120
        if self._titlebar_compact == compact:
            return
        self._titlebar_compact = compact
        self.title_project_label.setVisible(not compact)
        self.command_search.setMinimumWidth(120 if compact else 190)
        self.command_search.setPlaceholderText("Ctrl+P 搜索或跳转" if compact else "搜索文件、命令或跳转到…")
        self.title_new_button.setText("新建" if compact else "新会话")
        self.title_refresh_button.setText("↻" if compact else "刷新")
        self.ui_theme_button.setText("主题")
        self.ui_theme_button.setMinimumWidth(50)
        self.title_new_button.setToolTip("新建编码会话")
        self.title_refresh_button.setToolTip("刷新项目、任务与 Git 状态")
        self.title_git_button.setToolTip("打开开发版本树")

    def _adapt_git_layout(self) -> None:
        if not hasattr(self, "git_tree_splitter"):
            return
        narrow = self.git_tree_splitter.width() < 700
        short_layout = self.height() < 560
        short_layout_changed = self._git_tree_short_layout != short_layout
        self._git_tree_short_layout = short_layout
        self.git_page_header.setFixedHeight(56 if short_layout else 68)
        self.git_metrics_panel.setVisible(not narrow and not short_layout)
        if hasattr(self, "git_page_subtitle"):
            self.git_page_subtitle.setText(
                "主线与尝试方向"
                if narrow
                else "主线与尝试方向 · 非 Git 分支"
            )
        self.git_back_button.setText("返回" if narrow else "返回编码工作区")
        self.git_refresh_button.setText("刷新" if narrow else "刷新树")
        if self.git_init_button.text() in {"初始化", "初始化 Git"}:
            self.git_init_button.setText("初始化" if narrow else "初始化 Git")
        orientation = Qt.Orientation.Vertical if narrow else Qt.Orientation.Horizontal
        orientation_changed = self.git_tree_splitter.orientation() != orientation
        if orientation_changed:
            root = self.git_tree.rootObject()
            if narrow and root is not None:
                self._git_tree_compact_restore = (
                    float(root.property("zoom")),
                    float(root.property("panX")),
                    float(root.property("panY")),
                )
                self._git_tree_compact_auto_view = None
            elif not narrow and root is not None and self._git_tree_compact_restore is not None:
                zoom, pan_x, pan_y = self._git_tree_compact_restore
                root.setProperty("zoom", zoom)
                root.setProperty("panX", pan_x)
                root.setProperty("panY", pan_y)
                self._git_tree_compact_restore = None
                self._git_tree_compact_auto_view = None
            self.git_tree_splitter.setOrientation(orientation)
        # QSplitter swaps its orientation-specific default size policy when it
        # changes direction. Keep the responsive page filling its full width.
        self.git_tree_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        if narrow:
            self.git_tree_splitter.setStretchFactor(0, 1)
            self.git_tree_splitter.setStretchFactor(1, 1)
            self.git_details.setMinimumWidth(0)
            self.git_details.setMaximumWidth(16777215)
            self.git_details_scroll.setMinimumWidth(0)
            self.git_details_scroll.setMaximumWidth(16777215)
            self.git_tree_scroll.setMinimumHeight(0)
            self.git_details_scroll.setMinimumHeight(0)
            if orientation_changed or short_layout_changed:
                self.git_page.layout().activate()
                split_height = max(1, self.git_tree_splitter.height())
                # Keep a useful selected-node summary beside the compact canvas.
                details_ratio = 0.43 if short_layout else 0.42
                self.git_tree_splitter.setSizes(
                    [int(split_height * (1 - details_ratio)), int(split_height * details_ratio)]
                )
        else:
            self.git_tree_splitter.setStretchFactor(0, 1)
            self.git_tree_splitter.setStretchFactor(1, 0)
            self.git_details.setMinimumWidth(260)
            self.git_details.setMaximumWidth(360)
            self.git_details_scroll.setMinimumWidth(260)
            self.git_details_scroll.setMaximumWidth(360)
            self.git_tree_scroll.setMinimumHeight(0)
            self.git_details_scroll.setMinimumHeight(0)
            if orientation_changed:
                self.git_tree_splitter.setSizes([1, 300])
        if narrow:
            QTimer.singleShot(0, self._fit_git_tree_compact_layout)

    def _fit_git_tree_compact_layout(self) -> None:
        if (
            not hasattr(self, "git_tree_splitter")
            or self.git_tree_splitter.orientation() != Qt.Orientation.Vertical
            or self._git_tree_compact_restore is None
        ):
            return
        root = self.git_tree.rootObject()
        if root is None:
            return
        current = (
            float(root.property("zoom")),
            float(root.property("panX")),
            float(root.property("panY")),
        )
        previous_auto = self._git_tree_compact_auto_view
        if previous_auto is not None and any(abs(value - prior) > 0.02 for value, prior in zip(current, previous_auto)):
            # Respect a manual zoom or pan instead of repeatedly snapping it
            # back while the user resizes the compact layout.
            return

        attempts = [node for node in self.git_tree.nodes if node.get("lane") == "attempt"]
        if not attempts:
            return
        cards, _ = self.git_tree._positions()
        first_attempt = cards.get(str(attempts[0].get("id")))
        if first_attempt is None:
            return
        viewport_height = self.git_tree_scroll.viewport().height()
        if viewport_height <= 0:
            return
        base_zoom, pan_x, pan_y = self._git_tree_compact_restore
        available_height = max(1, viewport_height - 8 - pan_y)
        fit_zoom = min(base_zoom, available_height / max(1, first_attempt[2].bottom()))
        fit_zoom = max(0.65, fit_zoom)
        root.setProperty("zoom", fit_zoom)
        root.setProperty("panX", pan_x)
        root.setProperty("panY", pan_y)
        self._git_tree_compact_auto_view = (fit_zoom, pan_x, pan_y)

    def _build_find_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("FindBar")
        bar.setVisible(False)
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 5, 8, 5)
        row.setSpacing(6)

        find_label = QLabel("查找")
        find_label.setObjectName("Overline")
        row.addWidget(find_label)
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("查找当前文件")
        self.find_input.setMinimumWidth(170)
        self.find_input.textChanged.connect(self._on_find_text_changed)
        self.find_input.returnPressed.connect(self._find_next)
        row.addWidget(self.find_input)

        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("替换为")
        self.replace_input.setMinimumWidth(150)
        self.replace_input.returnPressed.connect(self._replace_current)
        self.replace_input.setVisible(False)
        row.addWidget(self.replace_input)

        self.find_case_checkbox = QCheckBox("区分大小写")
        self.find_case_checkbox.toggled.connect(lambda _checked: self._on_find_text_changed(self.find_input.text()))
        row.addWidget(self.find_case_checkbox)
        self.find_word_checkbox = QCheckBox("全字匹配")
        self.find_word_checkbox.toggled.connect(lambda _checked: self._on_find_text_changed(self.find_input.text()))
        row.addWidget(self.find_word_checkbox)

        previous = QToolButton()
        previous.setObjectName("IconButton")
        previous.setText("↑")
        previous.setToolTip("上一个匹配")
        previous.clicked.connect(self._find_previous)
        row.addWidget(previous)
        following = QToolButton()
        following.setObjectName("IconButton")
        following.setText("↓")
        following.setToolTip("下一个匹配")
        following.clicked.connect(self._find_next)
        row.addWidget(following)

        replace = QPushButton("替换")
        replace.setObjectName("Quiet")
        replace.clicked.connect(self._replace_current)
        self.replace_button = replace
        row.addWidget(replace)
        replace_all = QPushButton("全部替换")
        replace_all.setObjectName("Quiet")
        replace_all.clicked.connect(self._replace_all)
        self.replace_all_button = replace_all
        row.addWidget(replace_all)

        self.find_status = QLabel("")
        self.find_status.setObjectName("FindStatus")
        row.addWidget(self.find_status)
        row.addStretch(1)
        close = QToolButton()
        close.setObjectName("IconButton")
        close.setText("×")
        close.setToolTip("关闭查找")
        close.clicked.connect(self._close_find_bar)
        row.addWidget(close)
        return bar

    def _build_bottom_panel(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setObjectName("BottomTabs")
        tabs.setDocumentMode(True)
        tabs.setMaximumHeight(250)
        tabs.setVisible(False)

        search_page = QWidget()
        search_layout = QVBoxLayout(search_page)
        search_layout.setContentsMargins(10, 8, 10, 8)
        search_layout.setSpacing(6)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        self.workspace_search_input = QLineEdit()
        self.workspace_search_input.setPlaceholderText("搜索整个项目中的文本")
        self.workspace_search_input.returnPressed.connect(self._search_workspace)
        search_row.addWidget(self.workspace_search_input, 1)
        search_button = QPushButton("搜索")
        search_button.setObjectName("Primary")
        search_button.clicked.connect(self._search_workspace)
        search_row.addWidget(search_button)
        search_layout.addLayout(search_row)
        self.workspace_search_results = QTreeWidget()
        self.workspace_search_results.setHeaderHidden(True)
        self.workspace_search_results.setRootIsDecorated(False)
        self.workspace_search_results.itemDoubleClicked.connect(self._open_search_result)
        search_layout.addWidget(self.workspace_search_results, 1)
        tabs.addTab(search_page, "搜索")

        terminal_page = QWidget()
        terminal_layout = QVBoxLayout(terminal_page)
        terminal_layout.setContentsMargins(0, 0, 0, 0)
        terminal_layout.setSpacing(0)
        self.terminal_output = QPlainTextEdit()
        self.terminal_output.setObjectName("TerminalOutput")
        self.terminal_output.setReadOnly(True)
        self.terminal_output.setPlaceholderText("在项目目录中运行命令，输出会保留在当前会话")
        terminal_layout.addWidget(self.terminal_output, 1)
        terminal_row = QHBoxLayout()
        terminal_row.setContentsMargins(0, 0, 0, 0)
        terminal_row.setSpacing(0)
        self.terminal_input = QLineEdit()
        self.terminal_input.setObjectName("TerminalInput")
        self.terminal_input.setPlaceholderText("输入命令并按 Enter 运行")
        self.terminal_input.returnPressed.connect(self._run_terminal_command)
        terminal_row.addWidget(self.terminal_input, 1)
        run = QPushButton("运行")
        run.setObjectName("Quiet")
        run.clicked.connect(self._run_terminal_command)
        terminal_row.addWidget(run)
        terminal_layout.addLayout(terminal_row)
        tabs.addTab(terminal_page, "终端")

        problems_page = QWidget()
        problems_layout = QVBoxLayout(problems_page)
        problems_layout.setContentsMargins(0, 0, 0, 0)
        self.problems_tree = QTreeWidget()
        self.problems_tree.setHeaderHidden(True)
        self.problems_tree.setRootIsDecorated(False)
        self.problems_tree.setMaximumHeight(104)
        self.problems_tree.setToolTip("双击问题跳转到文件和行列")
        self.problems_tree.itemDoubleClicked.connect(self._open_problem)
        problems_layout.addWidget(self.problems_tree)
        self.problems_output = QPlainTextEdit()
        self.problems_output.setObjectName("ProblemsOutput")
        self.problems_output.setReadOnly(True)
        self.problems_output.setPlaceholderText("任务、终端和 Git 检查产生的问题会显示在这里")
        problems_layout.addWidget(self.problems_output)
        tabs.addTab(problems_page, "问题")

        outline_page = QWidget()
        outline_layout = QVBoxLayout(outline_page)
        outline_layout.setContentsMargins(10, 8, 10, 8)
        outline_hint = QLabel("当前文件中的类、函数和主要符号")
        outline_hint.setObjectName("Hint")
        outline_layout.addWidget(outline_hint)
        self.outline_tree = QTreeWidget()
        self.outline_tree.setHeaderHidden(True)
        self.outline_tree.setRootIsDecorated(False)
        self.outline_tree.itemDoubleClicked.connect(self._open_outline_result)
        outline_layout.addWidget(self.outline_tree, 1)
        tabs.addTab(outline_page, "大纲")
        return tabs

    def _build_git_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("GitPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("GitPageHeader")
        header.setFixedHeight(68)
        self.git_page_header = header
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 0, 14, 0)
        icon = QLabel("G")
        icon.setObjectName("AgentLogo")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(32, 32)
        header_layout.addWidget(icon)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("开发版本树")
        title.setObjectName("AppTitle")
        subtitle = QLabel("主线与尝试方向 · 非 Git 分支")
        subtitle.setObjectName("Subtle")
        subtitle.setToolTip("记录主线与被取消的尝试方向 · 不等同于 Git 分支")
        self.git_page_subtitle = subtitle
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addLayout(title_box)
        header_layout.addStretch(1)
        back = QPushButton("返回编码工作区")
        back.setObjectName("Quiet")
        back.clicked.connect(self.show_workspace)
        self.git_back_button = back
        header_layout.addWidget(back)
        refresh = QPushButton("刷新树")
        refresh.setObjectName("Quiet")
        refresh.clicked.connect(self._refresh_development_tree)
        self.git_refresh_button = refresh
        header_layout.addWidget(refresh)
        init = QPushButton("初始化 Git")
        init.setObjectName("GitPrimary")
        init.clicked.connect(self.init_git)
        self.git_init_button = init
        header_layout.addWidget(init)
        layout.addWidget(header)

        metrics_panel = QWidget()
        metrics_panel.setObjectName("GitMetricsPanel")
        metrics = QHBoxLayout(metrics_panel)
        metrics.setContentsMargins(18, 12, 18, 12)
        metrics.setSpacing(10)
        self.git_metrics_panel = metrics_panel
        self.git_main_value = self._metric_card(metrics, "主线", "0")
        self.git_attempt_value = self._metric_card(metrics, "尝试", "0")
        self.git_failed_value = self._metric_card(metrics, "失败", "0")
        self.git_head_value = self._metric_card(metrics, "HEAD", "暂无")
        layout.addWidget(metrics_panel)

        tree_splitter = QSplitter(Qt.Orientation.Horizontal)
        tree_splitter.setChildrenCollapsible(False)
        tree_splitter.setHandleWidth(1)
        tree_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.git_tree = DevelopmentTreeView(self.project_root / ".research" / "tree_layout.json", self.ui_profile)
        self.git_tree.node_selected.connect(self._handle_git_node_selected)
        tree_scroll = QScrollArea()
        tree_scroll.setObjectName("TreeScroll")
        tree_scroll.setWidgetResizable(True)
        tree_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tree_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        tree_scroll.setWidget(self.git_tree)
        self.git_tree_scroll = tree_scroll
        tree_splitter.addWidget(tree_scroll)

        details = QFrame()
        details.setObjectName("GitDetails")
        details.setMinimumWidth(260)
        details.setMaximumWidth(360)
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(18, 20, 18, 16)
        detail_layout.setSpacing(9)
        detail_label = QLabel("节点详情")
        detail_label.setObjectName("Overline")
        detail_layout.addWidget(detail_label)
        self.git_detail_title = QLabel("当前主线")
        self.git_detail_title.setObjectName("GitDetailTitle")
        self.git_detail_title.setWordWrap(True)
        detail_layout.addWidget(self.git_detail_title)
        self.git_detail_status = QLabel("当前主线")
        self.git_detail_status.setObjectName("GitDetailStatus")
        detail_layout.addWidget(self.git_detail_status)
        self.git_detail_description = QLabel("这里会显示选中开发节点的说明。")
        self.git_detail_description.setObjectName("Subtle")
        self.git_detail_description.setWordWrap(True)
        self.git_detail_description.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.git_detail_description)
        detail_layout.addSpacing(8)
        meta_label = QLabel("记录")
        meta_label.setObjectName("Overline")
        detail_layout.addWidget(meta_label)
        self.git_detail_meta = QLabel("暂无开发记录")
        self.git_detail_meta.setObjectName("Subtle")
        self.git_detail_meta.setWordWrap(True)
        self.git_detail_meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.git_detail_meta)
        self.git_retry_button = QPushButton("重新尝试这个方向")
        self.git_retry_button.setObjectName("Primary")
        self.git_retry_button.setEnabled(False)
        self.git_retry_button.clicked.connect(self.retry_selected_node)
        detail_layout.addWidget(self.git_retry_button)
        changes_label = QLabel("工作区变更")
        changes_label.setObjectName("Overline")
        detail_layout.addWidget(changes_label)
        self.git_changes_tree = QTreeWidget()
        self.git_changes_tree.setHeaderHidden(True)
        self.git_changes_tree.setRootIsDecorated(False)
        self.git_changes_tree.setMaximumHeight(132)
        self.git_changes_tree.setToolTip("双击文件查看 Git diff")
        self.git_changes_tree.itemDoubleClicked.connect(self._open_git_change)
        detail_layout.addWidget(self.git_changes_tree)
        change_actions = QHBoxLayout()
        change_actions.setContentsMargins(0, 0, 0, 0)
        change_actions.setSpacing(6)
        stage_selected = QPushButton("暂存选中")
        stage_selected.setObjectName("Quiet")
        stage_selected.clicked.connect(self.stage_selected_change)
        change_actions.addWidget(stage_selected)
        unstage_selected = QPushButton("取消暂存")
        unstage_selected.setObjectName("Quiet")
        unstage_selected.clicked.connect(self.unstage_selected_change)
        change_actions.addWidget(unstage_selected)
        stage_all = QPushButton("暂存全部")
        stage_all.setObjectName("Quiet")
        stage_all.clicked.connect(self.stage_all_changes)
        change_actions.addWidget(stage_all)
        detail_layout.addLayout(change_actions)
        workspace_label = QLabel("Git 状态")
        workspace_label.setObjectName("Overline")
        detail_layout.addWidget(workspace_label)
        self.git_status_output = QPlainTextEdit()
        self.git_status_output.setObjectName("ProblemsOutput")
        self.git_status_output.setReadOnly(True)
        self.git_status_output.setMaximumHeight(92)
        detail_layout.addWidget(self.git_status_output)
        self.git_commit_input = QLineEdit()
        self.git_commit_input.setPlaceholderText("提交说明")
        self.git_commit_input.returnPressed.connect(self.commit_workspace)
        detail_layout.addWidget(self.git_commit_input)
        commit = QPushButton("提交当前工作区")
        commit.setObjectName("GitPrimary")
        commit.clicked.connect(self.commit_workspace)
        detail_layout.addWidget(commit)
        detail_layout.addStretch(1)
        note = QLabel("树上的分支是编码过程中的尝试方向。取消或失败的方向会保留在这里，方便以后回看；只有 Agent 完成的修改才会进入主线。")
        note.setObjectName("Hint")
        note.setWordWrap(True)
        detail_layout.addWidget(note)
        details_scroll = QScrollArea()
        details_scroll.setObjectName("GitDetailsScroll")
        details_scroll.setWidgetResizable(True)
        details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        details_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        details_scroll.setWidget(details)
        self.git_details = details
        self.git_details_scroll = details_scroll
        tree_splitter.addWidget(details_scroll)
        tree_splitter.setSizes([1, 300])
        tree_splitter.setStretchFactor(0, 1)
        tree_splitter.setStretchFactor(1, 0)
        self.git_tree_splitter = tree_splitter
        layout.addWidget(tree_splitter, 1)
        return page

    @staticmethod
    def _metric_card(parent_layout: QHBoxLayout, label_text: str, value_text: str) -> QLabel:
        card = QFrame()
        card.setObjectName("MetricCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 9, 12, 9)
        card_layout.setSpacing(2)
        value = QLabel(value_text)
        value.setObjectName("MetricValue")
        card_layout.addWidget(value)
        label = QLabel(label_text)
        label.setObjectName("Subtle")
        card_layout.addWidget(label)
        card.setToolTip(label_text)
        parent_layout.addWidget(card, 1)
        return value

    def _development_nodes(self) -> list[dict[str, Any]]:
        branch = self.git.branch()
        head = self.git.head_sha()
        nodes: list[dict[str, Any]] = [{
            "id": "root",
            "lane": "main",
            "status": "main",
            "title": "当前主线",
            "description": "项目当前持续发展的编码主线。成功完成的编码任务会接在这里。",
            "meta": f"Git {branch} · HEAD {head[:10] if head else '暂无'}",
        }]
        current_main = "root"
        for item in reversed(self.ledger.list_tasks(limit=100)):
            raw_payload = item.get("payload", {})
            if isinstance(raw_payload, str):
                try:
                    raw_payload = json.loads(raw_payload)
                except json.JSONDecodeError:
                    raw_payload = {}
            prompt = str(raw_payload.get("prompt", "")) if isinstance(raw_payload, dict) else ""
            title = prompt.splitlines()[0].strip() or str(item.get("kind", "编码任务"))
            if title.startswith("编码目标："):
                title = title.removeprefix("编码目标：").strip()
            task_id = str(item.get("task_id", new_id("node")))
            status = str(item.get("status", ""))
            meta = f"{item.get('updated_at') or item.get('created_at') or '未知时间'} · {item.get('attempts', 0)} 次尝试"
            if status == "succeeded":
                node = {
                    "id": task_id,
                    "lane": "main",
                    "status": "main",
                    "parent_id": current_main,
                    "title": title,
                    "description": "该编码任务已完成，修改结果进入当前主线。",
                    "meta": meta,
                }
                nodes.append(node)
                current_main = task_id
            else:
                attempt_status = "failed" if status == "failed" else "retry" if status == "retry_wait" else "active"
                if status == "queued":
                    description = "任务已排队，尚未成为主线的一部分。"
                elif status == "running":
                    description = "Agent 正在尝试这个方向。"
                else:
                    description = str(item.get("last_error") or "这个方向没有进入当前主线。")
                nodes.append({
                    "id": task_id,
                    "lane": "attempt",
                    "status": attempt_status,
                    "parent_id": current_main,
                    "title": title,
                    "description": description,
                    "meta": meta,
                })
        return nodes

    def _refresh_development_tree(self) -> None:
        if not hasattr(self, "git_tree"):
            return
        nodes = self._development_nodes()
        self.git_tree.set_nodes(nodes)
        main_count = sum(node.get("lane") == "main" for node in nodes) - 1
        attempts = [node for node in nodes if node.get("lane") == "attempt"]
        failed_count = sum(node.get("status") == "failed" for node in attempts)
        self.git_main_value.setText(str(max(0, main_count)))
        self.git_attempt_value.setText(str(len(attempts)))
        self.git_failed_value.setText(str(failed_count))
        head = self.git.head_sha()
        self.git_head_value.setText(head[:8] if head else "暂无")

    def _handle_git_node_selected(self, node: dict[str, Any]) -> None:
        self.git_detail_title.setText(str(node.get("title", "未命名节点")))
        self.git_detail_status.setText(DevelopmentTreeView._status_text(node))
        self.git_detail_status.setStyleSheet(f"color: {DevelopmentTreeView._accent(node).name()};")
        self.git_detail_description.setText(str(node.get("description", "暂无说明")))
        self.git_detail_meta.setText(str(node.get("meta", "暂无记录")))
        can_retry = node.get("lane") == "attempt" and node.get("status") in {"failed", "retry"}
        self.git_retry_button.setEnabled(can_retry)
        self.git_retry_button.setProperty("task_id", node.get("id", ""))

    def retry_selected_node(self) -> None:
        if self.active_task_id:
            self._append_chat("系统", "当前任务仍在执行，请等待 Agent 完成后再重试其他方向。", "meta")
            return
        task_id = str(self.git_retry_button.property("task_id") or "")
        if not task_id or not self.ledger.retry_now(task_id):
            return
        self.active_task_id = task_id
        self.worker.wake()
        self._set_state(self.chat_status, "排队中 · 正在重新尝试", "StateWorking")
        self._set_state(self.workspace_state, "● 排队中", "StateWorking")
        self._append_chat("系统", f"已重新尝试开发方向：{task_id}", "meta")
        self._refresh_development_tree()

    def _build_chat_panel(self) -> QWidget:
        chat = QFrame()
        chat.setObjectName("ChatPane")
        chat.setMinimumWidth(self.WORKBENCH_COMPACT_MINIMUMS[2])
        chat.setMaximumWidth(500)
        layout = QVBoxLayout(chat)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("ChatHeader")
        header.setFixedHeight(58)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 0, 12, 0)
        agent_logo = QLabel("✦")
        agent_logo.setObjectName("AgentLogo")
        agent_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        agent_logo.setFixedSize(29, 29)
        header_layout.addWidget(agent_logo)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("Codex Agent")
        title.setObjectName("AppTitle")
        self.chat_status = QLabel("就绪 · 等待任务")
        self.chat_status.setObjectName("StateReady")
        title_box.addWidget(title)
        title_box.addWidget(self.chat_status)
        header_layout.addLayout(title_box)
        header_layout.addStretch(1)
        more = QToolButton()
        more.setObjectName("IconButton")
        more.setText("…")
        more.setToolTip("Agent 设置")
        header_layout.addWidget(more)
        more.clicked.connect(self.toggle_summary_settings)
        layout.addWidget(header)

        self.summary_settings_panel = QFrame()
        self.summary_settings_panel.setObjectName("SummarySettings")
        self.summary_settings_panel.setVisible(False)
        settings_layout = QGridLayout(self.summary_settings_panel)
        settings_layout.setContentsMargins(12, 9, 12, 9)
        settings_layout.setHorizontalSpacing(8)
        settings_layout.setVerticalSpacing(6)
        settings_title = QLabel("会话总结")
        settings_title.setObjectName("SummarySettingsTitle")
        settings_layout.addWidget(settings_title, 0, 0, 1, 4)
        self.summary_enabled_checkbox = QCheckBox("每次编码对话结束自动总结")
        self.summary_enabled_checkbox.setChecked(self.summary_settings.enabled)
        self.summary_enabled_checkbox.toggled.connect(lambda _checked: self._persist_summary_settings())
        settings_layout.addWidget(self.summary_enabled_checkbox, 1, 0, 1, 2)
        self.summary_state_label = QLabel(self._summary_status_text())
        self.summary_state_label.setObjectName("SummaryState")
        settings_layout.addWidget(self.summary_state_label, 1, 2, 1, 2, alignment=Qt.AlignmentFlag.AlignRight)
        settings_layout.addWidget(QLabel("模型"), 2, 0)
        self.summary_model_edit = QLineEdit(self.summary_settings.model)
        self.summary_model_edit.setPlaceholderText("跟随主模型")
        self.summary_model_edit.setToolTip("留空表示使用主 Agent 模型")
        self.summary_model_edit.editingFinished.connect(self._persist_summary_settings)
        settings_layout.addWidget(self.summary_model_edit, 2, 1, 1, 3)
        settings_layout.addWidget(QLabel("最大输出"), 3, 0)
        self.summary_tokens_spin = QSpinBox()
        self.summary_tokens_spin.setRange(200, 32000)
        self.summary_tokens_spin.setSingleStep(1000)
        self.summary_tokens_spin.setValue(self.summary_settings.max_tokens)
        self.summary_tokens_spin.setSuffix(" tokens")
        self.summary_tokens_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        settings_layout.addWidget(self.summary_tokens_spin, 3, 1)
        settings_layout.addWidget(QLabel("上下文"), 3, 2)
        self.summary_context_spin = QSpinBox()
        self.summary_context_spin.setRange(4000, 50000)
        self.summary_context_spin.setSingleStep(1000)
        self.summary_context_spin.setValue(self.summary_settings.context_chars)
        self.summary_context_spin.setSuffix(" chars")
        self.summary_context_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        settings_layout.addWidget(self.summary_context_spin, 3, 3)
        settings_layout.addWidget(QLabel("失败重试"), 4, 0)
        self.summary_retry_spin = QSpinBox()
        self.summary_retry_spin.setRange(0, 3)
        self.summary_retry_spin.setValue(self.summary_settings.retries)
        self.summary_retry_spin.setSuffix(" times")
        self.summary_retry_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        settings_layout.addWidget(self.summary_retry_spin, 4, 1)
        summary_note = QLabel("总结失败不会影响代码任务")
        summary_note.setObjectName("Hint")
        settings_layout.addWidget(summary_note, 4, 2, 1, 2)
        settings_layout.addWidget(QLabel("阶段频率"), 5, 0)
        self.summary_interval_spin = QSpinBox()
        self.summary_interval_spin.setRange(0, 32)
        self.summary_interval_spin.setValue(self.summary_settings.interval_turns)
        self.summary_interval_spin.setSuffix(" turns")
        self.summary_interval_spin.setSpecialValueText("仅结束")
        self.summary_interval_spin.setToolTip("每 N 轮 Agent 调用生成一次阶段总结；0 表示只在对话结束时总结")
        self.summary_interval_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        settings_layout.addWidget(self.summary_interval_spin, 5, 1)
        frequency_note = QLabel("0 = 只在结束时总结")
        frequency_note.setObjectName("Hint")
        settings_layout.addWidget(frequency_note, 5, 2, 1, 2)
        settings_layout.addWidget(QLabel("指令"), 6, 0)
        self.summary_instruction_edit = QLineEdit(self.summary_settings.instruction)
        self.summary_instruction_edit.setPlaceholderText("要求总结包含哪些内容")
        self.summary_instruction_edit.editingFinished.connect(self._persist_summary_settings)
        settings_layout.addWidget(self.summary_instruction_edit, 6, 1, 1, 3)
        layout.addWidget(self.summary_settings_panel)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setObjectName("ChatScroll")
        self.chat_scroll.setWidgetResizable(True)
        self.chat_content = QWidget()
        self.chat_content.setObjectName("ChatContent")
        self.chat_layout = QVBoxLayout(self.chat_content)
        self.chat_layout.setContentsMargins(12, 14, 12, 14)
        self.chat_layout.setSpacing(8)
        self.chat_layout.addStretch(1)
        self.chat_scroll.setWidget(self.chat_content)
        layout.addWidget(self.chat_scroll, 1)

        composer = QFrame()
        composer.setObjectName("Composer")
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(12, 10, 12, 10)
        composer_layout.setSpacing(7)
        composer_top = QHBoxLayout()
        self.chat_composer_layout = composer_layout
        self.chat_composer_top = composer_top
        self.chat_mode_chip = QLabel("Agent")
        self.chat_mode_chip.setObjectName("Chip")
        composer_top.addWidget(self.chat_mode_chip)
        self.retry_chip = QLabel("重试")
        self.retry_chip.setObjectName("Chip")
        self.retry_chip.setAccessibleName("自动重试")
        self.retry_chip.setToolTip("网络请求失败后自动排队并重试")
        composer_top.addWidget(self.retry_chip)
        self.summary_chip = QLabel(self._summary_chip_text())
        self.summary_chip.setObjectName("Chip")
        self.summary_chip.setToolTip(self._summary_status_text())
        self.summary_chip.setAccessibleName(self._summary_status_text())
        composer_top.addWidget(self.summary_chip)
        context_button = QToolButton()
        context_button.setObjectName("IconButton")
        context_button.setText("@")
        context_button.setToolTip("引用当前文件或选中代码")
        context_button.clicked.connect(self._insert_current_file_context)
        composer_top.addWidget(context_button)
        composer_top.addStretch(1)
        composer_layout.addLayout(composer_top)

        self.chat_input = PromptEdit()
        self.chat_input.setObjectName("ChatInput")
        self.chat_input.setPlaceholderText("告诉 Agent 你想修改什么…")
        self.chat_input.setFixedHeight(76)
        self.chat_input.send_requested.connect(self.submit_chat)
        composer_layout.addWidget(self.chat_input)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("Ctrl + Enter 发送")
        hint.setObjectName("Hint")
        bottom.addWidget(hint)
        bottom.addStretch(1)
        send = QPushButton("发送")
        send.setObjectName("Primary")
        send.clicked.connect(self.submit_chat)
        bottom.addWidget(send)
        composer_layout.addLayout(bottom)
        layout.addWidget(composer)
        return chat

    def _summary_status_text(self) -> str:
        settings = getattr(self, "summary_settings", SummarySettings())
        if not settings.enabled:
            return "总结已关闭"
        return f"总结已开启 · 每{settings.interval_turns}轮" if settings.interval_turns else "总结已开启 · 仅结束"

    def _summary_chip_text(self) -> str:
        settings = getattr(self, "summary_settings", SummarySettings())
        compact = self.width() < self.WORKBENCH_COMPACT_BREAKPOINT
        if not settings.enabled:
            return "关" if compact else "总结关"
        if settings.interval_turns:
            return f"{settings.interval_turns}轮" if compact else f"总结 {settings.interval_turns}轮"
        return "结束" if compact else "总结结束"

    def toggle_summary_settings(self) -> None:
        visible = not self.summary_settings_panel.isVisible()
        self.summary_settings_panel.setVisible(visible)
        if visible:
            self.summary_instruction_edit.setFocus()

    def _persist_summary_settings(self) -> None:
        if not hasattr(self, "summary_enabled_checkbox"):
            return
        self.summary_settings.enabled = self.summary_enabled_checkbox.isChecked()
        self.summary_settings.model = self.summary_model_edit.text().strip()
        self.summary_settings.max_tokens = self.summary_tokens_spin.value()
        self.summary_settings.context_chars = self.summary_context_spin.value()
        self.summary_settings.retries = self.summary_retry_spin.value()
        self.summary_settings.interval_turns = self.summary_interval_spin.value()
        self.summary_settings.instruction = self.summary_instruction_edit.text().strip() or SummarySettings.instruction
        try:
            self.summary_settings.save(self.project_root)
            self.agent.summary_settings = self.summary_settings
            state = self._summary_status_text()
            self.summary_state_label.setText(state)
            self.summary_chip.setText(self._summary_chip_text())
            self.summary_chip.setToolTip(state)
            self.summary_chip.setAccessibleName(state)
        except OSError as exc:
            self._append_log(f"总结设置保存失败: {exc}")

    def _provider_state(self) -> str:
        key = os.getenv("SCIDEV_API_KEY") or os.getenv("OPENAI_API_KEY")
        model = os.getenv("SCIDEV_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5"
        return f"● {model}" if key else "● LLM 未配置"

    def _emit_agent(self, kind: str, data: dict[str, Any]) -> None:
        if not self._closing:
            self.signals.agent_event.emit(kind, data)

    def _request_command_approval(self, command: str, timeout_seconds: int) -> bool:
        if self._closing:
            return False
        decision_ready = threading.Event()
        request = {
            "command": command,
            "timeout_seconds": timeout_seconds,
            "decision_ready": decision_ready,
            "approved": False,
        }
        self.signals.command_approval_requested.emit(request)
        decision_ready.wait()
        return bool(request["approved"])

    def _handle_command_approval_request(self, request: dict[str, Any]) -> None:
        decision_ready = request["decision_ready"]
        try:
            if self._closing:
                return
            command = str(request.get("command") or "")
            timeout_seconds = int(request.get("timeout_seconds") or 120)
            dialog = QMessageBox(self)
            dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
            dialog.setWindowTitle("安全确认")
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setTextFormat(Qt.TextFormat.PlainText)
            dialog.setText("允许 Agent 执行这条终端命令？")
            dialog.setInformativeText(
                f"工作目录：{self.project_root}\n"
                f"超时上限：{timeout_seconds} 秒。命令将以当前 Windows 用户权限执行。\n"
                "展开详细信息可检查完整命令。"
            )
            dialog.setDetailedText(command)
            dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            dialog.setButtonText(QMessageBox.StandardButton.Yes, "允许这一次")
            dialog.setButtonText(QMessageBox.StandardButton.No, "拒绝")
            dialog.setDefaultButton(QMessageBox.StandardButton.No)
            approved = dialog.exec() == QMessageBox.StandardButton.Yes
            request["approved"] = approved
            self._append_log("已批准 Agent 命令" if approved else "已拒绝 Agent 命令")
        finally:
            decision_ready.set()

    def _emit_worker(self, kind: str, data: dict[str, Any]) -> None:
        if not self._closing:
            self.signals.worker_event.emit(kind, data)

    def _handle_worker_event(self, event: str, payload: dict[str, Any]) -> None:
        task_id = payload.get("task_id", "")
        if event == "task_started" and task_id == self.active_task_id:
            self._set_state(self.chat_status, "工作中 · Agent 正在修改项目", "StateWorking")
            self._set_state(self.workspace_state, "● 工作中", "StateWorking")
        elif event == "task_retry" and task_id == self.active_task_id:
            self._interrupt_streaming_bubbles("Agent · 网络中断，等待自动重试")
            self._set_state(self.chat_status, "等待网络 · 自动重试中", "StateWorking")
            self._set_state(self.workspace_state, "● 重试中", "StateWorking")
            self._refresh_open_file_after_task()
        elif event == "task_completed" and task_id == self.active_task_id:
            self.active_task_id = None
            self._set_state(self.chat_status, "就绪 · 可以继续对话", "StateReady")
            self._set_state(self.workspace_state, "● 就绪", "StateReady")
        elif event == "task_failed" and task_id == self.active_task_id:
            self._interrupt_streaming_bubbles("Agent · 响应中断")
            self.active_task_id = None
            self._set_state(self.chat_status, "任务失败 · 可从历史重试", "StateError")
            self._set_state(self.workspace_state, "● 出错", "StateError")
            self.problems_output.setPlainText(str(payload.get("error") or "任务失败，未返回详细信息"))
            self._refresh_open_file_after_task()
        self._append_log(self._worker_message(event, payload))
        self.refresh_git_status()
        self.refresh_task_history()
        self._refresh_development_tree()

    @staticmethod
    def _set_state(widget: QLabel, text: str, name: str) -> None:
        widget.setText(text)
        widget.setObjectName(name)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    @staticmethod
    def _tool_event_speaker(payload: dict[str, Any]) -> str:
        if payload.get("source") == "harness_precommit":
            return "Harness · 提交前 diff"
        return f"工具 · {payload.get('name', '')}"

    def _handle_agent_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "summary_started":
            turn = payload.get("turn")
            label = f"第 {turn} 轮阶段总结" if payload.get("phase") == "checkpoint" and turn else "会话总结"
            self._append_chat(label, "正在整理本次对话、修改和验证结果…", "meta")
            self._append_log(f"正在生成{label}")
        elif event == "summary_completed":
            turn = payload.get("turn")
            label = f"第 {turn} 轮阶段总结" if payload.get("phase") == "checkpoint" and turn else "会话总结"
            self._append_chat(label, str(payload.get("text", "")), "summary")
            self._append_log(f"{label}已保存: {payload.get('path', '')}")
        elif event == "summary_failed":
            turn = payload.get("turn")
            label = f"第 {turn} 轮阶段总结" if payload.get("phase") == "checkpoint" and turn else "会话总结"
            self._append_chat(label, f"总结生成失败，但代码任务仍会继续：{payload.get('error', '')}", "meta")
            self._append_log(f"{label}失败，未影响代码任务")
        if event == "model_call_started":
            self._append_log(f"正在请求模型 · 第 {payload['turn']} 轮")
        elif event == "svg_creation_retry_scheduled":
            self._append_chat(
                "Harness · SVG 生成补救",
                "模型上一轮没有写出 SVG 文件，Harness 正在自动补问一次。",
                "meta",
            )
            self._append_log("模型未生成 SVG 文件，已安排一次自动补救")
        elif event == "assistant_delta":
            self._append_assistant_delta(payload)
        elif event == "assistant":
            self._finish_streamed_assistant(payload)
        elif event == "tool_started":
            args = json.dumps(payload.get("arguments", {}), ensure_ascii=False)
            self._append_chat(self._tool_event_speaker(payload), args, "tool")
        elif event == "tool_result":
            result = str(payload.get("result", ""))
            self._append_chat(self._tool_event_speaker(payload), result[-1400:], "tool")
        elif event == "git_auto_commit_skipped_paths":
            self._append_chat(
                "Harness · Git 自动提交保护",
                str(payload.get("message", "部分文件未自动提交，请在 Git 面板审核。")),
                "meta",
            )
            self._append_log("部分文件受 Git 自动提交安全策略保护，仍保留在工作区")
        elif event == "completed":
            sha = payload.get("git_result_sha") or "无新提交"
            self._append_chat("系统", f"任务完成 · Git commit: {sha[:12]}", "meta")
            self._refresh_open_file_after_task()
            self.refresh_git_status()
            self._refresh_development_tree()

    def _append_assistant_delta(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text", ""))
        if not text:
            return
        key = (str(payload.get("session_id", "")), int(payload.get("turn", 0)))
        bubble = self._streaming_bubbles.get(key)
        if bubble is None:
            bubble = MessageBubble("Agent · 正在生成", "", "agent")
            self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
            self._streaming_bubbles[key] = bubble
        bubble.append_message(text)
        QTimer.singleShot(0, lambda: self.chat_scroll.verticalScrollBar().setValue(self.chat_scroll.verticalScrollBar().maximum()))

    def _finish_streamed_assistant(self, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id", ""))
        try:
            turn = int(payload.get("turn", 0))
        except (TypeError, ValueError):
            turn = 0
        bubble = self._streaming_bubbles.pop((session_id, turn), None)
        text = str(payload.get("text", ""))
        if bubble is None:
            self._append_chat("Agent", text, "agent")
            return
        bubble.set_speaker("Agent")
        bubble.set_message(text)
        QTimer.singleShot(0, lambda: self.chat_scroll.verticalScrollBar().setValue(self.chat_scroll.verticalScrollBar().maximum()))

    def _interrupt_streaming_bubbles(self, speaker: str) -> None:
        for bubble in self._streaming_bubbles.values():
            bubble.set_speaker(speaker)
        self._streaming_bubbles.clear()

    @staticmethod
    def _worker_message(event: str, payload: dict[str, Any]) -> str:
        task_id = payload.get("task_id", "")
        if event == "task_retry":
            return f"{task_id} · 网络暂时不可用，已安排定时重试"
        if event == "task_failed":
            return f"{task_id} · 任务失败：{payload.get('error', '')}"
        if event == "task_completed":
            return f"{task_id} · 任务完成"
        if event == "task_started":
            return f"{task_id} · Agent 开始工作（第 {payload.get('attempts', 1)} 次尝试）"
        return f"{task_id} · {event}"

    def _append_log(self, message: str) -> None:
        self.statusBar().showMessage(message[:220])

    def _append_chat(self, speaker: str, message: str, kind: str) -> None:
        bubble = MessageBubble(speaker, message, kind)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        QTimer.singleShot(0, lambda: self.chat_scroll.verticalScrollBar().setValue(self.chat_scroll.verticalScrollBar().maximum()))

    def _clear_chat_history(self) -> None:
        while self.chat_layout.count() > 1:
            item = self.chat_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _set_activity_button(self, index: int) -> None:
        buttons = getattr(self, "activity_buttons", [])
        if 0 <= index < len(buttons):
            buttons[index].setChecked(True)

    def refresh_all(self) -> None:
        self.refresh_git_status()
        self.connection_label.setText(self._provider_state())
        self.refresh_task_history()
        self._refresh_development_tree()

    def refresh_git_status(self) -> None:
        raw_status = self.git.status()
        if hasattr(self, "git_status_output"):
            self.git_status_output.setPlainText(raw_status)
        self._refresh_git_changes()
        if hasattr(self, "git_init_button"):
            git_ready = self.git.is_repo()
            git_available = not raw_status.startswith("Git 不可用")
            self.git_init_button.setText(
                "Git 已初始化" if git_ready else ("需安装 Git" if not git_available else "初始化 Git")
            )
            self.git_init_button.setEnabled(not git_ready and git_available)
        if raw_status in {"工作区干净", "未初始化 Git 仓库"} or raw_status.startswith("Git 不可用"):
            status = raw_status
        else:
            change_count = len([line for line in raw_status.splitlines() if line.strip()])
            status = f"{change_count} 个工作区变更"
        self.status_branch.setText(f"Git · {status}")
        self.status_branch.setToolTip(raw_status)

    def _refresh_git_changes(self) -> None:
        if not hasattr(self, "git_changes_tree"):
            return
        selected_path = None
        selected_item = self.git_changes_tree.currentItem()
        if selected_item is not None:
            selected_path = selected_item.data(0, Qt.ItemDataRole.UserRole)
        self.git_changes_tree.clear()
        item_to_restore = None
        for entry in self.git.status_entries():
            relative = str(entry.get("path", "")).strip('"')
            code = str(entry.get("code", "  "))
            if not relative:
                continue
            item = QTreeWidgetItem([f"{code}  {relative}"])
            item.setData(0, Qt.ItemDataRole.UserRole, relative)
            item.setToolTip(0, "双击查看 diff；可用下方按钮管理暂存区")
            self.git_changes_tree.addTopLevelItem(item)
            if selected_path == relative:
                item_to_restore = item
        if self.git_changes_tree.topLevelItemCount() == 0:
            empty = QTreeWidgetItem(["工作区干净"])
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.git_changes_tree.addTopLevelItem(empty)
        elif item_to_restore is not None:
            self.git_changes_tree.setCurrentItem(item_to_restore)

    def _selected_git_change_path(self) -> Path | None:
        item = self.git_changes_tree.currentItem() if hasattr(self, "git_changes_tree") else None
        raw_path = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not raw_path:
            return None
        target = (self.project_root / str(raw_path)).resolve()
        return target if target.is_relative_to(self.project_root) else None

    def _open_git_change(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        raw_path = item.data(0, Qt.ItemDataRole.UserRole)
        if not raw_path:
            return
        target = (self.project_root / str(raw_path)).resolve()
        if target.exists() and target.is_file():
            self._open_file(target, preview=False)
        self._show_diff_for_path(target)

    def stage_selected_change(self) -> None:
        target = self._selected_git_change_path()
        if target is None:
            self._append_log("请先选择一个工作区变更")
            return
        try:
            self.git.stage_path(target)
            self.refresh_git_status()
            self._append_log(f"已暂存 {target.relative_to(self.project_root).as_posix()}")
        except (OSError, ValueError, RuntimeError) as exc:
            self.problems_output.setPlainText(str(exc))
            self._append_log(f"暂存失败: {exc}")

    def unstage_selected_change(self) -> None:
        target = self._selected_git_change_path()
        if target is None:
            self._append_log("请先选择一个工作区变更")
            return
        try:
            self.git.unstage_path(target)
            self.refresh_git_status()
            self._append_log(f"已取消暂存 {target.relative_to(self.project_root).as_posix()}")
        except (OSError, ValueError, RuntimeError) as exc:
            self.problems_output.setPlainText(str(exc))
            self._append_log(f"取消暂存失败: {exc}")

    def stage_all_changes(self) -> None:
        try:
            self.git.stage_all()
            self.refresh_git_status()
            self._append_log("已暂存全部工作区变更")
        except (OSError, RuntimeError) as exc:
            self.problems_output.setPlainText(str(exc))
            self._append_log(f"暂存失败: {exc}")

    def _show_diff_for_path(self, path: Path) -> None:
        path = path.resolve()
        if not path.is_relative_to(self.project_root):
            self._append_log("无法查看项目目录之外的 diff")
            return
        diff_text = self.git.diff(path)
        if not diff_text:
            self._append_log(f"{path.name} 没有未提交的差异")
            return
        title = f"差异 · {path.name}"
        diff_editor = next(
            (candidate for candidate, label in self._editor_titles.items() if label == title),
            None,
        )
        if diff_editor is None:
            diff_editor = CodeEditor()
            self._configure_editor(diff_editor)
            diff_editor.setReadOnly(True)
            diff_editor.setProperty("diff_path", str(path))
            self._editor_titles[diff_editor] = title
            group = self._active_editor_group or self.editor_tabs
            index = group.addTab(diff_editor, title)
            self._install_tab_close_button(group, index, diff_editor)
            group.setTabToolTip(index, f"Git diff · {path.as_posix()}")
        diff_editor.setPlainText(diff_text)
        diff_editor.document().setModified(False)
        group = self._editor_group_for(diff_editor) or self._active_editor_group or self.editor_tabs
        self._active_editor_group = group
        group.setCurrentWidget(diff_editor)
        self._append_log(f"已打开 {path.name} 的 Git diff")

    def commit_workspace(self) -> None:
        message = self.git_commit_input.text().strip()
        if not message:
            self.git_commit_input.setFocus()
            self._append_log("请填写提交说明")
            return
        try:
            sha = self.git.commit_changes(message)
            self.git_commit_input.clear()
            self._append_log(f"已创建 Git 提交 {sha[:12] if sha else '无变更'}")
            self.refresh_git_status()
            self._refresh_development_tree()
        except Exception as exc:  # noqa: BLE001
            self.problems_output.setPlainText(str(exc))
            self._append_log(f"Git 提交失败: {exc}")

    def _configure_editor(self, editor: CodeEditor) -> None:
        editor.set_ui_profile(self.ui_profile)
        self._editor_highlighters[editor] = PythonHighlighter(editor.document(), self.ui_profile)
        editor.document().modificationChanged.connect(
            lambda _changed, target=editor: self._update_editor_tab_for(target)
        )
        editor.cursorPositionChanged.connect(
            lambda target=editor: self._update_cursor_status(target)
        )
        editor.textChanged.connect(self._refresh_outline)
        editor.textChanged.connect(self._refresh_diagnostics)
        editor.installEventFilter(self)

    def _update_cursor_status(self, editor: CodeEditor | None = None) -> None:
        if not hasattr(self, "status_position"):
            return
        target = editor or self._active_editor()
        if target is None:
            self.status_position.setText("Ln 1, Col 1")
            return
        cursor = target.textCursor()
        self.status_position.setText(f"Ln {cursor.blockNumber() + 1}, Col {cursor.columnNumber() + 1}")
        path = self._editor_paths.get(target)
        if path is None and target.property("diff_path"):
            path = Path(str(target.property("diff_path")))
        if hasattr(self, "status_language"):
            suffix = path.suffix.lower() if path is not None else ""
            language = {
                ".py": "Python",
                ".js": "JavaScript",
                ".ts": "TypeScript",
                ".tsx": "TypeScript React",
                ".json": "JSON",
                ".md": "Markdown",
                ".html": "HTML",
                ".css": "CSS",
            }.get(suffix, "Plain Text")
            self.status_language.setText(language)

    def _active_editor(self) -> CodeEditor | None:
        group = getattr(self, "_active_editor_group", None) or getattr(self, "editor_tabs", None)
        editor = group.currentWidget() if group is not None else None
        return editor if isinstance(editor, CodeEditor) else None

    def _on_editor_tab_changed(self, index: int, group: QTabWidget | None = None) -> None:
        group = group or self._active_editor_group or self.editor_tabs
        self._active_editor_group = group
        widget = group.widget(index) if index >= 0 else None
        if isinstance(widget, CodeEditor):
            self.code_editor = widget
        path = self._editor_paths.get(widget) if widget is not None else None
        if path is None and widget is not None and widget.property("diff_path"):
            path = Path(str(widget.property("diff_path")))
        self.current_file = path
        if path is not None:
            relative = path.relative_to(self.project_root).as_posix()
            self.breadcrumb.setText(f"项目  /  {relative}")
        else:
            self.breadcrumb.setText("项目  /  欢迎页")
        self._update_editor_tab_for(widget)
        self._update_cursor_status(widget if isinstance(widget, CodeEditor) else None)
        self._refresh_outline()
        self._refresh_diagnostics()

    def _update_editor_tab_for(self, editor: QWidget | None = None) -> None:
        if not hasattr(self, "editor_tabs"):
            return
        active_group = getattr(self, "_active_editor_group", None) or self.editor_tabs
        target = editor or active_group.currentWidget()
        if target is None:
            return
        group = self._editor_group_for(target)
        if group is None:
            return
        index = group.indexOf(target)
        path = self._editor_paths.get(target)
        name = path.name if path is not None else self._editor_titles.get(target, "欢迎页")
        if isinstance(target, QPlainTextEdit) and target.document().isModified() and path is not None:
            name = "● " + name
        group.setTabText(index, name)

    def _find_editor(self, path: Path, group: QTabWidget | None = None) -> QWidget | None:
        target = path.resolve()
        if group is not None:
            editors = [group.widget(index) for index in range(group.count())]
            for editor in editors:
                if self._editor_paths.get(editor) == target:
                    return editor
        else:
            for editor, editor_path in self._editor_paths.items():
                if editor_path == target:
                    return editor
        return None

    def _show_editor_tab_menu(self, point: QPoint, group: QTabWidget | None = None) -> None:
        group = group or self._active_editor_group
        if group is None:
            return
        bar = group.tabBar()
        index = bar.tabAt(point)
        if index < 0:
            return
        widget = group.widget(index)
        menu = QMenu(self)
        save = menu.addAction("保存")
        save.setEnabled(widget in self._editor_paths and isinstance(widget, CodeEditor) and widget.document().isModified())
        save.triggered.connect(lambda: self._save_editor(widget))
        menu.addSeparator()
        close = menu.addAction("关闭")
        close.triggered.connect(lambda: self._close_editor_tab(index, group))
        close_others = menu.addAction("关闭其他标签")
        close_others.triggered.connect(lambda: self._close_other_editor_tabs(index, group))
        path = self._editor_paths.get(widget)
        open_side = menu.addAction("在右侧编辑器组打开")
        open_side.setEnabled(path is not None)
        open_side.triggered.connect(lambda: self._open_editor_to_side(path))
        menu.exec(bar.mapToGlobal(point))

    def _save_editor(self, editor: QWidget | None) -> None:
        group = self._editor_group_for(editor)
        if editor is None or group is None:
            self._append_log("当前没有可保存的编辑器")
            return
        self._active_editor_group = group
        if group.currentWidget() is not editor:
            group.setCurrentWidget(editor)
        self.save_current_file()

    def _close_other_editor_tabs(self, keep_index: int, group: QTabWidget | None = None) -> None:
        group = group or self._active_editor_group
        if group is None:
            return
        for index in range(group.count() - 1, -1, -1):
            if index != keep_index:
                self._close_editor_tab(index, group)

    def _open_editor_to_side(self, path: Path | None) -> None:
        if path is None:
            return
        if self.secondary_editor_tabs is None:
            self.secondary_editor_tabs = self._create_editor_group()
            self.editor_splitter.addWidget(self.secondary_editor_tabs)
            self.editor_splitter.setSizes([1, 1])
        self.secondary_editor_tabs.setVisible(True)
        self._open_file(path, preview=False, group=self.secondary_editor_tabs)

    def _current_editor_location(self) -> tuple[Path, int, int] | None:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if editor is None or path is None:
            return None
        cursor = editor.textCursor()
        return path, cursor.blockNumber() + 1, cursor.columnNumber() + 1

    @staticmethod
    def _symbol_at_cursor(editor: CodeEditor | None) -> str | None:
        if editor is None:
            return None
        line = editor.textCursor().block().text()
        position = editor.textCursor().positionInBlock()
        for match in re.finditer(r"\b[A-Za-z_]\w*\b", line):
            if match.start() <= position <= match.end():
                return match.group(0)
        return None

    def _remember_navigation(self) -> None:
        location = self._current_editor_location()
        if location is None:
            return
        if not self._navigation_back or self._navigation_back[-1] != location:
            self._navigation_back.append(location)
            self._navigation_back = self._navigation_back[-100:]
        self._navigation_forward.clear()

    def _open_navigation_location(self, location: tuple[Path, int, int]) -> None:
        path, line_number, column_number = location
        if not path.exists():
            self._append_log(f"导航目标不存在：{path.name}")
            return
        self._open_file(path, preview=False)
        editor = self._active_editor()
        if editor is not None:
            self._move_editor_to_location(editor, line_number, column_number)
            editor.setFocus()

    def _find_symbol_definitions(self, symbol: str) -> list[tuple[Path, int, int]]:
        definitions: list[tuple[Path, int, int]] = []
        pattern = re.compile(
            rf"^\s*(?:(?:async\s+)?def|class)\s+{re.escape(symbol)}\b|^\s*{re.escape(symbol)}\s*="
        )
        for path in self._project_files():
            if path.suffix.lower() != ".py":
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line_number, line in enumerate(lines, start=1):
                if pattern.search(line):
                    match = re.search(rf"\b{re.escape(symbol)}\b", line)
                    definitions.append((path, line_number, (match.start() + 1) if match else 1))
        current = self._current_editor_location()
        if current is not None:
            current_path = current[0]
            definitions.sort(key=lambda item: (0 if item[0] == current_path else 1, str(item[0])))
        return definitions

    def _go_to_definition(self) -> None:
        symbol = self._symbol_at_cursor(self._active_editor())
        if not symbol:
            self._append_log("光标处没有可导航的符号")
            return
        definitions = self._find_symbol_definitions(symbol)
        if not definitions:
            self._append_log(f"没有找到 {symbol} 的定义")
            return
        current = self._current_editor_location()
        target = definitions[0]
        if current is not None and len(definitions) > 1 and target[0] == current[0] and target[1] == current[1]:
            target = definitions[1]
        if current is not None:
            self._remember_navigation()
        self._open_navigation_location(target)
        self._append_log(f"已跳转到 {symbol} 的定义")

    def _find_symbol_references(self) -> None:
        symbol = self._symbol_at_cursor(self._active_editor())
        if not symbol:
            self._append_log("光标处没有可搜索的符号")
            return
        self._workspace_search_whole_word = True
        self.workspace_search_input.setText(symbol)
        self._search_workspace()
        self._append_log(f"已找到 {symbol} 的引用")

    def _navigate_back(self) -> None:
        if not self._navigation_back:
            self._append_log("没有可返回的导航位置")
            return
        current = self._current_editor_location()
        target = self._navigation_back.pop()
        if current is not None:
            self._navigation_forward.append(current)
        self._open_navigation_location(target)

    def _navigate_forward(self) -> None:
        if not self._navigation_forward:
            self._append_log("没有可前进的导航位置")
            return
        current = self._current_editor_location()
        target = self._navigation_forward.pop()
        if current is not None:
            self._navigation_back.append(current)
        self._open_navigation_location(target)

    def _rename_current_symbol(self) -> None:
        editor = self._active_editor()
        symbol = self._symbol_at_cursor(editor)
        if editor is None or symbol is None:
            self._append_log("光标处没有可重命名的符号")
            return
        replacement, accepted = QInputDialog.getText(self, "重命名符号", f"在当前文件中重命名 {symbol}：", QLineEdit.EchoMode.Normal, symbol)
        replacement = replacement.strip()
        if not accepted or replacement == symbol:
            return
        if not re.fullmatch(r"[A-Za-z_]\w*", replacement):
            self._append_log("符号名称必须是合法的 Python 标识符")
            return
        cursor = editor.textCursor()
        position = cursor.position()
        updated = re.sub(rf"\b{re.escape(symbol)}\b", replacement, editor.toPlainText())
        editor.setPlainText(updated)
        cursor = editor.textCursor()
        cursor.setPosition(min(position, len(updated)))
        editor.setTextCursor(cursor)
        self._append_log(f"已在当前文件中重命名 {symbol} → {replacement}，请保存")

    def _format_current_document(self) -> None:
        editor = self._active_editor()
        if editor is None or editor.isReadOnly():
            self._append_log("当前没有可格式化的编辑器")
            return
        original = editor.toPlainText()
        formatted_lines = [line.rstrip() for line in original.splitlines()]
        formatted = "\n".join(formatted_lines)
        if formatted and not formatted.endswith("\n"):
            formatted += "\n"
        if formatted == original:
            self._append_log("当前文档无需格式化")
            return
        cursor_position = editor.textCursor().position()
        editor.setPlainText(formatted)
        cursor = editor.textCursor()
        cursor.setPosition(min(cursor_position, len(formatted)))
        editor.setTextCursor(cursor)
        self._append_log("已清理行尾空格并统一文件末尾换行，请保存")

    def _close_editor_tab(self, index: int, group: QTabWidget | None = None) -> None:
        group = group or self._active_editor_group
        if group is None:
            return
        widget = group.widget(index)
        if not isinstance(widget, CodeEditor):
            return
        path = self._editor_paths.get(widget)
        if path is not None and widget.document().isModified():
            self._append_log(f"{path.name} 有未保存修改，请先按 Ctrl+S")
            return
        if widget is self.welcome_editor:
            self._append_log("欢迎页不能关闭")
            return
        if widget is self._preview_editor:
            self._preview_editor = None
        self._editor_paths.pop(widget, None)
        self._editor_titles.pop(widget, None)
        self._editor_highlighters.pop(widget, None)
        self._pinned_editors.discard(widget)
        group.removeTab(index)
        widget.deleteLater()

    def _on_file_preview(self, index: QModelIndex) -> None:
        source_index = self.file_proxy.mapToSource(index)
        path = Path(self.file_model.filePath(source_index))
        if path.is_file():
            self._open_file(path, preview=True)

    def _on_file_selected(self, index: QModelIndex) -> None:
        source_index = self.file_proxy.mapToSource(index)
        path = Path(self.file_model.filePath(source_index))
        if path.is_file():
            self._open_file(path, preview=False)

    def _open_file(self, path: Path, preview: bool = False, group: QTabWidget | None = None) -> None:
        try:
            path = path.resolve()
            group = group or self._active_editor_group
            if group is None:
                group = self.editor_tabs
            if not path.is_relative_to(self.project_root):
                self._append_log("拒绝打开项目目录之外的文件")
                return
            raw = path.read_bytes()
            if len(raw) > CodingToolbox.MAX_READ_BYTES or b"\x00" in raw:
                self._append_log(f"无法打开 {path.name}：文件过大或为二进制文件")
                return
            editor = self._find_editor(path, group)
            if editor is None:
                if preview and self._preview_editor is not None:
                    candidate = self._preview_editor
                    candidate_group = self._editor_group_for(candidate)
                    if candidate_group is group and isinstance(candidate, CodeEditor) and not candidate.document().isModified():
                        self._editor_paths.pop(candidate, None)
                        editor = candidate
                        self._editor_paths[editor] = path
                        group.setTabToolTip(group.indexOf(editor), path.as_posix())
                    else:
                        editor = None
                if editor is None:
                    editor = CodeEditor()
                    self._configure_editor(editor)
                    self._editor_paths[editor] = path
                    tab_index = group.addTab(editor, path.name)
                    self._install_tab_close_button(group, tab_index, editor)
                    group.setTabToolTip(tab_index, path.as_posix())
                editor.setPlainText(raw.decode("utf-8", errors="replace"))
                editor.document().setModified(False)
            self._active_editor_group = group
            index = group.indexOf(editor)
            group.setCurrentIndex(index)
            if preview:
                self._preview_editor = editor
            else:
                self._pinned_editors.add(editor)
                if editor is self._preview_editor:
                    self._preview_editor = None
            self.code_editor = editor
            self.current_file = path
            relative = path.relative_to(self.project_root).as_posix()
            self._update_editor_tab_for(editor)
            self.breadcrumb.setText(f"项目  /  {relative}")
            self._refresh_outline()
            self._append_log(f"{'预览' if preview else '已打开'} {relative}")
        except OSError as exc:
            self._append_log(f"读取失败：{exc}")

    def _show_welcome(self) -> None:
        self.current_file = None
        if hasattr(self, "editor_tabs") and self.welcome_editor is not None:
            # The welcome page belongs to the primary editor group. Explicitly
            # activate it so a secondary group's file is never overwritten
            # when starting a new coding session.
            self._active_editor_group = self.editor_tabs
            self.editor_tabs.setCurrentWidget(self.welcome_editor)
            self.code_editor = self.welcome_editor
            self.editor_tabs.setTabText(self.editor_tabs.indexOf(self.welcome_editor), "欢迎页")
        if hasattr(self, "breadcrumb"):
            self.breadcrumb.setText("项目  /  欢迎页")
        if self.welcome_editor is not None:
            self._show_editor_text(
                "SciDevHarness\n"
                "──────────────────────\n\n"
                "在左侧打开文件开始编辑，或在右侧直接输入指令。\n\n"
                "Agent 会读取项目并通过工具修改代码；网络不稳定时，任务会自动排队重试。\n\n"
                "Ctrl + P 打开文件\n"
                "Ctrl + Enter 发送任务  ·  Ctrl + S 保存"
            )

    def _show_editor_text(self, text: str) -> None:
        editor = self._active_editor() or self.code_editor
        editor.setPlainText(text)
        editor.document().setModified(False)

    def _update_editor_tab(self) -> None:
        self._update_editor_tab_for(self._active_editor())

    def _update_command_suggestions(self, text: str) -> None:
        if not hasattr(self, "command_completer"):
            return
        if text.lstrip().startswith(">"):
            self.command_completer.setCompletionPrefix(text.strip())
            if self.command_completer.completionCount() > 0:
                self.command_completer.complete()
            else:
                self.command_completer.popup().hide()
        else:
            self.command_completer.popup().hide()

    def _run_command_completion(self, command: str) -> None:
        self.command_search.setText(command)
        self.command_search.setCursorPosition(len(command))
        self._open_quick_search()

    def _focus_quick_search(self) -> None:
        self.show_workspace()
        self.command_search.selectAll()
        self.command_search.setFocus()

    def _focus_command_palette(self) -> None:
        self.show_workspace()
        self.command_search.setText("> ")
        self.command_search.setCursorPosition(len(self.command_search.text()))
        self.command_search.setFocus()

    def _project_files(self) -> list[Path]:
        files: list[Path] = []
        for root, directories, names in os.walk(self.project_root):
            directories[:] = [
                name for name in directories
                if name not in CodingToolbox.EXCLUDED_NAMES and not (Path(root) / name).is_symlink()
            ]
            for name in names:
                path = Path(root) / name
                if not path.is_symlink():
                    files.append(path)
        return files

    @staticmethod
    def _parse_quick_open_location(term: str) -> tuple[str, int | None, int | None]:
        parts = term.rsplit(":", 2)
        if len(parts) == 3 and parts[-1].isdigit() and parts[-2].isdigit():
            return parts[0], max(1, int(parts[-2])), max(1, int(parts[-1]))
        if len(parts) == 2 and parts[-1].isdigit():
            return parts[0], max(1, int(parts[-1])), None
        return term, None, None

    def _open_quick_search(self) -> None:
        term = self.command_search.text().strip()
        if not term:
            self._focus_quick_search()
            return
        if term.startswith(">"):
            command = term[1:].strip().casefold()
            if "open folder" in command or "打开文件夹" in command or "workspace" in command:
                self.command_search.clear()
                self._choose_workspace()
                return
            elif "terminal" in command or "终端" in command:
                self._toggle_bottom_panel(1)
            elif "problem" in command or "问题" in command:
                self._toggle_bottom_panel(2)
            elif "search" in command or "搜索" in command:
                self._show_workspace_search()
            elif "replace" in command or "替换" in command:
                self._show_find_bar(True)
            elif any(keyword in command for keyword in ("appearance", "theme", "主题", "外观")):
                self.ui_theme_button.showMenu()
            elif "outline" in command or "大纲" in command:
                self._show_outline()
            elif command == "git" or "git" in command:
                self.show_git()
            elif "explorer" in command or "资源" in command:
                self.focus_explorer()
            else:
                self._append_log(f"未识别命令: {term[1:].strip()}")
                return
            self.command_search.clear()
            return
        file_term, line_number, column_number = self._parse_quick_open_location(term)
        query = file_term.casefold()
        candidates = self._project_files()
        ranked: list[tuple[tuple[int, int, str], Path]] = []
        for path in candidates:
            relative = path.relative_to(self.project_root).as_posix()
            filename = path.name.casefold()
            relative_lower = relative.casefold()
            if query == filename:
                score = 0
            elif query == relative_lower:
                score = 1
            elif filename.startswith(query):
                score = 2
            elif query in filename:
                score = 3
            elif query in relative_lower:
                score = 4
            else:
                continue
            ranked.append(((score, len(relative), relative_lower), path))
        if not ranked:
            self._append_log(f"没有找到文件: {term}")
            return
        ranked.sort(key=lambda item: item[0])
        target = ranked[0][1]
        self.command_search.clear()
        self._open_file(target, preview=False)
        editor = self._active_editor()
        if editor is not None:
            if line_number is not None:
                self._move_editor_to_location(editor, line_number, column_number)
            editor.setFocus()

    def _show_workspace_search(self) -> None:
        if hasattr(self, "workspace_stack") and self.workspace_stack.currentWidget() is not self.workspace_page:
            self.workspace_stack.setCurrentWidget(self.workspace_page)
        self._set_activity_button(1)
        self.bottom_tabs.setCurrentIndex(0)
        self.bottom_tabs.setVisible(True)
        self.workspace_search_input.setFocus()
        self.workspace_search_input.selectAll()

    def _show_outline(self) -> None:
        if hasattr(self, "workspace_stack") and self.workspace_stack.currentWidget() is not self.workspace_page:
            self.workspace_stack.setCurrentWidget(self.workspace_page)
        self._refresh_outline()
        self.bottom_tabs.setCurrentIndex(3)
        self.bottom_tabs.setVisible(True)
        self.outline_tree.setFocus()

    def _refresh_outline(self) -> None:
        if not hasattr(self, "outline_tree"):
            return
        self.outline_tree.clear()
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if editor is None or path is None:
            return
        text = editor.toPlainText()
        count = 0

        def add_symbol(name: str, kind: str, line_number: int, parent: QTreeWidgetItem | None = None) -> QTreeWidgetItem:
            nonlocal count
            count += 1
            item = QTreeWidgetItem([f"{kind}  {name}  ·  {line_number}"])
            item.setData(0, Qt.ItemDataRole.UserRole, str(path))
            item.setData(0, Qt.ItemDataRole.UserRole + 1, line_number)
            if parent is None:
                self.outline_tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            return item

        if path.suffix.lower() == ".py":
            try:
                syntax_tree = ast.parse(text or "\n")
            except SyntaxError:
                syntax_tree = None
            if syntax_tree is not None:
                def visit(nodes: list[ast.AST], parent: QTreeWidgetItem | None = None) -> None:
                    for node in nodes:
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                            kind = "类" if isinstance(node, ast.ClassDef) else "函数"
                            item = add_symbol(node.name, kind, node.lineno, parent)
                            visit(getattr(node, "body", []), item)

                visit(syntax_tree.body)

        if count == 0:
            pattern = re.compile(r"^\s*(?:def|class|function|interface|enum|struct|fn)\s+([A-Za-z_]\w*)")
            for line_number, line in enumerate(text.splitlines(), start=1):
                match = pattern.match(line)
                if match:
                    add_symbol(match.group(1), "符号", line_number)
        if count == 0:
            item = QTreeWidgetItem(["当前文件没有可识别的符号"])
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)

    def _refresh_diagnostics(self) -> None:
        if not hasattr(self, "problems_tree"):
            return
        self.problems_tree.clear()
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if editor is None or path is None or path.suffix.lower() != ".py":
            return
        try:
            ast.parse(editor.toPlainText() or "\n")
        except SyntaxError as exc:
            line_number = max(1, int(exc.lineno or 1))
            column_number = max(1, int(exc.offset or 1))
            message = str(exc.msg or "语法错误")
            item = QTreeWidgetItem([f"{path.name}:{line_number}:{column_number}  {message}"])
            item.setData(0, Qt.ItemDataRole.UserRole, str(path))
            item.setData(0, Qt.ItemDataRole.UserRole + 1, line_number)
            item.setData(0, Qt.ItemDataRole.UserRole + 2, column_number)
            self.problems_tree.addTopLevelItem(item)

    def _open_problem(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        raw_path = item.data(0, Qt.ItemDataRole.UserRole)
        line_number = item.data(0, Qt.ItemDataRole.UserRole + 1)
        column_number = item.data(0, Qt.ItemDataRole.UserRole + 2)
        if not raw_path:
            return
        self._open_file(Path(str(raw_path)), preview=False)
        editor = self._active_editor()
        if editor is not None:
            self._move_editor_to_location(editor, int(line_number or 1), int(column_number or 1))
            editor.setFocus()

    def _open_outline_result(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        raw_path = item.data(0, Qt.ItemDataRole.UserRole)
        line_number = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if not raw_path or not line_number:
            return
        self._open_file(Path(str(raw_path)), preview=False)
        editor = self._active_editor()
        if editor is not None:
            self._move_editor_to_location(editor, int(line_number))
            editor.setFocus()

    def _search_workspace(self) -> None:
        query = self.workspace_search_input.text().strip()
        self.workspace_search_results.clear()
        if not query:
            self._append_log("请输入工作区搜索内容")
            return
        whole_word = self._workspace_search_whole_word
        self._workspace_search_whole_word = False
        query_folded = query.casefold()
        count = 0
        for path in self._project_files():
            if count >= 500:
                break
            try:
                raw = path.read_bytes()
                if len(raw) > CodingToolbox.MAX_READ_BYTES or b"\x00" in raw:
                    continue
                lines = raw.decode("utf-8", errors="replace").splitlines()
            except OSError:
                continue
            relative = path.relative_to(self.project_root).as_posix()
            for line_number, line in enumerate(lines, start=1):
                if whole_word:
                    matched = re.search(rf"\b{re.escape(query)}\b", line, re.IGNORECASE) is not None
                else:
                    matched = query_folded in line.casefold()
                if not matched:
                    continue
                item = QTreeWidgetItem([f"{relative}:{line_number}  {line.strip()[:180]}"])
                item.setData(0, Qt.ItemDataRole.UserRole, str(path))
                item.setData(0, Qt.ItemDataRole.UserRole + 1, line_number)
                self.workspace_search_results.addTopLevelItem(item)
                count += 1
                if count >= 500:
                    break
        suffix = "（已显示前 500 条）" if count >= 500 else ""
        self._append_log(f"工作区搜索完成：{count} 条{suffix}")

    def _open_search_result(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        raw_path = item.data(0, Qt.ItemDataRole.UserRole)
        line_number = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if not raw_path:
            return
        if self._current_editor_location() is not None:
            self._remember_navigation()
        self._open_file(Path(str(raw_path)), preview=False)
        editor = self._active_editor()
        if editor is None:
            return
        self._move_editor_to_location(editor, int(line_number or 1))
        editor.setFocus()

    @staticmethod
    def _move_editor_to_location(editor: CodeEditor, line_number: int, column_number: int | None = None) -> None:
        line_number = max(1, min(line_number, max(1, editor.blockCount())))
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        for _ in range(max(0, line_number - 1)):
            cursor.movePosition(QTextCursor.MoveOperation.Down)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        if column_number is None:
            cursor.movePosition(QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor)
        else:
            column = min(max(0, column_number - 1), len(cursor.block().text()))
            cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor, column)
        editor.setTextCursor(cursor)
        editor.ensureCursorVisible()

    def _show_find_bar(self, replace: bool = False) -> None:
        if hasattr(self, "workspace_stack") and self.workspace_stack.currentWidget() is not self.workspace_page:
            self.workspace_stack.setCurrentWidget(self.workspace_page)
        editor = self._active_editor()
        if editor is None:
            return
        self.find_bar.setVisible(True)
        self.replace_input.setVisible(replace)
        self.replace_button.setVisible(replace)
        self.replace_all_button.setVisible(replace)
        selected = editor.textCursor().selectedText().replace("\u2029", "\n")
        if selected and "\n" not in selected and not self.find_input.text():
            self.find_input.setText(selected)
        self.find_input.setFocus()
        self.find_input.selectAll()

    def _close_find_bar(self) -> None:
        self.find_bar.setVisible(False)
        editor = self._active_editor()
        if editor is not None:
            editor.setFocus()

    def _find_flags(self, backward: bool = False) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if self.find_case_checkbox.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if self.find_word_checkbox.isChecked():
            flags |= QTextDocument.FindFlag.FindWholeWords
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        return flags

    def _on_find_text_changed(self, text: str) -> None:
        if not text:
            self.find_status.setText("")
            return
        self._find_next()

    def _find_next(self) -> None:
        self._find_in_editor(backward=False)

    def _find_previous(self) -> None:
        self._find_in_editor(backward=True)

    def _find_in_editor(self, backward: bool) -> None:
        editor = self._active_editor()
        term = self.find_input.text()
        if editor is None or not term:
            self.find_status.setText("")
            return
        flags = self._find_flags(backward)
        if editor.find(term, flags):
            self.find_status.setText("已找到")
            return
        cursor = editor.textCursor()
        cursor.clearSelection()
        cursor.movePosition(
            QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start
        )
        editor.setTextCursor(cursor)
        if editor.find(term, flags):
            self.find_status.setText("已循环")
        else:
            self.find_status.setText("未找到")

    def _replace_current(self) -> None:
        editor = self._active_editor()
        term = self.find_input.text()
        if editor is None or not term:
            return
        cursor = editor.textCursor()
        selected = cursor.selectedText()
        matches = selected == term if self.find_case_checkbox.isChecked() else selected.casefold() == term.casefold()
        if not matches:
            self._find_next()
            cursor = editor.textCursor()
        if cursor.hasSelection():
            cursor.insertText(self.replace_input.text())
            self.find_status.setText("已替换")
            return
        self.find_status.setText("未找到")

    def _replace_all(self) -> None:
        editor = self._active_editor()
        term = self.find_input.text()
        if editor is None or not term:
            return
        document = editor.document()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        count = 0
        flags = self._find_flags(False)
        while True:
            match = document.find(term, cursor, flags)
            if match.isNull():
                break
            match.insertText(self.replace_input.text())
            cursor = match
            count += 1
        cursor.endEditBlock()
        self.find_status.setText(f"已替换 {count} 处")
        editor.setFocus()

    def _show_current_diff(self) -> None:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if path is None and editor is not None:
            contextual_path = editor.property("diff_path")
            if contextual_path:
                path = Path(str(contextual_path))
        if editor is None or path is None:
            self._append_log("请先打开一个项目文件")
            return
        diff_text = self.git.diff(path)
        if not diff_text:
            self._append_log(f"{path.name} 没有未提交的差异")
            return
        title = f"差异 · {path.name}"
        diff_editor = next(
            (candidate for candidate, label in self._editor_titles.items() if label == title),
            None,
        )
        if diff_editor is None:
            diff_editor = CodeEditor()
            self._configure_editor(diff_editor)
            diff_editor.setReadOnly(True)
            diff_editor.setProperty("diff_path", str(path))
            self._editor_titles[diff_editor] = title
            group = self._active_editor_group or self.editor_tabs
            index = group.addTab(diff_editor, title)
            group.setTabToolTip(index, f"Git diff · {path.as_posix()}")
        diff_editor.setPlainText(diff_text)
        diff_editor.document().setModified(False)
        group = self._editor_group_for(diff_editor) or self._active_editor_group or self.editor_tabs
        self._active_editor_group = group
        group.setCurrentWidget(diff_editor)
        self._append_log(f"已打开 {path.name} 的 Git 差异")

    def _toggle_bottom_panel(self, tab_index: int | None = None) -> None:
        if hasattr(self, "workspace_stack") and self.workspace_stack.currentWidget() is not self.workspace_page:
            self.workspace_stack.setCurrentWidget(self.workspace_page)
        if tab_index is not None:
            if self.bottom_tabs.isVisible() and self.bottom_tabs.currentIndex() == tab_index:
                self.bottom_tabs.setVisible(False)
                return
            self.bottom_tabs.setCurrentIndex(tab_index)
            self.bottom_tabs.setVisible(True)
        else:
            self.bottom_tabs.setVisible(not self.bottom_tabs.isVisible())
        if self.bottom_tabs.isVisible() and tab_index == 1:
            self.terminal_input.setFocus()

    def _run_terminal_command(self) -> None:
        command = self.terminal_input.text().strip()
        if not command:
            return
        if self.terminal_process is not None and self.terminal_process.state() != QProcess.ProcessState.NotRunning:
            self._append_terminal_output("\n[已有命令正在运行]\n")
            return
        if not self.bottom_tabs.isVisible() or self.bottom_tabs.currentIndex() != 1:
            self._toggle_bottom_panel(1)
        self.terminal_input.clear()
        self._append_terminal_output(f"\n$ {command}\n")
        self._terminal_buffer = ""
        process = QProcess(self)
        self.terminal_process = process
        process.setWorkingDirectory(str(self.terminal_cwd))
        process.readyReadStandardOutput.connect(self._read_terminal_output)
        process.readyReadStandardError.connect(self._read_terminal_output)
        process.finished.connect(self._terminal_finished)
        process.errorOccurred.connect(
            lambda error: self._append_terminal_output(f"\n[进程错误: {error.name}]\n")
        )
        environment = QProcessEnvironment.systemEnvironment()
        if os.name == "nt":
            environment.insert("PYTHONUTF8", "1")
            environment.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(environment)
        if os.name == "nt":
            # QProcess quotes each argument for CreateProcess. Passing the
            # whole command as the final ``cmd /c`` argument therefore turns
            # embedded quotes into literal backslashes on Windows. Use the
            # raw-command overload so quoted executable paths work correctly.
            shell = os.environ.get("COMSPEC", "cmd.exe")
            process.startCommand(f'"{shell}" /d /c chcp 65001>nul & {command}')
        else:
            process.start(os.environ.get("SHELL", "/bin/sh"), ["-lc", command])
        self.ledger.append(
            "terminal_command",
            {
                "command": command,
                "cwd": self.terminal_cwd.relative_to(self.project_root).as_posix() or ".",
            },
        )

    def _read_terminal_output(self) -> None:
        process = self.terminal_process
        if process is None:
            return
        output = self._decode_terminal_bytes(bytes(process.readAllStandardOutput()))
        output += self._decode_terminal_bytes(bytes(process.readAllStandardError()))
        if output:
            self._terminal_buffer += output
            self._append_terminal_output(output)

    @staticmethod
    def _decode_terminal_bytes(raw: bytes) -> str:
        if not raw:
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            fallback = "mbcs" if os.name == "nt" else "utf-8"
            return raw.decode(fallback, errors="replace")

    def _append_terminal_output(self, text: str) -> None:
        self.terminal_output.moveCursor(QTextCursor.MoveOperation.End)
        self.terminal_output.insertPlainText(text)
        self.terminal_output.moveCursor(QTextCursor.MoveOperation.End)

    def _terminal_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._read_terminal_output()
        self._append_terminal_output(f"\n[退出码 {exit_code}]\n")
        if exit_code != 0:
            self.problems_output.setPlainText(self._terminal_buffer.strip() or f"命令退出码: {exit_code}")
        self.refresh_git_status()
        process = self.terminal_process
        self.terminal_process = None
        if process is not None:
            process.deleteLater()

    def save_current_file(self) -> None:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if editor is None or path is None or not path.is_relative_to(self.project_root):
            self._append_log("当前没有可保存的项目文件")
            return
        try:
            relative = path.relative_to(self.project_root).as_posix()
            result = self.toolbox.write_file(relative, editor.toPlainText())
            editor.document().setModified(False)
            self._update_editor_tab_for(editor)
            self._append_log(result)
            self.refresh_git_status()
        except Exception as exc:  # noqa: BLE001
            self._show_message("保存失败", str(exc))

    def _explorer_path(self, index: QModelIndex | None = None) -> Path | None:
        target_index = index if index is not None and index.isValid() else self.file_tree.currentIndex()
        if not target_index.isValid():
            return self.project_root
        source_index = self.file_proxy.mapToSource(target_index)
        path = Path(self.file_model.filePath(source_index)).resolve()
        return path if path.is_relative_to(self.project_root) else None

    def _prepare_explorer_entry(
        self,
        mode: str,
        parent: Path | None = None,
        target: Path | None = None,
    ) -> None:
        self._explorer_entry_mode = mode
        self._rename_target = target
        self._entry_parent = (parent or self.project_root).resolve()
        if self.new_file_entry.isVisible() and self._explorer_entry_mode == mode:
            self.new_file_entry.setVisible(False)
            return
        self.new_file_entry.clear()
        if mode == "folder":
            self.new_file_entry.setPlaceholderText("输入文件夹名称，回车创建")
        elif mode == "rename":
            self.new_file_entry.setText(target.name if target is not None else "")
            self.new_file_entry.setPlaceholderText("输入新名称，回车确认")
            self.new_file_entry.selectAll()
        else:
            self.new_file_entry.setPlaceholderText("输入相对路径，例如 src/new_module.py，回车创建")
        self.new_file_entry.setVisible(True)
        self.new_file_entry.setFocus()

    def start_new_file_entry(self, parent: Path | None = None) -> None:
        self._prepare_explorer_entry("create", parent=parent)

    def start_new_folder_entry(self, parent: Path | None = None) -> None:
        self._prepare_explorer_entry("folder", parent=parent)

    def start_rename_entry(self, target: Path | None = None) -> None:
        target = target or self._explorer_path()
        if target is None or target == self.project_root or not target.exists():
            return
        self._prepare_explorer_entry("rename", target=target)

    def create_new_file(self) -> None:
        raw_name = self.new_file_entry.text().strip().replace("\\", "/")
        if not raw_name:
            self.new_file_entry.setVisible(False)
            return
        mode = self._explorer_entry_mode
        old_target = self._rename_target
        if mode == "rename" and old_target is not None:
            target = (old_target.parent / raw_name).resolve()
        else:
            target = (self._entry_parent / raw_name).resolve()
        try:
            relative = target.relative_to(self.project_root)
        except ValueError:
            self._append_log("资源操作失败：路径必须位于项目目录内")
            return
        if not relative.parts or relative.parts[0] in CodingToolbox.EXCLUDED_NAMES:
            self._append_log("资源操作失败：不能操作内部目录")
            return
        try:
            if mode == "rename":
                if old_target is None or not old_target.exists() or target.exists():
                    self._append_log("重命名失败：名称无效或目标已存在")
                    return
                old_relative = old_target.relative_to(self.project_root).as_posix()
                old_target.rename(target)
                self._update_open_paths_after_rename(old_target, target)
                self._append_log(f"已重命名 {old_relative} → {relative.as_posix()}")
            elif mode == "folder":
                if target.exists():
                    self._append_log("新建文件夹失败：目标已存在")
                    return
                target.mkdir(parents=True, exist_ok=False)
                self._append_log(f"已新建文件夹 {relative.as_posix()}")
            else:
                if target.exists():
                    self._append_log("新建文件失败：文件已存在")
                    return
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("", encoding="utf-8")
                self._open_file(target, preview=False)
                self._append_log(f"已新建 {relative.as_posix()}")
            self.new_file_entry.clear()
            self.new_file_entry.setVisible(False)
            self.refresh_file_tree()
        except OSError as exc:
            self._append_log(f"资源操作失败：{exc}")

    def _update_open_paths_after_rename(self, old_target: Path, new_target: Path) -> None:
        for editor, path in list(self._editor_paths.items()):
            if path == old_target or old_target in path.parents:
                updated = new_target / path.relative_to(old_target) if path != old_target else new_target
                self._editor_paths[editor] = updated
                group = self._editor_group_for(editor)
                index = group.indexOf(editor) if group is not None else -1
                if group is not None and index >= 0:
                    group.setTabToolTip(index, updated.as_posix())
                    self._update_editor_tab_for(editor)
        if self.current_file == old_target or (self.current_file is not None and old_target in self.current_file.parents):
            self.current_file = new_target / self.current_file.relative_to(old_target) if self.current_file != old_target else new_target

    def _show_explorer_menu(self, point: QPoint) -> None:
        index = self.file_tree.indexAt(point)
        # A blank-area context menu must not inherit the previously selected
        # resource; otherwise Rename/Delete can target the wrong file.
        path = self._explorer_path(index) if index.isValid() else None
        if path is not None and index.isValid():
            self.file_tree.setCurrentIndex(index)
        base = path if path is not None and path.is_dir() else (path.parent if path is not None else self.project_root)
        menu = QMenu(self)
        new_file = menu.addAction("新建文件")
        new_file.triggered.connect(lambda: self.start_new_file_entry(base))
        new_folder = menu.addAction("新建文件夹")
        new_folder.triggered.connect(lambda: self.start_new_folder_entry(base))
        menu.addSeparator()
        rename = menu.addAction("重命名")
        rename.setEnabled(path is not None and path != self.project_root)
        rename.triggered.connect(lambda: self.start_rename_entry(path))
        delete = menu.addAction("删除")
        delete.setEnabled(path is not None and path != self.project_root)
        delete.triggered.connect(lambda: self.delete_explorer_path(path))
        menu.addSeparator()
        open_terminal = menu.addAction("在终端中打开")
        open_terminal.triggered.connect(lambda: self.open_terminal_at(base))
        menu.exec(self.file_tree.viewport().mapToGlobal(point))

    def delete_explorer_path(self, target: Path | None) -> None:
        if target is None:
            return
        target = target.resolve()
        if target == self.project_root or not target.is_relative_to(self.project_root):
            return
        if target.name in CodingToolbox.EXCLUDED_NAMES:
            self._append_log("删除失败：不能操作内部目录")
            return
        dirty = [
            path.name for editor, path in self._editor_paths.items()
            if (path == target or target in path.parents) and editor.document().isModified()
        ]
        if dirty:
            self._append_log(f"删除失败：存在未保存文件 {', '.join(dirty[:3])}")
            return
        answer = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除 {target.name} 吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            editors = [
                (self._editor_group_for(editor), editor)
                for editor, path in self._editor_paths.items()
                if path == target or target in path.parents
            ]
            for group, editor in editors:
                if group is not None:
                    self._close_editor_tab(group.indexOf(editor), group)
            self.refresh_file_tree()
            self._append_log(f"已删除 {target.relative_to(self.project_root).as_posix()}")
        except OSError as exc:
            self._append_log(f"删除失败：{exc}")

    def open_terminal_at(self, target: Path | None) -> None:
        if target is None:
            target = self.project_root
        target = target.resolve()
        if not target.is_dir() or not target.is_relative_to(self.project_root):
            target = self.project_root
        self.terminal_cwd = target
        self._toggle_bottom_panel(1)
        relative = target.relative_to(self.project_root).as_posix() or "."
        self._append_terminal_output(f"\n[cwd: {relative}]\n")
        self.terminal_input.setFocus()

    def refresh_file_tree(self) -> None:
        root_index = self.file_model.setRootPath(str(self.project_root))
        self.file_tree.setRootIndex(self.file_proxy.mapFromSource(root_index))
        self._append_log("资源管理器已刷新")

    def _insert_current_file_context(self) -> None:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if editor is None or path is None:
            self._append_log("请先在编辑器中打开一个文件")
            return
        relative = path.relative_to(self.project_root).as_posix()
        selected = editor.textCursor().selectedText().replace("\u2029", "\n")[:4000]
        context = f"\n\n请参考当前文件：`{relative}`"
        if selected:
            context += f"\n当前选中代码：\n```\n{selected}\n```"
        self.chat_input.insertPlainText(context)
        self.chat_input.setFocus()

    def _prompt_with_editor_context(self, prompt: str) -> str:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if editor is None or path is None:
            return prompt
        relative = path.relative_to(self.project_root).as_posix()
        selected = editor.textCursor().selectedText().replace("\u2029", "\n")[:2400]
        context = f"\n\n当前编辑器文件：{relative}（如需修改，请先读取该文件）"
        if selected:
            context += f"\n当前选中代码：\n```\n{selected}\n```"
        return prompt + context

    def _refresh_open_file_after_task(self) -> None:
        for editor, path in list(self._editor_paths.items()):
            if not isinstance(editor, CodeEditor):
                continue
            if editor.document().isModified():
                if editor is self._active_editor():
                    self._append_log(f"Agent 已修改 {path.name}，当前标签有本地未保存内容，未自动覆盖")
                continue
            try:
                raw = path.read_bytes()
                if b"\x00" not in raw and len(raw) <= CodingToolbox.MAX_READ_BYTES:
                    editor.setPlainText(raw.decode("utf-8", errors="replace"))
                    editor.document().setModified(False)
                    self._update_editor_tab_for(editor)
            except OSError:
                continue

    def submit_chat(self) -> None:
        prompt = self.chat_input.toPlainText().strip()
        if not prompt:
            self.chat_input.setFocus()
            return
        self.chat_input.clear()
        self._submit_prompt(prompt)

    def _submit_prompt(self, prompt: str) -> None:
        if self.active_task_id:
            self._append_chat("系统", "当前任务仍在执行，请等待 Agent 完成后继续。", "meta")
            return
        if self.current_session_id is None:
            self.current_session_id = new_id("session")
            self._append_chat("系统", f"已创建会话 {self.current_session_id}", "meta")
        self._append_chat("你", prompt, "user")
        task_prompt = self._prompt_with_editor_context(prompt)
        self.active_task_id = self.worker.submit(
            "coding",
            {"session_id": self.current_session_id, "prompt": task_prompt},
            max_attempts=6,
        )
        self._set_state(self.chat_status, "排队中 · 等待 Agent", "StateWorking")
        self._set_state(self.workspace_state, "● 排队中", "StateWorking")
        self._append_log(f"编码任务已加入队列：{self.active_task_id}")

    def new_coding_task(self) -> None:
        if self.active_task_id:
            self._append_chat("系统", "当前任务仍在执行，暂时不能新建会话。", "meta")
            return
        self.show_workspace()
        self.current_session_id = None
        self._show_welcome()
        self._clear_chat_history()
        self._append_chat("系统", "已开始新会话。", "meta")
        self.chat_input.setFocus()

    def show_tasks(self) -> None:
        self.show_git()
        self._append_log("任务历史已收进开发版本树")

    def _clear_task_dialog(self, dialog: QDialog) -> None:
        if self.task_dialog is dialog:
            self.task_dialog = None
            self.task_history_tree = None

    def refresh_task_history(self) -> None:
        tree = self.task_history_tree
        if tree is None:
            return
        tree.clear()
        for item in self.ledger.list_tasks():
            row = QTreeWidgetItem(tree, [item["kind"], item["status"], str(item["attempts"]), item["last_error"]])
            row.setData(0, Qt.ItemDataRole.UserRole, item["task_id"])

    def retry_selected(self) -> None:
        tree = self.task_history_tree
        if tree is None or not tree.currentItem():
            return
        task_id = tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
        if task_id and self.ledger.retry_now(str(task_id)):
            self.worker.wake()
            self._append_log(f"已手动重试 {task_id}")
            self.refresh_task_history()

    def show_git(self) -> None:
        self._set_activity_button(2)
        self.workspace_stack.setCurrentWidget(self.git_page)
        self._adapt_git_layout()
        self._refresh_development_tree()
        self._append_log("已打开开发版本树")

    def init_git(self) -> None:
        try:
            self.git.init()
            self._append_log("Git 已准备好；编码任务完成后会自动提交。")
            self.refresh_git_status()
            self._refresh_development_tree()
        except Exception as exc:  # noqa: BLE001
            self._show_message("Git 错误", str(exc))

    def show_workspace(self) -> None:
        self._set_activity_button(0)
        self.workspace_stack.setCurrentWidget(self.workspace_page)
        self._append_log("已返回编码工作区")

    def focus_explorer(self) -> None:
        workspace_is_active = self.workspace_stack.currentWidget() is self.workspace_page
        should_show = self.explorer.isHidden() if workspace_is_active else True
        self._explorer_visibility_override = should_show
        self.explorer.setVisible(should_show)
        self.show_workspace()
        self._adapt_workbench_layout()
        if should_show:
            self.file_tree.setFocus()
        else:
            editor = self._active_editor()
            if editor is not None:
                editor.setFocus()

    def _handle_coding(self, task: dict[str, Any]) -> dict[str, Any]:
        return self.agent.run(task)

    def eventFilter(self, watched: QObject, event) -> bool:  # noqa: ANN001 - Qt event signature.
        if watched is self.file_tree.viewport() and event.type() == QEvent.Type.MouseButtonDblClick:
            index = self.file_tree.indexAt(event.position().toPoint())
            self._on_file_selected(index)
            return True
        if isinstance(watched, CodeEditor) and event.type() == QEvent.Type.FocusIn:
            group = self._editor_group_for(watched)
            if group is not None:
                self._active_editor_group = group
        if isinstance(watched, CodeEditor) and event.type() == QEvent.Type.ContextMenu:
            menu = watched.createStandardContextMenu()
            menu.addSeparator()
            go_to_definition = menu.addAction("转到定义")
            go_to_definition.triggered.connect(self._go_to_definition)
            find_references = menu.addAction("查找引用")
            find_references.triggered.connect(self._find_symbol_references)
            rename_symbol = menu.addAction("重命名符号")
            rename_symbol.triggered.connect(self._rename_current_symbol)
            format_document = menu.addAction("格式化当前文档")
            format_document.triggered.connect(self._format_current_document)
            menu.exec(event.globalPos())
            return True
        if isinstance(watched, CodeEditor) and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_S and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.save_current_file()
                return True
            if event.key() == Qt.Key.Key_W and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._close_editor_tab(self._active_editor_group.currentIndex() if self._active_editor_group is not None else -1)
                return True
            if event.key() == Qt.Key.Key_F2 and not event.modifiers():
                self._rename_current_symbol()
                return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        if self._closing:
            event.accept()
            return
        dirty_editors = [
            (editor, path)
            for editor, path in self._editor_paths.items()
            if isinstance(editor, CodeEditor) and editor.document().isModified()
        ]
        if dirty_editors:
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setWindowTitle("未保存的修改")
            dialog.setText(f"有 {len(dirty_editors)} 个文件尚未保存。")
            filenames = [path.name for _editor, path in dirty_editors[:5]]
            if len(dirty_editors) > len(filenames):
                filenames.append(f"另有 {len(dirty_editors) - len(filenames)} 个文件")
            dialog.setInformativeText("、".join(filenames))
            save_all = dialog.addButton("保存全部", QMessageBox.ButtonRole.AcceptRole)
            discard = dialog.addButton("放弃修改", QMessageBox.ButtonRole.DestructiveRole)
            cancel = dialog.addButton("继续编辑", QMessageBox.ButtonRole.RejectRole)
            dialog.setDefaultButton(save_all)
            dialog.setEscapeButton(cancel)
            dialog.exec()
            choice = dialog.clickedButton()
            if choice is cancel or choice not in (save_all, discard):
                event.ignore()
                return
            if choice is save_all:
                failures: list[str] = []
                for editor, path in dirty_editors:
                    try:
                        relative = path.relative_to(self.project_root).as_posix()
                        self.toolbox.write_file(relative, editor.toPlainText())
                    except Exception as exc:  # noqa: BLE001 - keep all unsaved buffers alive on failure.
                        failures.append(f"{path.name}: {exc}")
                        continue
                    editor.document().setModified(False)
                    self._update_editor_tab_for(editor)
                if failures:
                    self._append_log("关闭已取消，以下文件未能保存：" + "；".join(failures)[:360])
                    event.ignore()
                    return
        self._closing = True
        if self.terminal_process is not None and self.terminal_process.state() != QProcess.ProcessState.NotRunning:
            self.terminal_process.kill()
        self.worker.stop()
        app = QApplication.instance()
        windows = getattr(app, "_scidev_windows", []) if app is not None else []
        if self in windows:
            windows.remove(self)
        event.accept()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SciDevHarness desktop coding client")
    parser.add_argument(
        "--workspace",
        type=Path,
        help="open this project folder (defaults to the most recently used folder)",
    )
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    source_root = application_base_dir()
    if args.workspace is not None:
        try:
            workspace_root = resolve_initial_workspace(args.workspace, None, source_root)
        except ValueError as exc:
            parser.error(str(exc))

    app = QApplication([sys.argv[0]])
    app.setApplicationName("SciDevHarness")
    app.setOrganizationName("SciDevHarness")
    app.setOrganizationDomain("scidevharness.local")
    app.setApplicationDisplayName("SciDevHarness")
    ui_font = QFont("Segoe UI", 11)
    ui_font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(ui_font)

    settings = QSettings("SciDevHarness", "SciDevHarness")
    if args.workspace is None:
        last_workspace = str(settings.value("lastWorkspace", "") or "")
        is_packaged = bool(getattr(sys, "frozen", False) or "__compiled__" in globals())
        if is_packaged and not (last_workspace and Path(last_workspace).is_dir()):
            selected = _choose_workspace_directory(Path.home())
            if selected is None:
                return 0
            workspace_root = selected
        else:
            fallback = Path(sys.executable).resolve().parent if is_packaged else source_root
            workspace_root = resolve_initial_workspace(None, last_workspace, fallback)

    if not args.smoke_test:
        settings.setValue("lastWorkspace", str(workspace_root))
    window = ClientWindow(workspace_root)
    app._scidev_windows = [window]
    window.show()
    if args.smoke_test:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            app.processEvents()
            if window.git_tree.status() != QQuickWidget.Status.Loading:
                break
            time.sleep(0.01)
        qml_ready = window.git_tree.status() == QQuickWidget.Status.Ready and not window.git_tree.errors()
        if not qml_ready:
            errors = "; ".join(error.toString() for error in window.git_tree.errors())
            print(f"QML startup smoke test failed: {errors or 'loading timeout'}", file=sys.stderr)
        window.close()
        app.processEvents()
        return 0 if qml_ready else 1
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
