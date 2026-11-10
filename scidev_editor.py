"""Code editor widget with line numbers and completion support."""
from __future__ import annotations

import re

from PySide6.QtCore import QRect, QSize, QStringListModel, Qt
from PySide6.QtGui import QColor, QFont, QKeyEvent, QPainter, QTextCursor, QTextFormat
from PySide6.QtWidgets import QCompleter, QPlainTextEdit, QTextEdit, QWidget

from scidev_preferences import AppearancePreferences
from scidev_ui import DEFAULT_UI_PROFILE, editor_font_family, ui_profile_for_key


class LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):  # noqa: ANN201 - Qt API has no useful narrow type here.
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):  # noqa: ANN001 - Qt event signature.
        self.editor.line_number_area_paint_event(event)


class CodeEditor(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setObjectName("CodeEditor")
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._ui_scale = 1.0
        self.appearance_preferences = AppearancePreferences()
        editor_font = QFont("Consolas", 12)
        editor_font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(editor_font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.completer: QCompleter | None = None
        self.completion_model: QStringListModel | None = None
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

    def set_ui_scale(self, ui_scale: float) -> None:
        """Scale the fixed-pitch editor font with the rest of the workbench."""
        self._ui_scale = min(1.5, max(0.9, float(ui_scale)))
        preferences = self.appearance_preferences
        editor_font = QFont(editor_font_family(preferences.editor_font_family))
        editor_font.setStyleHint(QFont.StyleHint.Monospace)
        editor_font.setPointSizeF(preferences.editor_font_size * self._ui_scale)
        self.setFont(editor_font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * preferences.editor_tab_size)

    def set_appearance(self, preferences: AppearancePreferences) -> None:
        self.appearance_preferences = preferences
        self.set_ui_scale(self._ui_scale)
        if not self.property("welcomeMode"):
            self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth if preferences.editor_wrap else QPlainTextEdit.LineWrapMode.NoWrap)
        self.line_number_area.setVisible(preferences.line_numbers and not self.property("welcomeMode"))
        self.update_line_number_area_width(0)
        self.highlight_current_line()

    def _ensure_completer(self) -> QCompleter:
        if self.completer is None:
            self.completion_model = QStringListModel(self)
            self.completer = QCompleter(self)
            self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            self.completer.setModel(self.completion_model)
            self.completer.setWidget(self)
            self.completer.activated.connect(self._insert_completion)
        return self.completer

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.show_completions()
            event.accept()
            return
        completer = self.completer
        popup = completer.popup() if completer is not None else None
        if completer is not None and popup is not None and popup.isVisible():
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                completion = completer.currentCompletion()
                if completion:
                    self._insert_completion(completion)
                    event.accept()
                    return
            if event.key() == Qt.Key.Key_Escape:
                popup.hide()
                event.accept()
                return
        super().keyPressEvent(event)
        if not self.isReadOnly() and (event.text().isalnum() or event.text() == "_"):
            self.show_completions()

    def show_completions(self) -> None:
        if self.isReadOnly():
            return
        completer = self._ensure_completer()
        popup = completer.popup()
        if popup is None:
            return
        model = self.completion_model
        assert model is not None
        cursor = self.textCursor()
        before_cursor = cursor.block().text()[:cursor.positionInBlock()]
        match = re.search(r"[A-Za-z_]\w*$", before_cursor)
        prefix = match.group(0) if match else ""
        if not prefix:
            popup.hide()
            return
        keywords = {
            "and", "as", "assert", "async", "await", "break", "case", "class", "continue",
            "def", "elif", "else", "except", "False", "finally", "for", "from", "global",
            "if", "import", "in", "is", "lambda", "None", "not", "or", "pass", "raise",
            "return", "True", "try", "while", "with", "yield",
        }
        words = set(re.findall(r"\b[A-Za-z_]\w*\b", self.toPlainText())) | keywords
        suggestions = sorted(word for word in words if word.casefold().startswith(prefix.casefold()) and word != prefix)
        model.setStringList(suggestions[:100])
        if not suggestions:
            popup.hide()
            return
        completer.setCompletionPrefix(prefix)
        popup.setCurrentIndex(model.index(0, 0))
        rect = self.cursorRect()
        rect.setWidth(min(360, max(180, popup.sizeHintForColumn(0) + 30)))
        completer.complete(rect)

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
        if self.completer is not None:
            popup = self.completer.popup()
            if popup is not None:
                popup.hide()

    def line_number_area_width(self) -> int:
        if self.property("welcomeMode") or not self.appearance_preferences.line_numbers:
            return 0
        digits = max(2, len(str(max(1, self.blockCount()))))
        return 16 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, _new_block_count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)
        rect = self.contentsRect()
        self.line_number_area.setGeometry(QRect(rect.left(), rect.top(), self.line_number_area_width(), rect.height()))

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
        if not self.appearance_preferences.highlight_line:
            self.setExtraSelections([])
            return
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
