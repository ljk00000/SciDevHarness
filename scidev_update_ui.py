"""Small GUI surface for the release check; it never installs anything."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from scidev_update import UpdateError, UpdateInfo, check_for_update, detect_repo


class UpdateDialog(QDialog):
    def __init__(self, info: UpdateInfo, parent: QWidget | None = None):
        super().__init__(parent)
        self.info = info
        self.setWindowTitle("检查更新")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        if info.available:
            self.summary_label.setText(f"发现新版本：{info.current} → {info.latest}")
        else:
            self.summary_label.setText(f"已是最新版本：{info.current}")
        layout.addWidget(self.summary_label)
        self.detail_label = QLabel()
        self.detail_label.setWordWrap(True)
        details = []
        if info.published_at:
            details.append(f"发布时间：{info.published_at}")
        if info.url:
            details.append(info.url)
        if info.asset_name:
            details.append(f"安装包：{info.asset_name}（{info.asset_size} 字节）")
        if info.notes:
            details.append("")
            details.append(info.notes[:2000])
        self.detail_label.setText("\n".join(details) or "没有可显示的 Release 详情。")
        layout.addWidget(self.detail_label)
        self.open_button = QPushButton("打开 Release 页面")
        self.open_button.setEnabled(bool(info.url))
        self.open_button.clicked.connect(self._open_release)
        layout.addWidget(self.open_button)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _open_release(self) -> None:
        if self.info.url:
            QDesktopServices.openUrl(QUrl(self.info.url))

    @classmethod
    def show_for_workspace(
        cls,
        workspace: Path,
        parent: QWidget | None = None,
        *,
        checker: Callable[..., UpdateInfo] | None = None,
    ) -> UpdateDialog | None:
        checker = checker or check_for_update
        try:
            repo = detect_repo(Path(workspace))
            info = checker(repo=repo)
        except UpdateError as exc:
            QMessageBox.warning(parent, "检查更新失败", str(exc))
            return None
        dialog = cls(info, parent)
        dialog.exec()
        return dialog
