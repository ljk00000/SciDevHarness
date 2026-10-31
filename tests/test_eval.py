from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from scidev_eval import EvalError, EvalRunner


class EvalHarnessTests(unittest.TestCase):
    def _suite(self, root: Path, *, path_traversal: bool = False) -> Path:
        suite = root / "suite.json"
        tasks = [
            {
                "id": "write-answer",
                "prompt": "write answer",
                "setup": {"answer.txt": "old\n"},
                "check": [sys.executable, "-c", "from pathlib import Path; assert Path('answer.txt').read_text().strip() == 'ok'"],
            },
            {
                "id": "missing-output",
                "prompt": "do nothing",
                "check": [sys.executable, "-c", "from pathlib import Path; assert Path('never.txt').exists()"],
            },
        ]
        if path_traversal:
            tasks[0]["setup"] = {"../escape.txt": "bad"}
        suite.write_text(json.dumps({"name": "unit-suite", "tasks": tasks}), encoding="utf-8")
        return suite

    def test_runner_passes_and_fails_tasks_with_deterministic_checks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-eval-test-") as temp:
            root = Path(temp)
            suite = self._suite(root)

            def fake_runner(work: Path, task):
                if task.task_id == "write-answer":
                    (work / "answer.txt").write_text("ok\n", encoding="utf-8")
                    return {"success": True, "turn_count": 2, "tokens": 100}
                return {"success": True, "turn_count": 1, "tokens": 20}

            runner = EvalRunner(root, suite, task_runner=fake_runner, output_path=root / "report.json")
            report = runner.run()
            self.assertEqual(report["total"], 2)
            self.assertEqual(report["passed"], 1)
            self.assertEqual(report["failed"], 1)
            self.assertEqual(report["pass_rate"], 0.5)
            self.assertEqual(report["total_tokens"], 120)
            self.assertTrue((root / "report.json").is_file())
            self.assertEqual(report["results"][1]["check_exit"], 1)

            def all_pass(work: Path, task):
                if task.task_id == "write-answer":
                    (work / "answer.txt").write_text("ok\n", encoding="utf-8")
                else:
                    (work / "never.txt").write_text("created\n", encoding="utf-8")
                return {"success": True, "turn_count": 1, "tokens": 10}

            again = EvalRunner(root, suite, task_runner=all_pass, output_path=root / "report.json").run()
            self.assertEqual(again["pass_rate"], 1.0)
            self.assertEqual(again["previous_pass_rate"], 0.5)
            self.assertEqual(again["regression"], 0.5)

    def test_unsafe_setup_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-eval-path-") as temp:
            root = Path(temp)
            suite = self._suite(root, path_traversal=True)
            with self.assertRaises(EvalError):
                EvalRunner(root, suite, task_runner=lambda work, task: {"success": True})


if __name__ == "__main__":
    unittest.main()
