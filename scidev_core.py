"""SciDevHarness core services: coding agent, retry queue, Git and audit ledger."""

from __future__ import annotations

import hashlib
import json
import difflib
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_EXCLUDED_PATH_NAMES = frozenset(
    {".git", ".research", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}
)
DEFAULT_SENSITIVE_PATH_NAMES = frozenset(
    {
        ".aws",
        ".azure",
        ".envrc",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".ssh",
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "secrets",
        "secrets.json",
    }
)
DEFAULT_SAFE_ENV_TEMPLATES = (".example", ".sample", ".template", ".dist")


def _is_sensitive_component(
    name: str,
    sensitive_names: set[str] | frozenset[str] = DEFAULT_SENSITIVE_PATH_NAMES,
    safe_env_templates: tuple[str, ...] = DEFAULT_SAFE_ENV_TEMPLATES,
) -> bool:
    folded = name.casefold()
    if folded in sensitive_names or folded == ".env":
        return True
    return folded.startswith(".env.") and not folded.endswith(safe_env_templates)


def _is_protected_workspace_path(
    raw_path: str | Path,
    excluded_names: set[str] | frozenset[str] = DEFAULT_EXCLUDED_PATH_NAMES,
    sensitive_names: set[str] | frozenset[str] = DEFAULT_SENSITIVE_PATH_NAMES,
    safe_env_templates: tuple[str, ...] = DEFAULT_SAFE_ENV_TEMPLATES,
) -> bool:
    components = str(raw_path).replace("\\", "/").split("/")
    excluded = {name.casefold() for name in excluded_names}
    return any(
        component.casefold() in excluded
        or _is_sensitive_component(component, sensitive_names, safe_env_templates)
        for component in components
        if component and component not in {".", ".."}
    )


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
        self._lifecycle_lock = threading.Lock()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                if self._stop.is_set():
                    raise RuntimeError("RetryQueue 的旧 worker 仍在停止，请等待其退出后再启动")
                return
            self._stop.clear()
            self._wake.clear()
            self.ledger.recover_running_tasks()
            self._thread = threading.Thread(target=self._worker, name="scidev-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

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
    MAX_UNTRACKED_DIFF_BYTES = 240_000
    MAX_AUTO_COMMIT_FILE_BYTES = MAX_UNTRACKED_DIFF_BYTES

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()

    def _run(
        self,
        args: list[str],
        check: bool = False,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["git", *args]
        try:
            return subprocess.run(
                command,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=check,
                env=env,
            )
        except OSError as exc:
            # A desktop client should still open when Git is not installed.
            # Read-only probes then behave like an unavailable repository;
            # mutating calls keep a useful, conventional command error.
            if check:
                raise subprocess.CalledProcessError(
                    127,
                    command,
                    output="",
                    stderr=str(exc),
                ) from exc
            return subprocess.CompletedProcess(command, 127, "", str(exc))

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

    def status(self, *, protect_sensitive: bool = False) -> str:
        probe = self._run(["rev-parse", "--is-inside-work-tree"])
        if probe.returncode == 127:
            return "Git 不可用：请安装 Git CLI"
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return "未初始化 Git 仓库"
        if protect_sensitive:
            paths = self.status_paths()
            safe_paths = sorted(path for path in paths if not _is_protected_workspace_path(path))
            hidden_count = len(paths) - len(safe_paths)
            lines = [f"已变更：{path}" for path in safe_paths]
            if hidden_count:
                lines.append(f"[{hidden_count} 个敏感/内部路径已隐藏]")
            return "\n".join(lines) or "工作区干净"
        result = self._run(["status", "--short"])
        return result.stdout.strip() or "工作区干净"

    def status_entries(self) -> list[dict[str, str]]:
        """Return machine-readable porcelain entries for the source-control view."""
        if not self.is_repo():
            return []
        result = self._run(["status", "--porcelain=v1", "-uall"])
        entries: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            code = line[:2]
            raw_path = line[3:]
            path = raw_path.split(" -> ", 1)[-1]
            entries.append({"code": code, "path": path, "raw": raw_path})
        return entries

    def status_path_groups(self) -> list[set[str]]:
        """Return changed paths grouped by status record, keeping renames together."""
        if not self.is_repo():
            return []
        result = self._run(["status", "--porcelain=v1", "-z", "-uall"])
        records = result.stdout.split("\x00")
        groups: list[set[str]] = []
        index = 0
        while index < len(records):
            record = records[index]
            if len(record) >= 4:
                group = {record[3:]}
                if "R" in record[:2] or "C" in record[:2]:
                    index += 1
                    if index < len(records) and records[index]:
                        group.add(records[index])
                groups.append(group)
            index += 1
        return groups

    def status_paths(self) -> set[str]:
        """Return exact changed paths, using NUL-delimited porcelain for unusual filenames."""
        groups = self.status_path_groups()
        return set().union(*groups) if groups else set()

    def _sensitive_rename_groups(self) -> list[set[str]]:
        """Detect sensitive-file renames that porcelain reports as delete + untracked add."""
        changed_paths = self.status_paths()
        if not self.head_sha() or not any(
            _is_protected_workspace_path(path) for path in changed_paths
        ):
            return []

        candidates: list[str] = []
        for relative in sorted(changed_paths):
            unresolved = self.project_root / Path(relative)
            try:
                target = unresolved.resolve(strict=False)
                if not target.is_relative_to(self.project_root):
                    continue
                if unresolved.is_symlink() or unresolved.is_junction():
                    continue
                if target.exists() and target.is_file() and target.stat().st_size > self.MAX_AUTO_COMMIT_FILE_BYTES:
                    continue
            except (OSError, RuntimeError, ValueError):
                continue
            candidates.append(relative)
        if len(candidates) < 2:
            return []

        detected: list[set[str]] = []
        with tempfile.TemporaryDirectory(prefix="scidev-git-rename-check-") as temp_dir:
            environment = os.environ.copy()
            environment["GIT_INDEX_FILE"] = str(Path(temp_dir) / "index")
            self._run(["read-tree", self.head_sha()], check=True, env=environment)
            self._run(
                ["--literal-pathspecs", "add", "-A", "--", *candidates],
                check=True,
                env=environment,
            )
            result = self._run(
                ["--literal-pathspecs", "diff", "--cached", "--find-renames", "--name-status", "-z", "HEAD"],
                env=environment,
            )
            records = result.stdout.split("\x00")
            index = 0
            while index < len(records):
                status = records[index]
                index += 1
                if not status:
                    continue
                path_count = 2 if status.startswith(("R", "C")) else 1
                group = {path for path in records[index:index + path_count] if path}
                index += path_count
                if len(group) > 1 and any(_is_protected_workspace_path(path) for path in group):
                    detected.append(group)
        return detected

    def stage_all(self) -> None:
        self.init()
        self._run(["add", "-A"], check=True)

    def stage_path(self, path: Path) -> None:
        self.init()
        relative = self._relative_path(path)
        self._run(["add", "--", relative], check=True)

    def unstage_all(self) -> None:
        if not self.is_repo():
            return
        if self.head_sha():
            self._run(["reset", "HEAD", "--"], check=True)
        else:
            self._run(["reset"], check=True)

    def unstage_path(self, path: Path) -> None:
        if not self.is_repo():
            return
        relative = self._relative_path(path)
        if self.head_sha():
            self._run(["reset", "HEAD", "--", relative], check=True)
        else:
            self._run(["reset", "--", relative], check=True)

    def _relative_path(self, path: Path) -> str:
        target = Path(path).resolve()
        if not target.is_relative_to(self.project_root):
            raise ValueError("path must be inside the project")
        return target.relative_to(self.project_root).as_posix()

    def log(self, limit: int = 20) -> str:
        if not self.is_repo() or not self.head_sha():
            return "暂无提交"
        result = self._run(["log", f"-{limit}", "--oneline", "--decorate"])
        return result.stdout.strip()

    def diff(self, path: Path | None = None, *, protect_sensitive: bool = False) -> str:
        """Return a reviewable diff for the whole worktree or one file."""
        if not self.is_repo():
            return "Git 尚未初始化"
        protected_changes: set[str] = set()
        selected_paths: list[str] = []
        if path is None and protect_sensitive:
            path_groups = self.status_path_groups()
            sensitive_rename_groups = self._sensitive_rename_groups()
            sensitive_rename_paths = set().union(*sensitive_rename_groups) if sensitive_rename_groups else set()
            changed_paths = set().union(*path_groups) if path_groups else set()
            selected_paths = sorted(
                relative
                for group in path_groups
                if not any(
                    _is_protected_workspace_path(item) or item in sensitive_rename_paths
                    for item in group
                )
                for relative in group
            )
            protected_changes = (changed_paths - set(selected_paths)) | sensitive_rename_paths
            if not selected_paths:
                return "敏感/内部路径的差异已隐藏" if protected_changes else ""

        args = ["--literal-pathspecs", "diff", "--no-ext-diff"]
        has_head = bool(self.head_sha())
        if has_head:
            # Comparing with HEAD includes both staged and unstaged changes,
            # which is what an IDE's file diff should show.
            args.append("HEAD")
        relative = ""
        if path is not None:
            target = Path(path).resolve()
            if not target.is_relative_to(self.project_root):
                return "拒绝读取项目目录之外的文件"
            relative = target.relative_to(self.project_root).as_posix()
            if protect_sensitive and _is_protected_workspace_path(relative):
                return "敏感/内部路径的差异已隐藏"
            args.extend(["--", relative])
        elif protect_sensitive:
            args.extend(["--", *selected_paths])
        result = self._run(args)
        diff_text = result.stdout
        if not has_head:
            staged_args = ["--literal-pathspecs", "diff", "--cached", "--no-ext-diff"]
            if relative:
                staged_args.extend(["--", relative])
            elif protect_sensitive:
                staged_args.extend(["--", *selected_paths])
            staged = self._run(staged_args)
            diff_text += staged.stdout
        if path is None:
            # ``git diff`` does not include untracked files. The Agent needs
            # their content in its review context, otherwise a newly-created
            # file can be invisible until the first commit.
            untracked = self._run(["ls-files", "--others", "--exclude-standard", "-z"])
            for raw_relative in untracked.stdout.split("\x00"):
                relative_path = raw_relative
                if not relative_path:
                    continue
                if protect_sensitive and _is_protected_workspace_path(relative_path):
                    continue
                unresolved = self.project_root / Path(relative_path)
                if unresolved.is_symlink():
                    continue
                candidate = unresolved.resolve()
                if (
                    not candidate.is_relative_to(self.project_root)
                    or not candidate.is_file()
                ):
                    continue
                try:
                    raw_content = candidate.read_bytes()
                except OSError:
                    continue
                if len(raw_content) > self.MAX_UNTRACKED_DIFF_BYTES or b"\x00" in raw_content:
                    continue
                content = raw_content.decode("utf-8", errors="replace").splitlines(keepends=True)
                diff_text += "".join(
                    difflib.unified_diff(
                        [],
                        content,
                        fromfile="/dev/null",
                        tofile=relative_path,
                    )
                )
        if diff_text.strip():
            return diff_text
        if path is not None and relative:
            status = self._run(["status", "--short", "--", relative]).stdout.strip()
            if status.startswith("??"):
                try:
                    content = Path(path).read_text(encoding="utf-8").splitlines(keepends=True)
                except (OSError, UnicodeError):
                    return f"{relative}\n\n新文件无法以文本 diff 展示"
                return "".join(
                    difflib.unified_diff(
                        [],
                        content,
                        fromfile="/dev/null",
                        tofile=relative,
                    )
                )
        return ""

    def commit_changes(self, message: str) -> str:
        self.init()
        self._run(["add", "-A"], check=True)
        staged = self._run(["diff", "--cached", "--quiet"])
        if staged.returncode == 0:
            return self.head_sha()
        self._run(["commit", "-m", message], check=True)
        return self.head_sha()

    def filter_auto_commit_paths(
        self,
        paths: set[str] | list[str] | tuple[str, ...],
    ) -> tuple[set[str], dict[str, dict[str, str | int]]]:
        """Keep unsafe, sensitive, linked, and oversized artifacts out of Agent commits."""
        included: set[str] = set()
        skipped: dict[str, dict[str, str | int]] = {}
        sensitive_rename_paths = {
            path
            for group in self._sensitive_rename_groups()
            if len(group) > 1 and any(_is_protected_workspace_path(path) for path in group)
            for path in group
        }
        for relative in sorted({str(path) for path in paths}):
            if _is_protected_workspace_path(relative) or relative in sensitive_rename_paths:
                skipped[relative] = {"reason": "protected_path"}
                continue

            unresolved = self.project_root / Path(relative)
            try:
                target = unresolved.resolve(strict=False)
                if not target.is_relative_to(self.project_root):
                    skipped[relative] = {"reason": "outside_workspace"}
                    continue
                if unresolved.is_symlink() or unresolved.is_junction():
                    skipped[relative] = {"reason": "linked_path"}
                    continue
                if target.exists() and target.is_file():
                    size_bytes = target.stat().st_size
                    if size_bytes > self.MAX_AUTO_COMMIT_FILE_BYTES:
                        skipped[relative] = {"reason": "file_too_large", "size_bytes": size_bytes}
                        continue
                    try:
                        content = target.read_bytes()
                        content.decode("utf-8")
                    except (OSError, UnicodeDecodeError):
                        skipped[relative] = {"reason": "non_text_file"}
                        continue
                    if b"\x00" in content:
                        skipped[relative] = {"reason": "non_text_file"}
                        continue
            except (OSError, RuntimeError, ValueError):
                skipped[relative] = {"reason": "path_unavailable"}
                continue
            included.add(relative)
        return included, skipped

    def commit_paths(self, message: str, paths: set[str] | list[str] | tuple[str, ...]) -> str:
        """Commit only the given paths without consuming the user's existing index."""
        relative_paths = sorted({self._relative_path(self.project_root / Path(path)) for path in paths})
        if not relative_paths:
            return ""

        self.init()
        base_sha = self.head_sha()
        with tempfile.TemporaryDirectory(prefix="scidev-git-index-") as temp_dir:
            environment = os.environ.copy()
            environment["GIT_INDEX_FILE"] = str(Path(temp_dir) / "index")
            read_tree = ["read-tree", base_sha] if base_sha else ["read-tree", "--empty"]
            self._run(read_tree, check=True, env=environment)

            included_paths = []
            for relative in relative_paths:
                ignored = self._run(["check-ignore", "-q", "--", relative])
                if ignored.returncode != 0:
                    included_paths.append(relative)
            if not included_paths:
                return ""

            self._run(
                ["--literal-pathspecs", "add", "-A", "--", *included_paths],
                check=True,
                env=environment,
            )
            staged = self._run(["diff", "--cached", "--quiet"], env=environment)
            if staged.returncode == 0:
                return ""
            if staged.returncode != 1:
                raise subprocess.CalledProcessError(staged.returncode, staged.args, staged.stdout, staged.stderr)
            self._run(["commit", "-m", message], check=True, env=environment)

        result_sha = self.head_sha()
        if result_sha:
            # The selected paths were clean at session start; refresh only those entries
            # so unrelated user staging and worktree edits remain untouched.
            self._run(
                ["--literal-pathspecs", "reset", "--quiet", "HEAD", "--", *included_paths],
                check=True,
            )
        return result_sha


@dataclass
class SummarySettings:
    """Project-local policy for the automatic end-of-conversation summary."""

    enabled: bool = True
    model: str = ""
    max_tokens: int = 12000
    context_chars: int = 18000
    retries: int = 2
    interval_turns: int = 4
    instruction: str = "请用中文总结本次编码对话，包含：目标、实际修改、验证结果、未完成事项和下一步建议。不要编造没有发生的事实。"

    @classmethod
    def config_path(cls, project_root: Path) -> Path:
        return Path(project_root).resolve() / ".research" / "settings.json"

    @classmethod
    def load(cls, project_root: Path) -> "SummarySettings":
        path = cls.config_path(project_root)
        data: dict[str, Any] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, json.JSONDecodeError):
            pass

        enabled = data.get("summary_enabled", cls.enabled)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"0", "false", "no", "off", ""}
        if os.getenv("SCIDEV_SUMMARY_ENABLED") is not None:
            enabled = os.getenv("SCIDEV_SUMMARY_ENABLED", "").strip().lower() not in {"", "0", "false", "no", "off"}
        model = str(os.getenv("SCIDEV_SUMMARY_MODEL") or data.get("summary_model") or "").strip()
        max_tokens = data.get("summary_max_tokens", cls.max_tokens)
        context_chars = data.get("summary_context_chars", cls.context_chars)
        retries = data.get("summary_retries", cls.retries)
        interval_turns = data.get("summary_interval_turns", cls.interval_turns)
        try:
            max_tokens = max(200, min(32000, int(max_tokens)))
        except (TypeError, ValueError):
            max_tokens = cls.max_tokens
        try:
            context_chars = max(4000, min(50000, int(context_chars)))
        except (TypeError, ValueError):
            context_chars = cls.context_chars
        try:
            retries = max(0, min(3, int(retries)))
        except (TypeError, ValueError):
            retries = cls.retries
        if os.getenv("SCIDEV_SUMMARY_INTERVAL_TURNS") is not None:
            interval_turns = os.getenv("SCIDEV_SUMMARY_INTERVAL_TURNS", "").strip()
        try:
            interval_turns = max(0, min(32, int(interval_turns)))
        except (TypeError, ValueError):
            interval_turns = cls.interval_turns
        instruction = str(data.get("summary_instruction") or cls.instruction).strip() or cls.instruction
        return cls(bool(enabled), model, max_tokens, context_chars, retries, interval_turns, instruction)

    def save(self, project_root: Path) -> None:
        path = self.config_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "summary_enabled": self.enabled,
                    "summary_model": self.model,
                    "summary_max_tokens": self.max_tokens,
                    "summary_context_chars": self.context_chars,
                    "summary_retries": self.retries,
                    "summary_interval_turns": self.interval_turns,
                    "summary_instruction": self.instruction,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


