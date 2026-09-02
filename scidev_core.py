"""SciDevHarness core services: coding agent, retry queue, Git and audit ledger."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


class RetryableError(RuntimeError):
    """An operation failed for a reason that may disappear later."""


class PermanentError(RuntimeError):
    """An operation should not be retried automatically."""


class EventLedger:
    """Append-only JSONL audit log plus a small SQLite query index."""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()
        self.research_dir = self.project_root / ".research"
        self.research_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.research_dir / "events.jsonl"
        self.db_path = self.research_dir / "runs.db"
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 1,
                    next_retry_at REAL NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    transcript_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def append(self, event_type: str, payload: dict[str, Any]) -> int:
        timestamp = now_iso()
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        line = json.dumps(
            {"timestamp": timestamp, "event_type": event_type, "payload": payload},
            ensure_ascii=False,
        )
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            with self._db() as conn:
                cursor = conn.execute(
                    "INSERT INTO events(timestamp, event_type, payload) VALUES (?, ?, ?)",
                    (timestamp, event_type, serialized),
                )
                return int(cursor.lastrowid)

    def create_task(
        self, kind: str, payload: dict[str, Any], max_attempts: int = 1
    ) -> str:
        task_id = new_id("task")
        timestamp = now_iso()
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, kind, payload, status, attempts, max_attempts,
                                  next_retry_at, created_at, updated_at)
                VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    max(1, int(max_attempts)),
                    time.time(),
                    timestamp,
                    timestamp,
                ),
            )
        self.append("task_created", {"task_id": task_id, "kind": kind, "payload": payload})
        return task_id

    def recover_running_tasks(self) -> None:
        """Put interrupted coding tasks back into the retry queue after a client crash."""
        timestamp = now_iso()
        with self._db() as conn:
            conn.execute(
                "UPDATE tasks SET status='retry_wait', next_retry_at=?, updated_at=? "
                "WHERE status='running' AND kind='coding'",
                (time.time(), timestamp),
            )
            conn.execute(
                "UPDATE tasks SET status='retry_wait', next_retry_at=?, updated_at=? "
                "WHERE status='running' AND kind='provider'",
                (time.time(), timestamp),
            )

    def claim_due_task(self) -> dict[str, Any] | None:
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN ('queued', 'retry_wait') AND next_retry_at <= ?
                ORDER BY next_retry_at, created_at
                LIMIT 1
                """,
                (time.time(),),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            attempts = int(row["attempts"]) + 1
            conn.execute(
                "UPDATE tasks SET status='running', attempts=?, updated_at=? WHERE task_id=?",
                (attempts, now_iso(), row["task_id"]),
            )
            conn.commit()
            return {
                "task_id": row["task_id"],
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
                "attempts": attempts,
                "max_attempts": int(row["max_attempts"]),
            }

    def complete_task(self, task_id: str) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE tasks SET status='succeeded', updated_at=? WHERE task_id=?",
                (now_iso(), task_id),
            )

    def retry_or_fail(
        self,
        task_id: str,
        error: str,
        retryable: bool,
        base_delay: float = 5.0,
    ) -> str:
        with self._db() as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                return "missing"
            attempts = int(row["attempts"])
            allowed = retryable and attempts < int(row["max_attempts"])
            if allowed:
                delay = min(300.0, max(0.05, base_delay) * (2 ** max(0, attempts - 1)))
                status = "retry_wait"
                next_retry = time.time() + delay
            else:
                delay = 0.0
                status = "failed"
                next_retry = time.time()
            conn.execute(
                """
                UPDATE tasks SET status=?, next_retry_at=?, last_error=?, updated_at=?
                WHERE task_id=?
                """,
                (status, next_retry, error[:2000], now_iso(), task_id),
            )
            return status

    def retry_now(self, task_id: str) -> bool:
        with self._db() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks SET status='queued', next_retry_at=?, last_error='', updated_at=?
                WHERE task_id=? AND status IN ('failed', 'retry_wait')
                """,
                (time.time(), now_iso(), task_id),
            )
            return cursor.rowcount == 1

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def create_session(self, session_id: str, prompt: str, transcript_path: str) -> None:
        timestamp = now_iso()
        with self._db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sessions(
                    session_id, status, prompt, transcript_path, created_at, updated_at
                ) VALUES (?, 'active', ?, ?, ?, ?)
                """,
                (
                    session_id,
                    prompt,
                    transcript_path,
                    timestamp,
                    timestamp,
                ),
            )

    def update_session(self, session_id: str, status: str, prompt: str, transcript_path: str) -> None:
        with self._db() as conn:
            conn.execute(
                """
                UPDATE sessions SET status=?, prompt=?, transcript_path=?, updated_at=?
                WHERE session_id=?
                """,
                (
                    status,
                    prompt,
                    transcript_path,
                    now_iso(),
                    session_id,
                ),
            )

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


