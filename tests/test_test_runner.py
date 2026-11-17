from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "scidev_test_runner",
    ROOT / "scripts" / "run_tests_coverage.py",
)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


class _HungProcess:
    returncode = -1

    def communicate(self, timeout: float | None = None):
        if timeout is not None:
            raise subprocess.TimeoutExpired(["python"], timeout)
        return (b"", b"")

    def kill(self) -> None:
        self.returncode = -9


class _FinishedProcess:
    def __init__(self, returncode, write_result=None):
        self.returncode = returncode
        self.write_result = write_result

    def communicate(self, timeout=None):
        if self.write_result is not None:
            self.write_result()
        return (b"", b"")


class TestRunnerTimeoutTests(unittest.TestCase):
    def test_a_hung_target_becomes_an_incomplete_timeout_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-runner-timeout-") as temp:
            part = Path(temp) / "part.json"
            with patch.object(runner.subprocess, "Popen", return_value=_HungProcess()), patch.object(
                runner,
                "terminate_process_tree",
            ) as terminate:
                payload = runner._run_target("tests.test_hypothetical", part)
        self.assertIsNotNone(payload)
        assert payload is not None
        terminate.assert_called_once()
        self.assertFalse(payload["success"])
        self.assertFalse(payload["complete"])
        self.assertTrue(payload["timed_out"])
        self.assertEqual(payload["exit"], -1)


class TestRunnerExitTests(unittest.TestCase):
    def run_report(self, report, exit_code):
        with tempfile.TemporaryDirectory(prefix="scidev-runner-exit-") as temp:
            part = Path(temp) / "part.json"
            process = _FinishedProcess(
                exit_code,
                lambda: part.write_text(json.dumps(report), encoding="utf-8"),
            )
            with patch.object(runner.subprocess, "Popen", return_value=process):
                return runner._run_target("tests.example", part)

    def test_native_crash_after_success_report_is_not_success(self):
        payload = self.run_report({"success": True, "complete": True, "tests_run": 3}, 3221225477)
        self.assertFalse(payload["success"])
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["exit"], 3221225477)

    def test_assertion_failure_stays_complete_and_is_not_retried_as_a_crash(self):
        payload = self.run_report({"success": False, "complete": True, "failures": 1}, 1)
        self.assertFalse(payload["success"])
        self.assertTrue(payload["complete"])

    def test_clean_exit_and_success_report_are_success(self):
        payload = self.run_report({"success": True, "complete": True}, 0)
        self.assertTrue(payload["success"])
        self.assertTrue(payload["complete"])

    def test_old_success_file_cannot_hide_a_child_that_writes_no_report(self):
        with tempfile.TemporaryDirectory(prefix="scidev-runner-stale-") as temp:
            part = Path(temp) / "part.json"
            part.write_text('{"success": true, "complete": true}', encoding="utf-8")
            with patch.object(runner.subprocess, "Popen", return_value=_FinishedProcess(0)):
                payload = runner._run_target("tests.example", part)
            self.assertFalse(payload["success"])
            self.assertFalse(payload["complete"])

    def test_truncated_result_is_an_incomplete_failure(self):
        with tempfile.TemporaryDirectory(prefix="scidev-runner-truncated-") as temp:
            part = Path(temp) / "part.json"
            process = _FinishedProcess(3221225477, lambda: part.write_text('{"success":', encoding="utf-8"))
            with patch.object(runner.subprocess, "Popen", return_value=process):
                payload = runner._run_target("tests.example", part)
            self.assertFalse(payload["success"])
            self.assertFalse(payload["complete"])

    def test_timeout_retains_failures_reported_before_the_hang(self):
        with tempfile.TemporaryDirectory(prefix="scidev-runner-partial-") as temp:
            part = Path(temp) / "part.json"
            part.write_text(json.dumps({"last_test": "tests.example.test_hung", "tests_run": 2,
                                        "failures": 1, "errors": 0, "skipped": 0,
                                        "failure_details": [{"test": "tests.example.test_bad", "detail": "failed"}]}),
                            encoding="utf-8")
            payload = runner._timeout_payload(part)
        self.assertEqual(payload["failures"], 1)
        self.assertEqual(payload["tests_run"], 2)
        self.assertEqual(payload["failure_details"][0]["test"], "tests.example.test_bad")

    def test_a_native_abort_cannot_retry_away_an_already_reported_assertion_failure(self):
        with tempfile.TemporaryDirectory(prefix="scidev-runner-parent-") as temp:
            root = Path(temp)
            report = root / "result.json"
            payload = {"success": False, "complete": False, "tests_run": 2, "failures": 1,
                       "errors": 0, "skipped": 0, "last_test": "tests.example.test_bad", "exit": 3221225477}
            with patch.object(runner, "ROOT", root), patch.object(runner, "RESULT", report), \
                 patch.object(runner, "PARTS", root / "parts"), \
                 patch.object(runner, "_targets", return_value=["tests.example"]), \
                 patch.object(runner, "_run_target", return_value=payload) as run_target:
                exit_code = runner._parent()
            self.assertEqual(exit_code, 1)
            run_target.assert_called_once()
            self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["failures"], 1)


class TestRunnerQtLifecycleTests(unittest.TestCase):
    def test_closed_workspace_is_disposed_but_live_fixture_survives(self):
        script = '''
import os
from pathlib import Path
import sys
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['QT_QUICK_BACKEND'] = 'software'
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid
from scidev.desktop.client import ClientWindow
from tests.test_test_runner import runner
app = QApplication([])
root = Path(sys.argv[1])
settings = QSettings(str(root/'ui.ini'), QSettings.Format.IniFormat)
with patch('scidev.desktop.client.QSettings', return_value=settings):
    workspace = ClientWindow(root)
live_fixture = QWidget()
workspace.close()
assert isValid(workspace), 'fixture should still have its native window before disposal'
runner._dispose_closed_windows()
assert not isValid(workspace), 'closed native workspace leaked into the next test'
assert isValid(live_fixture), 'the runner deleted a live fixture'
live_fixture.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
os._exit(0)
'''
        with tempfile.TemporaryDirectory(prefix="scidev-runner-qt-") as temp:
            result = subprocess.run([sys.executable, "-B", "-c", script, temp], cwd=ROOT,
                                    capture_output=True, text=True, encoding="utf-8", timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
