"""Build and reflow the Agent panel, settings controls and usage indicators.

The host retains state and lifecycle ownership; this module does not import its entry point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from PySide6.QtCore import QMimeData
from PySide6.QtCore import Qt
from PySide6.QtCore import QTimer
from PySide6.QtCore import Signal
from PySide6.QtGui import QImage
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QCheckBox
from PySide6.QtWidgets import QComboBox
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QGridLayout
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QLayout
from PySide6.QtWidgets import QMenu
from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtWidgets import QProgressBar
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QScrollArea
from PySide6.QtWidgets import QSpinBox
from PySide6.QtWidgets import QSizePolicy
from PySide6.QtWidgets import QStackedWidget
from PySide6.QtWidgets import QTextEdit
from PySide6.QtWidgets import QToolButton
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from scidev.integrations.provider import PROVIDER_PRESETS
from scidev.integrations.provider import is_local_provider
from scidev.integrations.provider import key_status as provider_key_status
from scidev.runtime.policy import ExecutionMode
from scidev.runtime.policy import NetworkPolicy
from scidev.agent.project_settings import SummarySettings
from scidev.desktop.plan_ui import TaskPlanPanel
from scidev.desktop.widgets import ClickableLabel
from scidev.desktop.widgets import ElidedLabel as ElidedLabel
from scidev.agent.usage import ModelRequestUsage
from scidev.agent.usage import estimate_text_tokens
from scidev.agent.usage import format_token_count
from scidev.desktop.agent_settings import ProviderCheckThread


class PromptEdit(QTextEdit):
    send_requested = Signal()
    image_pasted = Signal(object)
    image_file_pasted = Signal(str)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.send_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source: QMimeData) -> None:
        if source.hasImage():
            image = source.imageData()
            if isinstance(image, QImage) and not image.isNull():
                self.image_pasted.emit(image)
                return
        if source.hasUrls():
            for url in source.urls():
                if not url.isLocalFile():
                    continue
                path = Path(url.toLocalFile())
                if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                    self.image_file_pasted.emit(str(path))
                    return
        super().insertFromMimeData(source)



class AgentPanelMixin:
    """Operate on host state; introduce no initialization or additional state."""

    def _summary_settings_available_width(self) -> int:
        panel = getattr(self, "summary_settings_panel", None)
        page = getattr(self, "settings_page", None)
        if panel is not None and page is not None and panel.parentWidget() is page.scroll.widget():
            return page.scroll.viewport().width()
        if panel is not None and panel.width() > 0:
            return panel.width()
        chat = getattr(self, "chat_panel", None)
        return chat.width() if chat is not None else 0


    def _reflow_composer_meta(self, compact: bool) -> None:
        """Keep usage metadata from crowding the composer on narrow panes."""
        top = getattr(self, "chat_composer_top", None)
        meta = getattr(self, "chat_composer_meta", None)
        if top is None or meta is None or not hasattr(self, "usage_chip"):
            return
        moving = [self.usage_chip]
        for name in ("context_usage_label", "context_usage_bar"):
            widget = getattr(self, name, None)
            if widget is not None:
                moving.append(widget)
        target = meta if compact else top
        for widget in moving:
            (top if target is meta else meta).removeWidget(widget)
        if target is meta:
            for widget in moving:
                meta.insertWidget(max(0, meta.count() - 1), widget)
            return
        anchor_widget = getattr(self, "context_button", None)
        for widget in moving:
            anchor = top.indexOf(anchor_widget) if anchor_widget is not None else -1
            if anchor >= 0:
                top.insertWidget(anchor, widget)
            else:
                top.addWidget(widget)


    def _adapt_summary_settings_layout(self) -> None:
        layout = getattr(self, "summary_settings_layout", None)
        if layout is None:
            return
        # A four-column form needs ~640 px, more than the normal Agent pane.
        # Reflow independently from the composer's compact-text threshold, and
        # measure the space the panel is given rather than its clamped width so
        # a wide grid cannot lock itself out of the compact layout.
        compact = self._summary_settings_available_width() < 720
        if self._summary_settings_compact == compact:
            return
        self._summary_settings_compact = compact

        widgets = (
            self.summary_settings_title,
            self.summary_enabled_checkbox,
            self.summary_state_label,
            self.summary_model_label,
            self.summary_model_edit,
            self.summary_tokens_label,
            self.summary_tokens_spin,
            self.summary_context_label,
            self.summary_context_spin,
            self.summary_retry_label,
            self.summary_retry_spin,
            self.summary_failure_note,
            self.summary_interval_label,
            self.summary_interval_spin,
            self.summary_frequency_note,
            self.summary_instruction_label,
            self.summary_instruction_edit,
            self.project_memory_checkbox,
            self.project_memory_results_label,
            self.project_memory_results_spin,
            self.project_memory_context_label,
            self.project_memory_context_spin,
            self.acceptance_mode_label,
            self.acceptance_mode_combo,
            self.acceptance_model_label,
            self.acceptance_model_edit,
            self.acceptance_note,
            self.multi_agent_enabled_checkbox,
            self.multi_agent_model_label,
            self.multi_agent_model_edit,
            self.multi_agent_note,
            self.context_window_label,
            self.context_window_spin,
            self.context_window_note,
        )
        for widget in widgets:
            layout.removeWidget(widget)

        if compact:
            self.summary_settings_title.setText("Agent 设置")
            self.multi_agent_enabled_checkbox.setText("启用协作 Agent")
            layout.setContentsMargins(10, 16, 10, 16)
            layout.setHorizontalSpacing(6)
            layout.setVerticalSpacing(10)
            placements = (
                (self.summary_settings_title, 0, 0, 1, 1),
                (self.summary_state_label, 0, 1, 1, 1),
                (self.summary_enabled_checkbox, 1, 0, 1, 2),
                (self.summary_model_label, 2, 0, 1, 1),
                (self.summary_model_edit, 2, 1, 1, 1),
                (self.summary_tokens_label, 3, 0, 1, 1),
                (self.summary_tokens_spin, 3, 1, 1, 1),
                (self.summary_context_label, 4, 0, 1, 1),
                (self.summary_context_spin, 4, 1, 1, 1),
                (self.summary_retry_label, 5, 0, 1, 1),
                (self.summary_retry_spin, 5, 1, 1, 1),
                (self.summary_interval_label, 6, 0, 1, 1),
                (self.summary_interval_spin, 6, 1, 1, 1),
                (self.summary_instruction_label, 7, 0, 1, 1),
                (self.summary_instruction_edit, 7, 1, 1, 1),
                (self.project_memory_checkbox, 8, 0, 1, 2),
                (self.project_memory_results_label, 9, 0, 1, 1),
                (self.project_memory_results_spin, 9, 1, 1, 1),
                (self.project_memory_context_label, 10, 0, 1, 1),
                (self.project_memory_context_spin, 10, 1, 1, 1),
                (self.acceptance_mode_label, 11, 0, 1, 1),
                (self.acceptance_mode_combo, 11, 1, 1, 1),
                (self.acceptance_model_label, 12, 0, 1, 1),
                (self.acceptance_model_edit, 12, 1, 1, 1),
                (self.acceptance_note, 13, 0, 1, 2),
                (self.multi_agent_enabled_checkbox, 14, 0, 1, 2),
                (self.multi_agent_model_label, 15, 0, 1, 1),
                (self.multi_agent_model_edit, 15, 1, 1, 1),
                (self.multi_agent_note, 16, 0, 1, 2),
                (self.context_window_label, 17, 0, 1, 1),
                (self.context_window_spin, 17, 1, 1, 1),
                (self.context_window_note, 18, 0, 1, 2),
            )
            self.summary_enabled_checkbox.setText("对话结束自动总结")
            self.summary_state_label.setText(self._summary_compact_status_text())
            self.summary_failure_note.setVisible(False)
            self.summary_frequency_note.setVisible(False)
            state_alignment = Qt.AlignmentFlag.AlignLeft
            column_stretches = (0, 1, 0, 0)
        else:
            self.summary_settings_title.setText("总结、记忆、协作与验收")
            self.multi_agent_enabled_checkbox.setText("允许主 Agent 委派协作 Agent")
            layout.setContentsMargins(14, 16, 14, 16)
            layout.setHorizontalSpacing(8)
            layout.setVerticalSpacing(10)
            placements = (
                (self.summary_settings_title, 0, 0, 1, 4),
                (self.summary_enabled_checkbox, 1, 0, 1, 2),
                (self.summary_state_label, 1, 2, 1, 2),
                (self.summary_model_label, 2, 0, 1, 1),
                (self.summary_model_edit, 2, 1, 1, 3),
                (self.summary_tokens_label, 3, 0, 1, 1),
                (self.summary_tokens_spin, 3, 1, 1, 1),
                (self.summary_context_label, 3, 2, 1, 1),
                (self.summary_context_spin, 3, 3, 1, 1),
                (self.summary_retry_label, 4, 0, 1, 1),
                (self.summary_retry_spin, 4, 1, 1, 1),
                (self.summary_failure_note, 4, 2, 1, 2),
                (self.summary_interval_label, 5, 0, 1, 1),
                (self.summary_interval_spin, 5, 1, 1, 1),
                (self.summary_frequency_note, 5, 2, 1, 2),
                (self.summary_instruction_label, 6, 0, 1, 1),
                (self.summary_instruction_edit, 6, 1, 1, 3),
                (self.project_memory_checkbox, 7, 0, 1, 2),
                (self.project_memory_results_label, 7, 2, 1, 1),
                (self.project_memory_results_spin, 7, 3, 1, 1),
                (self.project_memory_context_label, 8, 0, 1, 1),
                (self.project_memory_context_spin, 8, 1, 1, 1),
                (self.acceptance_mode_label, 9, 0, 1, 1),
                (self.acceptance_mode_combo, 9, 1, 1, 3),
                (self.acceptance_model_label, 10, 0, 1, 1),
                (self.acceptance_model_edit, 10, 1, 1, 3),
                (self.acceptance_note, 11, 0, 1, 4),
                (self.multi_agent_enabled_checkbox, 12, 0, 1, 4),
                (self.multi_agent_model_label, 13, 0, 1, 1),
                (self.multi_agent_model_edit, 13, 1, 1, 3),
                (self.multi_agent_note, 14, 0, 1, 4),
                (self.context_window_label, 15, 0, 1, 1),
                (self.context_window_spin, 15, 1, 1, 1),
                (self.context_window_note, 16, 0, 1, 4),
            )
            self.summary_enabled_checkbox.setText("每次编码对话结束自动总结")
            self.summary_state_label.setText(self._summary_status_text())
            self.summary_failure_note.setVisible(True)
            self.summary_frequency_note.setVisible(True)
            state_alignment = Qt.AlignmentFlag.AlignRight
            column_stretches = (0, 1, 0, 1)

        for column, stretch in enumerate(column_stretches):
            layout.setColumnStretch(column, stretch)
            layout.setColumnMinimumWidth(column, 56 if compact and column == 0 else 0)
        for widget, row, column, row_span, column_span in placements:
            if widget is self.summary_state_label:
                layout.addWidget(widget, row, column, row_span, column_span, alignment=state_alignment)
            else:
                layout.addWidget(widget, row, column, row_span, column_span)
        for widget in widgets:
            if isinstance(widget, (QLineEdit, QSpinBox, QComboBox)):
                widget.setMinimumHeight(round(32 * self._ui_scale))
                widget.setMinimumWidth(0)
                widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            if isinstance(widget, QCheckBox):
                widget.setMinimumHeight(round(28 * self._ui_scale))
        # Keep the panel's natural height without letting the grid force a
        # minimum width larger than the space a narrow scroll viewport offers.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetDefaultConstraint)
        self.summary_settings_panel.setMinimumWidth(0)


    def _build_chat_panel(self) -> QWidget:
        chat = QFrame()
        self.chat_panel = chat
        chat.setObjectName("ChatPane")
        chat.setMinimumWidth(self.WORKBENCH_COMPACT_MINIMUMS[2])
        chat.setMaximumWidth(16777215)
        layout = QVBoxLayout(chat)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("ChatHeader")
        self.chat_header = header
        header.setFixedHeight(58)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 0, 12, 0)
        agent_logo = QLabel("✦")
        agent_logo.setObjectName("AgentLogo")
        agent_logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        agent_logo.setFixedSize(29, 29)
        header_layout.addWidget(agent_logo)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = ElidedLabel(self._provider_model() if self._provider_configured() else "Coding Agent")
        self.agent_title = title
        title.setObjectName("AppTitle")
        title.setToolTip(self._provider_model() if self._provider_configured() else "配置模型后开始编码")
        self.chat_status = ClickableLabel("就绪 · 等待任务" if self._provider_configured() else "未配置模型")
        self.chat_status.setObjectName("StateReady" if self._provider_configured() else "StateUnconfigured")
        self.chat_status.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chat_status.setToolTip("点击配置模型接入")
        self.chat_status.clicked.connect(self._open_agent_settings_from_preferences)
        title_box.addWidget(title)
        title_box.addWidget(self.chat_status)
        header_layout.addLayout(title_box, 1)
        more = QToolButton()
        more.setObjectName("IconButton")
        more.setText("…")
        more.setToolTip("Agent 设置")
        header_layout.addWidget(more)
        more.clicked.connect(self.toggle_summary_settings)
        self.agent_settings_button = more
        layout.addWidget(header)

        self.chat_body_stack = QStackedWidget()
        layout.addWidget(self.chat_body_stack, 1)
        conversation = QWidget()
        self.conversation_page = conversation
        layout = QVBoxLayout(conversation)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.chat_body_stack.addWidget(conversation)
        self.summary_settings_panel = QFrame()
        self.summary_settings_panel.setObjectName("SummarySettings")
        self.summary_settings_panel.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        settings_layout = QGridLayout(self.summary_settings_panel)
        settings_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.summary_settings_layout = settings_layout
        settings_layout.setContentsMargins(12, 9, 12, 9)
        settings_layout.setHorizontalSpacing(8)
        settings_layout.setVerticalSpacing(6)
        self.summary_settings_title = QLabel("总结、记忆、协作与验收")
        self.summary_settings_title.setObjectName("SummarySettingsTitle")
        settings_layout.addWidget(self.summary_settings_title, 0, 0, 1, 4)
        self.summary_enabled_checkbox = QCheckBox("每次编码对话结束自动总结")
        self.summary_enabled_checkbox.setChecked(self.summary_settings.enabled)
        self.summary_enabled_checkbox.toggled.connect(lambda _checked: self._persist_summary_settings())
        settings_layout.addWidget(self.summary_enabled_checkbox, 1, 0, 1, 2)
        self.summary_state_label = QLabel(self._summary_status_text())
        self.summary_state_label.setObjectName("SummaryState")
        settings_layout.addWidget(self.summary_state_label, 1, 2, 1, 2, alignment=Qt.AlignmentFlag.AlignRight)
        self.summary_model_label = QLabel("模型")
        settings_layout.addWidget(self.summary_model_label, 2, 0)
        self.summary_model_edit = QLineEdit(self.summary_settings.model)
        self.summary_model_edit.setPlaceholderText("跟随主模型")
        self.summary_model_edit.setToolTip("留空表示使用主 Agent 模型")
        self.summary_model_edit.editingFinished.connect(self._persist_summary_settings)
        settings_layout.addWidget(self.summary_model_edit, 2, 1, 1, 3)
        self.summary_tokens_label = QLabel("最大输出")
        settings_layout.addWidget(self.summary_tokens_label, 3, 0)
        self.summary_tokens_spin = QSpinBox()
        self.summary_tokens_spin.setRange(200, 32000)
        self.summary_tokens_spin.setSingleStep(1000)
        self.summary_tokens_spin.setValue(self.summary_settings.max_tokens)
        self.summary_tokens_spin.setSuffix(" tok")
        self.summary_tokens_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        settings_layout.addWidget(self.summary_tokens_spin, 3, 1)
        self.summary_context_label = QLabel("上下文")
        settings_layout.addWidget(self.summary_context_label, 3, 2)
        self.summary_context_spin = QSpinBox()
        self.summary_context_spin.setRange(4000, 50000)
        self.summary_context_spin.setSingleStep(1000)
        self.summary_context_spin.setValue(self.summary_settings.context_chars)
        self.summary_context_spin.setSuffix(" 字符")
        self.summary_context_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        settings_layout.addWidget(self.summary_context_spin, 3, 3)
        self.summary_retry_label = QLabel("失败重试")
        settings_layout.addWidget(self.summary_retry_label, 4, 0)
        self.summary_retry_spin = QSpinBox()
        self.summary_retry_spin.setRange(0, 3)
        self.summary_retry_spin.setValue(self.summary_settings.retries)
        self.summary_retry_spin.setSuffix(" 次")
        self.summary_retry_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        settings_layout.addWidget(self.summary_retry_spin, 4, 1)
        self.summary_failure_note = QLabel("总结失败不会影响代码任务")
        self.summary_failure_note.setObjectName("Hint")
        settings_layout.addWidget(self.summary_failure_note, 4, 2, 1, 2)
        self.summary_interval_label = QLabel("阶段频率")
        settings_layout.addWidget(self.summary_interval_label, 5, 0)
        self.summary_interval_spin = QSpinBox()
        self.summary_interval_spin.setRange(0, 32)
        self.summary_interval_spin.setValue(self.summary_settings.interval_turns)
        self.summary_interval_spin.setSuffix(" 轮")
        self.summary_interval_spin.setSpecialValueText("仅结束")
        self.summary_interval_spin.setToolTip("每 N 轮 Agent 调用生成一次阶段总结；0 表示只在对话结束时总结")
        self.summary_interval_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        settings_layout.addWidget(self.summary_interval_spin, 5, 1)
        self.summary_frequency_note = QLabel("0 = 只在结束时总结")
        self.summary_frequency_note.setObjectName("Hint")
        settings_layout.addWidget(self.summary_frequency_note, 5, 2, 1, 2)
        self.summary_instruction_label = QLabel("指令")
        settings_layout.addWidget(self.summary_instruction_label, 6, 0)
        self.summary_instruction_edit = QPlainTextEdit(self.summary_settings.instruction)
        self.summary_instruction_edit.setObjectName("AgentInstruction")
        self.summary_instruction_edit.setPlaceholderText("要求总结包含哪些内容")
        self.summary_instruction_edit.setMinimumHeight(66)
        self.summary_instruction_edit.setMaximumHeight(118)
        self.summary_instruction_edit.setTabChangesFocus(True)
        self.summary_instruction_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.summary_instruction_save_timer = QTimer(self)
        self.summary_instruction_save_timer.setSingleShot(True)
        self.summary_instruction_save_timer.timeout.connect(self._persist_summary_settings)
        self.summary_instruction_edit.textChanged.connect(
            lambda: self.summary_instruction_save_timer.start(500)
        )
        settings_layout.addWidget(self.summary_instruction_edit, 6, 1, 1, 3)
        self.project_memory_checkbox = QCheckBox("检索本项目历史总结")
        self.project_memory_checkbox.setChecked(self.summary_settings.memory_enabled)
        self.project_memory_checkbox.setToolTip("每个新任务从本项目的旧会话最终总结中检索相关内容；仅保存在本机 .research，不调用额外模型")
        self.project_memory_checkbox.toggled.connect(lambda _checked: self._persist_summary_settings())
        settings_layout.addWidget(self.project_memory_checkbox, 7, 0, 1, 2)
        self.project_memory_results_label = QLabel("引用条数")
        settings_layout.addWidget(self.project_memory_results_label, 7, 2)
        self.project_memory_results_spin = QSpinBox()
        self.project_memory_results_spin.setRange(1, 8)
        self.project_memory_results_spin.setValue(self.summary_settings.memory_results)
        self.project_memory_results_spin.setSuffix(" 条")
        self.project_memory_results_spin.setToolTip("单个新任务最多注入的历史总结数")
        self.project_memory_results_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        settings_layout.addWidget(self.project_memory_results_spin, 7, 3)
        self.project_memory_context_label = QLabel("上下文上限")
        settings_layout.addWidget(self.project_memory_context_label, 8, 0)
        self.project_memory_context_spin = QSpinBox()
        self.project_memory_context_spin.setRange(500, 16000)
        self.project_memory_context_spin.setSingleStep(1000)
        self.project_memory_context_spin.setValue(self.summary_settings.memory_context_chars)
        self.project_memory_context_spin.setSuffix(" 字符")
        self.project_memory_context_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        settings_layout.addWidget(self.project_memory_context_spin, 8, 1)
        self.acceptance_mode_label = QLabel("语义验收")
        settings_layout.addWidget(self.acceptance_mode_label, 9, 0)
        self.acceptance_mode_combo = QComboBox()
        self.acceptance_mode_combo.setMinimumContentsLength(6)
        self.acceptance_mode_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.acceptance_mode_combo.addItem("关闭", "off")
        self.acceptance_mode_combo.addItem("建议（不阻止提交）", "advisory")
        self.acceptance_mode_combo.addItem("必需（未通过不提交）", "required")
        mode_index = self.acceptance_mode_combo.findData(self.summary_settings.acceptance_mode)
        self.acceptance_mode_combo.setCurrentIndex(max(0, mode_index))
        self.acceptance_mode_combo.setToolTip("默认关闭。启用后会把任务、变更 diff 和检查摘要发送给单独配置的复核模型。")
        self.acceptance_mode_combo.currentIndexChanged.connect(lambda _index: self._persist_summary_settings())
        settings_layout.addWidget(self.acceptance_mode_combo, 9, 1, 1, 3)
        self.acceptance_model_label = QLabel("复核模型")
        settings_layout.addWidget(self.acceptance_model_label, 10, 0)
        self.acceptance_model_edit = QLineEdit(self.summary_settings.acceptance_model)
        self.acceptance_model_edit.setPlaceholderText("填写不同于编码模型的模型 ID")
        self.acceptance_model_edit.setToolTip("模型 ID 必须与编码模型不同；同一服务商仍不等于真正独立的机构审查。")
        self.acceptance_model_edit.editingFinished.connect(self._persist_summary_settings)
        settings_layout.addWidget(self.acceptance_model_edit, 10, 1, 1, 3)
        self.acceptance_note = QLabel("额外 LLM 调用；必需模式在审查不通过或证据不完整时保留改动、不自动提交。")
        self.acceptance_note.setObjectName("Hint")
        self.acceptance_note.setWordWrap(True)
        settings_layout.addWidget(self.acceptance_note, 11, 0, 1, 4)
        self.multi_agent_enabled_checkbox = QCheckBox("允许主 Agent 委派协作 Agent")
        self.multi_agent_enabled_checkbox.setChecked(self.summary_settings.multi_agent_enabled)
        self.multi_agent_enabled_checkbox.setToolTip(
            "默认关闭；可委派架构、测试、安全、编码或审阅专员。编码专员只能提交逐块审批补丁；所有专员均不能运行命令、浏览网页或操作 Git。"
        )
        self.multi_agent_enabled_checkbox.toggled.connect(self._on_multi_agent_enabled_changed)
        settings_layout.addWidget(self.multi_agent_enabled_checkbox, 12, 0, 1, 4)
        self.multi_agent_model_label = QLabel("协作模型")
        settings_layout.addWidget(self.multi_agent_model_label, 13, 0)
        self.multi_agent_model_edit = QLineEdit(self.summary_settings.multi_agent_model)
        self.multi_agent_model_edit.setPlaceholderText("留空，跟随主模型")
        self.multi_agent_model_edit.setToolTip("可指定协作 Agent 使用的模型 ID；留空跟随主模型。模型 ID 不同不代表不同机构。")
        self.multi_agent_model_edit.setEnabled(self.summary_settings.multi_agent_enabled)
        self.multi_agent_model_edit.editingFinished.connect(self._persist_summary_settings)
        settings_layout.addWidget(self.multi_agent_model_edit, 13, 1, 1, 3)
        self.multi_agent_note = QLabel("每任务最多 3 次委派；编码专员仅能提交一次逐块审批补丁，其他角色只读。主 Agent 仍负责核验与测试。关闭时不增加模型调用。")
        self.multi_agent_note.setObjectName("Hint")
        self.multi_agent_note.setWordWrap(True)
        settings_layout.addWidget(self.multi_agent_note, 14, 0, 1, 4)
        self.execution_mode_label = QLabel("执行策略")
        self.execution_mode_combo = QComboBox()
        self.execution_mode_combo.setMinimumContentsLength(10)
        self.execution_mode_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.execution_mode_combo.addItem("只读（不写文件、不跑命令）", ExecutionMode.READ_ONLY.value)
        self.execution_mode_combo.addItem("手动审批（每条命令都询问）", ExecutionMode.MANUAL.value)
        self.execution_mode_combo.addItem("工作区可写（自动跑验证命令）", ExecutionMode.WORKSPACE_WRITE.value)
        self.execution_mode_combo.addItem("完全访问（自动跑所有命令，危险）", ExecutionMode.FULL_ACCESS.value)
        mode_index = self.execution_mode_combo.findData(self.summary_settings.execution_mode)
        self.execution_mode_combo.setCurrentIndex(max(0, mode_index))
        self.execution_mode_combo.setToolTip(
            "只读：禁止写文件和运行命令。\n"
            "手动审批：允许写文件，但每条终端命令都需要确认。\n"
            "工作区可写：文件限制在项目内；自动批准 pytest/unittest/npm test、ruff/eslint、mypy/tsc 等验证命令，"
            "其他命令仍需逐条批准。\n"
            "完全访问：自动批准所有终端命令，仍保留破坏性命令和手动 Git 操作拦截。"
        )
        self.execution_mode_combo.currentIndexChanged.connect(lambda _index: self._persist_summary_settings())
        settings_layout.addWidget(self.execution_mode_label, 16, 0)
        settings_layout.addWidget(self.execution_mode_combo, 16, 1, 1, 3)
        self.execution_mode_note = QLabel(
            "这是命令策略，不是操作系统沙箱：已批准的命令仍以当前 Windows 用户权限运行，"
            "无法保证网络或项目外文件访问。完全访问模式请只在可信项目中使用。"
        )
        self.execution_mode_note.setObjectName("Hint")
        self.execution_mode_note.setWordWrap(True)
        settings_layout.addWidget(self.execution_mode_note, 17, 0, 1, 4)
        self.network_policy_label = QLabel("网络策略")
        self.network_policy_combo = QComboBox()
        self.network_policy_combo.addItem("拒绝网络命令", NetworkPolicy.DENY.value)
        self.network_policy_combo.addItem("询问后执行", NetworkPolicy.ASK.value)
        self.network_policy_combo.addItem("允许网络命令", NetworkPolicy.ALLOW.value)
        self.network_policy_combo.setMinimumContentsLength(10)
        self.network_policy_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        network_index = self.network_policy_combo.findData(self.summary_settings.network_policy)
        self.network_policy_combo.setCurrentIndex(max(0, network_index))
        self.network_policy_combo.setToolTip(
            "只对识别出的 curl/wget/ssh/scp 等网络命令生效。\n"
            "拒绝：即使完全访问也直接阻断。\n"
            "询问：手动批准后执行，工作区可写不会自动执行。\n"
            "允许：工作区可写/完全访问会自动执行网络命令。"
        )
        self.network_policy_combo.currentIndexChanged.connect(lambda _index: self._persist_summary_settings())
        settings_layout.addWidget(self.network_policy_label, 18, 0)
        settings_layout.addWidget(self.network_policy_combo, 18, 1, 1, 3)
        self.allowed_commands_label = QLabel("命令白名单")
        self.allowed_commands_edit = QLineEdit()
        self.allowed_commands_edit.setPlaceholderText("用 ; 分隔，例如 pytest -q; ruff check .")
        self.allowed_commands_edit.setText("; ".join(self.summary_settings.allowed_commands))
        self.allowed_commands_edit.setToolTip(
            "精确命令前缀；匹配的命令在工作区可写/完全访问下自动执行，不再逐条询问。\n"
            "仍受破坏性命令、网络策略和绝对路径/上级目录检查约束。"
        )
        self.allowed_commands_edit.editingFinished.connect(lambda: self._persist_summary_settings())
        settings_layout.addWidget(self.allowed_commands_label, 19, 0)
        settings_layout.addWidget(self.allowed_commands_edit, 19, 1, 1, 3)
        self.context_window_label = QLabel("模型上下文窗口")
        self.context_window_spin = QSpinBox()
        self.context_window_spin.setRange(1024, 1_048_576)
        self.context_window_spin.setSingleStep(4096)
        self.context_window_spin.setValue(self.summary_settings.context_window_tokens)
        self.context_window_spin.setSuffix(" tok")
        self.context_window_spin.setToolTip("填写当前主模型实际支持的上下文窗口；默认 32768 tokens。")
        self.context_window_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        self.context_window_note = QLabel(
            "占用按请求估算；接口报告输入 tokens 后显示实际值。请按模型实际窗口设置。"
        )
        self.context_window_note.setObjectName("Hint")
        self.context_window_note.setWordWrap(True)
        settings_layout.addWidget(self.context_window_label, 21, 0)
        settings_layout.addWidget(self.context_window_spin, 21, 1)
        settings_layout.addWidget(self.context_window_note, 22, 0, 1, 4)
        self.sandbox_backend_label = QLabel("命令沙箱")
        self.sandbox_backend_combo = QComboBox()
        self.sandbox_backend_combo.setMinimumContentsLength(10)
        self.sandbox_backend_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.sandbox_backend_combo.addItem("本机（无 OS 沙箱）", "local")
        self.sandbox_backend_combo.addItem("Docker（仅工作区，默认断网）", "docker")
        sandbox_index = self.sandbox_backend_combo.findData(self.summary_settings.sandbox_backend)
        self.sandbox_backend_combo.setCurrentIndex(max(0, sandbox_index))
        self.sandbox_backend_combo.setToolTip(
            "本机：命令以当前用户权限运行，仅受策略/审批约束。\n"
            "Docker：命令在容器内运行，只挂载工作区；需要本机 Docker 和包含项目工具链的镜像。"
        )
        self.sandbox_backend_combo.currentIndexChanged.connect(lambda _index: self._persist_summary_settings())
        settings_layout.addWidget(self.sandbox_backend_label, 24, 0)
        settings_layout.addWidget(self.sandbox_backend_combo, 24, 1, 1, 3)
        self.sandbox_image_label = QLabel("沙箱镜像")
        self.sandbox_image_edit = QLineEdit()
        self.sandbox_image_edit.setPlaceholderText("例如 scidev-tools:latest")
        self.sandbox_image_edit.setText(self.summary_settings.sandbox_image)
        self.sandbox_image_edit.setToolTip(
            "Docker 后端必填；镜像必须自带项目解释器、依赖和测试工具。修改后对新命令生效。"
        )
        self.sandbox_image_edit.editingFinished.connect(lambda: self._persist_summary_settings())
        settings_layout.addWidget(self.sandbox_image_label, 25, 0)
        settings_layout.addWidget(self.sandbox_image_edit, 25, 1, 1, 3)
        self.sandbox_note = QLabel(
            "Docker 沙箱会拒绝网络（除非宿主网络策略为 allow 且显式设 bridge）、丢弃 capabilities，"
            "并限制 pids/内存/CPU；daemon 或镜像不可用时命令直接失败，不会回退到本机。"
        )
        self.sandbox_note.setObjectName("Hint")
        self.sandbox_note.setWordWrap(True)
        settings_layout.addWidget(self.sandbox_note, 26, 0, 1, 4)
        self.summary_settings_scroll = QScrollArea()
        self.summary_settings_scroll.setObjectName("SettingsScroll")
        self.summary_settings_scroll.setWidgetResizable(True)
        self.summary_settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.summary_settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.summary_settings_scroll.setWidget(self.summary_settings_panel)
        self.chat_body_stack.addWidget(self.summary_settings_scroll)
        self.chat_body_stack.setCurrentWidget(conversation)
        provider_note = QLabel(
            "模型接入配置保存在项目 .research/provider.json；API Key 存入 Windows 凭据管理器。\n"
            "没有保存配置时使用启动环境变量；保存后下一次任务生效。上面的“模型”字段只覆盖总结模型。"
        )
        provider_note.setObjectName("Hint")
        provider_note.setWordWrap(True)
        provider_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        settings_layout.addWidget(provider_note, 20, 0, 1, 4)
        self.restore_checkpoint_button = QPushButton("回滚上次任务改动的文件")
        self.restore_checkpoint_button.setObjectName("Quiet")
        self.restore_checkpoint_button.setToolTip(
            "只恢复 Agent 上次任务实际写过的文件；任务开始前不存在的文件会被删除。"
        )
        self.restore_checkpoint_button.clicked.connect(self._restore_last_checkpoint)
        settings_layout.addWidget(self.restore_checkpoint_button, 21, 0, 1, 4)

        provider = self.provider_config
        self.provider_title = QLabel("模型接入")
        self.provider_title.setObjectName("SectionTitle")
        self.provider_preset_combo = QComboBox()
        self.provider_preset_combo.setObjectName("ProviderPreset")
        self.provider_preset_combo.setMinimumContentsLength(12)
        self.provider_preset_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        for preset in PROVIDER_PRESETS:
            self.provider_preset_combo.addItem(preset.label, preset.key)
        self.provider_preset_combo.setToolTip("选择服务商模板；只填入地址和模型，不会自动上传 Key")
        self.provider_preset_combo.setMaximumWidth(260)
        self.provider_local_button = QPushButton("检测本地")
        self.provider_local_button.setObjectName("Quiet")
        self.provider_local_button.setToolTip("探测 Ollama、LM Studio、llama.cpp 本机端口")
        self.provider_local_button.clicked.connect(self._detect_local_providers)
        provider_header = QWidget()
        provider_header_layout = QHBoxLayout(provider_header)
        provider_header_layout.setContentsMargins(0, 0, 0, 0)
        provider_header_layout.setSpacing(6)
        provider_header_layout.addWidget(self.provider_title)
        provider_header_layout.addStretch(1)
        provider_header_layout.addWidget(self.provider_preset_combo)
        provider_header_layout.addWidget(self.provider_local_button)
        settings_layout.addWidget(provider_header, 22, 0, 1, 4)
        self.provider_base_label = QLabel("API Base URL")
        settings_layout.addWidget(self.provider_base_label, 23, 0)
        self.provider_base_edit = QLineEdit(provider.base_url if provider else "https://api.openai.com/v1")
        self.provider_base_edit.setPlaceholderText("https://api.openai.com/v1")
        settings_layout.addWidget(self.provider_base_edit, 23, 1, 1, 3)
        self.provider_model_label = QLabel("模型")
        settings_layout.addWidget(self.provider_model_label, 24, 0)
        self.provider_model_edit = QLineEdit(provider.model if provider else "gpt-5")
        self.provider_model_edit.setPlaceholderText("gpt-5")
        settings_layout.addWidget(self.provider_model_edit, 24, 1, 1, 3)
        self.provider_reasoning_label = QLabel("推理强度")
        settings_layout.addWidget(self.provider_reasoning_label, 25, 0)
        self.provider_reasoning_combo = QComboBox()
        for label, value in (
            ("默认", ""),
            ("none", "none"),
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
            ("max", "max"),
        ):
            self.provider_reasoning_combo.addItem(label, value)
        current_reasoning = provider.reasoning_effort if provider else ""
        reasoning_index = self.provider_reasoning_combo.findData(current_reasoning)
        self.provider_reasoning_combo.setCurrentIndex(max(0, reasoning_index))
        settings_layout.addWidget(self.provider_reasoning_combo, 25, 1, 1, 3)
        self.provider_key_label = QLabel("API Key")
        settings_layout.addWidget(self.provider_key_label, 26, 0)
        self.provider_key_edit = QLineEdit()
        self.provider_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_state = provider_key_status(self.project_root)
        self.provider_key_edit.setPlaceholderText(
            "已保存在 Windows 凭据管理器；留空表示不修改"
            if key_state == "credential"
            else "输入 API Key（保存到 Windows 凭据管理器）"
        )
        self.provider_key_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        if provider is not None and is_local_provider(provider.base_url):
            self.provider_key_edit.setPlaceholderText("本地服务通常无需 Key；如需可填写")
        settings_layout.addWidget(self.provider_key_edit, 26, 1)
        self.provider_fallback_label = QLabel("回退模型")
        settings_layout.addWidget(self.provider_fallback_label, 26, 2)
        self.provider_fallback_edit = QLineEdit(provider.fallback_model if provider else "")
        self.provider_fallback_edit.setPlaceholderText("可选")
        self.provider_fallback_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        settings_layout.addWidget(self.provider_fallback_edit, 26, 3)
        self.provider_actions = QWidget()
        provider_actions_layout = QHBoxLayout(self.provider_actions)
        provider_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.provider_save_button = QPushButton("保存模型配置")
        self.provider_save_button.setObjectName("Primary")
        self.provider_save_button.clicked.connect(self._save_provider_settings)
        provider_actions_layout.addWidget(self.provider_save_button)
        self.provider_test_button = QPushButton("测试连接")
        self.provider_test_button.setObjectName("Quiet")
        self.provider_test_button.clicked.connect(self._test_provider_connection)
        provider_actions_layout.addWidget(self.provider_test_button)
        self.provider_probe_button = QPushButton("探测能力")
        self.provider_probe_button.setObjectName("Quiet")
        self.provider_probe_button.clicked.connect(self._probe_provider_capabilities)
        provider_actions_layout.addWidget(self.provider_probe_button)
        provider_actions_layout.addStretch(1)
        settings_layout.addWidget(self.provider_actions, 27, 0, 1, 4)
        self.provider_status_label = QLabel(
            "API Key 只写入 Windows 凭据管理器，不写入项目设置或审计日志。"
        )
        self.provider_status_label.setObjectName("Hint")
        self.provider_status_label.setWordWrap(True)
        if provider is not None and provider.source == "saved":
            self.provider_status_label.setText(f"已保存：{provider.base_url} · {provider.model}")
        elif provider is not None:
            self.provider_status_label.setText(
                f"当前来自环境变量：{provider.base_url} · {provider.model}；保存后会覆盖环境变量。"
            )
        settings_layout.addWidget(self.provider_status_label, 28, 0, 1, 4)
        self.provider_preset_combo.currentIndexChanged.connect(self._apply_provider_preset)
        self._sync_provider_preset_selection()
        self._provider_check_thread: ProviderCheckThread | None = None

        self.budget_title = QLabel("任务预算")
        self.budget_title.setObjectName("SectionTitle")
        settings_layout.addWidget(self.budget_title, 29, 0, 1, 4)
        self.max_agent_turns_label = QLabel("最多模型轮次")
        settings_layout.addWidget(self.max_agent_turns_label, 30, 0)
        self.max_agent_turns_spin = QSpinBox()
        self.max_agent_turns_spin.setRange(1, 64)
        self.max_agent_turns_spin.setValue(self.summary_settings.max_agent_turns)
        self.max_agent_turns_spin.setSuffix(" 轮")
        self.max_agent_turns_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        settings_layout.addWidget(self.max_agent_turns_spin, 30, 1, 1, 3)
        self.max_task_tokens_label = QLabel("最多 token")
        settings_layout.addWidget(self.max_task_tokens_label, 31, 0)
        self.max_task_tokens_spin = QSpinBox()
        self.max_task_tokens_spin.setRange(0, 10_000_000)
        self.max_task_tokens_spin.setSingleStep(1000)
        self.max_task_tokens_spin.setValue(self.summary_settings.max_task_tokens)
        self.max_task_tokens_spin.setSpecialValueText("不限")
        self.max_task_tokens_spin.valueChanged.connect(lambda _value: self._persist_summary_settings())
        settings_layout.addWidget(self.max_task_tokens_spin, 31, 1, 1, 3)
        self.budget_note = QLabel(
            "轮次硬上限最多 64；token 预算只统计服务端实际返回的 usage，0 = 不限。"
        )
        self.budget_note.setObjectName("Hint")
        self.budget_note.setWordWrap(True)
        settings_layout.addWidget(self.budget_note, 32, 0, 1, 4)

        self.session_resume_title = QLabel("会话恢复")
        self.session_resume_title.setObjectName("SectionTitle")
        settings_layout.addWidget(self.session_resume_title, 33, 0, 1, 4)
        self.session_resume_label = QLabel("最近会话")
        settings_layout.addWidget(self.session_resume_label, 34, 0)
        self.session_resume_combo = QComboBox()
        self.session_resume_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.session_resume_combo.setMinimumContentsLength(8)
        self.session_resume_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        settings_layout.addWidget(self.session_resume_combo, 34, 1, 1, 3)
        self.session_resume_button = QPushButton("恢复选中会话")
        self.session_resume_button.setObjectName("Quiet")
        self.session_resume_button.clicked.connect(self._restore_selected_session)
        settings_layout.addWidget(self.session_resume_button, 35, 0, 1, 4)
        self.session_resume_note = QLabel(
            "恢复后下一条消息会继续该会话的历史上下文；不会自动重放已完成的工具调用。"
        )
        self.session_resume_note.setObjectName("Hint")
        self.session_resume_note.setWordWrap(True)
        settings_layout.addWidget(self.session_resume_note, 36, 0, 1, 4)
        self._refresh_session_list()

        self.task_plan_panel = TaskPlanPanel(
            self.planning_mode,
            self.ui_settings,
            self._on_planning_mode_changed,
            chat,
        )
        layout.addWidget(self.task_plan_panel)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setObjectName("ChatScroll")
        self.chat_scroll.setWidgetResizable(True)
        self.chat_content = QWidget()
        self.chat_content.setObjectName("ChatContent")
        self.chat_layout = QVBoxLayout(self.chat_content)
        self.chat_layout.setContentsMargins(12, 14, 12, 14)
        self.chat_layout.setSpacing(8)
        self.chat_layout.addStretch(1)
        self.chat_scroll.setWidget(self.chat_content)
        layout.addWidget(self.chat_scroll, 1)

        composer = QFrame()
        composer.setObjectName("Composer")
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(12, 10, 12, 10)
        composer_layout.setSpacing(7)
        composer_top = QHBoxLayout()
        self.chat_composer_layout = composer_layout
        self.chat_composer_top = composer_top
        self.chat_mode_chip = QLabel("Agent")
        self.chat_mode_chip.setObjectName("Chip")
        composer_top.addWidget(self.chat_mode_chip)
        self.retry_chip = QLabel("重试")
        self.retry_chip.setObjectName("Chip")
        self.retry_chip.setAccessibleName("自动重试")
        self.retry_chip.setToolTip("网络请求失败后自动排队并重试")
        self.usage_chip = QLabel("模型用量")
        self.usage_chip.setObjectName("UsageQuiet")
        self.usage_chip.setAccessibleName("模型请求用量")
        self._display_model_usage({"usage": self.model_usage, "record": None})
        self.summary_chip = QLabel(self._summary_chip_text())
        self.summary_chip.setObjectName("Chip")
        self.summary_chip.setToolTip(self._summary_status_text())
        self.summary_chip.setAccessibleName(self._summary_status_text())
        composer_top.addWidget(self.summary_chip)
        composer_top.addWidget(self.usage_chip)
        self.context_usage_label = QLabel()
        self.context_usage_label.setObjectName("ContextUsageLabel")
        self.context_usage_label.setAccessibleName("上下文占用")
        self.context_usage_label.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        composer_top.addWidget(self.context_usage_label)
        self.context_usage_bar = QProgressBar()
        self.context_usage_bar.setObjectName("ContextUsageBar")
        self.context_usage_bar.setRange(0, 1000)
        self.context_usage_bar.setTextVisible(False)
        self.context_usage_bar.setFixedSize(round(68 * self._ui_scale), max(6, round(6 * self._ui_scale)))
        self.context_usage_bar.setAccessibleName("上下文使用比例")
        composer_top.addWidget(self.context_usage_bar)
        self._render_context_meter()
        context_button = QToolButton()
        context_button.setObjectName("IconButton")
        context_button.setText("@")
        context_button.setToolTip("引用文件/选区，或查看 Agent 的项目上下文")
        context_button.setAccessibleName("项目上下文")
        context_menu = QMenu(context_button)
        context_menu.addAction("引用当前文件或选区", self._insert_current_file_context)
        context_menu.addAction("查看项目上下文", self.show_project_context)
        context_button.setMenu(context_menu)
        context_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.context_button = context_button
        composer_top.addWidget(context_button)
        composer_top.addStretch(1)
        composer_layout.addLayout(composer_top)
        self.chat_composer_meta = QHBoxLayout()
        self.chat_composer_meta.setContentsMargins(0, 0, 0, 0)
        self.chat_composer_meta.setSpacing(6)
        self.chat_composer_meta.addStretch(1)
        composer_layout.addLayout(self.chat_composer_meta)
        self.retry_chip.hide()
        for chip in (self.chat_mode_chip, self.summary_chip):
            chip.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

        self.chat_input = PromptEdit()
        self.chat_input.setObjectName("ChatInput")
        self.chat_input.setPlaceholderText("告诉 Agent 你想修改什么…")
        self.chat_input.setFixedHeight(76)
        self.chat_input.textChanged.connect(self._render_context_meter)
        self.chat_input.send_requested.connect(self.submit_chat)
        self.chat_input.image_pasted.connect(self._attach_pasted_image)
        self.chat_input.image_file_pasted.connect(self._attach_image_path)
        composer_layout.addWidget(self.chat_input)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("Ctrl + Enter 发送")
        hint.setObjectName("Hint")
        bottom.addWidget(hint)
        self.attach_image_button = QPushButton("图片")
        self.attach_image_button.setObjectName("Quiet")
        self.attach_image_button.setToolTip("附加 PNG/JPEG/GIF/WebP，最多 4 张，每张 5 MB")
        self.attach_image_button.clicked.connect(self._choose_image_attachment)
        bottom.addWidget(self.attach_image_button)
        self.image_attachment_label = QLabel()
        self.image_attachment_label.setObjectName("Hint")
        self.image_attachment_label.setVisible(False)
        bottom.addWidget(self.image_attachment_label)
        self.clear_image_button = QPushButton("清除")
        self.clear_image_button.setObjectName("Quiet")
        self.clear_image_button.setVisible(False)
        self.clear_image_button.clicked.connect(self._clear_image_attachments)
        bottom.addWidget(self.clear_image_button)
        bottom.addStretch(1)
        self.model_selector = QComboBox()
        self.model_selector.setObjectName("ModelSelector")
        self.model_selector.setMinimumContentsLength(12)
        self.model_selector.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.model_selector.setMaximumWidth(240)
        self.model_selector.setToolTip("选择本次任务使用的模型")
        self.model_selector.currentIndexChanged.connect(self._on_model_selector_activated)
        bottom.addWidget(self.model_selector)
        send = QPushButton("发送")
        send.setObjectName("Primary")
        send.clicked.connect(self.submit_chat)
        bottom.addWidget(send)
        self.stop_task_button = QPushButton("停止")
        self.stop_task_button.setObjectName("Quiet")
        self.stop_task_button.setToolTip("停止当前编码任务；本次改动不会自动提交")
        self.stop_task_button.clicked.connect(self.cancel_active_task)
        self.stop_task_button.setVisible(False)
        bottom.addWidget(self.stop_task_button)
        composer_layout.addLayout(bottom)
        layout.addWidget(composer)
        self._refresh_model_selector()
        return chat


    def _summary_status_text(self) -> str:
        settings = getattr(self, "summary_settings", SummarySettings())
        if not settings.enabled:
            return "总结已关闭"
        return f"总结已开启 · 每{settings.interval_turns}轮" if settings.interval_turns else "总结已开启 · 仅结束"


    def _display_model_usage(self, payload: dict[str, Any]) -> None:
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        self.model_usage = {
            name: max(0, int(usage.get(name, 0) or 0))
            for name in ModelRequestUsage.COUNTERS
        }
        if not hasattr(self, "usage_chip"):
            return
        attempts = self.model_usage["request_attempts"]
        request_size = ModelRequestUsage.format_bytes(self.model_usage["request_body_bytes"])
        response_size = ModelRequestUsage.format_bytes(self.model_usage["response_body_bytes"])
        usage_text = (
            f"LLM {attempts}"
            if self._is_chat_compact()
            else f"LLM {attempts} · ↑{request_size} ↓{response_size}"
        )
        self.usage_chip.setText(usage_text)
        self.usage_chip.setVisible(attempts > 0)
        record = payload.get("record")
        if (
            isinstance(record, dict)
            and record.get("kind") == "coding"
            and str(payload.get("session_id") or "") == self._context_usage_session_id
            and isinstance(record.get("prompt_tokens"), int)
            and not isinstance(record.get("prompt_tokens"), bool)
        ):
            self._context_usage_actual_tokens = max(0, record["prompt_tokens"])
            self._render_context_meter()
        token_text = (
            f"已报告 tokens：输入 {self.model_usage['prompt_tokens']:,} · "
            f"输出 {self.model_usage['completion_tokens']:,} · 总计 {self.model_usage['total_tokens']:,}"
            if self.model_usage["token_reports"] else "当前接口未返回 token usage"
        )
        self.usage_chip.setToolTip(
            f"模型请求尝试：{attempts}（重试 {self.model_usage['retry_attempts']} 次，"
            f"失败 {self.model_usage['failed_attempts']} 次，"
            f"代理回退 {self.model_usage['proxy_fallback_attempts']} 次）\n"
            f"请求体：{request_size} · 响应体：{response_size}\n"
            f"累计请求耗时：{self.model_usage['elapsed_ms'] / 1000:.1f} 秒\n{token_text}\n"
            "字节数统计 JSON 请求体与客户端读取的响应体，不含 HTTP/TLS 头和传输层开销。"
        )


    def _render_context_meter(self) -> None:
        label = getattr(self, "context_usage_label", None)
        if label is None:
            return
        settings = getattr(self, "summary_settings", SummarySettings())
        window_tokens = max(1024, int(settings.context_window_tokens))
        draft = estimate_text_tokens(
            self.chat_input.toPlainText() if hasattr(self, "chat_input") else ""
        )
        actual_tokens = self._context_usage_actual_tokens
        base_tokens = actual_tokens if actual_tokens is not None else self._context_usage_estimated_tokens
        used_tokens = max(0, base_tokens) + draft
        approximate = actual_tokens is None or draft > 0
        percentage = min(100, round(used_tokens * 100 / window_tokens))
        prefix = "≈" if approximate else ""
        count_text = f"{prefix}{format_token_count(used_tokens)}/{format_token_count(window_tokens)}"
        if self._is_chat_compact():
            label.setText(f"上下文 {count_text}")
        else:
            label.setText(f"上下文 {count_text} · {percentage}%")
        source = (
            "估算值：按请求 JSON UTF-8 字节数折算，非模型专用 tokenizer。"
            if actual_tokens is None
            else "占用以接口报告的本轮输入 tokens 为准。"
        )
        if draft:
            source += f"\n正在编辑的输入另估约 {draft:,} tokens。"
        label.setToolTip(
            f"上下文占用 {used_tokens:,} / {window_tokens:,} tokens（{percentage}%）\n"
            f"{source}\n模型上下文窗口可在 Agent 设置中调整。"
        )
        label.setAccessibleDescription(label.toolTip())
        bar = getattr(self, "context_usage_bar", None)
        if bar is not None:
            bar.setRange(0, 1000)
            bar.setValue(min(1000, round(used_tokens * 1000 / window_tokens)))
            level = "critical" if percentage >= 90 else "warning" if percentage >= 75 else "normal"
            if bar.property("level") != level:
                bar.setProperty("level", level)
                bar.style().unpolish(bar)
                bar.style().polish(bar)
            bar.setToolTip(label.toolTip())


    def _on_planning_mode_changed(self, mode: str) -> None:
        self.planning_mode = mode


    def _is_chat_compact(self) -> bool:
        chat_panel = getattr(self, "chat_panel", None)
        if chat_panel is None:
            return False
        # Compaction is about the pane that actually renders the chips; while
        # the pane is hidden (settings, version tree) or not yet shown there is
        # no width to compact against.
        if not chat_panel.isVisible():
            return False
        return chat_panel.width() < 390


    def _summary_compact_status_text(self) -> str:
        settings = getattr(self, "summary_settings", SummarySettings())
        if not settings.enabled:
            return "已关闭"
        return f"已开 · {settings.interval_turns}轮" if settings.interval_turns else "已开 · 结束"


    def _summary_chip_text(self) -> str:
        settings = getattr(self, "summary_settings", SummarySettings())
        compact = self._is_chat_compact()
        if not settings.enabled:
            return "关" if compact else "总结关"
        if settings.interval_turns:
            return f"{settings.interval_turns}轮" if compact else f"总结 {settings.interval_turns}轮"
        return "结束" if compact else "总结结束"


    def toggle_summary_settings(self) -> None:
        if self.workspace_stack.currentWidget() is self.settings_page:
            self._close_settings()
            return
        self._open_agent_settings_from_preferences()
