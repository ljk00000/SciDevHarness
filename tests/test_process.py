"""Captured desktop commands must not create a Windows console window."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scidev_context import ProjectContext
from scidev_core import CodingToolbox, EventLedger, GitManager, PermanentError
from scidev_process import background_process_options

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_QUICK_BACKEND"] = "software"
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from scidev_client import ClientWindow


class BackgroundProcessTests(unittest.TestCase):
    def test_non_windows_options_are_empty(self):
        with patch("scidev_process.os", SimpleNamespace(name="posix")):
            self.assertEqual(background_process_options(), {})

    @unittest.skipUnless(os.name == "nt", "Windows process flags")
    def test_windows_options_disable_console_and_hide_startup_window(self):
        first, second = background_process_options(), background_process_options()
        self.assertEqual(first["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertTrue(first["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(first["startupinfo"].wShowWindow, subprocess.SW_HIDE)
        self.assertIsNot(first["startupinfo"], second["startupinfo"])

    def test_git_checks_keep_capture_check_environment_and_background_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = GitManager(Path(temporary))
            completed = subprocess.CompletedProcess(["git", "status"], 0, "clean", "")
            with patch("scidev_core.background_process_options", return_value={"creationflags": 123}):
                with patch("scidev_core.subprocess.run", return_value=completed) as run:
                    result = manager._run(["status"], check=True, env={"TEST": "preserve"})
            self.assertIs(result, completed)
            kwargs = run.call_args.kwargs
            self.assertEqual(run.call_args.args[0], ["git", "status"])
            self.assertEqual(kwargs["creationflags"], 123)
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["check"])
            self.assertEqual(kwargs["env"], {"TEST": "preserve"})

    def test_git_listing_stays_captured_and_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            context = ProjectContext(Path(temporary), protected=lambda _path: False)
            completed = subprocess.CompletedProcess([], 0, b"index.html\0note.txt\0", b"")
            with patch("scidev_context.background_process_options", return_value={"creationflags": 123}):
                with patch("scidev_context.subprocess.run", return_value=completed) as run:
                    files = context._git_files()
            self.assertEqual([file.name for file in files], ["index.html", "note.txt"])
            self.assertEqual(run.call_args.kwargs["creationflags"], 123)
            self.assertEqual(run.call_args.kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(run.call_args.kwargs["timeout"], 4)

    def test_approved_agent_launch_preserves_output_and_denial(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = EventLedger(root)
            toolbox = CodingToolbox(root, ledger, command_approval=lambda *_args: True)
            process = Mock(returncode=7)
            process.communicate.return_value = ("failure detail", None)
            try:
                with patch("scidev_core.background_process_options", return_value={"creationflags": 123}):
                    with patch("scidev_core.subprocess.Popen", return_value=process) as launch:
                        result = toolbox.run_command("python -V", timeout_seconds=10)
                self.assertEqual(launch.call_args.kwargs["creationflags"], 123)
                self.assertEqual(launch.call_args.kwargs["stderr"], subprocess.STDOUT)
                self.assertIn("failure detail", result)
                self.assertEqual(toolbox.last_command_execution["exit_code"], 7)
                toolbox.command_approval = lambda *_args: False
                with patch("scidev_core.subprocess.Popen") as launch:
                    with self.assertRaisesRegex(PermanentError, "命令未执行"):
                        toolbox.run_command("python -V")
                launch.assert_not_called()
            finally:
                toolbox.close()

    @unittest.skipUnless(os.name == "nt", "Real Windows console handle")
    def test_real_direct_and_shell_children_have_no_console_but_keep_error_output(self):
        script = (
            "import ctypes,sys; "
            "print('console=' + str(ctypes.windll.kernel32.GetConsoleWindow())); "
            "print('stderr-preserved', file=sys.stderr); sys.exit(7)"
        )
        for shell in (False, True):
            with self.subTest(shell=shell):
                argv = [sys.executable, "-c", script]
                command = subprocess.list2cmdline(argv) if shell else argv
                result = subprocess.run(
                    command, shell=shell, capture_output=True, text=True,
                    timeout=10, **background_process_options(),
                )
                self.assertEqual(result.returncode, 7)
                self.assertIn("console=0", result.stdout)
                self.assertIn("stderr-preserved", result.stderr)


@unittest.skipUnless(os.name == "nt", "Windows desktop subprocess options")
class DesktopGitBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_real_version_tree_and_save_route_every_git_check_through_hidden_launch(self):
        with tempfile.TemporaryDirectory(prefix="scidev-hidden-git-") as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            source = root / "demo.py"
            source.write_text("value = 1\n", encoding="utf-8")
            settings = QSettings(str(base / "ui.ini"), QSettings.Format.IniFormat)
            real_run = subprocess.run
            calls = []
            phase = "startup"

            def run(*args, **kwargs):
                if args[0][0] == "git":
                    self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
                    self.assertTrue(kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
                    self.assertEqual(kwargs["startupinfo"].wShowWindow, subprocess.SW_HIDE)
                    calls.append((phase, args[0]))
                return real_run(*args, **kwargs)

            with patch("subprocess.run", side_effect=run), patch("scidev_client.QSettings", return_value=settings):
                GitManager(root).init()
                window = ClientWindow(root)
                try:
                    window._open_file(source, preview=False)
                    window.code_editor.insertPlainText("# saved without console\n")
                    phase = "save"
                    window.save_current_file()
                    self.assertIn("saved without console", source.read_text(encoding="utf-8"))
                    self.assertFalse(window.code_editor.document().isModified())
                    self.assertTrue(window.wait_for_git_status(), "background Git refresh did not settle")
                    phase = "version_tree"
                    window.show_git()
                    self.app.processEvents()
                    self.assertIs(window.workspace_stack.currentWidget(), window.git_page)
                    self.assertTrue(window.git_tree.nodes)
                    for expected in ("startup", "save", "version_tree"):
                        self.assertTrue(any(name == expected for name, _ in calls), expected)
                finally:
                    window.close()
                    self.app.processEvents()


class ProcessHardeningTests(unittest.TestCase):
    def test_destructive_guard_normalises_spacing_and_flag_order(self) -> None:
        from scidev_core import destructive_command_reason

        for command in (
            "git  reset --hard",
            "git clean -xdf",
            "git clean -fd",
            "rm  -fr build",
            "rmdir /s /q build",
            "del /q /s cache",
        ):
            self.assertTrue(destructive_command_reason(command), command)
        for command in ("git status --short", "git clean -n", "pytest -q", "python -V"):
            self.assertIsNone(destructive_command_reason(command), command)

    def test_destructive_command_is_rejected_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary))
            toolbox = CodingToolbox(Path(temporary), ledger, command_approval=lambda *_args: True)
            try:
                with patch("scidev_core.subprocess.Popen") as launch:
                    with self.assertRaisesRegex(PermanentError, "破坏性"):
                        toolbox.run_command("git  reset --hard")
                launch.assert_not_called()
            finally:
                toolbox.close()

    def test_timeout_terminates_the_whole_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = EventLedger(Path(temporary))
            toolbox = CodingToolbox(Path(temporary), ledger, command_approval=lambda *_args: True)
            process = Mock(returncode=-9)
            process.communicate.side_effect = [
                subprocess.TimeoutExpired("cmd", 1),
                ("partial output", None),
            ]
            try:
                with (
                    patch("scidev_core.subprocess.Popen", return_value=process),
                    patch("scidev_core.terminate_process_tree") as stop_tree,
                ):
                    result = toolbox.run_command("python -V", timeout_seconds=1)
                stop_tree.assert_called_once_with(process)
                self.assertIn("timed_out", result)
            finally:
                toolbox.close()

    def test_streaming_terminal_kill_stops_the_tree(self) -> None:
        from scidev_process import StreamingCommand

        process = Mock()
        process.poll.return_value = None
        command = StreamingCommand()
        command._process = process
        with patch("scidev_process.terminate_process_tree") as stop_tree:
            command.kill()
        stop_tree.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
