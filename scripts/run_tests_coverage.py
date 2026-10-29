"""Run the test suite under coverage and enforce the gates.

PySide6 can access-violate in offscreen Qt while a widget is being constructed
after many earlier windows were closed. A single process therefore loses the
remaining tests. The parent runs each test module (and each ``test_client``
class) in its own child, records partial results after every test, and then
combines coverage. A native crash now costs one target instead of the suite.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / ".research" / "test_result.json"
PARTS = ROOT / ".research" / "test_parts"
CHILD_FLAG = "--coverage-child"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _isolate_child_environment() -> None:
    """Keep every child's temp files and Git discovery out of the checkout.

    Some Python installs cannot write the system temp directory and
    ``tempfile`` then falls back to the current directory, which is this Git
    work tree. Without a ceiling, ``GitManager.init`` would refuse every test
    fixture as "inside a larger repository".
    """
    import tempfile

    fallback = ROOT / ".tmp-tests"
    fallback.mkdir(parents=True, exist_ok=True)
    current = Path(tempfile.gettempdir()).resolve()
    if current == ROOT.resolve() or not os.access(current, os.W_OK):
        for name in ("TEMP", "TMP", "TMPDIR"):
            os.environ[name] = str(fallback)
        tempfile.tempdir = str(fallback)
    os.environ.setdefault("GIT_CEILING_DIRECTORIES", str(ROOT))


def _write_result(path: Path, *, complete: bool, result, last_test: str) -> None:
    import unittest

    details: list[dict[str, str]] = []
    for test, traceback_text in list(result.failures) + list(result.errors):
        lines = [line.strip() for line in str(traceback_text).strip().splitlines() if line.strip()]
        details.append({
            "test": getattr(test, "id", lambda: str(test))(),
            "detail": (" | ".join(lines[-2:]) if lines else "")[:500],
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "success": isinstance(result, unittest.TestResult) and result.wasSuccessful(),
                "tests_run": getattr(result, "testsRun", 0),
                "failures": len(getattr(result, "failures", [])),
                "errors": len(getattr(result, "errors", [])),
                "skipped": len(getattr(result, "skipped", [])),
                "failure_details": details,
                "last_test": last_test,
                "complete": complete,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _run_child(target: str, result_path: Path) -> None:
    import unittest

    import coverage

    _isolate_child_environment()

    class RecordingResult(unittest.TextTestResult):
        """Persist progress after every test so a native crash is diagnosable."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.last_test = ""

        def startTest(self, test) -> None:  # noqa: N802 - unittest API
            self.last_test = test.id()
            super().startTest(test)
            _write_result(result_path, complete=False, result=self, last_test=self.last_test)

        def stopTest(self, test) -> None:  # noqa: N802 - unittest API
            super().stopTest(test)
            _write_result(result_path, complete=False, result=self, last_test=self.last_test)

        def addFailure(self, test, err) -> None:  # noqa: N802 - unittest API
            super().addFailure(test, err)
            _write_result(result_path, complete=False, result=self, last_test=self.last_test)

        def addError(self, test, err) -> None:  # noqa: N802 - unittest API
            super().addError(test, err)
            _write_result(result_path, complete=False, result=self, last_test=self.last_test)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    cov = coverage.Coverage(
        branch=True,
        config_file=str(ROOT / "pyproject.toml"),
        data_file=str(ROOT / ".coverage"),
        data_suffix=True,
    )
    cov.start()
    suite = unittest.defaultTestLoader.loadTestsFromName(target)
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    cov.stop()
    cov.save()
    _write_result(result_path, complete=True, result=result, last_test=result.last_test)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)


def _targets() -> list[str]:
    targets: list[str] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        module = f"tests.{path.stem}"
        if path.stem != "test_client":
            targets.append(module)
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        classes = [
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and any("TestCase" in ast.unparse(base) for base in node.bases)
        ]
        targets.extend(f"{module}.{name}" for name in classes)
    return targets


def _run_target(target: str, part: Path) -> dict | None:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), CHILD_FLAG, target, str(part)],
        cwd=str(ROOT),
    )
    if not part.exists():
        return {"success": False, "complete": False, "last_test": "", "exit": completed.returncode}
    payload = json.loads(part.read_text(encoding="utf-8"))
    payload["exit"] = completed.returncode
    return payload


def _parent() -> int:
    RESULT.unlink(missing_ok=True)
    shutil.rmtree(PARTS, ignore_errors=True)
    PARTS.mkdir(parents=True, exist_ok=True)
    totals = {"success": True, "tests_run": 0, "failures": 0, "errors": 0, "skipped": 0}
    details: list[dict[str, str]] = []
    crashed: list[str] = []
    retried: list[str] = []
    last_test = ""
    for index, target in enumerate(_targets()):
        part = PARTS / f"{index:03d}.json"
        payload = _run_target(target, part)
        if payload is not None and not payload.get("complete"):
            # Offscreen Qt occasionally access-violates inside a fresh child.
            # A retry in another clean process is cheap and keeps one native
            # crash from destroying an otherwise green suite.
            retry_part = PARTS / f"{index:03d}_retry.json"
            payload = _run_target(target, retry_part)
            retried.append(target)
        if payload is None:
            crashed.append(target)
            totals["success"] = False
            details.append({"test": target, "detail": "target produced no result"})
            continue
        for key in ("tests_run", "failures", "errors", "skipped"):
            totals[key] += int(payload.get(key, 0))
        last_test = payload.get("last_test") or last_test
        if not payload.get("complete"):
            crashed.append(target)
            totals["success"] = False
            details.append({
                "test": target,
                "detail": f"aborted after {payload.get('last_test')} (exit {payload.get('exit')})",
            })
        elif not payload.get("success"):
            totals["success"] = False
            details.extend(payload.get("failure_details") or [])
    aggregate = {
        **totals,
        "failure_details": details,
        "last_test": last_test,
        "complete": not crashed,
        "targets": len(_targets()),
        "crashed_targets": crashed,
        "retried_targets": retried,
    }
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(aggregate, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(aggregate, ensure_ascii=False))
    if not aggregate["success"]:
        for item in details:
            print(f"FAILED {item.get('test')}: {item.get('detail')}")
        return 1
    combined = subprocess.run(
        [sys.executable, "-m", "coverage", "combine"],
        cwd=str(ROOT),
    )
    if combined.returncode != 0:
        return 1
    report = subprocess.run(
        [sys.executable, "-m", "coverage", "report"],
        cwd=str(ROOT),
    )
    return 0 if report.returncode == 0 else 1


if __name__ == "__main__":
    if CHILD_FLAG in sys.argv:
        _run_child(sys.argv[sys.argv.index(CHILD_FLAG) + 1], Path(sys.argv[sys.argv.index(CHILD_FLAG) + 2]))
    else:
        sys.exit(_parent())
