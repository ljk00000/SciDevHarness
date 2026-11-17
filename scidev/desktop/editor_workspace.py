"""Editor workspace behavior shared by the desktop client window."""

from __future__ import annotations

import ast
import io
import keyword
import re
import tokenize
from pathlib import Path

from PySide6.QtCore import QModelIndex, QObject, QPoint, Qt
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextDocumentLayout,
    QPlainTextEdit,
    QPushButton,
    QTabBar,
    QTabWidget,
    QToolButton,
    QTreeWidgetItem,
    QWidget,
)

from scidev.desktop.editor import CodeEditor
from scidev.desktop.highlighting import LanguageHighlighter
from scidev.workspace.intel import IntelUnavailable, PythonIntel
from scidev.integrations.lsp import language_spec
from scidev.desktop.lsp_ui import ResidentLSPBridge
from scidev.agent.toolbox import CodingToolbox


class EditorWorkspaceMixin:
    """Provide editor operations using the client window's existing state."""

    def _create_editor_group(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.setTabsClosable(True)
        tabs.setMovable(True)
        tabs.setUsesScrollButtons(True)
        tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tabs.tabBar().customContextMenuRequested.connect(
            lambda point, group=tabs: self._show_editor_tab_menu(point, group)
        )
        tabs.tabCloseRequested.connect(
            lambda index, group=tabs: self._close_editor_tab(index, group)
        )
        tabs.currentChanged.connect(
            lambda index, group=tabs: self._on_editor_tab_changed(index, group)
        )
        return tabs

    def _install_tab_close_button(self, group: QTabWidget, index: int, editor: QWidget) -> None:
        close = QToolButton(group.tabBar())
        close.setObjectName("TabCloseButton")
        close.setText("×")
        close.setToolTip("关闭标签")
        close.setFixedSize(20, 20)
        close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close.clicked.connect(
            lambda _checked=False, target_group=group, target_editor=editor: self._close_editor_tab(
                target_group.indexOf(target_editor), target_group
            )
        )
        group.tabBar().setTabButton(index, QTabBar.ButtonPosition.RightSide, close)

    def _editor_groups(self) -> list[QTabWidget]:
        groups = [self.editor_tabs]
        if self.secondary_editor_tabs is not None:
            groups.append(self.secondary_editor_tabs)
        return groups

    def _editor_group_for(self, editor: QWidget | None) -> QTabWidget | None:
        if editor is None:
            return None
        for group in self._editor_groups():
            if group.indexOf(editor) >= 0:
                return group
        return None

    def _split_current_editor(self) -> None:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if path is None:
            self._append_log("请先打开一个项目文件，再进行编辑器分栏")
            return
        if self.secondary_editor_tabs is None:
            self.secondary_editor_tabs = self._create_editor_group()
            self.editor_splitter.addWidget(self.secondary_editor_tabs)
            self.editor_splitter.setSizes([1, 1])
        self.secondary_editor_tabs.setVisible(True)
        self._open_file(path, preview=False, group=self.secondary_editor_tabs)
        self._append_log(f"已在右侧编辑器组打开 {path.name}")

    def _toggle_editor_split(self) -> None:
        if self.secondary_editor_tabs is not None and self.secondary_editor_tabs.isVisible():
            self.secondary_editor_tabs.setVisible(False)
            self._active_editor_group = self.editor_tabs
            self._on_editor_tab_changed(self.editor_tabs.currentIndex(), self.editor_tabs)
            editor = self._active_editor()
            if editor is not None:
                editor.setFocus()
            else:
                self.editor_tabs.setFocus()
            self._append_log("已关闭编辑器分栏")
            return
        self._split_current_editor()

    def _build_find_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("FindBar")
        bar.setVisible(False)
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 5, 8, 5)
        row.setSpacing(6)

        find_label = QLabel("查找")
        find_label.setObjectName("Overline")
        row.addWidget(find_label)
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("查找当前文件")
        self.find_input.setMinimumWidth(170)
        self.find_input.textChanged.connect(self._on_find_text_changed)
        self.find_input.returnPressed.connect(self._find_next)
        row.addWidget(self.find_input)

        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("替换为")
        self.replace_input.setMinimumWidth(150)
        self.replace_input.returnPressed.connect(self._replace_current)
        self.replace_input.setVisible(False)
        row.addWidget(self.replace_input)

        self.find_case_checkbox = QCheckBox("区分大小写")
        self.find_case_checkbox.toggled.connect(lambda _checked: self._on_find_text_changed(self.find_input.text()))
        row.addWidget(self.find_case_checkbox)
        self.find_word_checkbox = QCheckBox("全字匹配")
        self.find_word_checkbox.toggled.connect(lambda _checked: self._on_find_text_changed(self.find_input.text()))
        row.addWidget(self.find_word_checkbox)

        previous = QToolButton()
        previous.setObjectName("IconButton")
        previous.setText("↑")
        previous.setToolTip("上一个匹配")
        previous.clicked.connect(self._find_previous)
        row.addWidget(previous)
        following = QToolButton()
        following.setObjectName("IconButton")
        following.setText("↓")
        following.setToolTip("下一个匹配")
        following.clicked.connect(self._find_next)
        row.addWidget(following)

        replace = QPushButton("替换")
        replace.setObjectName("Quiet")
        replace.clicked.connect(self._replace_current)
        self.replace_button = replace
        row.addWidget(replace)
        replace_all = QPushButton("全部替换")
        replace_all.setObjectName("Quiet")
        replace_all.clicked.connect(self._replace_all)
        self.replace_all_button = replace_all
        row.addWidget(replace_all)

        self.find_status = QLabel("")
        self.find_status.setObjectName("FindStatus")
        row.addWidget(self.find_status)
        row.addStretch(1)
        close = QToolButton()
        close.setObjectName("IconButton")
        close.setText("×")
        close.setToolTip("关闭查找")
        close.clicked.connect(self._close_find_bar)
        row.addWidget(close)
        return bar

    def _configure_editor(self, editor: CodeEditor) -> None:
        editor.set_ui_profile(self.ui_profile)
        editor.set_appearance(self.appearance_preferences)
        editor.set_ui_scale(self._ui_scale)
        document = editor.document()
        highlighter = next(
            (
                existing
                for existing in self._editor_highlighters.values()
                if existing.document() is document
            ),
            None,
        )
        if highlighter is None:
            highlighter = LanguageHighlighter(document, self.ui_profile, self._editor_paths.get(editor))
        self._editor_highlighters[editor] = highlighter
        connection = editor.document().modificationChanged.connect(
            lambda _changed, target=editor: self._update_editor_tab_for(target)
        )
        self._editor_document_change_connections[editor] = connection
        editor.cursorPositionChanged.connect(
            lambda target=editor: self._update_cursor_status(target)
        )
        editor.textChanged.connect(self._schedule_code_intel)
        completion_shortcut = QShortcut(QKeySequence("Ctrl+Space"), editor)
        completion_shortcut.activated.connect(lambda target=editor: self._show_completions(target))
        definition_shortcut = QShortcut(QKeySequence("Ctrl+F12"), editor)
        definition_shortcut.activated.connect(lambda target=editor: self._goto_definition(target))
        editor.installEventFilter(self)

    def _completion_candidates(self, editor: CodeEditor) -> list:
        path = self._editor_paths.get(editor)
        if path is None or path.suffix.lower() != ".py":
            return []
        cursor = editor.textCursor()
        try:
            intel = PythonIntel(self.project_root)
            return intel.complete(
                editor.toPlainText(),
                cursor.blockNumber() + 1,
                cursor.positionInBlock(),
                path,
            )
        except (IntelUnavailable, OSError, ValueError) as exc:
            self._append_log(f"补全不可用：{exc}")
            return []

    def _show_completions(self, editor: CodeEditor) -> None:
        completions = self._completion_candidates(editor)
        if not completions:
            return
        popup = getattr(self, "_completion_popup", None)
        if popup is not None:
            popup.close()
        popup = QListWidget(self)
        popup.setWindowFlags(Qt.WindowType.Popup)
        popup.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        popup.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for completion in completions:
            label = completion.name
            if completion.kind:
                label += f"  · {completion.kind}"
            if completion.signature:
                label += f"  {completion.signature}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, completion.complete)
            item.setToolTip(completion.description or completion.signature)
            popup.addItem(item)
        popup.itemActivated.connect(lambda item: self._insert_completion(editor, item))
        popup.itemClicked.connect(lambda item: self._insert_completion(editor, item))
        rect = editor.cursorRect()
        popup.move(editor.viewport().mapToGlobal(rect.bottomLeft()))
        popup.resize(360, min(260, 28 * min(len(completions), 8) + 8))
        self._completion_popup = popup
        popup.show()
        popup.setFocus()
        popup.setCurrentRow(0)

    def _insert_completion(self, editor: CodeEditor, item: QListWidgetItem) -> None:
        popup = getattr(self, "_completion_popup", None)
        if popup is not None:
            popup.close()
            self._completion_popup = None
        completion = str(item.data(Qt.ItemDataRole.UserRole) or item.text().split()[0])
        cursor = editor.textCursor()
        cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        cursor.insertText(completion)
        editor.setTextCursor(cursor)

    def _goto_definition(self, editor: CodeEditor) -> None:
        path = self._editor_paths.get(editor)
        if path is None or path.suffix.lower() != ".py":
            return
        cursor = editor.textCursor()
        try:
            intel = PythonIntel(self.project_root)
            definitions = intel.goto(
                editor.toPlainText(),
                cursor.blockNumber() + 1,
                cursor.positionInBlock(),
                path,
            )
        except (IntelUnavailable, OSError, ValueError) as exc:
            self._append_log(f"跳转不可用：{exc}")
            return
        if not definitions:
            self._append_log("当前位置没有找到定义")
            return
        definition = definitions[0]
        try:
            definition.path.relative_to(self.project_root)
        except ValueError:
            self._append_log("定义位于工作区之外，未跳转")
            return
        self._open_file(definition.path, preview=False)
        target = self._active_editor()
        if target is not None:
            self._move_editor_to_location(target, definition.line, definition.column)
            target.setFocus()

    def _update_cursor_status(self, editor: CodeEditor | None = None) -> None:
        if not hasattr(self, "status_position"):
            return
        workspace_visible = (
            not hasattr(self, "workspace_stack")
            or self.workspace_stack.currentWidget() is self.workspace_page
        )
        target = (editor or self._active_editor()) if workspace_visible else None
        if target is None:
            self.status_position.setText("Ln 1, Col 1")
            self.status_position.hide()
            return
        path = self._editor_paths.get(target)
        if path is None and target.property("diff_path"):
            path = Path(str(target.property("diff_path")))
        self.status_position.setVisible(path is not None)
        if path is None:
            return
        cursor = target.textCursor()
        self.status_position.setText(f"Ln {cursor.blockNumber() + 1}, Col {cursor.columnNumber() + 1}")
        self.status_position.show()
        highlighter = self._editor_highlighters.get(target)
        if isinstance(highlighter, LanguageHighlighter):
            highlighter.set_path(path)
        if hasattr(self, "status_language"):
            suffix = path.suffix.lower() if path is not None else ""
            language = {
                ".py": "Python",
                ".js": "JavaScript",
                ".ts": "TypeScript",
                ".tsx": "TypeScript React",
                ".json": "JSON",
                ".md": "Markdown",
                ".html": "HTML",
                ".css": "CSS",
            }.get(suffix, "Plain Text")
            self.status_language.setText(language)

    def _active_editor(self) -> CodeEditor | None:
        if hasattr(self, "settings_page") and self.workspace_stack.currentWidget() is self.settings_page:
            return None
        group = getattr(self, "_active_editor_group", None) or getattr(self, "editor_tabs", None)
        editor = group.currentWidget() if group is not None else None
        return editor if isinstance(editor, CodeEditor) else None

    def _on_editor_tab_changed(self, index: int, group: QTabWidget | None = None) -> None:
        group = group or self._active_editor_group or self.editor_tabs
        self._active_editor_group = group
        widget = group.widget(index) if index >= 0 else None
        if isinstance(widget, CodeEditor):
            self.code_editor = widget
        path = self._editor_paths.get(widget) if widget is not None else None
        if path is None and widget is not None and widget.property("diff_path"):
            path = Path(str(widget.property("diff_path")))
        self.current_file = path
        self.workspace_toolbar.setVisible(widget is not self.welcome_editor)
        if path is not None:
            relative = path.relative_to(self.project_root).as_posix()
            self.breadcrumb.setText(f"项目  /  {relative}")
        else:
            self.breadcrumb.setText("项目  /  欢迎页")
        self._update_editor_tab_for(widget)
        self._update_cursor_status(widget if isinstance(widget, CodeEditor) else None)
        self._refresh_code_intel()

    def _update_editor_tab_for(self, editor: QWidget | None = None) -> None:
        if not hasattr(self, "editor_tabs"):
            return
        active_group = getattr(self, "_active_editor_group", None) or self.editor_tabs
        target = editor or active_group.currentWidget()
        if target is None:
            return
        group = self._editor_group_for(target)
        if group is None:
            return
        index = group.indexOf(target)
        path = self._editor_paths.get(target)
        name = path.name if path is not None else self._editor_titles.get(target, "欢迎页")
        if isinstance(target, QPlainTextEdit) and target.document().isModified() and path is not None:
            name = "● " + name
        group.setTabText(index, name)

    def _find_editor(self, path: Path, group: QTabWidget | None = None) -> QWidget | None:
        target = path.resolve()
        if group is not None:
            editors = [group.widget(index) for index in range(group.count())]
            for editor in editors:
                if self._editor_paths.get(editor) == target:
                    return editor
        else:
            for editor, editor_path in self._editor_paths.items():
                if editor_path == target:
                    return editor
        return None

    def _show_editor_tab_menu(self, point: QPoint, group: QTabWidget | None = None) -> None:
        group = group or self._active_editor_group
        if group is None:
            return
        bar = group.tabBar()
        index = bar.tabAt(point)
        if index < 0:
            return
        widget = group.widget(index)
        menu = QMenu(self)
        save = menu.addAction("保存")
        save.setEnabled(widget in self._editor_paths and isinstance(widget, CodeEditor) and widget.document().isModified())
        save.triggered.connect(lambda: self._save_editor(widget))
        path = self._editor_paths.get(widget)
        show_diff = menu.addAction("显示未保存差异")
        show_diff.setEnabled(
            isinstance(widget, CodeEditor) and path is not None and widget.document().isModified()
        )
        show_diff.triggered.connect(lambda: self._show_editor_buffer_diff(widget))
        clear_diff = menu.addAction("清除差异标记")
        clear_diff.setEnabled(isinstance(widget, CodeEditor))
        clear_diff.triggered.connect(lambda: self._clear_editor_diff(widget))
        self._add_external_file_actions(menu, path)
        menu.addSeparator()
        close = menu.addAction("关闭")
        close.triggered.connect(lambda: self._close_editor_tab(index, group))
        close_others = menu.addAction("关闭其他标签")
        close_others.triggered.connect(lambda: self._close_other_editor_tabs(index, group))
        open_side = menu.addAction("在右侧编辑器组打开")
        open_side.setEnabled(path is not None)
        open_side.triggered.connect(lambda: self._open_editor_to_side(path))
        menu.exec(bar.mapToGlobal(point))

    def _show_editor_buffer_diff(self, editor: QWidget | None) -> None:
        if not isinstance(editor, CodeEditor):
            self._append_log("当前标签不是代码编辑器")
            return
        path = self._editor_paths.get(editor)
        if path is None:
            self._append_log("当前标签没有对应的项目文件")
            return
        try:
            disk_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self._append_log(f"读取磁盘版本失败：{exc}")
            return
        added, modified = editor.highlight_unsaved_diff(disk_text)
        self._append_log(f"差异标记：新增 {added} 行，修改 {modified} 行；保存后自动消失")

    def _clear_editor_diff(self, editor: QWidget | None) -> None:
        if not isinstance(editor, CodeEditor):
            self._append_log("当前标签不是代码编辑器")
            return
        editor.clear_diff_lines()
        self._append_log("已清除差异标记")

    def _save_editor(self, editor: QWidget | None) -> None:
        group = self._editor_group_for(editor)
        if editor is None or group is None:
            self._append_log("当前没有可保存的编辑器")
            return
        self._active_editor_group = group
        if group.currentWidget() is not editor:
            group.setCurrentWidget(editor)
        self.save_current_file(editor)

    def _close_other_editor_tabs(self, keep_index: int, group: QTabWidget | None = None) -> None:
        group = group or self._active_editor_group
        if group is None:
            return
        for index in range(group.count() - 1, -1, -1):
            if index != keep_index:
                self._close_editor_tab(index, group)

    def _open_editor_to_side(self, path: Path | None) -> None:
        if path is None:
            return
        if self.secondary_editor_tabs is None:
            self.secondary_editor_tabs = self._create_editor_group()
            self.editor_splitter.addWidget(self.secondary_editor_tabs)
            self.editor_splitter.setSizes([1, 1])
        self.secondary_editor_tabs.setVisible(True)
        self._open_file(path, preview=False, group=self.secondary_editor_tabs)

    def _current_editor_location(self) -> tuple[Path, int, int] | None:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if editor is None or path is None:
            return None
        cursor = editor.textCursor()
        return path, cursor.blockNumber() + 1, cursor.columnNumber() + 1

    @staticmethod
    def _symbol_at_cursor(editor: CodeEditor | None) -> str | None:
        if editor is None:
            return None
        line = editor.textCursor().block().text()
        position = editor.textCursor().positionInBlock()
        for match in re.finditer(r"\b[^\W\d]\w*\b", line):
            if match.start() <= position <= match.end():
                return match.group(0)
        return None

    def _remember_navigation(self) -> None:
        location = self._current_editor_location()
        if location is None:
            return
        if not self._navigation_back or self._navigation_back[-1] != location:
            self._navigation_back.append(location)
            self._navigation_back = self._navigation_back[-100:]
        self._navigation_forward.clear()

    def _open_navigation_location(self, location: tuple[Path, int, int]) -> None:
        path, line_number, column_number = location
        if not path.exists():
            self._append_log(f"导航目标不存在：{path.name}")
            return
        self._open_file(path, preview=False)
        editor = self._active_editor()
        if editor is not None:
            self._move_editor_to_location(editor, line_number, column_number)
            editor.setFocus()

    def _find_symbol_definitions(self, symbol: str) -> list[tuple[Path, int, int]]:
        definitions: list[tuple[Path, int, int]] = []
        pattern = re.compile(
            rf"^\s*(?:(?:async\s+)?def|class)\s+{re.escape(symbol)}\b|^\s*{re.escape(symbol)}\s*="
        )
        for path in self._project_files():
            if path.suffix.lower() != ".py":
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line_number, line in enumerate(lines, start=1):
                if pattern.search(line):
                    match = re.search(rf"\b{re.escape(symbol)}\b", line)
                    definitions.append((path, line_number, (match.start() + 1) if match else 1))
        current = self._current_editor_location()
        if current is not None:
            current_path = current[0]
            definitions.sort(key=lambda item: (0 if item[0] == current_path else 1, str(item[0])))
        return definitions

    def _go_to_definition(self) -> None:
        symbol = self._symbol_at_cursor(self._active_editor())
        if not symbol:
            self._append_log("光标处没有可导航的符号")
            return
        definitions = self._find_symbol_definitions(symbol)
        if not definitions:
            self._append_log(f"没有找到 {symbol} 的定义")
            return
        current = self._current_editor_location()
        target = definitions[0]
        if current is not None and len(definitions) > 1 and target[0] == current[0] and target[1] == current[1]:
            target = definitions[1]
        if current is not None:
            self._remember_navigation()
        self._open_navigation_location(target)
        self._append_log(f"已跳转到 {symbol} 的定义")

    def _find_symbol_references(self) -> None:
        symbol = self._symbol_at_cursor(self._active_editor())
        if not symbol:
            self._append_log("光标处没有可搜索的符号")
            return
        self._show_workspace_search()
        self._workspace_search_whole_word = True
        self._workspace_search_case_sensitive = True
        self.workspace_search_input.setText(symbol)
        self._search_workspace()
        count = self.workspace_search_results.topLevelItemCount()
        self._append_log(
            f"已找到 {symbol} 的引用：{count} 条"
            if count
            else f"没有找到 {symbol} 的引用"
        )

    def _navigate_back(self) -> None:
        if not self._navigation_back:
            self._append_log("没有可返回的导航位置")
            return
        current = self._current_editor_location()
        target = self._navigation_back.pop()
        if current is not None:
            self._navigation_forward.append(current)
        self._open_navigation_location(target)

    def _navigate_forward(self) -> None:
        if not self._navigation_forward:
            self._append_log("没有可前进的导航位置")
            return
        current = self._current_editor_location()
        target = self._navigation_forward.pop()
        if current is not None:
            self._navigation_back.append(current)
        self._open_navigation_location(target)

    def _rename_current_symbol(self) -> None:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if path is None or path.suffix.casefold() not in {".py", ".pyi", ".pyw"}:
            self._append_log("安全重命名目前仅支持已打开的 Python 文件")
            return
        symbol = self._symbol_at_cursor(editor)
        if editor is None or symbol is None:
            self._append_log("光标处没有可重命名的符号")
            return
        replacement, accepted = QInputDialog.getText(self, "重命名符号", f"在当前文件中重命名 {symbol}：", QLineEdit.EchoMode.Normal, symbol)
        replacement = replacement.strip()
        if not accepted or replacement == symbol:
            return
        if not replacement.isidentifier() or keyword.iskeyword(replacement):
            self._append_log("符号名称必须是合法的 Python 标识符")
            return
        original = editor.toPlainText()
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(original).readline))
        except (IndentationError, SyntaxError, tokenize.TokenError) as exc:
            self._append_log(f"当前 Python 文件无法安全分词，未执行重命名：{exc}")
            return

        line_starts = [0]
        for line in original.splitlines(keepends=True):
            line_starts.append(line_starts[-1] + len(line))
        replacements = [
            (
                line_starts[token.start[0] - 1] + token.start[1],
                line_starts[token.end[0] - 1] + token.end[1],
            )
            for token in tokens
            if token.type == tokenize.NAME and token.string == symbol
        ]
        if not replacements:
            self._append_log(f"当前 Python 文件中没有可重命名的代码标识符：{symbol}")
            return

        cursor_position = editor.textCursor().position()
        updated = original
        for start, end in reversed(replacements):
            updated = updated[:start] + replacement + updated[end:]

        updated_cursor_position = cursor_position
        prior_shift = 0
        for start, end in replacements:
            if cursor_position >= end:
                prior_shift += len(replacement) - (end - start)
            elif cursor_position >= start:
                updated_cursor_position = (
                    start + prior_shift + min(cursor_position - start, len(replacement))
                )
                break
            else:
                break
        else:
            updated_cursor_position += prior_shift

        editor.setPlainText(updated)
        cursor = editor.textCursor()
        cursor.setPosition(max(0, min(updated_cursor_position, len(updated))))
        editor.setTextCursor(cursor)
        self._append_log(f"已在当前文件中重命名 {symbol} → {replacement}，请保存")

    def _format_current_document(self) -> None:
        editor = self._active_editor()
        if editor is None or editor.isReadOnly():
            self._append_log("当前没有可格式化的编辑器")
            return
        original = editor.toPlainText()
        formatted_lines = [line.rstrip() for line in original.splitlines()]
        formatted = "\n".join(formatted_lines)
        if formatted and not formatted.endswith("\n"):
            formatted += "\n"
        if formatted == original:
            self._append_log("当前文档无需格式化")
            return
        cursor_position = editor.textCursor().position()
        editor.setPlainText(formatted)
        cursor = editor.textCursor()
        cursor.setPosition(min(cursor_position, len(formatted)))
        editor.setTextCursor(cursor)
        self._append_log("已清理行尾空格并统一文件末尾换行，请保存")

    def _close_editor_tab(self, index: int, group: QTabWidget | None = None) -> None:
        group = group or self._active_editor_group
        if group is None:
            return
        widget = group.widget(index)
        if not isinstance(widget, CodeEditor):
            return
        path = self._editor_paths.get(widget)
        if path is not None and widget.document().isModified():
            self._append_log(f"{path.name} 有未保存修改，请先按 Ctrl+S")
            return
        if widget is self.welcome_editor:
            self._append_log("欢迎页不能关闭")
            return
        if widget is self._preview_editor:
            self._preview_editor = None
        shared_document = self._shared_editor_documents.get(path) if path is not None else None
        editor_highlighter = self._editor_highlighters.pop(widget, None)
        document_change_connection = self._editor_document_change_connections.pop(widget, None)
        if document_change_connection is not None:
            QObject.disconnect(document_change_connection)
        self._editor_paths.pop(widget, None)
        self._editor_line_endings.pop(widget, None)
        self._editor_titles.pop(widget, None)
        self._pinned_editors.discard(widget)
        group.removeTab(index)
        if shared_document is not None and widget.document() is shared_document:
            # A shared document must outlive its first editor view.
            replacement = QTextDocument(widget)
            replacement.setDocumentLayout(QPlainTextDocumentLayout(replacement))
            widget.setDocument(replacement)
            has_remaining_view = any(
                other_path == path
                and isinstance(other, CodeEditor)
                and other.document() is shared_document
                for other, other_path in self._editor_paths.items()
            )
            if not has_remaining_view:
                self._shared_editor_documents.pop(path, None)
                highlighters = {}
                if (
                    editor_highlighter is not None
                    and editor_highlighter.document() is shared_document
                ):
                    highlighters[id(editor_highlighter)] = editor_highlighter
                for other, highlighter in list(self._editor_highlighters.items()):
                    if highlighter.document() is shared_document:
                        self._editor_highlighters.pop(other, None)
                        highlighters[id(highlighter)] = highlighter
                for highlighter in highlighters.values():
                    highlighter.setDocument(None)
                shared_document.deleteLater()
        widget.deleteLater()
        if not any(self.editor_tabs.tabBar().isTabVisible(i) for i in range(self.editor_tabs.count())):
            self._show_welcome()

    def _on_file_preview(self, index: QModelIndex) -> None:
        source_index = self.file_proxy.mapToSource(index)
        path = Path(self.file_model.filePath(source_index))
        if path.is_file():
            self._open_file(path, preview=True)

    def _on_file_selected(self, index: QModelIndex) -> None:
        source_index = self.file_proxy.mapToSource(index)
        path = Path(self.file_model.filePath(source_index))
        if path.is_file():
            self._open_file(path, preview=False)

    @staticmethod
    def _detect_line_ending(text: str) -> str:
        match = re.search(r"\r\n|\r|\n", text)
        return match.group(0) if match else "\n"

    def _open_file(self, path: Path, preview: bool = False, group: QTabWidget | None = None) -> None:
        try:
            path = path.resolve()
            group = group or self._active_editor_group
            if group is None:
                group = self.editor_tabs
            if not path.is_relative_to(self.project_root):
                self._append_log("拒绝打开项目目录之外的文件")
                return
            editor = self._find_editor(path, group)
            existing_editor = self._find_editor(path) if editor is None else None
            shared_document = None
            text = None
            if isinstance(existing_editor, CodeEditor):
                shared_document = self._shared_editor_documents.get(path)
                if shared_document is None:
                    shared_document = existing_editor.document()
                    shared_document.setParent(self)
                    self._shared_editor_documents[path] = shared_document
            elif editor is None:
                raw = path.read_bytes()
                if len(raw) > CodingToolbox.MAX_READ_BYTES or b"\x00" in raw:
                    self._append_log(f"无法打开 {path.name}：文件过大或为二进制文件")
                    return
                text = raw.decode("utf-8", errors="replace")

            if editor is None:
                if preview and self._preview_editor is not None:
                    candidate = self._preview_editor
                    candidate_group = self._editor_group_for(candidate)
                    candidate_is_shared = False
                    if isinstance(candidate, CodeEditor):
                        candidate_document = candidate.document()
                        candidate_is_shared = any(
                            document is candidate_document
                            for document in self._shared_editor_documents.values()
                        )
                    can_reuse_preview = (
                        shared_document is None
                        and candidate_group is group
                        and isinstance(candidate, CodeEditor)
                        and not candidate.document().isModified()
                        and not candidate_is_shared
                    )
                    if can_reuse_preview:
                        assert isinstance(candidate, CodeEditor)
                        self._editor_paths.pop(candidate, None)
                        editor = candidate
                        self._editor_paths[editor] = path
                        group.setTabToolTip(group.indexOf(editor), path.as_posix())
                    elif isinstance(candidate, CodeEditor):
                        self._pinned_editors.add(candidate)
                if editor is None:
                    editor = CodeEditor()
                    if shared_document is not None:
                        editor.setDocument(shared_document)
                    self._editor_paths[editor] = path
                    self._configure_editor(editor)
                    tab_index = group.addTab(editor, path.name)
                    self._install_tab_close_button(group, tab_index, editor)
                    group.setTabToolTip(tab_index, path.as_posix())
                if text is not None:
                    self._editor_line_endings[editor] = self._detect_line_ending(text)
                    editor.setPlainText(text)
                    editor.document().setModified(False)
                elif isinstance(existing_editor, CodeEditor):
                    self._editor_line_endings[editor] = self._editor_line_endings.get(
                        existing_editor,
                        "\n",
                    )
            self._active_editor_group = group
            index = group.indexOf(editor)
            group.setCurrentIndex(index)
            if preview:
                # Clicking an already pinned tab must not downgrade it into
                # the single replaceable preview slot.
                if editor not in self._pinned_editors:
                    self._preview_editor = editor
            else:
                self._pinned_editors.add(editor)
                if editor is self._preview_editor:
                    self._preview_editor = None
            self.code_editor = editor
            self.current_file = path
            self.workspace_toolbar.setVisible(True)
            highlighter = self._editor_highlighters.get(editor)
            if isinstance(highlighter, LanguageHighlighter):
                highlighter.set_path(path)
            if self.welcome_editor is not None:
                self.editor_tabs.tabBar().setTabVisible(self.editor_tabs.indexOf(self.welcome_editor), False)
            relative = path.relative_to(self.project_root).as_posix()
            self._update_editor_tab_for(editor)
            self.breadcrumb.setText(f"项目  /  {relative}")
            self._refresh_outline()
            self._append_log(f"{'预览' if preview else '已打开'} {relative}")
        except OSError as exc:
            self._append_log(f"读取失败：{exc}")

    def _show_welcome(self) -> None:
        self.current_file = None
        if hasattr(self, "editor_tabs") and self.welcome_editor is not None:
            # The welcome page belongs to the primary editor group. Explicitly
            # activate it so a secondary group's file is never overwritten
            # when starting a new coding session.
            self._active_editor_group = self.editor_tabs
            self.editor_tabs.setCurrentWidget(self.welcome_editor)
            self.code_editor = self.welcome_editor
            self.editor_tabs.setTabText(self.editor_tabs.indexOf(self.welcome_editor), "欢迎页")
            self.editor_tabs.tabBar().setTabVisible(self.editor_tabs.indexOf(self.welcome_editor), True)
        if hasattr(self, "breadcrumb"):
            self.breadcrumb.setText("项目  /  欢迎页")
        self.workspace_toolbar.setVisible(False)

    def _show_editor_text(self, text: str) -> None:
        editor = self._active_editor() or self.code_editor
        editor.setPlainText(text)
        editor.document().setModified(False)

    def _update_editor_tab(self) -> None:
        self._update_editor_tab_for(self._active_editor())

    def _schedule_code_intel(self) -> None:
        """Debounce symbol/syntax refresh so typing one key costs no parse."""
        if getattr(self, "_closing", False):
            return
        timer = getattr(self, "_code_intel_timer", None)
        if timer is not None:
            timer.start()

    def _refresh_code_intel(self) -> None:
        """Parse the active Python document once and update outline + problems."""
        if getattr(self, "_closing", False):
            return
        timer = getattr(self, "_code_intel_timer", None)
        if timer is not None:
            timer.stop()
        if not hasattr(self, "outline_tree"):
            return
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if editor is None or path is None:
            return
        text = editor.toPlainText()
        syntax_tree = None
        syntax_error = None
        if path.suffix.lower() == ".py" and len(text) <= self.CODE_INTEL_MAX_PARSE_CHARS:
            try:
                syntax_tree = ast.parse(text or "\n")
            except SyntaxError as exc:
                syntax_error = exc
        self._render_outline(path, text, syntax_tree)
        if syntax_error is not None:
            self._code_intel_syntax_errors[str(path)] = syntax_error
        else:
            self._code_intel_syntax_errors.pop(str(path), None)
        self._render_diagnostics(path, syntax_error)
        if language_spec(path) is not None:
            self._schedule_lsp_diagnostics(path, text)

    def _schedule_lsp_diagnostics(self, path: Path, text: str) -> None:
        if getattr(self, "_closing", False):
            return
        if len(text) > self.CODE_INTEL_MAX_PARSE_CHARS:
            return
        if not hasattr(self, "_resident_lsp_bridge") or self._resident_lsp_bridge is None:
            self._resident_lsp_bridge = ResidentLSPBridge(self.project_root, self)
            self._resident_lsp_bridge.diagnostics_ready.connect(self._on_lsp_diagnostics)
        self._lsp_generation += 1
        self._resident_lsp_bridge.submit(path, text, self._lsp_generation)

    def _on_lsp_diagnostics(self, raw_path: str, generation: int, diagnostics: object, error: str) -> None:
        if getattr(self, "_closing", False):
            return
        if generation != self._lsp_generation:
            return
        path = Path(raw_path)
        if error:
            return
        items = diagnostics if isinstance(diagnostics, list) else []
        self._lsp_diagnostics[str(path)] = [item for item in items if isinstance(item, dict)]
        editor = self._active_editor()
        active_path = self._editor_paths.get(editor) if editor is not None else None
        if editor is None or active_path is None or active_path != path:
            return
        syntax_error = self._code_intel_syntax_errors.get(str(path))
        self._render_diagnostics(path, syntax_error)

    def _refresh_outline(self) -> None:
        self._refresh_code_intel()

    def _refresh_diagnostics(self) -> None:
        self._refresh_code_intel()

    def _render_outline(
        self,
        path: Path,
        text: str,
        syntax_tree: ast.AST | None,
    ) -> None:
        if not hasattr(self, "outline_tree"):
            return
        self.outline_tree.clear()
        count = 0

        def add_symbol(name: str, kind: str, line_number: int, parent: QTreeWidgetItem | None = None) -> QTreeWidgetItem:
            nonlocal count
            count += 1
            item = QTreeWidgetItem([f"{kind}  {name}  ·  {line_number}"])
            item.setData(0, Qt.ItemDataRole.UserRole, str(path))
            item.setData(0, Qt.ItemDataRole.UserRole + 1, line_number)
            if parent is None:
                self.outline_tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            return item

        if syntax_tree is not None:
            def visit(nodes: list[ast.AST], parent: QTreeWidgetItem | None = None) -> None:
                for node in nodes:
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        kind = "类" if isinstance(node, ast.ClassDef) else "函数"
                        item = add_symbol(node.name, kind, node.lineno, parent)
                        visit(getattr(node, "body", []), item)

            visit(syntax_tree.body)

        if count == 0:
            pattern = re.compile(r"^\s*(?:def|class|function|interface|enum|struct|fn)\s+([A-Za-z_]\w*)")
            for line_number, line in enumerate(text.splitlines(), start=1):
                match = pattern.match(line)
                if match:
                    add_symbol(match.group(1), "符号", line_number)
        if count == 0:
            item = QTreeWidgetItem(["当前文件没有可识别的符号"])
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.outline_tree.addTopLevelItem(item)

    def _render_diagnostics(self, path: Path, syntax_error: SyntaxError | None) -> None:
        if not hasattr(self, "problems_tree"):
            return
        self.problems_tree.clear()
        if path.suffix.lower() != ".py":
            return
        if syntax_error is not None:
            line_number = max(1, int(syntax_error.lineno or 1))
            column_number = max(1, int(syntax_error.offset or 1))
            message = str(syntax_error.msg or "语法错误")
            item = QTreeWidgetItem([f"{path.name}:{line_number}:{column_number}  {message}"])
            item.setData(0, Qt.ItemDataRole.UserRole, str(path))
            item.setData(0, Qt.ItemDataRole.UserRole + 1, line_number)
            item.setData(0, Qt.ItemDataRole.UserRole + 2, column_number)
            self.problems_tree.addTopLevelItem(item)
        for diagnostic in self._lsp_diagnostics.get(str(path), []):
            if not isinstance(diagnostic, dict):
                continue
            try:
                diag_line = max(1, int(diagnostic.get("line", 1)))
                diag_column = max(1, int(diagnostic.get("column", 1)))
            except (TypeError, ValueError):
                continue
            severity = str(diagnostic.get("severity") or "error")
            diag_message = str(diagnostic.get("message") or "LSP 诊断")[:500]
            code = str(diagnostic.get("code") or "").strip()
            suffix = f" [{code}]" if code else ""
            diag_item = QTreeWidgetItem([
                f"{path.name}:{diag_line}:{diag_column}  [{severity}] {diag_message}{suffix}"
            ])
            diag_item.setData(0, Qt.ItemDataRole.UserRole, str(path))
            diag_item.setData(0, Qt.ItemDataRole.UserRole + 1, diag_line)
            diag_item.setData(0, Qt.ItemDataRole.UserRole + 2, diag_column)
            self.problems_tree.addTopLevelItem(diag_item)

    def _open_problem(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        raw_path = item.data(0, Qt.ItemDataRole.UserRole)
        line_number = item.data(0, Qt.ItemDataRole.UserRole + 1)
        column_number = item.data(0, Qt.ItemDataRole.UserRole + 2)
        if not raw_path:
            return
        self._open_file(Path(str(raw_path)), preview=False)
        editor = self._active_editor()
        if editor is not None:
            self._move_editor_to_location(editor, int(line_number or 1), int(column_number or 1))
            editor.setFocus()

    def _open_outline_result(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        raw_path = item.data(0, Qt.ItemDataRole.UserRole)
        line_number = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if not raw_path or not line_number:
            return
        self._open_file(Path(str(raw_path)), preview=False)
        editor = self._active_editor()
        if editor is not None:
            self._move_editor_to_location(editor, int(line_number))
            editor.setFocus()

    @staticmethod
    def _move_editor_to_location(editor: CodeEditor, line_number: int, column_number: int | None = None) -> None:
        line_number = max(1, min(line_number, max(1, editor.blockCount())))
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        for _ in range(max(0, line_number - 1)):
            cursor.movePosition(QTextCursor.MoveOperation.Down)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        if column_number is None:
            cursor.movePosition(QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor)
        else:
            column = min(max(0, column_number - 1), len(cursor.block().text()))
            cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor, column)
        editor.setTextCursor(cursor)
        editor.ensureCursorVisible()

    def _show_find_bar(self, replace: bool = False) -> None:
        if hasattr(self, "workspace_stack") and self.workspace_stack.currentWidget() is not self.workspace_page:
            self.workspace_stack.setCurrentWidget(self.workspace_page)
        editor = self._active_editor()
        if editor is None:
            return
        self.find_bar.setVisible(True)
        self.replace_input.setVisible(replace)
        self.replace_button.setVisible(replace)
        self.replace_all_button.setVisible(replace)
        selected = editor.textCursor().selectedText().replace("\u2029", "\n")
        if selected and "\n" not in selected and not self.find_input.text():
            self.find_input.setText(selected)
        self.find_input.setFocus()
        self.find_input.selectAll()

    def _close_find_bar(self) -> None:
        self.find_bar.setVisible(False)
        editor = self._active_editor()
        if editor is not None:
            editor.setFocus()

    def _find_flags(self, backward: bool = False) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if self.find_case_checkbox.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if self.find_word_checkbox.isChecked():
            flags |= QTextDocument.FindFlag.FindWholeWords
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        return flags

    def _on_find_text_changed(self, text: str) -> None:
        if not text:
            self.find_status.setText("")
            return
        self._find_next()

    def _find_next(self) -> None:
        self._find_in_editor(backward=False)

    def _find_previous(self) -> None:
        self._find_in_editor(backward=True)

    def _find_in_editor(self, backward: bool) -> None:
        editor = self._active_editor()
        term = self.find_input.text()
        if editor is None or not term:
            self.find_status.setText("")
            return
        flags = self._find_flags(backward)
        if editor.find(term, flags):
            self.find_status.setText("已找到")
            return
        cursor = editor.textCursor()
        cursor.clearSelection()
        cursor.movePosition(
            QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start
        )
        editor.setTextCursor(cursor)
        if editor.find(term, flags):
            self.find_status.setText("已循环")
        else:
            self.find_status.setText("未找到")

    def _replace_current(self) -> None:
        editor = self._active_editor()
        term = self.find_input.text()
        if editor is None or not term:
            return
        cursor = editor.textCursor()
        selected = cursor.selectedText()
        matches = selected == term if self.find_case_checkbox.isChecked() else selected.casefold() == term.casefold()
        if not matches:
            self._find_next()
            cursor = editor.textCursor()
        if cursor.hasSelection():
            cursor.insertText(self.replace_input.text())
            self.find_status.setText("已替换")
            return
        self.find_status.setText("未找到")

    def _replace_all(self) -> None:
        editor = self._active_editor()
        term = self.find_input.text()
        if editor is None or not term:
            return
        document = editor.document()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        count = 0
        flags = self._find_flags(False)
        while True:
            match = document.find(term, cursor, flags)
            if match.isNull():
                break
            match.insertText(self.replace_input.text())
            cursor = match
            count += 1
        cursor.endEditBlock()
        self.find_status.setText(f"已替换 {count} 处")
        editor.setFocus()

    def save_current_file(self, editor: CodeEditor | None = None) -> None:
        editor = editor or self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if editor is None or path is None or not path.is_relative_to(self.project_root):
            self._append_log("当前没有可保存的项目文件")
            return
        try:
            relative = path.relative_to(self.project_root).as_posix()
            text = editor.toPlainText()
            line_ending = self._editor_line_endings.get(editor, "\n")
            if line_ending != "\n":
                text = text.replace("\n", line_ending)
            result = self.toolbox.write_file(relative, text)
            editor.document().setModified(False)
            editor.clear_diff_lines()
            self._update_editor_tab_for(editor)
            self._append_log(result)
            self.refresh_git_status()
        except Exception as exc:  # noqa: BLE001
            self._show_message("保存失败", str(exc))

    def _refresh_open_file_after_task(self) -> None:
        active_editor = self._active_editor()
        editors = list(self._editor_paths.items())
        editors.sort(key=lambda item: item[0] is not active_editor)
        refreshed_paths: set[Path] = set()
        for editor, path in editors:
            if not isinstance(editor, CodeEditor):
                continue
            if path in refreshed_paths:
                continue
            refreshed_paths.add(path)
            if editor.document().isModified():
                if editor is active_editor:
                    self._append_log(f"Agent 已修改 {path.name}，当前标签有本地未保存内容，未自动覆盖")
                continue
            try:
                raw = path.read_bytes()
                if b"\x00" not in raw and len(raw) <= CodingToolbox.MAX_READ_BYTES:
                    text = raw.decode("utf-8", errors="replace")
                    self._editor_line_endings[editor] = self._detect_line_ending(text)
                    editor.setPlainText(text)
                    editor.document().setModified(False)
                    self._update_editor_tab_for(editor)
            except OSError:
                continue
