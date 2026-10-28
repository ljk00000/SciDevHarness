"""Keep the test suite from writing into the real checkout or user settings.

Importing this module (which unittest discovery does before running any test)
redirects ``tempfile`` away from a VCS work tree and stops Git from discovering
an enclosing repository. That combination previously let the suite commit test
fixtures into the developer's real branch.
"""
from __future__ import annotations

import os
import pathlib
import re
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _vcs_root(path: pathlib.Path) -> pathlib.Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _isolate_temp() -> pathlib.Path:
    current = pathlib.Path(tempfile.gettempdir()).resolve()
    if _vcs_root(current) is None and current != ROOT:
        return current
    fallback = ROOT / ".tmp-tests"
    fallback.mkdir(parents=True, exist_ok=True)
    for name in ("TEMP", "TMP", "TMPDIR"):
        os.environ[name] = str(fallback)
    tempfile.tempdir = str(fallback)
    # Even inside the checkout, a temporary project must not inherit the
    # repository that hosts it.
    os.environ.setdefault("GIT_CEILING_DIRECTORIES", str(ROOT))
    return fallback


ISOLATED_TEMP = _isolate_temp()


class TestIsolationGuard(unittest.TestCase):
    def test_temp_directory_is_never_a_vcs_work_tree(self) -> None:
        resolved = pathlib.Path(tempfile.gettempdir()).resolve()
        self.assertNotEqual(resolved, ROOT, "temp must never be the checkout itself")
        enclosing = _vcs_root(resolved)
        if enclosing is not None:
            # Local sandboxes can refuse the system temp dir. The controlled
            # fallback is allowed only because Git discovery is ceilinged here.
            self.assertEqual(resolved, (ROOT / ".tmp-tests").resolve())
            self.assertEqual(os.environ.get("GIT_CEILING_DIRECTORIES"), str(ROOT))

    def test_tests_never_open_the_real_user_settings(self) -> None:
        pattern = re.compile(r'QSettings\(\s*["\']SciDevHarness["\']')
        offenders = []
        for path in sorted((ROOT / "tests").glob("*.py")):
            if path.name == pathlib.Path(__file__).name:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [], "tests must patch QSettings to a temporary INI")
