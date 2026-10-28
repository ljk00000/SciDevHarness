"""Searchable workbench settings page, with no workspace or Agent ownership."""
from __future__ import annotations

from dataclasses import dataclass
import re

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from scidev_preferences import AppearancePreferences
from scidev_ui import UI_PROFILES


@dataclass(frozen=True)
class SettingSpec:
    key: str
    category: str
    title: str
    description: str
    search_key: str
    choices: tuple = ()
    limits: tuple[int, int] | None = None
    suffix: str = ""


CATEGORIES = ("全部", "外观", "文字", "布局", "编辑器", "Agent")
SPECS = (
    SettingSpec("profile", "外观", "颜色主题", "主题同时应用于工作台、编辑器和开发版本树。", "workbench color theme", tuple((p.label, k) for k, p in UI_PROFILES.items())),
    SettingSpec("accent_color", "外观", "强调色", "输入 #RRGGBB；留空跟随主题。按钮文字自动匹配明暗对比度。", "workbench accent color 颜色"),
    SettingSpec("density", "外观", "界面密度", "舒适模式留出呼吸空间；紧凑模式缩小按钮与文件列表间距。", "workbench density spacing", (("舒适", "comfortable"), ("紧凑", "compact"))),
    SettingSpec("zoom", "外观", "界面缩放", "自动适配显示器，或使用固定缩放；Windows 的 DPI 缩放仍会生效。", "window zoom scale", (("自动适配屏幕", 0), ("90%", 90), ("100%", 100), ("110%", 110), ("125%", 125), ("150%", 150))),
    SettingSpec("font_family", "文字", "界面字体", "自动优先中文无衬线字体；不会下载或安装新字体。", "workbench font family 字体"),
    SettingSpec("font_size", "文字", "界面字号", "调整导航、对话和设置文字；代码字号可单独设置。", "workbench font size 字号", limits=(10, 16), suffix=" pt"),
    SettingSpec("font_weight", "文字", "正文粗细", "让文字更清晰；按钮和标题使用独立字重。", "workbench font weight 字重", (("常规 · 400", 400), ("中等 · 500", 500), ("较粗 · 600", 600))),
    SettingSpec("control_weight", "文字", "按钮与标题粗细", "同时调整按钮、导航标题和标签文字。", "workbench control font weight 字重", (("中等 · 500", 500), ("半粗 · 600", 600), ("加粗 · 700", 700))),
    SettingSpec("rail_width", "布局", "左侧图标栏宽度", "点击区域随栏宽变化；关闭资源栏时也保留此宽度。", "activity bar sidebar width", limits=(64, 104), suffix=" px"),
    SettingSpec("icon_size", "布局", "侧栏图标大小", "高 DPI 下使用高分辨率图标；尺寸不会超出点击区域。", "activity bar icon size", limits=(24, 40), suffix=" px"),
    SettingSpec("explorer_mode", "布局", "资源管理器显示", "自动模式在窄窗口收起；也可以始终显示或默认隐藏。", "explorer sidebar visibility", (("自动 · 响应式", "auto"), ("始终显示", "show"), ("默认隐藏", "hide"))),
    SettingSpec("editor_font_family", "编辑器", "代码字体", "仅列出已安装的等宽字体，不影响界面和对话文字。", "editor font family 字体"),
    SettingSpec("editor_font_size", "编辑器", "代码字号", "应用于所有打开的文件及后续新文件，不改变文件内容。", "editor font size 字号", limits=(10, 28), suffix=" pt"),
    SettingSpec("editor_tab_size", "编辑器", "Tab 显示宽度", "只调整缩进的显示宽度，不替换文件中的空格或 Tab。", "editor tab size indentation", limits=(2, 8), suffix=" 空格"),
    SettingSpec("editor_wrap", "编辑器", "自动换行", "按编辑区宽度折行，不插入真实换行符。", "editor word wrap", (("关闭", False), ("开启", True))),
    SettingSpec("line_numbers", "编辑器", "显示行号", "关闭后释放左侧行号空间；欢迎页始终不显示行号。", "editor line numbers", (("显示", True), ("隐藏", False))),
    SettingSpec("highlight_line", "编辑器", "当前行高亮", "显示或隐藏光标所在行的背景，不影响语法颜色。", "editor render line highlight", (("开启", True), ("关闭", False))),
)


