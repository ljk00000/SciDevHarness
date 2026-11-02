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


class _HungProcess:
    returncode = -1

    def communicate(self, timeout: float | None = None):
        if timeout is not None:
            raise subprocess.TimeoutExpired(["python"], timeout)
        return (b"", b"")

    def kill(self) -> None:
        self.returncode = -9


class TestRunnerTimeoutTests(unittest.TestCase):
    def test_a_hung_target_becomes_an_incomplete_timeout_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-runner-timeout-") as temp:
            part = Path(temp) / "part.json"
            with patch.object(runner.subprocess, "Popen", return_value=_HungProcess()), patch.object(
                runner,
                "terminate_process_tree",
            ) as terminate:
                payload = runner._run_target("tests.test_hypothetical", part)
        self.assertIsNotNone(payload)
        assert payload is not None
        terminate.assert_called_once()
        self.assertFalse(payload["success"])
        self.assertFalse(payload["complete"])
        self.assertTrue(payload["timed_out"])
        self.assertEqual(payload["exit"], -1)


if __name__ == "__main__":
    unittest.main()
