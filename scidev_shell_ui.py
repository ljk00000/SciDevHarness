"""Workspace explorer proxy and frameless title bar."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QModelIndex, QObject, QPoint, QSortFilterProxyModel, QTimer, Qt
from PySide6.QtWidgets import QFrame, QMainWindow, QWidget

from scidev_core import CodingToolbox
from scidev_paths import _is_link_or_junction

_EXCLUDED_PROJECT_NAMES_CASEFOLD = frozenset(
    name.casefold() for name in CodingToolbox.EXCLUDED_NAMES
)


class ProjectFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._filter_text = ""
        self._indexed_root: str | None = None
        self._indexed_files: tuple[tuple[str, str], ...] = ()
        self._candidate_files: tuple[tuple[str, str], ...] | None = None
        self._visible_path_keys: set[str] | None = None
        self._search_refresh_timer = QTimer(self)
        self._search_refresh_timer.setSingleShot(True)
        self._search_refresh_timer.setInterval(150)
        self._search_refresh_timer.timeout.connect(self._refresh_active_filter)

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(os.path.normpath(os.fspath(path)))

    def invalidate_search_index(self) -> None:
        self._indexed_root = None
        self._indexed_files = ()
        self._candidate_files = None

    def source_paths_changed(self, *_args: object) -> None:
        self.invalidate_search_index()
        if self._filter_text:
            self._search_refresh_timer.start()

    def _refresh_active_filter(self) -> None:
        self.set_filter_text(self._filter_text)

    def _ensure_search_index(self) -> Path | None:
        model = self.sourceModel()
        root_path_method = getattr(model, "rootPath", None)
        if not callable(root_path_method):
            self._indexed_root = None
            self._indexed_files = ()
            return None

        root = Path(root_path_method())
        root_key = self._path_key(root)
        if root_key != self._indexed_root:
            indexed_files: list[tuple[str, str]] = []
            for current, directories, filenames in os.walk(root, topdown=True):
                current_path = Path(current)
                directories[:] = [
                    name
                    for name in directories
                    if name.casefold() not in _EXCLUDED_PROJECT_NAMES_CASEFOLD
                    and not _is_link_or_junction(current_path / name)
                ]
                for name in filenames:
                    path = current_path / name
                    if _is_link_or_junction(path):
                        continue
                    try:
                        relative_name = path.relative_to(root).as_posix().casefold()
                    except ValueError:
                        continue
                    indexed_files.append((self._path_key(path), relative_name))
            self._indexed_root = root_key
            self._indexed_files = tuple(indexed_files)
            self._candidate_files = None
        return root

    def _visible_paths_for_query(self, query: str) -> set[str]:
        root = self._ensure_search_index()
        if root is None:
            self._candidate_files = ()
            return set()
        visible: set[str] = set()
        if self._candidate_files is not None and query.startswith(self._filter_text):
            candidates = self._candidate_files
        else:
            candidates = self._indexed_files
        matching_files = tuple(
            (raw_file_path, relative_name)
            for raw_file_path, relative_name in candidates
            if query in relative_name
        )
        self._candidate_files = matching_files
        for raw_file_path, _relative_name in matching_files:
            current = Path(raw_file_path)
            while True:
                current_key = self._path_key(current)
                visible.add(current_key)
                parent = current.parent
                if parent == current:
                    break
                current = parent
        return visible

    def matching_directory_paths(self) -> tuple[Path, ...]:
        """Return parent folders of the current file matches, shallowest first."""
        root = self._ensure_search_index()
        if root is None or not self._filter_text or self._candidate_files is None:
            return ()

        directories: dict[str, Path] = {}
        for raw_file_path, _relative_name in self._candidate_files:
            current = Path(raw_file_path).parent
            while current != root:
                try:
                    current.relative_to(root)
                except ValueError:
                    break
                directories[self._path_key(current)] = current
                current = current.parent

        return tuple(
            sorted(directories.values(), key=lambda path: (len(path.parts), os.fspath(path).casefold()))
        )

    def set_filter_text(self, text: str) -> None:
        self._search_refresh_timer.stop()
        filter_text = text.strip().casefold()
        visible_path_keys = self._visible_paths_for_query(filter_text) if filter_text else None
        if not filter_text:
            self._candidate_files = None
        begin_change = getattr(self, "beginFilterChange", None)
        end_change = getattr(self, "endFilterChange", None)
        if callable(begin_change) and callable(end_change):
            begin_change()
            self._filter_text = filter_text
            self._visible_path_keys = visible_path_keys
            end_change(QSortFilterProxyModel.Direction.Rows)
            return

        self._filter_text = filter_text
        self._visible_path_keys = visible_path_keys
        invalidate_rows = getattr(self, "invalidateRowsFilter", None)
        if callable(invalidate_rows):
            invalidate_rows()
        else:
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        index = model.index(source_row, 0, source_parent)
        name = model.fileName(index)
        path = Path(model.filePath(index))
        if _is_link_or_junction(path):
            return False
        if name.casefold() in _EXCLUDED_PROJECT_NAMES_CASEFOLD:
            return False
        if not self._filter_text:
            return True
        return self._path_key(path) in (self._visible_path_keys or set())


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
