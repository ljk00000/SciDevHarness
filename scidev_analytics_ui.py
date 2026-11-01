"""Compact read-only failure trend dialog."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from scidev_analytics import TrendReport, collect_trends, format_sparkline


class ErrorTrendDialog(QDialog):
    def __init__(self, project_root: Path, parent=None):
        super().__init__(parent)
        self.project_root = Path(project_root)
        self.setWindowTitle("错误趋势")
        self.setMinimumSize(660, 420)
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("统计天数"))
        self.days = QSpinBox()
        self.days.setRange(1, 90)
        self.days.setValue(14)
        self.days.valueChanged.connect(self.refresh)
        row.addWidget(self.days)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.spark = QLabel("")
        layout.addWidget(self.spark)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["失败分类", "次数", "占比", "可重试"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self) -> None:
        report = collect_trends(self.project_root, days=self.days.value())
        self._render(report)

    def _render(self, report: TrendReport) -> None:
        total = report.completed + report.failed
        self.summary.setText(
            f"开始 {report.started} · 完成 {report.completed} · 失败 {report.failed} · "
            f"成功率 {report.success_rate:.0%}（完成/已结束，n={total}）"
        )
        days = self.days.value()
        today = datetime.now(timezone.utc).date()
        values: list[int] = []
        for offset in range(days - 1, -1, -1):
            key = (today - timedelta(days=offset)).isoformat()
            values.append(report.by_day.get(key, 0))
        self.spark.setText(f"每日失败：{format_sparkline(values)}  （左旧右新，共 {sum(values)}）")
        self.table.setRowCount(len(report.by_kind))
        for row, (kind, count) in enumerate(report.by_kind.items()):
            share = count / report.failed if report.failed else 0.0
            retryable = report.retryable_kinds.get(kind)
            values_row = [
                kind,
                str(count),
                f"{share:.0%}",
                "未知" if retryable is None else ("是" if retryable else "否"),
            ]
            for column, text in enumerate(values_row):
                self.table.setItem(row, column, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
