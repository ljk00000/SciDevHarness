from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from scidev_hooks import HookError, HookRunner


class HookTests(unittest.TestCase):
    def _workspace(self) -> tuple[Path, Path]:
        temp = tempfile.TemporaryDirectory(prefix="scidev-hooks-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / ".research").mkdir(parents=True)
        start = root / "start.py"
        start.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "Path('started.json').write_text(os.environ.get('SCIDEV_HOOK_CONTEXT', ''), encoding='utf-8')\n",
            encoding="utf-8",
        )
        deny = root / "deny.py"
        deny.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
        (root / ".research" / "hooks.json").write_text(json.dumps({
            "enabled": True,
            "hooks": {
                "task_start": [{"command": sys.executable, "args": [str(start)]}],
                "before_tool": [{"command": sys.executable, "args": [str(deny)]}],
                "after_tool": [{"command": sys.executable, "args": [str(deny)]}],
                "before_commit": [{"command": sys.executable, "args": [str(deny)]}],
            },
        }), encoding="utf-8")
        return root, start

    def test_disabled_config_runs_nothing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-hooks-disabled-") as temp:
            root = Path(temp)
            (root / ".research").mkdir()
            (root / ".research" / "hooks.json").write_text('{"enabled": false}', encoding="utf-8")
            runner = HookRunner(root)
            self.assertEqual(runner.run("task_start", {"prompt": "x"}), [])

    def test_task_start_hook_receives_bounded_context(self) -> None:
        root, _start = self._workspace()
        runner = HookRunner(root)
        results = runner.run("task_start", {"prompt": "hello", "session_id": "s1"})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].exit_code, 0)
        context = json.loads((root / "started.json").read_text(encoding="utf-8"))
        self.assertEqual(context["prompt"], "hello")
        self.assertEqual(context["session_id"], "s1")

    def test_blocking_hook_failure_raises(self) -> None:
        root, _start = self._workspace()
        runner = HookRunner(root)
        with self.assertRaises(HookError):
            runner.run("before_tool", {"tool": "write_file"})
        with self.assertRaises(HookError):
            runner.run("before_commit", {"paths": ["a.py"]})

    def test_after_tool_failure_is_advisory(self) -> None:
        root, _start = self._workspace()
        runner = HookRunner(root)
        results = runner.run("after_tool", {"tool": "read_file"}, blocking=False)
        self.assertEqual(results[0].exit_code, 3)


if __name__ == "__main__":
    unittest.main()
