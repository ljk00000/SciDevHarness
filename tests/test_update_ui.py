from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from scidev_update import UpdateError, UpdateInfo
from scidev_update_ui import UpdateDialog


def _info(available: bool = True) -> UpdateInfo:
    return UpdateInfo(
        current="0.1.0",
        latest="0.2.0" if available else "0.1.0",
        url="https://github.com/example/repo/releases/tag/v0.2.0",
        published_at="2026-01-01",
        notes="release notes",
        asset_url="https://example/setup.exe",
        asset_name="setup.exe",
        asset_size=123,
        asset_digest="sha256:" + "a" * 64,
    )


class UpdateDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_reports_available_release_without_installing(self) -> None:
        dialog = UpdateDialog(_info())
        self.assertEqual(dialog.windowTitle(), "检查更新")
        self.assertIn("0.1.0", dialog.summary_label.text())
        self.assertIn("0.2.0", dialog.summary_label.text())
        self.assertIn("setup.exe", dialog.detail_label.text())
        self.assertTrue(dialog.open_button.isEnabled())
        with patch.object(UpdateDialog, "_open_release") as open_release:
            dialog.open_button.click()
            open_release.assert_called_once()
        dialog.close()

    def test_current_release_is_reported_as_up_to_date(self) -> None:
        dialog = UpdateDialog(_info(available=False))
        self.assertIn("已是最新", dialog.summary_label.text())
        dialog.close()

    def test_download_requires_a_release_asset(self) -> None:
        info = _info()
        info = UpdateInfo(
            current=info.current,
            latest=info.latest,
            url=info.url,
            published_at=info.published_at,
            notes=info.notes,
        )
        dialog = UpdateDialog(info)
        self.assertFalse(dialog.download_button.isEnabled())
        dialog.close()

    def test_workspace_checker_is_injected_and_dialogs_are_not_modal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-update-ui-", ignore_cleanup_errors=True) as temp:
            seen: list[str] = []

            def checker(*, repo: str) -> UpdateInfo:
                seen.append(repo)
                return _info()

            with patch.object(UpdateDialog, "exec", return_value=0):
                dialog = UpdateDialog.show_for_workspace(Path(temp), checker=checker)
            self.assertIsNotNone(dialog)
            self.assertEqual(len(seen), 1)
            assert dialog is not None
            dialog.close()

    def test_checker_failure_shows_a_warning(self) -> None:
        def checker(*, repo: str) -> UpdateInfo:
            raise UpdateError("offline")

        with tempfile.TemporaryDirectory(prefix="scidev-update-ui-", ignore_cleanup_errors=True) as temp:
            with patch("scidev_update_ui.QMessageBox.warning") as warning:
                dialog = UpdateDialog.show_for_workspace(Path(temp), checker=checker)
        self.assertIsNone(dialog)
        warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
