"""Construct the desktop shell and editor/output panes.

The host retains state and lifecycle ownership; this module does not import its entry point.
"""

from __future__ import annotations

from PySide6.QtCore import QDir
from PySide6.QtCore import QSize
from PySide6.QtCore import Qt
from PySide6.QtCore import QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QDialog
from PySide6.QtWidgets import QFileSystemModel
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QLayout
from PySide6.QtWidgets import QMenu
from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QScrollArea
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QSizeGrip
from PySide6.QtWidgets import QSplitter
from PySide6.QtWidgets import QStackedWidget
from PySide6.QtWidgets import QStatusBar
from PySide6.QtWidgets import QTabBar
from PySide6.QtWidgets import QTabWidget
from PySide6.QtWidgets import QToolButton
from PySide6.QtWidgets import QTreeView
from PySide6.QtWidgets import QTreeWidget
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from scidev.desktop.settings import SettingsPage
from scidev.agent.project_settings import SummarySettings
from scidev.desktop.icons import activity_icon
from scidev.desktop.chat_ui import NotificationToast as NotificationToast
from scidev.desktop.shell_ui import ProjectFilterProxy as ProjectFilterProxy
from scidev.desktop.shell_ui import WindowTitleBar
from scidev.desktop.editor import CodeEditor as CodeEditor
from scidev.desktop.widgets import ElidedLabel as ElidedLabel
from scidev.desktop.theme import ACTIVITY_ICON_SIZE
from scidev.desktop.theme import UI_PROFILES


