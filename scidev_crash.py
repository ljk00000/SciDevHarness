"""Local crash reporting without network or variable dumps.

Uncaught exceptions are written under ``.research/crashes`` with a bounded
traceback, version, platform and thread name. API-key-like strings and the
user's home directory are redacted before anything touches disk.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import sys
import threading
import traceback
from types import TracebackType
from typing import Any

MAX_REPORTS = 20
MAX_TRACEBACK = 64_000
_SECRET = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*['\"]?([^\s'\"]{6,})"
)


class CrashReporter:
    def __init__(self, project_root: Path, *, version: str = "0.1.0"):
        self.project_root = Path(project_root).resolve()
        self.version = version
        self.report_dir = self.project_root / ".research" / "crashes"
        self._previous_hook = sys.excepthook
        self._previous_thread_hook = getattr(threading, "excepthook", None)
        self._installed = False

    def _redact(self, text: str) -> str:
        value = str(text)
        value = value.replace(str(Path.home()), "~")
        return _SECRET.sub(lambda match: f"{match.group(1)}=<redacted>", value)

    def record(
        self,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
        *,
        thread_name: str = "",
    ) -> Path | None:
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            return None
        trace = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        trace = self._redact(trace)[-MAX_TRACEBACK:]
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": self.version,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "thread": thread_name or threading.current_thread().name,
            "exception_type": exc_type.__name__,
            "message": self._redact(str(exc_value))[:4000],
            "traceback": trace,
        }
        try:
            self.report_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path = self.report_dir / f"crash-{stamp}-{os.getpid()}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._prune()
            return path
        except OSError:
            return None

    def _prune(self) -> None:
        reports = sorted(self.report_dir.glob("crash-*.json"), key=lambda item: item.name)
        for stale in reports[:-MAX_REPORTS]:
            try:
                stale.unlink()
            except OSError:
                pass

    def _handle(self, exc_type, exc_value, exc_traceback) -> None:
        self.record(exc_type, exc_value, exc_traceback)
        if self._previous_hook not in {sys.__excepthook__, self._handle}:
            self._previous_hook(exc_type, exc_value, exc_traceback)

    def _handle_thread(self, args) -> None:
        self.record(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            thread_name=getattr(args.thread, "name", ""),
        )
        previous = self._previous_thread_hook
        if previous is not None and previous is not self._handle_thread:
            previous(args)

    def install(self) -> "CrashReporter":
        if not self._installed:
            sys.excepthook = self._handle
            if self._previous_thread_hook is not None:
                threading.excepthook = self._handle_thread
            self._installed = True
        return self

    def uninstall(self) -> None:
        if self._installed:
            sys.excepthook = self._previous_hook
            if self._previous_thread_hook is not None:
                threading.excepthook = self._previous_thread_hook
            self._installed = False


def install_crash_reporter(project_root: Path, *, version: str = "0.1.0") -> CrashReporter:
    return CrashReporter(project_root, version=version).install()
