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

    def test_xml_fenced_declared_write_file_envelope_is_recovered_safely(self) -> None:
        source = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 200"><circle cx="90" cy="150" r="30"/></svg>'
        envelope = json.dumps(
            {"name": "write_file", "arguments": {"path": "pelican_bicycle.svg", "content": source}}
        )
        response = f"```xml\n{envelope}\n```"
        with tempfile.TemporaryDirectory() as temporary:
            call = SvgArtifactAdapter.create_tool_call(
                "Generate an SVG of a pelican riding a bicycle", response, Path(temporary)
            )

        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual(
            json.loads(call["function"]["arguments"]),
            {"path": "pelican_bicycle.svg", "content": source},
        )

    def test_xml_fenced_tool_envelopes_reject_unknown_tools_and_unsafe_paths(self) -> None:
        source = '<svg xmlns="http://www.w3.org/2000/svg"><circle cx="10" cy="10" r="5"/></svg>'
        envelopes = (
            {"name": "run_command", "arguments": {"path": "art.svg", "content": source}},
            {"name": "write_file", "arguments": {"path": "../art.svg", "content": source}},
            {"name": "write_file", "arguments": {"path": "art.svg", "content": source, "mode": "append"}},
        )
        with tempfile.TemporaryDirectory() as temporary:
            for envelope in envelopes:
                response = f"```xml\n{json.dumps(envelope)}\n```"
                with self.subTest(envelope=envelope):
                    self.assertIsNone(
                        SvgArtifactAdapter.create_tool_call("Generate an SVG", response, Path(temporary))
                    )

    def test_complete_svg_is_salvaged_from_a_truncated_write_file_envelope(self) -> None:
        source = '<svg xmlns="http://www.w3.org/2000/svg"><circle cx="10" cy="10" r="5"/></svg>'
        response = (
            '```xml\n{"name":"write_file","arguments":{"path":"art.svg",'
            f'"content":"{source}\n```'
        )
        with tempfile.TemporaryDirectory() as temporary:
            call = SvgArtifactAdapter.create_tool_call("Generate an SVG", response, Path(temporary))

        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "art.svg", "content": source})

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

    def test_explicit_repair_of_an_existing_svg_updates_that_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pelican_bicycle.svg").write_text("old", encoding="utf-8")
            call = SvgArtifactAdapter.create_tool_call(
                "Create a corrected version of the existing pelican_bicycle.svg",
                SAFE_SVG_RESPONSE,
                root,
            )

        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual(json.loads(call["function"]["arguments"])["path"], "pelican_bicycle.svg")

    def test_localized_svg_repair_cannot_be_promoted_to_a_full_file_write(self) -> None:
        prompt = (
            "Repair only these localized SVG issues in pelican_bicycle.svg. "
            "Call replace_in_file with exact old_string and new_string arguments."
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pelican_bicycle.svg").write_text("known-good source", encoding="utf-8")
            call = SvgArtifactAdapter.create_tool_call(prompt, SAFE_SVG_RESPONSE, root)

        self.assertIsNone(call)

    def test_numbered_tool_response_svg_is_recovered_for_explicit_repair(self) -> None:
        source = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 200">\n'
            "  <title>Pelican on a bicycle</title>\n"
            '  <circle cx="90" cy="150" r="30"/>\n'
            "</svg>"
        )
        wrapped = "<tool_response>\n" + "\n".join(
            f"{line_number}: {line}" for line_number, line in enumerate(source.splitlines(), start=1)
        ) + "\n</tool_response>"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pelican_bicycle.svg").write_text("old", encoding="utf-8")
            call = SvgArtifactAdapter.create_tool_call(
                "Create a corrected version of the existing pelican_bicycle.svg",
                wrapped,
                root,
            )

        self.assertIsNotNone(call)
        assert call is not None
        self.assertEqual(json.loads(call["function"]["arguments"]), {
            "path": "pelican_bicycle.svg",
            "content": source,
        })

    def test_raw_svg_recovery_rejects_surrounding_prose_and_multiple_roots(self) -> None:
        raw_svg = '<svg xmlns="http://www.w3.org/2000/svg"><title>pelican</title></svg>'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for response in (f"Here is the SVG:\n{raw_svg}", raw_svg + raw_svg):
                with self.subTest(response=response[:32]):
                    self.assertIsNone(
                        SvgArtifactAdapter.create_tool_call("Generate an SVG", response, root)
                    )


if __name__ == "__main__":
    unittest.main()
