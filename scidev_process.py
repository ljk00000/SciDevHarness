"""Platform-specific launch options for captured desktop subprocesses."""
from __future__ import annotations

import os
import queue
import subprocess
import threading
from typing import Any


def background_process_options() -> dict[str, Any]:
    """Prevent Windows console flashes without changing pipes or shell policy."""
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}


def shell_command_arguments(command: str) -> list[str] | str:
    """Build a platform shell invocation that preserves quoted executables.

    On Windows the whole command line is passed verbatim so embedded quotes in
    ``"C:\\path\\python.exe" -c "..."`` reach ``cmd.exe`` unchanged; letting
    ``subprocess`` quote a single argv element would escape them.
    """
    if os.name == "nt":
        shell = os.environ.get("COMSPEC", "cmd.exe")
        return f'"{shell}" /d /c chcp 65001>nul & {command}'
    return [os.environ.get("SHELL", "/bin/sh"), "-lc", command]


def terminate_process_tree(process: subprocess.Popen[Any], timeout: float = 5.0) -> None:
    """Stop a process and every child it spawned.

    ``Popen.kill`` on Windows only terminates ``cmd.exe``; pytest or a dev
    server started by the command would keep running as an orphan. ``taskkill
    /T`` walks the tree instead.
    """
    try:
        if process.poll() is not None:
            return
    except (AttributeError, OSError):
        pass
    pid = getattr(process, "pid", None)
    if os.name == "nt" and isinstance(pid, int):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=timeout,
                **background_process_options(),
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    try:
        process.kill()
    except (OSError, AttributeError):
        pass


class StreamingCommand:
    """Run one shell command without an external console and stream its output.

    Qt's ``QProcess`` cannot set Windows creation flags from PySide6, so the
    built-in terminal used to inherit (or spawn) a visible console. This keeps
    the hidden-window options in one place and exposes bytes for the caller to
    decode, which keeps encoding policy out of the process layer.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._chunks: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self._open_streams = 0
        self._lock = threading.Lock()
        self._readers: list[threading.Thread] = []

    def start(
        self,
        command: str,
        cwd: os.PathLike[str] | str,
        environment: dict[str, str] | None = None,
    ) -> None:
        if self._process is not None:
            raise RuntimeError("命令行已在运行")
        self._process = subprocess.Popen(
            shell_command_arguments(command),
            cwd=os.fspath(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **background_process_options(),
        )
        with self._lock:
            self._open_streams = 2
        for name in ("stdout", "stderr"):
            reader = threading.Thread(target=self._read_stream, args=(name,), daemon=True)
            self._readers.append(reader)
            reader.start()

    def _read_stream(self, name: str) -> None:
        process = self._process
        stream = getattr(process, name, None) if process is not None else None
        try:
            if stream is not None:
                descriptor = stream.fileno()
                while True:
                    try:
                        chunk = os.read(descriptor, 8192)
                    except OSError:
                        break
                    if not chunk:
                        break
                    self._chunks.put((name, chunk))
        finally:
            with self._lock:
                self._open_streams -= 1

    def drain(self) -> list[tuple[str, bytes]]:
        """Return every chunk received since the previous call, in order."""
        chunks: list[tuple[str, bytes]] = []
        while True:
            try:
                chunks.append(self._chunks.get_nowait())
            except queue.Empty:
                break
        return chunks

    def exit_code(self) -> int | None:
        if self._process is None:
            return None
        return self._process.poll()

    def streams_finished(self) -> bool:
        with self._lock:
            return self._open_streams <= 0

    def kill(self) -> None:
        process = self._process
        if process is not None:
            terminate_process_tree(process)
        self.close()

    def close(self) -> None:
        """Release the pipe handles once the caller has drained the output."""
        process = self._process
        if process is None:
            return
        for stream in (process.stdout, process.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass
        self._process = None
