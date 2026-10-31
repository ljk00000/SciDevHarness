from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scidev_intel import PythonIntel


class PythonIntelTests(unittest.TestCase):
    def test_completion_reports_os_members(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-intel-") as temp:
            root = Path(temp)
            intel = PythonIntel(root)
            completions = intel.complete("import os\nos.pa", 2, 5)
        names = {item.name for item in completions}
        self.assertIn("path", names)
        self.assertIn("pardir", names)
        PythonIntel.shutdown()

    def test_completion_is_bounded_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-intel-") as temp:
            intel = PythonIntel(Path(temp))
            completions = intel.complete("import os\nos.", 2, 3, limit=5)
        self.assertLessEqual(len(completions), 5)
        names = [item.name for item in completions]
        self.assertEqual(len(names), len(set(names)))
        PythonIntel.shutdown()


if __name__ == "__main__":
    unittest.main()
