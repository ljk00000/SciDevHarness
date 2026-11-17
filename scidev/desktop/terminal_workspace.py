"""Integrated terminal processes and lightweight pdb sessions.

The host retains state and lifecycle ownership; this module does not import its entry point.
"""

from __future__ import annotations

import os
import time
from PySide6.QtCore import QTimer
from PySide6.QtGui import QTextCursor
from scidev.runtime.process import StreamingCommand
from scidev.runtime.process import TerminalOutputDecoder as TerminalOutputDecoder
from scidev.runtime.pty import PTYSession
from scidev.runtime.pty import PTYUnavailable
from scidev.workspace.debug import DebugError
from scidev.workspace.debug import breakpoint_commands
from scidev.workspace.debug import build_debug_argv
from scidev.workspace.debug import command_line as debug_command_line


class TerminalWorkspaceMixin:
    """Operate on host state; introduce no initialization or additional state."""

    def _toggle_bottom_panel(self, tab_index: int | None = None) -> None:
        if hasattr(self, "workspace_stack") and self.workspace_stack.currentWidget() is not self.workspace_page:
            self.workspace_stack.setCurrentWidget(self.workspace_page)
        if tab_index is not None:
            if self.bottom_tabs.isVisible() and self.bottom_tabs.currentIndex() == tab_index:
                self.bottom_tabs.setVisible(False)
                return
            self.bottom_tabs.setCurrentIndex(tab_index)
            self.bottom_tabs.setVisible(True)
        else:
            self.bottom_tabs.setVisible(not self.bottom_tabs.isVisible())
        if self.bottom_tabs.isVisible() and tab_index == 1:
            self.terminal_input.setFocus()


    def _debug_current_file(self) -> None:
        if self.current_file is None:
            self._append_log("请先打开一个 Python 文件，再按 F5 调试")
            return
        if self.terminal_pty is not None and self.terminal_pty.is_alive():
            self._append_log("终端已有正在运行的程序；先停止它再启动 pdb")
            return
        if self.terminal_process is not None and self.terminal_process.exit_code() is None:
            self._append_log("终端已有正在运行的程序；先停止它再启动 pdb")
            return
        try:
            argv = build_debug_argv(self.project_root, self.current_file)
        except DebugError as exc:
            self._append_log(f"无法启动 pdb: {exc}")
            return
        editor = self._active_editor()
        if editor is not None and editor.document().isModified():
            self._append_log("提示：pdb 运行磁盘版本，当前文件有未保存修改")
        self.terminal_input.setText(debug_command_line(argv))
        self._run_terminal_command()
        current_line = editor.textCursor().blockNumber() + 1 if editor is not None else 1
        if current_line > 1 and self.terminal_pty is not None:
            try:
                commands = breakpoint_commands(str(argv[-1]), current_line)
            except DebugError:
                return
            QTimer.singleShot(450, lambda: self._send_debug_startup(commands))


    def _send_debug_startup(self, commands: str) -> None:
        pty = self.terminal_pty
        if pty is None or not pty.is_alive():
            return
        try:
            pty.write(commands.replace("\n", "\r\n"))
            self._append_terminal_output(commands)
        except PTYUnavailable as exc:
            self._append_log(f"无法向 pdb 发送断点命令: {exc}")


    def _send_debug_command(self, command: str) -> None:
        pty = self.terminal_pty
        if pty is None or not pty.is_alive():
            self._append_log("pdb 未运行；先按 F5 启动调试")
            return
        try:
            pty.write(command + "\r\n")
            self._append_terminal_output(command + "\n")
        except PTYUnavailable as exc:
            self._append_log(f"无法向 pdb 发送命令: {exc}")


    def _run_terminal_command(self) -> None:
        command = self.terminal_input.text().strip()
        if not command:
            return
        if self.terminal_pty is not None and self.terminal_pty.is_alive():
            self.terminal_input.clear()
            try:
                self.terminal_pty.write(command + "\r\n")
                self._append_terminal_output(command + "\n")
            except PTYUnavailable as exc:
                self._append_terminal_output(f"\n[ConPTY 输入失败: {exc}]\n")
            return
        if self.terminal_process is not None and self.terminal_process.exit_code() is None:
            self._append_terminal_output("\n[已有命令正在运行]\n")
            return
        if not self.bottom_tabs.isVisible() or self.bottom_tabs.currentIndex() != 1:
            self._toggle_bottom_panel(1)
        self.terminal_input.clear()
        self._append_terminal_output(f"\n$ {command}\n")
        self._terminal_buffer = ""
        fallback_encoding = "mbcs" if os.name == "nt" else "utf-8"
        self._terminal_decoders = {
            "stdout": TerminalOutputDecoder(fallback_encoding),
            "stderr": TerminalOutputDecoder(fallback_encoding),
        }
        environment = os.environ.copy()
        if os.name == "nt":
            environment["PYTHONUTF8"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"
        if PTYSession.available():
            pty = PTYSession()
            try:
                pty.start(command, self.terminal_cwd, environment)
            except PTYUnavailable as exc:
                pty.close()
                self._append_terminal_output(f"\n[ConPTY 不可用，回退管道: {exc}]\n")
            else:
                self.terminal_pty = pty
                self._terminal_started_at = time.monotonic()
                self._terminal_timer.start()
                self.ledger.append(
                    "terminal_command",
                    {
                        "command": command,
                        "cwd": self.terminal_cwd.relative_to(self.project_root).as_posix() or ".",
                        "backend": "conpty",
                    },
                )
                return
        process = StreamingCommand()
        self.terminal_process = process
        try:
            process.start(command, self.terminal_cwd, environment)
        except OSError as exc:
            self.terminal_process = None
            self._append_terminal_output(f"\n[进程错误: {exc}]\n")
            return
        self._terminal_started_at = time.monotonic()
        self._terminal_timer.start()
        self.ledger.append(
            "terminal_command",
            {
                "command": command,
                "cwd": self.terminal_cwd.relative_to(self.project_root).as_posix() or ".",
                "backend": "pipe",
            },
        )


    def _poll_terminal_output(self) -> None:
        pty = self.terminal_pty
        if pty is not None:
            self._read_terminal_output()
            if pty.is_alive():
                return
            if not pty.eof and time.monotonic() - self._terminal_started_at < 0.5:
                return
            exit_code = pty.exit_code()
            if exit_code is None and time.monotonic() - self._terminal_started_at < 0.3:
                return
            self._terminal_finished(int(exit_code or 0))
            return
        process = self.terminal_process
        if process is None:
            self._terminal_timer.stop()
            return
        self._read_terminal_output()
        exit_code = process.exit_code()
        if exit_code is None:
            return
        # Give the pipe readers a moment to flush the final bytes, then finish
        # even if a grandchild keeps the inherited handle open.
        if not process.streams_finished() and time.monotonic() - self._terminal_started_at < 0.6:
            return
        self._terminal_finished(exit_code)


    def _read_terminal_output(self) -> None:
        pty = self.terminal_pty
        if pty is not None:
            output = pty.drain()
            if output:
                self._terminal_buffer += output
                self._append_terminal_output(output)
            return
        process = self.terminal_process
        if process is None:
            return
        output = ""
        for stream, raw in process.drain():
            decoder = self._terminal_decoders.get(stream)
            output += decoder.decode(raw) if decoder is not None else self._decode_terminal_bytes(raw)
        if output:
            self._terminal_buffer += output
            self._append_terminal_output(output)


    @staticmethod
    def _decode_terminal_bytes(raw: bytes) -> str:
        if not raw:
            return ""
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            fallback = "mbcs" if os.name == "nt" else "utf-8"
            return raw.decode(fallback, errors="replace")


    def _append_terminal_output(self, text: str) -> None:
        self.terminal_output.moveCursor(QTextCursor.MoveOperation.End)
        self.terminal_output.insertPlainText(text)
        self.terminal_output.moveCursor(QTextCursor.MoveOperation.End)


    def _terminal_finished(self, exit_code: int) -> None:
        pty = self.terminal_pty
        if pty is not None:
            self._read_terminal_output()
            pty.close()
            self.terminal_pty = None
        self._read_terminal_output()
        pending_text = "".join(
            decoder.decode(b"", final=True) for decoder in self._terminal_decoders.values()
        )
        self._terminal_decoders.clear()
        if pending_text:
            self._terminal_buffer += pending_text
            self._append_terminal_output(pending_text)
        self._append_terminal_output(f"\n[退出码 {exit_code}]\n")
        if exit_code != 0:
            self.problems_output.setPlainText(self._terminal_buffer.strip() or f"命令退出码: {exit_code}")
        self._terminal_timer.stop()
        self.refresh_git_status()
        process = self.terminal_process
        self.terminal_process = None
        if process is not None:
            process.close()
