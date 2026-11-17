"""SciDevHarness desktop coding client.

The shell intentionally follows the information hierarchy of a modern code
editor: activity bar, project explorer, editor workbench, and an Agent panel.
The coding Agent and its retry/audit services remain UI-independent in
``scidev/agent/core.py``.
"""

from __future__ import annotations

from scidev.desktop.chat_workflow import ChatWorkflowMixin

from scidev.desktop.terminal_workspace import TerminalWorkspaceMixin

from scidev.desktop.explorer_workspace import ExplorerWorkspaceMixin

from scidev.desktop.git_workspace import GitWorkspaceMixin

from scidev.desktop.window_layout import WindowLayoutMixin

from scidev.desktop.agent_panel import AgentPanelMixin
from scidev.desktop.agent_panel import PromptEdit as PromptEdit

from scidev.desktop.window_builders import WindowBuilderMixin

from scidev.desktop.agent_settings import AgentSettingsMixin
from scidev.desktop.agent_settings import LocalProviderCheckThread as LocalProviderCheckThread
from scidev.desktop.agent_settings import ProviderCheckThread as ProviderCheckThread

import argparse
import sys
import threading
import time
import webbrowser as webbrowser
from pathlib import Path
from typing import Any
from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSize,
    QSettings,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QDesktopServices as QDesktopServices, QTextDocument
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMainWindow, QMenu as QMenu, QMessageBox, QToolButton, QTreeWidget, QWidget
from PySide6.QtQuickWidgets import QQuickWidget

from scidev.agent.planning import planning_mode
from scidev.integrations.provider import (
    ProviderConfigError,
    load as load_provider_config,
)
from scidev.runtime.preferences import AppearancePreferences
from scidev.runtime.process import StreamingCommand, TerminalOutputDecoder as TerminalOutputDecoder
from scidev.agent.core import (
    CodingAgent,
    CodingToolbox,
    EventLedger,
    GitManager,
    RetryQueue,
    SummarySettings,
)
from scidev.desktop.highlighting import PythonHighlighter
from scidev.desktop.lsp_ui import ResidentLSPBridge
from scidev.desktop.patch_ui import PatchDiffPanel as PatchDiffPanel, PatchReviewCard as PatchReviewCard
from scidev.runtime.paths import (
    _is_link_or_junction as _is_link_or_junction,
    application_base_dir,
    default_sample_workspace as default_sample_workspace,
    resolve_initial_workspace as resolve_initial_workspace,
    resolve_startup_workspace as resolve_startup_workspace,
)
from scidev.desktop.chat_ui import ExecutionGroup, MessageBubble, NotificationToast as NotificationToast
from scidev.desktop.tree_ui import MAX_TREE_LAYOUT_BYTES as MAX_TREE_LAYOUT_BYTES, DevelopmentTreeView as DevelopmentTreeView
from scidev.desktop.shell_ui import ProjectFilterProxy as ProjectFilterProxy
from scidev.desktop.editor import CodeEditor as CodeEditor
from scidev.desktop.editor_workspace import EditorWorkspaceMixin
from scidev.desktop.widgets import ElidedLabel as ElidedLabel
from scidev.workspace.worktree import WorktreeError, WorktreeManager
from scidev.desktop.worktree_ui import WorktreeDialog
from scidev.runtime.pty import PTYSession
from scidev.runtime.crash import install_crash_reporter
from scidev.desktop.analytics_ui import ErrorTrendDialog
from scidev.desktop.pr_ui import PullRequestDialog
from scidev.integrations.mcp import MCPManager
from scidev.desktop.mcp_ui import MCPBrowserDialog
from scidev.workspace.intel import PythonIntel
from scidev.desktop.update_ui import UpdateDialog
from scidev.workspace.environment import ProjectEnvironmentInspector as ProjectEnvironmentInspector
from scidev.agent.usage import ModelRequestUsage
from scidev.desktop.theme import (
    DEFAULT_UI_PROFILE,
    THEME as THEME,
    UI_PROFILES,
    create_ui_font,
    stylesheet_for_profile,
    ui_profile_for_key,
    ui_scale_for_resolution,
)


from scidev.desktop.explorer_workspace import _EXCLUDED_PROJECT_NAMES_CASEFOLD as _EXCLUDED_PROJECT_NAMES_CASEFOLD


def _app_settings() -> QSettings:
    """Honor Qt's configured storage format, including isolated INI settings."""
    return QSettings(
        QSettings.defaultFormat(), QSettings.Scope.UserScope, "SciDevHarness", "SciDevHarness",
    )


