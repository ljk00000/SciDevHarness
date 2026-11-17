"""Git changes, development history and version-tree interactions.

The host retains state and lifecycle ownership; this module does not import its entry point.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from PySide6.QtCore import QRect
from PySide6.QtCore import Qt
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QDialog
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QGridLayout
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QScrollArea
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QSplitter
from PySide6.QtWidgets import QToolButton
from PySide6.QtWidgets import QTreeWidget
from PySide6.QtWidgets import QTreeWidgetItem
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from scidev.runtime.helpers import new_id
from scidev.desktop.tree_ui import DevelopmentTreeView as DevelopmentTreeView
from scidev.desktop.editor import CodeEditor as CodeEditor


class GitWorkspaceMixin:
    """Operate on host state; introduce no initialization or additional state."""

    def _adapt_git_layout(self) -> None:
        if not hasattr(self, "git_tree_splitter"):
            return
        narrow = self.git_tree_splitter.width() < 900
        short_layout = self.height() < 560
        short_layout_changed = self._git_tree_short_layout != short_layout
        self._git_tree_short_layout = short_layout
        self.git_page_header.setFixedHeight(56 if short_layout else 68)
        self.git_metrics_panel.setVisible(not narrow and not short_layout)
        self.git_fit_button.setText("全图" if narrow else "适应全图")
        details_visible = self._git_details_expanded and (not short_layout or self._git_details_forced)
        self.git_details_scroll.setVisible(details_visible)
        # Collapsing details must release the whole dock, otherwise the hidden
        # scroll area still reserves its 260-360 px beside the canvas.
        self.git_details.setVisible(details_visible)
        self.git_details_button.setChecked(details_visible)
        self.git_chat_button.setChecked(self._git_chat_visible)
        if hasattr(self, "git_page_subtitle"):
            self.git_page_subtitle.setText(
                "主线与尝试方向"
                if narrow
                else "主线与尝试方向 · 非 Git 分支"
            )
        self.git_back_button.setText("返回" if narrow else "返回编码工作区")
        self.git_refresh_button.setText("刷新" if narrow else "刷新树")
        if self.git_init_button.text() in {"初始化", "初始化 Git"}:
            self.git_init_button.setText("初始化" if narrow else "初始化 Git")
        orientation = Qt.Orientation.Vertical if narrow else Qt.Orientation.Horizontal
        orientation_changed = self.git_tree_splitter.orientation() != orientation
        if orientation_changed:
            root = self.git_tree.rootObject()
            if narrow and root is not None:
                self._git_tree_compact_restore = (
                    float(root.property("zoom")),
                    float(root.property("panX")),
                    float(root.property("panY")),
                )
                self._git_tree_compact_auto_view = None
            elif not narrow and root is not None and self._git_tree_compact_restore is not None:
                zoom, pan_x, pan_y = self._git_tree_compact_restore
                root.setProperty("zoom", zoom)
                root.setProperty("panX", pan_x)
                root.setProperty("panY", pan_y)
                self._git_tree_compact_restore = None
                self._git_tree_compact_auto_view = None
            self.git_tree_splitter.setOrientation(orientation)
        # QSplitter swaps its orientation-specific default size policy when it
        # changes direction. Keep the responsive page filling its full width.
        self.git_tree_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        if narrow:
            self.git_tree_splitter.setStretchFactor(0, 1)
            self.git_tree_splitter.setStretchFactor(1, 1)
            self.git_details.setMinimumWidth(0)
            self.git_details.setMaximumWidth(16777215)
            self.git_details_scroll.setMinimumWidth(0)
            self.git_details_scroll.setMaximumWidth(16777215)
            self.git_tree_scroll.setMinimumHeight(0)
            self.git_details_scroll.setMinimumHeight(0)
            if orientation_changed or short_layout_changed:
                self.git_page.layout().activate()
                split_height = max(1, self.git_tree_splitter.height())
                # Keep a useful selected-node summary beside the compact canvas.
                details_ratio = 0.38
                self.git_tree_splitter.setSizes(
                    [int(split_height * (1 - details_ratio)), int(split_height * details_ratio)]
                )
        else:
            self.git_tree_splitter.setStretchFactor(0, 1)
            self.git_tree_splitter.setStretchFactor(1, 0)
            self.git_details.setMinimumWidth(260)
            self.git_details.setMaximumWidth(360)
            self.git_details_scroll.setMinimumWidth(260)
            self.git_details_scroll.setMaximumWidth(360)
            self.git_tree_scroll.setMinimumHeight(0)
            self.git_details_scroll.setMinimumHeight(0)
            if orientation_changed:
                self.git_tree_splitter.setSizes([1, 300])
        if narrow:
            QTimer.singleShot(0, self._fit_git_tree_compact_layout)


    def _fit_development_tree(self) -> None:
        root = self.git_tree.rootObject()
        cards, _dots = self.git_tree._positions()
        viewport = self.git_tree_scroll.viewport().size()
        if root is None or not cards or viewport.width() <= 0 or viewport.height() <= 0:
            return
        bounds = QRect()
        for _x, _y, rect in cards.values():
            bounds = rect if not bounds.isValid() else bounds.united(rect)
        ui_scale = max(0.1, float(root.property("uiScale")))
        available_width = max(1, viewport.width() - 36)
        available_height = max(1, viewport.height() - 78)
        zoom = min(
            1.8,
            max(0.65, min(
                available_width / max(1, bounds.width() * ui_scale),
                available_height / max(1, bounds.height() * ui_scale),
            )),
        )
        render_scale = zoom * ui_scale
        root.setProperty("zoom", zoom)
        root.setProperty("panX", (viewport.width() - bounds.width() * render_scale) / 2 - bounds.left() * render_scale)
        root.setProperty("panY", 48 + (available_height - bounds.height() * render_scale) / 2 - bounds.top() * render_scale)
        self._git_tree_compact_restore = None
        self._git_tree_compact_auto_view = None


    def _toggle_git_details(self) -> None:
        self._git_details_expanded = not self.git_details_scroll.isVisible()
        self._git_details_forced = True
        self._adapt_git_layout()


    def _toggle_git_chat(self) -> None:
        self._git_chat_visible = not self._git_chat_visible
        self._adapt_workbench_layout()
        self._adapt_git_layout()


    def _fit_git_tree_compact_layout(self) -> None:
        if (
            not hasattr(self, "git_tree_splitter")
            or self.git_tree_splitter.orientation() != Qt.Orientation.Vertical
            or self._git_tree_compact_restore is None
        ):
            return
        root = self.git_tree.rootObject()
        if root is None:
            return
        current = (
            float(root.property("zoom")),
            float(root.property("panX")),
            float(root.property("panY")),
        )
        previous_auto = self._git_tree_compact_auto_view
        if previous_auto is not None and any(abs(value - prior) > 0.02 for value, prior in zip(current, previous_auto)):
            # Respect a manual zoom or pan instead of repeatedly snapping it
            # back while the user resizes the compact layout.
            return

        attempts = [node for node in self.git_tree.nodes if node.get("lane") == "attempt"]
        if not attempts:
            return
        cards, _ = self.git_tree._positions()
        first_attempt = cards.get(str(attempts[0].get("id")))
        if first_attempt is None:
            return
        viewport_height = self.git_tree_scroll.viewport().height()
        if viewport_height <= 0:
            return
        base_zoom, pan_x, pan_y = self._git_tree_compact_restore
        available_height = max(1, viewport_height - 8 - pan_y)
        ui_scale = float(root.property("uiScale"))
        fit_zoom = min(
            base_zoom,
            available_height / max(1, first_attempt[2].bottom() * ui_scale),
        )
        fit_zoom = max(0.65, fit_zoom)
        root.setProperty("zoom", fit_zoom)
        root.setProperty("panX", pan_x)
        root.setProperty("panY", pan_y)
        self._git_tree_compact_auto_view = (fit_zoom, pan_x, pan_y)


    def _build_git_page(self) -> QWidget:
        page = QFrame()
        page.setObjectName("GitPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("GitPageHeader")
        header.setFixedHeight(68)
        self.git_page_header = header
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 0, 14, 0)
        icon = QLabel("G")
        icon.setObjectName("AgentLogo")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(32, 32)
        header_layout.addWidget(icon)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("开发版本树")
        title.setObjectName("AppTitle")
        subtitle = QLabel("主线与尝试方向 · 非 Git 分支")
        subtitle.setObjectName("Subtle")
        subtitle.setToolTip("记录主线与被取消的尝试方向 · 不等同于 Git 分支")
        self.git_page_subtitle = subtitle
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addLayout(title_box)
        header_layout.addStretch(1)
        back = QPushButton("返回编码工作区")
        back.setObjectName("Quiet")
        back.clicked.connect(self.show_workspace)
        self.git_back_button = back
        header_layout.addWidget(back)
        refresh = QPushButton("刷新树")
        refresh.setObjectName("Quiet")
        refresh.clicked.connect(self._refresh_development_tree)
        self.git_refresh_button = refresh
        header_layout.addWidget(refresh)
        fit_tree = QPushButton("适应全图")
        fit_tree.setObjectName("Quiet")
        fit_tree.setToolTip("缩放并居中显示全部主线与尝试分支")
        fit_tree.clicked.connect(self._fit_development_tree)
        self.git_fit_button = fit_tree
        header_layout.addWidget(fit_tree)
        details_toggle = QToolButton()
        details_toggle.setObjectName("Quiet")
        details_toggle.setText("详情")
        details_toggle.setCheckable(True)
        details_toggle.setToolTip("展开 / 收起节点详情；工作区提交区始终固定显示")
        details_toggle.clicked.connect(self._toggle_git_details)
        self.git_details_button = details_toggle
        header_layout.addWidget(details_toggle)
        chat_toggle = QToolButton()
        chat_toggle.setObjectName("Quiet")
        chat_toggle.setText("Agent")
        chat_toggle.setCheckable(True)
        chat_toggle.setToolTip("显示 / 隐藏对话面板，为版本树留出空间")
        chat_toggle.clicked.connect(self._toggle_git_chat)
        self.git_chat_button = chat_toggle
        header_layout.addWidget(chat_toggle)
        init = QPushButton("初始化 Git")
        init.setObjectName("GitPrimary")
        init.clicked.connect(self.init_git)
        self.git_init_button = init
        header_layout.addWidget(init)
        layout.addWidget(header)

        metrics_panel = QWidget()
        metrics_panel.setObjectName("GitMetricsPanel")
        metrics = QHBoxLayout(metrics_panel)
        metrics.setContentsMargins(18, 6, 18, 6)
        metrics.setSpacing(10)
        self.git_metrics_panel = metrics_panel
        self.git_main_value = self._metric_card(metrics, "主线", "0")
        self.git_attempt_value = self._metric_card(metrics, "尝试", "0")
        self.git_failed_value = self._metric_card(metrics, "失败", "0")
        self.git_head_value = self._metric_card(metrics, "HEAD", "暂无")
        layout.addWidget(metrics_panel)

        tree_splitter = QSplitter(Qt.Orientation.Horizontal)
        tree_splitter.setChildrenCollapsible(False)
        tree_splitter.setHandleWidth(1)
        tree_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.git_tree = DevelopmentTreeView(self.project_root / ".research" / "tree_layout.json", self.ui_profile)
        self.git_tree.node_selected.connect(self._handle_git_node_selected)
        tree_scroll = QScrollArea()
        tree_scroll.setObjectName("TreeScroll")
        tree_scroll.setWidgetResizable(True)
        tree_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tree_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        tree_scroll.setWidget(self.git_tree)
        self.git_tree_scroll = tree_scroll
        tree_splitter.addWidget(tree_scroll)

        details = QFrame()
        details.setObjectName("GitDetailContent")
        details.setMinimumWidth(0)
        details.setMaximumWidth(16777215)
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(18, 20, 18, 16)
        detail_layout.setSpacing(9)
        detail_label = QLabel("节点详情")
        detail_label.setObjectName("Overline")
        detail_layout.addWidget(detail_label)
        self.git_detail_title = QLabel("当前主线")
        self.git_detail_title.setObjectName("GitDetailTitle")
        self.git_detail_title.setWordWrap(True)
        detail_layout.addWidget(self.git_detail_title)
        self.git_detail_status = QLabel("当前主线")
        self.git_detail_status.setObjectName("GitDetailStatus")
        detail_layout.addWidget(self.git_detail_status)
        self.git_detail_description = QLabel("这里会显示选中开发节点的说明。")
        self.git_detail_description.setObjectName("Subtle")
        self.git_detail_description.setWordWrap(True)
        self.git_detail_description.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.git_detail_description)
        detail_layout.addSpacing(8)
        meta_label = QLabel("记录")
        meta_label.setObjectName("Overline")
        detail_layout.addWidget(meta_label)
        self.git_detail_meta = QLabel("暂无开发记录")
        self.git_detail_meta.setObjectName("Subtle")
        self.git_detail_meta.setWordWrap(True)
        self.git_detail_meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.git_detail_meta)
        self.git_retry_button = QPushButton("重新尝试这个方向")
        self.git_retry_button.setObjectName("Primary")
        self.git_retry_button.setEnabled(False)
        self.git_retry_button.clicked.connect(self.retry_selected_node)
        detail_layout.addWidget(self.git_retry_button)
        changes_label = QLabel("工作区变更")
        changes_label.setObjectName("Overline")
        detail_layout.addWidget(changes_label)
        self.git_changes_tree = QTreeWidget()
        self.git_changes_tree.setHeaderHidden(True)
        self.git_changes_tree.setRootIsDecorated(False)
        self.git_changes_tree.setMaximumHeight(132)
        self.git_changes_tree.setToolTip("双击文件查看 Git diff")
        self.git_changes_tree.itemDoubleClicked.connect(self._open_git_change)
        detail_layout.addWidget(self.git_changes_tree)
        change_actions = QGridLayout()
        change_actions.setContentsMargins(0, 0, 0, 0)
        change_actions.setSpacing(6)
        stage_selected = QPushButton("暂存选中")
        stage_selected.setObjectName("Quiet")
        stage_selected.clicked.connect(self.stage_selected_change)
        change_actions.addWidget(stage_selected, 0, 0)
        unstage_selected = QPushButton("取消暂存")
        unstage_selected.setObjectName("Quiet")
        unstage_selected.clicked.connect(self.unstage_selected_change)
        change_actions.addWidget(unstage_selected, 0, 1)
        stage_all = QPushButton("暂存全部")
        stage_all.setObjectName("Quiet")
        stage_all.clicked.connect(self.stage_all_changes)
        change_actions.addWidget(stage_all, 1, 0, 1, 2)
        detail_layout.addLayout(change_actions)
        workspace_label = QLabel("Git 状态")
        workspace_label.setObjectName("Overline")
        detail_layout.addWidget(workspace_label)
        self.git_status_output = QPlainTextEdit()
        self.git_status_output.setObjectName("ProblemsOutput")
        self.git_status_output.setReadOnly(True)
        self.git_status_output.setMaximumHeight(92)
        detail_layout.addWidget(self.git_status_output)
        self.git_commit_input = QLineEdit()
        self.git_commit_input.setPlaceholderText("提交说明")
        self.git_commit_input.returnPressed.connect(self.commit_workspace)
        detail_layout.addWidget(self.git_commit_input)
        commit = QPushButton("提交当前工作区")
        commit.setObjectName("GitPrimary")
        commit.clicked.connect(self.commit_workspace)
        detail_layout.addWidget(commit)
        detail_layout.addStretch(1)
        note = QLabel("树上的分支是编码过程中的尝试方向。取消或失败的方向会保留在这里，方便以后回看；只有 Agent 完成的修改才会进入主线。")
        note.setObjectName("Hint")
        note.setWordWrap(True)
        detail_layout.addWidget(note)
        details_scroll = QScrollArea()
        details_scroll.setObjectName("GitDetailsScroll")
        details_scroll.setWidgetResizable(True)
        details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        details_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        details_scroll.setWidget(details)
        detail_layout.removeWidget(self.git_commit_input)
        detail_layout.removeWidget(commit)
        commit_footer = QFrame()
        commit_footer.setObjectName("GitCommitFooter")
        commit_layout = QVBoxLayout(commit_footer)
        commit_layout.setContentsMargins(12, 10, 12, 12)
        commit_layout.setSpacing(7)
        self.git_commit_input.setParent(commit_footer)
        commit.setParent(commit_footer)
        commit_layout.addWidget(self.git_commit_input)
        commit_layout.addWidget(commit)
        details_dock = QFrame()
        details_dock.setObjectName("GitDetails")
        details_dock.setMinimumWidth(260)
        details_dock.setMaximumWidth(360)
        dock_layout = QVBoxLayout(details_dock)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        dock_layout.setSpacing(0)
        dock_layout.addWidget(details_scroll, 1)
        dock_layout.addWidget(commit_footer)
        self.git_details = details_dock
        self.git_details_scroll = details_scroll
        self.git_commit_footer = commit_footer
        tree_splitter.addWidget(details_dock)
        tree_splitter.setSizes([1, 300])
        tree_splitter.setStretchFactor(0, 1)
        tree_splitter.setStretchFactor(1, 0)
        self.git_tree_splitter = tree_splitter
        layout.addWidget(tree_splitter, 1)
        return page


    @staticmethod
    def _metric_card(parent_layout: QHBoxLayout, label_text: str, value_text: str) -> QLabel:
        card = QFrame()
        card.setObjectName("MetricCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 6, 12, 6)
        card_layout.setSpacing(2)
        value = QLabel(value_text)
        value.setObjectName("MetricValue")
        card_layout.addWidget(value)
        label = QLabel(label_text)
        label.setObjectName("Subtle")
        card_layout.addWidget(label)
        card.setToolTip(label_text)
        parent_layout.addWidget(card, 1)
        return value


    def _development_nodes(self) -> list[dict[str, Any]]:
        branch = self.git.branch()
        head = self.git.head_sha()
        nodes: list[dict[str, Any]] = [{
            "id": "root",
            "lane": "main",
            "status": "main",
            "title": "当前主线",
            "description": "项目当前持续发展的编码主线。成功完成的编码任务会接在这里。",
            "meta": f"Git {branch} · HEAD {head[:10] if head else '暂无'}",
        }]
        current_main = "root"
        for item in reversed(self.ledger.list_tasks(limit=100)):
            raw_payload = item.get("payload", {})
            if isinstance(raw_payload, str):
                try:
                    raw_payload = json.loads(raw_payload)
                except json.JSONDecodeError:
                    raw_payload = {}
            prompt = str(raw_payload.get("prompt", "")) if isinstance(raw_payload, dict) else ""
            title = prompt.splitlines()[0].strip() or str(item.get("kind", "编码任务"))
            if title.startswith("编码目标："):
                title = title.removeprefix("编码目标：").strip()
            task_id = str(item.get("task_id", new_id("node")))
            status = str(item.get("status", ""))
            meta = f"{item.get('updated_at') or item.get('created_at') or '未知时间'} · {item.get('attempts', 0)} 次尝试"
            if status == "succeeded":
                node = {
                    "id": task_id,
                    "lane": "main",
                    "status": "main",
                    "parent_id": current_main,
                    "title": title,
                    "description": "该编码任务已完成，修改结果进入当前主线。",
                    "meta": meta,
                }
                nodes.append(node)
                current_main = task_id
            else:
                attempt_status = "failed" if status == "failed" else "retry" if status == "retry_wait" else "active"
                if status == "queued":
                    description = "任务已排队，尚未成为主线的一部分。"
                elif status == "running":
                    description = "Agent 正在尝试这个方向。"
                else:
                    description = str(item.get("last_error") or "这个方向没有进入当前主线。")
                nodes.append({
                    "id": task_id,
                    "lane": "attempt",
                    "status": attempt_status,
                    "parent_id": current_main,
                    "title": title,
                    "description": description,
                    "meta": meta,
                })
        return nodes


    def _refresh_development_tree(self) -> None:
        if not hasattr(self, "git_tree"):
            return
        nodes = self._development_nodes()
        self.git_tree.set_nodes(nodes)
        main_count = sum(node.get("lane") == "main" for node in nodes) - 1
        attempts = [node for node in nodes if node.get("lane") == "attempt"]
        failed_count = sum(node.get("status") == "failed" for node in attempts)
        self.git_main_value.setText(str(max(0, main_count)))
        self.git_attempt_value.setText(str(len(attempts)))
        self.git_failed_value.setText(str(failed_count))
        head = getattr(self, "_cached_head_sha", "")
        if head is None:
            head = self.git.head_sha()
        self.git_head_value.setText(head[:8] if head else "暂无")


    def _handle_git_node_selected(self, node: dict[str, Any]) -> None:
        self.git_detail_title.setText(str(node.get("title", "未命名节点")))
        self.git_detail_status.setText(DevelopmentTreeView._status_text(node))
        color = DevelopmentTreeView._accent(node).name()
        if self.ui_profile == "paper":
            color = {"main": "#13836b", "failed": "#b7473c", "retry": "#91600c", "active": "#266dc0"}.get(
                "main" if node.get("lane") == "main" else str(node.get("status")), "#65758a"
            )
        self.git_detail_status.setStyleSheet(f"color: {color};")
        self.git_detail_description.setText(str(node.get("description", "暂无说明")))
        self.git_detail_meta.setText(str(node.get("meta", "暂无记录")))
        can_retry = node.get("lane") == "attempt" and node.get("status") in {"failed", "retry"}
        self.git_retry_button.setEnabled(can_retry)
        self.git_retry_button.setProperty("task_id", node.get("id", ""))


    def retry_selected_node(self) -> None:
        if self._environment_check_running:
            self._append_chat("系统", "项目环境核验进行中，请完成后再重试开发方向。", "meta")
            return
        if self.active_task_id:
            self._append_chat("系统", "当前任务仍在执行，请等待 Agent 完成后再重试其他方向。", "meta")
            return
        task_id = str(self.git_retry_button.property("task_id") or "")
        if not task_id or not self.ledger.retry_now(task_id):
            return
        self.active_task_id = task_id
        self._sync_stop_button()
        self.worker.wake()
        self._set_state(self.chat_status, "排队中 · 正在重新尝试", "StateWorking")
        self._set_state(self.workspace_state, "● 排队中", "StateWorking")
        self._append_chat("系统", f"已重新尝试开发方向：{task_id}", "meta")
        self._refresh_development_tree()


    def refresh_git_status(self, *, background: bool = True) -> None:
        """Refresh the Git views without running Git on the UI thread.

        ``background=False`` performs one synchronous snapshot; it exists for
        callers that need the result immediately (and for deterministic tests).
        """
        if not background:
            with self._git_status_lock:
                self._git_status_seq += 1
                sequence = self._git_status_seq
            snapshot = self.git.snapshot()
            snapshot["_seq"] = sequence
            self._apply_git_snapshot(snapshot)
            return
        with self._git_status_lock:
            self._git_status_seq += 1
            self._git_status_dirty = True
            if self._git_status_busy:
                return
            self._git_status_busy = True
        threading.Thread(target=self._git_status_worker, name="scidev-git-status", daemon=True).start()


    def _git_status_worker(self) -> None:
        while True:
            with self._git_status_lock:
                if not self._git_status_dirty:
                    self._git_status_busy = False
                    return
                self._git_status_dirty = False
                sequence = self._git_status_seq
            try:
                snapshot = self.git.snapshot()
            except Exception as exc:  # noqa: BLE001 - surface as a Git message, never crash the worker
                snapshot = {"state": "error", "text": f"Git 状态读取失败：{exc}", "entries": []}
            if self._closing:
                # The window (and later the QApplication) may already be gone;
                # emitting into a destroyed QObject is a native crash.
                with self._git_status_lock:
                    self._git_status_busy = False
                    self._git_status_dirty = False
                return
            snapshot["_seq"] = sequence
            self.signals.git_status_ready.emit(snapshot)


    def wait_for_git_status(self, timeout: float = 5.0) -> bool:
        """Process events until the background Git refresh is applied to the UI."""
        deadline = time.monotonic() + timeout
        app = QApplication.instance()
        while time.monotonic() < deadline:
            if app is not None:
                app.processEvents()
            with self._git_status_lock:
                idle = not self._git_status_busy and not self._git_status_dirty
                applied = self._git_status_applied
                issued = self._git_status_seq
            if idle and applied >= issued:
                # Deliver the queued snapshot before the caller inspects the UI.
                if app is not None:
                    app.processEvents()
                return True
            time.sleep(0.01)
        return False


    def _apply_git_snapshot(self, snapshot: dict[str, Any]) -> None:
        sequence = int(snapshot.get("_seq") or 0)
        if sequence and sequence < self._git_status_applied:
            # A slower, older read must not overwrite a newer result.
            return
        if sequence:
            self._git_status_applied = sequence
        raw_status = str(snapshot.get("text") or "")
        entries = snapshot.get("entries") or []
        state = str(snapshot.get("state") or "")
        if "head" in snapshot:
            head = str(snapshot.get("head") or "")
            if head != getattr(self, "_cached_head_sha", None):
                self._cached_head_sha = head
                # The tree may already have rendered before this snapshot
                # arrived (for example right after a commit); refresh the label.
                if hasattr(self, "git_head_value"):
                    self.git_head_value.setText(head[:8] if head else "暂无")
            self._cached_head_sha = head
        if hasattr(self, "git_status_output"):
            self.git_status_output.setPlainText(raw_status)
        self._refresh_git_changes(entries)
        enclosing = snapshot.get("enclosing")
        if hasattr(self, "git_init_button"):
            git_ready = state in {"clean", "changes"}
            git_available = state != "unavailable"
            if enclosing:
                label = "工作区在仓库内"
            else:
                label = "Git 已初始化" if git_ready else ("需安装 Git" if not git_available else "初始化 Git")
            self.git_init_button.setText(label)
            self.git_init_button.setEnabled(not enclosing and not git_ready and git_available)
            self.git_init_button.setToolTip(
                f"外层仓库：{enclosing}；请在仓库根目录打开工作区"
                if enclosing
                else ""
            )
        if state in {"clean", "no_repo", "unavailable", "nested", "timeout", "error"}:
            status = raw_status
        else:
            status = f"{len(entries)} 个工作区变更"
        self.status_branch.setText(f"Git · {status}")
        self.status_branch.setToolTip(raw_status)


    def _refresh_git_changes(self, entries: list[dict[str, str]] | None = None) -> None:
        if not hasattr(self, "git_changes_tree"):
            return
        selected_path = None
        selected_item = self.git_changes_tree.currentItem()
        if selected_item is not None:
            selected_path = selected_item.data(0, Qt.ItemDataRole.UserRole)
        self.git_changes_tree.clear()
        item_to_restore = None
        for entry in (self.git.status_entries() if entries is None else entries):
            relative = str(entry.get("path", "")).strip('"')
            code = str(entry.get("code", "  "))
            if not relative:
                continue
            item = QTreeWidgetItem([f"{code}  {relative}"])
            item.setData(0, Qt.ItemDataRole.UserRole, relative)
            item.setToolTip(0, "双击查看 diff；可用下方按钮管理暂存区")
            self.git_changes_tree.addTopLevelItem(item)
            if selected_path == relative:
                item_to_restore = item
        if self.git_changes_tree.topLevelItemCount() == 0:
            empty = QTreeWidgetItem(["工作区干净"])
            empty.setFlags(empty.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.git_changes_tree.addTopLevelItem(empty)
        elif item_to_restore is not None:
            self.git_changes_tree.setCurrentItem(item_to_restore)


    def _selected_git_change_path(self) -> Path | None:
        item = self.git_changes_tree.currentItem() if hasattr(self, "git_changes_tree") else None
        raw_path = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if not raw_path:
            return None
        target = (self.project_root / str(raw_path)).resolve()
        return target if target.is_relative_to(self.project_root) else None


    def _open_git_change(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        raw_path = item.data(0, Qt.ItemDataRole.UserRole)
        if not raw_path:
            return
        target = (self.project_root / str(raw_path)).resolve()
        if target.exists() and target.is_file():
            self._open_file(target, preview=False)
        self._show_diff_for_path(target)


    def stage_selected_change(self) -> None:
        target = self._selected_git_change_path()
        if target is None:
            self._append_log("请先选择一个工作区变更")
            return
        try:
            self.git.stage_path(target)
            self.refresh_git_status()
            self._append_log(f"已暂存 {target.relative_to(self.project_root).as_posix()}")
        except (OSError, ValueError, RuntimeError) as exc:
            self.problems_output.setPlainText(str(exc))
            self._append_log(f"暂存失败: {exc}")


    def unstage_selected_change(self) -> None:
        target = self._selected_git_change_path()
        if target is None:
            self._append_log("请先选择一个工作区变更")
            return
        try:
            self.git.unstage_path(target)
            self.refresh_git_status()
            self._append_log(f"已取消暂存 {target.relative_to(self.project_root).as_posix()}")
        except (OSError, ValueError, RuntimeError) as exc:
            self.problems_output.setPlainText(str(exc))
            self._append_log(f"取消暂存失败: {exc}")


    def stage_all_changes(self) -> None:
        try:
            self.git.stage_all()
            self.refresh_git_status()
            self._append_log("已暂存全部工作区变更")
        except (OSError, RuntimeError) as exc:
            self.problems_output.setPlainText(str(exc))
            self._append_log(f"暂存失败: {exc}")


    def _show_diff_for_path(self, path: Path) -> None:
        path = path.resolve()
        if not path.is_relative_to(self.project_root):
            self._append_log("无法查看项目目录之外的 diff")
            return
        diff_text = self.git.diff(path)
        if not diff_text:
            self._append_log(f"{path.name} 没有未提交的差异")
            return
        relative = path.relative_to(self.project_root).as_posix()
        title = f"差异 · {relative}"
        diff_editor = next(
            (
                candidate
                for candidate in self._editor_titles
                if candidate.property("diff_path") == str(path)
            ),
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
            self._install_tab_close_button(group, index, diff_editor)
            group.setTabToolTip(index, f"Git diff · {relative}")
        diff_editor.setPlainText(diff_text)
        diff_editor.document().setModified(False)
        group = self._editor_group_for(diff_editor) or self._active_editor_group or self.editor_tabs
        self._active_editor_group = group
        group.setCurrentWidget(diff_editor)
        self._append_log(f"已打开 {path.name} 的 Git diff")


    def commit_workspace(self) -> None:
        message = self.git_commit_input.text().strip()
        if not message:
            self.git_commit_input.setFocus()
            self._append_log("请填写提交说明")
            return
        try:
            sha = self.git.commit_changes(message)
            self.git_commit_input.clear()
            self._append_log(f"已创建 Git 提交 {sha[:12] if sha else '无变更'}")
            self.refresh_git_status()
            self._refresh_development_tree()
        except Exception as exc:  # noqa: BLE001
            self.problems_output.setPlainText(str(exc))
            self._append_log(f"Git 提交失败: {exc}")


    def show_tasks(self) -> None:
        self.show_git()
        self._append_log("任务历史已收进开发版本树")


    def _clear_task_dialog(self, dialog: QDialog) -> None:
        if self.task_dialog is dialog:
            self.task_dialog = None
            self.task_history_tree = None


    def refresh_task_history(self) -> None:
        tree = self.task_history_tree
        if tree is None:
            return
        tree.clear()
        for item in self.ledger.list_tasks():
            row = QTreeWidgetItem(tree, [item["kind"], item["status"], str(item["attempts"]), item["last_error"]])
            row.setData(0, Qt.ItemDataRole.UserRole, item["task_id"])


    def retry_selected(self) -> None:
        if self._environment_check_running:
            self._append_log("项目环境核验进行中，暂时不能重试编码任务。")
            return
        tree = self.task_history_tree
        if tree is None or not tree.currentItem():
            return
        task_id = tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
        if task_id and self.ledger.retry_now(str(task_id)):
            self.worker.wake()
            self._append_log(f"已手动重试 {task_id}")
            self.refresh_task_history()


    def show_git(self) -> None:
        self._git_focus = True
        self._git_details_expanded = self.width() >= 1100
        self._git_details_forced = False
        self._set_activity_button(2)
        self.workspace_stack.setCurrentWidget(self.git_page)
        self._adapt_workbench_layout()
        self._adapt_git_layout()
        self._refresh_development_tree()
        self._append_log("已打开开发版本树")


    def init_git(self) -> None:
        try:
            self.git.init()
            self._append_log("Git 已准备好；编码任务完成后会自动提交。")
            self.refresh_git_status()
            self._refresh_development_tree()
        except Exception as exc:  # noqa: BLE001
            self._show_message("Git 错误", str(exc))
