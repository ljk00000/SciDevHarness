from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scidev_core import SvgArtifactAdapter


SAFE_SVG_RESPONSE = """Here is the illustration; save it as `pelican_bicycle.svg`.

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 200">
  <title>Pelican on a bicycle</title><circle cx="90" cy="150" r="30"/>
</svg>
```
"""


class SvgArtifactAdapterTests(unittest.TestCase):
    def test_explicit_svg_creation_turns_a_fenced_answer_into_a_safe_write_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            call = SvgArtifactAdapter.create_tool_call(
                "Generate an SVG of a pelican riding a bicycle",
                SAFE_SVG_RESPONSE,
                Path(temporary),
            )

        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual(call["function"]["name"], "write_file")
        self.assertEqual(
            json.loads(call["function"]["arguments"]),
            {"path": "pelican_bicycle.svg", "content": SAFE_SVG_RESPONSE.split("```xml\n", 1)[1].split("\n```", 1)[0]},
        )

    def test_non_creation_request_and_multiple_svg_blocks_are_not_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(SvgArtifactAdapter.create_tool_call("Explain what SVG is", SAFE_SVG_RESPONSE, root))
            two_blocks = SAFE_SVG_RESPONSE + "\n```svg\n<svg/>\n```"
            self.assertIsNone(
                SvgArtifactAdapter.create_tool_call("Generate an SVG", two_blocks, root)
            )

    def test_active_or_external_svg_content_is_rejected(self) -> None:
        dangerous_sources = (
            '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/a.png"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><image href="local.png"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><rect onload="alert(1)"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(https://example.com/a.svg#x)"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(https://example.com/a.css)</style></svg>',
            '<!DOCTYPE svg [<!ENTITY x "bad">]><svg xmlns="http://www.w3.org/2000/svg"/>',
        )
        with tempfile.TemporaryDirectory() as temporary:
            for source in dangerous_sources:
                with self.subTest(source=source[:50]):
                    response = f"```svg\n{source}\n```"
                    self.assertIsNone(
                        SvgArtifactAdapter.create_tool_call("Generate an SVG", response, Path(temporary))
                    )

    def test_existing_target_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "pelican_bicycle.svg"
            existing.write_text("keep", encoding="utf-8")
            call = SvgArtifactAdapter.create_tool_call(
                "Generate an SVG", SAFE_SVG_RESPONSE, root
            )

        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual(json.loads(call["function"]["arguments"])["path"], "pelican_bicycle-1.svg")


if __name__ == "__main__":
    unittest.main()
