"""Task-scoped file checkpoints for reverting Agent writes.

A checkpoint records only files the Agent is about to modify.  It is not a Git
commit and it never touches unrelated user changes.  Restore is explicit: it
rewrites captured files to their pre-task bytes and removes files that did not
exist before the task.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from typing import Any


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except (OSError, ValueError):
            pass


@dataclass(frozen=True)
class CheckpointEntry:
    path: str
    existed: bool
    backup: str = ""
    size: int = 0


class WorkspaceCheckpoint:
    """Persist the original bytes of files changed during one task."""

    def __init__(self, project_root: Path, checkpoint_id: str):
        self.project_root = Path(project_root).resolve()
        safe_id = "".join(char for char in checkpoint_id if char.isalnum() or char in "-_")[:80]
        if not safe_id:
            raise ValueError("checkpoint id is empty")
        self.checkpoint_id = safe_id
        self.root = self.project_root / ".research" / "checkpoints" / safe_id
        self.files_dir = self.root / "files"
        self.manifest_path = self.root / "manifest.json"
        self.entries: dict[str, CheckpointEntry] = {}
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self._write_manifest()

    def _write_manifest(self) -> None:
        payload = {
            "checkpoint_id": self.checkpoint_id,
            "entries": [
                {
                    "path": entry.path,
                    "existed": entry.existed,
                    "backup": entry.backup,
                    "size": entry.size,
                }
                for entry in sorted(self.entries.values(), key=lambda item: item.path)
            ],
        }
        _atomic_write(
            self.manifest_path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    def _relative(self, target: Path) -> str:
        try:
            return target.resolve().relative_to(self.project_root).as_posix()
        except (OSError, ValueError) as exc:
            raise ValueError("checkpoint path escapes the project root") from exc

    def capture(self, target: Path) -> None:
        """Record a file once, before the Agent changes it."""
        target = Path(target)
        relative = self._relative(target)
        with self._lock:
            if relative in self.entries:
                return
            if target.exists() and not target.is_file():
                raise ValueError(f"checkpoint target is not a file: {relative}")
            if target.is_symlink():
                raise ValueError(f"refusing to checkpoint a symlink: {relative}")
            if not target.exists():
                self.entries[relative] = CheckpointEntry(relative, False)
                self._write_manifest()
                return
            data = target.read_bytes()
            digest = hashlib.sha256(data).hexdigest()[:16]
            backup_name = f"{len(self.entries):04d}-{digest}.bin"
            _atomic_write(self.files_dir / backup_name, data)
            self.entries[relative] = CheckpointEntry(relative, True, backup_name, len(data))
            self._write_manifest()

    def restore(self) -> dict[str, Any]:
        """Restore captured files and delete files that did not exist before."""
        restored: list[str] = []
        deleted: list[str] = []
        failed: dict[str, str] = {}
        with self._lock:
            for entry in sorted(self.entries.values(), key=lambda item: item.path):
                target = self.project_root / entry.path
                try:
                    if entry.existed:
                        backup = self.files_dir / entry.backup
                        if not backup.is_file():
                            failed[entry.path] = "backup is missing"
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        _atomic_write(target, backup.read_bytes())
                        restored.append(entry.path)
                    else:
                        if target.is_symlink() or target.is_file():
                            target.unlink()
                            deleted.append(entry.path)
                        elif target.exists():
                            failed[entry.path] = "target is not a regular file"
                except (OSError, ValueError) as exc:
                    failed[entry.path] = str(exc)[:300]
        return {"restored": restored, "deleted": deleted, "failed": failed}

    def discard(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @classmethod
    def prune(cls, project_root: Path, keep: int = 10) -> None:
        """Keep the most recent checkpoints and delete older task directories."""
        base = Path(project_root).resolve() / ".research" / "checkpoints"
        if not base.is_dir():
            return
        directories = [item for item in base.iterdir() if item.is_dir()]
        directories.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for old in directories[max(1, keep):]:
            shutil.rmtree(old, ignore_errors=True)
