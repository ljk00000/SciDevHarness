"""Render representative IDE layouts for visual regression review."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
if os.name == "nt":
    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if windows_fonts.is_dir():
        os.environ["QT_QPA_FONTDIR"] = str(windows_fonts)

from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QImage, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QSizePolicy

from scidev_client import ClientWindow, DevelopmentTreeView
from scidev_ui import UI_PROFILES


def capture(
    window: ClientWindow,
    app: QApplication,
    output_dir: Path,
    name: str,
    size: tuple[int, int],
    *,
    physical_size: bool = False,
) -> QImage:
    scale = float(window.devicePixelRatioF()) if physical_size else 1.0
    logical_size = (round(size[0] / scale), round(size[1] / scale))
    window.resize(*logical_size)
    window.show()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        app.processEvents()
        if window.git_tree.status() == QQuickWidget.Status.Ready:
            break
        time.sleep(0.01)

    if window.git_tree.status() != QQuickWidget.Status.Ready:
        errors = "; ".join(error.toString() for error in window.git_tree.errors())
        raise RuntimeError(f"version-tree QML did not become ready: {errors or 'timeout'}")

    # QQuickWidget may accept a root-property update before its scene graph
    # has painted the corresponding frame (visible at non-100% DPI). Let a
    # few render/event-loop turns pass so the screenshot reflects the UI state
    # that the geometry assertions inspect.
    for _ in range(3):
        app.processEvents()
        time.sleep(0.016)
    image = window.grab().toImage()
    if physical_size:
        wrong_size = abs(image.width() - size[0]) > 8 or abs(image.height() - size[1]) > 8
    else:
        wrong_size = image.width() < size[0] - 8 or image.height() < size[1] - 8
    if image.isNull() or wrong_size:
        raise RuntimeError(f"unexpected screenshot size for {name}: {image.width()}x{image.height()}")
    destination = output_dir / f"{name}.png"
    if not image.save(str(destination), "PNG"):
        raise RuntimeError(f"could not save screenshot: {destination}")
    print(f"{destination} ({image.width()}x{image.height()})")
    return image


def verify_screenshot_content(window: ClientWindow, name: str) -> None:
    image = window.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)
    grid_columns = min(256, max(48, image.width() // 10))
    grid_rows = min(180, max(32, image.height() // 10))
    step_x = max(1, image.width() // grid_columns)
    step_y = max(1, image.height() // grid_rows)
    colors = {
        image.pixelColor(x, y).rgba()
        for x in range(0, image.width(), step_x)
        for y in range(0, image.height(), step_y)
    }
    if len(colors) < 24:
        raise RuntimeError(f"screenshot looks blank or incomplete for {name}: only {len(colors)} sampled colors")


def verify_title_bar_layout(window: ClientWindow, name: str) -> None:
    title_bar = window.centralWidget().layout().itemAt(0).widget()
    if title_bar is None:
        raise RuntimeError(f"window title bar is missing for {name}")
    layout = title_bar.layout()
    visible_widgets = [
        item.widget()
        for index in range(layout.count())
        if (item := layout.itemAt(index)).widget() is not None and item.widget().isVisible()
    ]
    rectangles = [widget.geometry() for widget in visible_widgets]
    if any(
        rect.left() < 0
        or rect.top() < 0
        or rect.right() >= title_bar.width()
        or rect.bottom() >= title_bar.height()
        for rect in rectangles
    ):
        raise RuntimeError(
            f"title-bar control is outside the visible window for {name}: "
            f"{[(widget.objectName(), widget.geometry()) for widget in visible_widgets]} "
            f"within {title_bar.size()}"
        )
    if any(left.intersects(right) for index, left in enumerate(rectangles) for right in rectangles[index + 1 :]):
        raise RuntimeError(f"title-bar controls overlap for {name}: {rectangles}")
    minimum_search_width = 120 if window.width() < 1120 else 190
    if window.command_search.width() < minimum_search_width:
        raise RuntimeError(
            f"command search is clipped for {name}: {window.command_search.width()}px "
            f"< {minimum_search_width}px"
        )


def verify_attempt_accent_pixel(
    window: ClientWindow,
    image: QImage,
    name: str,
    node_id: str,
) -> None:
    node = next((item for item in window.git_tree.nodes if str(item.get("id")) == node_id), None)
    card = window.git_tree._positions()[0].get(node_id)
    if node is None or card is None:
        raise RuntimeError(f"cannot locate attempt node {node_id!r} for screenshot check {name}")
    root = window.git_tree.rootObject()
    zoom = float(root.property("zoom"))
    pan_x = float(root.property("panX"))
    pan_y = float(root.property("panY"))
    origin = window.git_tree.mapTo(window, QPoint(0, 0))
    device_scale = image.devicePixelRatio()
    logical_x = origin.x() + pan_x + (card[2].left() + 2.0) * zoom
    logical_y = origin.y() + pan_y + (card[2].top() + card[2].height() / 2.0) * zoom
    pixel_x = round(logical_x * device_scale)
    pixel_y = round(logical_y * device_scale)
    if not (0 <= pixel_x < image.width() and 0 <= pixel_y < image.height()):
        raise RuntimeError(f"attempt-node screenshot sample is outside the image for {name}: {(pixel_x, pixel_y)}")

    expected = DevelopmentTreeView._accent(node).toRgb()
    expected_hue = expected.hue()
    closest_hue_difference = 361
    closest_color = None
    for offset_y in range(-5, 6):
        for offset_x in range(-5, 6):
            sample_x = pixel_x + offset_x
            sample_y = pixel_y + offset_y
            if not (0 <= sample_x < image.width() and 0 <= sample_y < image.height()):
                continue
            actual = image.pixelColor(sample_x, sample_y).toRgb()
            actual_hue = actual.hue()
            if actual_hue < 0 or actual.saturation() < 64:
                continue
            hue_difference = abs(actual_hue - expected_hue)
            hue_difference = min(hue_difference, 360 - hue_difference)
            if hue_difference < closest_hue_difference:
                closest_hue_difference = hue_difference
                closest_color = actual.name()
    if closest_hue_difference > 14:
        raise RuntimeError(
            f"attempt node accent is not painted at its expected zoomed position for {name}: "
            f"pixel={(pixel_x, pixel_y)} nearest={closest_color} expected={expected.name()} "
            f"hue-delta={closest_hue_difference} zoom={zoom:.3f} dpr={device_scale:.2f}"
        )


def verify_keyboard_focus_ring(window: ClientWindow, image: QImage, name: str) -> None:
    button = window.title_git_button
    if not button.hasFocus():
        raise RuntimeError(f"keyboard-focus target did not receive focus for {name}")

    point = button.mapTo(window, QPoint(button.width() // 2, 1))
    scale = image.devicePixelRatio()
    pixel_x = round(point.x() * scale)
    pixel_y = round(point.y() * scale)
    expected = QColor(UI_PROFILES[window.ui_profile].activity_icon_colors[1]).toRgb()
    closest_difference = 256
    closest_color = None
    for offset_x in range(-2, 3):
        for offset_y in range(-2, 3):
            x = pixel_x + offset_x
            y = pixel_y + offset_y
            if not (0 <= x < image.width() and 0 <= y < image.height()):
                continue
            actual = image.pixelColor(x, y).toRgb()
            difference = max(
                abs(left - right)
                for left, right in zip(actual.getRgb()[:3], expected.getRgb()[:3])
            )
            if difference < closest_difference:
                closest_difference = difference
                closest_color = actual.name()
    if closest_difference > 18:
        raise RuntimeError(
            f"theme focus ring is not painted for {name}: pixel={(pixel_x, pixel_y)} "
            f"nearest={closest_color} expected={expected.name()} delta={closest_difference}"
        )


def verify_workbench_layout(window: ClientWindow, name: str) -> None:
    QApplication.processEvents()
    splitter = window.workbench
    panes = [splitter.widget(index) for index in range(splitter.count())]
    minimum_widths = window._workbench_minimum_widths()
    sizes = splitter.sizes()
    explorer_should_be_visible = window._explorer_visibility_override
    if explorer_should_be_visible is None:
        explorer_should_be_visible = window.width() >= window.EXPLORER_COLLAPSE_BREAKPOINT
    explorer_is_visible = not window.explorer.isHidden()
    if explorer_is_visible != explorer_should_be_visible:
        raise RuntimeError(
            f"responsive explorer visibility is wrong for {name}: "
            f"visible={explorer_is_visible}, expected={explorer_should_be_visible}"
        )
    if len(panes) != 3 or len(sizes) != 3:
        raise RuntimeError(f"unexpected workbench pane count for {name}: {len(panes)}")
    if any(not pane.isVisible() or pane.width() < minimum for pane, minimum in zip(panes, minimum_widths)):
        widths = [pane.width() for pane in panes]
        raise RuntimeError(f"workbench pane collapsed or clipped for {name}: {widths}")
    rectangles = [pane.geometry() for pane in panes]
    if any(left.intersects(right) for left, right in zip(rectangles, rectangles[1:])):
        raise RuntimeError(f"workbench panes overlap for {name}: {rectangles}")
    if not window.chat_input.isVisible() or window.chat_input.width() < 200:
        raise RuntimeError(f"Agent composer is not usable for {name}: {window.chat_input.size()}")
    if explorer_is_visible:
        git_entry_width = window.git_entry_button.contentsRect().width()
        for line in window.git_entry_button.text().splitlines():
            if window.git_entry_button.fontMetrics().horizontalAdvance(line) > git_entry_width:
                raise RuntimeError(
                    f"Explorer development-tree label is clipped for {name}: "
                    f"{line!r} needs more than {git_entry_width}px"
                )
    if not window.git_entry_button.toolTip():
        raise RuntimeError(f"Explorer development-tree entry lost its full description for {name}")
    composer = window.chat_input.parentWidget()
    if composer is None or not composer.rect().contains(window.chat_input.geometry()):
        raise RuntimeError(f"Agent input is clipped inside its composer for {name}")
    for chip in (window.chat_mode_chip, window.retry_chip, window.summary_chip):
        rendered_width = chip.fontMetrics().horizontalAdvance(chip.text())
        available_width = chip.contentsRect().width()
        if rendered_width > available_width:
            raise RuntimeError(
                f"chat composer chip is clipped for {name}: {chip.text()!r} "
                f"needs {rendered_width}px, has {available_width}px"
            )
    summary_status = window._summary_status_text()
    if window.summary_chip.toolTip() != summary_status or window.summary_chip.accessibleName() != summary_status:
        raise RuntimeError(f"compact summary status lost its full accessible description for {name}")
    labels = [window.project_label] if explorer_is_visible else []
    if window.title_project_label.isVisible():
        labels.append(window.title_project_label)
    for label in labels:
        visible_text_width = label.fontMetrics().horizontalAdvance(label.text())
        if visible_text_width > label.contentsRect().width() + 1:
            raise RuntimeError(
                f"workspace name is clipped for {name}: {label.text()!r} exceeds {label.width()}px"
            )
        if visible_text_width < label.fontMetrics().horizontalAdvance(label.full_text) and "…" not in label.text():
            raise RuntimeError(f"workspace name was truncated without an ellipsis for {name}: {label.text()!r}")
    verify_title_bar_layout(window, name)
    verify_status_bar_layout(window, name)
    verify_screenshot_content(window, name)
    print(f"{name} size={window.width()}x{window.height()} workbench widths={sizes}")


def verify_status_bar_layout(window: ClientWindow, name: str) -> None:
    status_bar = window.statusBar()
    labels = [label for label in status_bar.findChildren(QLabel) if label.isVisible()]
    if not labels:
        raise RuntimeError(f"status bar has no visible labels for {name}")
    bounds = status_bar.rect()
    rectangles = [
        QRect(label.mapTo(status_bar, QPoint(0, 0)), label.size())
        for label in labels
    ]
    if any(not bounds.contains(rect) for rect in rectangles):
        raise RuntimeError(
            f"status-bar label is clipped for {name}: "
            f"{[(label.text(), rect) for label, rect in zip(labels, rectangles)]} within {bounds}"
        )
    if any(
        left.intersects(right)
        for index, left in enumerate(rectangles)
        for right in rectangles[index + 1 :]
    ):
        raise RuntimeError(
            f"status-bar labels overlap for {name}: "
            f"{[(label.text(), rect) for label, rect in zip(labels, rectangles)]}"
        )


def capture_unsaved_close_confirmation(
    window: ClientWindow,
    app: QApplication,
    output_dir: Path,
) -> None:
    editor = window._active_editor()
    if editor is None or not hasattr(editor, "document"):
        raise RuntimeError("cannot prepare an editor buffer for the unsaved-close visual check")
    editor.selectAll()
    editor.insertPlainText("unsaved screenshot check\n")
    captured: list[Path] = []
    errors: list[str] = []
    deadline = time.monotonic() + 3

    def capture_and_cancel() -> None:
        active_dialog = app.activeModalWidget()
        dialogs = [active_dialog] if isinstance(active_dialog, QMessageBox) else [
            widget
            for widget in app.topLevelWidgets()
            if isinstance(widget, QMessageBox) and widget.isVisible()
        ]
        if dialogs:
            dialog = dialogs[-1]
            try:
                expected = {"保存全部", "放弃修改", "继续编辑"}
                buttons = {button.text(): button for button in dialog.buttons()}
                missing = expected - buttons.keys()
                if missing:
                    raise RuntimeError(f"unsaved-close dialog is missing buttons: {sorted(missing)}")
                if any(
                    not buttons[text].isVisible()
                    or not dialog.rect().contains(buttons[text].geometry())
                    or buttons[text].width() < buttons[text].sizeHint().width()
                    or buttons[text].height() < buttons[text].sizeHint().height()
                    for text in expected
                ):
                    raise RuntimeError("unsaved-close dialog has hidden or clipped action buttons")
                image = dialog.grab().toImage()
                destination = output_dir / "unsaved-close-confirmation.png"
                if image.isNull() or not image.save(str(destination), "PNG"):
                    raise RuntimeError("could not capture the unsaved-close confirmation dialog")
                captured.append(destination)
            except Exception as exc:  # noqa: BLE001 - always dismiss the temporary modal dialog.
                errors.append(f"{type(exc).__name__}: {exc}")
                print(f"unsaved-close screenshot diagnostic: {type(exc).__name__}: {exc}", flush=True)
            finally:
                dialog.done(0)
            return
        if window.isVisible() and time.monotonic() < deadline:
            QTimer.singleShot(10, capture_and_cancel)
            return
        if isinstance(active_dialog, QMessageBox):
            print("unsaved-dialog: timeout reject", flush=True)
            errors.append("timed out while waiting to capture the unsaved-close dialog")
            active_dialog.reject()

    QTimer.singleShot(0, capture_and_cancel)
    closed = window.close()
    app.processEvents()
    dirty_buffer_survived = editor.document().isModified()
    editor.document().setModified(False)
    if closed:
        raise RuntimeError("closing with a dirty editor did not show a confirmation dialog")
    if not captured:
        raise RuntimeError("the unsaved-close confirmation dialog was not captured")
    if errors:
        raise RuntimeError("unsaved-close confirmation visual check failed: " + "; ".join(errors))
    if not dirty_buffer_survived:
        raise RuntimeError("canceling the unsaved-close confirmation lost the dirty editor state")
    print(f"{captured[0]} (unsaved-close confirmation)")


def verify_git_splitter_layout(window: ClientWindow, name: str, orientation: Qt.Orientation) -> None:
    page = window.git_page
    splitter = window.git_tree_splitter
    root = window.git_tree.rootObject()
    if splitter.orientation() != orientation:
        raise RuntimeError(f"unexpected version-tree splitter orientation for {name}: {splitter.orientation()}")
    page_width = page.contentsRect().width()
    if abs(splitter.width() - page_width) > 2:
        raise RuntimeError(
            f"version-tree content does not fill its page for {name}: "
            f"splitter={splitter.width()} page={page_width}"
        )
    policy = splitter.sizePolicy()
    if (
        policy.horizontalPolicy() != QSizePolicy.Policy.Expanding
        or policy.verticalPolicy() != QSizePolicy.Policy.Expanding
    ):
        raise RuntimeError(
            f"version-tree splitter is not responsive for {name}: "
            f"{policy.horizontalPolicy().name}/{policy.verticalPolicy().name}"
        )
    short_layout = window.height() < 560
    narrow_layout = splitter.width() < 700
    if window.git_metrics_panel.isHidden() != (short_layout or narrow_layout):
        raise RuntimeError(f"version-tree metrics do not match the compact-layout policy for {name}")
    expected_header_height = 56 if short_layout else 68
    if window.git_page_header.height() != expected_header_height:
        raise RuntimeError(
            f"version-tree header has the wrong height for {name}: "
            f"{window.git_page_header.height()}px != {expected_header_height}px"
        )
    expected_compact_canvas_header = (
        float(root.property("width")) < 520 or float(root.property("height")) < 360
    )
    if bool(root.property("compactHeader")) != expected_compact_canvas_header:
        raise RuntimeError(f"version-tree canvas header did not adapt to its viewport for {name}")
    for index in range(splitter.count()):
        child = splitter.widget(index)
        rect = child.geometry()
        if (
            rect.x() < 0
            or rect.y() < 0
            or rect.x() + rect.width() > splitter.width() + 1
            or rect.y() + rect.height() > splitter.height() + 1
        ):
            raise RuntimeError(f"version-tree pane is clipped for {name}: {rect} / {splitter.size()}")
    if orientation == Qt.Orientation.Vertical:
        tree_height, details_height = splitter.sizes()
        minimum_details_height = max(144, int(splitter.height() * 0.36))
        if details_height < minimum_details_height:
            raise RuntimeError(
                f"version-tree details are too short for {name}: "
                f"tree={tree_height}px details={details_height}px; expected at least {minimum_details_height}px"
            )
        if tree_height < int(splitter.height() * 0.50):
            raise RuntimeError(
                f"version-tree canvas is too short for {name}: "
                f"tree={tree_height}px details={details_height}px"
            )
        if window.git_details_scroll.viewport().height() < 136:
            raise RuntimeError(
                f"version-tree detail content has no usable scroll area for {name}: "
                f"{window.git_details_scroll.viewport().size()}"
            )
        viewport = window.git_details_scroll.viewport()
        for control_name, control in (
            ("title", window.git_detail_title),
            ("status", window.git_detail_status),
            ("description", window.git_detail_description),
        ):
            origin = control.mapTo(viewport, QPoint(0, 0))
            control_rect = QRect(origin, control.size())
            if not viewport.rect().contains(control_rect):
                raise RuntimeError(
                    f"selected-node {control_name} is not fully visible for {name}: "
                    f"control={control_rect} viewport={viewport.rect()}"
                )
        print(f"{name} version-tree heights=[{tree_height}, {details_height}]")


def verify_ui_profile(window: ClientWindow, profile_key: str, name: str) -> None:
    if window.ui_profile != profile_key:
        raise RuntimeError(f"wrong appearance profile selected for {name}: {window.ui_profile}")
    if len(window.ui_theme_actions) != 3:
        raise RuntimeError(f"the UI profile menu is missing choices for {name}")
    checked = [key for key, action in window.ui_theme_actions.items() if action.isChecked()]
    if checked != [profile_key]:
        raise RuntimeError(f"UI profile checkmark is inconsistent for {name}: {checked}")
    if not window.ui_theme_button.isVisible() or window.ui_theme_button.accessibleName() != "界面方案":
        raise RuntimeError(f"the responsive UI profile selector is missing for {name}")
    root = window.git_tree.rootObject()
    if root is None or root.property("visualTheme") != profile_key:
        raise RuntimeError(f"the version-tree canvas did not receive profile {profile_key!r} for {name}")


def capture_notification_toast(
    window: ClientWindow,
    app: QApplication,
    output_dir: Path,
    profile_key: str,
    size: tuple[int, int],
) -> None:
    name = f"notification-{profile_key}-{size[0]}x{size[1]}"
    window.set_ui_profile(profile_key, persist=False)
    window.show_workspace()
    window.resize(*size)
    window.show()
    app.processEvents()
    window._show_message("保存失败", "截图回归中的示例提示，不会阻塞代码编辑。")
    for _ in range(3):
        app.processEvents()
        time.sleep(0.016)

    notification = window._active_notification
    root = window.centralWidget()
    if notification is None or not notification.isVisible() or app.activeModalWidget() is not None:
        raise RuntimeError(f"the non-modal notification is not visible for {name}")
    if not root.rect().contains(notification.geometry()):
        raise RuntimeError(
            f"the notification is clipped for {name}: {notification.geometry()} / {root.rect()}"
        )

    image = window.grab().toImage()
    destination = output_dir / f"{name}.png"
    if image.isNull() or not image.save(str(destination), "PNG"):
        raise RuntimeError(f"could not capture the notification toast: {destination}")
    print(f"{destination} ({image.width()}x{image.height()})")
    notification.dismiss()
    app.processEvents()


def capture_ui_profile_menu(window: ClientWindow, app: QApplication, output_dir: Path) -> None:
    menu_position = window.ui_theme_button.mapToGlobal(QPoint(0, window.ui_theme_button.height()))
    window.ui_theme_menu.popup(menu_position)
    app.processEvents()
    if not window.ui_theme_menu.isVisible() or len(window.ui_theme_menu.actions()) != 3:
        raise RuntimeError("the UI profile selector did not expose all three choices")
    image = window.ui_theme_menu.grab().toImage()
    destination = output_dir / "ui-profile-menu.png"
    if image.isNull() or not image.save(str(destination), "PNG"):
        raise RuntimeError("could not capture the UI profile chooser")
    window.ui_theme_menu.close()
    print(f"{destination} ({image.width()}x{image.height()})")


def capture_summary_settings_panel(window: ClientWindow, app: QApplication, output_dir: Path) -> None:
    was_visible = window.summary_settings_panel.isVisible()
    if not was_visible:
        window.toggle_summary_settings()
    try:
        for suffix, size in (
            ("wide", (1500, 920)),
            ("medium", (1120, 700)),
            ("compact", (820, 600)),
        ):
            name = f"summary-settings-{suffix}"
            image = capture(window, app, output_dir, name, size)
            verify_screenshot_content(window, name)
            verify_workbench_layout(window, name)
    finally:
        if not was_visible and window.summary_settings_panel.isVisible():
            window.toggle_summary_settings()


def send_wheel(
    window: ClientWindow,
    modifiers: Qt.KeyboardModifier,
    angle_delta_y: int = -120,
) -> None:
    tree = window.git_tree
    position = QPointF(80, 100)
    global_position = QPointF(tree.mapToGlobal(QPoint(80, 100)))
    event = QWheelEvent(
        position,
        global_position,
        QPoint(0, 0),
        QPoint(0, angle_delta_y),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(tree, event)


def send_mouse_drag(
    tree: DevelopmentTreeView,
    app: QApplication,
    start: QPointF,
    end: QPointF,
) -> None:
    def send_mouse(event_type, position: QPointF, button, buttons) -> None:
        global_position = QPointF(tree.mapToGlobal(position.toPoint()))
        event = QMouseEvent(event_type, position, global_position, button, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(tree, event)
        app.processEvents()

    send_mouse(QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    send_mouse(QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    send_mouse(QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)


def pan_canvas(window: ClientWindow, app: QApplication, output_dir: Path, size: tuple[int, int]) -> None:
    tree = window.git_tree
    root = tree.rootObject()
    original_x = float(root.property("panX"))
    original_y = float(root.property("panY"))
    send_mouse_drag(tree, app, QPointF(90, 460), QPointF(108, 476))
    moved_x = float(root.property("panX"))
    moved_y = float(root.property("panY"))
    if moved_x - original_x < 10 or moved_y - original_y < 10:
        raise RuntimeError(f"dragging the empty canvas did not pan the version tree: {moved_x}, {moved_y}")
    capture(window, app, output_dir, "tree-wide-panned", size)
    root.setProperty("panX", original_x)
    root.setProperty("panY", original_y)
    app.processEvents()


def drag_attempt_node(window: ClientWindow, app: QApplication, output_dir: Path, name: str, size: tuple[int, int]) -> None:
    tree = window.git_tree
    cards, _dots = tree._positions()
    card = cards["fast-warmup"][2]
    start = QPointF(card.center())
    end = start + QPointF(28, 18)

    send_mouse_drag(tree, app, start, end)

    offsets = json.loads(window.git_tree._layout_path.read_text(encoding="utf-8"))
    position = offsets.get("fast-warmup", {})
    if position.get("x", 0) < 20 or position.get("y", 0) < 10:
        raise RuntimeError(f"dragging an attempt node did not persist its new offset: {position}")
    reloaded_tree = DevelopmentTreeView(window.git_tree._layout_path)
    try:
        restored_offset = reloaded_tree._node_offsets.get("fast-warmup")
        if restored_offset is None or restored_offset.x() != position["x"] or restored_offset.y() != position["y"]:
            raise RuntimeError(f"reopening the version tree did not restore the dragged position: {restored_offset}")
    finally:
        reloaded_tree.close()
    cards_after_drag, _dots = tree._positions()
    moved_rect = cards_after_drag["fast-warmup"][2]
    for node_id, (_x, _y, other_rect) in cards_after_drag.items():
        if node_id != "fast-warmup" and moved_rect.intersects(other_rect):
            raise RuntimeError(f"dragged attempt node overlaps {node_id}: {moved_rect} / {other_rect}")
    capture(window, app, output_dir, name, size)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="directory for PNG files (defaults to a temporary folder)")
    args = parser.parse_args(argv)

    temporary_output = None
    if args.output_dir is None:
        temporary_output = tempfile.TemporaryDirectory(prefix="scidev-ui-shots-")
        output_dir = Path(temporary_output.name)
    else:
        output_dir = args.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication([sys.argv[0]])
    ui_font = QFont("Microsoft YaHei UI", 11)
    ui_font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(ui_font)
    workspace_temp = tempfile.TemporaryDirectory(prefix="scidev-ui-workspace-")
    workspace = Path(workspace_temp.name)
    source_dir = workspace / "src"
    source_dir.mkdir()
    source_file = source_dir / "analysis.py"
    source_file.write_text(
        "from dataclasses import dataclass\n\n\n"
        "@dataclass\n"
        "class ExperimentConfig:\n"
        "    learning_rate: float = 0.001\n"
        "    warmup_steps: int = 200\n\n\n"
        "def score_prediction(prediction: float, target: float) -> float:\n"
        "    return 1.0 - abs(prediction - target)\n",
        encoding="utf-8",
    )
    (workspace / "README.md").write_text(
        "# Example research project\n\nA small project used for desktop visual regression checks.\n",
        encoding="utf-8",
    )
    window = None
    try:
        window = ClientWindow(workspace)
        original_saved_profile = window.ui_settings.value("uiProfile", "studio")
        window.set_ui_profile("studio", persist=False)
        window._open_file(source_file, preview=False)
        capture(window, app, output_dir, "editor-wide", (1500, 920))
        verify_workbench_layout(window, "editor-wide")
        verify_ui_profile(window, "studio", "editor-wide")
        capture_summary_settings_panel(window, app, output_dir)
        capture_ui_profile_menu(window, app, output_dir)
        window.show_git()
        fixture_nodes = [
            {
                "id": "root",
                "lane": "main",
                "status": "main",
                "title": "当前主线",
                "description": "稳定的编码主线。",
                "meta": "Git main",
            },
            {
                "id": "editor-cleanup",
                "lane": "main",
                "status": "main",
                "parent_id": "root",
                "title": "整理指标计算",
                "description": "抽离评价逻辑并补齐类型标注。",
                "meta": "成功 · 2026-10-01",
            },
            {
                "id": "fast-warmup",
                "lane": "attempt",
                "status": "failed",
                "parent_id": "editor-cleanup",
                "title": "缩短 warmup",
                "description": "验证集波动明显，暂不保留。",
                "meta": "失败 · 3 次尝试",
            },
            {
                "id": "typed-config",
                "lane": "attempt",
                "status": "active",
                "parent_id": "editor-cleanup",
                "title": "类型化配置",
                "description": "把配置默认值整理成 dataclass。",
                "meta": "进行中 · 1 次尝试",
            },
        ]
        window.git_tree.set_nodes(fixture_nodes)
        window.git_main_value.setText("1")
        window.git_attempt_value.setText("2")
        window.git_failed_value.setText("1")

        responsive_viewports = (
            ("1080p", (1920, 1080)),
            ("2k", (2560, 1440)),
            ("4k", (3840, 2160)),
            ("laptop-1366", (1366, 768)),
            ("720p", (1280, 720)),
            ("expanded-breakpoint", (1120, 700)),
            ("compact-breakpoint", (1119, 700)),
            ("xga", (1024, 768)),
            ("explorer-visible-edge", (900, 700)),
            ("explorer-collapsed-edge", (899, 700)),
        )
        for profile_key in UI_PROFILES:
            window.set_ui_profile(profile_key, persist=False)
            verify_ui_profile(window, profile_key, f"responsive-{profile_key}")
            window.show_workspace()
            window.title_git_button.setFocus()
            app.processEvents()
            focus_name = f"keyboard-focus-{profile_key}"
            focus_image = capture(window, app, output_dir, focus_name, (1500, 920))
            verify_keyboard_focus_ring(window, focus_image, focus_name)
            for suffix, size in responsive_viewports:
                name = f"responsive-{profile_key}-{suffix}"
                capture(
                    window,
                    app,
                    output_dir,
                    name,
                    size,
                    physical_size=suffix in {"720p", "1080p", "2k", "4k"},
                )
                verify_workbench_layout(window, name)

        for profile_key in UI_PROFILES:
            capture_notification_toast(window, app, output_dir, profile_key, (1500, 920))
        capture_notification_toast(window, app, output_dir, "paper", (820, 600))

        for profile_key in ("paper", "focus"):
            window.set_ui_profile(profile_key, persist=False)
            verify_ui_profile(window, profile_key, f"editor-{profile_key}")
            window.show_workspace()
            for suffix, size in (
                ("wide", (1500, 920)),
                ("medium", (940, 620)),
                ("minimum", (780, 480)),
            ):
                name = f"editor-{profile_key}-{suffix}"
                capture(window, app, output_dir, name, size)
                verify_workbench_layout(window, name)

            window.workspace_stack.setCurrentWidget(window.git_page)
            for suffix, size, orientation in (
                ("wide", (1500, 920), Qt.Orientation.Horizontal),
                ("minimum", (780, 480), Qt.Orientation.Vertical),
            ):
                name = f"tree-{profile_key}-{suffix}"
                capture(window, app, output_dir, name, size)
                verify_git_splitter_layout(window, name, orientation)

        window.set_ui_profile("studio", persist=False)
        window.git_tree.set_nodes(fixture_nodes)
        if window.ui_settings.value("uiProfile", "studio") != original_saved_profile:
            raise RuntimeError("offscreen theme review changed the user's saved UI choice")
        window.workspace_stack.setCurrentWidget(window.git_page)
        capture(window, app, output_dir, "tree-wide", (1500, 920))
        verify_workbench_layout(window, "tree-wide")
        verify_git_splitter_layout(window, "tree-wide", Qt.Orientation.Horizontal)
        pan_canvas(window, app, output_dir, (1500, 920))
        if window.git_tree_splitter.orientation() != Qt.Orientation.Horizontal:
            raise RuntimeError("wide version tree did not use a side-by-side layout")
        drag_attempt_node(window, app, output_dir, "tree-wide-dragged", (1500, 920))
        window.git_tree._node_offsets.pop("fast-warmup", None)
        window.git_tree._save_layout()
        window.git_tree._root_item.setProperty("layoutOffsets", window.git_tree._qml_offsets())
        window.git_tree.set_nodes(window.git_tree.nodes)
        window.git_tree_scroll.verticalScrollBar().setValue(0)
        app.processEvents()
        capture(window, app, output_dir, "tree-medium", (1180, 760))
        verify_workbench_layout(window, "tree-medium")
        verify_git_splitter_layout(window, "tree-medium", Qt.Orientation.Vertical)
        narrow_tree_image = capture(window, app, output_dir, "tree-narrow", (820, 600))
        verify_workbench_layout(window, "tree-narrow")
        verify_git_splitter_layout(window, "tree-narrow", Qt.Orientation.Vertical)
        if window.git_tree_splitter.orientation() != Qt.Orientation.Vertical:
            raise RuntimeError("narrow version tree did not switch to a stacked layout")
        verify_attempt_accent_pixel(window, narrow_tree_image, "tree-narrow", "fast-warmup")
        minimum_tree_image = capture(window, app, output_dir, "tree-minimum", (780, 480))
        verify_workbench_layout(window, "tree-minimum")
        verify_git_splitter_layout(window, "tree-minimum", Qt.Orientation.Vertical)
        verify_attempt_accent_pixel(window, minimum_tree_image, "tree-minimum", "fast-warmup")
        branch_card = window.git_tree._positions()[0]["fast-warmup"][2]
        root = window.git_tree.rootObject()
        visible_branch_bottom = (
            branch_card.bottom() * float(root.property("zoom")) + float(root.property("panY"))
        )
        print(
            "tree-narrow fit "
            f"zoom={float(root.property('zoom')):.3f} panY={float(root.property('panY')):.1f} "
            f"branch-bottom={visible_branch_bottom:.1f}/{window.git_tree_scroll.viewport().height()}"
        )
        if visible_branch_bottom >= window.git_tree_scroll.viewport().height():
            raise RuntimeError(
                "the first attempt node is clipped in the narrow version-tree layout: "
                f"visual-card-bottom={visible_branch_bottom:.1f}px logical-card-bottom={branch_card.bottom()}px viewport="
                f"{window.git_tree_scroll.viewport().size()} splitter={window.git_tree_splitter.sizes()}"
            )
        if float(root.property("zoom")) < 0.95:
            raise RuntimeError(
                "compact version-tree auto-fit shrank the cards below the readable zoom target: "
                f"zoom={float(root.property('zoom')):.3f}"
            )
        logical_card = window.git_tree._positions()[0]["fast-warmup"][2]
        if logical_card.width() * float(root.property("zoom")) < 130:
            raise RuntimeError("compact version-tree branch cards are too narrow for readable labels")
        scroll_bar = window.git_tree_scroll.verticalScrollBar()
        if scroll_bar.maximum() <= 0:
            raise RuntimeError("narrow version tree does not expose the offscreen content")
        initial_scroll = scroll_bar.value()
        initial_zoom = float(root.property("zoom"))
        send_wheel(window, Qt.KeyboardModifier.NoModifier)
        app.processEvents()
        if scroll_bar.value() <= initial_scroll:
            raise RuntimeError("plain mouse wheel did not scroll the narrow version tree")
        if float(root.property("zoom")) != initial_zoom:
            raise RuntimeError("plain mouse wheel unexpectedly zoomed the version tree")
        capture(window, app, output_dir, "tree-narrow-scrolled", (820, 600))
        initial_scroll = scroll_bar.value()
        send_wheel(window, Qt.KeyboardModifier.ControlModifier, angle_delta_y=120)
        app.processEvents()
        if float(root.property("zoom")) <= initial_zoom:
            raise RuntimeError("Ctrl+mouse wheel did not zoom the version tree")
        if scroll_bar.value() != initial_scroll:
            raise RuntimeError("Ctrl+mouse wheel unexpectedly scrolled the version tree")
        compact_restore = window._git_tree_compact_restore
        if compact_restore is None:
            raise RuntimeError("compact version tree did not keep its wide-layout view state")
        capture(window, app, output_dir, "tree-wide-restored", (1500, 920))
        verify_git_splitter_layout(window, "tree-wide-restored", Qt.Orientation.Horizontal)
        restored_view = (
            float(root.property("zoom")),
            float(root.property("panX")),
            float(root.property("panY")),
        )
        if any(abs(value - expected) > 0.02 for value, expected in zip(restored_view, compact_restore)):
            raise RuntimeError(
                f"wide-layout version-tree zoom/pan was not restored: {restored_view} != {compact_restore}"
        )
        if window._git_tree_compact_restore is not None:
            raise RuntimeError("compact-layout view state was not cleared after returning wide")
        capture(window, app, output_dir, "tree-wide-short", (1500, 520))
        verify_workbench_layout(window, "tree-wide-short")
        verify_git_splitter_layout(window, "tree-wide-short", Qt.Orientation.Horizontal)
        capture(window, app, output_dir, "tree-wide-short-restored", (1500, 920))
        verify_workbench_layout(window, "tree-wide-short-restored")
        verify_git_splitter_layout(window, "tree-wide-short-restored", Qt.Orientation.Horizontal)
        for resolution, size in (
            ("720p", (1280, 720)),
            ("1080p", (1920, 1080)),
            ("2k", (2560, 1440)),
            ("4k", (3840, 2160)),
        ):
            name = f"tree-resolution-studio-{resolution}"
            capture(window, app, output_dir, name, size, physical_size=True)
            verify_workbench_layout(window, name)
            orientation = Qt.Orientation.Vertical if resolution == "720p" else Qt.Orientation.Horizontal
            verify_git_splitter_layout(window, name, orientation)
        window.show_workspace()
        for name, size in (
            ("editor-narrow", (940, 620)),
            ("editor-compact", (820, 600)),
            ("editor-minimum", (780, 480)),
        ):
            capture(window, app, output_dir, name, size)
            verify_workbench_layout(window, name)
        hint = window.workspace_hint
        if hint.fontMetrics().horizontalAdvance(hint.text()) > hint.width():
            raise RuntimeError("workspace footer status is clipped at the narrow layout")
        capture_unsaved_close_confirmation(window, app, output_dir)
    finally:
        if window is not None:
            window.close()
        app.processEvents()
        app.quit()
        workspace_temp.cleanup()
        if temporary_output is not None:
            temporary_output.cleanup()

    print("UI screenshot smoke test: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
