from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from scidev_mcp import MCPTool
from scidev_mcp_ui import MCPBrowserDialog


class _FakeManager:
    errors: list[str] = []

    def __init__(self):
        self.closed = False

    def start(self):
        return [MCPTool("demo", "echo", "Echo text")]

    def list_resources(self):
        return [{"server": "demo", "uri": "file:///notes.txt", "name": "notes"}]

    def list_prompts(self):
        return [{"server": "demo", "name": "greet", "description": "greeting", "arguments": []}]

    def read_resource(self, server, uri):
        return f"resource:{server}:{uri}"

    def get_prompt(self, server, name, arguments=None):
        return f"prompt:{server}:{name}"

    def close(self):
        self.closed = True


class MCPBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_lists_reads_and_inserts_a_resource(self) -> None:
        manager = _FakeManager()
        dialog = MCPBrowserDialog(manager)
        try:
            self.assertEqual(dialog.tree.topLevelItemCount(), 3)
            resources = dialog.tree.topLevelItem(1)
            self.assertEqual(resources.childCount(), 1)
            dialog.tree.setCurrentItem(resources.child(0))
            self.assertTrue(dialog.read_selected())
            self.assertEqual(
                dialog.preview.toPlainText(),
                "resource:demo:file:///notes.txt",
            )
            inserted: list[str] = []
            dialog.insert_requested.connect(inserted.append)
            self.assertTrue(dialog.insert_preview())
            self.assertEqual(inserted, ["resource:demo:file:///notes.txt"])
        finally:
            dialog.close()

    def test_renders_a_prompt_without_arguments(self) -> None:
        dialog = MCPBrowserDialog(_FakeManager())
        try:
            prompts = dialog.tree.topLevelItem(2)
            self.assertEqual(prompts.childCount(), 1)
            dialog.tree.setCurrentItem(prompts.child(0))
            self.assertTrue(dialog.read_selected())
            self.assertEqual(dialog.preview.toPlainText(), "prompt:demo:greet")
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