class OpenAICompatibleProvider:
    """OpenAI-compatible Chat Completions adapter with function/tool calling."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 90.0,
        text_tool_call_fallback: bool = False,
        streaming: bool = True,
        presence_penalty: float | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.text_tool_call_fallback = text_tool_call_fallback
        self.streaming = streaming
        if presence_penalty is not None and not -2.0 <= float(presence_penalty) <= 2.0:
            raise ValueError("presence_penalty must be between -2 and 2")
        self.presence_penalty = float(presence_penalty) if presence_penalty is not None else None

    def with_model(self, model: str) -> "OpenAICompatibleProvider":
        return type(self)(
            self.base_url,
            self.api_key,
            model.strip() or self.model,
            self.timeout,
            self.text_tool_call_fallback,
            self.streaming,
            self.presence_penalty,
        )

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
        raw_timeout = os.getenv("SCIDEV_REQUEST_TIMEOUT_SECONDS", "90").strip()
        try:
            request_timeout = float(raw_timeout)
        except ValueError as exc:
            raise PermanentError("SCIDEV_REQUEST_TIMEOUT_SECONDS 必须是 5 到 600 秒之间的数字") from exc
        if not 5 <= request_timeout <= 600:
            raise PermanentError("SCIDEV_REQUEST_TIMEOUT_SECONDS 必须是 5 到 600 秒之间的数字")
        raw_streaming = os.getenv("SCIDEV_STREAMING", "true").strip().lower()
        if raw_streaming not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise PermanentError("SCIDEV_STREAMING 必须是 true/false")
        streaming = raw_streaming in {"1", "true", "yes", "on"}
        text_tool_call_fallback = os.getenv("SCIDEV_TEXT_TOOL_CALL_FALLBACK", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        raw_presence_penalty = os.getenv("SCIDEV_PRESENCE_PENALTY", "").strip()
        presence_penalty = None
        if raw_presence_penalty:
            try:
                presence_penalty = float(raw_presence_penalty)
            except ValueError as exc:
                raise PermanentError("SCIDEV_PRESENCE_PENALTY 必须是 -2 到 2 之间的数字") from exc
            if not -2.0 <= presence_penalty <= 2.0:
                raise PermanentError("SCIDEV_PRESENCE_PENALTY 必须是 -2 到 2 之间的数字")
        return cls(
            base_url,
            api_key,
            model,
            timeout=request_timeout,
            text_tool_call_fallback=text_tool_call_fallback,
            streaming=streaming,
            presence_penalty=presence_penalty,
        )

    @staticmethod
    def _coerce_text_tool_calls(
        message: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Normalize explicitly tagged or fenced Qwen-style tool JSON when native calls are absent."""
        content = message.get("content")
        if not isinstance(content, str) or not content:
            return message

        allowed: dict[str, tuple[set[str], set[str]]] = {}
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if not isinstance(function, dict) or not function.get("name"):
                continue
            parameters = function.get("parameters") or {}
            properties = parameters.get("properties") or {}
            allowed[str(function["name"])] = (
                set(properties) if isinstance(properties, dict) else set(),
                set(parameters.get("required") or ()),
            )
        if not allowed:
            return message

        patterns = (
            re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.IGNORECASE | re.DOTALL),
            re.compile(r"<tool_request>\s*(.*?)\s*</tool_request>", re.IGNORECASE | re.DOTALL),
            re.compile(r"```(?:json|xml)\s*(.*?)```", re.IGNORECASE | re.DOTALL),
            # Some local models omit the requested wrapper. Only accept a JSON
            # object that occupies the entire response; prose/code examples
            # must never be promoted to executable workspace actions.
            re.compile(r"\A\s*(\{.*\})\s*\Z", re.DOTALL),
        )
        matches = sorted(
            (match for pattern in patterns for match in pattern.finditer(content)),
            key=lambda match: match.start(),
        )
        tool_calls: list[dict[str, Any]] = []
        consumed_spans: list[tuple[int, int]] = []
        for match in matches:
            candidate = match.group(1).strip()
            try:
                payload = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                # Local models occasionally append one redundant closing brace
                # to an otherwise valid tool envelope. Recover only a valid
                # JSON prefix followed by exactly that one character; tool and
                # argument allowlists below still decide whether it can run.
                try:
                    payload, end = json.JSONDecoder().raw_decode(candidate)
                except (json.JSONDecodeError, TypeError):
                    continue
                if candidate[end:].strip() != "}":
                    continue
            if (
                not isinstance(payload, dict)
                or set(payload) != {"name", "arguments"}
                or not isinstance(payload.get("name"), str)
            ):
                continue
            name = payload["name"]
            arguments = payload.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    continue
            if not isinstance(arguments, dict) or name not in allowed:
                continue
            properties, required = allowed[name]
            if required - arguments.keys() or arguments.keys() - properties:
                continue
            tool_calls.append(
                {
                    "id": new_id("call"),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            )
            consumed_spans.append((match.start(), match.end()))

        if not tool_calls:
            return message
        remaining = content
        for start, end in reversed(consumed_spans):
            remaining = remaining[:start] + remaining[end:]
        normalized = dict(message)
        normalized["content"] = remaining.strip()
        normalized["tool_calls"] = tool_calls
        return normalized

    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    @staticmethod
    def _stream_message(
        response: Any,
        on_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Assemble an OpenAI-compatible SSE response, including fragmented tool calls."""
        content_parts: list[str] = []
        tool_parts: dict[int, dict[str, Any]] = {}
        saw_done = False
        saw_finish = False
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
            line = line.strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                saw_done = True
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError as exc:
                raise PermanentError("Provider returned malformed SSE JSON") from exc
            if isinstance(chunk, dict) and chunk.get("error"):
                raise RetryableError(f"Provider stream error: {str(chunk['error'])[:500]}")
            choices = chunk.get("choices", []) if isinstance(chunk, dict) else []
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason") is not None:
                    saw_finish = True
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                text = delta.get("content")
                if isinstance(text, str) and text:
                    content_parts.append(text)
                    if on_delta:
                        on_delta(text)
                elif isinstance(text, list):
                    for part in text:
                        if not isinstance(part, dict) or part.get("type") not in {"text", "output_text"}:
                            continue
                        piece = str(part.get("text", ""))
                        if piece:
                            content_parts.append(piece)
                            if on_delta:
                                on_delta(piece)

                raw_tool_calls = delta.get("tool_calls") or []
                if not isinstance(raw_tool_calls, list):
                    continue
                for position, fragment in enumerate(raw_tool_calls):
                    if not isinstance(fragment, dict):
                        continue
                    try:
                        index = int(fragment.get("index", position))
                    except (TypeError, ValueError):
                        index = position
                    target = tool_parts.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if fragment.get("id"):
                        target["id"] = str(fragment["id"])
                    if fragment.get("type"):
                        target["type"] = str(fragment["type"])
                    function = fragment.get("function")
                    if isinstance(function, dict):
                        if function.get("name"):
                            target["function"]["name"] += str(function["name"])
                        if function.get("arguments") is not None:
                            target["function"]["arguments"] += str(function["arguments"])

        if not saw_done and not saw_finish:
            raise RetryableError("Provider stream ended before a completion marker")
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
        if tool_parts:
            message["tool_calls"] = [
                {
                    **tool_parts[index],
                    "id": tool_parts[index]["id"] or new_id("call"),
                }
                for index in sorted(tool_parts)
            ]
        return message

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 12000,
        request_id: str = "",
        on_delta: Callable[[str], None] | None = None,
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        body_data: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "stream": self.streaming,
        }
        if self.presence_penalty is not None:
            body_data["presence_penalty"] = self.presence_penalty
        if tools:
            body_data["tools"] = tools
            if tool_choice not in {"auto", "required"}:
                raise PermanentError("tool_choice must be 'auto' or 'required'")
            body_data["tool_choice"] = tool_choice
        if self.text_tool_call_fallback and tools:
            request_messages = [dict(message) for message in messages]
            protocol = (
                "\n\nLocal tool compatibility mode: you MUST use the declared tools for workspace actions. "
                "Never claim an action is complete until you receive that tool's result. If native tool calls "
                "are unavailable, emit exactly <tool_call>{\"name\":\"declared_function_name\","
                "\"arguments\":{...}}</tool_call> with valid JSON, outside Markdown fences."
            )
            system_message = next(
                (message for message in request_messages if message.get("role") == "system"),
                None,
            )
            if system_message is None:
                request_messages.insert(0, {"role": "system", "content": protocol.strip()})
            else:
                system_message["content"] = f"{system_message.get('content') or ''}{protocol}"
            body_data["messages"] = request_messages
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
                if self.streaming:
                    message = self._stream_message(response, on_delta=on_delta)
                else:
                    data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 429 or exc.code >= 500:
                raise RetryableError(f"HTTP {exc.code}: {detail}") from exc
            raise PermanentError(f"HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RetryableError(f"网络连接失败：{exc}") from exc
        if self.streaming:
            if self.text_tool_call_fallback and tools and not message.get("tool_calls"):
                message = self._coerce_text_tool_calls(message, tools)
            return message
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PermanentError("Provider 返回格式不包含 choices[0].message") from exc
        if not isinstance(message, dict):
            raise PermanentError("Provider 返回的 message 格式无效")
        if self.text_tool_call_fallback and tools and not message.get("tool_calls"):
            message = self._coerce_text_tool_calls(message, tools)
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
    EXCLUDED_NAMES = set(DEFAULT_EXCLUDED_PATH_NAMES)
    SENSITIVE_NAMES = set(DEFAULT_SENSITIVE_PATH_NAMES)
    SAFE_ENV_TEMPLATES = DEFAULT_SAFE_ENV_TEMPLATES

    def __init__(
        self,
        project_root: Path,
        ledger: EventLedger,
        command_approval: Callable[[str, int], bool] | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.ledger = ledger
        self.command_approval = command_approval

    @classmethod
    def _is_sensitive_name(cls, name: str) -> bool:
        return _is_sensitive_component(name, cls.SENSITIVE_NAMES, cls.SAFE_ENV_TEMPLATES)

    @classmethod
    def is_protected_path(cls, raw_path: str | Path) -> bool:
        return _is_protected_workspace_path(
            raw_path,
            cls.EXCLUDED_NAMES,
            cls.SENSITIVE_NAMES,
            cls.SAFE_ENV_TEMPLATES,
        )

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

        definitions = [
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
                "读取项目中的文本文件，可指定行号范围；返回内容带行号，行号仅为显示标记，不属于文件原文。不要读取二进制文件或密钥。",
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
                "在项目根目录执行必要的测试、检查或构建命令；每条命令都须经用户逐次批准。不要运行未被请求的科研长实验。",
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
        replace_tool = next(
            item for item in definitions if item["function"]["name"] == "replace_in_file"
        )
        replace_tool["function"]["description"] += (
            " For several independent edits in this same file, provide an edits array; "
            "the entire batch is validated before one write."
        )
        parameters = replace_tool["function"]["parameters"]
        parameters["properties"]["edits"] = {
            "type": "array",
            "description": "1-50 exact replacements, validated in order before a single file write",
            "minItems": 1,
            "maxItems": 50,
            "items": {
                "type": "object",
                "properties": {
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["old_text", "new_text"],
                "additionalProperties": False,
            },
        }
        parameters["required"] = ["path"]
        return definitions

    def _resolve(self, raw_path: str, allow_root: bool = True) -> Path:
        raw_path = str(raw_path or ".").strip()
        candidate = (self.project_root / raw_path).resolve()
        try:
            relative = candidate.relative_to(self.project_root)
        except ValueError as exc:
            raise PermanentError(f"路径越过项目根目录：{raw_path}") from exc
        if not allow_root and candidate == self.project_root:
            raise PermanentError("这里需要文件路径，不能使用项目根目录")
        excluded_names = {name.casefold() for name in self.EXCLUDED_NAMES}
        for component in relative.parts:
            if component.casefold() in excluded_names:
                raise PermanentError(f"禁止访问内部目录：{component}")
            if self._is_sensitive_name(component):
                raise PermanentError(f"禁止通过 Agent 文件工具访问敏感路径：{component}")
        return candidate

    @staticmethod
    def _decode_utf8_text(raw: bytes, path: Path) -> str:
        if b"\x00" in raw:
            raise PermanentError(f"拒绝将包含 NUL 字节的文件作为文本读取或编辑：{path.name}")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PermanentError(f"文件不是有效 UTF-8 文本，拒绝读取或编辑：{path.name}") from exc

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
            excluded_names = {name.casefold() for name in self.EXCLUDED_NAMES}
            for child in children:
                if (
                    child.name.casefold() in excluded_names
                    or self._is_sensitive_name(child.name)
                ):
                    continue
                try:
                    child.resolve(strict=False).relative_to(self.project_root)
                except (OSError, RuntimeError, ValueError):
                    # Do not expose names or metadata from links/junctions that
                    # resolve outside the workspace, even when is_symlink() is false.
                    continue
                if child.is_symlink() or child.is_junction():
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
        text = self._decode_utf8_text(raw, target)
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
        if b"\x00" in encoded:
            raise PermanentError(f"拒绝写入包含 NUL 字节的文本文件：{path}")
        if target.exists():
            if not target.is_file():
                raise PermanentError(f"目标不是普通文件，拒绝覆盖：{path}")
            if target.stat().st_size > self.MAX_WRITE_BYTES:
                raise PermanentError(f"目标文件过大，拒绝完整重写：{path}")
            self._decode_utf8_text(target.read_bytes(), target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        relative = target.relative_to(self.project_root).as_posix()
        self.ledger.append("file_changed", {"operation": "write", "path": relative, "bytes": len(encoded)})
        return f"已写入 {relative}（{len(encoded)} bytes）"

    def replace_in_file(
        self,
        path: str,
        old_text: str = "",
        new_text: str = "",
        replace_all: bool = False,
        edits: list[dict[str, Any]] | None = None,
    ) -> str:
        if edits is not None:
            if old_text or new_text or replace_all:
                raise PermanentError("Do not mix batch edits with single-replacement arguments")
            return self.replace_many_in_file(path, edits)
        target = self._resolve(path, allow_root=False)
        if not target.exists() or not target.is_file():
            raise PermanentError(f"文件不存在：{path}")
        if not old_text:
            raise PermanentError("拒绝使用空 old_text，避免意外插入或扩展整份文件")
        raw = target.read_bytes()
        if len(raw) > self.MAX_WRITE_BYTES:
            raise PermanentError(f"文件过大（{len(raw)} bytes），请使用更小范围的编辑")
        text = self._decode_utf8_text(raw, target)
        matched_old_text = old_text
        count = text.count(matched_old_text)
        if count == 0 and "\n" in old_text and "\r" not in old_text:
            # read_file presents normalized lines, while Windows source files
            # commonly contain CRLF bytes. Match the source's actual newline
            # style when the model supplies an otherwise exact LF snippet.
            for newline in ("\r\n", "\r"):
                candidate = old_text.replace("\n", newline)
                candidate_count = text.count(candidate)
                if candidate_count:
                    matched_old_text = candidate
                    count = candidate_count
                    if "\n" in new_text and "\r" not in new_text:
                        new_text = new_text.replace("\n", newline)
                    break
        if count == 0:
            raise PermanentError(f"old_text 在 {path} 中未找到，请先重新读取文件")
        if count > 1 and not replace_all:
            raise PermanentError(f"old_text 在 {path} 中匹配 {count} 次，请提供更精确文本或明确 replace_all=true")
        updated = text.replace(matched_old_text, new_text, -1 if replace_all else 1)
        encoded = updated.encode("utf-8")
        if len(encoded) > self.MAX_WRITE_BYTES:
            raise PermanentError(f"拒绝写入过大文件（上限 {self.MAX_WRITE_BYTES} bytes）")
        if b"\x00" in encoded:
            raise PermanentError(f"拒绝写入包含 NUL 字节的文本文件：{path}")
        target.write_bytes(encoded)
        relative = target.relative_to(self.project_root).as_posix()
        self.ledger.append("file_changed", {"operation": "replace", "path": relative, "matches": count})
        return f"已修改 {relative}（匹配 {count} 次）"

    def replace_many_in_file(self, path: str, edits: list[dict[str, Any]]) -> str:
        if not isinstance(edits, list) or not 1 <= len(edits) <= 50:
            raise PermanentError("Batch edits must contain between 1 and 50 replacements")
        normalized: list[tuple[str, str, bool]] = []
        input_characters = 0
        for edit in edits:
            if not isinstance(edit, dict):
                raise PermanentError("Each batch edit must be an object")
            if set(edit) - {"old_text", "new_text", "replace_all"}:
                raise PermanentError("Batch edits contain unsupported fields")
            before = edit.get("old_text")
            after = edit.get("new_text")
            replace_all = edit.get("replace_all", False)
            if not isinstance(before, str) or not before or not isinstance(after, str):
                raise PermanentError("Each batch edit requires non-empty old_text and string new_text")
            if not isinstance(replace_all, bool):
                raise PermanentError("replace_all must be a boolean")
            input_characters += len(before) + len(after)
            if input_characters > self.MAX_WRITE_BYTES:
                raise PermanentError("Combined batch edit text exceeds the file edit size limit")
            normalized.append((before, after, replace_all))

        target = self._resolve(path, allow_root=False)
        if not target.exists() or not target.is_file():
            raise PermanentError(f"File not found: {path}")
        raw = target.read_bytes()
        if len(raw) > self.MAX_WRITE_BYTES:
            raise PermanentError(f"File is too large to edit: {path}")
        original = self._decode_utf8_text(raw, target)
        updated = original
        total_matches = 0

        for before, after, replace_all in normalized:
            matched_before = before
            matched_after = after
            count = updated.count(matched_before)
            if count == 0 and "\n" in before and "\r" not in before:
                for newline in ("\r\n", "\r"):
                    candidate = before.replace("\n", newline)
                    candidate_count = updated.count(candidate)
                    if candidate_count:
                        matched_before = candidate
                        count = candidate_count
                        if "\n" in after and "\r" not in after:
                            matched_after = after.replace("\n", newline)
                        break
            if count == 0:
                raise PermanentError(f"Exact old_text not found in {path}; reread the file before retrying")
            if count > 1 and not replace_all:
                raise PermanentError(
                    f"old_text matched {count} times in {path}; provide more context or set replace_all=true"
                )
            updated = updated.replace(matched_before, matched_after, -1 if replace_all else 1)
            if "\x00" in updated or len(updated.encode("utf-8")) > self.MAX_WRITE_BYTES:
                raise PermanentError("Batch edit would create a binary or oversized text file")
            total_matches += count

        target.write_bytes(updated.encode("utf-8"))
        relative = target.relative_to(self.project_root).as_posix()
        self.ledger.append(
            "file_changed",
            {"operation": "replace", "path": relative, "matches": total_matches, "edits": len(normalized)},
        )
        return f"Applied {len(normalized)} exact replacements to {relative} ({total_matches} match(es))"

    def run_command(self, command: str, timeout_seconds: int = 120) -> str:
        command = str(command or "").strip()
        if not command:
            raise PermanentError("命令不能为空")
        lowered = command.lower()
        blocked = ("git reset --hard", "git clean -fd", "rm -rf", "rmdir /s", "del /s")
        if any(fragment in lowered for fragment in blocked):
            raise PermanentError("为保护项目，拒绝执行破坏性命令")
        timeout = max(1, min(int(timeout_seconds or 120), 300))
        self.ledger.append(
            "command_approval_requested",
            {"command": command, "timeout_seconds": timeout, "cwd": str(self.project_root)},
        )
        approved = False
        reason = "未配置用户审批回调"
        if self.command_approval is not None:
            try:
                approved = bool(self.command_approval(command, timeout))
                reason = "用户拒绝或审批超时"
            except Exception as exc:
                reason = f"审批界面异常：{type(exc).__name__}: {exc}"
        if not approved:
            self.ledger.append(
                "command_approval_denied",
                {"command": command, "reason": reason},
            )
            raise PermanentError(f"命令未执行：{reason}。Agent 不应重试该命令。")
        self.ledger.append(
            "command_approval_granted",
            {"command": command, "timeout_seconds": timeout},
        )
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
        manager = GitManager(self.project_root)
        status = manager.status(protect_sensitive=True)
        changes = manager.diff(protect_sensitive=True).strip() or "（工作区没有可展示的文本 diff）"
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


class SvgArtifactAdapter:
    """Recover an explicitly requested SVG when a model returns it as fenced XML."""

    _REQUEST_VERBS = re.compile(r"\b(generate|create|draw|make|produce)\b|生成|绘制|制作|画", re.IGNORECASE)
    _REPAIR_DIRECTIVE = re.compile(
        r"\b(?:repair|fix|edit|revise|adjust)\s+(?:only|the|this|existing|current|listed|reported|duplicate)\b"
        r"|(?:修复|修改|编辑)(?:现有|当前|指定|列出|此|该)?",
        re.IGNORECASE,
    )
    _LOCAL_REPAIR_SCOPE = re.compile(r"\b(?:localized|targeted|minimal|smallest exact)\b", re.IGNORECASE)
    _SVG_BLOCK = re.compile(r"```(?:svg|xml)\s*(.*?)```", re.IGNORECASE | re.DOTALL)
    _TOOL_RESPONSE = re.compile(r"\s*<tool_response>\s*(.*?)\s*</tool_response>\s*", re.IGNORECASE | re.DOTALL)
    _RAW_SVG = re.compile(r"\A(?:<\?xml\s+[^?]*\?>\s*)?<svg\b.*</svg\s*>\Z", re.IGNORECASE | re.DOTALL)
    _SAFE_FILENAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.svg\Z", re.IGNORECASE)
    _ACTIVE_ELEMENTS = {"script", "foreignobject", "iframe", "object", "embed"}

    @classmethod
    def is_repair_request(cls, prompt: str) -> bool:
        return bool("svg" in prompt.casefold() and cls._REPAIR_DIRECTIVE.search(prompt))

    @classmethod
    def is_local_repair_request(cls, prompt: str) -> bool:
        return bool(
            cls.is_repair_request(prompt)
            and cls._LOCAL_REPAIR_SCOPE.search(prompt)
            and "replace_in_file" in prompt.casefold()
        )

    @classmethod
    def is_creation_request(cls, prompt: str) -> bool:
        return bool(
            not cls.is_repair_request(prompt)
            and cls._REQUEST_VERBS.search(prompt)
            and "svg" in prompt.casefold()
        )

    @classmethod
    def is_svg_artifact_request(cls, prompt: str) -> bool:
        return cls.is_creation_request(prompt) or cls.is_repair_request(prompt)

    @classmethod
    def _recover_complete_svg_from_tool_text(cls, candidate: str) -> tuple[str, str] | None:
        """Recover a complete SVG from a malformed but explicit write_file envelope."""
        if not re.search(r'"name"\s*:\s*"write_file"', candidate):
            return None
        path_match = re.search(
            r'"path"\s*:\s*"([A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.svg)"',
            candidate,
            re.IGNORECASE,
        )
        content_match = re.search(r'"content"\s*:\s*"', candidate)
        if not path_match or not content_match:
            return None
        content_start = content_match.end()
        svg_start = candidate.find("<svg", content_start)
        svg_end = candidate.find("</svg>", svg_start)
        if svg_start < 0 or svg_end < 0 or candidate.find("<svg", svg_start + 4) >= 0:
            return None
        encoded = candidate[svg_start : svg_end + len("</svg>")]
        try:
            source = json.loads('"' + encoded + '"')
        except json.JSONDecodeError:
            # The closing root proves the artifact is complete; normalize only
            # JSON's two common string escapes, then retain strict SVG checks.
            source = encoded.replace(r"\n", "\n").replace(r"\t", "\t").replace(r'\"', '"')
        if not cls._RAW_SVG.fullmatch(source):
            return None
        return source, path_match.group(1)

    @classmethod
    def _extract_svg_payload(cls, response: str) -> tuple[str, str | None] | None:
        """Accept one SVG or a narrowly validated textual write_file envelope."""
        matches = list(cls._SVG_BLOCK.finditer(response))
        if matches:
            if len(matches) != 1:
                return None
            candidate = matches[0].group(1).strip()
            payload_filename: str | None = None
            try:
                envelope = json.loads(candidate)
            except json.JSONDecodeError:
                recovered = cls._recover_complete_svg_from_tool_text(candidate)
                if recovered:
                    return recovered
                envelope = None
            if isinstance(envelope, dict):
                arguments = envelope.get("arguments")
                if (
                    envelope.get("name") != "write_file"
                    or not isinstance(arguments, dict)
                    or set(arguments) != {"path", "content"}
                    or not isinstance(arguments.get("path"), str)
                    or not cls._SAFE_FILENAME.fullmatch(arguments["path"])
                    or not isinstance(arguments.get("content"), str)
                ):
                    return None
                candidate = arguments["content"].strip()
                payload_filename = arguments["path"]
            if cls._RAW_SVG.fullmatch(candidate):
                return candidate, payload_filename
            return None

        candidate = response.strip()
        wrapped = cls._TOOL_RESPONSE.fullmatch(candidate)
        if wrapped:
            candidate = wrapped.group(1)
            # Some local models mimic a tool result by prefixing each XML line
            # with a display line number. Normalize only this narrowly matched
            # wrapper; arbitrary prose around an SVG is never recovered.
            candidate = re.sub(r"(?m)^\s*\d+\s*:\s?", "", candidate)
        candidate = candidate.strip()
        payload_filename = None
        try:
            envelope = json.loads(candidate)
        except json.JSONDecodeError:
            recovered = cls._recover_complete_svg_from_tool_text(candidate)
            if recovered:
                return recovered
            envelope = None
        if isinstance(envelope, dict):
            arguments = envelope.get("arguments")
            if (
                envelope.get("name") != "write_file"
                or not isinstance(arguments, dict)
                or set(arguments) != {"path", "content"}
                or not isinstance(arguments.get("path"), str)
                or not cls._SAFE_FILENAME.fullmatch(arguments["path"])
                or not isinstance(arguments.get("content"), str)
            ):
                return None
            candidate = arguments["content"].strip()
            payload_filename = arguments["path"]
        return (candidate, payload_filename) if cls._RAW_SVG.fullmatch(candidate) else None

    @classmethod
    def _extract_svg_source(cls, response: str) -> str | None:
        payload = cls._extract_svg_payload(response)
        return payload[0] if payload else None

    @classmethod
    def create_tool_call(cls, prompt: str, response: str, project_root: Path) -> dict[str, Any] | None:
        """Recover one safe SVG creation or an explicitly scoped edit of an existing SVG."""
        repair_request = cls.is_repair_request(prompt)
        if cls.is_local_repair_request(prompt):
            return None
        if not cls.is_creation_request(prompt) and not repair_request:
            return None
        payload = cls._extract_svg_payload(response)
        if payload is None:
            return None
        source, payload_filename = payload
        if not source or len(source.encode("utf-8")) > CodingToolbox.MAX_WRITE_BYTES:
            return None
        lowered = source.casefold()
        if "<!doctype" in lowered or "<!entity" in lowered:
            return None
        try:
            root = ET.fromstring(source)
        except ET.ParseError:
            return None
        if not isinstance(root.tag, str) or root.tag.rsplit("}", 1)[-1].casefold() != "svg":
            return None
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            if element.tag.rsplit("}", 1)[-1].casefold() in cls._ACTIVE_ELEMENTS:
                return None
            if element.tag.rsplit("}", 1)[-1].casefold() == "style" and (
                "@import" in (element.text or "").casefold()
                or re.search(r"url\s*\(\s*(?!['\"]?#)", element.text or "", re.I)
            ):
                return None
            for raw_name, value in element.attrib.items():
                name = raw_name.rsplit("}", 1)[-1].casefold()
                if name.startswith("on"):
                    return None
                if name in {"href", "src"} and value.strip() and not value.strip().startswith("#"):
                    return None
                if re.search(r"url\s*\(\s*(?!['\"]?#)", value, re.I) or (
                    name == "style" and "@import" in value.casefold()
                ):
                    return None

        filename_pattern = r"(?<![A-Za-z0-9_.-])([A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.svg)(?![A-Za-z0-9_.-])"
        names = re.findall(
            filename_pattern,
            response,
            re.I,
        )
        existing_targets = re.findall(filename_pattern, prompt, re.I)
        workspace = Path(project_root).resolve()
        overwrite_target = ""
        if repair_request:
            target_match = re.search(
                r"\bin\s+`?([A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.svg)`?\b",
                prompt,
                re.IGNORECASE,
            )
            if not target_match:
                return None
            overwrite_target = target_match.group(1)
            target_path = (workspace / overwrite_target).resolve()
            if (
                not cls._SAFE_FILENAME.fullmatch(overwrite_target)
                or workspace not in target_path.parents
                or not target_path.is_file()
                or (payload_filename and payload_filename.casefold() != overwrite_target.casefold())
            ):
                return None
        else:
            overwrite_target = next(
                (
                    name
                    for name in existing_targets
                    if cls._SAFE_FILENAME.fullmatch(name)
                    and re.search(rf"\bexisting\s+`?{re.escape(name)}\b`?", prompt, re.I)
                ),
                "",
            )
        filename = overwrite_target or payload_filename or next(
            (name for name in reversed(names) if cls._SAFE_FILENAME.fullmatch(name)),
            "generated.svg",
        )
        candidate = filename
        suffix = 1
        while (
            (not overwrite_target and (workspace / candidate).exists())
            or CodingToolbox.is_protected_path(candidate)
        ):
            candidate = f"{Path(filename).stem}-{suffix}.svg"
            suffix += 1
        return {
            "id": new_id("call"),
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": candidate, "content": source}, ensure_ascii=False),
            },
        }


class CodingAgent:
    """Codex-style coding loop: inspect, edit, run checks, inspect diff, commit."""

    MAX_TURNS = 32
    MAX_SVG_CREATION_RETRIES = 1
    SVG_CREATION_TOOLS = frozenset({"write_file"})
    SVG_LOCAL_REPAIR_TOOLS = frozenset({"read_file", "replace_in_file", "git_diff"})
    SVG_ARTIFACT_TOOLS = frozenset({"read_file", "write_file", "replace_in_file", "git_diff"})
    EXPLICIT_COMMAND_INTENT = re.compile(
        r"\b(?:run|execute)\s+(?:(?:the|a)\s+)?(?:commands?|scripts?|tests?|test suite|checks?)\b"
        r"|\b(?:pytest|npm\s+test|python(?:\.exe)?\s+-m\s+pytest)\b"
        r"|(?:运行|执行)(?:命令|脚本|测试)",
        re.IGNORECASE,
    )
    SYSTEM_PROMPT = """你是 SciDevHarness 的本地科研编码 Agent。直接用工具完成用户在工作区内的请求；实际操作，不能用方案、代码块或声称代替工具结果。

文件任务
- 用户要求创建/生成可保存文件即为授权：选合理文件名，在工作区根目录调用 `write_file` 一次；不要为名称、路径、尺寸或风格追问。简单图像/SVG 不用 shell 或下载。写入成功后简短确认，不重复输出整份文件。
- 修改现有文件时，将引用的旧请求只视为背景。先读取目标及必要上下文，保留无关内容；局部修改用 `replace_in_file`，同文件多项独立精确修改可一次批量调用；仅在确需整体替换时用 `write_file`。实际调用工具并核验返回结果。
- 仅在任务依赖时读取项目文件/目录。不得访问 `.git`、`.research`、密钥、环境变量、数据集或工作区外路径。
- 修改后运行最小相关检查。运行命令须逐条经 UI 批准；被拒绝或超时后不得重试、拆分或变形规避。用户点名的工具/检查必须实际执行；未执行或失败要明说，不能将请求、计划或工具 JSON 当作结果。工具报错先分析修正，不假称完成；完成后简述改动、验证和限制。
- 仅当实质歧义会改变结果或任务超出范围时询问；否则采用合理默认。不要擅自运行长实验/训练或下载大文件。

图像与 SVG
- 先定画布、构图与比例，再画清可识别主体和各部件的连接/接触关系，最后加细节和样式。动作、承载、操作须表现真实支撑和接触点；分离的近邻形状或色块不代表交互。
- 保持主体姿态和关键部件清晰；避免不合理遮挡、重叠和 viewBox 裁切，留白与对比适度。简单 SVG 少于 60 个元素并闭合标签；语义 ID 可辅助检查但不能代替可见几何。
- 创建后尽可能实际渲染/验证；只有工具确实确认后才能声称已完成渲染/验证。
"""

    def __init__(
        self,
        project_root: Path,
        ledger: EventLedger,
        git: GitManager,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        summary_settings: SummarySettings | None = None,
        command_approval: Callable[[str, int], bool] | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.ledger = ledger
        self.git = git
        self.event_callback = event_callback
        self.summary_settings = summary_settings or SummarySettings.load(self.project_root)
        self.toolbox = CodingToolbox(self.project_root, ledger, command_approval=command_approval)

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
            f"Git 状态：\n{self.git.status(protect_sensitive=True)}\n\n"
            f"项目指令 AGENTS.md：\n{instruction or '未找到'}"
        )

    def _tool_definitions_for_prompt(self, prompt: str) -> list[dict[str, Any]]:
        definitions = self.toolbox.definitions()
        if self.EXPLICIT_COMMAND_INTENT.search(prompt):
            return definitions
        if SvgArtifactAdapter.is_creation_request(prompt):
            allowed_tools = self.SVG_CREATION_TOOLS
        elif SvgArtifactAdapter.is_repair_request(prompt):
            allowed_tools = (
                self.SVG_LOCAL_REPAIR_TOOLS
                if SvgArtifactAdapter.is_local_repair_request(prompt)
                else self.SVG_ARTIFACT_TOOLS
            )
        else:
            return definitions
        return [
            definition
            for definition in definitions
            if definition.get("function", {}).get("name") in allowed_tools
        ]

    @classmethod
    def _tool_choice_for_tools(cls, tool_names: set[str]) -> str:
        """Require the sole authorized file-creation tool instead of accepting narration."""
        return "required" if tool_names == cls.SVG_CREATION_TOOLS else "auto"

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
            "git_preexisting_paths": sorted(self.git.status_paths()),
            "agent_changed_paths": [],
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

    def _auto_commit_warning(
        self,
        skipped_paths: dict[str, dict[str, str | int]],
        commit_sha: str,
    ) -> str:
        if not skipped_paths:
            return ""
        reason_labels = {
            "protected_path": "敏感/内部路径",
            "outside_workspace": "越出工作区的路径",
            "linked_path": "符号链接或目录联接",
            "path_unavailable": "无法安全核验的路径",
            "file_too_large": f"超过 {self.git.MAX_AUTO_COMMIT_FILE_BYTES:,} bytes 自动提交上限",
            "non_text_file": "二进制或非 UTF-8 文件",
        }
        lines = ["Git 自动提交保护：以下变更未自动提交，仍保留在工作区，可在 Git 面板审核后手动提交："]
        for relative, detail in sorted(skipped_paths.items())[:12]:
            label = json.dumps(relative, ensure_ascii=False)
            reason = reason_labels.get(str(detail.get("reason")), "安全策略拦截")
            size = detail.get("size_bytes")
            if isinstance(size, int):
                reason += f"（{size:,} bytes）"
            lines.append(f"- {label}：{reason}")
        remaining = len(skipped_paths) - min(12, len(skipped_paths))
        if remaining:
            lines.append(f"- 另有 {remaining} 个文件因同一安全策略保留未提交。")
        lines.append(
            f"其余安全文件已提交 {commit_sha[:12]}。"
            if commit_sha
            else "本次没有安全文件产生自动提交。"
        )
        return "\n".join(lines)

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

    def _summary_source(self, session: dict[str, Any]) -> str:
        """Build a compact, role-labelled transcript for the summary request."""
        chunks: list[str] = []
        for message in session.get("messages", []):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "message")
            if role == "system":
                continue
            content = self._text_content(message.get("content")).strip()
            if content:
                chunks.append(f"[{role}]\n{content}")
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    function = call.get("function") or {}
                    if isinstance(function, dict):
                        name = str(function.get("name") or "unknown")
                        chunks.append(f"[tool_call] {name}")
        commit_warning = str(session.get("git_auto_commit_warning") or "").strip()
        if commit_warning:
            chunks.append(f"[harness_git_safety]\n{commit_warning}")
        source = "\n\n".join(chunks)
        limit = self.summary_settings.context_chars
        if len(source) <= limit:
            return source
        head = max(1200, limit // 3)
        tail = max(1200, limit - head)
        return source[:head] + "\n\n[中间内容已折叠]\n\n" + source[-tail:]

    def _summarize_session(
        self,
        provider: Any,
        session: dict[str, Any],
        phase: str = "final",
        turn: int | None = None,
    ) -> str:
        """Create and persist a checkpoint/final summary without blocking completion."""
        settings = self.summary_settings
        session_id = str(session["session_id"])
        if not settings.enabled:
            self.ledger.append(
                "conversation_summary_skipped",
                {"session_id": session_id, "reason": "disabled", "phase": phase, "turn": turn},
            )
            return ""

        self.ledger.append(
            "conversation_summary_started",
            {
                "session_id": session_id,
                "model": settings.model or getattr(provider, "model", "default"),
                "phase": phase,
                "turn": turn,
            },
        )
        self._emit("summary_started", {"session_id": session_id, "phase": phase, "turn": turn})
        summary_provider = provider
        if settings.model and hasattr(provider, "with_model"):
            summary_provider = provider.with_model(settings.model)
        summary_messages = [
            {
                "role": "system",
                "content": "你是科研编码项目的会话记录器。只根据给定对话总结已经发生的事实，禁止臆测。输出简洁、可检索的中文 Markdown。",
            },
            {
                "role": "user",
                "content": (
                    f"{settings.instruction}\n\n"
                    f"会话 ID：{session_id}\n"
                    f"Git 结果提交：{session.get('git_result_sha') or '无'}\n\n"
                    f"对话记录：\n{self._summary_source(session)}"
                ),
            },
        ]
        attempts = settings.retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                message = summary_provider.chat(
                    summary_messages,
                    max_tokens=settings.max_tokens,
                    request_id=f"{session_id}-summary",
                )
                text = self._text_content(message.get("content")).strip()
                if not text:
                    raise PermanentError("总结模型返回了空内容")
                summary_id = new_id("summary")
                summary_path = self.project_root / ".research" / "summaries" / f"{session_id}_{summary_id}.json"
                record = {
                    "summary_id": summary_id,
                    "session_id": session_id,
                    "created_at": now_iso(),
                    "phase": phase,
                    "turn": turn,
                    "model": getattr(summary_provider, "model", settings.model or "default"),
                    "text": text,
                    "config": {
                        "max_tokens": settings.max_tokens,
                        "context_chars": settings.context_chars,
                        "instruction": settings.instruction,
                    },
                }
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                summary_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                relative_path = summary_path.relative_to(self.project_root).as_posix()
                session.setdefault("summaries", []).append(
                    {"summary_id": summary_id, "created_at": record["created_at"], "path": relative_path, "text": text}
                )
                session["summary"] = text
                session["summary_path"] = relative_path
                session["summary_created_at"] = record["created_at"]
                self.ledger.append(
                    "conversation_summary_created",
                    {
                        "session_id": session_id,
                        "summary_id": summary_id,
                        "path": relative_path,
                        "phase": phase,
                        "turn": turn,
                        "text": text[:4000],
                    },
                )
                self._emit(
                    "summary_completed",
                    {
                        "session_id": session_id,
                        "summary_id": summary_id,
                        "path": relative_path,
                        "phase": phase,
                        "turn": turn,
                        "model": record["model"],
                        "text": text,
                    },
                )
                return text
            except RetryableError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(min(2.0, 0.5 * (attempt + 1)))
            except Exception as exc:  # Summary is auxiliary; a failed summary must not erase completed code work.
                last_error = exc
                break

        detail = str(last_error or "未知错误")
        self.ledger.append(
            "conversation_summary_failed",
            {"session_id": session_id, "error": detail[:1000], "attempts": attempts, "phase": phase, "turn": turn},
        )
        self._emit(
            "summary_failed",
            {"session_id": session_id, "error": detail[:1000], "phase": phase, "turn": turn},
        )
        return ""

    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        payload = task["payload"]
        session_id = payload["session_id"]
        prompt = payload["prompt"]
        session = self._load_session(session_id, prompt)
        session.setdefault("git_preexisting_paths", sorted(self.git.status_paths()))
        session.setdefault("agent_changed_paths", [])
        if session.get("last_prompt") != prompt:
            session["git_preexisting_paths"] = sorted(self.git.status_paths())
            session["agent_changed_paths"] = []
            session["svg_creation_retries"] = 0
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
        tool_definitions = self._tool_definitions_for_prompt(prompt)
        allowed_tool_names = {
            definition["function"]["name"]
            for definition in tool_definitions
            if isinstance(definition, dict)
            and isinstance(definition.get("function"), dict)
            and isinstance(definition["function"].get("name"), str)
        }
        workspace_revision = 0
        diff_verified_revision = -1
        recovered_artifact_path = ""

        for turn in range(1, self.MAX_TURNS + 1):
            if recovered_artifact_path:
                message = {
                    "role": "assistant",
                    "content": f"Saved SVG artifact to {recovered_artifact_path}.",
                    "tool_calls": [],
                }
            else:
                self.ledger.append("model_call_started", {"session_id": session_id, "turn": turn})
                self._emit("model_call_started", {"session_id": session_id, "turn": turn})
                stream_options: dict[str, Any] = {}
                if isinstance(provider, OpenAICompatibleProvider):
                    stream_options["on_delta"] = lambda text: self._emit(
                        "assistant_delta",
                        {"session_id": session_id, "turn": turn, "text": text},
                    )
                    stream_options["tool_choice"] = self._tool_choice_for_tools(allowed_tool_names)
                message = provider.chat(
                    messages,
                    tools=tool_definitions,
                    max_tokens=12000,
                    request_id=f"{session_id}-turn-{turn}",
                    **stream_options,
                )
            content = self._text_content(message.get("content"))
            tool_calls = message.get("tool_calls") or []
            recovered_tool_ids: set[str] = set()
            if not tool_calls and content:
                recovered = SvgArtifactAdapter.create_tool_call(prompt, content, self.project_root)
                if recovered:
                    tool_calls = [recovered]
                    recovered_tool_ids.add(str(recovered["id"]))
                    self.ledger.append(
                        "artifact_response_recovered",
                        {
                            "session_id": session_id,
                            "format": "svg",
                            "path": json.loads(recovered["function"]["arguments"])["path"],
                        },
                    )
            # Ollama templates commonly serialize assistant content *instead of*
            # tool calls when both fields are present. Keep narration in the UI
            # event below, but give the next model turn an unambiguous call record.
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": "" if tool_calls else content,
            }
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            messages.append(assistant_message)
            if content and tool_calls:
                display_text = content
                if recovered_tool_ids and len(recovered_tool_ids) == len(tool_calls):
                    display_text = "Harness 识别到 SVG 文件内容，正在安全保存。"
                self._emit("assistant", {"session_id": session_id, "turn": turn, "text": display_text})
            if tool_calls:
                self._save_session(session)

            if not tool_calls:
                preexisting_paths = set(session.get("git_preexisting_paths") or [])
                changed_paths = (self.git.status_paths() - preexisting_paths) | (
                    set(session.get("agent_changed_paths") or []) - preexisting_paths
                )
                has_svg_output = any(path.casefold().endswith(".svg") for path in changed_paths)
                svg_retries = int(session.get("svg_creation_retries", 0) or 0)
                missing_svg = SvgArtifactAdapter.is_creation_request(prompt) and not has_svg_output
                if missing_svg and svg_retries < self.MAX_SVG_CREATION_RETRIES:
                    if content:
                        self._emit("assistant", {"session_id": session_id, "turn": turn, "text": content})
                    # A malformed, very long artifact response can dominate the
                    # next prompt and make a local model repeat the same failure.
                    # Keep the audit event, but replace that non-actionable turn
                    # in session context with a concise marker before retrying.
                    if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
                        messages[-1] = {
                            "role": "assistant",
                            "content": "The previous response did not save an SVG file.",
                        }
                    svg_retries += 1
                    session["svg_creation_retries"] = svg_retries
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Your previous reply did not create an SVG file, so this task is not complete. "
                                "Immediately call the declared write_file tool exactly once with a safe .svg "
                                "filename and complete SVG markup. Do not return a JSON tool-call object or "
                                "wrap the tool call in XML. If a tool call cannot be emitted, return only one "
                                "complete fenced svg block. Keep the requested subject recognizable and all "
                                "drawing geometry inside the viewBox."
                            ),
                        }
                    )
                    retry_event = {
                        "session_id": session_id,
                        "attempt": svg_retries,
                        "reason": "explicit SVG creation returned without creating an SVG file",
                    }
                    self.ledger.append("svg_creation_retry_scheduled", retry_event)
                    self._emit("svg_creation_retry_scheduled", retry_event)
                    session["updated_at"] = now_iso()
                    self._save_session(session)
                    continue
                if missing_svg:
                    session["status"] = "failed"
                    session["error"] = "模型在一次自动补救后仍未创建 SVG 文件；任务未标记为完成。"
                    session["updated_at"] = now_iso()
                    failure = {
                        "session_id": session_id,
                        "error": session["error"],
                        "svg_creation_retries": svg_retries,
                    }
                    self.ledger.append("coding_session_failed", failure)
                    self._save_session(session)
                    raise PermanentError(session["error"])
                if changed_paths and diff_verified_revision != workspace_revision:
                    # The model's premature final text is not shown or retained;
                    # it must review the actual diff before producing a final answer.
                    messages.pop()
                    call_id = new_id("call")
                    tool_call = {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "git_diff", "arguments": "{}"},
                    }
                    messages.append({"role": "assistant", "content": "", "tool_calls": [tool_call]})
                    self.ledger.append(
                        "tool_call_started",
                        {
                            "session_id": session_id,
                            "name": "git_diff",
                            "source": "harness_precommit",
                            "reason": "unverified_agent_changes",
                        },
                    )
                    self._emit(
                        "tool_started",
                        {
                            "session_id": session_id,
                            "name": "git_diff",
                            "arguments": {},
                            "source": "harness_precommit",
                        },
                    )
                    try:
                        diff_result = str(self.toolbox.execute("git_diff", {}))[-30000:]
                    except Exception as exc:
                        error = f"提交前 Git diff 检查失败：{type(exc).__name__}: {exc}"
                        self.ledger.append(
                            "tool_result",
                            {
                                "session_id": session_id,
                                "name": "git_diff",
                                "source": "harness_precommit",
                                "error": error[:1000],
                            },
                        )
                        self._emit(
                            "tool_result",
                            {
                                "session_id": session_id,
                                "name": "git_diff",
                                "source": "harness_precommit",
                                "result": error,
                            },
                        )
                        session["status"] = "failed"
                        session["error"] = error
                        session["updated_at"] = now_iso()
                        self._save_session(session)
                        raise PermanentError(error) from exc
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": diff_result})
                    self.ledger.append(
                        "tool_result",
                        {
                            "session_id": session_id,
                            "name": "git_diff",
                            "source": "harness_precommit",
                            "result": diff_result[:3000],
                        },
                    )
                    self._emit(
                        "tool_result",
                        {
                            "session_id": session_id,
                            "name": "git_diff",
                            "result": diff_result,
                            "source": "harness_precommit",
                        },
                    )
                    diff_verified_revision = workspace_revision
                    session["updated_at"] = now_iso()
                    self._save_session(session)
                    continue

                if content:
                    self._emit("assistant", {"session_id": session_id, "turn": turn, "text": content})
                self._save_session(session)
                summary = " ".join(prompt.split())[:72] or session_id
                session["status"] = "completed"
                session["final_message"] = content
                commit_paths, skipped_paths = self.git.filter_auto_commit_paths(changed_paths)
                session["git_result_sha"] = self.git.commit_paths(f"[codex] {summary}", commit_paths)
                session["git_auto_commit_skipped_paths"] = skipped_paths
                commit_warning = self._auto_commit_warning(skipped_paths, session["git_result_sha"])
                if commit_warning:
                    session["git_auto_commit_warning"] = commit_warning
                    session["final_message"] = f"{content.rstrip()}\n\n{commit_warning}".strip()
                    payload = {
                        "session_id": session_id,
                        "skipped_paths": skipped_paths,
                        "limit_bytes": self.git.MAX_AUTO_COMMIT_FILE_BYTES,
                        "message": commit_warning,
                    }
                    self.ledger.append("git_auto_commit_skipped_paths", payload)
                    self._emit("git_auto_commit_skipped_paths", payload)
                session["updated_at"] = now_iso()
                conversation_summary = self._summarize_session(provider, session, phase="final", turn=turn)
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
                self._emit(
                    "completed",
                    {
                        "session_id": session_id,
                        "text": content,
                        "git_result_sha": session["git_result_sha"],
                        "summary": conversation_summary,
                        "summary_path": session.get("summary_path", ""),
                        "git_auto_commit_skipped_paths": skipped_paths,
                        "git_auto_commit_warning": commit_warning,
                    },
                )
                return {
                    "session_id": session_id,
                    "git_result_sha": session["git_result_sha"],
                    "text": content,
                    "summary": conversation_summary,
                    "summary_path": session.get("summary_path", ""),
                    "git_auto_commit_skipped_paths": skipped_paths,
                    "git_auto_commit_warning": commit_warning,
                }

            for call in tool_calls:
                function = call.get("function", {}) if isinstance(call, dict) else {}
                name = str(function.get("name", ""))
                call_id = str(call.get("id", new_id("tool")))
                raw_arguments = function.get("arguments", "{}")
                tool_succeeded = False
                try:
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                    if not isinstance(arguments, dict):
                        raise ValueError("工具参数必须是 JSON 对象")
                    if name not in allowed_tool_names:
                        raise PermanentError(f"Tool {name!r} is not available for this task.")
                    tool_event = {"session_id": session_id, "name": name, "arguments": arguments}
                    if call_id in recovered_tool_ids:
                        tool_event["source"] = "harness_svg_artifact_recovery"
                    self._emit("tool_started", tool_event)
                    result = self.toolbox.execute(name, arguments)
                    tool_succeeded = True
                except Exception as exc:  # Tool errors go back to the model for correction.
                    result = f"工具执行失败：{type(exc).__name__}: {exc}"
                if tool_succeeded:
                    if call_id in recovered_tool_ids and name == "write_file":
                        recovered_artifact_path = str(arguments.get("path", ""))
                    if name in {"write_file", "replace_in_file"}:
                        changed = set(session.get("agent_changed_paths") or [])
                        changed.add(str(arguments.get("path", "")))
                        session["agent_changed_paths"] = sorted(path for path in changed if path)
                        workspace_revision += 1
                    elif name == "run_command":
                        workspace_revision += 1
                    elif name == "git_diff":
                        diff_verified_revision = workspace_revision
                result = str(result)[-30000:]
                messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
                self.ledger.append(
                    "tool_result",
                    {"session_id": session_id, "name": name, "result": result[:3000]},
                )
                self._emit("tool_result", {"session_id": session_id, "name": name, "result": result})
                self._save_session(session)

            interval = self.summary_settings.interval_turns
            if (
                not recovered_artifact_path
                and self.summary_settings.enabled
                and interval > 0
                and turn % interval == 0
            ):
                session["updated_at"] = now_iso()
                self._summarize_session(provider, session, phase="checkpoint", turn=turn)
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