class SettingsPage(QWidget):
    # Below this page width the category list becomes a dropdown. Tests and the
    # responsive contract read this constant instead of duplicating the number.
    COMPACT_BREAKPOINT = 840
    value_changed = Signal(str, object)
    reset_requested = Signal()
    reset_layout_requested = Signal()
    close_requested = Signal()
    agent_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PreferencesPage")
        self.setMinimumWidth(0)
        self.preferences = AppearancePreferences()
        self.profile = "studio"
        self.controls = {}
        self.rows = {}
        self._compact = None
        self.agent_settings_panel = None
        self.agent_modified = None
        root = QVBoxLayout(self)
        self.root_layout = root
        root.setContentsMargins(14, 10, 14, 9)
        root.setSpacing(8)
        header = QHBoxLayout()
        title = QLabel("设置")
        title.setObjectName("PreferencesTitle")
        self.title_label = title
        header.addWidget(title)
        header.addStretch(1)
        back = QPushButton("返回工作区")
        back.setObjectName("Quiet")
        back.clicked.connect(self.close_requested)
        header.addWidget(back)
        root.addLayout(header)
        self.search = QLineEdit()
        self.search.setObjectName("PreferencesSearch")
        self.search.setPlaceholderText("搜索设置，例如：字体、侧栏、editor font")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._search_changed)
        root.addWidget(self.search)
        filters = QHBoxLayout()
        self.modified_only = QCheckBox("只看已修改")
        self.modified_only.toggled.connect(self._filter_rows)
        filters.addWidget(self.modified_only)
        filters.addStretch(1)
        self.reset_button = QPushButton("恢复默认外观")
        self.reset_button.setObjectName("Quiet")
        self.reset_button.clicked.connect(self.reset_requested)
        filters.addWidget(self.reset_button)
        root.addLayout(filters)
        self.category_combo = QComboBox()
        self.category_combo.addItems(CATEGORIES)
        self.category_combo.currentIndexChanged.connect(self._category_changed)
        root.addWidget(self.category_combo)
        content = QHBoxLayout()
        content.setSpacing(16)
        self.categories = QListWidget()
        self.categories.setObjectName("PreferencesCategories")
        self.categories.addItems(CATEGORIES)
        self.categories.setFixedWidth(150)
        self.categories.currentRowChanged.connect(self._category_changed)
        content.addWidget(self.categories)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("PreferencesScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        surface = QWidget()
        surface.setObjectName("PreferencesSurface")
        column = QVBoxLayout(surface)
        column.setContentsMargins(0, 0, 6, 0)
        column.setSpacing(8)
        families = [family for family in QFontDatabase.families() if family.strip()]
        for spec in SPECS:
            card = QFrame()
            card.setObjectName("PreferenceCard")
            card.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            body = QVBoxLayout(card)
            body.setContentsMargins(12, 8, 12, 8)
            heading = QHBoxLayout()
            name = QLabel(f"{spec.category} › {spec.title}")
            name.setObjectName("PreferenceName")
            name.setWordWrap(True)
            heading.addWidget(name, 1)
            reset = QPushButton("重置")
            reset.setObjectName("Quiet")
            reset.setToolTip(f"恢复 {spec.title} 的默认值")
            reset.clicked.connect(lambda _checked=False, key=spec.key: self._reset_one(key))
            heading.addWidget(reset)
            body.addLayout(heading)
            description = QLabel(spec.description)
            description.setObjectName("PreferenceDescription")
            description.setWordWrap(True)
            description.setMinimumWidth(0)
            body.addWidget(description)
            if spec.limits:
                control = QSpinBox()
                control.setRange(*spec.limits)
                control.setSuffix(spec.suffix)
                control.valueChanged.connect(lambda value, key=spec.key: self.value_changed.emit(key, value))
            elif spec.choices or spec.key in ("font_family", "editor_font_family"):
                control = QComboBox()
                choices = spec.choices
                if spec.key == "font_family":
                    choices = (("自动 · 中文无衬线", ""),) + tuple((f, f) for f in families)
                elif spec.key == "editor_font_family":
                    choices = tuple((f, f) for f in families if QFontDatabase.isFixedPitch(f))
                for label, value in choices:
                    control.addItem(label, value)
                control.setMinimumContentsLength(6)
                control.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                control.currentIndexChanged.connect(lambda _index, key=spec.key, box=control: self.value_changed.emit(key, box.currentData()))
            else:
                control = QLineEdit()
                control.setPlaceholderText("跟随主题，例如 #3278df")
                control.editingFinished.connect(self._commit_accent)
                control.textChanged.connect(lambda _text: self._show_accent_validity(True))
            control.setObjectName(f"Preference_{spec.key}")
            control.setAccessibleName(spec.title)
            control.setToolTip(spec.description)
            control.setMinimumWidth(0)
            control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            body.addWidget(control)
            self.controls[spec.key] = control
            self.rows[spec.key] = card
            column.addWidget(card)
        self.content_column = column
        self.layout_card = self._action_card("布局 › 分栏宽度", "分隔线可直接拖动，宽度按主题记忆。", "重置当前主题分栏", self.reset_layout_requested)
        column.addWidget(self.layout_card)
        self.agent_card = self._action_card("Agent › 模型、总结与任务规划", "复用右侧 Agent 设置：总结频率、记忆、协作和验收仍按项目保存。", "打开 Agent 配置", self.agent_requested)
        column.addWidget(self.agent_card)
        self.empty = QLabel("没有匹配的设置，试试其他关键词或取消“只看已修改”。")
        self.empty.setObjectName("Hint")
        self.empty.setWordWrap(True)
        column.addWidget(self.empty)
        column.addStretch(1)
        self.scroll.setWidget(surface)
        content.addWidget(self.scroll, 1)
        root.addLayout(content, 1)
        footer = QLabel("自动保存到本机 · 即时生效 · 不修改项目代码")
        footer.setObjectName("Hint")
        footer.setWordWrap(True)
        self.footer_label = footer
        root.addWidget(footer)
        self.categories.setCurrentRow(0)
        self.sync_values(self.preferences, self.profile)

    def attach_agent_settings(self, panel: QWidget, modified_callback=None) -> None:
        """Place project-scoped Agent controls in the central settings workspace."""
        if self.agent_settings_panel is panel:
            return
        panel.setParent(self.scroll.widget())
        panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.content_column.insertWidget(self.content_column.indexOf(self.empty), panel)
        self.agent_settings_panel = panel
        self.agent_modified = modified_callback
        self.agent_card.hide()
        self._filter_rows()

    def set_category(self, category: str) -> None:
        index = CATEGORIES.index(category) if category in CATEGORIES else 0
        self.search.clear()
        self._category_changed(index)

    def refresh_filter(self) -> None:
        self._filter_rows()

    def _action_card(self, title, description, action, signal):
        card = QFrame()
        card.setObjectName("PreferenceCard")
        card.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        body = QVBoxLayout(card)
        name = QLabel(title)
        name.setObjectName("PreferenceName")
        name.setWordWrap(True)
        body.addWidget(name)
        label = QLabel(description)
        label.setObjectName("PreferenceDescription")
        label.setWordWrap(True)
        body.addWidget(label)
        button = QPushButton(action)
        button.setObjectName("Quiet")
        button.clicked.connect(signal)
        body.addWidget(button)
        return card

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = self.width() < self.COMPACT_BREAKPOINT
        if compact != self._compact:
            self._compact = compact
            self.categories.setVisible(not compact)
            self.category_combo.setVisible(compact)
            self.footer_label.setVisible(not compact)
            self.root_layout.setContentsMargins(10, 6, 10, 6) if compact else self.root_layout.setContentsMargins(14, 10, 14, 9)
            self.root_layout.setSpacing(4 if compact else 8)
            self.setProperty("compact", compact)
            self.style().unpolish(self)
            self.style().polish(self)

    def sync_values(self, preferences: AppearancePreferences, profile: str) -> None:
        self.preferences, self.profile = preferences, profile
        for key, control in self.controls.items():
            value = profile if key == "profile" else getattr(preferences, key)
            blocker = QSignalBlocker(control)
            if isinstance(control, QSpinBox):
                control.setValue(value)
            elif isinstance(control, QComboBox):
                index = control.findData(value)
                if index < 0 and key.endswith("font_family"):
                    control.addItem(f"{value}（不可用，使用回退字体）", value)
                    index = control.count() - 1
                control.setCurrentIndex(index)
            elif control.text() != value:
                control.setText(value)
            del blocker
        self._show_accent_validity(True)
        self._filter_rows()

    def _reset_one(self, key):
        self.value_changed.emit(key, "studio" if key == "profile" else getattr(AppearancePreferences(), key))

    def _commit_accent(self):
        value = self.controls["accent_color"].text().strip()
        valid = not value or bool(re.fullmatch(r"#[0-9a-fA-F]{6}", value))
        self._show_accent_validity(valid)
        if valid:
            self.value_changed.emit("accent_color", value)

    def _show_accent_validity(self, valid):
        field = self.controls["accent_color"]
        field.setProperty("invalid", not valid)
        field.setToolTip("请输入 #RRGGBB，或留空跟随主题" if not valid else SPECS[1].description)
        field.style().unpolish(field)
        field.style().polish(field)

    def _category_changed(self, index):
        if index < 0:
            return
        for control in (self.categories, self.category_combo):
            blocker = QSignalBlocker(control)
            if isinstance(control, QListWidget):
                control.setCurrentRow(index)
            else:
                control.setCurrentIndex(index)
            del blocker
        self._filter_rows()
        self.scroll.verticalScrollBar().setValue(0)

    def _search_changed(self, text):
        if text:
            self._category_changed(0)
        self._filter_rows()

    def _filter_rows(self, *_args):
        category = CATEGORIES[max(0, self.categories.currentRow())]
        words = self.search.text().lower().split()
        count = 0
        defaults = AppearancePreferences()
        for spec in SPECS:
            haystack = f"{spec.title} {spec.category} {spec.description} {spec.search_key} {spec.key}".lower()
            value = self.profile if spec.key == "profile" else getattr(self.preferences, spec.key)
            default = "studio" if spec.key == "profile" else getattr(defaults, spec.key)
            visible = (category in ("全部", spec.category) and all(word in haystack for word in words)
                       and (not self.modified_only.isChecked() or value != default))
            self.rows[spec.key].setVisible(visible)
            count += visible
        for card, group, keywords in (
            (self.layout_card, "布局", "布局 分栏 宽度 layout splitter"),
            (self.agent_card, "Agent", "agent 模型 总结 规划 记忆 协作 验收 llm"),
        ):
            visible = (category in ("全部", group)
                       and all(word in keywords.lower() for word in words)
                       and not self.modified_only.isChecked()
                       and not (card is self.agent_card and self.agent_settings_panel is not None))
            card.setVisible(visible)
            count += visible
        panel = self.agent_settings_panel
        if panel is not None:
            texts = [
                widget.text() for widget in panel.findChildren(QLabel)
                if widget.text()
            ]
            texts.extend(
                widget.text() for widget in panel.findChildren(QCheckBox)
                if widget.text()
            )
            texts.extend(
                widget.placeholderText() for widget in panel.findChildren(QLineEdit)
                if widget.placeholderText()
            )
            agent_haystack = ("agent model summary checkpoint planning memory context acceptance "
                              "multi-agent instruction token retries llm 自动总结 每几轮 轮次 指令 "
                              "项目上下文 接受模式 多智能体 网络重试 " + " ".join(texts)).lower()
            agent_visible = (
                category in ("全部", "Agent")
                and all(word in agent_haystack for word in words)
                and (not self.modified_only.isChecked()
                     or bool(self.agent_modified and self.agent_modified()))
            )
            panel.setVisible(agent_visible)
            count += agent_visible
        self.empty.setVisible(count == 0)
