"""Windows ConPTY session with a non-blocking reader thread.

The optional ``pywinpty`` wheel provides the native ConPTY bridge. If it is not
installed the desktop terminal keeps using its existing pipe implementation.
"""
from __future__ import annotations

import os
from pathlib import Path
import queue
import re
import tempfile
import threading
from typing import Any

try:  # pragma: no cover - availability is platform/dependency dependent
    import winpty
except ImportError:  # pragma: no cover
    winpty = None


CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
OTHER_ESCAPE_RE = re.compile(r"\x1b[@-_]")


def strip_ansi(text: str) -> str:
    cleaned = OSC_RE.sub("", text)
    cleaned = CSI_RE.sub("", cleaned)
    cleaned = OTHER_ESCAPE_RE.sub("", cleaned)
    cleaned = re.sub(r"\x1b[78]", "", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "")
    # ConPTY clears its screen with long runs of padding spaces. Keep normal
    # alignment but collapse pathological blank runs.
    return re.sub(r"[ \t]{120,}", " ", cleaned)


class PTYUnavailable(RuntimeError):
    pass


class PTYSession:
    def __init__(self) -> None:
        self.process: Any = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._eof = False
        self._batch_path: Path | None = None

    @staticmethod
    def available() -> bool:
        return os.name == "nt" and winpty is not None

    def start(
        self,
        command: str,
        cwd: Path,
        env: dict[str, str] | None = None,
        *,
        rows: int = 30,
        cols: int = 120,
    ) -> None:
        if not self.available():
            raise PTYUnavailable("pywinpty/ConPTY 不可用")
        # A temporary batch file preserves the user's quoting for cmd.exe.
        # Passing the command as one argv element would make pywinpty escape
        # embedded quotes and silently truncate Python -c commands.
        try:
            fd, raw_path = tempfile.mkstemp(prefix="scidev-terminal-", suffix=".cmd")
            batch_path = Path(raw_path)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as handle:
                handle.write("@echo off\r\nchcp 65001>nul\r\n")
                handle.write(command)
                handle.write("\r\n")
            self._batch_path = batch_path
            argv = ["cmd.exe", "/d", "/c", str(batch_path)]
            self.process = winpty.PtyProcess.spawn(
                argv,
                cwd=str(cwd),
                env=dict(env or os.environ),
                dimensions=(max(8, int(rows)), max(20, int(cols))),
            )
        except Exception as exc:  # noqa: BLE001 - normalize native spawn errors
            self._cleanup_batch()
            raise PTYUnavailable(f"无法启动 ConPTY：{exc}") from exc
        self._eof = False
        self._reader = threading.Thread(target=self._read_loop, name="scidev-pty-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while True:
            try:
                data = self.process.read(65536) if self.process is not None else ""
            except (EOFError, OSError):
                self._eof = True
                self._queue.put(None)
                return
            except Exception:  # noqa: BLE001 - a reader failure must not kill the UI
                self._eof = True
                self._queue.put(None)
                return
            if not data:
                self._eof = True
                self._queue.put(None)
                return
            self._queue.put(data)

    def drain(self, *, strip: bool = True) -> str:
        parts: list[str] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._eof = True
                continue
            parts.append(item)
        text = "".join(parts)
        return strip_ansi(text) if strip else text

    def write(self, text: str) -> None:
        if self.process is None:
            raise PTYUnavailable("ConPTY 未启动")
        try:
            self.process.write(text)
        except (EOFError, OSError) as exc:
            raise PTYUnavailable(f"ConPTY 写入失败：{exc}") from exc

    def resize(self, rows: int, cols: int) -> None:
        if self.process is None:
            return
        try:
            self.process.setwinsize(max(8, int(rows)), max(20, int(cols)))
        except (OSError, ValueError):
            pass

    def is_alive(self) -> bool:
        if self.process is None:
            return False
        try:
            return bool(self.process.isalive())
        except (OSError, ValueError):
            return False

    @property
    def eof(self) -> bool:
        return bool(self._eof)

    def exit_code(self) -> int | None:
        if self.process is None:
            return None
        try:
            value = self.process.exitstatus
        except (OSError, ValueError):
            return None
        return int(value) if value is not None else None

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is not None:
            try:
                process.terminate(force=True)
            except (OSError, ValueError):
                pass
            try:
                process.close()
            except (OSError, ValueError):
                pass
            for attribute in ("fileobj", "_server"):
                stream = getattr(process, attribute, None)
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass
        reader = self._reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=0.5)
        self._reader = None
        self._eof = True
        self._cleanup_batch()

    def _cleanup_batch(self) -> None:
        path = self._batch_path
        self._batch_path = None
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
