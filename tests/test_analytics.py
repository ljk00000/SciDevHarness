from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

from scidev_analytics import collect_trends, format_sparkline
from scidev_analytics_ui import ErrorTrendDialog
from scidev_core import EventLedger


class AnalyticsTests(unittest.TestCase):
    def test_counts_outcomes_and_classifies_legacy_failures(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-analytics-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            ledger = EventLedger(root)
            ledger.append("task_started", {"task_id": "a", "attempt": 0})
            ledger.append("task_completed", {"task_id": "a"})
            ledger.append("task_failed", {
                "task_id": "b",
                "error": "HTTP 429 too many requests",
                "error_kind": "rate_limit",
                "retryable": True,
            })
            ledger.append("task_failed", {
                "task_id": "c",
                "error": "pytest failed: 1 error",
            })
            ledger.append("tool_called", {"tool": "read_file"})
            report = collect_trends(root, days=7)
            self.assertEqual(report.started, 1)
            self.assertEqual(report.completed, 1)
            self.assertEqual(report.failed, 2)
            self.assertEqual(report.by_kind["rate_limit"], 1)
            self.assertEqual(report.by_kind["verification"], 1)
            self.assertTrue(report.retryable_kinds["rate_limit"])
            self.assertAlmostEqual(report.success_rate, 1 / 3)
            self.assertEqual(sum(report.by_day.values()), 2)

    def test_missing_database_is_an_empty_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-analytics-empty-", ignore_cleanup_errors=True) as temp:
            report = collect_trends(Path(temp), days=14)
            self.assertEqual((report.started, report.completed, report.failed), (0, 0, 0))
            self.assertEqual(report.by_kind, {})
            self.assertEqual(report.success_rate, 0.0)

    def test_sparkline_handles_empty_zero_and_peak(self) -> None:
        self.assertEqual(format_sparkline([]), "")
        self.assertEqual(set(format_sparkline([0, 0])), {" "})
        line = format_sparkline([0, 1, 4, 8])
        self.assertEqual(len(line), 4)
        self.assertEqual(line[-1], "█")


class AnalyticsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_renders_kind_rows_and_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-analytics-ui-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            ledger = EventLedger(root)
            ledger.append("task_completed", {"task_id": "ok"})
            ledger.append("task_failed", {
                "task_id": "bad",
                "error": "HTTP 429",
                "error_kind": "rate_limit",
                "retryable": True,
            })
            dialog = ErrorTrendDialog(root)
            try:
                self.assertEqual(dialog.table.rowCount(), 1)
                self.assertEqual(dialog.table.item(0, 0).text(), "rate_limit")
                self.assertEqual(dialog.table.item(0, 1).text(), "1")
                self.assertEqual(dialog.table.item(0, 3).text(), "是")
                self.assertIn("成功率", dialog.summary.text())
                self.assertIn("每日失败", dialog.spark.text())
            finally:
                dialog.close()


if __name__ == "__main__":
    unittest.main()
