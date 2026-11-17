"""Process-wide runtime helpers shared by the core, ledger and CLI."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import uuid


_SHARED_PATH_LOCKS: dict[str, threading.RLock] = {}
_SHARED_LOCKS_GUARD = threading.Lock()
MAX_SERVER_RETRY_AFTER_SECONDS = 3600.0

# Runtime state written by the Harness itself. Without this file a project
# without .gitignore would show prompts/transcripts as changes and "commit all"
# could publish them.
RESEARCH_IGNORE_PATTERNS = (
    "events.jsonl",
    "runs.db",
    "runs.db-*",
    "requests/",
    "sessions/",
    "settings.json",
    "memory.sqlite3*",
    "summaries/",
    "tree_layout.json",
    "validation/",
    "browser/",
)


def shared_thread_lock(kind: str, path: Path) -> threading.RLock:
    """Return one in-process lock per repository or ledger path."""
    key = f"{kind}:{os.path.normcase(os.fspath(Path(path).resolve()))}"
    with _SHARED_LOCKS_GUARD:
        lock = _SHARED_PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _SHARED_PATH_LOCKS[key] = lock
        return lock


def coding_task_lock_path(project_root: Path) -> Path:
    """Lock file shared by every SciDevHarness process for one project."""
    root = Path(project_root)
    git_dir = root / ".git"
    if git_dir.is_dir():
        return git_dir / "scidev-task.lock"
    return root / ".research" / "locks" / "task.lock"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"
