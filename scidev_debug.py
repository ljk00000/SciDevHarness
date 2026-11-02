"""Small, dependency-free Python debugger launcher.

This intentionally uses the standard-library ``pdb`` in the integrated
terminal. It is not a DAP debugger with a variables/stack UI; it gives the
existing ConPTY terminal a reliable "debug this file" entry point.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys


class DebugError(RuntimeError):
    pass


def python_interpreter(project_root: Path) -> str:
    root = Path(project_root).resolve()
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        root / "venv" / "Scripts" / "python.exe",
        root / "venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    if not sys.executable:
        raise DebugError("找不到可用的 Python 解释器")
    return sys.executable


def build_debug_argv(project_root: Path, target: Path) -> list[str]:
    root = Path(project_root).resolve()
    path = Path(target).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise DebugError("只能调试工作区内的文件") from exc
    if not path.is_file():
        raise DebugError(f"文件不存在：{relative.as_posix()}")
    if path.suffix.lower() != ".py":
        raise DebugError("当前只支持 Python (.py) 文件的 pdb 调试")
    return [python_interpreter(root), "-m", "pdb", relative.as_posix()]


def command_line(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)
