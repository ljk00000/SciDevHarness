"""Background retry worker for durable task-queue entries.

Kept independent of the agent loop so the queue can be tested and reused
without importing the full CodingAgent module.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable

from scidev_errors import PermanentError, classify_exception
from scidev_ledger import EventLedger


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
                classification = classify_exception(exc, retryable=False)
                self.ledger.retry_or_fail(task_id, error, retryable=False)
                self.ledger.append("task_failed", {
                    "task_id": task_id,
                    "error": error,
                    **classification.as_dict(),
                })
                self._notify("task_failed", {
                    **task,
                    "error": error,
                    "retryable": False,
                    **classification.as_dict(),
                })
            except Exception as exc:  # noqa: BLE001 - worker must keep running
                error = f"{type(exc).__name__}: {exc}"
                classification = classify_exception(exc)
                status = self.ledger.retry_or_fail(
                    task_id,
                    error,
                    retryable=True,
                    base_delay=self.retry_base_seconds,
                    retry_after_seconds=getattr(exc, "retry_after_seconds", None),
                )
                self.ledger.append(
                    "task_retry_scheduled" if status == "retry_wait" else "task_failed",
                    {
                        "task_id": task_id,
                        "error": error,
                        "status": status,
                        **classification.as_dict(),
                    },
                )
                self._notify(
                    "task_retry" if status == "retry_wait" else "task_failed",
                    {
                        **task,
                        "error": error,
                        "status": status,
                        "retryable": True,
                        **classification.as_dict(),
                    },
                )
