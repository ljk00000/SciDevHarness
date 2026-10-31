from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scidev_migrations import SCHEMA_VERSION, migrate_project


class MigrationTests(unittest.TestCase):
    def _project(self, root: Path) -> None:
        research = root / ".research"
        (research / "sessions").mkdir(parents=True)
        (research / "settings.json").write_text(
            json.dumps({
                "execution_mode": "nonsense",
                "network_policy": "invalid",
                "allowed_commands": ["pytest -q", "../escape", "ruff check ."],
                "max_agent_turns": 999,
            }),
            encoding="utf-8",
        )
        (research / "sessions" / "old.json").write_text(
            json.dumps({"session_id": "old", "messages": "not-a-list"}),
            encoding="utf-8",
        )
        with sqlite3.connect(research / "events.db") as connection:
            connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
            connection.commit()

    def test_dry_run_then_migrate_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-migrate-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            self._project(root)
            before = (root / ".research" / "settings.json").read_text(encoding="utf-8")
            preview = migrate_project(root, dry_run=True)
            self.assertTrue(preview.changed)
            self.assertEqual((root / ".research" / "settings.json").read_text(encoding="utf-8"), before)
            self.assertFalse((root / ".research" / "backups").exists())

            report = migrate_project(root)
            self.assertFalse(report.errors)
            self.assertTrue(report.backups)
            settings = json.loads((root / ".research" / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["schema_version"], SCHEMA_VERSION)
            self.assertEqual(settings["execution_mode"], "workspace-write")
            self.assertEqual(settings["network_policy"], "ask")
            self.assertEqual(settings["allowed_commands"], ["pytest -q", "ruff check ."])
            self.assertEqual(settings["max_agent_turns"], 64)
            session = json.loads((root / ".research" / "sessions" / "old.json").read_text(encoding="utf-8"))
            self.assertEqual(session["schema_version"], SCHEMA_VERSION)
            self.assertEqual(session["messages"], [])
            self.assertEqual(session["attachments"], [])
            with sqlite3.connect(root / ".research" / "events.db") as connection:
                self.assertEqual(int(connection.execute("PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)

            second = migrate_project(root)
            self.assertFalse(second.changed)
            self.assertFalse(second.backups)

    def test_broken_settings_are_reported_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-migrate-broken-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            research = root / ".research"
            research.mkdir()
            path = research / "settings.json"
            path.write_text("{not json", encoding="utf-8")
            report = migrate_project(root)
            self.assertTrue(report.errors)
            self.assertEqual(path.read_text(encoding="utf-8"), "{not json")


if __name__ == "__main__":
    unittest.main()
