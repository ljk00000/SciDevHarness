"""Read-only MCP stdio server exposing the project inspection tools.

The server deliberately never exposes write_file/apply_patch/run_command. It is
for other agents that need local project context, not a remote execution
surface.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scidev_core import CodingToolbox, EventLedger, PermanentError
from scidev_tool_schema import tool_definitions
from scidev_update import APP_VERSION

READ_ONLY_TOOLS = frozenset({
    "search_code",
    "get_project_context",
    "list_files",
    "read_file",
    "git_diff",
})
PROTOCOL_VERSION = "2024-11-05"


class MCPServer:
    def __init__(self, project_root: Path, *, toolbox: CodingToolbox | None = None):
        self.project_root = Path(project_root).resolve()
        self.toolbox = toolbox or CodingToolbox(
            self.project_root,
            EventLedger(self.project_root),
            execution_mode="read-only",
        )
        schemas = tool_definitions()
        self._schemas = {
            item["function"]["name"]: item
            for item in schemas
            if item["function"]["name"] in READ_ONLY_TOOLS
        }

    def tools_list(self) -> list[dict[str, Any]]:
        result = []
        for name in sorted(self._schemas):
            schema = self._schemas[name]
            result.append({
                "name": name,
                "description": schema["function"].get("description", ""),
                "inputSchema": schema["function"]["parameters"],
            })
        return result

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in READ_ONLY_TOOLS:
            return self._tool_error(name, "未知或不可暴露的工具；此服务只读")
        handler = getattr(self.toolbox, name, None)
        if handler is None:
            return self._tool_error(name, f"工具未实现：{name}")
        try:
            text = handler(**(arguments or {}))
        except (PermanentError, TypeError, ValueError) as exc:
            return self._tool_error(name, str(exc))
        if not isinstance(text, str):
            text = str(text)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    @staticmethod
    def _tool_error(name: str, message: str) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": f"{name}: {message}"}],
            "isError": True,
        }

    def handle(self, request: object) -> dict[str, Any] | None:
        if not isinstance(request, dict):
            return self._error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(method, str):
            return self._error(request_id, -32600, "Invalid Request")
        if method == "notifications/initialized" or method == "initialized":
            return None
        result: Any
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "scidev-harness", "version": APP_VERSION},
            }
        elif method == "tools/list":
            result = {"tools": self.tools_list()}
        elif method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict):
                return self._error(request_id, -32602, "tools/call 需要对象参数")
            name = params.get("name")
            arguments = params.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return self._error(request_id, -32602, "tools/call 缺少 name/arguments")
            result = self.call(name, arguments)
        elif method == "ping":
            result = {}
        elif method == "shutdown":
            result = None
        else:
            return self._error(request_id, -32601, f"Method not found: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve_stdio(server: MCPServer, input_stream, output_stream) -> None:
    for raw in input_stream:
        line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response: dict[str, Any] | None = MCPServer._error(None, -32700, f"Parse error: {exc.msg}")
        else:
            response = server.handle(request)
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()
