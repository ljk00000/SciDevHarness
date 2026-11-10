"""QML-backed development tree widget."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRect, Qt, QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QScrollArea

from scidev_paths import application_base_dir
from scidev_ui import DEFAULT_UI_PROFILE, ui_font_family, ui_profile_for_key

MAX_TREE_LAYOUT_BYTES = 1_000_000
MAX_TREE_LAYOUT_OFFSET = 50_000.0


def _finite_tree_coordinate(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        coordinate = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(coordinate) or abs(coordinate) > MAX_TREE_LAYOUT_OFFSET:
        return None
    return coordinate


def _load_tree_layout_offsets(layout_path: Path | None) -> dict[str, QPointF]:
    if layout_path is None:
        return {}
    try:
        if layout_path.stat().st_size > MAX_TREE_LAYOUT_BYTES:
            return {}
        raw = json.loads(layout_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return {}
    if not isinstance(raw, dict):
        return {}

    offsets: dict[str, QPointF] = {}
    for node_id, value in raw.items():
        if not isinstance(node_id, str) or not node_id or len(node_id) > 256 or not isinstance(value, dict):
            continue
        x = _finite_tree_coordinate(value.get("x", 0))
        y = _finite_tree_coordinate(value.get("y", 0))
        if x is not None and y is not None:
            offsets[node_id] = QPointF(x, y)
    return offsets

class DevelopmentTreeView(QQuickWidget):
    """QML-backed development tree with a Python persistence/compatibility shell."""

    node_selected = Signal(object)

    def __init__(
        self,
        layout_path: Path | None = None,
        visual_theme: str = DEFAULT_UI_PROFILE,
    ):
        super().__init__()
        self.setObjectName("DevelopmentTree")
        self.setMinimumHeight(420)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._visual_theme = ui_profile_for_key(visual_theme).tree_mode
        self._ui_scale = 1.0
        self.setClearColor(QColor("#101419"))
        self.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self.nodes: list[dict[str, Any]] = []
        self.selected_id: str | None = None
        self._layout_path = Path(layout_path).resolve() if layout_path is not None else None
        self._node_offsets: dict[str, QPointF] = {}
        self._root_item = None
        self._load_layout()

        qml_path = application_base_dir() / "ui" / "qml" / "DevelopmentTree.qml"
        self.setSource(QUrl.fromLocalFile(str(qml_path)))
        root = self.rootObject()
        if root is not None:
            self._root_item = root
            root.nodeSelected.connect(self._on_qml_node_selected)
            root.nodeMoved.connect(self._on_qml_node_moved)
            root.setProperty("layoutOffsets", self._qml_offsets())
            root.setProperty("visualTheme", self._visual_theme)
            root.setProperty("uiScale", self._ui_scale)
            root.setProperty("uiFontFamily", ui_font_family())

    def set_ui_scale(self, ui_scale: float) -> None:
        """Keep version-tree labels and cards aligned with screen UI scaling."""
        self._ui_scale = min(2.2, max(0.9, float(ui_scale)))
        self._update_minimum_height()
        if self._root_item is not None:
            self._root_item.setProperty("uiScale", self._ui_scale)

    def set_visual_theme(self, profile_key: str) -> None:
        """Keep the QML canvas aligned with the selected workbench profile."""
        self._visual_theme = ui_profile_for_key(profile_key).tree_mode
        clear_color = {
            "paper": "#f5f8fb",
            "focus": "#11131b",
            "studio": "#101419",
        }[self._visual_theme]
        self.setClearColor(QColor(clear_color))
        if self._root_item is not None:
            self._root_item.setProperty("visualTheme", self._visual_theme)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if self._root_item is None:
                super().wheelEvent(event)
                return
            delta = event.pixelDelta().y() or event.angleDelta().y()
            if delta == 0:
                event.ignore()
                return
            old_zoom = float(self._root_item.property("zoom"))
            ui_scale = float(self._root_item.property("uiScale"))
            factor = 1.12 if delta > 0 else 1 / 1.12
            new_zoom = max(0.65, min(1.8, old_zoom * factor))
            position = event.position()
            pan_x = float(self._root_item.property("panX"))
            pan_y = float(self._root_item.property("panY"))
            logical_x = (position.x() - pan_x) / (old_zoom * ui_scale)
            logical_y = (position.y() - pan_y) / (old_zoom * ui_scale)
            self._root_item.setProperty("zoom", new_zoom)
            self._root_item.setProperty("panX", position.x() - logical_x * new_zoom * ui_scale)
            self._root_item.setProperty("panY", position.y() - logical_y * new_zoom * ui_scale)
            event.accept()
            return

        scroll_area = self.parentWidget()
        while scroll_area is not None and not isinstance(scroll_area, QScrollArea):
            scroll_area = scroll_area.parentWidget()
        if scroll_area is None:
            super().wheelEvent(event)
            return

        scroll_bar = scroll_area.verticalScrollBar()
        delta = event.pixelDelta().y()
        if delta == 0:
            wheel_steps = event.angleDelta().y() / 120
            delta = wheel_steps * max(1, scroll_bar.singleStep()) * 3
        if delta == 0:
            super().wheelEvent(event)
            return
        scroll_bar.setValue(scroll_bar.value() - round(delta))
        event.accept()

    @staticmethod
    def _accent(node: dict[str, Any]) -> QColor:
        if node.get("lane") == "main":
            return QColor("#55e3c1")
        status = node.get("status")
        if not isinstance(status, str):
            status = ""
        return {
            "failed": QColor("#ff8978"),
            "retry": QColor("#f2ca78"),
            "active": QColor("#70b8ff"),
        }.get(status, QColor("#a7b1b8"))

    @staticmethod
    def _status_text(node: dict[str, Any]) -> str:
        if node.get("lane") == "main":
            return "当前主线"
        return {
            "failed": "已取消 / 失败",
            "retry": "等待重试",
            "active": "进行中",
        }.get(node.get("status") or "", "尝试方向")

    def _load_layout(self) -> None:
        self._node_offsets = _load_tree_layout_offsets(self._layout_path)

    def _save_layout(self) -> None:
        if self._layout_path is None:
            return
        try:
            self._layout_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                node_id: {"x": round(offset.x(), 1), "y": round(offset.y(), 1)}
                for node_id, offset in self._node_offsets.items()
            }
            self._layout_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            return

    def _qml_offsets(self) -> dict[str, dict[str, float]]:
        return {
            node_id: {"x": float(offset.x()), "y": float(offset.y())}
            for node_id, offset in self._node_offsets.items()
        }

    def _on_qml_node_selected(self, node_id: str) -> None:
        self.selected_id = str(node_id)
        selected = next((node for node in self.nodes if str(node.get("id")) == self.selected_id), None)
        if selected is not None:
            self.node_selected.emit(selected)

    def _on_qml_node_moved(self, node_id: str, offset_x: float, offset_y: float) -> None:
        x = _finite_tree_coordinate(offset_x)
        y = _finite_tree_coordinate(offset_y)
        if x is None or y is None:
            return
        self._node_offsets[str(node_id)] = QPointF(x, y)
        self._update_minimum_height()
        self._save_layout()

    def _update_minimum_height(self) -> None:
        main_count = max(1, sum(node.get("lane") == "main" for node in self.nodes))
        attempts_by_parent: dict[str, int] = {}
        for node in self.nodes:
            if node.get("lane") == "attempt":
                parent_id = str(node.get("parent_id") or "root")
                attempts_by_parent[parent_id] = attempts_by_parent.get(parent_id, 0) + 1
        extra_height = sum(max(0, count - 1) * 100 for count in attempts_by_parent.values())
        offset_bottom = max(
            (max(0.0, offset.y()) for offset in self._node_offsets.values()),
            default=0.0,
        )
        scale = self._ui_scale
        content_height = (160 + main_count * 140 + extra_height + offset_bottom) * scale
        self.setMinimumHeight(max(math.ceil(420 * scale), math.ceil(content_height)))

    def set_nodes(self, nodes: list[dict[str, Any]]) -> None:
        self.nodes = nodes
        ids = {str(node["id"]) for node in nodes}
        self._node_offsets = {node_id: offset for node_id, offset in self._node_offsets.items() if node_id in ids}
        if self.selected_id not in ids:
            self.selected_id = str(nodes[0]["id"]) if nodes else None

        self._update_minimum_height()

        if self._root_item is not None:
            qml_nodes = []
            for node in nodes:
                item = dict(node)
                offset = self._node_offsets.get(str(node["id"]), QPointF())
                item["offset_x"] = float(offset.x())
                item["offset_y"] = float(offset.y())
                qml_nodes.append(item)
            self._root_item.setProperty("nodes", qml_nodes)
            self._root_item.setProperty("layoutOffsets", self._qml_offsets())
            self._root_item.setProperty("selectedId", self.selected_id or "")

        selected = next((node for node in nodes if str(node.get("id")) == self.selected_id), None)
        if selected is not None:
            self.node_selected.emit(selected)

    def _positions(self) -> tuple[dict[str, tuple[int, int, QRect]], dict[str, tuple[int, int]]]:
        """Return the logical geometry used by the QML scene and UI smoke checks."""
        main_nodes = [node for node in self.nodes if node.get("lane") == "main"]
        if not main_nodes:
            return {}, {}
        card_width = min(300, max(190, int(self.width() * 0.31)))
        if self.width() < 620:
            card_width = min(300, max(150, int((self.width() - 44) / 2)))
        trunk_x = self.width() // 2
        card_height = 90
        compact_layout = self.width() < 520 or self.height() < 360
        top = 56 if compact_layout else 104
        row_height = 104 if compact_layout else 140
        attempt_offset = 52 if compact_layout else 62
        attempt_slot_height = 100
        cards: dict[str, tuple[int, int, QRect]] = {}
        dots: dict[str, tuple[int, int]] = {}
        attempts_by_parent: dict[str, int] = {}
        for node in (item for item in self.nodes if item.get("lane") == "attempt"):
            parent_id = str(node.get("parent_id") or "root")
            attempts_by_parent[parent_id] = attempts_by_parent.get(parent_id, 0) + 1
        main_y: dict[str, int] = {}
        y = top
        for node in main_nodes:
            main_y[node["id"]] = y
            y += row_height + max(0, attempts_by_parent.get(str(node["id"]), 0) - 1) * attempt_slot_height
        for node in main_nodes:
            y = main_y[node["id"]]
            dots[node["id"]] = (trunk_x, y)
            card_x = max(12, trunk_x - card_width - 58)
            cards[node["id"]] = (trunk_x, y, QRect(card_x, y - card_height // 2, card_width, card_height))

        children_count: dict[str, int] = {}
        for node in (item for item in self.nodes if item.get("lane") == "attempt"):
            parent_id = str(node.get("parent_id") or main_nodes[-1]["id"])
            parent_x, parent_y = dots.get(parent_id, dots[main_nodes[-1]["id"]])
            slot = children_count.get(parent_id, 0)
            children_count[parent_id] = slot + 1
            offset = self._node_offsets.get(str(node["id"]), QPointF())
            dot_x = int(parent_x + 78 + offset.x())
            dot_y = max(90, int(parent_y + attempt_offset + slot * attempt_slot_height + offset.y()))
            card_x = int(min(self.width() - card_width - 12, parent_x + 102) + offset.x())
            cards[node["id"]] = (dot_x, dot_y, QRect(card_x, dot_y - card_height // 2, card_width, card_height))
            dots[node["id"]] = (dot_x, dot_y)
        return cards, dots