class WindowBuilderMixin:
    """Operate on host state; introduce no initialization or additional state."""

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_title_bar())

        workbench = QSplitter(Qt.Orientation.Horizontal)
        self.workbench = workbench
        workbench.setObjectName("Workbench")
        workbench.setChildrenCollapsible(False)
        workbench.setHandleWidth(5)
        workbench.addWidget(self._build_left_panel())
        self.workspace_stack = QStackedWidget()
        self.workspace_stack.setMinimumWidth(self.WORKBENCH_COMPACT_MINIMUMS[1])
        self.workspace_page = self._build_workspace()
        self.git_page = self._build_git_page()
        self.workspace_stack.addWidget(self.workspace_page)
        self.workspace_stack.addWidget(self.git_page)
        self.settings_page = SettingsPage()
        self.settings_page.value_changed.connect(self._on_appearance_setting_changed)
        self.settings_page.reset_requested.connect(self._reset_appearance)
        self.settings_page.reset_layout_requested.connect(self._reset_workbench_layout)
        self.settings_page.close_requested.connect(self._close_settings)
        self.settings_page.agent_requested.connect(self._open_agent_settings_from_preferences)
        self.workspace_stack.addWidget(self.settings_page)
        self.workspace_stack.currentChanged.connect(self._on_workspace_stack_page_changed)
        workbench.addWidget(self.workspace_stack)
        self.chat_panel = self._build_chat_panel()
        workbench.addWidget(self.chat_panel)
        self.summary_settings_scroll.takeWidget()
        self.settings_page.attach_agent_settings(
            self.summary_settings_panel,
            lambda: self.summary_settings != SummarySettings(),
        )
        profile = UI_PROFILES[self.ui_profile]
        workbench.setSizes(
            [
                round(profile.layout_ratios[0] * 1000),
                1,
                round(profile.layout_ratios[1] * 1000),
            ]
        )
        workbench.setStretchFactor(0, 0)
        workbench.setStretchFactor(1, 1)
        workbench.setStretchFactor(2, 0)
        workbench.splitterMoved.connect(self._on_workbench_splitter_moved)
        root_layout.addWidget(workbench, 1)
        self.setCentralWidget(root)
        self._quick_search_expand_query = ""
        self._quick_search_expand_directory_keys: set[str] = set()
        self._quick_search_expand_timer = QTimer(self)
        self._quick_search_expand_timer.setSingleShot(True)
        self._quick_search_expand_timer.setInterval(80)
        self._quick_search_expand_timer.timeout.connect(self._expand_filtered_file_branches_now)
        self._quick_search_visible_expand_timer = QTimer(self)
        self._quick_search_visible_expand_timer.setSingleShot(True)
        self._quick_search_visible_expand_timer.setInterval(12)
        self._quick_search_visible_expand_timer.timeout.connect(
            self._expand_visible_filtered_file_branches
        )
        self.file_tree.verticalScrollBar().valueChanged.connect(
            self._schedule_visible_filtered_branch_expansion
        )
        self.file_model.rowsInserted.connect(self._schedule_visible_filtered_branch_expansion)
        self.command_search.textChanged.connect(self.file_proxy.set_filter_text)
        self.command_search.textChanged.connect(self._update_command_suggestions)
        self.command_search.textChanged.connect(self._expand_filtered_file_branches)
        self.command_search.returnPressed.connect(self._open_quick_search)

        status = QStatusBar()
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("StatusText")
        status.addWidget(self.status_label, 1)
        status.messageChanged.connect(lambda message: self.status_label.setVisible(not bool(message)))
        self.status_branch = QLabel("Git · 未初始化")
        self.status_branch.setObjectName("StatusBranch")
        self.status_branch.setMinimumWidth(0)
        self.status_branch.setMaximumWidth(300)
        self.status_branch.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        status.addPermanentWidget(self.status_branch)
        self.status_position = QLabel("Ln 1, Col 1")
        self.status_position.setVisible(False)
        status.addPermanentWidget(self.status_position)
        status.addPermanentWidget(QLabel("UTF-8"))
        self.status_language = QLabel("Plain Text")
        status.addPermanentWidget(self.status_language)
        status.addPermanentWidget(QSizeGrip(self))
        self.setStatusBar(status)


    def _install_shortcuts(self) -> None:
        """Install workbench-wide shortcuts expected from a code editor."""
        self.quick_open_shortcut = QShortcut(QKeySequence("Ctrl+P"), self)
        self.quick_open_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.quick_open_shortcut.activated.connect(self._focus_quick_search)

        self.open_workspace_shortcut = QShortcut(QKeySequence("Ctrl+K, Ctrl+O"), self)
        self.open_workspace_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.open_workspace_shortcut.activated.connect(self._choose_workspace)

        self.command_palette_shortcut = QShortcut(QKeySequence("Ctrl+Shift+P"), self)
        self.command_palette_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.command_palette_shortcut.activated.connect(self._focus_command_palette)

        self.find_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self.find_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.find_shortcut.activated.connect(lambda: self._show_find_bar(False))

        self.workspace_search_shortcut = QShortcut(QKeySequence("Ctrl+Shift+F"), self)
        self.workspace_search_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.workspace_search_shortcut.activated.connect(self._show_workspace_search)

        self.outline_shortcut = QShortcut(QKeySequence("Ctrl+Shift+O"), self)
        self.outline_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.outline_shortcut.activated.connect(self._show_outline)

        self.definition_shortcut = QShortcut(QKeySequence("F12"), self)
        self.definition_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.definition_shortcut.activated.connect(self._go_to_definition)

        self.debug_shortcut = QShortcut(QKeySequence("F5"), self)
        self.debug_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.debug_shortcut.activated.connect(self._debug_current_file)

        self.references_shortcut = QShortcut(QKeySequence("Shift+F12"), self)
        self.references_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.references_shortcut.activated.connect(self._find_symbol_references)

        self.back_navigation_shortcut = QShortcut(QKeySequence("Alt+Left"), self)
        self.back_navigation_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.back_navigation_shortcut.activated.connect(self._navigate_back)

        self.forward_navigation_shortcut = QShortcut(QKeySequence("Alt+Right"), self)
        self.forward_navigation_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.forward_navigation_shortcut.activated.connect(self._navigate_forward)

        self.format_shortcut = QShortcut(QKeySequence("Shift+Alt+F"), self)
        self.format_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.format_shortcut.activated.connect(self._format_current_document)

        self.replace_shortcut = QShortcut(QKeySequence("Ctrl+H"), self)
        self.replace_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.replace_shortcut.activated.connect(lambda: self._show_find_bar(True))

        self.close_find_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.close_find_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.close_find_shortcut.activated.connect(
            lambda: self._close_settings() if self.workspace_stack.currentWidget() is self.settings_page
            else self._close_find_bar() if self.find_bar.isVisible() else None
        )

        self.settings_shortcut = QShortcut(QKeySequence("Ctrl+,"), self)
        self.settings_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.settings_shortcut.activated.connect(self.show_settings)

        self.terminal_shortcut = QShortcut(QKeySequence("Ctrl+`"), self)
        self.terminal_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.terminal_shortcut.activated.connect(lambda: self._toggle_bottom_panel(1))


    def _build_title_bar(self) -> QFrame:
        title_bar = WindowTitleBar(self)
        self.title_bar = title_bar
        title_bar.setFixedHeight(42)
        layout = QHBoxLayout(title_bar)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(9)

        logo = QLabel("S")
        logo.setObjectName("Logo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(25, 25)
        layout.addWidget(logo)
        title = ElidedLabel("SciDevHarness")
        self.title_brand = title
        title.setObjectName("WindowTitle")
        title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        title.setMinimumWidth(64)
        title.setMaximumWidth(160)
        layout.addWidget(title)
        layout.addWidget(self._vertical_rule())
        project = ElidedLabel(self.project_root.name)
        project.setObjectName("Subtle")
        project.setToolTip(str(self.project_root))
        project.setMinimumWidth(96)
        project.setMaximumWidth(380)
        project.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.title_project_label = project
        layout.addWidget(project, 1)

        search = QLineEdit()
        self.command_search = search
        search.setObjectName("CommandSearch")
        search.setPlaceholderText("搜索文件、命令或跳转到…")
        search.setMinimumWidth(190)
        search.setMaximumWidth(820)
        search.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(search, 3)
        layout.addStretch(1)

        new_button = QPushButton("新会话")
        new_button.setObjectName("Primary")
        self.title_new_button = new_button
        new_button.clicked.connect(self.new_coding_task)
        layout.addWidget(new_button)
        git_button = QPushButton("版本树")
        git_button.setObjectName("GitPrimary")
        self.title_git_button = git_button
        git_button.clicked.connect(self.show_git)
        layout.addWidget(git_button)
        for text, command in (("刷新", self.refresh_all),):
            button = QPushButton(text)
            button.setObjectName("Quiet")
            button.setAccessibleName("刷新工作区")
            self.title_refresh_button = button
            button.clicked.connect(command)
            layout.addWidget(button)

        self.ui_theme_button = QToolButton()
        self.ui_theme_button.setObjectName("ThemeSelector")
        self.ui_theme_button.setText("主题")
        self.ui_theme_button.setAccessibleName("界面方案")
        self.ui_theme_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.ui_theme_button.setFixedHeight(28)
        self.ui_theme_menu = QMenu(self.ui_theme_button)
        self.ui_theme_actions = {}
        for profile_key, profile in UI_PROFILES.items():
            action = self.ui_theme_menu.addAction(profile.label)
            action.setCheckable(True)
            action.setChecked(profile_key == self.ui_profile)
            action.setToolTip(profile.description)
            action.triggered.connect(
                lambda _checked=False, selected=profile_key: self.set_ui_profile(selected)
            )
            self.ui_theme_actions[profile_key] = action
        self.ui_theme_button.setMenu(self.ui_theme_menu)
        layout.addWidget(self.ui_theme_button)

        self.connection_label = ElidedLabel(self._provider_state())
        self.connection_label.setObjectName("StateReady" if self._provider_configured() else "StateWorking")
        self.connection_label.setMaximumWidth(220)
        self.connection_label.setToolTip("已配置模型（尚未验证连接）" if self._provider_configured() else "尚未配置 LLM；打开 Agent 设置查看配置方式")
        self.connection_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.connection_label.installEventFilter(self)
        layout.addSpacing(4)
        layout.addWidget(self.connection_label)

        for symbol, tip, object_name, command in (
            ("—", "最小化", "WindowButton", lambda: self.showMinimized()),
            ("□", "最大化 / 还原", "WindowButton", self._toggle_maximize),
            ("×", "关闭", "WindowClose", lambda: self.close()),
        ):
            button = QToolButton()
            button.setObjectName(object_name)
            button.setText(symbol)
            button.setToolTip(tip)
            button.setFixedSize(34, 28)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(command)
            layout.addWidget(button)
            if symbol == "□":
                self.window_max_button = button
        return title_bar


    @staticmethod
    def _vertical_rule() -> QFrame:
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.VLine)
        rule.setFrameShadow(QFrame.Shadow.Plain)
        rule.setStyleSheet("color: #3a3a3a;")
        rule.setFixedHeight(18)
        return rule


    def _dialog_title_bar(self, dialog: QDialog, title: str) -> WindowTitleBar:
        bar = WindowTitleBar(dialog, dialog)
        bar.setFixedHeight(42)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 0, 8, 0)
        label = QLabel(title)
        label.setObjectName("WindowTitle")
        layout.addWidget(label)
        layout.addStretch(1)
        close = QToolButton()
        close.setObjectName("WindowClose")
        close.setText("×")
        close.setToolTip("关闭")
        close.setFixedSize(34, 28)
        close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close.clicked.connect(dialog.close)
        layout.addWidget(close)
        return bar


    def _show_message(self, title: str, message: str) -> None:
        if self._active_notification is None:
            notification = NotificationToast(self.centralWidget())
            notification.dismissed.connect(self._clear_active_notification)
            self._active_notification = notification
        else:
            notification = self._active_notification
        notification.show_message(title, message)
        self._position_notification()
        notification.show()
        notification.raise_()


    def _build_left_panel(self) -> QWidget:
        shell = QWidget()
        shell.setMinimumWidth(self._activity_rail_width())
        shell.setMaximumWidth(480)
        shell.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QHBoxLayout(shell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        rail = QFrame()
        self.activity_rail = rail
        rail.setObjectName("ActivityRail")
        rail.setFixedWidth(self._activity_rail_width())
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 10, 0, 10)
        rail_layout.setSpacing(8)
        self.activity_buttons: list[QToolButton] = []
        for index, (icon_name, tip, command) in enumerate((
            ("explorer", "资源管理器", self.focus_explorer),
            ("search", "搜索项目", self._show_workspace_search),
            ("git", "开发版本树", self.show_git),
        )):
            button = QToolButton()
            button.setObjectName("ActivityButton")
            button.setIcon(
                activity_icon(icon_name, *UI_PROFILES[self.ui_profile].activity_icon_colors)
            )
            button.setIconSize(QSize(ACTIVITY_ICON_SIZE, ACTIVITY_ICON_SIZE))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setText("")
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setChecked(index == 0)
            button.clicked.connect(command)
            rail_layout.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
            self.activity_buttons.append(button)
        rail_layout.addStretch(1)
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("ActivityButton")
        self.settings_button.setIcon(activity_icon("settings", *UI_PROFILES[self.ui_profile].activity_icon_colors))
        self.settings_button.setCheckable(True)
        self.settings_button.setToolTip("设置  (Ctrl+,)")
        self.settings_button.setAccessibleName("打开设置")
        self.settings_button.clicked.connect(self.show_settings)
        rail_layout.addWidget(self.settings_button, 0, Qt.AlignmentFlag.AlignHCenter)
        logo_bottom = QLabel("S")
        logo_bottom.setObjectName("Logo")
        logo_bottom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_bottom.setFixedSize(30, 30)
        rail_layout.addWidget(logo_bottom, 0, Qt.AlignmentFlag.AlignHCenter)
        self._update_activity_rail_metrics()

        explorer = QFrame()
        explorer.setObjectName("ExplorerPane")
        explorer_layout = QVBoxLayout(explorer)
        explorer_layout.setContentsMargins(12, 12, 8, 8)
        explorer_layout.setSpacing(7)
        heading = QHBoxLayout()
        overline = QLabel("资源管理器")
        overline.setObjectName("Overline")
        heading.addWidget(overline)
        heading.addStretch(1)
        for symbol, tip in (("+", "新建文件"), ("…", "刷新资源管理器")):
            action = QToolButton()
            action.setObjectName("IconButton")
            action.setText(symbol)
            action.setToolTip(tip)
            action.clicked.connect(self.start_new_file_entry if symbol == "+" else self.refresh_file_tree)
            heading.addWidget(action)
            if not hasattr(self, "explorer_header_actions"):
                self.explorer_header_actions = []
            self.explorer_header_actions.append(action)
        self.explorer_heading_label = overline
        self.explorer_header_layout = heading
        explorer_layout.addLayout(heading)

        self.new_file_entry = QLineEdit()
        self.new_file_entry.setPlaceholderText("输入相对路径，例如 src/new_module.py，回车创建")
        self.new_file_entry.setVisible(False)
        self.new_file_entry.returnPressed.connect(self.create_new_file)
        explorer_layout.addWidget(self.new_file_entry)

        project_row = QHBoxLayout()
        project = ElidedLabel(f"⌄  {self.project_root.name}")
        project.setToolTip(str(self.project_root))
        project.setObjectName("AppTitle")
        self.project_label = project
        project_row.addWidget(project, 1)
        open_workspace = QToolButton()
        open_workspace.setObjectName("IconButton")
        open_workspace.setText("⋯")
        open_workspace.setToolTip("打开文件夹…  (Ctrl+K, Ctrl+O)")
        open_workspace.setAccessibleName("打开文件夹")
        open_workspace.clicked.connect(self._choose_workspace)
        self.open_workspace_button = open_workspace
        project_row.addWidget(open_workspace)
        explorer_layout.addLayout(project_row)

        # Git stays prominent in the title bar and activity rail; no duplicate
        # promotional card taking up space above the project files.
        git_entry_button = self.title_git_button
        git_entry_button.setToolTip("查看主线与成功、失败、取消中的尝试方向；这是 Harness 开发树，不是 Git 分支")
        git_entry_button.setAccessibleName("开发版本树：查看主线与尝试方向")
        self.git_entry_button = git_entry_button

        self.file_model = QFileSystemModel(self)
        self.file_model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        source_root = self.file_model.setRootPath(str(self.project_root))
        self.file_proxy = ProjectFilterProxy(self)
        self.file_proxy.setSourceModel(self.file_model)
        self.file_model.rowsInserted.connect(self.file_proxy.source_paths_changed)
        self.file_model.rowsRemoved.connect(self.file_proxy.source_paths_changed)
        self.file_model.fileRenamed.connect(self.file_proxy.source_paths_changed)
        self.file_tree = QTreeView()
        self.file_tree.setObjectName("FileTree")
        self.file_tree.setModel(self.file_proxy)
        self.file_tree.setRootIndex(self.file_proxy.mapFromSource(source_root))
        self.file_tree.setHeaderHidden(True)
        self.file_tree.setAnimated(True)
        self.file_tree.setIndentation(14)
        self.file_tree.setUniformRowHeights(True)
        self.file_tree.setSelectionMode(QTreeView.SelectionMode.SingleSelection)
        for column in (1, 2, 3):
            self.file_tree.hideColumn(column)
        self.file_tree.clicked.connect(self._on_file_preview)
        self.file_tree.doubleClicked.connect(self._on_file_selected)
        self.file_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.file_tree.customContextMenuRequested.connect(self._show_explorer_menu)
        self.explorer_rename_shortcut = QShortcut(QKeySequence("F2"), self.file_tree)
        self.explorer_rename_shortcut.activated.connect(self.start_rename_entry)
        self.explorer_delete_shortcut = QShortcut(QKeySequence("Delete"), self.file_tree)
        self.explorer_delete_shortcut.activated.connect(lambda: self.delete_explorer_path(self._explorer_path()))
        self.file_tree.viewport().installEventFilter(self)
        explorer_layout.addWidget(self.file_tree, 1)

        hint = QLabel("本地 · .research")
        hint.setObjectName("Hint")
        hint.setToolTip("本地工作区 · .research 已启用")
        self.workspace_hint = hint
        explorer_layout.addWidget(hint)
        layout.addWidget(rail)
        layout.addWidget(explorer, 1)
        self.left_panel = shell
        self.explorer = explorer
        return shell


    def _build_workspace(self) -> QWidget:
        workspace = QFrame()
        workspace.setObjectName("WorkspacePane")
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        workspace_toolbar = QFrame()
        workspace_toolbar.setObjectName("WorkspaceToolbar")
        workspace_toolbar.setFixedHeight(40)
        self.workspace_toolbar = workspace_toolbar
        toolbar_layout = QHBoxLayout(workspace_toolbar)
        toolbar_layout.setContentsMargins(14, 0, 12, 0)
        self.breadcrumb = QLabel("项目  /  欢迎页")
        self.breadcrumb.setObjectName("Subtle")
        toolbar_layout.addWidget(self.breadcrumb)
        toolbar_layout.addStretch(1)
        self.workspace_state = QLabel("● 就绪")
        self.workspace_state.setObjectName("StateReady")
        toolbar_layout.addWidget(self.workspace_state)
        # A single contextual toolbar replaces two redundant header rows.
        self.workspace_state.setVisible(False)
        editor_layout = toolbar_layout
        save = QPushButton("保存")
        save.setObjectName("Quiet")
        save.setToolTip("保存当前文件（Ctrl+S）")
        save.clicked.connect(self.save_current_file)
        editor_layout.addWidget(save)
        find = QPushButton("查找")
        find.setObjectName("Quiet")
        find.clicked.connect(lambda: self._show_find_bar(False))
        editor_layout.addWidget(find)
        terminal = QPushButton("终端")
        terminal.setObjectName("Quiet")
        terminal.clicked.connect(lambda: self._toggle_bottom_panel(1))
        editor_layout.addWidget(terminal)
        more = QToolButton()
        more.setObjectName("Quiet")
        more.setText("更多")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        editor_menu = QMenu(more)
        editor_menu.addAction("搜索整个项目", self._show_workspace_search)
        editor_menu.addAction("文件大纲", self._show_outline)
        editor_menu.addAction("问题", lambda: self._toggle_bottom_panel(2))
        editor_menu.addAction("查看当前文件差异", self._show_current_diff)
        editor_menu.addAction("调试当前 Python 文件 (F5)", self._debug_current_file)
        debug_menu = editor_menu.addMenu("Pdb 控制")
        for label, command in (
            ("继续", "c"),
            ("单步跳过", "n"),
            ("单步进入", "s"),
            ("跳出函数", "r"),
            ("退出调试", "q"),
        ):
            debug_menu.addAction(
                label,
                lambda checked=False, value=command: self._send_debug_command(value),
            )
        editor_menu.addSeparator()
        editor_menu.addAction("在右侧分栏打开", self._toggle_editor_split)
        more.setMenu(editor_menu)
        editor_layout.addWidget(more)
        layout.addWidget(workspace_toolbar)

        self.find_bar = self._build_find_bar()
        layout.addWidget(self.find_bar)

        self.editor_tabs = self._create_editor_group()
        self._active_editor_group = self.editor_tabs
        self.secondary_editor_tabs: QTabWidget | None = None
        self.welcome_editor = CodeEditor()
        self.welcome_editor.setProperty("welcomeMode", True)
        self.welcome_editor.setReadOnly(True)
        self.welcome_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.welcome_editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._configure_editor(self.welcome_editor)
        self.welcome_editor.line_number_area.hide()
        self.welcome_editor.setViewportMargins(0, 0, 0, 0)
        self._build_welcome_content()
        self.code_editor = self.welcome_editor
        welcome_index = self.editor_tabs.addTab(self.welcome_editor, "欢迎页")
        self.editor_tabs.tabBar().setTabButton(welcome_index, QTabBar.ButtonPosition.RightSide, None)
        self.editor_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.editor_splitter.setObjectName("EditorSplitter")
        self.editor_splitter.setChildrenCollapsible(False)
        self.editor_splitter.setHandleWidth(1)
        self.editor_splitter.addWidget(self.editor_tabs)
        layout.addWidget(self.editor_splitter, 1)
        self.bottom_tabs = self._build_bottom_panel()
        layout.addWidget(self.bottom_tabs)
        self._show_welcome()
        return workspace


    def _build_welcome_content(self) -> None:
        # This empty read-only editor keeps the tab/document lifecycle uniform,
        # but its viewport contains real widgets, not fake line-numbered code.
        editor = self.welcome_editor
        viewport_layout = QVBoxLayout(editor.viewport())
        viewport_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("WelcomeScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        surface = QWidget()
        surface.setObjectName("WelcomeSurface")
        outer = QVBoxLayout(surface)
        self.welcome_outer_layout = outer
        self._welcome_compact = None
        outer.setContentsMargins(24, 18, 24, 18)
        outer.addStretch(1)
        card = QFrame()
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        card.setObjectName("WelcomeCard")
        card.setFont(self._base_ui_font)
        card.setMaximumWidth(530)
        body = QVBoxLayout(card)
        body.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        body.setContentsMargins(20, 18, 20, 18)
        body.setSpacing(14)
        brand = QLabel("SciDevHarness")
        brand.setObjectName("WelcomeTitle")
        self.welcome_brand_label = brand
        body.addWidget(brand)
        project = ElidedLabel(self.project_root.name)
        project.setObjectName("SectionTitle")
        project.setToolTip(str(self.project_root))
        body.addWidget(project)
        description = QLabel("打开文件，专注编码。\n右侧 Agent 能读取项目、修改代码并保留开发过程。")
        description.setObjectName("Subtle")
        description.setWordWrap(True)
        self.welcome_description = description
        body.addWidget(description)
        open_file = QPushButton("打开文件    Ctrl + P")
        open_file.setObjectName("Primary")
        open_file.clicked.connect(lambda: self.command_search.setFocus())
        body.addWidget(open_file)
        sample = self.project_root / "index.html"
        self.welcome_sample_button = None
        if sample.is_file():
            open_sample = QPushButton("打开 index.html")
            open_sample.setObjectName("Quiet")
            open_sample.clicked.connect(lambda: self._open_file(sample))
            body.addWidget(open_sample)
            self.welcome_sample_button = open_sample
        shortcuts_layout = QVBoxLayout()
        shortcuts_layout.setContentsMargins(0, 0, 0, 0)
        shortcuts_layout.setSpacing(2)
        shortcut_labels = []
        for text in ("保存  Ctrl + S", "项目搜索  Ctrl + Shift + F", "发送指令  Ctrl + Enter"):
            shortcut = QLabel(text)
            shortcut.setObjectName("WelcomeShortcut")
            shortcut.setWordWrap(False)
            shortcut.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            shortcuts_layout.addWidget(shortcut)
            shortcut_labels.append(shortcut)
        body.addLayout(shortcuts_layout)
        outer.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)
        scroll.setWidget(surface)
        viewport_layout.addWidget(scroll)
        self.welcome_scroll = scroll
        self._welcome_text_labels = (description, *shortcut_labels)
        self.welcome_shortcut_labels = tuple(shortcut_labels)
        self.welcome_content = card
        card.installEventFilter(self)
        QTimer.singleShot(0, self._update_welcome_text_sizes)


    def _build_bottom_panel(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setObjectName("BottomTabs")
        tabs.setDocumentMode(True)
        tabs.setMaximumHeight(250)
        tabs.setVisible(False)

        search_page = QWidget()
        search_layout = QVBoxLayout(search_page)
        search_layout.setContentsMargins(10, 8, 10, 8)
        search_layout.setSpacing(6)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        self.workspace_search_input = QLineEdit()
        self.workspace_search_input.setPlaceholderText("搜索整个项目中的文本")
        self.workspace_search_input.returnPressed.connect(self._search_workspace)
        search_row.addWidget(self.workspace_search_input, 1)
        search_button = QPushButton("搜索")
        search_button.setObjectName("Primary")
        search_button.clicked.connect(self._search_workspace)
        search_row.addWidget(search_button)
        search_layout.addLayout(search_row)
        self.workspace_search_results = QTreeWidget()
        self.workspace_search_results.setHeaderHidden(True)
        self.workspace_search_results.setRootIsDecorated(False)
        self.workspace_search_results.itemDoubleClicked.connect(self._open_search_result)
        search_layout.addWidget(self.workspace_search_results, 1)
        tabs.addTab(search_page, "搜索")

        terminal_page = QWidget()
        terminal_layout = QVBoxLayout(terminal_page)
        terminal_layout.setContentsMargins(0, 0, 0, 0)
        terminal_layout.setSpacing(0)
        self.terminal_output = QPlainTextEdit()
        self.terminal_output.setObjectName("TerminalOutput")
        self.terminal_output.setReadOnly(True)
        self.terminal_output.setPlaceholderText("在项目目录中运行命令；ConPTY 命令运行中可直接输入并回车")
        terminal_layout.addWidget(self.terminal_output, 1)
        terminal_row = QHBoxLayout()
        terminal_row.setContentsMargins(0, 0, 0, 0)
        terminal_row.setSpacing(0)
        self.terminal_input = QLineEdit()
        self.terminal_input.setObjectName("TerminalInput")
        self.terminal_input.setPlaceholderText("输入命令并按 Enter 运行")
        self.terminal_input.returnPressed.connect(self._run_terminal_command)
        terminal_row.addWidget(self.terminal_input, 1)
        run = QPushButton("运行")
        run.setObjectName("Quiet")
        run.clicked.connect(self._run_terminal_command)
        terminal_row.addWidget(run)
        terminal_layout.addLayout(terminal_row)
        tabs.addTab(terminal_page, "终端")

        problems_page = QWidget()
        problems_layout = QVBoxLayout(problems_page)
        problems_layout.setContentsMargins(0, 0, 0, 0)
        self.problems_tree = QTreeWidget()
        self.problems_tree.setHeaderHidden(True)
        self.problems_tree.setRootIsDecorated(False)
        self.problems_tree.setMaximumHeight(104)
        self.problems_tree.setToolTip("双击问题跳转到文件和行列")
        self.problems_tree.itemDoubleClicked.connect(self._open_problem)
        problems_layout.addWidget(self.problems_tree)
        self.problems_output = QPlainTextEdit()
        self.problems_output.setObjectName("ProblemsOutput")
        self.problems_output.setReadOnly(True)
        self.problems_output.setPlaceholderText("任务、终端和 Git 检查产生的问题会显示在这里")
        problems_layout.addWidget(self.problems_output)
        tabs.addTab(problems_page, "问题")

        outline_page = QWidget()
        outline_layout = QVBoxLayout(outline_page)
        outline_layout.setContentsMargins(10, 8, 10, 8)
        outline_hint = QLabel("当前文件中的类、函数和主要符号")
        outline_hint.setObjectName("Hint")
        outline_layout.addWidget(outline_hint)
        self.outline_tree = QTreeWidget()
        self.outline_tree.setHeaderHidden(True)
        self.outline_tree.setRootIsDecorated(False)
        self.outline_tree.itemDoubleClicked.connect(self._open_outline_result)
        outline_layout.addWidget(self.outline_tree, 1)
        tabs.addTab(outline_page, "大纲")

        context_page = QWidget()
        context_layout = QVBoxLayout(context_page)
        context_layout.setContentsMargins(10, 8, 10, 8)
        context_layout.setSpacing(6)
        context_header = QHBoxLayout()
        self.project_context_meta = QLabel("发送任务时自动检索相关代码与项目指令")
        self.project_context_meta.setObjectName("Hint")
        self.project_context_meta.setWordWrap(True)
        context_header.addWidget(self.project_context_meta, 1)
        self.project_context_refresh = QPushButton("刷新")
        self.project_context_refresh.setObjectName("Quiet")
        self.project_context_refresh.setToolTip("按输入框中的任务或当前文件重新检索；不调用 LLM")
        self.project_context_refresh.clicked.connect(self.refresh_project_context)
        context_header.addWidget(self.project_context_refresh)
        self.project_environment_check = QPushButton("核验环境")
        self.project_environment_check.setObjectName("Quiet")
        self.project_environment_check.setToolTip(
            "按项目清单核验 Python 虚拟环境及 Node/Rust/Go 工具链；需一次批准，不联网、不安装依赖"
        )
        self.project_environment_check.clicked.connect(self.check_project_environment)
        context_header.addWidget(self.project_environment_check)
        context_layout.addLayout(context_header)
        self.project_context_output = QPlainTextEdit()
        self.project_context_output.setObjectName("ProjectContextOutput")
        self.project_context_output.setReadOnly(True)
        self.project_context_output.setPlaceholderText("这里显示提供给 Agent 的项目概况、指令、相关文件与片段")
        context_layout.addWidget(self.project_context_output, 1)
        self.project_context_tab_index = tabs.addTab(context_page, "上下文")
        return tabs
