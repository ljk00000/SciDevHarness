"""Inline and editor-side patch review widgets."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


class UnifiedDiffHighlighter(QSyntaxHighlighter):
    """Color added, removed and hunk-header lines in a read-only diff tab."""

    def __init__(self, document: QTextDocument):
        super().__init__(document)
        self.added_format = QTextCharFormat()
        self.added_format.setForeground(QColor("#8ce99a"))
        self.added_format.setBackground(QColor("#173a24"))
        self.removed_format = QTextCharFormat()
        self.removed_format.setForeground(QColor("#ffa8a8"))
        self.removed_format.setBackground(QColor("#3a1d20"))
        self.header_format = QTextCharFormat()
        self.header_format.setForeground(QColor("#8ecae6"))
        self.header_format.setFontWeight(QFont.Weight.Bold)

    def highlightBlock(self, text: str) -> None:
        if text.startswith(("+++", "---", "diff --git", "index ")):
            self.setFormat(0, len(text), self.header_format)
        elif text.startswith("@@"):
            self.setFormat(0, len(text), self.header_format)
        elif text.startswith("+"):
            self.setFormat(0, len(text), self.added_format)
        elif text.startswith("-"):
            self.setFormat(0, len(text), self.removed_format)


class PatchDiffPanel(QWidget):
    """Editor-side review surface for a pending patch.

    The chat card remains the compact entry point; this tab gives the same
    hunk decisions a larger, syntax-highlighted diff surface.
    """

    decision = Signal(object)

    def __init__(self, preview: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.preview = preview
        self._decided = False
        self.hunk_checks: dict[int, QCheckBox] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(7)

        hunks = [item for item in preview.get("hunks", []) if isinstance(item, dict)]
        header = QLabel(
            f"补丁审核 · {int(preview.get('file_count', 0) or 0)} 个文件 · {len(hunks)} 个修改块"
        )
        header.setObjectName("SectionTitle")
        layout.addWidget(header)
        note = QLabel("勾选要应用的块；应用后本次审核结束。也可以回到聊天卡片操作。")
        note.setObjectName("Hint")
        note.setWordWrap(True)
        layout.addWidget(note)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        for item in hunks:
            raw_id = item.get("id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
                continue
            try:
                hunk_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if hunk_id in self.hunk_checks:
                continue
            path = str(item.get("path") or "（未知路径）")
            check = QCheckBox(f"#{hunk_id} · {path}")
            check.setObjectName("PatchReviewHunk")
            check.toggled.connect(self._sync_selection)
            self.hunk_checks[hunk_id] = check
            left_layout.addWidget(check)
        left_layout.addStretch(1)
        splitter.addWidget(left)

        diff_text = "\n\n".join(
            f"### 修改块 {item.get('id')} · {item.get('path')} · {item.get('operation')}\n"
            f"{str(item.get('diff') or '[无文本差异]')}"
            for item in hunks
        )
        self.diff_editor = QPlainTextEdit()
        self.diff_editor.setReadOnly(True)
        self.diff_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff_editor.setPlainText(diff_text or "补丁没有可显示的差异")
        self._highlighter = UnifiedDiffHighlighter(self.diff_editor.document())
        splitter.addWidget(self.diff_editor)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.select_all_button = QPushButton("全选")
        self.select_all_button.setObjectName("Quiet")
        self.select_all_button.clicked.connect(lambda: self._set_all(True))
        actions.addWidget(self.select_all_button)
        actions.addStretch(1)
        self.apply_button = QPushButton("应用已选修改")
        self.apply_button.setObjectName("Primary")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._accept_selected)
        actions.addWidget(self.apply_button)
        self.reject_button = QPushButton("拒绝全部")
        self.reject_button.setObjectName("Quiet")
        self.reject_button.clicked.connect(self._reject_all)
        actions.addWidget(self.reject_button)
        layout.addLayout(actions)

    def selected_hunks(self) -> set[int]:
        return {hunk_id for hunk_id, check in self.hunk_checks.items() if check.isChecked()}

    def _set_all(self, checked: bool) -> None:
        for check in self.hunk_checks.values():
            check.setChecked(checked)

    def _sync_selection(self, _checked: bool = False) -> None:
        if not self._decided:
            self.apply_button.setEnabled(bool(self.selected_hunks()))

    def _accept_selected(self) -> None:
        selected = self.selected_hunks()
        if selected:
            self._finish(selected)

    def _reject_all(self) -> None:
        self._finish(None)

    def _finish(self, selected: set[int] | None) -> None:
        if self._decided:
            return
        self._decided = True
        for check in self.hunk_checks.values():
            check.setEnabled(False)
        self.select_all_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.reject_button.setEnabled(False)
        self.decision.emit(selected)


class PatchReviewCard(QFrame):
    """Inline, non-modal review surface for per-hunk code changes."""

    decision = Signal(object)
    open_requested = Signal(object)

    def __init__(self, preview: dict[str, Any]):
        super().__init__()
        self.setObjectName("PatchReviewCard")
        self.preview = preview
        self._decided = False
        self.hunk_checks: dict[int, QCheckBox] = {}

        hunks = [item for item in preview.get("hunks", []) if isinstance(item, dict)]
        file_count = int(preview.get("file_count", 0) or 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 11, 12, 11)
        layout.setSpacing(7)

        self.title_label = QLabel("审核代码补丁")
        self.title_label.setObjectName("PatchReviewTitle")
        layout.addWidget(self.title_label)
        self.summary_label = QLabel(
            f"{file_count} 个文件 · {len(hunks)} 个修改块。勾选要应用的块；拒绝或部分批准会停止当前任务。"
        )
        self.summary_label.setObjectName("PatchReviewSummary")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        hunk_scroll = QScrollArea()
        hunk_scroll.setObjectName("PatchReviewHunkScroll")
        hunk_scroll.setWidgetResizable(True)
        hunk_scroll.setFrameShape(QFrame.Shape.NoFrame)
        hunk_scroll.setMaximumHeight(170)
        hunk_container = QWidget()
        hunk_layout = QVBoxLayout(hunk_container)
        hunk_layout.setContentsMargins(2, 2, 2, 2)
        hunk_layout.setSpacing(4)

        diff_sections = []
        operation_names = {"update": "修改", "add": "新增", "delete": "删除"}
        for item in hunks:
            raw_id = item.get("id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
                continue
            try:
                hunk_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if hunk_id in self.hunk_checks:
                continue
            path = str(item.get("path") or "（未知路径）")
            operation = operation_names.get(str(item.get("operation") or ""), "变更")
            added = int(item.get("added_lines", 0) or 0)
            removed = int(item.get("removed_lines", 0) or 0)
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(1, 2, 1, 2)
            row_layout.setSpacing(7)
            check = QCheckBox()
            check.setObjectName("PatchReviewHunk")
            check.setAccessibleName(f"选择补丁块 {hunk_id}：{path}")
            check.setToolTip(f"选择是否应用此修改块：{path}")
            self.hunk_checks[hunk_id] = check
            check.toggled.connect(self._sync_selection)
            row_layout.addWidget(check, 0, Qt.AlignmentFlag.AlignTop)
            description = QLabel(f"#{hunk_id} · {path}\n{operation} · +{added} / −{removed} 行")
            description.setObjectName("PatchReviewHunkText")
            description.setTextFormat(Qt.TextFormat.PlainText)
            description.setWordWrap(True)
            row_layout.addWidget(description, 1)
            hunk_layout.addWidget(row)
            diff_sections.append(
                f"### 修改块 {hunk_id} · {path} · {operation}\n{str(item.get('diff') or '[无文本差异]')}"
            )
        hunk_layout.addStretch(1)
        hunk_scroll.setWidget(hunk_container)
        layout.addWidget(hunk_scroll)

        self.diff_view = QPlainTextEdit()
        self.diff_view.setObjectName("PatchReviewDiff")
        self.diff_view.setReadOnly(True)
        self.diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff_view.setPlaceholderText("补丁没有可显示的差异")
        self.diff_view.setPlainText("\n\n".join(diff_sections))
        self.diff_view.setMinimumHeight(110)
        self.diff_view.setMaximumHeight(210)
        layout.addWidget(self.diff_view)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self.open_diff_button = QPushButton("在编辑器中审核")
        self.open_diff_button.setObjectName("Quiet")
        self.open_diff_button.clicked.connect(lambda: self.open_requested.emit(self.preview))
        action_row.addWidget(self.open_diff_button)
        self.select_all_button = QPushButton("全选")
        self.select_all_button.setObjectName("Quiet")
        self.select_all_button.setEnabled(bool(self.hunk_checks))
        self.select_all_button.clicked.connect(lambda: self._set_all(True))
        action_row.addWidget(self.select_all_button)
        self.apply_button = QPushButton("应用已选修改")
        self.apply_button.setObjectName("Primary")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._accept_selected)
        action_row.addWidget(self.apply_button)
        self.reject_button = QPushButton("拒绝全部")
        self.reject_button.setObjectName("Quiet")
        self.reject_button.clicked.connect(self._reject_all)
        action_row.addWidget(self.reject_button)
        layout.addLayout(action_row)

    def selected_hunks(self) -> set[int]:
        return {hunk_id for hunk_id, check in self.hunk_checks.items() if check.isChecked()}

    def _set_all(self, checked: bool) -> None:
        for check in self.hunk_checks.values():
            check.setChecked(checked)

    def _sync_selection(self, _checked: bool = False) -> None:
        if not self._decided:
            self.apply_button.setEnabled(bool(self.selected_hunks()))
            self.select_all_button.setEnabled(bool(self.hunk_checks))

    def _accept_selected(self) -> None:
        selected = self.selected_hunks()
        if selected:
            self._finish(selected)

    def _reject_all(self) -> None:
        self._finish(None)

    def _finish(self, selected: set[int] | None) -> None:
        if self._decided:
            return
        self._decided = True
        count = len(selected or ())
        self.summary_label.setText(
            f"已批准 {count} 个修改块。" if selected is not None else "已拒绝全部修改。"
        )
        for check in self.hunk_checks.values():
            check.setEnabled(False)
        self.select_all_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.reject_button.setEnabled(False)
        self.decision.emit(selected)
