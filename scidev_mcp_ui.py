"""Read-only MCP capability browser with explicit composer insertion."""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from scidev_mcp import MCPError, MCPManager


class MCPBrowserDialog(QDialog):
    insert_requested = Signal(str)

    def __init__(self, manager: MCPManager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("MCP 能力")
        self.setMinimumSize(720, 480)
        layout = QVBoxLayout(self)

        self.hint = QLabel("资源与提示词不会自动发给模型；选中后读取，再显式插入输入框。")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["能力", "名称", "详情"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_: self.read_selected())
        layout.addWidget(self.tree, 1)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("读取资源或渲染提示词后显示在这里")
        self.preview.setMaximumHeight(180)
        layout.addWidget(self.preview)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        self.read_button = QPushButton("读取")
        self.read_button.clicked.connect(self.read_selected)
        self.insert_button = QPushButton("插入输入框")
        self.insert_button.clicked.connect(self.insert_preview)
        row.addWidget(self.refresh_button)
        row.addWidget(self.read_button)
        row.addWidget(self.insert_button)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

        self.refresh()
        self._update_buttons()

    def _update_buttons(self) -> None:
        kind, _payload = self._selected()
        self.read_button.setEnabled(kind in {"resource", "prompt"})
        self.insert_button.setEnabled(bool(self.preview.toPlainText().strip()))

    def _selected(self) -> tuple[str, dict[str, Any]]:
        item = self.tree.currentItem()
        while item is not None and item.parent() is not None and not item.data(0, Qt.ItemDataRole.UserRole):
            item = item.parent()
        if item is None:
            return "", {}
        kind = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
        payload = item.data(0, Qt.ItemDataRole.UserRole + 1)
        return kind, payload if isinstance(payload, dict) else {}

    def refresh(self) -> None:
        self.tree.clear()
        self.preview.clear()
        self.status.setText("正在启动 MCP 服务…")
        try:
            tools = self.manager.start()
            resources = self.manager.list_resources()
            prompts = self.manager.list_prompts()
        except MCPError as exc:
            self.status.setText(str(exc))
            return
        tool_root = QTreeWidgetItem(["工具", f"{len(tools)}", ""])
        for tool in tools:
            child = QTreeWidgetItem([tool.public_name, tool.name, tool.description[:160]])
            tool_root.addChild(child)
        resource_root = QTreeWidgetItem(["资源", f"{len(resources)}", ""])
        for resource in resources:
            child = QTreeWidgetItem([
                str(resource.get("server") or ""),
                str(resource.get("name") or resource.get("uri") or ""),
                str(resource.get("description") or resource.get("uri") or "")[:160],
            ])
            child.setData(0, Qt.ItemDataRole.UserRole, "resource")
            child.setData(0, Qt.ItemDataRole.UserRole + 1, {
                "server": str(resource.get("server") or ""),
                "uri": str(resource.get("uri") or ""),
                "name": str(resource.get("name") or ""),
            })
            resource_root.addChild(child)
        prompt_root = QTreeWidgetItem(["提示词", f"{len(prompts)}", ""])
        for prompt in prompts:
            child = QTreeWidgetItem([
                str(prompt.get("server") or ""),
                str(prompt.get("name") or ""),
                str(prompt.get("description") or "")[:160],
            ])
            child.setData(0, Qt.ItemDataRole.UserRole, "prompt")
            child.setData(0, Qt.ItemDataRole.UserRole + 1, {
                "server": str(prompt.get("server") or ""),
                "name": str(prompt.get("name") or ""),
                "arguments": prompt.get("arguments") if isinstance(prompt.get("arguments"), list) else [],
            })
            prompt_root.addChild(child)
        self.tree.addTopLevelItem(tool_root)
        self.tree.addTopLevelItem(resource_root)
        self.tree.addTopLevelItem(prompt_root)
        self.tree.expandAll()
        errors = list(self.manager.errors)
        self.status.setText(
            f"{len(tools)} 工具 · {len(resources)} 资源 · {len(prompts)} 提示词"
            + (f" · {len(errors)} 个错误" if errors else "")
        )
        self._update_buttons()

    def read_selected(self) -> bool:
        kind, payload = self._selected()
        if kind not in {"resource", "prompt"}:
            return False
        try:
            if kind == "resource":
                text = self.manager.read_resource(payload["server"], payload["uri"])
            else:
                arguments = self._prompt_arguments(payload)
                if arguments is None:
                    return False
                text = self.manager.get_prompt(payload["server"], payload["name"], arguments)
        except MCPError as exc:
            self.status.setText(str(exc))
            return False
        self.preview.setPlainText(text)
        self.status.setText(f"已读取 {kind}（{len(text)} 字符）")
        self._update_buttons()
        return True

    def _prompt_arguments(self, payload: dict[str, Any]) -> dict[str, str] | None:
        arguments = payload.get("arguments")
        if not arguments:
            return {}
        text, accepted = QInputDialog.getText(
            self,
            "提示词参数",
            "JSON 参数（例如 {\"name\": \"Ada\"}）：",
            text="{}",
        )
        if not accepted:
            return None
        try:
            parsed = json.loads(text or "{}")
        except ValueError:
            self.status.setText("参数不是合法 JSON")
            return None
        if not isinstance(parsed, dict):
            self.status.setText("参数必须是 JSON 对象")
            return None
        return {str(key): str(value) for key, value in parsed.items()}

    def insert_preview(self) -> bool:
        text = self.preview.toPlainText().strip()
        if not text:
            return False
        self.insert_requested.emit(text)
        self.status.setText(f"已插入 {len(text)} 字符")
        return True
