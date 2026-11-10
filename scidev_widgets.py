"""Small reusable Qt widgets."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class ElidedLabel(QLabel):
    """Keep long workspace names readable without letting them push out controls."""

    def __init__(self, full_text: str, parent: QWidget | None = None):
        super().__init__(full_text, parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.full_text = str(full_text)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setAccessibleName(self.full_text)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        if hasattr(self, "full_text"):
            hint.setWidth(max(hint.width(), self.fontMetrics().horizontalAdvance(self.full_text) + 2))
        return hint

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        super().resizeEvent(event)
        self.setText(self.full_text)

    def changeEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, "full_text"):
            self.setText(self.full_text)

    def setText(self, text: str) -> None:
        self.full_text = str(text)
        self.setAccessibleName(self.full_text)
        available_width = max(0, self.contentsRect().width())
        if available_width == 0:
            QLabel.setText(self, self.full_text)
            return
        visible_text = self.fontMetrics().elidedText(
            self.full_text,
            Qt.TextElideMode.ElideRight,
            max(0, available_width - 2),
        )
        if visible_text != self.text():
            QLabel.setText(self, visible_text)
