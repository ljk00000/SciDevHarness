"""Project file operations, quick navigation and workspace search.

The host retains state and lifecycle ownership; this module does not import its entry point.
"""

from __future__ import annotations

import os
import re
import shutil
import webbrowser
from pathlib import Path
from PySide6.QtCore import QModelIndex
from PySide6.QtCore import QPoint
from PySide6.QtCore import QStringListModel
from PySide6.QtCore import Qt
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCompleter
from PySide6.QtWidgets import QMenu
from PySide6.QtWidgets import QMessageBox
from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtWidgets import QTreeWidgetItem
from scidev.agent.toolbox import CodingToolbox
from scidev.runtime.paths import _is_link_or_junction as _is_link_or_junction
from scidev.desktop.editor import CodeEditor as CodeEditor

_EXCLUDED_PROJECT_NAMES_CASEFOLD = frozenset(
    name.casefold() for name in CodingToolbox.EXCLUDED_NAMES
)


class ExplorerWorkspaceMixin:
    """Operate on host state; introduce no initialization or additional state."""

    def _ensure_command_completer(self) -> QCompleter:
        completer = getattr(self, "command_completer", None)
        if completer is None:
            completer = QCompleter(self)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer.setModel(
                QStringListModel(
                    [
                        "> open folder",
                        "> new worktree",
                        "> worktrees",
                        "> mcp",
                        "> errors",
                        "> pr",
                        "> terminal",
                        "> problems",
                        "> search",
                        "> replace",
                        "> outline",
                        "> git",
                        "> explorer",
                        "> appearance",
                        "> settings",
                    ],
                    completer,
                )
            )
            completer.setWidget(self.command_search)
            completer.activated.connect(self._run_command_completion)
            self.command_completer = completer
        return completer


    def _update_command_suggestions(self, text: str) -> None:
        if text.lstrip().startswith(">"):
            completer = self._ensure_command_completer()
            completer.setCompletionPrefix(text.strip())
            if completer.completionCount() > 0:
                completer.complete()
            else:
                completer.popup().hide()
        elif hasattr(self, "command_completer"):
            self.command_completer.popup().hide()


    def _run_command_completion(self, command: str) -> None:
        self.command_search.setText(command)
        self.command_search.setCursorPosition(len(command))
        self._open_quick_search()


    def _focus_quick_search(self) -> None:
        self.show_workspace()
        self.command_search.selectAll()
        self.command_search.setFocus()


    def _expand_filtered_file_branches(self, text: str) -> None:
        query = text.strip().casefold()
        self._quick_search_expand_query = query
        self._quick_search_expand_directory_keys.clear()
        self._quick_search_visible_expand_timer.stop()
        if not query or query.startswith(">"):
            self._quick_search_expand_timer.stop()
            return
        self._quick_search_expand_timer.start()


    def _expand_filtered_file_branches_now(self) -> None:
        query = self.command_search.text().strip().casefold()
        if self.file_proxy._filter_text != query or self._quick_search_expand_query != query:
            return
        directories = self.file_proxy.matching_directory_paths()
        if len(directories) > self.QUICK_SEARCH_BULK_EXPANSION_LIMIT:
            self._quick_search_expand_directory_keys = {
                self.file_proxy._path_key(directory) for directory in directories
            }
            self._schedule_visible_filtered_branch_expansion()
            return
        for directory in directories:
            source_index = self.file_model.index(os.fspath(directory))
            proxy_index = self.file_proxy.mapFromSource(source_index)
            if proxy_index.isValid():
                self.file_tree.expand(proxy_index)


    def _schedule_visible_filtered_branch_expansion(self, *_args: object) -> None:
        if not self._quick_search_expand_directory_keys:
            return
        if (
            self.file_proxy._filter_text != self._quick_search_expand_query
            or not self.file_tree.isVisible()
        ):
            return
        self._quick_search_visible_expand_timer.start()


    def _expand_visible_filtered_file_branches(self) -> None:
        query = self.command_search.text().strip().casefold()
        if (
            self.file_proxy._filter_text != query
            or self._quick_search_expand_query != query
            or not self._quick_search_expand_directory_keys
            or not self.file_tree.isVisible()
        ):
            return

        viewport = self.file_tree.viewport()
        row_height = max(1, self.file_tree.sizeHintForRow(0))
        expansions_this_batch = 0
        processed_any = False
        while expansions_this_batch < self.QUICK_SEARCH_VISIBLE_EXPANSION_BATCH_SIZE:
            y = 0
            processed_visible_branch = False
            while y < viewport.height():
                index = self.file_tree.indexAt(QPoint(2, y))
                if not index.isValid():
                    y += row_height
                    continue
                item_rect = self.file_tree.visualRect(index)
                y = max(y + 1, item_rect.bottom() + 1)
                source_index = self.file_proxy.mapToSource(index)
                path_key = self.file_proxy._path_key(
                    Path(self.file_model.filePath(source_index))
                )
                if path_key not in self._quick_search_expand_directory_keys:
                    continue
                self._quick_search_expand_directory_keys.discard(path_key)
                processed_any = True
                if self.file_tree.isExpanded(index):
                    processed_visible_branch = True
                    break
                self.file_tree.expand(index)
                expansions_this_batch += 1
                processed_visible_branch = True
                break
            if not processed_visible_branch:
                break

        if self._quick_search_expand_directory_keys and processed_any:
            self._quick_search_visible_expand_timer.start()


    def _focus_command_palette(self) -> None:
        self.show_workspace()
        self.command_search.setText("> ")
        self.command_search.setCursorPosition(len(self.command_search.text()))
        self.command_search.setFocus()


    def _on_workspace_stack_page_changed(self, index: int) -> None:
        if hasattr(self, "settings_button"):
            self.settings_button.setChecked(self.workspace_stack.widget(index) is getattr(self, "settings_page", None))
        tree_active = self.workspace_stack.widget(index) is self.git_page
        if tree_active and not self._git_focus:
            self._git_details_expanded = self.width() >= 1100
            self._git_details_forced = False
        self._git_focus = tree_active
        self._adapt_workbench_layout()
        self._update_cursor_status()
        if not tree_active:
            if self._quick_search_expand_directory_keys:
                self._schedule_visible_filtered_branch_expansion()
            else:
                self._expand_filtered_file_branches(self.command_search.text())
        else:
            self._adapt_git_layout()


    def _project_files(self) -> list[Path]:
        files: list[Path] = []
        for root, directories, names in os.walk(self.project_root):
            directories[:] = [
                name for name in directories
                if name.casefold() not in _EXCLUDED_PROJECT_NAMES_CASEFOLD
                and not _is_link_or_junction(Path(root) / name)
            ]
            for name in names:
                path = Path(root) / name
                if not _is_link_or_junction(path):
                    files.append(path)
        return files


    @staticmethod
    def _parse_quick_open_location(term: str) -> tuple[str, int | None, int | None]:
        parts = term.rsplit(":", 2)
        if len(parts) == 3 and parts[-1].isdigit() and parts[-2].isdigit():
            return parts[0], max(1, int(parts[-2])), max(1, int(parts[-1]))
        if len(parts) == 2 and parts[-1].isdigit():
            return parts[0], max(1, int(parts[-1])), None
        return term, None, None


    def _open_quick_search(self) -> None:
        term = self.command_search.text().strip()
        if not term:
            self._focus_quick_search()
            return
        if term.startswith(">"):
            command = term[1:].strip().casefold()
            if "new worktree" in command or "隔离工作区" in command or "新建 worktree" in command:
                self.command_search.clear()
                self._create_isolated_worktree()
                return
            if command == "worktrees" or "worktree list" in command or "工作树" in command:
                self.command_search.clear()
                self._show_worktree_dialog()
                return
            if command == "mcp" or "mcp 能力" in command:
                self.command_search.clear()
                self._show_mcp_browser()
                return
            if command == "errors" or "错误趋势" in command:
                self.command_search.clear()
                self._show_error_trends()
                return
            if command in {"update", "check update"} or "检查更新" in command:
                self.command_search.clear()
                self._show_update_dialog()
                return
            if command == "pr" or "pull request" in command:
                self.command_search.clear()
                self._show_pull_request()
                return
            if "debug" in command or "调试" in command or "pdb" in command:
                self.command_search.clear()
                self._debug_current_file()
                return
            if "open folder" in command or "打开文件夹" in command or "workspace" in command:
                self.command_search.clear()
                self._choose_workspace()
                return
            elif "terminal" in command or "终端" in command:
                self._toggle_bottom_panel(1)
            elif "problem" in command or "问题" in command:
                self._toggle_bottom_panel(2)
            elif "search" in command or "搜索" in command:
                self._show_workspace_search()
            elif "replace" in command or "替换" in command:
                self._show_find_bar(True)
            elif any(keyword in command for keyword in ("appearance", "theme", "settings", "设置", "主题", "外观")):
                self.show_settings()
            elif "outline" in command or "大纲" in command:
                self._show_outline()
            elif "context" in command or "上下文" in command:
                self.show_project_context()
            elif command == "git" or "git" in command:
                self.show_git()
            elif "explorer" in command or "资源" in command:
                self.focus_explorer()
            else:
                self._append_log(f"未识别命令: {term[1:].strip()}")
                return
            self.command_search.clear()
            return
        file_term, line_number, column_number = self._parse_quick_open_location(term)
        if not file_term and line_number is not None:
            if self.current_file is None:
                self._append_log("请先打开文件，再跳转到指定行列")
                return
            file_term = self.current_file.relative_to(self.project_root).as_posix()
        query = file_term.casefold()
        candidates = self._project_files()
        ranked: list[tuple[tuple[int, int, str], Path]] = []
        for path in candidates:
            relative = path.relative_to(self.project_root).as_posix()
            filename = path.name.casefold()
            relative_lower = relative.casefold()
            if query == filename:
                score = 0
            elif query == relative_lower:
                score = 1
            elif filename.startswith(query):
                score = 2
            elif query in filename:
                score = 3
            elif query in relative_lower:
                score = 4
            else:
                continue
            ranked.append(((score, len(relative), relative_lower), path))
        if not ranked:
            self._append_log(f"没有找到文件: {term}")
            return
        ranked.sort(key=lambda item: item[0])
        target = ranked[0][1]
        self.command_search.clear()
        self._open_file(target, preview=False)
        editor = self._active_editor()
        if editor is not None:
            if line_number is not None:
                self._move_editor_to_location(editor, line_number, column_number)
            editor.setFocus()


    def _show_workspace_search(self) -> None:
        if hasattr(self, "workspace_stack") and self.workspace_stack.currentWidget() is not self.workspace_page:
            self.workspace_stack.setCurrentWidget(self.workspace_page)
        self._set_activity_button(1)
        self.bottom_tabs.setCurrentIndex(0)
        self.bottom_tabs.setVisible(True)
        self.workspace_search_input.setFocus()
        self.workspace_search_input.selectAll()


    def _show_outline(self) -> None:
        if hasattr(self, "workspace_stack") and self.workspace_stack.currentWidget() is not self.workspace_page:
            self.workspace_stack.setCurrentWidget(self.workspace_page)
        self._refresh_outline()
        self.bottom_tabs.setCurrentIndex(3)
        self.bottom_tabs.setVisible(True)
        self.outline_tree.setFocus()


    def _search_workspace(self) -> None:
        query = self.workspace_search_input.text().strip()
        self.workspace_search_results.clear()
        whole_word = self._workspace_search_whole_word
        case_sensitive = self._workspace_search_case_sensitive
        self._workspace_search_whole_word = False
        self._workspace_search_case_sensitive = False
        if not query:
            self._append_log("请输入工作区搜索内容")
            return
        query_folded = query.casefold()
        count = 0
        for path in self._project_files():
            if count >= 500:
                break
            try:
                if path.stat().st_size > CodingToolbox.MAX_READ_BYTES:
                    continue
                with path.open("rb") as stream:
                    raw = stream.read(CodingToolbox.MAX_READ_BYTES + 1)
                if len(raw) > CodingToolbox.MAX_READ_BYTES or b"\x00" in raw:
                    continue
                lines = raw.decode("utf-8", errors="replace").splitlines()
            except OSError:
                continue
            relative = path.relative_to(self.project_root).as_posix()
            for line_number, line in enumerate(lines, start=1):
                if whole_word:
                    flags = 0 if case_sensitive else re.IGNORECASE
                    matched = re.search(rf"\b{re.escape(query)}\b", line, flags) is not None
                else:
                    matched = query_folded in line.casefold()
                if not matched:
                    continue
                item = QTreeWidgetItem([f"{relative}:{line_number}  {line.strip()[:180]}"])
                item.setData(0, Qt.ItemDataRole.UserRole, str(path))
                item.setData(0, Qt.ItemDataRole.UserRole + 1, line_number)
                self.workspace_search_results.addTopLevelItem(item)
                count += 1
                if count >= 500:
                    break
        suffix = "（已显示前 500 条）" if count >= 500 else ""
        self._append_log(f"工作区搜索完成：{count} 条{suffix}")


    def _open_search_result(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        raw_path = item.data(0, Qt.ItemDataRole.UserRole)
        line_number = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if not raw_path:
            return
        if self._current_editor_location() is not None:
            self._remember_navigation()
        self._open_file(Path(str(raw_path)), preview=False)
        editor = self._active_editor()
        if editor is None:
            return
        self._move_editor_to_location(editor, int(line_number or 1))
        editor.setFocus()


    def _show_current_diff(self) -> None:
        editor = self._active_editor()
        path = self._editor_paths.get(editor) if editor is not None else None
        if path is None and editor is not None:
            contextual_path = editor.property("diff_path")
            if contextual_path:
                path = Path(str(contextual_path))
        if editor is None or path is None:
            self._append_log("请先打开一个项目文件")
            return
        diff_text = self.git.diff(path)
        if not diff_text:
            self._append_log(f"{path.name} 没有未提交的差异")
            return
        title = f"差异 · {path.name}"
        diff_editor = next(
            (candidate for candidate, label in self._editor_titles.items() if label == title),
            None,
        )
        if diff_editor is None:
            diff_editor = CodeEditor()
            self._configure_editor(diff_editor)
            diff_editor.setReadOnly(True)
            diff_editor.setProperty("diff_path", str(path))
            self._editor_titles[diff_editor] = title
            group = self._active_editor_group or self.editor_tabs
            index = group.addTab(diff_editor, title)
            group.setTabToolTip(index, f"Git diff · {path.as_posix()}")
        diff_editor.setPlainText(diff_text)
        diff_editor.document().setModified(False)
        group = self._editor_group_for(diff_editor) or self._active_editor_group or self.editor_tabs
        self._active_editor_group = group
        group.setCurrentWidget(diff_editor)
        self._append_log(f"已打开 {path.name} 的 Git 差异")


    def _explorer_path(self, index: QModelIndex | None = None) -> Path | None:
        target_index = index if index is not None and index.isValid() else self.file_tree.currentIndex()
        if not target_index.isValid():
            return self.project_root
        source_index = self.file_proxy.mapToSource(target_index)
        path = Path(self.file_model.filePath(source_index)).resolve()
        return path if path.is_relative_to(self.project_root) else None


    def _prepare_explorer_entry(
        self,
        mode: str,
        parent: Path | None = None,
        target: Path | None = None,
    ) -> None:
        if self.new_file_entry.isVisible() and self._explorer_entry_mode == mode:
            self.new_file_entry.setVisible(False)
            return
        self._explorer_entry_mode = mode
        self._rename_target = target
        self._entry_parent = (parent or self.project_root).resolve()
        self.new_file_entry.clear()
        if mode == "folder":
            self.new_file_entry.setPlaceholderText("输入文件夹名称，回车创建")
        elif mode == "rename":
            self.new_file_entry.setText(target.name if target is not None else "")
            self.new_file_entry.setPlaceholderText("输入新名称，回车确认")
            self.new_file_entry.selectAll()
        else:
            self.new_file_entry.setPlaceholderText("输入相对路径，例如 src/new_module.py，回车创建")
        self.new_file_entry.setVisible(True)
        self.new_file_entry.setFocus()


    def start_new_file_entry(self, parent: Path | None = None) -> None:
        self._prepare_explorer_entry("create", parent=parent)


    def start_new_folder_entry(self, parent: Path | None = None) -> None:
        self._prepare_explorer_entry("folder", parent=parent)


    def start_rename_entry(self, target: Path | None = None) -> None:
        target = target or self._explorer_path()
        if target is None or target == self.project_root or not target.exists():
            return
        self._prepare_explorer_entry("rename", target=target)


    def create_new_file(self) -> None:
        raw_name = self.new_file_entry.text().strip().replace("\\", "/")
        if not raw_name:
            self.new_file_entry.setVisible(False)
            return
        mode = self._explorer_entry_mode
        old_target = self._rename_target
        if mode == "rename" and old_target is not None:
            # QFileSystemModel may hand back a Windows 8.3 path (for example
            # RUNNER~1) while project_root was opened through its long name.
            # Canonicalize before computing the relative path and updating tabs.
            old_target = old_target.resolve()
        if mode == "rename" and old_target is not None:
            target = (old_target.parent / raw_name).resolve()
        else:
            target = (self._entry_parent / raw_name).resolve()
        try:
            relative = target.relative_to(self.project_root)
        except ValueError:
            self._append_log("资源操作失败：路径必须位于项目目录内")
            return
        if not relative.parts or any(
            part.casefold() in _EXCLUDED_PROJECT_NAMES_CASEFOLD for part in relative.parts
        ):
            self._append_log("资源操作失败：不能操作内部目录")
            return
        try:
            if mode == "rename":
                if old_target is None or not old_target.exists() or target.exists():
                    self._append_log("重命名失败：名称无效或目标已存在")
                    return
                old_relative = old_target.relative_to(self.project_root).as_posix()
                old_target.rename(target)
                self._update_open_paths_after_rename(old_target, target)
                self._append_log(f"已重命名 {old_relative} → {relative.as_posix()}")
            elif mode == "folder":
                if target.exists():
                    self._append_log("新建文件夹失败：目标已存在")
                    return
                target.mkdir(parents=True, exist_ok=False)
                self._append_log(f"已新建文件夹 {relative.as_posix()}")
            else:
                if target.exists():
                    self._append_log("新建文件失败：文件已存在")
                    return
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("", encoding="utf-8")
                self._open_file(target, preview=False)
                self._append_log(f"已新建 {relative.as_posix()}")
            self.new_file_entry.clear()
            self.new_file_entry.setVisible(False)
            self.refresh_file_tree()
        except OSError as exc:
            self._append_log(f"资源操作失败：{exc}")


    def _update_open_paths_after_rename(self, old_target: Path, new_target: Path) -> None:
        old_target = old_target.resolve()
        new_target = new_target.resolve()

        def rebase(path: Path) -> Path:
            try:
                path = path.resolve()
            except OSError:
                pass
            if path == old_target or old_target in path.parents:
                return new_target / path.relative_to(old_target) if path != old_target else new_target
            return path

        for editor, path in list(self._editor_paths.items()):
            if path == old_target or old_target in path.parents:
                updated = rebase(path)
                self._editor_paths[editor] = updated
                group = self._editor_group_for(editor)
                index = group.indexOf(editor) if group is not None else -1
                if group is not None and index >= 0:
                    group.setTabToolTip(index, updated.as_posix())
                    self._update_editor_tab_for(editor)
        for path, document in list(self._shared_editor_documents.items()):
            if path == old_target or old_target in path.parents:
                updated = rebase(path)
                self._shared_editor_documents.pop(path, None)
                self._shared_editor_documents[updated] = document
        if self.current_file is not None:
            self.current_file = rebase(self.current_file)
        self._navigation_back = [
            (rebase(path), line, column) for path, line, column in self._navigation_back
        ]
        self._navigation_forward = [
            (rebase(path), line, column) for path, line, column in self._navigation_forward
        ]
        self.terminal_cwd = rebase(self.terminal_cwd)

        for index in range(self.workspace_search_results.topLevelItemCount()):
            item = self.workspace_search_results.topLevelItem(index)
            raw_path = item.data(0, Qt.ItemDataRole.UserRole)
            if not raw_path:
                continue
            old_path = Path(str(raw_path))
            updated = rebase(old_path)
            if updated == old_path:
                continue
            item.setData(0, Qt.ItemDataRole.UserRole, str(updated))
            line = int(item.data(0, Qt.ItemDataRole.UserRole + 1) or 1)
            relative = updated.relative_to(self.project_root).as_posix()
            _separator, _found, detail = item.text(0).partition("  ")
            item.setText(0, f"{relative}:{line}  {detail}" if detail else f"{relative}:{line}")

        for group in self._editor_groups():
            for index in range(group.count()):
                editor = group.widget(index)
                raw_diff_path = editor.property("diff_path")
                if not raw_diff_path:
                    continue
                old_diff_path = Path(str(raw_diff_path)).resolve()
                updated_diff_path = rebase(old_diff_path)
                if updated_diff_path == old_diff_path:
                    continue
                editor.setProperty("diff_path", str(updated_diff_path))
                title = f"差异 · {updated_diff_path.name}"
                self._editor_titles[editor] = title
                group.setTabToolTip(index, f"Git diff · {updated_diff_path.as_posix()}")
                self._update_editor_tab_for(editor)
                if isinstance(editor, QPlainTextEdit):
                    editor.setPlainText(self.git.diff(updated_diff_path))
                    editor.document().setModified(False)

        active_editor = self._active_editor()
        active_group = self._editor_group_for(active_editor)
        if active_editor is not None and active_group is not None:
            self._on_editor_tab_changed(active_group.indexOf(active_editor), active_group)


    def _show_explorer_menu(self, point: QPoint) -> None:
        index = self.file_tree.indexAt(point)
        # A blank-area context menu must not inherit the previously selected
        # resource; otherwise Rename/Delete can target the wrong file.
        path = self._explorer_path(index) if index.isValid() else None
        if path is not None and index.isValid():
            self.file_tree.setCurrentIndex(index)
        base = path if path is not None and path.is_dir() else (path.parent if path is not None else self.project_root)
        menu = QMenu(self)
        if path is not None and path.is_file():
            open_editor = menu.addAction("在编辑器中打开")
            open_editor.triggered.connect(lambda: self._open_file(path, preview=False))
            self._add_external_file_actions(menu, path)
            menu.addSeparator()
        new_file = menu.addAction("新建文件")
        new_file.triggered.connect(lambda: self.start_new_file_entry(base))
        new_folder = menu.addAction("新建文件夹")
        new_folder.triggered.connect(lambda: self.start_new_folder_entry(base))
        menu.addSeparator()
        rename = menu.addAction("重命名")
        rename.setEnabled(path is not None and path != self.project_root)
        rename.triggered.connect(lambda: self.start_rename_entry(path))
        delete = menu.addAction("删除")
        delete.setEnabled(path is not None and path != self.project_root)
        delete.triggered.connect(lambda: self.delete_explorer_path(path))
        menu.addSeparator()
        open_terminal = menu.addAction("在终端中打开")
        open_terminal.triggered.connect(lambda: self.open_terminal_at(base))
        menu.exec(self.file_tree.viewport().mapToGlobal(point))


    def _add_external_file_actions(self, menu: QMenu, path: Path | None) -> None:
        if path is None or not path.is_file():
            return
        if path.suffix.casefold() in {".html", ".htm", ".xhtml"}:
            browser_action = menu.addAction("在浏览器中打开")
            browser_action.triggered.connect(lambda: self._open_external_file(path, browser=True))
        default_action = menu.addAction("使用默认程序打开")
        default_action.triggered.connect(lambda: self._open_external_file(path))


    def _open_external_file(self, path: Path, *, browser: bool = False) -> bool:
        try:
            target = Path(path).resolve(strict=True)
            if not target.is_relative_to(self.project_root) or not target.is_file():
                self._append_log("无法打开：请选择项目内的文件")
                return False
            relative = target.relative_to(self.project_root)
            if any(part.casefold() in _EXCLUDED_PROJECT_NAMES_CASEFOLD for part in relative.parts):
                self._append_log("无法打开：不能操作内部目录")
                return False
            if browser and target.suffix.casefold() not in {".html", ".htm", ".xhtml"}:
                self._append_log("在浏览器中打开需要 HTML 文件")
                return False
            opened = (webbrowser.open_new_tab(target.as_uri()) if browser
                      else QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))))
            if not opened:
                self._append_log(f"无法打开 {target.name}：未找到可用的{'浏览器' if browser else '默认程序'}")
                return False
            message = f"已请求{'浏览器' if browser else '默认程序'}打开 {relative.as_posix()}"
            if any(editor_path == target and editor.document().isModified()
                   for editor, editor_path in self._editor_paths.items()):
                message += "（显示磁盘版本，请先保存未保存的修改）"
            self._append_log(message)
            return True
        except (OSError, ValueError, webbrowser.Error) as exc:
            self._append_log(f"打开文件失败：{exc}")
            return False


    def delete_explorer_path(self, target: Path | None) -> None:
        if target is None:
            return
        target = Path(target)
        if _is_link_or_junction(target):
            self._append_log("删除失败：不能将链接当作普通项目文件夹删除")
            return
        target = target.resolve()
        if target == self.project_root or not target.is_relative_to(self.project_root):
            return
        relative = target.relative_to(self.project_root)
        if any(part.casefold() in _EXCLUDED_PROJECT_NAMES_CASEFOLD for part in relative.parts):
            self._append_log("删除失败：不能操作内部目录")
            return
        dirty = [
            path.name for editor, path in self._editor_paths.items()
            if (path == target or target in path.parents) and editor.document().isModified()
        ]
        if dirty:
            self._append_log(f"删除失败：存在未保存文件 {', '.join(dirty[:3])}")
            return
        answer = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除 {target.name} 吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            editors = [
                (self._editor_group_for(editor), editor)
                for editor, path in self._editor_paths.items()
                if path == target or target in path.parents
            ]
            for group, editor in editors:
                if group is not None:
                    self._close_editor_tab(group.indexOf(editor), group)
            self.refresh_file_tree()
            self._append_log(f"已删除 {target.relative_to(self.project_root).as_posix()}")
        except OSError as exc:
            self._append_log(f"删除失败：{exc}")


    def open_terminal_at(self, target: Path | None) -> None:
        if target is None:
            target = self.project_root
        target = target.resolve()
        if not target.is_dir() or not target.is_relative_to(self.project_root):
            target = self.project_root
        self.terminal_cwd = target
        self._toggle_bottom_panel(1)
        relative = target.relative_to(self.project_root).as_posix() or "."
        self._append_terminal_output(f"\n[cwd: {relative}]\n")
        self.terminal_input.setFocus()


    def refresh_file_tree(self) -> None:
        self.file_proxy.invalidate_search_index()
        root_index = self.file_model.setRootPath(str(self.project_root))
        self.file_tree.setRootIndex(self.file_proxy.mapFromSource(root_index))
        self.file_proxy.set_filter_text(self.command_search.text())
        self._append_log("资源管理器已刷新")
