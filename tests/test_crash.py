from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from scidev_crash import CrashReporter


class CrashReporterTests(unittest.TestCase):
    def test_records_redacted_exception_and_prunes_old_reports(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-crash-", ignore_cleanup_errors=True) as temp:
            reporter = CrashReporter(Path(temp), version="9.9.9")
            try:
                raise RuntimeError("api_key=super-secret-token and token: abcdef123")
            except RuntimeError:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                path = reporter.record(exc_type, exc_value, exc_traceback)
            self.assertIsNotNone(path)
            assert path is not None
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], "9.9.9")
            self.assertEqual(payload["exception_type"], "RuntimeError")
            self.assertNotIn("super-secret-token", json.dumps(payload))
            self.assertNotIn("abcdef123", json.dumps(payload))
            self.assertIn("redacted", payload["traceback"] + payload["message"])

            for index in range(25):
                old = reporter.report_dir / f"crash-20200101T00000000000{index:02d}Z-1.json"
                old.write_text("{}", encoding="utf-8")
            reporter.record(*sys.exc_info() if sys.exc_info()[0] else (RuntimeError, RuntimeError("x"), None))
            self.assertLessEqual(len(list(reporter.report_dir.glob("crash-*.json"))), 20)

    def test_keyboard_interrupt_is_not_recorded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-crash-int-", ignore_cleanup_errors=True) as temp:
            reporter = CrashReporter(Path(temp))
            self.assertIsNone(reporter.record(KeyboardInterrupt, KeyboardInterrupt(), None))

    def test_install_and_uninstall_restore_hooks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-crash-hook-", ignore_cleanup_errors=True) as temp:
            before = sys.excepthook
            reporter = CrashReporter(Path(temp))
            reporter.install()
            self.assertIs(getattr(sys.excepthook, "__self__", None), reporter)
            reporter.uninstall()
            self.assertIs(sys.excepthook, before)


if __name__ == "__main__":
    unittest.main()
