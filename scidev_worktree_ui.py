"""Small Qt browser for project workloads created by WorktreeManager."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from scidev_worktree import AgentWorktree, WorktreeError, WorktreeManager


class WorktreeDialog(QDialog):
    open_requested = Signal(str)

    def __init__(
        self,
        manager: WorktreeManager,
        current_root: Path,
        parent=None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.current_root = Path(current_root).resolve()
        self.worktrees: list[AgentWorktree] = []
        self.setWindowTitle("隔离工作区")
        self.setMinimumSize(620, 320)
        layout = QVBoxLayout(self)

        hint = QLabel("每个 worktree 有独立文件和分支；删除前请确认改动已合并或不再需要。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["分支", "路径", "HEAD"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.open_button = QPushButton("打开")
        self.open_button.clicked.connect(self._open_selected)
        self.remove_button = QPushButton("删除")
        self.remove_button.clicked.connect(self._remove_selected)
        self.force_checkbox = QPushButton("强制删除")
        self.force_checkbox.setCheckable(True)
        self.force_checkbox.setToolTip("忽略未提交改动，删除 worktree 和分支")
        row.addWidget(self.open_button)
        row.addWidget(self.remove_button)
        row.addWidget(self.force_checkbox)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

        self.refresh()
        self._update_buttons()

    def refresh(self) -> None:
        self.table.setRowCount(0)
        self.worktrees = []
        try:
            self.worktrees = self.manager.list()
        except WorktreeError as exc:
            self.status.setText(str(exc))
            return
        for worktree in self.worktrees:
            row = self.table.rowCount()
            self.table.insertRow(row)
            branch = QTableWidgetItem(worktree.branch)
            branch.setData(Qt.ItemDataRole.UserRole, str(worktree.path))
            self.table.setItem(row, 0, branch)
            self.table.setItem(row, 1, QTableWidgetItem(str(worktree.path)))
            self.table.setItem(row, 2, QTableWidgetItem(worktree.base_sha[:12]))
        if not self.worktrees:
            self.status.setText("没有隔离工作区")
        else:
            self.status.setText(f"{len(self.worktrees)} 个隔离工作区")

    def selected_worktree(self) -> AgentWorktree | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.worktrees):
            return None
        return self.worktrees[row]

    def _update_buttons(self) -> None:
        selected = self.selected_worktree()
        self.open_button.setEnabled(selected is not None)
        self.remove_button.setEnabled(selected is not None)
        self.force_checkbox.setEnabled(selected is not None)

    def _open_selected(self) -> None:
        selected = self.selected_worktree()
        if selected is not None:
            self.open_requested.emit(str(selected.path))
            self.accept()

    def _remove_selected(self) -> None:
        selected = self.selected_worktree()
        if selected is None:
            return
        if selected.path == self.current_root:
            QMessageBox.warning(self, "不能删除", "当前窗口正在使用这个 worktree。")
            return
        if QMessageBox.question(
            self,
            "删除隔离工作区",
            f"删除 {selected.branch}？\n{selected.path}",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.remove_selected(force=self.force_checkbox.isChecked())

    def remove_selected(self, *, force: bool = False) -> bool:
        selected = self.selected_worktree()
        if selected is None:
            return False
        try:
            self.manager.remove(selected, force=force)
        except WorktreeError as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return False
        self.refresh()
        return True
