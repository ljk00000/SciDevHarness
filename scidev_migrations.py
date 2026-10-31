"""Idempotent migration of project settings and session JSON files."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any

from scidev_policy import ExecutionMode, NetworkPolicy, normalize_command_prefixes

SCHEMA_VERSION = 2
MAX_SESSION_FILES = 200
MAX_BACKUPS = 5


@dataclass
class MigrationReport:
    schema_version: int = SCHEMA_VERSION
    changed: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "changed": self.changed,
            "backups": self.backups,
            "errors": self.errors,
            "dry_run": self.dry_run,
        }


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _backup(path: Path, backup_dir: Path, report: MigrationReport) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, target)
    report.backups.append(str(target))
    backups = sorted(backup_dir.glob(f"{path.name}.*.bak"))
    for stale in backups[:-MAX_BACKUPS]:
        try:
            stale.unlink()
        except OSError:
            pass


def _migrate_settings(root: Path, report: MigrationReport, dry_run: bool) -> None:
    path = root / ".research" / "settings.json"
    if not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.errors.append(f"settings.json: {exc}")
        return
    if not isinstance(raw, dict):
        report.errors.append("settings.json: top-level value is not an object")
        return
    migrated = dict(raw)
    migrated["schema_version"] = SCHEMA_VERSION
    migrated["execution_mode"] = ExecutionMode.parse(migrated.get("execution_mode")).value
    migrated["network_policy"] = NetworkPolicy.parse(migrated.get("network_policy")).value
    migrated["allowed_commands"] = normalize_command_prefixes(migrated.get("allowed_commands"))
    try:
        migrated["max_agent_turns"] = max(1, min(64, int(migrated.get("max_agent_turns", 32))))
    except (TypeError, ValueError):
        migrated["max_agent_turns"] = 32
    try:
        migrated["max_task_tokens"] = max(0, min(10_000_000, int(migrated.get("max_task_tokens", 0))))
    except (TypeError, ValueError):
        migrated["max_task_tokens"] = 0
    if migrated == raw:
        return
    report.changed.append(str(path))
    if dry_run:
        return
    _backup(path, root / ".research" / "backups", report)
    _atomic_write(path, json.dumps(migrated, ensure_ascii=False, indent=2) + "\n")


def _migrate_sessions(root: Path, report: MigrationReport, dry_run: bool) -> None:
    directory = root / ".research" / "sessions"
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json"))[:MAX_SESSION_FILES]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report.errors.append(f"{path.name}: {exc}")
            continue
        if not isinstance(raw, dict):
            report.errors.append(f"{path.name}: top-level value is not an object")
            continue
        migrated = dict(raw)
        migrated["schema_version"] = SCHEMA_VERSION
        if not isinstance(migrated.get("messages"), list):
            migrated["messages"] = []
        if not isinstance(migrated.get("attachments"), list):
            migrated["attachments"] = []
        if not isinstance(migrated.get("status"), str) or not migrated["status"]:
            migrated["status"] = "unknown"
        if migrated == raw:
            continue
        report.changed.append(str(path))
        if dry_run:
            continue
        _backup(path, root / ".research" / "backups", report)
        _atomic_write(path, json.dumps(migrated, ensure_ascii=False, indent=2) + "\n")


def _migrate_event_db(root: Path, report: MigrationReport, dry_run: bool) -> None:
    path = root / ".research" / "events.db"
    if not path.is_file():
        return
    try:
        with sqlite3.connect(path) as connection:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0] or 0)
            if current >= SCHEMA_VERSION:
                return
            report.changed.append(str(path))
            if dry_run:
                return
            _backup(path, root / ".research" / "backups", report)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
    except sqlite3.Error as exc:
        report.errors.append(f"events.db: {exc}")


def migrate_project(root: Path, *, dry_run: bool = False) -> MigrationReport:
    root = Path(root).resolve()
    report = MigrationReport(dry_run=dry_run)
    if not root.is_dir():
        report.errors.append(f"workspace is not a directory: {root}")
        return report
    _migrate_settings(root, report, dry_run)
    _migrate_sessions(root, report, dry_run)
    _migrate_event_db(root, report, dry_run)
    return report
