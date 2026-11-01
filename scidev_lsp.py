"""Minimal LSP client used for on-demand Python diagnostics.

The client starts the installed Ruff language server for one document, waits
for its first publishDiagnostics notification, then shuts the server down. It
is intentionally one-shot: the desktop editor keeps its own debounce and never
blocks the UI thread waiting for a language server.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from scidev_process import background_process_options


class LSPUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class LSPDiagnostic:
    line: int
    column: int
    end_line: int
    end_column: int
    severity: str
    message: str
    code: str = ""
    source: str = "ruff"


SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
SUPPORTED_SUFFIXES = {".py", ".pyi"}


def document_uri(path: Path) -> str:
    return Path(path).resolve().as_uri()


def _encode_message(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _parse_diagnostics(payload: dict[str, Any]) -> list[LSPDiagnostic]:
    result: list[LSPDiagnostic] = []
    for item in payload.get("diagnostics") or []:
        if not isinstance(item, dict):
            continue
        start = item.get("range", {}).get("start", {}) if isinstance(item.get("range"), dict) else {}
        end = item.get("range", {}).get("end", {}) if isinstance(item.get("range"), dict) else {}
        try:
            line = max(1, int(start.get("line", 0)) + 1)
            column = max(1, int(start.get("character", 0)) + 1)
            end_line = max(line, int(end.get("line", start.get("line", 0))) + 1)
            end_column = max(1, int(end.get("character", start.get("character", 0))) + 1)
        except (TypeError, ValueError):
            continue
        message = str(item.get("message") or "LSP 诊断")[:1000]
        code = item.get("code")
        code_text = str(code)[:80] if code is not None else ""
        severity = SEVERITY.get(int(item.get("severity") or 1), "error")
        result.append(LSPDiagnostic(
            line=line,
            column=column,
            end_line=end_line,
            end_column=end_column,
            severity=severity,
            message=message,
            code=code_text,
            source=str(item.get("source") or "ruff")[:60],
        ))
        if len(result) >= 100:
            break
    return result


class LSPClient:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()
        self.process: subprocess.Popen[bytes] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._next_id = 1
        self._stderr_text = ""

    def start(self) -> None:
        command = [sys.executable, "-m", "ruff", "server"]
        try:
            self.process = subprocess.Popen(
                command,
                cwd=self.project_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                **background_process_options(),
            )
        except OSError as exc:
            raise LSPUnavailable(f"无法启动 Ruff LSP：{exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, name="scidev-lsp-reader", daemon=True)
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
            raise LSPUnavailable("Ruff LSP 未启动")
        self.process.stdin.write(_encode_message(payload))
        self.process.stdin.flush()

    def _next_request_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _read_until(self, deadline: float, predicate) -> dict[str, Any] | None:
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty:
                return None
            if predicate(message):
                return message
        return None

    def initialize(self, timeout: float = 5.0) -> None:
        request_id = self._next_request_id()
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "processId": os.getpid(),
                "rootUri": self.project_root.as_uri(),
                "capabilities": {
                    "textDocument": {"publishDiagnostics": {}},
                    "workspace": {"workspaceFolders": True},
                },
                "workspaceFolders": [{
                    "uri": self.project_root.as_uri(),
                    "name": self.project_root.name,
                }],
            },
        })
        response = self._read_until(
            time.monotonic() + timeout,
            lambda message: message.get("id") == request_id
            and ("result" in message or "error" in message),
        )
        if response is None:
            raise LSPUnavailable("Ruff LSP 初始化超时")
        if "error" in response:
            raise LSPUnavailable(f"Ruff LSP 初始化失败：{response['error']}")
        self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    def did_open(self, path: Path, text: str, language_id: str = "python") -> str:
        uri = document_uri(path)
        self._send({
            "jsonrpc": "2.0",
            "method": "textDocument/didOpen",
            "params": {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": 1,
                    "text": text,
                },
            },
        })
        return uri

    def did_change(self, path: Path, text: str, version: int) -> str:
        uri = document_uri(path)
        self._send({
            "jsonrpc": "2.0",
            "method": "textDocument/didChange",
            "params": {
                "textDocument": {"uri": uri, "version": max(1, int(version))},
                "contentChanges": [{"text": text}],
            },
        })
        return uri

    def wait_diagnostics(self, uri: str, timeout: float = 6.0) -> list[LSPDiagnostic]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty:
                break
            if message.get("method") != "textDocument/publishDiagnostics":
                continue
            params = message.get("params")
            if not isinstance(params, dict) or str(params.get("uri") or "") != uri:
                continue
            return _parse_diagnostics(params)
        return []

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            request_id = self._next_request_id()
            self._send({"jsonrpc": "2.0", "id": request_id, "method": "shutdown", "params": None})
            self._read_until(
                time.monotonic() + 1.0,
                lambda message: message.get("id") == request_id,
            )
            self._send({"jsonrpc": "2.0", "method": "exit", "params": None})
        except (OSError, LSPUnavailable):
            pass
        finally:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
            reader = self._reader
            if reader is not None and reader.is_alive():
                reader.join(timeout=1.0)
            for stream in (process.stdin, process.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            self.process = None
            self._reader = None


class ResidentLSP:
    """One long-lived Ruff process with coalesced didOpen/didChange requests."""

    def __init__(
        self,
        project_root: Path,
        callback: Callable[[Path, int, list[LSPDiagnostic], str], None],
        *,
        timeout: float = 3.0,
        client_factory: Callable[[Path], LSPClient] | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.callback = callback
        self.timeout = max(1.0, min(float(timeout), 10.0))
        self.client_factory = client_factory or LSPClient
        self._client: LSPClient | None = None
        self._queue: queue.Queue[tuple[str, Path, int, str] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._lock = threading.Lock()
        self._versions: dict[str, int] = {}
        self._opened: set[str] = set()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._closed = False
            self._thread = threading.Thread(target=self._run, name="scidev-resident-lsp", daemon=True)
            self._thread.start()

    def submit(self, path: Path, text: str, generation: int = 0) -> None:
        if self._closed:
            return
        self.start()
        self._queue.put((str(path), Path(path), int(generation), str(text)))

    def _ensure_client(self) -> LSPClient:
        if self._client is None:
            client = self.client_factory(self.project_root)
            client.start()
            client.initialize()
            self._client = client
            self._versions.clear()
            self._opened.clear()
        return self._client

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            latest: dict[str, tuple[str, Path, int, str]] = {item[0]: item}
            while True:
                try:
                    extra = self._queue.get_nowait()
                except queue.Empty:
                    break
                if extra is None:
                    self._queue.put(None)
                    break
                latest[extra[0]] = extra
            for _key, (raw_path, path, generation, text) in latest.items():
                try:
                    client = self._ensure_client()
                    uri = document_uri(path)
                    version = self._versions.get(uri, 0) + 1
                    if uri in self._opened:
                        client.did_change(path, text, version)
                    else:
                        client.did_open(path, text)
                        self._opened.add(uri)
                    self._versions[uri] = version
                    diagnostics = client.wait_diagnostics(uri, timeout=self.timeout)
                    self.callback(path, generation, diagnostics, "")
                except (LSPUnavailable, OSError, ValueError, RuntimeError) as exc:
                    self._close_client()
                    self.callback(path, generation, [], f"{type(exc).__name__}: {exc}")

    def _close_client(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close()
            except (OSError, RuntimeError):
                pass

    def close(self, timeout: float = 5.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            self._thread = None
        if thread is not None:
            self._queue.put(None)
            thread.join(timeout=max(0.1, timeout))
        self._close_client()


def collect_diagnostics(
    project_root: Path,
    path: Path,
    text: str,
    *,
    timeout: float = 6.0,
) -> list[LSPDiagnostic]:
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise LSPUnavailable(f"不支持的 LSP 文件类型：{path.suffix or '(none)'}")
    client = LSPClient(project_root)
    try:
        client.start()
        client.initialize()
        uri = client.did_open(path, text)
        wait_timeout = min(timeout, 2.0)
        return client.wait_diagnostics(uri, timeout=wait_timeout)
    except LSPUnavailable:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise LSPUnavailable(f"Ruff LSP 诊断失败：{exc}") from exc
    finally:
        client.close()
