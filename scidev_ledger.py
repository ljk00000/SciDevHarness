"""Append-only JSONL audit log plus a small SQLite query index."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any

from scidev_context import ProjectContext
from scidev_runtime import (
    MAX_SERVER_RETRY_AFTER_SECONDS,
    RESEARCH_IGNORE_PATTERNS,
    new_id,
    now_iso,
    shared_thread_lock,
)


class EventLedger:
    """Append-only JSONL audit log plus a small SQLite query index."""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()
        self.research_dir = self.project_root / ".research"
        self.research_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.research_dir / "events.jsonl"
        self.db_path = self.research_dir / "runs.db"
        # All ledgers for one project share a lock so concurrent appends from
        # several windows/workers cannot interleave JSONL or SQLite writes.
        self._lock = shared_thread_lock("ledger", self.research_dir)
        self._ensure_gitignore()
        self._init_db()

    def _ensure_gitignore(self) -> None:
        """Keep private runtime state out of the project's commits."""
        path = self.research_dir / ".gitignore"
        if path.exists():
            return
        try:
            path.write_text("\n".join(RESEARCH_IGNORE_PATTERNS) + "\n", encoding="utf-8")
        except OSError:
            pass

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
                CREATE TABLE IF NOT EXISTS tool_call_journal (
                    session_id TEXT NOT NULL,
                    assistant_message_index INTEGER NOT NULL,
                    call_index INTEGER NOT NULL,
                    call_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, assistant_message_index, call_index)
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
            # The JSONL log is the audit source of truth; the SQLite table is a
            # query index. A transient lock must not fail the coding task.
            for attempt in range(3):
                try:
                    with self._db() as conn:
                        cursor = conn.execute(
                            "INSERT INTO events(timestamp, event_type, payload) VALUES (?, ?, ?)",
                            (timestamp, event_type, serialized),
                        )
                        return int(cursor.lastrowid)
                except sqlite3.OperationalError:
                    if attempt == 2:
                        return 0
                    time.sleep(0.05 * (attempt + 1))
            return 0

    def prepare_tool_calls(
        self, session_id: str, assistant_message_index: int, tool_calls: list[dict[str, Any]],
    ) -> None:
        """Durably mark the model's calls pending before their transcript is saved or executed."""
        timestamp = now_iso()
        rows = []
        for call_index, call in enumerate(tool_calls):
            function = call.get("function") if isinstance(call, dict) else None
            function = function if isinstance(function, dict) else {}
            rows.append((
                session_id,
                int(assistant_message_index),
                call_index,
                str(call.get("id") or ""),
                str(function.get("name") or "")[:120],
                "pending",
                "",
                timestamp,
            ))
        if not rows:
            return
        with self._lock:
            with self._db() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO tool_call_journal "
                    "(session_id, assistant_message_index, call_index, call_id, name, status, result, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                conn.executemany(
                    "UPDATE tool_call_journal SET call_id=?, name=?, result='', updated_at=? "
                    "WHERE session_id=? AND assistant_message_index=? AND call_index=? AND status='pending'",
                    [(row[3], row[4], timestamp, row[0], row[1], row[2]) for row in rows],
                )

    def start_tool_call(
        self, session_id: str, assistant_message_index: int, call_index: int,
        call_id: str, name: str,
    ) -> dict[str, Any] | None:
        """Atomically transition a pending call to started; never authorize a second execution."""
        with self._lock:
            with self._db() as conn:
                cursor = conn.execute(
                    "UPDATE tool_call_journal SET status='started', updated_at=? "
                    "WHERE session_id=? AND assistant_message_index=? AND call_index=? "
                    "AND call_id=? AND name=? AND status='pending'",
                    (now_iso(), session_id, int(assistant_message_index), int(call_index), call_id, name[:120]),
                )
                should_execute = cursor.rowcount == 1
                row = conn.execute(
                    "SELECT call_id, name, status, result FROM tool_call_journal "
                    "WHERE session_id=? AND assistant_message_index=? AND call_index=?",
                    (session_id, int(assistant_message_index), int(call_index)),
                ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["identity_matches"] = record["call_id"] == call_id and record["name"] == name[:120]
        record["should_execute"] = should_execute and record["identity_matches"]
        return record

    def complete_tool_call(
        self, session_id: str, assistant_message_index: int, call_index: int, result: str,
    ) -> None:
        """Persist a bounded result before adding it to the session transcript."""
        with self._lock:
            with self._db() as conn:
                cursor = conn.execute(
                    "UPDATE tool_call_journal SET status='completed', result=?, updated_at=? "
                    "WHERE session_id=? AND assistant_message_index=? AND call_index=? AND status='started'",
                    (str(result)[-30000:], now_iso(), session_id,
                     int(assistant_message_index), int(call_index)),
                )
                if cursor.rowcount:
                    return
                row = conn.execute(
                    "SELECT status, result FROM tool_call_journal "
                    "WHERE session_id=? AND assistant_message_index=? AND call_index=?",
                    (session_id, int(assistant_message_index), int(call_index)),
                ).fetchone()
                if row is None or row["status"] != "completed" or row["result"] != str(result)[-30000:]:
                    raise RuntimeError("无法持久化工具调用结果")

    def get_tool_call(
        self, session_id: str, assistant_message_index: int, call_index: int,
    ) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT call_id, name, status, result FROM tool_call_journal "
                "WHERE session_id=? AND assistant_message_index=? AND call_index=?",
                (session_id, int(assistant_message_index), int(call_index)),
            ).fetchone()
        return dict(row) if row is not None else None

    def tool_calls_for_session(self, session_id: str) -> dict[tuple[int, int], dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT assistant_message_index, call_index, call_id, name, status, result "
                "FROM tool_call_journal WHERE session_id=?",
                (session_id,),
            ).fetchall()
        return {
            (int(row["assistant_message_index"]), int(row["call_index"])): dict(row)
            for row in rows
        }

    def clear_tool_call(self, session_id: str, assistant_message_index: int, call_index: int) -> None:
        with self._lock:
            with self._db() as conn:
                conn.execute(
                    "DELETE FROM tool_call_journal "
                    "WHERE session_id=? AND assistant_message_index=? AND call_index=?",
                    (session_id, int(assistant_message_index), int(call_index)),
                )

    def create_task(
        self, kind: str, payload: dict[str, Any], max_attempts: int = 1
    ) -> str:
        task_id = new_id("task")
        timestamp = now_iso()
        event_payload = dict(payload)
        overrides = event_payload.pop("context_overrides", None)
        event_payload.pop("context_readonly_paths", None)
        if isinstance(overrides, dict):
            event_payload["context_override_paths"] = sorted(
                path for path in overrides if isinstance(path, str)
            )[:ProjectContext.MAX_CONTENT_OVERRIDES]
            event_payload["context_override_bytes"] = sum(
                len(text.encode("utf-8"))
                for text in overrides.values()
                if isinstance(text, str)
            )
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
        self.append("task_created", {
            "task_id": task_id,
            "kind": kind,
            "payload": event_payload,
        })
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
            row = conn.execute("SELECT payload FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            payload_text = row["payload"] if row is not None else "{}"
            try:
                payload = json.loads(payload_text)
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                payload.pop("context_overrides", None)
                payload.pop("context_readonly_paths", None)
            conn.execute(
                "UPDATE tasks SET status='succeeded', payload=?, updated_at=? WHERE task_id=?",
                (json.dumps(payload, ensure_ascii=False), now_iso(), task_id),
            )

    def retry_or_fail(
        self,
        task_id: str,
        error: str,
        retryable: bool,
        base_delay: float = 5.0,
        retry_after_seconds: float | None = None,
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
                try:
                    server_delay = (
                        float(retry_after_seconds) if retry_after_seconds is not None else 0.0
                    )
                except (TypeError, ValueError, OverflowError):
                    server_delay = 0.0
                if math.isfinite(server_delay):
                    delay = max(delay, min(MAX_SERVER_RETRY_AFTER_SECONDS, max(0.0, server_delay)))
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
            rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC, created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]
