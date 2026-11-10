"""Chat bubbles, execution groups and transient workspace notifications."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class MessageBubble(QFrame):
    def __init__(self, speaker: str, message: str, kind: str):
        super().__init__()
        self.setObjectName(
            {
                "user": "UserBubble",
                "agent": "AgentBubble",
                "tool": "ToolBubble",
                "meta": "MetaBubble",
                "summary": "SummaryBubble",
            }.get(kind, "AgentBubble")
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 9)
        layout.setSpacing(4)
        self.role_label = QLabel(speaker)
        self.role_label.setObjectName("BubbleRole")
        layout.addWidget(self.role_label)
        self.body_label = QLabel(message)
        self.body_label.setObjectName("ToolText" if kind == "tool" else "BubbleText")
        self.body_label.setTextFormat(Qt.TextFormat.PlainText)
        self.body_label.setWordWrap(True)
        self.body_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.body_label)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

    def set_speaker(self, speaker: str) -> None:
        self.role_label.setText(speaker)

    def set_message(self, message: str) -> None:
        self.body_label.setText(message)

    def append_message(self, text: str) -> None:
        self.body_label.setText(self.body_label.text() + text)


class ExecutionGroup(QFrame):
    """Collapsible, per-task history of model/tool activity in the conversation."""

    def __init__(self, session_id: str):
        super().__init__()
        self.session_id = session_id
        self.state = "running"
        self.activity = "正在准备请求"
        self.model_call_count = 0
        self.tool_call_count = 0
        self.item_count = 0
        self.setObjectName("ExecutionGroup")

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 3, 6, 4)
        root.setSpacing(3)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(7)
        self.toggle = QToolButton()
        self.toggle.setObjectName("ExecutionToggle")
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setText("执行过程")
        self.toggle.setCheckable(True)
        self.toggle.setToolTip("展开查看 Agent 回复、工具调用参数和结果")
        self.toggle.setAccessibleName("展开执行过程")
        self.toggle.toggled.connect(self.set_expanded)
        header.addWidget(self.toggle)

        self.status_label = QLabel(self.activity)
        self.status_label.setObjectName("ExecutionStatus")
        self.status_label.setProperty("state", self.state)
        header.addWidget(self.status_label, 1)

        self.count_label = QLabel("0 条记录")
        self.count_label.setObjectName("ExecutionCount")
        header.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignRight)
        root.addLayout(header)

        self.body = QWidget()
        self.body.setObjectName("ExecutionBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(4, 2, 4, 3)
        self.body_layout.setSpacing(6)
        self.body.hide()
        root.addWidget(self.body)
        self._refresh_header()

    def set_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        self.body.setVisible(expanded)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.toggle.setAccessibleName("收起执行过程" if expanded else "展开执行过程")
        self.toggle.setToolTip("收起执行过程" if expanded else "展开查看 Agent 回复、工具调用参数和结果")

    def add_item(self, widget: QWidget) -> None:
        self.body_layout.addWidget(widget)
        self.item_count += 1
        self._refresh_header()

    def record_model_call(self, turn: Any) -> None:
        self.model_call_count += 1
        try:
            turn_number = max(1, int(turn))
        except (TypeError, ValueError, OverflowError):
            turn_number = self.model_call_count
        self.activity = f"模型请求 · 第 {turn_number} 轮"
        self._refresh_header()

    def record_tool_call(self, name: str) -> None:
        self.tool_call_count += 1
        self.activity = f"正在调用 {name or '工具'}"
        self._refresh_header()

    def set_activity(self, activity: str) -> None:
        if activity:
            self.activity = activity[:100]
            self._refresh_header()

    def set_state(self, state: str) -> None:
        self.state = state if state in {"running", "waiting", "completed", "failed"} else "running"
        self.status_label.setProperty("state", self.state)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self._refresh_header()

    def _refresh_header(self) -> None:
        status_text = {
            "running": self.activity,
            "waiting": "等待网络重试",
            "completed": "已完成",
            "failed": "执行失败",
        }[self.state]
        self.status_label.setText(status_text)
        if self.status_label.property("state") != self.state:
            self.status_label.setProperty("state", self.state)
            self.status_label.style().unpolish(self.status_label)
            self.status_label.style().polish(self.status_label)
        self.count_label.setText(f"{self.tool_call_count} 次工具 · {self.item_count} 条记录")
        self.setToolTip(
            f"模型请求 {self.model_call_count} 次 · 工具调用 {self.tool_call_count} 次 · "
            f"过程记录 {self.item_count} 条"
        )


class NotificationToast(QFrame):
    """A compact, non-blocking workspace notification."""

    dismissed = Signal()
    DISPLAY_MS = 9000

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("NotificationToast")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumWidth(0)
        self.setMaximumWidth(440)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 11, 9, 11)
        layout.setSpacing(10)

        badge = QLabel("!")
        badge.setObjectName("NotificationBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(24, 24)
        badge.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(3)
        self.title_label = QLabel()
        self.title_label.setObjectName("NotificationTitle")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        self.title_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        content.addWidget(self.title_label)
        self.body_label = QLabel()
        self.body_label.setObjectName("NotificationBody")
        self.body_label.setTextFormat(Qt.TextFormat.PlainText)
        self.body_label.setWordWrap(True)
        self.body_label.setMaximumHeight(96)
        self.body_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content.addWidget(self.body_label)
        layout.addLayout(content, 1)

        self.close_button = QToolButton()
        self.close_button.setObjectName("NotificationClose")
        self.close_button.setText("×")
        self.close_button.setToolTip("关闭通知")
        self.close_button.setAccessibleName("关闭通知")
        self.close_button.setFixedSize(26, 26)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.clicked.connect(self.dismiss)
        layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

    def show_message(self, title: str, message: str) -> None:
        self.title_label.setText(title)
        self.body_label.setText(message)
        self.body_label.setToolTip(message)
        self.adjustSize()
        self._timer.start(self.DISPLAY_MS)

    def dismiss(self) -> None:
        self._timer.stop()
        self.hide()
        self.dismissed.emit()
