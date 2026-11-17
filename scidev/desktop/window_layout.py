"""Screen scaling, appearance preferences and responsive window layout.

The host retains state and lifecycle ownership; this module does not import its entry point.
"""

from __future__ import annotations

from functools import partial
from typing import Any
from PySide6.QtCore import QObject
from PySide6.QtCore import QRect
from PySide6.QtCore import QSize
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QLabel
from scidev.runtime.preferences import AppearancePreferences
from scidev.desktop.icons import activity_icon
from scidev.desktop.editor import CodeEditor as CodeEditor
from scidev.desktop.theme import DEFAULT_UI_PROFILE
from scidev.desktop.theme import THEME as THEME
from scidev.desktop.theme import UI_PROFILES
from scidev.desktop.theme import create_ui_font
from scidev.desktop.theme import stylesheet_for_profile
from scidev.desktop.theme import ui_profile_for_key
from scidev.desktop.theme import ui_font_family
from scidev.desktop.theme import ui_scale_for_resolution


class WindowLayoutMixin:
    """Operate on host state; introduce no initialization or additional state."""

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()


    def set_ui_profile(self, profile_key: str, *, persist: bool = True) -> None:
        """Apply and optionally persist a complete, switchable workbench style."""
        profile = ui_profile_for_key(profile_key)
        changed = profile.key != self.ui_profile
        self.ui_profile = profile.key
        self.setStyleSheet(stylesheet_for_profile(THEME, profile, self._ui_scale, self.appearance_preferences))

        if persist:
            self.ui_settings.setValue("uiProfile", profile.key)
            self.ui_settings.sync()

        for editor in self.findChildren(CodeEditor):
            editor.set_ui_profile(profile.key)
        unique_highlighters = {
            id(highlighter): highlighter
            for highlighter in self._editor_highlighters.values()
        }
        for highlighter in unique_highlighters.values():
            highlighter.set_ui_profile(profile.key)
        if hasattr(self, "git_tree"):
            self.git_tree.set_visual_theme(profile.key)
            selected = next((node for node in self.git_tree.nodes if str(node.get("id")) == self.git_tree.selected_id), None)
            if selected is not None:
                self._handle_git_node_selected(selected)

        if hasattr(self, "ui_theme_actions"):
            for key, action in self.ui_theme_actions.items():
                action.setChecked(key == profile.key)
        if hasattr(self, "ui_theme_button"):
            self.ui_theme_button.setToolTip(f"界面方案：{profile.label}\n{profile.description}")
            self.ui_theme_button.setAccessibleDescription(profile.description)
        for button, icon_name in zip(
            getattr(self, "activity_buttons", ()),
            ("explorer", "search", "git"),
        ):
            button.setIcon(activity_icon(icon_name, profile.activity_icon_colors[0], self.appearance_preferences.accent_color or profile.activity_icon_colors[1]))
        if hasattr(self, "settings_button"):
            self.settings_button.setIcon(activity_icon("settings", profile.activity_icon_colors[0], self.appearance_preferences.accent_color or profile.activity_icon_colors[1]))
        if hasattr(self, "settings_page"):
            self.settings_page.sync_values(self.appearance_preferences, self.ui_profile)

        if changed:
            self._workbench_user_ratios = self._saved_workbench_ratios(profile.key)
            if hasattr(self, "workbench"):
                QTimer.singleShot(0, self._adapt_workbench_layout)

        if persist:
            app = QApplication.instance()
            sibling_windows = getattr(app, "_scidev_windows", []) if app is not None else ()
            for sibling in sibling_windows:
                if sibling is not self:
                    sibling.set_ui_profile(profile.key, persist=False)


    def showEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and handle is not self._screen_window_handle:
            self._screen_window_handle = handle
            screen_changed = getattr(handle, "screenChanged", None)
            if screen_changed is not None:
                screen_changed.connect(self._on_screen_changed)
        screen = self.screen()
        if screen is None and handle is not None:
            handle_screen = getattr(handle, "screen", None)
            if callable(handle_screen):
                screen = handle_screen()
        self._on_screen_changed(screen)


    def _on_screen_changed(self, screen) -> None:  # noqa: ANN001 - QScreen is binding-defined.
        if screen is None:
            return
        self._bind_screen_metric_signals(screen)
        geometry = screen.geometry()
        previous_scale = self._ui_scale
        target_scale = self._resolved_ui_scale(geometry.width(), geometry.height())
        self._apply_screen_resolution(geometry.width(), geometry.height())
        if not self._offscreen_platform:
            self._fit_window_to_screen(screen, previous_scale, target_scale)


    def _bind_screen_metric_signals(self, screen) -> None:  # noqa: ANN001 - QScreen is binding-defined.
        if screen is self._screen_metric_signal_screen:
            return
        for connection in self._screen_metric_connections:
            QObject.disconnect(connection)
        self._screen_metric_connections.clear()
        self._screen_metric_signal_screen = screen

        update_screen = partial(self._on_screen_metrics_changed, screen)
        for signal_name in ("geometryChanged", "availableGeometryChanged"):
            signal = getattr(screen, signal_name, None)
            if signal is not None:
                self._screen_metric_connections.append(signal.connect(update_screen))


    def _on_screen_metrics_changed(self, screen, *_args: object) -> None:
        if screen is self._screen_metric_signal_screen:
            self._on_screen_changed(screen)


    def _fit_window_to_screen(self, screen, previous_scale: float, target_scale: float) -> None:
        """Keep a normal window visible and proportionate after a monitor change."""
        if self.isMaximized() or self.isFullScreen():
            return

        available = screen.availableGeometry()
        available_width = max(1, available.width())
        available_height = max(1, available.height())
        minimum_width = min(self._base_minimum_size.width(), available_width)
        minimum_height = min(self._base_minimum_size.height(), available_height)
        self.setMinimumSize(minimum_width, minimum_height)

        ratio = target_scale / previous_scale if previous_scale > 0 else 1.0
        current = self.geometry()
        width = min(available_width, max(minimum_width, round(current.width() * ratio)))
        height = min(available_height, max(minimum_height, round(current.height() * ratio)))
        left = available.x()
        top = available.y()
        x = min(max(current.x(), left), left + available_width - width)
        y = min(max(current.y(), top), top + available_height - height)
        self.setGeometry(QRect(x, y, width, height))


    def _apply_screen_resolution(self, width: int, height: int) -> None:
        self._screen_dimensions = (width, height)
        scale = self._resolved_ui_scale(width, height)
        self._ui_scale = scale
        self.setStyleSheet(stylesheet_for_profile(THEME, self.ui_profile, scale, self.appearance_preferences))
        scaled_font = QFont(self._base_ui_font)
        if scaled_font.pointSizeF() > 0:
            scaled_font.setPointSizeF(scaled_font.pointSizeF() * scale)
            self.setFont(scaled_font)
        for editor in self.findChildren(CodeEditor):
            editor.set_appearance(self.appearance_preferences)
            editor.set_ui_scale(scale)
        if hasattr(self, "git_tree"):
            self.git_tree.set_ui_scale(scale * self.appearance_preferences.font_size / 11)
            root = self.git_tree.rootObject()
            if root is not None:
                root.setProperty("uiFontFamily", ui_font_family(preferred=self.appearance_preferences.font_family))
        self._update_activity_rail_metrics()
        self._refresh_appearance_geometry()
        if hasattr(self, "workbench"):
            self._adapt_workbench_layout()
        self.updateGeometry()


    def _resolved_ui_scale(self, width: int, height: int) -> float:
        zoom = self.appearance_preferences.zoom
        return zoom / 100 if zoom else ui_scale_for_resolution(width, height)


    def _on_appearance_setting_changed(self, key: str, value: Any) -> None:
        if key == "profile":
            self.set_ui_profile(value)
            self._refresh_appearance_geometry()
            return
        preferences = self.appearance_preferences.with_value(key, value)
        self._set_appearance_preferences(preferences)


    def _set_appearance_preferences(self, preferences: AppearancePreferences, *, persist: bool = True) -> None:
        previous_mode = self.appearance_preferences.explorer_mode
        self.appearance_preferences = preferences
        if persist:
            preferences.save(self.ui_settings)
        self._base_ui_font = create_ui_font(preferences.font_size, preferences.font_family, preferences.font_weight)
        if preferences.explorer_mode != previous_mode:
            self._explorer_visibility_override = {"show": True, "hide": False}.get(preferences.explorer_mode)
        self._apply_screen_resolution(*self._screen_dimensions)
        self.set_ui_profile(self.ui_profile, persist=False)
        if hasattr(self, "settings_page"):
            self.settings_page.sync_values(preferences, self.ui_profile)
        if persist:
            for sibling in getattr(QApplication.instance(), "_scidev_windows", ()):
                if sibling is not self:
                    sibling._set_appearance_preferences(preferences, persist=False)


    def _refresh_appearance_geometry(self) -> None:
        if hasattr(self, "title_bar"):
            self._adapt_title_bar_layout()
            self.title_brand.setVisible(not getattr(self, "_titlebar_compact", False))
            self.ui_theme_button.setFixedHeight(max(28, self.ui_theme_button.fontMetrics().height() + 8))
            self.title_bar.setFixedHeight(max(42, self.title_bar.layout().sizeHint().height()))
        if hasattr(self, "chat_header"):
            self.chat_header.setFixedHeight(max(58, self.agent_title.fontMetrics().lineSpacing() + self.chat_status.fontMetrics().lineSpacing() + 12))
        if hasattr(self, "workspace_toolbar"):
            self.workspace_toolbar.setFixedHeight(max(40, self.workspace_toolbar.layout().sizeHint().height()))
        if hasattr(self, "welcome_content"):
            QTimer.singleShot(0, self._update_welcome_text_sizes)


    def _reset_appearance(self) -> None:
        self._set_appearance_preferences(AppearancePreferences())
        self.set_ui_profile(DEFAULT_UI_PROFILE)


    def _reset_workbench_layout(self) -> None:
        self.ui_settings.remove(f"workbenchRatios/v2/{self.ui_profile}")
        self.ui_settings.remove(f"workbenchRatios/{self.ui_profile}")
        self.ui_settings.sync()
        self._workbench_user_ratios = UI_PROFILES[self.ui_profile].layout_ratios
        self._adapt_workbench_layout()


    def show_settings(self) -> None:
        if self.workspace_stack.currentWidget() is not self.settings_page:
            self._settings_return_was_git = self._git_focus
            self.show_workspace()
            self.workspace_stack.setCurrentWidget(self.settings_page)
        self.settings_page.sync_values(self.appearance_preferences, self.ui_profile)
        self.settings_page.search.setFocus()


    def _close_settings(self) -> None:
        if getattr(self, "_settings_return_was_git", False):
            self.show_git()
        else:
            self.show_workspace()


    def _open_agent_settings_from_preferences(self) -> None:
        self.show_settings()
        self.settings_page.set_category("Agent")
        self.summary_enabled_checkbox.setFocus()


    def _activity_rail_width(self) -> int:
        return round(self.appearance_preferences.rail_width * self._ui_scale)


    def _update_activity_rail_metrics(self) -> None:
        rail = getattr(self, "activity_rail", None)
        if rail is None:
            return
        rail.setFixedWidth(self._activity_rail_width())
        rail.layout().setSpacing(round((4 if self.appearance_preferences.density == "compact" else 8) * self._ui_scale))
        target = round(self.appearance_preferences.button_size * self._ui_scale)
        icon = round(self.appearance_preferences.effective_icon_size * self._ui_scale)
        for button in self.activity_buttons + [self.settings_button]:
            button.setFixedSize(target, target)
            button.setIconSize(QSize(icon, icon))


    def _update_welcome_text_sizes(self) -> None:
        if self._closing:
            return
        # A centered QFrame inside a scroll area otherwise takes its vertical
        # minimum before Qt has accounted for the labels' wrapped text.
        for label in self._welcome_text_labels:
            wrapped = isinstance(label, QLabel) and label.wordWrap()
            required = max(
                label.fontMetrics().lineSpacing(),
                label.heightForWidth(max(1, label.width())) if wrapped else 0,
            )
            if label.minimumHeight() != required:
                label.setMinimumHeight(required)
        compact = self.height() < 600
        if compact != self._welcome_compact:
            self._welcome_compact = compact
            outer = self.welcome_outer_layout
            for index in reversed(range(outer.count())):
                if outer.itemAt(index).widget() is None:
                    outer.takeAt(index)
            if compact:
                outer.setContentsMargins(12, 8, 12, 8)
                outer.insertSpacing(0, 4)
                self.welcome_content.layout().setContentsMargins(16, 12, 16, 12)
                self.welcome_content.layout().setSpacing(8)
                self.welcome_description.setText("打开文件开始编码。Agent 可直接读取和修改项目代码。")
            else:
                outer.setContentsMargins(24, 18, 24, 18)
                outer.insertStretch(0, 1)
                outer.addStretch(1)
                self.welcome_content.layout().setContentsMargins(20, 18, 20, 18)
                self.welcome_content.layout().setSpacing(14)
                self.welcome_description.setText(
                    "打开文件，专注编码。\n右侧 Agent 能读取项目、修改代码并保留开发过程。"
                )
            self.welcome_content.setProperty("compact", compact)
            self.welcome_content.style().unpolish(self.welcome_content)
            self.welcome_content.style().polish(self.welcome_content)
            if self.welcome_sample_button is not None:
                self.welcome_sample_button.setVisible(not compact)
            QTimer.singleShot(0, self._update_welcome_text_sizes)


    def _on_workbench_splitter_moved(self, _position: int, _index: int) -> None:
        settings_focus = self.workspace_stack.currentWidget() is getattr(self, "settings_page", None)
        if not self._workbench_adapting and not self._git_focus and not settings_focus:
            sizes = self.workbench.sizes()
            total = sum(sizes)
            if total > 0 and len(sizes) == 3:
                if self.explorer.isHidden():
                    left_ratio = (
                        self._workbench_user_ratios[0]
                        if self._workbench_user_ratios is not None
                        else UI_PROFILES[self.ui_profile].layout_ratios[0]
                    )
                else:
                    left_ratio = sizes[0] / total
                self._workbench_user_ratios = (left_ratio, sizes[2] / total)
                self.ui_settings.setValue(f"workbenchRatios/v2/{self.ui_profile}", list(self._workbench_user_ratios))
                # Keep the legacy key readable by older packaged clients.
                self.ui_settings.setValue(f"workbenchRatios/{self.ui_profile}", list(self._workbench_user_ratios))
                self.ui_settings.sync()


    def _saved_workbench_ratios(self, profile_key: str) -> tuple[float, float]:
        value = self.ui_settings.value(f"workbenchRatios/v2/{profile_key}")
        try:
            left, chat = map(float, value)
            if 0.10 <= left <= 0.40 and 0.10 <= chat <= 0.62 and left + chat < 0.85:
                return left, chat
        except (ValueError, TypeError):
            pass
        return UI_PROFILES[profile_key].layout_ratios


    def _workbench_minimum_widths(self) -> tuple[int, int, int]:
        minimums = (
            self.WORKBENCH_COMPACT_MINIMUMS
            if self.width() < self.WORKBENCH_COMPACT_BREAKPOINT
            else self.WORKBENCH_WIDE_MINIMUMS
        )
        if hasattr(self, "explorer") and self.explorer.isHidden():
            return (self._activity_rail_width(), minimums[1], minimums[2])
        return minimums


    def _apply_responsive_explorer_visibility(self) -> bool:
        if not hasattr(self, "explorer"):
            return True
        should_show = self._explorer_visibility_override
        if should_show is None:
            should_show = self.width() >= self.EXPLORER_COLLAPSE_BREAKPOINT
        settings_focus = self.workspace_stack.currentWidget() is getattr(self, "settings_page", None)
        if self._git_focus or settings_focus:
            should_show = False
        if (not self.explorer.isHidden()) != should_show:
            self.explorer.setVisible(should_show)
        if hasattr(self, "left_panel"):
            if should_show:
                minimum = (
                    self.WORKBENCH_COMPACT_MINIMUMS[0]
                    if self.width() < self.WORKBENCH_COMPACT_BREAKPOINT
                    else self.WORKBENCH_WIDE_MINIMUMS[0]
                )
            else:
                minimum = self._activity_rail_width()
            self.left_panel.setMinimumWidth(minimum)
        if getattr(self, "activity_buttons", None):
            label = "隐藏资源管理器" if should_show else "显示资源管理器"
            self.activity_buttons[0].setToolTip(label)
            self.activity_buttons[0].setAccessibleName(label)
            self.activity_buttons[0].setAccessibleDescription(label)
        return should_show


    def _adapt_workbench_layout(self) -> None:
        self._workbench_adapt_pending = False
        if not hasattr(self, "workbench"):
            return
        settings_focus = self.workspace_stack.currentWidget() is getattr(self, "settings_page", None)
        explorer_visible = self._apply_responsive_explorer_visibility()
        self.chat_panel.setVisible(not settings_focus and (not self._git_focus or self._git_chat_visible))
        total = self.workbench.width() - self.workbench.handleWidth() * 2
        if total <= 0:
            return
        if self._workbench_user_ratios is None:
            left = int(total * 0.20)
            chat = int(total * 0.50)
        else:
            left_ratio, chat_ratio = self._workbench_user_ratios
            left = int(total * left_ratio)
            chat = int(total * chat_ratio)
        left_minimum, center_minimum, chat_minimum = self._workbench_minimum_widths()
        if explorer_visible:
            left = min(440, max(left_minimum, left))
        else:
            left = self._activity_rail_width()
        chat_maximum = max(chat_minimum, round(total * 0.62))
        chat = min(chat_maximum, max(chat_minimum, chat)) if self.chat_panel.isVisible() else 0
        editor_floor = 0
        if self.width() < self.WORKBENCH_COMPACT_BREAKPOINT and self.chat_panel.isVisible():
            editor_floor = min(
                self.WORKBENCH_COMPACT_EDITOR_MINIMUM,
                max(center_minimum, total - left - chat_minimum),
            )
            chat = min(chat, max(chat_minimum, total - left - editor_floor))
        center = total - left - chat
        if center < center_minimum:
            deficit = center_minimum - center
            chat_reduction = min(deficit, max(0, chat - chat_minimum))
            chat -= chat_reduction
            deficit -= chat_reduction
            if explorer_visible:
                left -= min(deficit, max(0, left - left_minimum))
            center = total - left - chat
        self._workbench_adapting = True
        try:
            self.workbench.setSizes([left, max(1, center), chat])
            # QSplitter rounds its handle budget, so verify the editor really
            # received its floor and take the difference from the Agent pane.
            actual = self.workbench.sizes()
            editor_minimum = max(center_minimum, editor_floor)
            if (
                len(actual) == 3
                and 0 < actual[1] < editor_minimum
                and actual[2] - (editor_minimum - actual[1]) >= chat_minimum
            ):
                shortfall = editor_minimum - actual[1]
                self.workbench.setSizes(
                    [actual[0], actual[1] + shortfall, actual[2] - shortfall]
                )
        finally:
            self._workbench_adapting = False
        self._adapt_explorer_density()
        self._adapt_chat_composer_density()
        self._schedule_visible_filtered_branch_expansion()


    def _adapt_explorer_density(self) -> None:
        git_entry = getattr(self, "git_entry_button", None)
        if git_entry is not None:
            git_entry.setText("版本树")
        explorer = getattr(self, "explorer", None)
        heading = getattr(self, "explorer_heading_label", None)
        actions = getattr(self, "explorer_header_actions", ())
        if explorer is None or heading is None:
            return
        available = explorer.width()
        full_title_width = heading.fontMetrics().horizontalAdvance("资源管理器")
        action_width = sum(button.sizeHint().width() for button in actions)
        margins = explorer.layout().contentsMargins()
        full_heading_width = full_title_width + action_width + margins.left() + margins.right() + 12
        if len(actions) > 1:
            actions[1].setVisible(available >= full_heading_width)
        compact_title_width = heading.fontMetrics().horizontalAdvance("资源")
        primary_width = actions[0].sizeHint().width() if actions else 0
        if available < compact_title_width + primary_width + margins.left() + margins.right() + 12:
            heading.setText("资源")
        else:
            heading.setText("资源管理器")
        heading.setToolTip("资源管理器")


    def _adapt_chat_composer_density(self) -> None:
        composer_layout = getattr(self, "chat_composer_layout", None)
        composer_top = getattr(self, "chat_composer_top", None)
        if composer_layout is None or composer_top is None:
            return
        compact = self._is_chat_compact()
        short_window = self.height() < 600
        horizontal_margin = 8 if compact else 12
        composer_layout.setContentsMargins(horizontal_margin, 6 if short_window else 10,
                                           horizontal_margin, 6 if short_window else 10)
        composer_layout.setSpacing(4 if short_window else 7)
        self.chat_input.setFixedHeight(62 if short_window else 76)
        if hasattr(self, "summary_chip"):
            self.summary_chip.setText(self._summary_chip_text())
            self.summary_chip.setToolTip(self._summary_status_text())
            self.summary_chip.setAccessibleName(self._summary_status_text())
        if hasattr(self, "usage_chip"):
            self._display_model_usage({"usage": self.model_usage, "record": None})
        if hasattr(self, "context_usage_bar"):
            self.context_usage_bar.setVisible(not compact)
            self._render_context_meter()
        if hasattr(self, "model_selector"):
            self.model_selector.setMaximumWidth(160 if compact else 240)
        self._reflow_composer_meta(compact)
        self._adapt_summary_settings_layout()
        if hasattr(self, "task_plan_panel"):
            self.task_plan_panel.update_viewport_height()


    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt event signature.
        super().resizeEvent(event)
        if hasattr(self, "command_search"):
            self._adapt_title_bar_layout()
            self._refresh_appearance_geometry()
        if hasattr(self, "workbench") and not self._workbench_adapt_pending:
            self._workbench_adapt_pending = True
            QTimer.singleShot(0, self._adapt_workbench_layout)
        if hasattr(self, "git_tree_splitter"):
            QTimer.singleShot(0, self._adapt_git_layout)
        if self._active_notification is not None:
            QTimer.singleShot(0, self._position_notification)


    def _adapt_title_bar_layout(self) -> None:
        compact = self.width() < 1120
        compact_changed = self._titlebar_compact != compact
        self._titlebar_compact = compact
        scale = self._ui_scale
        self.title_brand.setVisible(not compact)
        self.title_project_label.setVisible(True)
        self.title_project_label.setMinimumWidth(round((64 if compact else 96) * scale))
        self.title_project_label.setMaximumWidth(round((160 if compact else 380) * scale))
        self.command_search.setMinimumWidth(round((110 if compact else 190) * scale))
        self.command_search.setMaximumWidth(round((820 if compact else 1500) * scale))
        self.connection_label.setMaximumWidth(round((112 if compact else 220) * scale))
        self.title_new_button.setMaximumWidth(round((96 if compact else 148) * scale))
        self.title_git_button.setMaximumWidth(round((96 if compact else 132) * scale))
        self.title_refresh_button.setMaximumWidth(round((58 if compact else 88) * scale))
        self.ui_theme_button.setMaximumWidth(round(104 * scale))
        if compact_changed:
            self.command_search.setPlaceholderText("Ctrl+P 搜索或跳转" if compact else "搜索文件、命令或跳转到…")
            self.title_new_button.setText("新建" if compact else "新会话")
            self.title_refresh_button.setText("↻" if compact else "刷新")
        self.ui_theme_button.setText("主题")
        self.ui_theme_button.setMinimumWidth(round(50 * scale))
        self.title_new_button.setToolTip("新建编码会话")
        self.title_refresh_button.setToolTip("刷新项目、任务与 Git 状态")
        self.title_git_button.setToolTip("打开开发版本树")
