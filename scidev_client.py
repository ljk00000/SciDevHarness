"""SciDevHarness desktop coding client.

The shell intentionally follows the information hierarchy of a modern code
editor: activity bar, project explorer, editor workbench, and an Agent panel.
The coding Agent and its retry/audit services remain UI-independent in
``scidev_core.py``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDir, QEvent, QModelIndex, QObject, QPoint, QRect, QRegularExpression, Qt, QSortFilterProxyModel, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeyEvent, QPainter, QSyntaxHighlighter, QTextCharFormat, QTextFormat
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileSystemModel,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSizeGrip,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from scidev_core import CodingAgent, CodingToolbox, EventLedger, GitManager, RetryQueue, new_id


THEME = """
* {
    font-family: "Segoe UI";
    font-size: 10pt;
}
QMainWindow, QWidget#Root {
    background: #1e1e1e;
    color: #cccccc;
}
QFrame#TitleBar {
    background: #181818;
    border-bottom: 1px solid #2b2b2b;
}
QFrame#ActivityRail {
    background: #181818;
    border-right: 1px solid #2b2b2b;
}
QFrame#ExplorerPane {
    background: #181818;
    border-right: 1px solid #2b2b2b;
}
QFrame#WorkspacePane {
    background: #1e1e1e;
}
QFrame#ChatPane {
    background: #181818;
    border-left: 1px solid #2b2b2b;
}
QFrame#WorkspaceToolbar, QFrame#EditorToolbar {
    background: #1e1e1e;
    border-bottom: 1px solid #2b2b2b;
}
QFrame#TaskPanel {
    background: #252526;
    border-bottom: 1px solid #3a3a3a;
}
QFrame#ChatHeader {
    background: #181818;
    border-bottom: 1px solid #2b2b2b;
}
QFrame#Composer {
    background: #181818;
    border-top: 1px solid #2b2b2b;
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
    color: #9d9d9d;
}
QLabel#Overline {
    color: #bdbdbd;
    font-size: 9pt;
    font-weight: 600;
}
QLabel#SectionTitle {
    color: #eeeeee;
    font-size: 11pt;
    font-weight: 600;
}
QLabel#Logo {
    background: #0078d4;
    color: #ffffff;
    border-radius: 5px;
    font-size: 12pt;
    font-weight: 700;
}
QLabel#AgentLogo {
    background: #2f81f7;
    color: #ffffff;
    border-radius: 5px;
    font-size: 12pt;
    font-weight: 700;
}
QLabel#StateReady { color: #73c991; font-weight: 600; }
QLabel#StateWorking { color: #e5c07b; font-weight: 600; }
QLabel#StateError { color: #f48771; font-weight: 600; }
QLineEdit, QTextEdit, QPlainTextEdit {
    background: #1f1f1f;
    color: #d4d4d4;
    border: 1px solid #3c3c3c;
    border-radius: 4px;
    padding: 8px 10px;
    selection-background-color: #264f78;
    selection-color: #ffffff;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border: 1px solid #007acc;
}
QLineEdit#CommandSearch {
    background: #252526;
    border: 1px solid #3c3c3c;
    color: #cccccc;
    padding: 7px 10px;
}
QTextEdit#ChatInput {
    background: #252526;
    border: 1px solid #4a4a4a;
    border-radius: 5px;
    padding: 8px 9px;
}
QPlainTextEdit#CodeEditor {
    background: #1e1e1e;
    color: #d4d4d4;
    border: none;
    border-radius: 0px;
    padding: 14px 18px;
}
QPushButton, QToolButton {
    background: #2d2d2d;
    color: #cccccc;
    border: 1px solid #454545;
    border-radius: 4px;
    padding: 5px 10px;
}
QPushButton:hover, QToolButton:hover {
    background: #3a3d41;
    border-color: #606060;
}
QPushButton:pressed, QToolButton:pressed {
    background: #333333;
}
QPushButton#Primary {
    background: #0e639c;
    color: #ffffff;
    border: 1px solid #1177bb;
    font-weight: 600;
}
QPushButton#Primary:hover { background: #1177bb; }
QPushButton#Quiet, QToolButton#Quiet {
    background: transparent;
    border: 1px solid transparent;
}
QPushButton#Quiet:hover, QToolButton#Quiet:hover { background: #2a2d2e; }
QToolButton#ActivityButton {
    background: transparent;
    color: #858585;
    border: 1px solid transparent;
    border-radius: 0px;
    font-size: 18pt;
    padding: 5px 0px;
}
QToolButton#ActivityButton:hover {
    background: #2a2d2e;
    color: #ffffff;
}
QToolButton#ActivityButton:checked {
    color: #ffffff;
    border-left: 2px solid #007acc;
}
QToolButton#IconButton {
    background: transparent;
    color: #bdbdbd;
    border: 1px solid transparent;
    padding: 2px 5px;
}
QToolButton#IconButton:hover { background: #2a2d2e; color: #ffffff; }
QToolButton#WindowButton {
    background: transparent;
    color: #bdbdbd;
    border: none;
    border-radius: 0px;
    font-size: 11pt;
    padding: 0px;
}
QToolButton#WindowButton:hover { background: #333333; color: #ffffff; }
QToolButton#WindowClose:hover { background: #c42b1c; color: #ffffff; }
QTreeView {
    background: #181818;
    alternate-background-color: #1b1b1b;
    color: #cccccc;
    border: none;
    outline: none;
    padding: 5px 0px;
    font-size: 10pt;
}
QTreeView::item { padding: 5px 6px; border-radius: 3px; }
QTreeView::item:hover { background: #2a2d2e; }
QTreeView::item:selected { background: #37373d; color: #ffffff; }
QTabWidget::pane {
    border: none;
    background: #1e1e1e;
}
QTabBar {
    background: #181818;
}
QTabBar::tab {
    background: #181818;
    color: #969696;
    border: none;
    border-right: 1px solid #2b2b2b;
    padding: 10px 18px;
    min-width: 88px;
}
QTabBar::tab:selected {
    background: #1e1e1e;
    color: #f2f2f2;
    border-top: 1px solid #007acc;
}
QSplitter::handle { background: #2b2b2b; }
QSplitter::handle:hover { background: #3f3f46; }
QScrollArea, QScrollArea#ChatScroll, QScrollArea#ChatScroll > QWidget, QWidget#ChatContent {
    background: #181818;
    border: none;
}
QScrollBar:vertical {
    background: #181818;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical { background: #424242; min-height: 26px; border-radius: 4px; }
QScrollBar::handle:vertical:hover { background: #5a5a5a; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; border: none; }
QScrollBar:horizontal { background: #1e1e1e; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #424242; min-width: 26px; border-radius: 4px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; border: none; }
QStatusBar {
    background: #007acc;
    color: #ffffff;
    border: none;
}
QStatusBar::item { border: none; }
QStatusBar QLabel#StatusText { color: #ffffff; }
QFrame#UserBubble { background: #264f78; border: 1px solid #3b6e9e; border-radius: 5px; }
QFrame#AgentBubble { background: #252526; border: 1px solid #3b3b3b; border-radius: 5px; }
QFrame#ToolBubble { background: #202b24; border: 1px solid #395241; border-radius: 5px; }
QFrame#MetaBubble { background: transparent; border: none; }
QLabel#BubbleRole { color: #a8a8a8; font-size: 9pt; font-weight: 600; }
QLabel#BubbleText { color: #e1e1e1; font-size: 10pt; }
QLabel#ToolText { color: #b7d7bf; font-family: "Cascadia Mono", "Consolas", monospace; font-size: 9pt; }
QLabel#Chip { color: #9cdcfe; background: #252526; border: 1px solid #3c3c3c; border-radius: 3px; padding: 3px 7px; }
QDialog { background: #252526; color: #cccccc; }
QTreeWidget { background: #1e1e1e; color: #cccccc; border: 1px solid #3c3c3c; }
QHeaderView::section { background: #252526; color: #cccccc; border: none; border-bottom: 1px solid #3c3c3c; padding: 6px; }
"""


class AppSignals(QObject):
    agent_event = Signal(str, object)
    worker_event = Signal(str, object)


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
        editor_font = QFont("Cascadia Code", 11)
        editor_font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(editor_font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(self.highlight_current_line)
        self.update_line_number_area_width(0)
        self.highlight_current_line()

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
        painter.fillRect(event.rect(), QColor("#1e1e1e"))
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#858585"))
                painter.drawText(0, top, self.line_number_area.width() - 8, self.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, str(block_number + 1))
            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1

    def highlight_current_line(self) -> None:
        selection = QTextEdit.ExtraSelection()
        selection.format.setBackground(QColor("#252526"))
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        self.setExtraSelections([selection])


class PythonHighlighter(QSyntaxHighlighter):
    def __init__(self, document):  # noqa: ANN001 - document type varies by binding.
        super().__init__(document)
        self.rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        for pattern, color, bold in (
            (r"\b(and|as|assert|async|await|break|case|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|match|not|or|pass|raise|return|try|while|with|yield)\b", "#c586c0", True),
            (r"\b(True|False|None|self)\b", "#569cd6", False),
            (r"\b\d+(\.\d+)?\b", "#b5cea8", False),
            (r"#[^\n]*", "#6a9955", False),
            (r"\"[^\"\n]*\"|'[^'\n]*'", "#ce9178", False),
        ):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.Weight.Bold)
            self.rules.append((QRegularExpression(pattern), fmt))

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


class MessageBubble(QFrame):
    def __init__(self, speaker: str, message: str, kind: str):
        super().__init__()
        self.setObjectName({"user": "UserBubble", "agent": "AgentBubble", "tool": "ToolBubble", "meta": "MetaBubble"}.get(kind, "AgentBubble"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 9)
        layout.setSpacing(4)
        role = QLabel(speaker)
        role.setObjectName("BubbleRole")
        layout.addWidget(role)
        body = QLabel(message)
        body.setObjectName("ToolText" if kind == "tool" else "BubbleText")
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(body)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)


class ClientWindow(QMainWindow):
    def __init__(self, project_root: Path):
        super().__init__()
        self.project_root = Path(project_root).resolve()
        self.setWindowTitle("SciDevHarness — Coding Workspace")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.resize(1500, 920)
        self.setMinimumSize(1180, 760)
        self.setStyleSheet(THEME)
        self.ledger = EventLedger(self.project_root)
        self.git = GitManager(self.project_root)
        self.toolbox = CodingToolbox(self.project_root, self.ledger)
        self.signals = AppSignals()
        self.current_session_id: str | None = None
        self.active_task_id: str | None = None
        self.current_file: Path | None = None
        self._closing = False
        self.task_dialog: QDialog | None = None
        self.task_history_tree: QTreeWidget | None = None
        self.window_max_button: QToolButton | None = None

        self.agent = CodingAgent(self.project_root, self.ledger, self.git, event_callback=self._emit_agent)
        self.worker = RetryQueue(
            self.ledger,
            {"coding": self._handle_coding},
            event_callback=self._emit_worker,
            retry_base_seconds=5.0,
        )
        self.signals.agent_event.connect(self._handle_agent_event)
        self.signals.worker_event.connect(self._handle_worker_event)
        self._build_ui()
        self.worker.start()
        self.refresh_all()
        self._append_chat("系统", "准备好了。填写中间的编码任务，或直接在右侧告诉 Agent 要修改什么。", "meta")

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_title_bar())

        workbench = QSplitter(Qt.Orientation.Horizontal)
        workbench.setObjectName("Workbench")
        workbench.setChildrenCollapsible(False)
        workbench.setHandleWidth(1)
        workbench.addWidget(self._build_left_panel())
        workbench.addWidget(self._build_workspace())
        workbench.addWidget(self._build_chat_panel())
        workbench.setSizes([370, 1, 430])
        workbench.setStretchFactor(0, 0)
        workbench.setStretchFactor(1, 1)
        workbench.setStretchFactor(2, 0)
        root_layout.addWidget(workbench, 1)
        self.setCentralWidget(root)
        self.command_search.textChanged.connect(self.file_proxy.set_filter_text)

        status = QStatusBar()
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("StatusText")
        status.addWidget(self.status_label, 1)
        self.status_branch = QLabel("Git · 未初始化")
        status.addPermanentWidget(self.status_branch)
        status.addPermanentWidget(QLabel("UTF-8"))
        status.addPermanentWidget(QLabel("Python"))
        status.addPermanentWidget(QSizeGrip(self))
        self.setStatusBar(status)

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
        project = QLabel(self.project_root.name)
        project.setObjectName("Subtle")
        layout.addWidget(project)

        search = QLineEdit()
        self.command_search = search
        search.setObjectName("CommandSearch")
        search.setPlaceholderText("搜索文件、命令或跳转到…")
        search.setMinimumWidth(190)
        search.setMaximumWidth(340)
        search.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addStretch(1)
        layout.addWidget(search)
        layout.addStretch(1)

        new_button = QPushButton("新建任务")
        new_button.setObjectName("Primary")
        new_button.clicked.connect(self.new_coding_task)
        layout.addWidget(new_button)
        for text, command in (("任务历史", self.show_tasks), ("Git", self.show_git), ("刷新", self.refresh_all)):
            button = QPushButton(text)
            button.setObjectName("Quiet")
            button.clicked.connect(command)
            layout.addWidget(button)
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
        dialog = QDialog(self)
        dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(520, 220)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 14)
        layout.setSpacing(0)
        layout.addWidget(self._dialog_title_bar(dialog, title))
        body = QLabel(message)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body_layout = QVBoxLayout()
        body_layout.setContentsMargins(18, 18, 18, 10)
        body_layout.addWidget(body)
        layout.addLayout(body_layout, 1)
        actions = QHBoxLayout()
        actions.setContentsMargins(18, 0, 18, 0)
        actions.addStretch(1)
        close = QPushButton("关闭")
        close.setObjectName("Primary")
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        layout.addLayout(actions)
        dialog.exec()

    def _build_left_panel(self) -> QWidget:
        shell = QWidget()
        shell.setMinimumWidth(340)
        shell.setMaximumWidth(520)
        shell.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QHBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        rail = QFrame()
        rail.setObjectName("ActivityRail")
        rail.setFixedWidth(70)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 10, 0, 8)
        rail_layout.setSpacing(2)
        for index, (symbol, tip, command) in enumerate((
            ("⌂", "资源管理器", self.focus_explorer),
            ("▣", "文件", self.focus_explorer),
            ("◌", "任务历史", self.show_tasks),
            ("⌁", "Git", self.show_git),
        )):
            button = QToolButton()
            button.setObjectName("ActivityButton")
            button.setText(symbol)
            button.setToolTip(tip)
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setFixedHeight(50)
            button.clicked.connect(command)
            rail_layout.addWidget(button)
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
        for symbol, tip in (("+", "新建文件"), ("…", "更多操作")):
            action = QToolButton()
            action.setObjectName("IconButton")
            action.setText(symbol)
            action.setToolTip(tip)
            heading.addWidget(action)
        explorer_layout.addLayout(heading)

        project_row = QHBoxLayout()
        project = QLabel(f"⌄  {self.project_root.name.upper()}")
        project.setObjectName("AppTitle")
        project_row.addWidget(project)
        project_row.addStretch(1)
        explorer_layout.addLayout(project_row)

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
        self.file_tree.doubleClicked.connect(self._on_file_selected)
        explorer_layout.addWidget(self.file_tree, 1)

        hint = QLabel("本地工作区  ·  .research 已启用")
        hint.setObjectName("Hint")
        explorer_layout.addWidget(hint)
        layout.addWidget(rail)
        layout.addWidget(explorer, 1)
        self.explorer = explorer
        return shell

    def _build_workspace(self) -> QWidget:
        workspace = QFrame()
        workspace.setObjectName("WorkspacePane")
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        workspace_toolbar = QFrame()
        workspace_toolbar.setObjectName("WorkspaceToolbar")
        workspace_toolbar.setFixedHeight(36)
        toolbar_layout = QHBoxLayout(workspace_toolbar)
        toolbar_layout.setContentsMargins(14, 0, 12, 0)
        self.breadcrumb = QLabel("项目  /  新建编码任务")
        self.breadcrumb.setObjectName("Subtle")
        toolbar_layout.addWidget(self.breadcrumb)
        toolbar_layout.addStretch(1)
        self.workspace_state = QLabel("● 就绪")
        self.workspace_state.setObjectName("StateReady")
        toolbar_layout.addWidget(self.workspace_state)
        layout.addWidget(workspace_toolbar)

        task_panel = QFrame()
        task_panel.setObjectName("TaskPanel")
        task_layout = QGridLayout(task_panel)
        task_layout.setContentsMargins(16, 12, 16, 12)
        task_layout.setHorizontalSpacing(10)
        task_layout.setVerticalSpacing(7)

        heading = QLabel("开始一个编码任务")
        heading.setObjectName("SectionTitle")
        task_layout.addWidget(heading, 0, 0, 1, 1)
        description = QLabel("描述目标，Agent 会先阅读项目，再通过工具修改代码并保留 Git 轨迹。")
        description.setObjectName("Subtle")
        task_layout.addWidget(description, 0, 1, 1, 3)

        goal_label = self._field_label("目标")
        task_layout.addWidget(goal_label, 1, 0)
        self.task_goal = PromptEdit()
        self.task_goal.setPlaceholderText("例如：给现有项目增加配置文件读取功能")
        self.task_goal.setFixedHeight(52)
        self.task_goal.send_requested.connect(self.submit_questionnaire)
        task_layout.addWidget(self.task_goal, 1, 1, 1, 3)

        task_layout.addWidget(self._field_label("范围"), 2, 0)
        self.task_scope = QLineEdit()
        self.task_scope.setPlaceholderText("例如：只修改 Python 源码，不新增第三方依赖")
        task_layout.addWidget(self.task_scope, 2, 1, 1, 1)
        task_layout.addWidget(self._field_label("验收"), 2, 2)
        self.task_verify = QLineEdit()
        self.task_verify.setPlaceholderText("例如：运行现有单元测试")
        task_layout.addWidget(self.task_verify, 2, 3)

        start = QPushButton("开始编码  Ctrl+Enter")
        start.setObjectName("Primary")
        start.setMinimumWidth(145)
        start.clicked.connect(self.submit_questionnaire)
        task_layout.addWidget(start, 3, 3, 1, 1, Qt.AlignmentFlag.AlignRight)
        task_layout.setColumnStretch(1, 1)
        task_layout.setColumnStretch(3, 1)
        layout.addWidget(task_panel)

        editor_toolbar = QFrame()
        editor_toolbar.setObjectName("EditorToolbar")
        editor_toolbar.setFixedHeight(34)
        editor_layout = QHBoxLayout(editor_toolbar)
        editor_layout.setContentsMargins(14, 0, 8, 0)
        editor_label = QLabel("编辑器")
        editor_label.setObjectName("Overline")
        editor_layout.addWidget(editor_label)
        editor_layout.addStretch(1)
        save = QPushButton("保存  Ctrl+S")
        save.setObjectName("Quiet")
        save.clicked.connect(self.save_current_file)
        editor_layout.addWidget(save)
        layout.addWidget(editor_toolbar)

        self.editor_tabs = QTabWidget()
        self.editor_tabs.setDocumentMode(True)
        self.editor_tabs.setTabsClosable(False)
        self.code_editor = CodeEditor()
        self.highlighter = PythonHighlighter(self.code_editor.document())
        self.editor_tabs.addTab(self.code_editor, "欢迎页")
        layout.addWidget(self.editor_tabs, 1)
        self._show_welcome()
        self.code_editor.document().modificationChanged.connect(lambda _changed: self._update_editor_tab())
        self.code_editor.installEventFilter(self)
        return workspace

    def _build_chat_panel(self) -> QWidget:
        chat = QFrame()
        chat.setObjectName("ChatPane")
        chat.setMinimumWidth(360)
        chat.setMaximumWidth(520)
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
        layout.addWidget(header)

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
        mode = QLabel("Agent")
        mode.setObjectName("Chip")
        composer_top.addWidget(mode)
        retry = QLabel("网络重试已开启")
        retry.setObjectName("Hint")
        composer_top.addWidget(retry)
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
        send = QPushButton("发送  ↵")
        send.setObjectName("Primary")
        send.clicked.connect(self.submit_chat)
        bottom.addWidget(send)
        composer_layout.addLayout(bottom)
        layout.addWidget(composer)
        return chat

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Subtle")
        label.setMinimumWidth(44)
        return label

    def _provider_state(self) -> str:
        key = os.getenv("SCIDEV_API_KEY") or os.getenv("OPENAI_API_KEY")
        model = os.getenv("SCIDEV_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5"
        return f"● {model}" if key else "● LLM 未配置"

    def _emit_agent(self, kind: str, data: dict[str, Any]) -> None:
        if not self._closing:
            self.signals.agent_event.emit(kind, data)

    def _emit_worker(self, kind: str, data: dict[str, Any]) -> None:
        if not self._closing:
            self.signals.worker_event.emit(kind, data)

    def _handle_worker_event(self, event: str, payload: dict[str, Any]) -> None:
        task_id = payload.get("task_id", "")
        if event == "task_started" and task_id == self.active_task_id:
            self._set_state(self.chat_status, "工作中 · Agent 正在修改项目", "StateWorking")
            self._set_state(self.workspace_state, "● 工作中", "StateWorking")
        elif event == "task_retry" and task_id == self.active_task_id:
            self._set_state(self.chat_status, "等待网络 · 自动重试中", "StateWorking")
            self._set_state(self.workspace_state, "● 重试中", "StateWorking")
        elif event == "task_completed" and task_id == self.active_task_id:
            self.active_task_id = None
            self._set_state(self.chat_status, "就绪 · 可以继续对话", "StateReady")
            self._set_state(self.workspace_state, "● 就绪", "StateReady")
        elif event == "task_failed" and task_id == self.active_task_id:
            self.active_task_id = None
            self._set_state(self.chat_status, "任务失败 · 可从历史重试", "StateError")
            self._set_state(self.workspace_state, "● 出错", "StateError")
        self._append_log(self._worker_message(event, payload))
        self.refresh_git_status()
        self.refresh_task_history()

    @staticmethod
    def _set_state(widget: QLabel, text: str, name: str) -> None:
        widget.setText(text)
        widget.setObjectName(name)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _handle_agent_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "model_call_started":
            self._append_log(f"正在请求模型 · 第 {payload['turn']} 轮")
        elif event == "assistant":
            self._append_chat("Agent", payload.get("text", ""), "agent")
        elif event == "tool_started":
            args = json.dumps(payload.get("arguments", {}), ensure_ascii=False)
            self._append_chat(f"工具 · {payload.get('name', '')}", args, "tool")
        elif event == "tool_result":
            result = str(payload.get("result", ""))
            self._append_chat(f"结果 · {payload.get('name', '')}", result[-1400:], "tool")
        elif event == "completed":
            sha = payload.get("git_result_sha") or "无新提交"
            self._append_chat("系统", f"任务完成 · Git commit: {sha[:12]}", "meta")
            self.refresh_git_status()

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

    def refresh_all(self) -> None:
        self.refresh_git_status()
        self.connection_label.setText(self._provider_state())
        self.refresh_task_history()

    def refresh_git_status(self) -> None:
        status = self.git.status().replace("\n", "  ")
        self.status_branch.setText(f"Git · {status[:80]}")

    def _on_file_selected(self, index: QModelIndex) -> None:
        source_index = self.file_proxy.mapToSource(index)
        path = Path(self.file_model.filePath(source_index))
        if path.is_file():
            self._open_file(path)

    def _open_file(self, path: Path) -> None:
        try:
            raw = path.read_bytes()
            if len(raw) > CodingToolbox.MAX_READ_BYTES or b"\x00" in raw:
                self._show_editor_text(f"无法在编辑器中打开此文件：\n{path.name}\n\n文件过大或为二进制文件。")
            else:
                self._show_editor_text(raw.decode("utf-8", errors="replace"))
            self.current_file = path.resolve()
            relative = path.relative_to(self.project_root).as_posix()
            self.editor_tabs.setTabText(0, f"{path.name}")
            self.breadcrumb.setText(f"项目  /  {relative}")
            self._append_log(f"已打开 {relative}")
        except OSError as exc:
            self._show_editor_text(f"读取失败：{exc}")

    def _show_welcome(self) -> None:
        self.current_file = None
        if hasattr(self, "editor_tabs"):
            self.editor_tabs.setTabText(0, "欢迎页")
        if hasattr(self, "breadcrumb"):
            self.breadcrumb.setText("项目  /  新建编码任务")
        if hasattr(self, "code_editor"):
            self._show_editor_text(
                "SciDevHarness Coding Workspace\n"
                "────────────────────────────────────────\n\n"
                "在上方填写编码任务，或在右侧直接输入指令。\n\n"
                "Agent 会先读取项目，再通过工具修改代码；网络暂时不可用时，任务会自动排队重试。\n\n"
                "快捷键：Ctrl + Enter 发送任务    Ctrl + S 保存当前文件"
            )

    def _show_editor_text(self, text: str) -> None:
        self.code_editor.setPlainText(text)
        self.code_editor.document().setModified(False)

    def _update_editor_tab(self) -> None:
        if self.current_file is None:
            return
        name = self.current_file.name
        if self.code_editor.document().isModified():
            name = "● " + name
        self.editor_tabs.setTabText(0, name)

    def save_current_file(self) -> None:
        if self.current_file is None or not self.current_file.is_relative_to(self.project_root):
            self._append_log("当前没有可保存的项目文件")
            return
        try:
            relative = self.current_file.relative_to(self.project_root).as_posix()
            result = self.toolbox.write_file(relative, self.code_editor.toPlainText())
            self.code_editor.document().setModified(False)
            self._append_log(result)
            self.refresh_git_status()
        except Exception as exc:  # noqa: BLE001
            self._show_message("保存失败", str(exc))

    def submit_questionnaire(self) -> None:
        goal = self.task_goal.toPlainText().strip()
        scope = self.task_scope.text().strip()
        verify = self.task_verify.text().strip()
        if not goal:
            self.task_goal.setFocus()
            return
        prompt = f"编码目标：{goal}"
        if scope:
            prompt += f"\n范围与约束：{scope}"
        if verify:
            prompt += f"\n验收标准：{verify}"
        self._submit_prompt(prompt)

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
        self.active_task_id = self.worker.submit(
            "coding",
            {"session_id": self.current_session_id, "prompt": prompt},
            max_attempts=6,
        )
        self._set_state(self.chat_status, "排队中 · 等待 Agent", "StateWorking")
        self._set_state(self.workspace_state, "● 排队中", "StateWorking")
        self._append_log(f"编码任务已加入队列：{self.active_task_id}")

    def new_coding_task(self) -> None:
        if self.active_task_id:
            self._append_chat("系统", "当前任务仍在执行，暂时不能新建会话。", "meta")
            return
        self.current_session_id = None
        self._show_welcome()
        self.task_goal.clear()
        self.task_scope.clear()
        self.task_verify.clear()
        self._append_chat("系统", "已开始新会话。", "meta")
        self.task_goal.setFocus()

    def show_tasks(self) -> None:
        if self.task_dialog is not None and self.task_dialog.isVisible():
            self.task_dialog.raise_()
            self.task_dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("任务历史 · SciDevHarness")
        dialog.resize(780, 430)
        self.task_dialog = dialog
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(self._dialog_title_bar(dialog, "编码任务历史"))
        tree = QTreeWidget()
        tree.setHeaderLabels(["类型", "状态", "尝试", "错误 / 详情"])
        tree.setColumnWidth(0, 100)
        tree.setColumnWidth(1, 110)
        tree.setColumnWidth(2, 70)
        tree.setColumnWidth(3, 440)
        self.task_history_tree = tree
        layout.addWidget(tree, 1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        retry = QPushButton("手动重试")
        retry.setObjectName("Primary")
        retry.clicked.connect(self.retry_selected)
        actions.addWidget(retry)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.close)
        actions.addWidget(close)
        layout.addLayout(actions)
        dialog.finished.connect(lambda _result: self._clear_task_dialog(dialog))
        self.refresh_task_history()
        dialog.show()

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
        dialog = QDialog(self)
        dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("Git 状态 · SciDevHarness")
        dialog.resize(780, 520)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(self._dialog_title_bar(dialog, "Git 工作区"))
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(
            f"仓库：{self.project_root}\n"
            f"分支：{self.git.branch()}\n"
            f"当前提交：{self.git.head_sha() or '暂无'}\n\n"
            f"工作区：\n{self.git.status()}\n\n"
            f"提交记录：\n{self.git.log()}"
        )
        layout.addWidget(text, 1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        init = QPushButton("初始化 Git")
        init.setObjectName("Primary")
        init.clicked.connect(self.init_git)
        actions.addWidget(init)
        layout.addLayout(actions)
        dialog.show()

    def init_git(self) -> None:
        try:
            self.git.init()
            self._append_log("Git 已准备好；编码任务完成后会自动提交。")
            self.refresh_git_status()
        except Exception as exc:  # noqa: BLE001
            self._show_message("Git 错误", str(exc))

    def focus_explorer(self) -> None:
        self.file_tree.setFocus()

    def _handle_coding(self, task: dict[str, Any]) -> dict[str, Any]:
        return self.agent.run(task)

    def eventFilter(self, watched: QObject, event) -> bool:  # noqa: ANN001 - Qt event signature.
        if watched is self.code_editor and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_S and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.save_current_file()
                return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        if self._closing:
            event.accept()
            return
        self._closing = True
        self.worker.stop()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SciDevHarness")
    app.setApplicationDisplayName("SciDevHarness")
    ui_font = QFont("Segoe UI", 10)
    ui_font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(ui_font)
    # Use the application directory so a shortcut or double-click never
    # accidentally opens the current shell directory as the project.
    window = ClientWindow(Path(__file__).resolve().parent)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
