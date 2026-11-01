"""Desktop pull-request viewer with a bounded diff and review actions."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from scidev_pr import PullRequestError, pull_request_diff, review_pull_request, view_pull_request


class PRLoadThread(QThread):
    loaded = Signal(object, object, str)

    def __init__(self, workspace: Path, number: int, allow_network: bool, parent=None):
        super().__init__(parent)
        self.workspace = Path(workspace)
        self.number = int(number)
        self.allow_network = bool(allow_network)

    def run(self) -> None:
        try:
            view = view_pull_request(
                self.workspace, self.number, allow_network=self.allow_network,
            )
            diff = pull_request_diff(
                self.workspace, self.number, allow_network=self.allow_network,
            )
        except (PullRequestError, OSError, ValueError) as exc:
            self.loaded.emit({}, {}, str(exc))
            return
        self.loaded.emit(view, diff, "")


class PullRequestDialog(QDialog):
    def __init__(self, workspace: Path, parent=None):
        super().__init__(parent)
        self.workspace = Path(workspace)
        self._thread: PRLoadThread | None = None
        self._loaded_number = 0
        self.setWindowTitle("GitHub PR")
        self.setMinimumSize(780, 560)

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("PR 编号"))
        self.number = QSpinBox()
        self.number.setRange(1, 1_000_000_000)
        self.number.setValue(1)
        top.addWidget(self.number)
        self.allow_network = QCheckBox("允许网络（gh）")
        top.addWidget(self.allow_network)
        self.load_button = QPushButton("加载")
        self.load_button.clicked.connect(self.load)
        top.addWidget(self.load_button)
        top.addStretch(1)
        layout.addLayout(top)

        self.metadata = QLabel("尚未加载")
        self.metadata.setWordWrap(True)
        layout.addWidget(self.metadata)

        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.diff.setFont(fixed)
        layout.addWidget(self.diff, 1)

        self.comment = QPlainTextEdit()
        self.comment.setPlaceholderText("review 备注（comment / request-changes 必填）")
        self.comment.setMaximumHeight(82)
        layout.addWidget(self.comment)

        actions = QHBoxLayout()
        self.approve_button = QPushButton("批准")
        self.approve_button.clicked.connect(lambda: self.review("approve"))
        self.comment_button = QPushButton("评论")
        self.comment_button.clicked.connect(lambda: self.review("comment"))
        self.changes_button = QPushButton("请求修改")
        self.changes_button.clicked.connect(lambda: self.review("request-changes"))
        actions.addWidget(self.approve_button)
        actions.addWidget(self.comment_button)
        actions.addWidget(self.changes_button)
        actions.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        actions.addWidget(buttons)
        layout.addLayout(actions)
        self._set_actions_enabled(False)

    def _set_actions_enabled(self, enabled: bool) -> None:
        for button in (self.approve_button, self.comment_button, self.changes_button):
            button.setEnabled(enabled)

    def load(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        self.load_button.setEnabled(False)
        self.metadata.setText("正在加载 PR…")
        self.diff.clear()
        self._set_actions_enabled(False)
        thread = PRLoadThread(
            self.workspace,
            self.number.value(),
            self.allow_network.isChecked(),
            self,
        )
        thread.loaded.connect(self._render_loaded)
        thread.finished.connect(self._thread_finished)
        self._thread = thread
        thread.start()

    def _thread_finished(self) -> None:
        self.load_button.setEnabled(True)
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.deleteLater()

    def _render_loaded(self, view: object, diff: object, error: str) -> None:
        if error:
            self.metadata.setText(f"加载失败：{error}")
            self._set_actions_enabled(False)
            return
        view_data = view if isinstance(view, dict) else {}
        diff_data = diff if isinstance(diff, dict) else {}
        number = int(view_data.get("number") or self.number.value())
        title = str(view_data.get("title") or "")
        state = str(view_data.get("state") or "")
        branch = str(view_data.get("headRefName") or "")
        base = str(view_data.get("baseRefName") or "")
        self.metadata.setText(
            f"#{number} {title} · {state} · {branch} → {base} · "
            f"{'diff 已截断' if diff_data.get('truncated') else 'diff 完整'}"
        )
        self.diff.setPlainText(str(diff_data.get("diff") or ""))
        self._loaded_number = number
        self._set_actions_enabled(True)

    def review(self, action: str) -> None:
        if self._loaded_number <= 0:
            return
        body = self.comment.toPlainText().strip()
        if action in {"comment", "request-changes"} and not body:
            self.metadata.setText(f"{action} 需要填写备注")
            return
        try:
            result = review_pull_request(
                self.workspace,
                self._loaded_number,
                action,
                body=body,
                allow_network=self.allow_network.isChecked(),
            )
        except PullRequestError as exc:
            self.metadata.setText(f"提交失败：{exc}")
            return
        self.metadata.setText(f"已提交 {result.get('action')} · {result.get('url')}")
