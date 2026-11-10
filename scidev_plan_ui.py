"""Task-plan panel for the desktop workbench."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from scidev_planning import TaskPlan, planning_mode
from scidev_widgets import ElidedLabel


class TaskPlanPanel(QFrame):
    """Compact, persistent-in-session view of the active task's steps."""

    MODE_LABELS = {"auto": "自动", "required": "必需", "off": "关闭"}
    MODE_DESCRIPTIONS = {
        "auto": "自动规划：多步骤任务显示计划，简单任务直接执行",
        "required": "必须先规划：Agent 创建计划后才能调用其他工具",
        "off": "关闭规划：不提供计划工具，也不显示步骤计划",
    }
    STATUS_LABELS = {
        "pending": "待处理",
        "in_progress": "进行中",
        "completed": "已完成",
        "blocked": "受阻",
        "skipped": "已跳过",
    }

    def __init__(self, mode: str, settings: QSettings, mode_changed, parent: QWidget | None = None):  # noqa: ANN001
        super().__init__(parent)
        self.setObjectName("TaskPlan")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.settings = settings
        self.mode_changed = mode_changed
        self.mode = planning_mode(mode)
        self.plan: TaskPlan | None = None
        self.run_state = "idle"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 7)
        outer.setSpacing(5)
        header = QHBoxLayout()
        header.setSpacing(7)
        self.title_label = QLabel("任务计划")
        self.title_label.setObjectName("TaskPlanTitle")
        header.addWidget(self.title_label)
        self.progress_label = QLabel()
        self.progress_label.setObjectName("TaskPlanProgress")
        header.addWidget(self.progress_label, 1)
        self.mode_button = QToolButton()
        self.mode_button.setObjectName("TaskPlanMode")
        self.mode_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.mode_menu = QMenu(self.mode_button)
        self.mode_actions: dict[str, QAction] = {}
        for value, label in (("auto", "自动规划"), ("required", "必须先规划"), ("off", "关闭规划")):
            action = self.mode_menu.addAction(label)
            if action is None:
                continue
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, selected=value: self.set_mode(selected, persist=True))
            self.mode_actions[value] = action
        self.mode_button.setMenu(self.mode_menu)
        header.addWidget(self.mode_button)
        self.collapse_button = QToolButton()
        self.collapse_button.setObjectName("TaskPlanCollapse")
        self.collapse_button.setFixedWidth(27)
        self.collapse_button.clicked.connect(self.toggle_collapsed)
        header.addWidget(self.collapse_button)
        outer.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("TaskPlanScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setMaximumHeight(240)
        self.body = QWidget()
        self.body.setObjectName("TaskPlanBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(4)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll)
        self.collapsed = bool(settings.value("taskPlanCollapsed", False))
        self._sync_controls()
        self._render_plan()

    def set_mode(self, mode: str, *, persist: bool = False) -> None:
        self.mode = planning_mode(mode)
        if persist:
            self.settings.setValue("planningMode", self.mode)
            self.settings.sync()
            self.mode_changed(self.mode)
        self._sync_controls()
        self._render_empty()

    def set_plan(self, record: Any, *, run_state: str | None = None) -> None:
        self.plan = TaskPlan.from_record(record)
        if run_state is not None:
            self.run_state = run_state
        self._sync_controls()
        self._render_plan()

    def set_run_state(self, state: str) -> None:
        self.run_state = state
        self._render_plan()

    def toggle_collapsed(self) -> None:
        self.collapsed = not self.collapsed
        self.settings.setValue("taskPlanCollapsed", self.collapsed)
        self.settings.sync()
        self._sync_controls()

    def _sync_controls(self) -> None:
        self.mode_button.setText(self.MODE_LABELS[self.mode])
        description = f"{self.MODE_DESCRIPTIONS[self.mode]}；仅影响之后提交的新任务，当前任务配置不会改变"
        self.mode_button.setToolTip(description)
        self.mode_button.setAccessibleName(description)
        for key, action in self.mode_actions.items():
            action.setChecked(key == self.mode)
        self.scroll.setVisible(not self.collapsed)
        self.collapse_button.setText("›" if self.collapsed else "⌄")
        self.collapse_button.setToolTip("展开任务计划" if self.collapsed else "收起任务计划")
        self.collapse_button.setAccessibleName(self.collapse_button.toolTip())

    def _render_plan(self) -> None:
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if self.plan is None:
            self._render_empty()
            return
        completed, total = self.plan.progress
        prefix = {
            "running": "执行中",
            "waiting": "等待重试",
            "failed": "未完成",
            "incomplete": "需继续",
            "completed": "全部完成",
            "idle": "已保存",
        }.get(self.run_state, "进行中")
        self._progress_text = f"{prefix} · {completed}/{total}"
        self.progress_label.setText(self._progress_text)
        for index, step in enumerate(self.plan.steps, 1):
            row = QFrame()
            row.setObjectName("TaskPlanStep")
            row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(7, 5, 7, 5)
            row_layout.setSpacing(7)
            number = QLabel(str(index))
            number.setObjectName("TaskPlanNumber")
            number.setAlignment(Qt.AlignmentFlag.AlignCenter)
            number.setFixedSize(21, 21)
            row_layout.addWidget(number, 0, Qt.AlignmentFlag.AlignTop)
            label = ElidedLabel(step.step)
            label.setObjectName("TaskPlanStepText")
            label.setToolTip(step.step)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row_layout.addWidget(label, 1)
            status = QLabel(self.STATUS_LABELS[step.status])
            status.setObjectName("TaskPlanStepStatus")
            status.setProperty("status", step.status)
            status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            row_layout.addWidget(status)
            self.body_layout.addWidget(row)
        if self.plan.explanation:
            explanation = QLabel(self.plan.explanation)
            explanation.setObjectName("TaskPlanEmpty")
            explanation.setWordWrap(True)
            self.body_layout.addWidget(explanation)
        self.body_layout.addStretch(1)
        # Reserve real space for nearby steps rather than trusting the scroll
        # area's tiny default hint. Longer plans remain independently scrollable.
        self.update_viewport_height()

    def update_viewport_height(self) -> None:
        count = len(self.plan.steps) if self.plan else 0
        desired = min(240, count * 44 + (36 if self.plan and self.plan.explanation else 0)) if count else 30
        if self.window().height() < 600:
            desired = min(desired, 84)
        self.scroll.setFixedHeight(round(max(30, desired)))
        if self.plan:
            completed, total = self.plan.progress
            compact_header = self.window().width() < 1000 or (self.isVisible() and self.width() < 300)
            self.title_label.setText("计划" if compact_header else "任务计划")
            self.progress_label.setToolTip(f"{self.run_state} · {completed}/{total}")
            if compact_header:
                self.progress_label.setText(f"{completed}/{total}")
            else:
                self.progress_label.setText(self._progress_text)
        QTimer.singleShot(0, self._reveal_current_step)

    def _reveal_current_step(self) -> None:
        if self.plan is None:
            return
        for index, step in enumerate(self.plan.steps):
            if step.status == "in_progress":
                item = self.body_layout.itemAt(index)
                if item and item.widget():
                    self.scroll.ensureWidgetVisible(item.widget(), 0, 0)
                    self.scroll.verticalScrollBar().setValue(item.widget().y())
                break

    def _render_empty(self) -> None:
        if self.plan is not None:
            return
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        empty = QLabel(
            "等待 Agent 创建计划" if self.mode == "required"
            else "计划已关闭" if self.mode == "off"
            else "多步骤任务计划会显示在这里"
        )
        empty.setObjectName("TaskPlanEmpty")
        empty.setWordWrap(False)
        empty.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.body_layout.addWidget(empty)
        state_label = {
            "running": "正在执行",
            "waiting": "等待网络重试",
            "failed": "失败 · 可重试",
            "incomplete": "计划未完成",
            "completed": "已结束",
        }.get(self.run_state, "")
        self.progress_label.setText(state_label)
        self.update_viewport_height()
