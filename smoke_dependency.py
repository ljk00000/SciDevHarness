"""Minimal dependency smoke test for SciDevHarness."""

from pathlib import Path
import sys

import PySide6

from scidev_client import ClientWindow, CodeEditor
from scidev_core import CodingToolbox, EventLedger


project_root = Path(__file__).resolve().parent
assert project_root.name == "SciDevHarness"
assert sys.prefix != sys.base_prefix, "not running inside the project virtual environment"
assert PySide6.__version__
assert ClientWindow.__name__ == "ClientWindow"
assert CodeEditor.__name__ == "CodeEditor"
assert ".research" in CodingToolbox.EXCLUDED_NAMES
EventLedger(project_root)

print("SciDevHarness dependency smoke test: OK")
print(f"python: {sys.executable}")
print(f"PySide6: {PySide6.__version__}")
print("project modules: scidev_client, scidev_core")
