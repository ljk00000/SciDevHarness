"""Qt bridge for the resident language server used by the desktop client."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from scidev_lsp import ResidentLSP


class ResidentLSPBridge(QObject):
    diagnostics_ready = Signal(str, int, object, str)

    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self._resident = ResidentLSP(root, self._deliver)
        self._resident.start()

    def _deliver(self, path: Path, generation: int, diagnostics: object, error: str) -> None:
        try:
            self.diagnostics_ready.emit(str(path), generation, diagnostics, error)
        except RuntimeError:
            # The window closed while the resident LSP thread was finishing.
            return

    def submit(self, path: Path, text: str, generation: int) -> None:
        self._resident.submit(path, text, generation)

    def close(self) -> None:
        self._resident.close()
