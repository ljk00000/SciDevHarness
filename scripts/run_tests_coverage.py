"""Run the test suite under coverage and enforce the gates.

PySide6 can access-violate while the interpreter finalises (offscreen Qt).
The tests and coverage data are complete before that point, so the parent
process runs the child, reads its recorded result, and then evaluates coverage
in a fresh process.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / ".research" / "test_result.json"
CHILD_FLAG = "--coverage-child"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run_child() -> None:
    import unittest

    import coverage

    class RecordingResult(unittest.TextTestResult):
        """Persist progress after every test so a native crash is diagnosable."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.last_test = ""

        def startTest(self, test) -> None:  # noqa: N802 - unittest API
            self.last_test = test.id()
            super().startTest(test)
            _write(complete=False, result=self, last_test=self.last_test)

        def stopTest(self, test) -> None:  # noqa: N802 - unittest API
            super().stopTest(test)
            _write(complete=False, result=self, last_test=self.last_test)

        def addFailure(self, test, err) -> None:  # noqa: N802 - unittest API
            super().addFailure(test, err)
            _write(complete=False, result=self, last_test=self.last_test)

        def addError(self, test, err) -> None:  # noqa: N802 - unittest API
            super().addError(test, err)
            _write(complete=False, result=self, last_test=self.last_test)

    def _details(result: unittest.TextTestResult) -> list[dict[str, str]]:
        details: list[dict[str, str]] = []
        for test, traceback_text in list(result.failures) + list(result.errors):
            lines = [line.strip() for line in str(traceback_text).strip().splitlines() if line.strip()]
            details.append({
                "test": getattr(test, "id", lambda: str(test))(),
                "detail": (" | ".join(lines[-2:]) if lines else "")[:500],
            })
        return details

    def _write(*, complete: bool, result: unittest.TextTestResult, last_test: str) -> None:
        RESULT.parent.mkdir(parents=True, exist_ok=True)
        RESULT.write_text(
            json.dumps(
                {
                    "success": result.wasSuccessful(),
                    "tests_run": result.testsRun,
                    "failures": len(result.failures),
                    "errors": len(result.errors),
                    "skipped": len(result.skipped),
                    "failure_details": _details(result),
                    "last_test": last_test,
                    "complete": complete,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    cov = coverage.Coverage(branch=True, config_file=str(ROOT / "pyproject.toml"))
    cov.start()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    cov.stop()
    cov.save()
    _write(complete=True, result=result, last_test=result.last_test)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)


def _parent() -> int:
    RESULT.unlink(missing_ok=True)
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), CHILD_FLAG],
        cwd=str(ROOT),
    )
    if not RESULT.exists():
        print(f"test run aborted (child exit {completed.returncode}); no result recorded")
        return 1
    payload = json.loads(RESULT.read_text(encoding="utf-8"))
    print(json.dumps(payload, ensure_ascii=False))
    if not payload.get("complete"):
        print(
            f"test run aborted after {payload.get('last_test')} "
            f"(child exit {completed.returncode}); partial result recorded"
        )
        for item in payload.get("failure_details") or []:
            print(f"FAILED {item.get('test')}: {item.get('detail')}")
        return 1
    if not payload.get("success"):
        for item in payload.get("failure_details") or []:
            print(f"FAILED {item.get('test')}: {item.get('detail')}")
        return 1
    report = subprocess.run(
        [sys.executable, "-m", "coverage", "report"],
        cwd=str(ROOT),
    )
    return 0 if report.returncode == 0 else 1


if __name__ == "__main__":
    if CHILD_FLAG in sys.argv:
        _run_child()
    else:
        sys.exit(_parent())
