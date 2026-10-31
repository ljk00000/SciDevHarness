from __future__ import annotations

import os
from pathlib import Path
import time
import unittest

from scidev_pty import PTYSession, PTYUnavailable, strip_ansi


class PTYTests(unittest.TestCase):
    def test_strip_ansi_removes_csi_and_osc(self) -> None:
        text = "\x1b[31mred\x1b[0m\r\n\x1b]0;title\x07plain"
        self.assertEqual(strip_ansi(text), "red\nplain")

    def test_conpty_runs_a_command_and_reports_exit(self) -> None:
        if not PTYSession.available():
            self.skipTest("pywinpty/ConPTY is not available")
        session = PTYSession()
        try:
            session.start("echo conpty-ok", Path.cwd(), os.environ.copy())
            deadline = time.monotonic() + 4
            output = ""
            while time.monotonic() < deadline:
                output += session.drain()
                if "conpty-ok" in output and not session.is_alive():
                    break
                time.sleep(0.05)
            self.assertIn("conpty-ok", output)
            self.assertEqual(session.exit_code(), 0)
        finally:
            session.close()

    def test_conpty_accepts_stdin(self) -> None:
        if not PTYSession.available():
            self.skipTest("pywinpty/ConPTY is not available")
        session = PTYSession()
        try:
            session.start("set /p X=Name?", Path.cwd(), os.environ.copy())
            deadline = time.monotonic() + 3
            output = ""
            while time.monotonic() < deadline and "Name?" not in output:
                output += session.drain()
                time.sleep(0.05)
            session.write("alice\r\n")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                output += session.drain()
                if "alice" in output:
                    break
                time.sleep(0.05)
            self.assertIn("alice", output)
        finally:
            session.close()

    def test_unavailable_start_is_explicit(self) -> None:
        session = PTYSession()
        if PTYSession.available():
            self.skipTest("ConPTY is available")
        with self.assertRaises(PTYUnavailable):
            session.start("echo x", Path.cwd(), os.environ.copy())


if __name__ == "__main__":
    unittest.main()
