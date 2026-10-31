from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scidev_checkpoint import WorkspaceCheckpoint
from scidev_core import CodingAgent, CodingToolbox, EventLedger, GitManager


class WorkspaceCheckpointTests(unittest.TestCase):
    def test_capture_existing_and_new_files_then_restore(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            existing = root / "app.py"
            existing.write_text("original\n", encoding="utf-8")
            checkpoint = WorkspaceCheckpoint(root, "task-1")
            checkpoint.capture(existing)
            existing.write_text("changed\n", encoding="utf-8")
            created = root / "new.py"
            checkpoint.capture(created)
            created.write_text("created by agent\n", encoding="utf-8")

            result = checkpoint.restore()

            self.assertEqual(existing.read_text(encoding="utf-8"), "original\n")
            self.assertFalse(created.exists())
            self.assertEqual(result["restored"], ["app.py"])
            self.assertEqual(result["deleted"], ["new.py"])
            self.assertEqual(result["failed"], {})

    def test_capture_is_idempotent_and_keeps_the_first_bytes(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            target = root / "app.py"
            target.write_text("first\n", encoding="utf-8")
            checkpoint = WorkspaceCheckpoint(root, "task-2")
            checkpoint.capture(target)
            target.write_text("second\n", encoding="utf-8")
            checkpoint.capture(target)
            target.write_text("third\n", encoding="utf-8")

            checkpoint.restore()

            self.assertEqual(target.read_text(encoding="utf-8"), "first\n")

    def test_manifest_is_written_and_prune_keeps_recent_directories(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            target = root / "app.py"
            target.write_text("original\n", encoding="utf-8")
            first = WorkspaceCheckpoint(root, "first")
            first.capture(target)
            second = WorkspaceCheckpoint(root, "second")
            second.capture(target)

            manifest = json.loads((second.root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["checkpoint_id"], "second")
            self.assertEqual(manifest["entries"][0]["path"], "app.py")
            self.assertTrue((second.root / "files" / manifest["entries"][0]["backup"]).is_file())

            WorkspaceCheckpoint.prune(root, keep=1)
            self.assertFalse(first.root.exists())
            self.assertTrue(second.root.exists())

    def test_toolbox_writes_are_captured_before_restore(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            (root / "app.py").write_text("original\n", encoding="utf-8")
            checkpoint = WorkspaceCheckpoint(root, "task-3")
            toolbox = CodingToolbox(root, EventLedger(root))
            toolbox.set_checkpoint(checkpoint)

            toolbox.write_file("app.py", "changed\n")
            toolbox.write_file("new.py", "new\n")
            checkpoint.restore()

            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "original\n")
            self.assertFalse((root / "new.py").exists())

    def test_agent_restore_reports_checkpoint_result(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            target = root / "app.py"
            target.write_text("original\n", encoding="utf-8")
            checkpoint = WorkspaceCheckpoint(root, "task-4")
            checkpoint.capture(target)
            target.write_text("changed\n", encoding="utf-8")
            agent = CodingAgent(
                root, EventLedger(root), GitManager(root),
                require_verification=False,
            )
            agent.last_checkpoint = checkpoint

            result = agent.restore_last_checkpoint()

            self.assertEqual(result["restored"], ["app.py"])
            self.assertEqual(target.read_text(encoding="utf-8"), "original\n")
            self.assertIn(
                "checkpoint_restored",
                (root / ".research" / "events.jsonl").read_text(encoding="utf-8"),
            )

    def test_missing_checkpoint_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            agent = CodingAgent(
                root, EventLedger(root), GitManager(root),
                require_verification=False,
            )
            with self.assertRaisesRegex(Exception, "没有可回滚"):
                agent.restore_last_checkpoint()


if __name__ == "__main__":
    unittest.main()
