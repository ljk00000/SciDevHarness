"""Temporary directories that tolerate deferred Qt file handles on Windows.

Qt's file-system model can release its native watches a few event-loop turns
after a window is destroyed.  Standard ``TemporaryDirectory`` cleanup raises
``WinError 32`` in that window, making otherwise deterministic tests flaky.
"""
from __future__ import annotations

import gc
import shutil
import tempfile
import time


class QtTemporaryDirectory(tempfile.TemporaryDirectory):
    def cleanup(self) -> None:
        for attempt in range(8):
            try:
                super().cleanup()
                return
            except (PermissionError, OSError):
                gc.collect()
                time.sleep(0.05 * (attempt + 1))
        shutil.rmtree(self.name, ignore_errors=True)
