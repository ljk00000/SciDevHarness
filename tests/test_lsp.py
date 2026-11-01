from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scidev_lsp import (
    LSPDiagnostic,
    LSPUnavailable,
    ResidentLSP,
    collect_diagnostics,
    document_uri,
    language_spec,
)


class ResidentLSPTests(unittest.TestCase):
    def test_reuses_one_client_and_sends_did_change(self) -> None:
        class FakeClient:
            instances = []

            def __init__(self, root):
                self.root = root
                self.opened = []
                self.changed = []
                self.closed = False
                FakeClient.instances.append(self)

            def start(self):
                pass

            def initialize(self):
                pass

            def did_open(self, path, text, language_id="python"):
                self.opened.append((str(path), text))
                return document_uri(path)

            def did_change(self, path, text, version):
                self.changed.append((str(path), text, version))
                return document_uri(path)

            def wait_diagnostics(self, uri, timeout=6.0):
                return [LSPDiagnostic(1, 1, 1, 2, "error", "fake")]

            def close(self):
                self.closed = True

        events: list[tuple[str, int, int]] = []
        first_done = threading.Event()
        ready = threading.Event()

        def callback(path, generation, diagnostics, error):
            events.append((str(path), generation, len(diagnostics)))
            if generation == 1:
                first_done.set()
            if len(events) >= 2:
                ready.set()

        with tempfile.TemporaryDirectory(prefix="scidev-resident-lsp-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            path = root / "app.py"
            path.write_text("x = 1\n", encoding="utf-8")
            resident = ResidentLSP(root, callback, client_factory=FakeClient)
            try:
                resident.start()
                resident.submit(path, "x = 1\n", 1)
                self.assertTrue(first_done.wait(3.0), events)
                resident.submit(path, "x = 2\n", 2)
                self.assertTrue(ready.wait(3.0), events)
            finally:
                resident.close()
            self.assertEqual(len(FakeClient.instances), 1)
            client = FakeClient.instances[0]
            self.assertGreaterEqual(len(client.opened), 1)
            self.assertGreaterEqual(len(client.changed), 1)
            self.assertTrue(client.closed)


    def test_python_and_typescript_documents_use_separate_clients(self) -> None:
        class FakeClient:
            instances = []

            def __init__(self, root):
                self.root = root
                self.languages = []
                self.closed = False
                FakeClient.instances.append(self)

            def start(self):
                pass

            def initialize(self):
                pass

            def did_open(self, path, text, language_id=None):
                self.languages.append(language_id)
                return document_uri(path)

            def did_change(self, path, text, version):
                return document_uri(path)

            def wait_diagnostics(self, uri, timeout=6.0):
                return []

            def close(self):
                self.closed = True

        done = threading.Event()
        seen: list[str] = []

        def callback(path, generation, diagnostics, error):
            seen.append(str(path))
            if len(seen) >= 2:
                done.set()

        with tempfile.TemporaryDirectory(prefix="scidev-multi-lsp-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            py = root / "app.py"
            ts = root / "app.ts"
            py.write_text("x = 1\n", encoding="utf-8")
            ts.write_text("const x = 1\n", encoding="utf-8")
            resident = ResidentLSP(root, callback, client_factory=FakeClient)
            try:
                resident.start()
                resident.submit(py, "x = 1\n", 1)
                resident.submit(ts, "const x = 1\n", 2)
                self.assertTrue(done.wait(3.0), seen)
            finally:
                resident.close()
            self.assertEqual(len(FakeClient.instances), 2)
            languages = [language for client in FakeClient.instances for language in client.languages]
            self.assertEqual(sorted(languages), ["python", "typescript"])
            self.assertTrue(all(client.closed for client in FakeClient.instances))


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

    def test_typescript_spec_uses_an_explicit_command_and_language_id(self) -> None:
        with patch.dict(os.environ, {"SCIDEV_TYPESCRIPT_LSP": "custom-ls --stdio"}):
            spec = language_spec(Path("app.ts"))
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.source, "typescript")
        self.assertEqual(spec.language_id, "typescript")
        self.assertEqual(spec.command, ("custom-ls", "--stdio"))
        self.assertIn(".tsx", spec.suffixes)
        self.assertIsNone(language_spec(Path("notes.txt")))

    def test_document_uri_is_absolute_file_uri(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            path = Path(temp) / "a.py"
            self.assertTrue(document_uri(path).startswith("file:"))


if __name__ == "__main__":
    unittest.main()