@dataclass
class RetryQueue:
    ledger: EventLedger
    handlers: dict[str, Callable[[dict[str, Any]], Any]]
    event_callback: Callable[[str, dict[str, Any]], None] | None = None
    retry_base_seconds: float = 5.0

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.ledger.recover_running_tasks()
        self._thread = threading.Thread(target=self._worker, name="scidev-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def submit(self, kind: str, payload: dict[str, Any], max_attempts: int = 1) -> str:
        task_id = self.ledger.create_task(kind, payload, max_attempts=max_attempts)
        self._wake.set()
        return task_id

    def wake(self) -> None:
        self._wake.set()

    def _notify(self, event: str, data: dict[str, Any]) -> None:
        if self.event_callback:
            self.event_callback(event, data)

    def _worker(self) -> None:
        while not self._stop.is_set():
            task = self.ledger.claim_due_task()
            if task is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            task_id = task["task_id"]
            self.ledger.append("task_started", {"task_id": task_id, "attempt": task["attempts"]})
            self._notify("task_started", task)
            try:
                handler = self.handlers.get(task["kind"])
                if handler is None:
                    raise PermanentError(f"未知任务类型：{task['kind']}")
                result = handler(task)
                self.ledger.complete_task(task_id)
                self.ledger.append("task_completed", {"task_id": task_id})
                self._notify("task_completed", {**task, "result": result})
            except PermanentError as exc:
                error = str(exc)
                self.ledger.retry_or_fail(task_id, error, retryable=False)
                self.ledger.append("task_failed", {"task_id": task_id, "error": error})
                self._notify("task_failed", {**task, "error": error, "retryable": False})
            except Exception as exc:  # noqa: BLE001 - worker must keep running
                error = f"{type(exc).__name__}: {exc}"
                status = self.ledger.retry_or_fail(
                    task_id, error, retryable=True, base_delay=self.retry_base_seconds
                )
                self.ledger.append(
                    "task_retry_scheduled" if status == "retry_wait" else "task_failed",
                    {"task_id": task_id, "error": error, "status": status},
                )
                self._notify(
                    "task_retry" if status == "retry_wait" else "task_failed",
                    {**task, "error": error, "status": status, "retryable": True},
                )


class GitManager:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()

    def _run(self, args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )

    def is_repo(self) -> bool:
        result = self._run(["rev-parse", "--is-inside-work-tree"])
        return result.returncode == 0 and result.stdout.strip() == "true"

    def init(self) -> str:
        if not self.is_repo():
            self._run(["init"], check=True)
        if self._run(["config", "--get", "user.name"]).returncode != 0:
            self._run(["config", "user.name", "SciDevHarness"], check=True)
        if self._run(["config", "--get", "user.email"]).returncode != 0:
            self._run(["config", "user.email", "scidev@localhost"], check=True)
        return self.status()

    def head_sha(self) -> str:
        if not self.is_repo():
            return ""
        result = self._run(["rev-parse", "HEAD"])
        return result.stdout.strip() if result.returncode == 0 else ""

    def branch(self) -> str:
        if not self.is_repo():
            return "-"
        result = self._run(["branch", "--show-current"])
        return result.stdout.strip() or "(detached)"

    def status(self) -> str:
        if not self.is_repo():
            return "未初始化 Git 仓库"
        result = self._run(["status", "--short"])
        return result.stdout.strip() or "工作区干净"

    def log(self, limit: int = 20) -> str:
        if not self.is_repo() or not self.head_sha():
            return "暂无提交"
        result = self._run(["log", f"-{limit}", "--oneline", "--decorate"])
        return result.stdout.strip()

    def commit_changes(self, message: str) -> str:
        self.init()
        self._run(["add", "-A"], check=True)
        staged = self._run(["diff", "--cached", "--quiet"])
        if staged.returncode == 0:
            return self.head_sha()
        self._run(["commit", "-m", message], check=True)
        return self.head_sha()


class OpenAICompatibleProvider:
    """OpenAI-compatible Chat Completions adapter with function/tool calling."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 90.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "OpenAICompatibleProvider":
        api_key = (os.getenv("SCIDEV_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
        base_url = (
            os.getenv("SCIDEV_API_BASE")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).strip()
        model = (os.getenv("SCIDEV_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5").strip()
        if not api_key:
            raise PermanentError("未配置 SCIDEV_API_KEY 或 OPENAI_API_KEY")
        return cls(base_url, api_key, model)

    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 12000,
        request_id: str = "",
    ) -> dict[str, Any]:
        body_data: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "stream": False,
        }
        if tools:
            body_data["tools"] = tools
            body_data["tool_choice"] = "auto"
        body = json.dumps(
            body_data,
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if request_id:
            headers["Idempotency-Key"] = request_id
        request = Request(
            self._endpoint(),
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 429 or exc.code >= 500:
                raise RetryableError(f"HTTP {exc.code}: {detail}") from exc
            raise PermanentError(f"HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RetryableError(f"网络连接失败：{exc}") from exc
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PermanentError("Provider 返回格式不包含 choices[0].message") from exc
        if not isinstance(message, dict):
            raise PermanentError("Provider 返回的 message 格式无效")
        return message

    def call(self, prompt: str, max_tokens: int = 12000) -> str:
        """Compatibility helper for a plain, non-tool request."""
        message = self.chat([{"role": "user", "content": prompt}], max_tokens=max_tokens)
        content = message.get("content") or ""
        return str(content)


class CodingToolbox:
    """Tools exposed to the model; every path is constrained to the project root."""

    MAX_READ_BYTES = 240_000
    MAX_WRITE_BYTES = 360_000
    EXCLUDED_NAMES = {".git", ".research", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}

    def __init__(self, project_root: Path, ledger: EventLedger):
        self.project_root = Path(project_root).resolve()
        self.ledger = ledger

    @staticmethod
    def definitions() -> list[dict[str, Any]]:
        def function(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None):
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required or [],
                        "additionalProperties": False,
                    },
                },
            }

        return [
            function(
                "list_files",
                "列出项目中的文件和目录。先用它了解结构；默认忽略 .git、.research、缓存和虚拟环境。",
                {
                    "path": {"type": "string", "description": "相对项目根目录的路径，默认为项目根目录"},
                    "depth": {"type": "integer", "description": "递归深度，1 到 5，默认 3"},
                },
            ),
            function(
                "read_file",
                "读取项目中的文本文件，可指定行号范围。不要读取二进制文件或密钥。",
                {
                    "path": {"type": "string", "description": "相对项目根目录的文件路径"},
                    "start_line": {"type": "integer", "description": "起始行号，从 1 开始，默认 1"},
                    "end_line": {"type": "integer", "description": "结束行号，默认最多读取 400 行"},
                },
                ["path"],
            ),
            function(
                "write_file",
                "创建或完整重写一个文本文件。适合新文件或小文件；已有大文件优先使用 replace_in_file。",
                {
                    "path": {"type": "string", "description": "相对项目根目录的文件路径"},
                    "content": {"type": "string", "description": "文件的完整 UTF-8 内容"},
                },
                ["path", "content"],
            ),
            function(
                "replace_in_file",
                "在文件中进行精确文本替换。old_text 必须与文件中的内容完全匹配。",
                {
                    "path": {"type": "string", "description": "相对项目根目录的文件路径"},
                    "old_text": {"type": "string", "description": "需要被替换的原文"},
                    "new_text": {"type": "string", "description": "替换后的文本"},
                    "replace_all": {"type": "boolean", "description": "是否替换全部匹配，默认 false"},
                },
                ["path", "old_text", "new_text"],
            ),
            function(
                "run_command",
                "在项目根目录执行必要的测试、检查或构建命令。不要运行未被请求的科研长实验。",
                {
                    "command": {"type": "string", "description": "要执行的命令"},
                    "timeout_seconds": {"type": "integer", "description": "超时秒数，默认 120，最大 300"},
                },
                ["command"],
            ),
            function(
                "git_diff",
                "查看当前工作区状态和代码差异，用于确认修改内容。",
                {},
            ),
        ]

    def _resolve(self, raw_path: str, allow_root: bool = True) -> Path:
        raw_path = str(raw_path or ".").strip()
        candidate = (self.project_root / raw_path).resolve()
        try:
            relative = candidate.relative_to(self.project_root)
        except ValueError as exc:
            raise PermanentError(f"路径越过项目根目录：{raw_path}") from exc
        if not allow_root and candidate == self.project_root:
            raise PermanentError("这里需要文件路径，不能使用项目根目录")
        if relative.parts and relative.parts[0] in self.EXCLUDED_NAMES:
            raise PermanentError(f"禁止访问内部目录：{relative.parts[0]}")
        return candidate

    def list_files(self, path: str = ".", depth: int = 3) -> str:
        base = self._resolve(path)
        if not base.exists():
            return f"路径不存在：{path}"
        if base.is_file():
            return str(base.relative_to(self.project_root))
        max_depth = max(1, min(int(depth or 3), 5))
        result: list[str] = []
        queue_items: list[tuple[Path, int, str]] = [(base, 0, "")]
        while queue_items and len(result) < 500:
            directory, current_depth, prefix = queue_items.pop(0)
            try:
                children = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
            except OSError as exc:
                result.append(f"{prefix}[无法读取：{exc}]")
                continue
            for child in children:
                if child.name in self.EXCLUDED_NAMES or child.is_symlink():
                    continue
                marker = "目录" if child.is_dir() else f"文件 {child.stat().st_size} bytes"
                result.append(f"{prefix}{child.name}{'/' if child.is_dir() else ''}  [{marker}]")
                if child.is_dir() and current_depth + 1 < max_depth:
                    queue_items.append((child, current_depth + 1, prefix + "  "))
        if len(result) >= 500:
            result.append("[输出已截断：项目文件超过 500 项]")
        return "\n".join(result) or "（空目录）"

    def read_file(self, path: str, start_line: int = 1, end_line: int = 400) -> str:
        target = self._resolve(path, allow_root=False)
        if not target.exists() or not target.is_file():
            return f"文件不存在：{path}"
        raw = target.read_bytes()
        if len(raw) > self.MAX_READ_BYTES:
            raise PermanentError(f"文件过大（{len(raw)} bytes），请只读取相关文件或拆分读取")
        if b"\x00" in raw:
            raise PermanentError(f"不读取二进制文件：{path}")
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, int(start_line or 1))
        end = min(len(lines), max(start, int(end_line or 400)), start + 399)
        selected = [f"{number}: {lines[number - 1]}" for number in range(start, end + 1)]
        return "\n".join(selected) or "（空文件）"

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path, allow_root=False)
        encoded = str(content).encode("utf-8")
        if len(encoded) > self.MAX_WRITE_BYTES:
            raise PermanentError(f"拒绝写入过大文件（上限 {self.MAX_WRITE_BYTES} bytes）")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        relative = target.relative_to(self.project_root).as_posix()
        self.ledger.append("file_changed", {"operation": "write", "path": relative, "bytes": len(encoded)})
        return f"已写入 {relative}（{len(encoded)} bytes）"

    def replace_in_file(self, path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        target = self._resolve(path, allow_root=False)
        if not target.exists() or not target.is_file():
            raise PermanentError(f"文件不存在：{path}")
        raw = target.read_bytes()
        if len(raw) > self.MAX_WRITE_BYTES:
            raise PermanentError(f"文件过大（{len(raw)} bytes），请使用更小范围的编辑")
        text = raw.decode("utf-8", errors="replace")
        count = text.count(old_text)
        if count == 0:
            raise PermanentError(f"old_text 在 {path} 中未找到，请先重新读取文件")
        if count > 1 and not replace_all:
            raise PermanentError(f"old_text 在 {path} 中匹配 {count} 次，请提供更精确文本或明确 replace_all=true")
        updated = text.replace(old_text, new_text, -1 if replace_all else 1)
        target.write_text(updated, encoding="utf-8")
        relative = target.relative_to(self.project_root).as_posix()
        self.ledger.append("file_changed", {"operation": "replace", "path": relative, "matches": count})
        return f"已修改 {relative}（匹配 {count} 次）"

    def run_command(self, command: str, timeout_seconds: int = 120) -> str:
        command = str(command or "").strip()
        if not command:
            raise PermanentError("命令不能为空")
        lowered = command.lower()
        blocked = ("git reset --hard", "git clean -fd", "rm -rf", "rmdir /s", "del /s")
        if any(fragment in lowered for fragment in blocked):
            raise PermanentError("为保护项目，拒绝执行破坏性命令")
        timeout = max(1, min(int(timeout_seconds or 120), 300))
        self.ledger.append("tool_called", {"tool": "run_command", "command": command})
        try:
            process = subprocess.Popen(
                command,
                cwd=self.project_root,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
                return f"命令超时（{timeout}s），已终止。\n{output[-20000:]}"
        except OSError as exc:
            raise PermanentError(f"命令启动失败：{exc}") from exc
        output = (output or "")[-24000:]
        return f"退出码：{process.returncode}\n{output}".rstrip()

    def git_diff(self) -> str:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        diff = subprocess.run(
            ["git", "diff", "--no-ext-diff"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        status = result.stdout.strip() or "工作区干净或尚未初始化 Git"
        changes = diff.stdout.strip() or "（没有已跟踪文件的 diff；新文件请结合 git status 判断）"
        return f"状态：\n{status}\n\nDiff：\n{changes[-30000:]}"

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        handlers = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "write_file": self.write_file,
            "replace_in_file": self.replace_in_file,
            "run_command": self.run_command,
            "git_diff": self.git_diff,
        }
        handler = handlers.get(name)
        if handler is None:
            raise PermanentError(f"未知工具：{name}")
        return str(handler(**arguments))


class CodingAgent:
    """Codex-style coding loop: inspect, edit, run checks, inspect diff, commit."""

    MAX_TURNS = 32
    SYSTEM_PROMPT = """你是 SciDevHarness 的本地科研编码 Agent，工作方式类似 Codex。

你的任务是直接帮助用户修改当前项目代码，而不是泛泛解释代码。你可以使用工具查看文件、精确编辑文件、运行必要的测试/检查命令和查看 Git diff。

工作规则：
1. 先了解项目结构并读取相关文件，再修改代码；不要凭空猜文件内容。
2. 只修改完成用户请求所需的文件；不要触碰 .git、.research、密钥、环境变量和数据集。
3. 修改后运行与本次改动相关的最小测试或静态检查。除非用户明确要求，不要运行长时间科研实验、训练或下载大文件。
4. 遇到工具报错，分析报错并修复；不要假装已经完成。
5. 最后简要说明改了什么、验证了什么、仍有什么限制。所有文件修改和工具调用都会被本地记录。
"""

    def __init__(
        self,
        project_root: Path,
        ledger: EventLedger,
        git: GitManager,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.ledger = ledger
        self.git = git
        self.event_callback = event_callback
        self.toolbox = CodingToolbox(self.project_root, ledger)

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self.event_callback:
            self.event_callback(event, data)

    def _session_path(self, session_id: str) -> Path:
        return self.project_root / ".research" / "sessions" / f"{session_id}.json"

    def _workspace_context(self) -> str:
        instruction = ""
        instruction_path = self.project_root / "AGENTS.md"
        if instruction_path.exists():
            instruction = instruction_path.read_text(encoding="utf-8", errors="replace")[:9000]
        return (
            f"项目根目录：{self.project_root}\n\n"
            f"当前文件树：\n{self.toolbox.list_files('.', 3)}\n\n"
            f"Git 状态：\n{self.git.status()}\n\n"
            f"项目指令 AGENTS.md：\n{instruction or '未找到'}"
        )

    def _save_session(self, session: dict[str, Any]) -> None:
        path = self._session_path(session["session_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.ledger.update_session(
            session["session_id"],
            session["status"],
            session["prompt"],
            path.relative_to(self.project_root).as_posix(),
        )

    def _load_session(self, session_id: str, prompt: str) -> dict[str, Any]:
        path = self._session_path(session_id)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PermanentError(f"无法恢复编码会话：{exc}") from exc
        session = {
            "session_id": session_id,
            "prompt": prompt,
            "status": "active",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "last_prompt": prompt,
            "git_base_sha": self.git.head_sha(),
            "git_result_sha": "",
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": f"工作区上下文：\n{self._workspace_context()}\n\n用户任务：\n{prompt}"},
            ],
        }
        self.ledger.create_session(
            session_id,
            prompt,
            path.relative_to(self.project_root).as_posix(),
        )
        self.ledger.append("coding_session_started", {"session_id": session_id, "prompt": prompt})
        self._save_session(session)
        return session

    @staticmethod
    def _text_content(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                    chunks.append(str(item.get("text", "")))
            return "\n".join(chunks)
        return str(content or "")

    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        payload = task["payload"]
        session_id = payload["session_id"]
        prompt = payload["prompt"]
        session = self._load_session(session_id, prompt)
        if session.get("last_prompt") != prompt:
            session["messages"].append(
                {
                    "role": "user",
                    "content": f"继续当前编码会话。\n最新工作区上下文：\n{self._workspace_context()}\n\n新任务：\n{prompt}",
                }
            )
            session["last_prompt"] = prompt
            session["prompt"] = prompt
        session["status"] = "active"
        session["updated_at"] = now_iso()
        self._save_session(session)
        provider = OpenAICompatibleProvider.from_env()
        messages = session["messages"]

        for turn in range(1, self.MAX_TURNS + 1):
            self.ledger.append("model_call_started", {"session_id": session_id, "turn": turn})
            self._emit("model_call_started", {"session_id": session_id, "turn": turn})
            message = provider.chat(
                messages,
                tools=self.toolbox.definitions(),
                max_tokens=12000,
                request_id=f"{session_id}-turn-{turn}",
            )
            content = self._text_content(message.get("content"))
            assistant_message: dict[str, Any] = {"role": "assistant", "content": content}
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            messages.append(assistant_message)
            if content:
                self._emit("assistant", {"session_id": session_id, "text": content})
            self._save_session(session)

            if not tool_calls:
                summary = " ".join(prompt.split())[:72] or session_id
                session["status"] = "completed"
                session["final_message"] = content
                session["git_result_sha"] = self.git.commit_changes(f"[codex] {summary}")
                session["updated_at"] = now_iso()
                self._save_session(session)
                if session["git_result_sha"]:
                    self.ledger.append(
                        "git_commit_created",
                        {"session_id": session_id, "git_result_sha": session["git_result_sha"]},
                    )
                self.ledger.append(
                    "coding_session_completed",
                    {"session_id": session_id, "git_result_sha": session["git_result_sha"]},
                )
                self._emit("completed", {"session_id": session_id, "text": content, "git_result_sha": session["git_result_sha"]})
                return {"session_id": session_id, "git_result_sha": session["git_result_sha"], "text": content}

            for call in tool_calls:
                function = call.get("function", {}) if isinstance(call, dict) else {}
                name = str(function.get("name", ""))
                call_id = str(call.get("id", new_id("tool")))
                raw_arguments = function.get("arguments", "{}")
                try:
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                    if not isinstance(arguments, dict):
                        raise ValueError("工具参数必须是 JSON 对象")
                    self._emit("tool_started", {"session_id": session_id, "name": name, "arguments": arguments})
                    result = self.toolbox.execute(name, arguments)
                except Exception as exc:  # Tool errors go back to the model for correction.
                    result = f"工具执行失败：{type(exc).__name__}: {exc}"
                result = str(result)[-30000:]
                messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
                self.ledger.append(
                    "tool_result",
                    {"session_id": session_id, "name": name, "result": result[:3000]},
                )
                self._emit("tool_result", {"session_id": session_id, "name": name, "result": result})
                self._save_session(session)

        session["status"] = "failed"
        session["error"] = f"超过最大 Agent 步数：{self.MAX_TURNS}"
        self._save_session(session)
        raise PermanentError(session["error"])


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
