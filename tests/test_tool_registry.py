from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from scidev_core import CodingToolbox, EventLedger
from scidev_tool_registry import READ_ONLY_TOOL_NAMES, ToolRegistry
from scidev_tool_schema import tool_definitions


class ToolRegistryTests(unittest.TestCase):
    def test_registry_covers_builtin_handlers_and_read_only_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scidev-registry-", ignore_cleanup_errors=True) as temp:
            root = Path(temp)
            toolbox = CodingToolbox(root, EventLedger(root))
            registry = toolbox.tool_registry
            self.assertIsInstance(registry, ToolRegistry)
            schema_names = {item["function"]["name"] for item in tool_definitions()}
            self.assertTrue(schema_names.issubset(set(registry.names())))
            self.assertEqual(set(registry.read_only_names()), set(READ_ONLY_TOOL_NAMES))
            read_binding = registry.get("read_file")
            self.assertIsNotNone(read_binding)
            assert read_binding is not None
            self.assertTrue(callable(read_binding.handler))
            self.assertIsNone(registry.get("missing_tool"))
            self.assertNotIn("write_file", registry.read_only_names())
            self.assertNotIn("run_command", registry.read_only_names())


if __name__ == "__main__":
    unittest.main()