def _choose_workspace_directory(initial_directory: Path, parent: QWidget | None = None) -> Path | None:
    """Show a Qt directory picker without the platform-native title bar."""
    dialog = QFileDialog(parent)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
    dialog.setWindowTitle("打开文件夹")
    dialog.setDirectory(str(initial_directory))
    profile_key = getattr(parent, "ui_profile", DEFAULT_UI_PROFILE)
    dialog.setStyleSheet(stylesheet_for_profile(THEME, profile_key))
    dialog.resize(860, 600)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    selections = dialog.selectedFiles()
    if not selections:
        return None
    selected = Path(selections[0]).expanduser().resolve()
    return selected if selected.is_dir() else None


class AppSignals(QObject):
    agent_event = Signal(str, object)
    worker_event = Signal(str, object)
    command_approval_requested = Signal(object)
    browser_approval_requested = Signal(object)
    patch_review_requested = Signal(object)
    git_status_ready = Signal(object)
    provider_check_finished = Signal(bool, str)


class ClientWindow(ChatWorkflowMixin, TerminalWorkspaceMixin, ExplorerWorkspaceMixin, GitWorkspaceMixin, WindowLayoutMixin, AgentPanelMixin, WindowBuilderMixin, AgentSettingsMixin, EditorWorkspaceMixin, QMainWindow):
    WORKBENCH_COMPACT_BREAKPOINT = 1000
    EXPLORER_COLLAPSE_BREAKPOINT = 900
    QUICK_SEARCH_BULK_EXPANSION_LIMIT = 150
    QUICK_SEARCH_VISIBLE_EXPANSION_BATCH_SIZE = 20
    WORKBENCH_WIDE_MINIMUMS = (260, 360, 280)
    WORKBENCH_COMPACT_MINIMUMS = (220, 320, 232)
    # Narrow windows keep the code editor at a usable width before the Agent
    # pane takes its percentage share.
    WORKBENCH_COMPACT_EDITOR_MINIMUM = 480
    CODE_INTEL_DEBOUNCE_MS = 250
    CODE_INTEL_MAX_PARSE_CHARS = 400_000

    def __init__(self, project_root: Path):
        super().__init__()
        self.project_root = Path(project_root).resolve()
        self.setWindowTitle("SciDevHarness — Coding Workspace")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(780, 480)
        self._base_minimum_size = QSize(self.minimumSize())
        self._ui_scale = 1.0
        self._base_ui_font = create_ui_font()
        self.setFont(self._base_ui_font)
        self._screen_window_handle = None
        self._screen_metric_signal_screen = None
        self._screen_metric_connections = []
        initial_screen = self.screen()
        self._offscreen_platform = QApplication.platformName().casefold() == "offscreen"
        if initial_screen is not None and not self._offscreen_platform:
            initial_geometry = initial_screen.geometry()
            available_geometry = initial_screen.availableGeometry()
            self._ui_scale = ui_scale_for_resolution(
                initial_geometry.width(), initial_geometry.height()
            )
            initial_width = min(
                round(1500 * self._ui_scale),
                max(780, round(available_geometry.width() * 0.96)),
            )
            initial_height = min(
                round(920 * self._ui_scale),
                max(480, round(available_geometry.height() * 0.92)),
            )
            self.resize(initial_width, initial_height)
        else:
            # Qt's offscreen test backend reports a tiny synthetic screen
            # (often 800x600); using it as a real display would force normal
            # workbench tests into the compact layout and hide the explorer.
            self.resize(1500, 920)
        self.ui_settings = _app_settings()
        self.appearance_preferences = AppearancePreferences.load(self.ui_settings)
        self._base_ui_font = create_ui_font(self.appearance_preferences.font_size,
                                          self.appearance_preferences.font_family,
                                          self.appearance_preferences.font_weight)
        self.setFont(self._base_ui_font)
        self._screen_dimensions = (initial_screen.geometry().width(), initial_screen.geometry().height()) if initial_screen is not None and not self._offscreen_platform else (1280, 720)
        self._ui_scale = self._resolved_ui_scale(*self._screen_dimensions)
        try:
            self.planning_mode = planning_mode(self.ui_settings.value("planningMode", "auto"))
        except ValueError:
            self.planning_mode = "auto"
        self.ui_profile = ui_profile_for_key(
            self.ui_settings.value("uiProfile", DEFAULT_UI_PROFILE)
        ).key
        self.setStyleSheet(stylesheet_for_profile(THEME, self.ui_profile, self._ui_scale, self.appearance_preferences))
        self.ledger = EventLedger(self.project_root)
        self.git = GitManager(self.project_root)
        self.toolbox = CodingToolbox(self.project_root, self.ledger)
        self.summary_settings = SummarySettings.load(self.project_root)
        try:
            self.provider_config = load_provider_config(self.project_root)
        except (ProviderConfigError, OSError, ValueError):
            self.provider_config = None
        self._available_models: list[str] = []
        self._pending_model_choice = ""
        self._detected_local_providers: list[dict[str, Any]] = []
        self._local_provider_thread: LocalProviderCheckThread | None = None
        self.model_usage = ModelRequestUsage.empty()
        self._context_usage_estimated_tokens = 0
        self._context_usage_actual_tokens: int | None = None
        self._context_usage_session_id = ""
        self.signals = AppSignals()
        self.current_session_id: str | None = None
        self._pending_images: list[dict[str, Any]] = []
        self.active_task_id: str | None = None
        self._cancel_requested = False
        self._streaming_bubbles: dict[tuple[str, int], MessageBubble] = {}
        self._execution_group_active = False
        self._execution_group_session_id = ""
        self._active_execution_group: ExecutionGroup | None = None
        self._pending_patch_reviews: dict[int, dict[str, Any]] = {}
        self._active_notification: NotificationToast | None = None
        self.project_context_snapshot: dict[str, Any] = {}
        self._project_environment_report = ""
        self._environment_check_running = False
        self._context_preview_pending = False
        self._context_generation = 0
        self._last_context_query = ""
        self.current_file: Path | None = None
        self._editor_paths: dict[QWidget, Path] = {}
        # QTextDocument normalizes newline encodings; retain each file's EOL for save.
        self._editor_line_endings: dict[QWidget, str] = {}
        self._shared_editor_documents: dict[Path, QTextDocument] = {}
        self._editor_titles: dict[QWidget, str] = {}
        self._editor_highlighters: dict[QWidget, PythonHighlighter] = {}
        self._editor_document_change_connections: dict[QWidget, object] = {}
        self._pinned_editors: set[QWidget] = set()
        self._preview_editor: QWidget | None = None
        self.welcome_editor: CodeEditor | None = None
        self._closing = False
        self.task_dialog: QDialog | None = None
        self.task_history_tree: QTreeWidget | None = None
        self.window_max_button: QToolButton | None = None
        self.terminal_process: StreamingCommand | None = None
        self.terminal_pty: PTYSession | None = None
        self._terminal_buffer = ""
        self._terminal_decoders: dict[str, Any] = {}
        self._terminal_started_at = 0.0
        self._terminal_timer = QTimer(self)
        self._terminal_timer.setInterval(40)
        self._terminal_timer.timeout.connect(self._poll_terminal_output)
        self._code_intel_timer = QTimer(self)
        self._code_intel_timer.setSingleShot(True)
        self._code_intel_timer.setInterval(self.CODE_INTEL_DEBOUNCE_MS)
        self._code_intel_timer.timeout.connect(self._refresh_code_intel)
        self._resident_lsp_bridge: ResidentLSPBridge | None = None
        self._lsp_generation = 0
        self._lsp_diagnostics: dict[str, list[dict]] = {}
        self._code_intel_syntax_errors: dict[str, SyntaxError] = {}
        self.terminal_cwd = self.project_root
        self._entry_parent = self.project_root
        self._explorer_entry_mode = "create"
        self._rename_target: Path | None = None
        self._navigation_back: list[tuple[Path, int, int]] = []
        self._navigation_forward: list[tuple[Path, int, int]] = []
        self._workspace_search_whole_word = False
        self._workspace_search_case_sensitive = False
        self._workbench_user_ratios: tuple[float, float] | None = UI_PROFILES[
            self.ui_profile
        ].layout_ratios
        self._workbench_user_ratios = self._saved_workbench_ratios(self.ui_profile)
        self._workbench_adapting = False
        self._workbench_adapt_pending = False
        self._explorer_visibility_override: bool | None = {"show": True, "hide": False}.get(self.appearance_preferences.explorer_mode)
        self._git_focus = False
        self._git_details_expanded = False
        self._git_details_forced = False
        self._git_chat_visible = False
        self._titlebar_compact: bool | None = None
        self._summary_settings_compact: bool | None = None
        self._git_tree_compact_restore: tuple[float, float, float] | None = None
        self._git_tree_compact_auto_view: tuple[float, float, float] | None = None
        self._git_tree_short_layout: bool | None = None

        self.agent = CodingAgent(
            self.project_root,
            self.ledger,
            self.git,
            event_callback=self._emit_agent,
            summary_settings=self.summary_settings,
            command_approval=self._request_command_approval,
            browser_approval=self._request_browser_approval,
            require_verification=True,
            patch_review=self._request_patch_review,
        )
        self.worker = RetryQueue(
            self.ledger,
            {"coding": self._handle_coding},
            event_callback=self._emit_worker,
            retry_base_seconds=5.0,
        )
        self.signals.agent_event.connect(self._handle_agent_event)
        self.signals.worker_event.connect(self._handle_worker_event)
        self.signals.command_approval_requested.connect(self._handle_command_approval_request)
        self.signals.browser_approval_requested.connect(self._handle_browser_approval_request)
        self.signals.patch_review_requested.connect(self._handle_patch_review_request)
        self.signals.git_status_ready.connect(self._apply_git_snapshot)
        self._git_status_lock = threading.Lock()
        self._git_status_busy = False
        self._git_status_dirty = False
        self._git_status_seq = 0
        self._git_status_applied = 0
        self._build_ui()
        self.set_ui_profile(self.ui_profile, persist=False)
        self._apply_screen_resolution(*self._screen_dimensions)
        self._install_shortcuts()
        self.worker.start()
        self.refresh_all()
        self._append_chat("系统", "准备好了。打开项目文件开始编辑，或直接在右侧告诉 Agent 要修改什么。", "meta")

    def _choose_workspace(self) -> None:
        selected = _choose_workspace_directory(self.project_root, self)
        if selected is not None:
            self._open_workspace_root(selected)

    def _show_worktree_dialog(self) -> None:
        dialog = WorktreeDialog(WorktreeManager(self.project_root), self.project_root, self)
        dialog.open_requested.connect(lambda path: self._open_workspace_root(path))
        dialog.exec()

    def _show_mcp_browser(self) -> None:
        manager = MCPManager(self.project_root, ledger=getattr(self.agent, "ledger", None))
        dialog = MCPBrowserDialog(manager, self)
        dialog.insert_requested.connect(self._insert_mcp_text)
        dialog.finished.connect(lambda _result: manager.close())
        dialog.exec()

    def _insert_mcp_text(self, text: str) -> None:
        value = str(text).strip()
        if not value:
            return
        self.chat_input.insertPlainText(value)
        self.chat_input.setFocus()

    def _show_error_trends(self) -> None:
        ErrorTrendDialog(self.project_root, self).exec()

    def _show_pull_request(self) -> None:
        PullRequestDialog(self.project_root, self).exec()

    def _show_update_dialog(self) -> None:
        UpdateDialog.show_for_workspace_async(self.project_root, self)

    def _create_isolated_worktree(self) -> "ClientWindow | None":
        counter = getattr(self, "_gui_worktree_counter", 0) + 1
        self._gui_worktree_counter = counter
        task_id = f"gui-{time.strftime('%Y%m%d-%H%M%S')}-{counter}"
        try:
            worktree = WorktreeManager(self.project_root).create(task_id)
        except WorktreeError as exc:
            self._show_message("无法创建隔离工作区", str(exc))
            return None
        window = self._open_workspace_root(worktree.path)
        if window is None:
            self._append_log(f"隔离 worktree 已创建但无法打开：{worktree.path}")
            return None
        window._append_log(f"隔离 worktree：{worktree.branch}")
        self.statusBar().showMessage(f"已创建 {worktree.branch}", 5000)
        return window

    def _open_workspace_root(self, workspace: str | Path) -> "ClientWindow | None":
        target = Path(workspace).expanduser().resolve()
        if not target.is_dir():
            self._show_message("无法打开文件夹", f"所选工作区不存在或不是文件夹：\n{target}")
            return None

        app = QApplication.instance()
        if app is None:
            return None
        windows: list[ClientWindow] = getattr(app, "_scidev_windows", [])
        if self not in windows:
            windows.append(self)
        app._scidev_windows = windows

        settings = _app_settings()
        for window in windows:
            if window.project_root == target:
                settings.setValue("lastWorkspace", str(target))
                settings.sync()
                if window.isMinimized():
                    window.showNormal()
                window.raise_()
                window.activateWindow()
                return window

        settings.setValue("lastWorkspace", str(target))
        settings.sync()
        new_window = ClientWindow(target)
        windows.append(new_window)
        new_window.show()
        return new_window

    def _clear_active_notification(self) -> None:
        notification = self.sender()
        if notification is not self._active_notification:
            return
        self._active_notification = None
        notification.deleteLater()

    def _position_notification(self) -> None:
        notification = self._active_notification
        if notification is None or not hasattr(self, "workspace_stack"):
            return
        root = self.centralWidget()
        if root is None:
            return
        anchor = self.workspace_stack.mapTo(root, QPoint(0, 0))
        anchor_rect = QRect(anchor, self.workspace_stack.size())
        width = max(1, min(440, anchor_rect.width() - 24))
        notification.setFixedWidth(width)
        notification.adjustSize()
        height = notification.sizeHint().height()
        x = anchor_rect.x() + max(8, anchor_rect.width() - width - 12)
        y = max(8, root.height() - height - 14)
        notification.setGeometry(x, y, width, height)

    def _emit_agent(self, kind: str, data: dict[str, Any]) -> None:
        if not self._closing:
            self.signals.agent_event.emit(kind, data)

    def _request_command_approval(
        self,
        command: str,
        timeout_seconds: int,
        *,
        purpose: str = "允许 Agent 执行这条终端命令？",
        explanation: str = "",
    ) -> bool:
        if self._closing:
            return False
        decision_ready = threading.Event()
        request = {
            "command": command,
            "timeout_seconds": timeout_seconds,
            "purpose": purpose,
            "explanation": explanation,
            "decision_ready": decision_ready,
            "approved": False,
        }
        self.signals.command_approval_requested.emit(request)
        decision_ready.wait()
        return bool(request["approved"])

    def _handle_command_approval_request(self, request: dict[str, Any]) -> None:
        decision_ready = request["decision_ready"]
        try:
            if self._closing:
                return
            command = str(request.get("command") or "")
            timeout_seconds = int(request.get("timeout_seconds") or 120)
            dialog = QMessageBox(self)
            dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
            dialog.setWindowTitle("安全确认")
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setTextFormat(Qt.TextFormat.PlainText)
            dialog.setText(str(request.get("purpose") or "允许 Agent 执行这条终端命令？"))
            informative_text = (
                f"工作目录：{self.project_root}\n"
                f"超时上限：{timeout_seconds} 秒。命令将以当前 Windows 用户权限执行。\n"
                + (str(request.get("explanation") or "") + "\n" if request.get("explanation") else "")
                + "展开详细信息可检查完整命令。"
            )
            dialog.setInformativeText(informative_text)
            dialog.setDetailedText(command)
            dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            dialog.button(QMessageBox.StandardButton.Yes).setText("允许这一次")
            dialog.button(QMessageBox.StandardButton.No).setText("拒绝")
            dialog.setDefaultButton(QMessageBox.StandardButton.No)
            approved = dialog.exec() == QMessageBox.StandardButton.Yes
            request["approved"] = approved
            self._append_log("已批准终端命令" if approved else "已拒绝终端命令")
        finally:
            decision_ready.set()

    def _request_browser_approval(self, action: str, details: str) -> bool:
        if self._closing:
            return False
        decision_ready = threading.Event()
        request = {
            "action": str(action)[:80],
            "details": str(details)[:1400],
            "decision_ready": decision_ready,
            "approved": False,
        }
        self.signals.browser_approval_requested.emit(request)
        decision_ready.wait()
        return bool(request["approved"])

    def _handle_browser_approval_request(self, request: dict[str, Any]) -> None:
        decision_ready = request["decision_ready"]
        try:
            if self._closing:
                return
            dialog = QMessageBox(self)
            dialog.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
            dialog.setWindowTitle("浏览器操作确认")
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setTextFormat(Qt.TextFormat.PlainText)
            dialog.setText("允许 Agent 在独立 Edge 浏览器中执行此操作？")
            dialog.setInformativeText(
                "浏览器使用临时独立配置，不会读取你的 Edge 登录态。网页会收到本次访问；"
                "点击可能提交表单或改变网站数据。网页文字可能包含恶意指令，请检查操作内容。"
            )
            dialog.setDetailedText(str(request.get("details") or "")[:1400])
            approve_button = dialog.addButton("允许这一次", QMessageBox.ButtonRole.AcceptRole)
            deny_button = dialog.addButton("拒绝", QMessageBox.ButtonRole.RejectRole)
            dialog.setDefaultButton(deny_button)
            dialog.setEscapeButton(deny_button)
            dialog.exec()
            approved = dialog.clickedButton() is approve_button
            request["approved"] = approved
            self._append_log("已批准浏览器操作" if approved else "已拒绝浏览器操作")
        finally:
            decision_ready.set()

    def _request_patch_review(self, preview: dict[str, Any]) -> set[int] | None:
        if self._closing:
            return None
        decision_ready = threading.Event()
        request: dict[str, Any] = {
            "preview": preview,
            "decision_ready": decision_ready,
            "selected_hunks": None,
            "resolved": False,
            "card": None,
        }
        request_id = id(request)
        self._pending_patch_reviews[request_id] = request
        self.signals.patch_review_requested.emit(request)
        decision_ready.wait()
        self._pending_patch_reviews.pop(request_id, None)
        selected = request.get("selected_hunks")
        return set(selected) if isinstance(selected, set) else None

    def _handle_patch_review_request(self, request: dict[str, Any]) -> None:
        if self._closing or request.get("resolved"):
            request["selected_hunks"] = None
            request["resolved"] = True
            request["decision_ready"].set()
            return
        card = PatchReviewCard(request.get("preview", {}))
        request["card"] = card
        card.decision.connect(lambda selected, pending=request: self._resolve_patch_review(pending, selected))
        card.open_requested.connect(lambda _preview, pending=request: self._open_patch_diff_tab(pending))
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, card)
        QTimer.singleShot(
            0,
            self._scroll_chat_to_bottom,
        )

    def _open_patch_diff_tab(self, request: dict[str, Any]) -> None:
        if self._closing or request.get("resolved"):
            return
        group = self._active_editor_group or self.editor_tabs
        panel = request.get("diff_panel")
        if not isinstance(panel, PatchDiffPanel):
            panel = PatchDiffPanel(request.get("preview", {}))
            request["diff_panel"] = panel
            index = group.addTab(panel, "补丁 diff")
            self._install_tab_close_button(group, index, panel)
            group.setTabToolTip(index, "待审核补丁的编辑器 diff；勾选修改块后应用")
            panel.decision.connect(
                lambda selected, pending=request: self._finish_patch_diff_tab(pending, selected)
            )
        group.setCurrentWidget(panel)
        panel.setFocus()

    def _finish_patch_diff_tab(self, request: dict[str, Any], selected: object) -> None:
        panel = request.get("diff_panel")
        if isinstance(panel, PatchDiffPanel):
            group = self._editor_group_for(panel) or self._active_editor_group or self.editor_tabs
            index = group.indexOf(panel)
            if index >= 0:
                group.removeTab(index)
            panel.deleteLater()
            request["diff_panel"] = None
        card = request.get("card")
        if isinstance(card, PatchReviewCard):
            card._finish(selected if isinstance(selected, set) else None)
        else:
            self._resolve_patch_review(request, selected)

    def _resolve_patch_review(self, request: dict[str, Any], selected: object) -> None:
        if request.get("resolved"):
            return
        panel = request.get("diff_panel")
        if isinstance(panel, PatchDiffPanel):
            group = self._editor_group_for(panel) or self._active_editor_group or self.editor_tabs
            index = group.indexOf(panel)
            if index >= 0:
                group.removeTab(index)
            panel.deleteLater()
            request["diff_panel"] = None
        request["selected_hunks"] = set(selected) if isinstance(selected, set) else None
        request["resolved"] = True
        request["decision_ready"].set()

    def _emit_worker(self, kind: str, data: dict[str, Any]) -> None:
        if not self._closing:
            self.signals.worker_event.emit(kind, data)

    def _handle_worker_event(self, event: str, payload: dict[str, Any]) -> None:
        task_id = payload.get("task_id", "")
        if event == "task_started" and task_id == self.active_task_id:
            task_payload = payload.get("payload")
            task_payload = task_payload if isinstance(task_payload, dict) else {}
            retry_session_id = str(task_payload.get("session_id") or "")
            if not self._execution_group_active and payload.get("kind") == "coding" and retry_session_id:
                self._execution_group_active = True
                self._execution_group_session_id = retry_session_id
                self._active_execution_group = None
            if self._active_execution_group is not None:
                self._active_execution_group.set_state("running")
                self._active_execution_group.set_activity("Agent 正在处理")
            self._set_state(self.chat_status, "工作中 · Agent 正在修改项目", "StateWorking")
            self._set_state(self.workspace_state, "● 工作中", "StateWorking")
        elif event == "task_retry" and task_id == self.active_task_id:
            self._move_streams_to_execution_group(
                self._execution_group_session_id,
                speaker="Agent · 网络中断，等待自动重试",
            )
            self._interrupt_streaming_bubbles("Agent · 网络中断，等待自动重试")
            if self._active_execution_group is not None:
                self._active_execution_group.set_state("waiting")
            self.task_plan_panel.set_run_state("waiting")
            self._set_state(self.chat_status, "等待网络 · 自动重试中", "StateWorking")
            self._set_state(self.workspace_state, "● 重试中", "StateWorking")
            self._refresh_open_file_after_task()
        elif event == "task_completed" and task_id == self.active_task_id:
            self._finish_execution_group("completed")
            self.active_task_id = None
            self._cancel_requested = False
            self._sync_stop_button()
            self.task_plan_panel.set_run_state("completed")
            self._set_state(self.chat_status, "就绪 · 可以继续对话", "StateReady")
            self._set_state(self.workspace_state, "● 就绪", "StateReady")
        elif event == "task_failed" and task_id == self.active_task_id:
            error_message = str(payload.get("error") or "任务失败，未返回详细信息")
            self._move_streams_to_execution_group(
                self._execution_group_session_id,
                speaker="Agent · 未完成回复",
            )
            self._interrupt_streaming_bubbles("Agent · 未完成回复")
            self._finish_execution_group("failed")
            self.active_task_id = None
            self._sync_stop_button()
            cancelled = self._cancel_requested
            self._cancel_requested = False
            if cancelled:
                self.task_plan_panel.set_run_state("failed")
                self._set_state(self.chat_status, "已停止 · 可以继续对话", "StateReady")
                self._set_state(self.workspace_state, "● 已停止", "StateReady")
                self.problems_output.setPlainText(error_message)
                self._append_chat(
                    "Harness · 任务已停止",
                    "已按你的请求停止；本次改动未自动提交，仍保留在工作区。",
                    "system",
                )
            else:
                self.task_plan_panel.set_run_state("failed")
                self._set_state(self.chat_status, "任务失败 · 可从历史重试", "StateError")
                self._set_state(self.workspace_state, "● 出错", "StateError")
                self.problems_output.setPlainText(error_message)
                visible_error = error_message if len(error_message) <= 2400 else error_message[:2400].rstrip() + "…"
                error_kind = str(payload.get("error_kind") or "")
                if error_kind and error_kind != "internal":
                    visible_error = f"[{error_kind}] {visible_error}"
                self._append_chat("Harness · 任务失败", visible_error, "system")
            self._refresh_open_file_after_task()
        self._append_log(self._worker_message(event, payload))
        self.refresh_git_status()
        self.refresh_task_history()
        self._refresh_development_tree()
        self._refresh_session_list()

    def _set_activity_button(self, index: int) -> None:
        buttons = getattr(self, "activity_buttons", [])
        if 0 <= index < len(buttons):
            buttons[index].setChecked(True)

    def refresh_all(self) -> None:
        self.refresh_git_status()
        self.connection_label.setText(self._provider_state())
        self.connection_label.setObjectName("StateReady" if self._provider_configured() else "StateWorking")
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)
        self.agent_title.setText(self._provider_model() if self._provider_configured() else "Coding Agent")
        self._refresh_model_selector()
        self.refresh_task_history()
        self._refresh_development_tree()

    def show_workspace(self) -> None:
        self._git_focus = False
        self._set_activity_button(0)
        self.workspace_stack.setCurrentWidget(self.workspace_page)
        self._adapt_workbench_layout()
        self._append_log("已返回编码工作区")

    def focus_explorer(self) -> None:
        workspace_is_active = self.workspace_stack.currentWidget() is self.workspace_page
        should_show = self.explorer.isHidden() if workspace_is_active else True
        self._explorer_visibility_override = should_show
        self.explorer.setVisible(should_show)
        self.show_workspace()
        self._adapt_workbench_layout()
        if should_show:
            self.file_tree.setFocus()
        else:
            editor = self._active_editor()
            if editor is not None:
                editor.setFocus()

    def _handle_coding(self, task: dict[str, Any]) -> dict[str, Any]:
        return self.agent.run(task)

    def eventFilter(self, watched: QObject, event) -> bool:  # noqa: ANN001 - Qt event signature.
        if watched is getattr(self, "welcome_content", None):
            if event.type() == QEvent.Type.Resize:
                QTimer.singleShot(0, self._update_welcome_text_sizes)
            return False
        if watched is getattr(self, "connection_label", None) and event.type() == QEvent.Type.MouseButtonRelease:
            if self._git_focus:
                self._git_chat_visible = True
                self._adapt_workbench_layout()
            if self.chat_body_stack.currentWidget() is not self.summary_settings_scroll:
                self.toggle_summary_settings()
            return True
        if watched is getattr(self, "connection_label", None):
            return False
        if watched is self.file_tree.viewport() and event.type() == QEvent.Type.MouseButtonDblClick:
            index = self.file_tree.indexAt(event.position().toPoint())
            self._on_file_selected(index)
            return True
        if isinstance(watched, CodeEditor) and event.type() == QEvent.Type.FocusIn:
            group = self._editor_group_for(watched)
            if group is not None:
                self._active_editor_group = group
        if isinstance(watched, CodeEditor) and event.type() == QEvent.Type.ContextMenu:
            menu = watched.createStandardContextMenu()
            menu.addSeparator()
            go_to_definition = menu.addAction("转到定义")
            go_to_definition.triggered.connect(self._go_to_definition)
            find_references = menu.addAction("查找引用")
            find_references.triggered.connect(self._find_symbol_references)
            rename_symbol = menu.addAction("重命名符号")
            rename_symbol.triggered.connect(self._rename_current_symbol)
            format_document = menu.addAction("格式化当前文档")
            format_document.triggered.connect(self._format_current_document)
            menu.exec(event.globalPos())
            return True
        if isinstance(watched, CodeEditor) and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_S and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.save_current_file()
                return True
            if event.key() == Qt.Key.Key_W and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._close_editor_tab(self._active_editor_group.currentIndex() if self._active_editor_group is not None else -1)
                return True
            if event.key() == Qt.Key.Key_F2 and not event.modifiers():
                self._rename_current_symbol()
                return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        if self._closing:
            event.accept()
            return
        dirty_editors = []
        dirty_paths: set[Path] = set()
        for editor, path in self._editor_paths.items():
            if (
                isinstance(editor, CodeEditor)
                and editor.document().isModified()
                and path not in dirty_paths
            ):
                dirty_paths.add(path)
                dirty_editors.append((editor, path))
        if dirty_editors:
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setWindowTitle("未保存的修改")
            dialog.setText(f"有 {len(dirty_editors)} 个文件尚未保存。")
            filenames = [path.name for _editor, path in dirty_editors[:5]]
            if len(dirty_editors) > len(filenames):
                filenames.append(f"另有 {len(dirty_editors) - len(filenames)} 个文件")
            dialog.setInformativeText("、".join(filenames))
            save_all = dialog.addButton("保存全部", QMessageBox.ButtonRole.AcceptRole)
            discard = dialog.addButton("放弃修改", QMessageBox.ButtonRole.DestructiveRole)
            cancel = dialog.addButton("继续编辑", QMessageBox.ButtonRole.RejectRole)
            dialog.setDefaultButton(save_all)
            dialog.setEscapeButton(cancel)
            dialog.exec()
            choice = dialog.clickedButton()
            if choice is cancel or choice not in (save_all, discard):
                event.ignore()
                return
            if choice is save_all:
                failures: list[str] = []
                for editor, path in dirty_editors:
                    try:
                        relative = path.relative_to(self.project_root).as_posix()
                        self.toolbox.write_file(relative, editor.toPlainText())
                    except Exception as exc:  # noqa: BLE001 - keep all unsaved buffers alive on failure.
                        failures.append(f"{path.name}: {exc}")
                        continue
                    editor.document().setModified(False)
                    self._update_editor_tab_for(editor)
                if failures:
                    self._append_log("关闭已取消，以下文件未能保存：" + "；".join(failures)[:360])
                    event.ignore()
                    return
        self._closing = True
        self._code_intel_timer.stop()
        # A running task must not keep editing or auto-committing after the
        # workspace window is gone.
        self.agent.request_cancel(self.active_task_id)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with self._git_status_lock:
                if not self._git_status_busy and not self._git_status_dirty:
                    break
            time.sleep(0.01)
        for request in list(self._pending_patch_reviews.values()):
            self._resolve_patch_review(request, None)
        if self.terminal_process is not None:
            self.terminal_process.kill()
        if self.terminal_pty is not None:
            self.terminal_pty.close()
            self.terminal_pty = None
        self._terminal_timer.stop()
        self.worker.stop()
        self.agent.toolbox.close()
        PythonIntel.shutdown()
        completion_popup = getattr(self, "_completion_popup", None)
        if completion_popup is not None:
            completion_popup.close()
            self._completion_popup = None
        resident_lsp = getattr(self, "_resident_lsp_bridge", None)
        if resident_lsp is not None:
            resident_lsp.close()
            self._resident_lsp_bridge = None
        local_thread = getattr(self, "_local_provider_thread", None)
        if local_thread is not None and local_thread.isRunning():
            local_thread.wait(2500)
        self.file_model.setRootPath("")
        app = QApplication.instance()
        windows = getattr(app, "_scidev_windows", []) if app is not None else []
        if self in windows:
            windows.remove(self)
        event.accept()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SciDevHarness desktop coding client")
    parser.add_argument(
        "--workspace",
        type=Path,
        help="open this project folder (defaults to the most recently used folder)",
    )
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    source_root = application_base_dir()
    if args.workspace is not None:
        try:
            workspace_root = resolve_startup_workspace(args.workspace, None, source_root)
        except ValueError as exc:
            parser.error(str(exc))

    app = QApplication([sys.argv[0]])
    app.setApplicationName("SciDevHarness")
    app.setOrganizationName("SciDevHarness")
    app.setOrganizationDomain("scidevharness.local")
    app.setApplicationDisplayName("SciDevHarness")
    app.setFont(create_ui_font())

    settings = _app_settings()
    if args.workspace is None:
        last_workspace = str(settings.value("lastWorkspace", "") or "")
        is_packaged = bool(getattr(sys, "frozen", False) or "__compiled__" in globals())
        has_valid_last_workspace = bool(last_workspace and Path(last_workspace).expanduser().is_dir())
        has_bundled_sample = default_sample_workspace(source_root) != source_root
        if is_packaged and not has_valid_last_workspace and not has_bundled_sample:
            selected = _choose_workspace_directory(Path.home())
            if selected is None:
                return 0
            workspace_root = selected
        else:
            workspace_root = resolve_startup_workspace(None, last_workspace, source_root)

    if not args.smoke_test:
        settings.setValue("lastWorkspace", str(workspace_root))
        settings.sync()
    app._scidev_crash_reporter = install_crash_reporter(workspace_root)
    window = ClientWindow(workspace_root)
    app._scidev_windows = [window]
    window.show()
    if args.smoke_test:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            app.processEvents()
            if window.git_tree.status() != QQuickWidget.Status.Loading:
                break
            time.sleep(0.01)
        qml_ready = window.git_tree.status() == QQuickWidget.Status.Ready and not window.git_tree.errors()
        if not qml_ready:
            errors = "; ".join(error.toString() for error in window.git_tree.errors())
            print(f"QML startup smoke test failed: {errors or 'loading timeout'}", file=sys.stderr)
        window.close()
        app.processEvents()
        return 0 if qml_ready else 1
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
