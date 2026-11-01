"""Read-only task outcome and failure-kind trends from the local event ledger."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

from scidev_errors import classify_message


@dataclass(frozen=True)
class TrendReport:
    days: int
    started: int
    completed: int
    failed: int
    by_kind: dict[str, int] = field(default_factory=dict)
    by_day: dict[str, int] = field(default_factory=dict)
    retryable_kinds: dict[str, bool] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        if self.completed + self.failed <= 0:
            return 0.0
        return self.completed / (self.completed + self.failed)


def _event_rows(db_path: Path, cutoff: str, limit: int) -> list[tuple[str, str, str]]:
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT timestamp, event_type, payload FROM events "
            "WHERE timestamp >= ? ORDER BY id DESC LIMIT ?",
            (cutoff, max(1, min(int(limit), 50000))),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]


def collect_trends(
    project_root: Path,
    *,
    days: int = 14,
    now: datetime | None = None,
    limit: int = 20000,
) -> TrendReport:
    days = max(1, min(int(days), 90))
    current = now or datetime.now(timezone.utc)
    cutoff = (current - timedelta(days=days)).isoformat()
    db_path = Path(project_root).resolve() / ".research" / "runs.db"
    started = 0
    completed = 0
    failed = 0
    kinds: dict[str, int] = {}
    retryable: dict[str, bool] = {}
    by_day: dict[str, int] = {}
    for timestamp, event_type, raw_payload in _event_rows(db_path, cutoff, limit):
        if event_type not in {"task_started", "task_completed", "task_failed"}:
            continue
        if event_type == "task_started":
            started += 1
            continue
        if event_type == "task_completed":
            completed += 1
            continue
        failed += 1
        try:
            payload = json.loads(raw_payload)
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        kind = str(payload.get("error_kind") or "").strip()
        if not kind:
            kind = classify_message(str(payload.get("error") or "")).kind
        kinds[kind] = kinds.get(kind, 0) + 1
        if "retryable" in payload:
            retryable[kind] = bool(payload.get("retryable"))
        day = timestamp[:10]
        by_day[day] = by_day.get(day, 0) + 1
    return TrendReport(
        days=days,
        started=started,
        completed=completed,
        failed=failed,
        by_kind=dict(sorted(kinds.items(), key=lambda item: (-item[1], item[0]))),
        by_day=dict(sorted(by_day.items())),
        retryable_kinds=retryable,
    )


def format_sparkline(values: list[int]) -> str:
    if not values:
        return ""
    marks = " ▁▂▃▄▅▆▇█"
    peak = max(values) or 1
    result: list[str] = []
    for value in values:
        if value <= 0:
            result.append(marks[0])
            continue
        index = min(len(marks) - 1, max(1, round(value / peak * (len(marks) - 1))))
        result.append(marks[index])
    return "".join(result)
