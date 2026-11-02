from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
import time
import unittest

from scidev_filelock import FileLock, FileLockTimeout


class FileLockTests(unittest.TestCase):
    def test_lock_excludes_another_process_and_is_released(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-lock-", ignore_cleanup_errors=True) as temp:
            path = Path(temp) / "project.lock"
            script = (
                "import sys, time\n"
                "from pathlib import Path\n"
                "from scidev_filelock import FileLock\n"
                "with FileLock(Path(sys.argv[1])):\n"
                "    print('locked', flush=True)\n"
                "    time.sleep(10)\n"
            )
            holder = subprocess.Popen(
                [sys.executable, "-c", script, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert holder.stdout is not None
                line = holder.stdout.readline().strip()
                self.assertEqual(line, "locked")
                with self.assertRaises(FileLockTimeout):
                    FileLock(path, timeout=0.2, poll=0.02).__enter__()
            finally:
                holder.terminate()
                try:
                    holder.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.wait(timeout=5)
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    with FileLock(path, timeout=0.2):
                        break
                except FileLockTimeout:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)

    def test_mutating_git_commands_are_classified(self) -> None:
        from scidev_core import GitManager

        self.assertTrue(GitManager._is_mutating(["--literal-pathspecs", "add", "-A"]))
        self.assertTrue(GitManager._is_mutating(["commit", "-m", "x"]))
        self.assertFalse(GitManager._is_mutating(["status", "--short"]))
        self.assertFalse(GitManager._is_mutating([]))

    def test_mutation_session_is_reentrant(self) -> None:
        from scidev_core import GitManager

        with tempfile.TemporaryDirectory(prefix="scidev-lock-reentrant-") as temp:
            manager = GitManager(Path(temp))
            with manager._mutation_session():
                with manager._mutation_session():
                    self.assertEqual(manager._mutation_depth(), 2)
                self.assertEqual(manager._mutation_depth(), 1)
            self.assertEqual(manager._mutation_depth(), 0)


if __name__ == "__main__":
    unittest.main()
