from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from scidev_client import ClientWindow


class DebugCommandTests(unittest.TestCase):
    def test_control_command_is_written_to_the_active_pty(self) -> None:
        pty = MagicMock()
        pty.is_alive.return_value = True
        window = MagicMock()
        window.terminal_pty = pty
        window._append_terminal_output = MagicMock()

        ClientWindow._send_debug_command(window, "s")

        pty.write.assert_called_once_with("s\r\n")
        window._append_terminal_output.assert_called_once_with("s\n")

    def test_control_command_without_pdb_only_logs(self) -> None:
        window = MagicMock()
        window.terminal_pty = None
        window._append_log = MagicMock()

        ClientWindow._send_debug_command(window, "c")

        window._append_log.assert_called_once()
        self.assertIn("F5", window._append_log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
