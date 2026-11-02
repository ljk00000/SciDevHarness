from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
import unittest

from scidev_mcp_server import MCPServer, serve_stdio


class MCPServerTests(unittest.TestCase):
    def _server(self, root: Path) -> MCPServer:
        (root / "app.py").write_text("print('hello')\n", encoding="utf-8")
        return MCPServer(root)

    def test_initialize_and_tools_list_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-mcp-", ignore_cleanup_errors=True) as temp:
            server = self._server(Path(temp))
            initialized = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            assert initialized is not None
            self.assertEqual(initialized["result"]["protocolVersion"], "2024-11-05")
            listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert listed is not None
            names = {item["name"] for item in listed["result"]["tools"]}
            self.assertIn("read_file", names)
            self.assertIn("search_code", names)
            self.assertNotIn("write_file", names)
            self.assertNotIn("run_command", names)

    def test_read_call_and_write_rejection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-mcp-", ignore_cleanup_errors=True) as temp:
            server = self._server(Path(temp))
            result = server.call("read_file", {"path": "app.py"})
            self.assertFalse(result["isError"])
            self.assertIn("print('hello')", result["content"][0]["text"])
            forbidden = server.call("write_file", {"path": "app.py", "content": "changed"})
            self.assertTrue(forbidden["isError"])
            self.assertIn("只读", forbidden["content"][0]["text"])
            unknown = server.handle({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "run_command", "arguments": {"command": "whoami"}},
            })
            assert unknown is not None
            self.assertTrue(unknown["result"]["isError"])

    def test_stdio_ignores_notifications_and_unknown_methods(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-mcp-", ignore_cleanup_errors=True) as temp:
            server = self._server(Path(temp))
            requests = [
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "id": 2, "method": "missing"},
                {"jsonrpc": "2.0", "id": 3, "method": "shutdown"},
            ]
            source = io.StringIO("\n".join(json.dumps(item) for item in requests) + "\n")
            sink = io.StringIO()
            serve_stdio(server, source, sink)
            responses = [json.loads(line) for line in sink.getvalue().splitlines()]
            self.assertEqual([item.get("id") for item in responses], [1, 2, 3])
            self.assertEqual(responses[0]["result"], {})
            self.assertIn("error", responses[1])
            self.assertIsNone(responses[2]["result"])


if __name__ == "__main__":
    unittest.main()
