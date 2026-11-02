"""Small GUI surface for the release check; it never installs anything."""
from __future__ import annotations

from pathlib import Path
import threading
from typing import Callable

from PySide6.QtCore import QStandardPaths, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from scidev_update import UpdateError, UpdateInfo, check_for_update, detect_repo, download_asset


class _DownloadWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, info: UpdateInfo, destination: Path, overwrite: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self.info = info
        self.destination = destination
        self.overwrite = overwrite
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        try:
            result = download_asset(
                self.info,
                self.destination,
                overwrite=self.overwrite,
                timeout=120.0,
                cancel=self._cancelled.is_set,
            )
        except (UpdateError, OSError, ValueError) as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit(str(result.path))


class _CheckWorker(QThread):
    checked = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        workspace: Path,
        checker: Callable[..., UpdateInfo] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.workspace = Path(workspace)
        self.checker = checker or check_for_update

    def run(self) -> None:
        try:
            repo = detect_repo(self.workspace)
            info = self.checker(repo=repo)
        except Exception as exc:  # noqa: BLE001 - surface any check failure to the dialog
            self.failed.emit(str(exc))
        else:
            self.checked.emit(info)


class UpdateDialog(QDialog):
    def __init__(self, info: UpdateInfo, parent: QWidget | None = None):
        super().__init__(parent)
        self.info = info
        self._worker: _DownloadWorker | None = None
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
        self.download_button = QPushButton("下载并校验安装包")
        self.download_button.setEnabled(bool(info.asset_url and info.asset_name))
        self.download_button.clicked.connect(self._choose_download)
        layout.addWidget(self.download_button)
        self.cancel_button = QPushButton("取消下载")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_download)
        layout.addWidget(self.cancel_button)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _open_release(self) -> None:
        if self.info.url:
            QDesktopServices.openUrl(QUrl(self.info.url))

    def _choose_download(self) -> None:
        if not self.info.asset_url or not self.info.asset_name:
            return
        downloads = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation,
        )
        default = Path(downloads or str(Path.home())) / self.info.asset_name
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "保存并校验安装包",
            str(default),
            "安装包 (*.exe *.msi);;所有文件 (*)",
        )
        if not selected:
            return
        destination = Path(selected)
        overwrite = destination.exists()
        if overwrite:
            answer = QMessageBox.question(
                self,
                "覆盖已有文件",
                f"目标已存在，是否覆盖？\n{destination}",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._start_download(destination, overwrite)

    def _start_download(self, destination: Path, overwrite: bool) -> None:
        self.download_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.buttons.setEnabled(False)
        self.status_label.setText("下载中并校验 SHA-256；完成前不会执行安装包。")
        worker = _DownloadWorker(self.info, destination, overwrite, self)
        self._worker = worker
        worker.completed.connect(self._download_completed)
        worker.failed.connect(self._download_failed)
        worker.finished.connect(self._download_finished)
        worker.start()

    def _download_completed(self, path: str) -> None:
        self.status_label.setText(f"已下载并校验：{path}")

    def _download_failed(self, message: str) -> None:
        if message == "下载已取消":
            self.status_label.setText("已取消下载，未保存文件。")
            return
        self.status_label.setText("下载失败，未执行任何文件。")
        QMessageBox.warning(self, "下载失败", message)

    def _cancel_download(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        self.cancel_button.setEnabled(False)
        self.status_label.setText("正在取消下载…")

    def _download_finished(self) -> None:
        self._worker = None
        self.download_button.setEnabled(bool(self.info.asset_url and self.info.asset_name))
        self.open_button.setEnabled(bool(self.info.url))
        self.cancel_button.setVisible(False)
        self.buttons.setEnabled(True)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "正在下载", "下载和校验完成前不能关闭这个窗口。")
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "正在下载", "下载和校验完成前不能关闭这个窗口。")
            return
        super().reject()

    @classmethod
    def show_for_workspace_async(
        cls,
        workspace: Path,
        parent: QWidget | None = None,
        *,
        checker: Callable[..., UpdateInfo] | None = None,
    ) -> _CheckWorker | None:
        """Check in the background so the GUI thread keeps repainting."""
        app = QApplication.instance()
        if app is None:
            return None
        progress = QProgressDialog("正在检查 GitHub Release…", "取消", 0, 0, parent)
        progress.setWindowTitle("检查更新")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.show()
        worker = _CheckWorker(Path(workspace), checker, app)
        state = {"cancelled": False}

        def finish(info: UpdateInfo) -> None:
            if state["cancelled"]:
                return
            progress.close()
            dialog = cls(info, parent)
            dialog.exec()

        def fail(message: str) -> None:
            if state["cancelled"]:
                return
            progress.close()
            QMessageBox.warning(parent, "检查更新失败", message)

        def cancelled() -> None:
            state["cancelled"] = True
            progress.close()

        def finished() -> None:
            if getattr(app, "_scidev_update_check_worker", None) is worker:
                delattr(app, "_scidev_update_check_worker")
            if getattr(app, "_scidev_update_check_progress", None) is progress:
                delattr(app, "_scidev_update_check_progress")
            worker.deleteLater()

        progress.canceled.connect(cancelled)
        worker.checked.connect(finish)
        worker.failed.connect(fail)
        worker.finished.connect(finished)
        app._scidev_update_check_worker = worker  # type: ignore[attr-defined]
        app._scidev_update_check_progress = progress  # type: ignore[attr-defined]
        worker.start()
        return worker

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
