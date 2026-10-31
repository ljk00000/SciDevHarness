"""Small MCP stdio client used to expose project-local tools.

Configuration lives in ``.research/mcp.json`` and must opt in explicitly::

    {
      "enabled": true,
      "servers": {
        "demo": {
          "command": "python",
          "args": ["path/to/server.py"],
          "cwd": "."
        }
      }
    }

Only tools advertised by the server are added to the model's tool list. Every
call still goes through the normal command-approval callback; this module does
not grant permissions by itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit

from scidev_process import background_process_options


class MCPError(RuntimeError):
    pass


@dataclass(frozen=True)
class MCPTool:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def public_name(self) -> str:
        return f"mcp__{self.server}__{self.name}"


def _safe_component(value: str, fallback: str = "tool") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(value or "")).strip("_")
    return (cleaned or fallback)[:48]


class MCPHTTPClient:
    """Minimal Streamable HTTP MCP client (JSON or SSE responses)."""

    MAX_RESPONSE_BYTES = 4_000_000

    def __init__(self, server_name: str, url: str, timeout: float = 15.0):
        self.server_name = server_name
        self.url = str(url).strip()
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise MCPError("MCP HTTP URL 必须是 http/https 且包含主机名")
        if parsed.username or parsed.password:
            raise MCPError("MCP HTTP URL 不能包含用户名或密码")
        self.timeout = max(1.0, min(float(timeout), 60.0))
        self.session_id = ""
        self._next_id = 1
        self._lock = threading.Lock()

    def start(self) -> None:
        return

    def _post(self, payload: dict[str, Any], *, expect_response: bool = True) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "SciDevHarness-MCP/1",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib_request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib_request.urlopen(request, timeout=self.timeout) as response:
                session = str(response.headers.get("Mcp-Session-Id") or "").strip()
                if session:
                    self.session_id = session
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
                content_type = str(response.headers.get("Content-Type") or "").lower()
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise MCPError(f"MCP HTTP {self.server_name} 请求失败：{exc}") from exc
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise MCPError(f"MCP HTTP {self.server_name} 响应过大")
        if not expect_response or not raw:
            return {}
        if "text/event-stream" in content_type:
            message: dict[str, Any] | None = None
            for line in raw.decode("utf-8", errors="replace").splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    candidate = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(candidate, dict):
                    message = candidate
                    break
            if message is None:
                raise MCPError(f"MCP HTTP {self.server_name} 没有有效的 SSE data")
            return message
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise MCPError(f"MCP HTTP {self.server_name} 返回非 JSON") from exc
        return decoded if isinstance(decoded, dict) else {}

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
        message = self._post({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        })
        if "error" in message:
            raise MCPError(f"MCP {self.server_name}/{method} 失败：{message['error']}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def initialize(self) -> None:
        self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "SciDevHarness", "version": "1"},
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, expect_response=False)

    def list_tools(self) -> list[MCPTool]:
        result = self._request("tools/list", {})
        tools: list[MCPTool] = []
        for item in result.get("tools") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            schema = item.get("inputSchema")
            tools.append(MCPTool(
                server=self.server_name,
                name=name[:80],
                description=str(item.get("description") or "")[:500],
                input_schema=schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
            ))
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise MCPError(f"MCP 工具 {self.server_name}/{name} 返回错误")
        parts = [
            str(item.get("text") or "")
            for item in result.get("content") or []
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if parts:
            return "\n".join(parts)[:80_000]
        structured = result.get("structuredContent")
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False)[:80_000]
        return "MCP 工具没有返回文本内容"

    def close(self) -> None:
        return


def _network_denied(project_root: Path) -> bool:
    try:
        data = json.loads((Path(project_root) / ".research" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    policy = str(os.getenv("SCIDEV_NETWORK_POLICY") or data.get("network_policy") or "ask").strip().lower()
    return policy in {"deny", "denied", "off", "block"}


class MCPClient:
    def __init__(
        self,
        server_name: str,
        command: str,
        args: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: float = 15.0,
    ):
        self.server_name = server_name
        self.command = command
        self.args = list(args)
        self.cwd = Path(cwd)
        self.env = dict(env or {})
        self.timeout = max(1.0, min(float(timeout), 60.0))
        self.process: subprocess.Popen[bytes] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._next_id = 1
        self._lock = threading.Lock()

    def start(self) -> None:
        environment = os.environ.copy()
        environment.update(self.env)
        try:
            self.process = subprocess.Popen(
                [self.command, *self.args],
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                **background_process_options(),
            )
        except OSError as exc:
            raise MCPError(f"无法启动 MCP 服务 {self.server_name}：{exc}") from exc
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"scidev-mcp-{self.server_name}",
            daemon=True,
        )
        self._reader.start()

    def _read_loop(self) -> None:
        stream = self.process.stdout if self.process is not None else None
        if stream is None:
            return
        while True:
            headers: dict[str, str] = {}
            while True:
                line = stream.readline()
                if not line:
                    return
                if line in {b"\r\n", b"\n"}:
                    break
                try:
                    key, value = line.decode("ascii", errors="replace").split(":", 1)
                except ValueError:
                    continue
                headers[key.strip().lower()] = value.strip()
            try:
                length = int(headers.get("content-length", "0"))
            except ValueError:
                return
            if length <= 0 or length > 16_000_000:
                return
            body = stream.read(length)
            if not body:
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeError):
                continue
            if isinstance(payload, dict):
                self._messages.put(payload)

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise MCPError(f"MCP 服务 {self.server_name} 未启动")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        frame = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        with self._lock:
            self.process.stdin.write(frame)
            self.process.stdin.flush()

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        })
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty:
                break
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise MCPError(f"MCP {self.server_name}/{method} 失败：{message['error']}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        raise MCPError(f"MCP {self.server_name}/{method} 超时")

    def initialize(self) -> None:
        self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "SciDevHarness", "version": "1"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def list_tools(self) -> list[MCPTool]:
        result = self._request("tools/list", {})
        tools: list[MCPTool] = []
        for item in result.get("tools") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            schema = item.get("inputSchema")
            tools.append(MCPTool(
                server=self.server_name,
                name=name[:80],
                description=str(item.get("description") or "")[:500],
                input_schema=schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
            ))
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise MCPError(f"MCP 工具 {self.server_name}/{name} 返回错误")
        parts: list[str] = []
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        if parts:
            return "\n".join(parts)[:80_000]
        structured = result.get("structuredContent")
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False)[:80_000]
        return "MCP 工具没有返回文本内容"

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self.process = None


class MCPManager:
    def __init__(
        self,
        project_root: Path,
        *,
        ledger: Any = None,
        config_path: Path | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.ledger = ledger
        self.config_path = Path(config_path) if config_path is not None else self.project_root / ".research" / "mcp.json"
        self.clients: dict[str, MCPClient] = {}
        self.tools: list[MCPTool] = []
        self.errors: list[str] = []
        self._started = False
        self._lock = threading.Lock()

    def _log(self, event: str, payload: dict[str, Any]) -> None:
        append = getattr(self.ledger, "append", None)
        if callable(append):
            try:
                append(event, payload)
            except Exception:  # noqa: BLE001 - logging must not break tool discovery
                pass

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            return {}
        try:
            raw = self.config_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise MCPError(f"无法读取 MCP 配置：{exc}") from exc
        if not isinstance(data, dict):
            raise MCPError("MCP 配置必须是 JSON 对象")
        return data

    def start(self) -> list[MCPTool]:
        with self._lock:
            if self._started:
                return list(self.tools)
            self._started = True
            try:
                config = self._load_config()
            except MCPError as exc:
                self.errors.append(str(exc))
                return []
            if not config or config.get("enabled") is not True:
                return []
            servers = config.get("servers")
            if not isinstance(servers, dict):
                self.errors.append("MCP 配置缺少 servers 对象")
                return []
            for raw_name, raw_server in list(servers.items())[:8]:
                name = _safe_component(str(raw_name), "server")
                if not isinstance(raw_server, dict):
                    self.errors.append(f"{name}: 服务配置必须是对象")
                    continue
                url = str(raw_server.get("url") or "").strip()
                transport = str(raw_server.get("transport") or "").strip().lower()
                if url:
                    if transport not in {"", "http", "streamable-http", "streamable_http", "sse"}:
                        self.errors.append(f"{name}: 不支持的 HTTP transport：{transport}")
                        continue
                    if _network_denied(self.project_root):
                        self.errors.append(f"{name}: 网络策略为 deny，拒绝 MCP HTTP 服务")
                        continue
                    try:
                        client = MCPHTTPClient(
                            name,
                            url,
                            timeout=float(raw_server.get("timeout", 15) or 15),
                        )
                    except MCPError as exc:
                        self.errors.append(f"{name}: {exc}")
                        continue
                else:
                    command = str(raw_server.get("command") or "").strip()
                    args_raw = raw_server.get("args") or []
                    args = [str(value) for value in args_raw] if isinstance(args_raw, list) else []
                    if not command:
                        self.errors.append(f"{name}: 需要 command 或 url")
                        continue
                    raw_cwd = str(raw_server.get("cwd") or ".")
                    cwd = (self.project_root / raw_cwd).resolve()
                    try:
                        cwd.relative_to(self.project_root)
                    except ValueError:
                        self.errors.append(f"{name}: cwd 必须在工作区内")
                        continue
                    env_raw = raw_server.get("env") or {}
                    env = {
                        str(key): str(value)
                        for key, value in env_raw.items()
                    } if isinstance(env_raw, dict) else {}
                    client = MCPClient(
                        name,
                        command,
                        args,
                        cwd,
                        env=env,
                        timeout=float(raw_server.get("timeout", 15) or 15),
                    )
                try:
                    client.start()
                    client.initialize()
                    tools = client.list_tools()
                except (MCPError, OSError, ValueError) as exc:
                    client.close()
                    self.errors.append(f"{name}: {exc}")
                    continue
                self.clients[name] = client
                self.tools.extend(tools)
                self._log("mcp_server_started", {"server": name, "tools": len(tools)})
            return list(self.tools)

    def tool_definitions(self) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        for tool in self.start():
            definitions.append({
                "type": "function",
                "function": {
                    "name": tool.public_name,
                    "description": (
                        f"[MCP {tool.server}] {tool.description}"
                        if tool.description else f"[MCP {tool.server}] {tool.name}"
                    )[:600],
                    "parameters": tool.input_schema,
                },
            })
        return definitions

    def call(self, public_name: str, arguments: dict[str, Any]) -> str:
        self.start()
        for tool in self.tools:
            if tool.public_name == public_name:
                client = self.clients.get(tool.server)
                if client is None:
                    raise MCPError(f"MCP 服务未连接：{tool.server}")
                return client.call_tool(tool.name, arguments)
        raise MCPError(f"未知 MCP 工具：{public_name}")

    def close(self) -> None:
        with self._lock:
            for client in self.clients.values():
                client.close()
            self.clients.clear()
            self.tools.clear()
            self._started = False
