from __future__ import annotations

import os
import tempfile
from pathlib import Path
import unittest

from scidev_debug import DebugError, build_debug_argv, command_line


class DebugLauncherTests(unittest.TestCase):
    def test_venv_interpreter_and_relative_script_are_used(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-debug-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            venv_python = root / ".venv" / "Scripts" / "python.exe"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_bytes(b"fixture")
            script = root / "app.py"
            script.write_text("print('hello')\n", encoding="utf-8")
            argv = build_debug_argv(root, script)
            self.assertEqual(argv, [str(venv_python), "-m", "pdb", "app.py"])
            rendered = command_line(argv)
            self.assertIn("-m", rendered)
            self.assertIn("pdb", rendered)
            self.assertIn("app.py", rendered)

    def test_only_python_files_inside_the_workspace_are_debuggable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-debug-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            (root / "app.txt").write_text("not python", encoding="utf-8")
            with self.assertRaisesRegex(DebugError, "Python"):
                build_debug_argv(root, root / "app.txt")
            with self.assertRaisesRegex(DebugError, "不存在"):
                build_debug_argv(root, root / "missing.py")
        with tempfile.TemporaryDirectory(prefix="scidev-debug-", ignore_cleanup_errors=True) as temp:
            root = Path(temp) / "project"
            outside = Path(temp) / "outside.py"
            root.mkdir()
            outside.write_text("print(1)\n", encoding="utf-8")
            with self.assertRaisesRegex(DebugError, "工作区内"):
                build_debug_argv(root, outside)

    @unittest.skipUnless(os.name == "nt", "Windows quoting is platform-specific")
    def test_windows_command_line_quotes_paths_with_spaces(self) -> None:
        rendered = command_line([r"C:\Program Files\Python\python.exe", "-m", "pdb", "my app.py"])
        self.assertIn('"C:\\Program Files\\Python\\python.exe"', rendered)
        self.assertIn('"my app.py"', rendered)


if __name__ == "__main__":
    unittest.main()
