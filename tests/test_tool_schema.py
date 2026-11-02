from __future__ import annotations

import unittest

from scidev_tool_schema import tool_definitions


class ToolSchemaTests(unittest.TestCase):
    def test_builtin_schemas_are_complete_and_conditional_tools_are_opt_in(self) -> None:
        definitions = tool_definitions()
        names = [item["function"]["name"] for item in definitions]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("apply_patch", names)
        self.assertIn("run_command", names)
        self.assertNotIn("browser_open", names)
        self.assertNotIn("inspect_visual_artifact", names)
        browser = tool_definitions(include_browser=True)
        self.assertIn("browser_open", [item["function"]["name"] for item in browser])
        visual = tool_definitions(include_visual_review=True)
        self.assertIn("inspect_visual_artifact", [item["function"]["name"] for item in visual])
        for item in definitions:
            self.assertEqual(item["type"], "function")
            parameters = item["function"]["parameters"]
            self.assertEqual(parameters["type"], "object")
            self.assertIsInstance(parameters["properties"], dict)


if __name__ == "__main__":
    unittest.main()
