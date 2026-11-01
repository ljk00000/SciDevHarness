from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class StandardEvalSuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suite_path = Path(__file__).resolve().parents[1] / "evals" / "standard" / "suite.json"
        cls.data = json.loads(cls.suite_path.read_text(encoding="utf-8"))

    def test_suite_has_twenty_unique_tasks_with_checks(self) -> None:
        tasks = self.data["tasks"]
        ids = [task["id"] for task in tasks]
        self.assertGreaterEqual(len(tasks), 20)
        self.assertEqual(len(ids), len(set(ids)))
        for task in tasks:
            self.assertTrue(task["prompt"].strip(), task["id"])
            self.assertTrue(task["check"], task["id"])

    def test_each_task_fails_before_the_requested_fix(self) -> None:
        for task in self.data["tasks"]:
            with self.subTest(task=task["id"]):
                with tempfile.TemporaryDirectory(
                    prefix=f"scidev-eval-pre-{task['id']}-", ignore_cleanup_errors=True,
                ) as temp:
                    root = Path(temp)
                    for relative, content in (task.get("setup") or {}).items():
                        target = root / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(content, encoding="utf-8")
                    check = [
                        sys.executable if part == "python" else part
                        for part in task["check"]
                    ]
                    completed = subprocess.run(
                        check,
                        cwd=root,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                    self.assertNotEqual(
                        completed.returncode,
                        0,
                        f"{task['id']} unexpectedly passed before the fix: {completed.stdout}",
                    )


if __name__ == "__main__":
    unittest.main()
