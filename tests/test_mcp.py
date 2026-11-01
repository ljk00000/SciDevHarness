from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
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
    elif method == "resources/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"resources": [
            {"uri": "file:///notes.txt", "name": "notes", "mimeType": "text/plain"},
        ]}})
    elif method == "resources/read":
        uri = request.get("params", {}).get("uri")
        send({"jsonrpc": "2.0", "id": request_id, "result": {"contents": [
            {"uri": uri, "mimeType": "text/plain", "text": "resource-body"},
        ]}})
    elif method == "prompts/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"prompts": [
            {"name": "greet", "description": "greeting", "arguments": [{"name": "who"}]},
        ]}})
    elif method == "prompts/get":
        arguments = request.get("params", {}).get("arguments", {})
        send({"jsonrpc": "2.0", "id": request_id, "result": {"messages": [
            {"role": "user", "content": {"type": "text", "text": "hello " + str(arguments.get("who", ""))}},
        ]}})
    elif request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown"}})
'''


class _HTTPHandler(BaseHTTPRequestHandler):
    tools = [{
        "name": "echo",
        "description": "echo text",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    }]

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        method = payload.get("method")
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        result = {}
        if method == "initialize":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "test-session")
        elif method == "tools/list":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            result = {"tools": self.tools}
        elif method == "tools/call":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            args = payload.get("params", {}).get("arguments", {})
            result = {"content": [{"type": "text", "text": str(args.get("text") or "")}]}
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.end_headers()
        self.wfile.write(json.dumps({
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "result": result,
        }).encode("utf-8"))


class MCPHTTPTests(unittest.TestCase):
    def test_http_transport_discovers_and_calls_tools(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HTTPHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="scidev-mcp-http-", ignore_cleanup_errors=True) as temp:
                root = Path(temp)
                (root / ".research").mkdir()
                (root / ".research" / "mcp.json").write_text(json.dumps({
                    "enabled": True,
                    "servers": {
                        "remote": {
                            "url": f"http://127.0.0.1:{server.server_port}/mcp",
                            "transport": "http",
                        }
                    },
                }), encoding="utf-8")
                manager = MCPManager(root)
                try:
                    tools = manager.start()
                    self.assertEqual([tool.public_name for tool in tools], ["mcp__remote__echo"])
                    self.assertEqual(
                        manager.clients["remote"].call_tool("echo", {"text": "hello"}),
                        "hello",
                    )
                finally:
                    manager.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_http_transport_is_blocked_by_network_deny(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-mcp-http-deny-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            (root / ".research").mkdir()
            (root / ".research" / "settings.json").write_text(
                '{"network_policy": "deny"}', encoding="utf-8",
            )
            (root / ".research" / "mcp.json").write_text(json.dumps({
                "enabled": True,
                "servers": {"remote": {"url": "http://127.0.0.1:9/mcp"}},
            }), encoding="utf-8")
            manager = MCPManager(root)
            self.assertEqual(manager.start(), [])
            self.assertTrue(any("网络策略" in error for error in manager.errors))


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

    def test_manager_lists_resources_and_prompts(self) -> None:
        root, _server = self._workspace()
        manager = MCPManager(root)
        try:
            resources = manager.list_resources()
            self.assertEqual(resources[0]["uri"], "file:///notes.txt")
            self.assertEqual(resources[0]["server"], "demo")
            self.assertEqual(manager.read_resource("demo", "file:///notes.txt"), "resource-body")
            prompts = manager.list_prompts()
            self.assertEqual(prompts[0]["name"], "greet")
            self.assertEqual(
                manager.get_prompt("demo", "greet", {"who": "Ada"}),
                "hello Ada",
            )
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
