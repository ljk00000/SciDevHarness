from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scidev_lsp import LSPUnavailable, collect_diagnostics, document_uri


class LSPDiagnosticsTests(unittest.TestCase):
    def test_collects_ruff_syntax_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            (root / "ruff.toml").write_text("respect-gitignore = false\n", encoding="utf-8")
            path = root / "bad.py"
            text = "def broken(:\n    pass\n"
            path.write_text(text, encoding="utf-8")
            try:
                diagnostics = collect_diagnostics(root, path, text, timeout=4)
            except LSPUnavailable as exc:
                self.skipTest(f"Ruff LSP unavailable: {exc}")
            self.assertGreaterEqual(len(diagnostics), 1)
            self.assertEqual(diagnostics[0].severity, "error")
            self.assertEqual(diagnostics[0].line, 1)
            self.assertTrue(diagnostics[0].message)

    def test_clean_file_has_no_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            path = root / "clean.py"
            text = "def ok():\n    return 1\n"
            path.write_text(text, encoding="utf-8")
            try:
                diagnostics = collect_diagnostics(root, path, text, timeout=4)
            except LSPUnavailable as exc:
                self.skipTest(f"Ruff LSP unavailable: {exc}")
            self.assertEqual(diagnostics, [])

    def test_unsupported_suffix_is_rejected_without_starting_a_server(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            path = root / "notes.txt"
            with self.assertRaises(LSPUnavailable):
                collect_diagnostics(root, path, "text", timeout=1)

    def test_document_uri_is_absolute_file_uri(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            path = Path(temp) / "a.py"
            self.assertTrue(document_uri(path).startswith("file:"))


if __name__ == "__main__":
    unittest.main()
