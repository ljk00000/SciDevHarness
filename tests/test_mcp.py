from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from scidev_core import CodingToolbox, EventLedger, PermanentError
from scidev_mcp import MCPManager


FAKE_SERVER = r'''
import json
import sys


def send(payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()


def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.strip().lower()] = value.strip()
    body = sys.stdin.buffer.read(int(headers["content-length"]))
    return json.loads(body.decode("utf-8"))


while True:
    request = read()
    if request is None:
        break
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"},
        }})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{
            "name": "echo",
            "description": "Echo the supplied text.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }]}})
    elif method == "tools/call":
        arguments = request.get("params", {}).get("arguments", {})
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "content": [{"type": "text", "text": "echo:" + str(arguments.get("text", ""))}],
            "isError": False,
        }})
    elif request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown"}})
'''


class MCPTests(unittest.TestCase):
    def _workspace(self) -> tuple[Path, Path]:
        temp = tempfile.TemporaryDirectory(prefix="scidev-mcp-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        server = root / "fake_mcp.py"
        server.write_text(FAKE_SERVER, encoding="utf-8")
        config_dir = root / ".research"
        config_dir.mkdir(parents=True)
        (config_dir / "mcp.json").write_text(json.dumps({
            "enabled": True,
            "servers": {
                "demo": {
                    "command": sys.executable,
                    "args": [str(server)],
                    "cwd": ".",
                    "timeout": 10,
                },
            },
        }), encoding="utf-8")
        return root, server

    def test_disabled_config_never_starts_a_server(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-mcp-disabled-") as temp:
            root = Path(temp)
            (root / ".research").mkdir()
            (root / ".research" / "mcp.json").write_text('{"enabled": false}', encoding="utf-8")
            manager = MCPManager(root)
            try:
                self.assertEqual(manager.tool_definitions(), [])
                self.assertEqual(manager.clients, {})
            finally:
                manager.close()

    def test_manager_lists_and_calls_a_fake_stdio_tool(self) -> None:
        root, _server = self._workspace()
        manager = MCPManager(root)
        try:
            definitions = manager.tool_definitions()
            names = [item["function"]["name"] for item in definitions]
            self.assertIn("mcp__demo__echo", names)
            result = manager.call("mcp__demo__echo", {"text": "hello"})
            self.assertEqual(result, "echo:hello")
        finally:
            manager.close()

    def test_toolbox_requires_approval_and_executes_mcp_tool(self) -> None:
        root, _server = self._workspace()
        ledger = EventLedger(root / ".research" / "events.db")
        toolbox = CodingToolbox(
            root,
            ledger,
            command_approval=lambda _command, _timeout: True,
            execution_mode="workspace-write",
        )
        try:
            definitions = toolbox.all_definitions()
            self.assertTrue(any(
                item["function"]["name"] == "mcp__demo__echo" for item in definitions
            ))
            self.assertEqual(
                toolbox.execute("mcp__demo__echo", {"text": "approved"}),
                "echo:approved",
            )
        finally:
            toolbox.close()

    def test_toolbox_rejects_mcp_without_approval_callback(self) -> None:
        root, _server = self._workspace()
        ledger = EventLedger(root / ".research" / "events.db")
        toolbox = CodingToolbox(root, ledger, execution_mode="workspace-write")
        try:
            with self.assertRaises(PermanentError):
                toolbox.execute("mcp__demo__echo", {"text": "no"})
        finally:
            toolbox.close()


if __name__ == "__main__":
    unittest.main()
