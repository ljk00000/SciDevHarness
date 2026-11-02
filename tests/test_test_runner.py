from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
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


class TestRunnerTimeoutTests(unittest.TestCase):
    def test_a_hung_target_becomes_an_incomplete_timeout_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-runner-timeout-") as temp:
            part = Path(temp) / "part.json"
            with patch.object(
                runner.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["python"], 1),
            ):
                payload = runner._run_target("tests.test_hypothetical", part)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertFalse(payload["success"])
        self.assertFalse(payload["complete"])
        self.assertTrue(payload["timed_out"])
        self.assertEqual(payload["exit"], -1)


if __name__ == "__main__":
    unittest.main()
