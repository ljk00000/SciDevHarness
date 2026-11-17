"""Cross-process advisory file lock used to serialize mutating Git commands."""
from __future__ import annotations

import os
from pathlib import Path
import time
from types import TracebackType
from typing import BinaryIO

if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import msvcrt
else:  # pragma: no cover - exercised on POSIX only
    import fcntl


class FileLockTimeout(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: Path, *, timeout: float = 10.0, poll: float = 0.05):
        self.path = Path(path)
        self.timeout = max(0.0, float(timeout))
        self.poll = max(0.01, float(poll))
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._acquire(handle)
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise FileLockTimeout(f"文件锁被占用：{self.path}")
                time.sleep(self.poll)
                continue
            self._handle = handle
            return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            self._release(handle)
        finally:
            handle.close()

    @staticmethod
    def _acquire(handle: BinaryIO) -> None:
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(  # type: ignore[attr-defined]
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
            )

    @staticmethod
    def _release(handle: BinaryIO) -> None:
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
