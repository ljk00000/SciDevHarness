from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QImage

from scidev_core import CodingToolbox, EventLedger
from scidev_visual import VisualArtifactReviewer, VisualReviewError


SIMPLE_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="140" viewBox="0 0 240 140">
<title>private-source-title</title><rect width="240" height="140" fill="#ffffff"/>
<circle cx="70" cy="70" r="42" fill="#e9785b"/><path d="M125 105 L180 30" stroke="#18324b" stroke-width="10"/>
</svg>'''


class FakeVisionProvider:
    model = "fake-vision"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, max_tokens=12000, request_id="", reasoning_effort=None):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
                "request_id": request_id,
                "reasoning_effort": reasoning_effort,
            }
        )
        return {"role": "assistant", "content": '{"visible_scene":"circle and line","confidence":"high"}'}


class VisualArtifactTests(unittest.TestCase):
    def test_normalized_review_stays_valid_json_under_extreme_model_output(self) -> None:
        long_text = '"\\' * 800
        review = {
            "visible_scene": long_text,
            "recognizable_entities": [
                {"category": f"{index}{long_text}", "clarity": long_text, "visual_evidence": long_text}
                for index in range(5)
            ],
            "layout_or_clipping": long_text,
            "visible_connections_or_contact": long_text,
            "actionable_defects": [f"{index}{long_text}" for index in range(5)],
            "confidence": "high",
        }

        encoded = VisualArtifactReviewer._normalize_critique(json.dumps(review))

        self.assertLessEqual(len(encoded), VisualArtifactReviewer.MAX_REVIEW_CHARS)
        self.assertEqual(json.loads(encoded)["confidence"], "high")

    def test_review_is_target_blind_and_sends_a_bounded_rendered_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "shape.svg").write_bytes(SIMPLE_SVG)
            provider = FakeVisionProvider()
            reviewer = VisualArtifactReviewer(root, provider)

            result = reviewer.inspect("shape.svg")

            self.assertIn("未收到原始任务", result)
            self.assertIn("circle and line", result)
            self.assertEqual(len(provider.calls), 1)
            request = provider.calls[0]
            self.assertEqual(request["max_tokens"], 12000)
            self.assertIsNone(request["tools"])
            parts = request["messages"][0]["content"]
            self.assertEqual([part["type"] for part in parts], ["text", "image_url"])
            self.assertNotIn("shape.svg", parts[0]["text"])
            self.assertNotIn("private-source-title", parts[0]["text"])
            png_data = base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1])
            rendered = QImage.fromData(QByteArray(png_data), "PNG")
            self.assertFalse(rendered.isNull())
            self.assertLessEqual(max(rendered.width(), rendered.height()), reviewer.MAX_RENDER_DIMENSION)

    def test_identical_content_reuses_the_in_memory_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "shape.svg").write_bytes(SIMPLE_SVG)
            provider = FakeVisionProvider()
            reviewer = VisualArtifactReviewer(root, provider)

            first = reviewer.inspect("shape.svg")
            second = reviewer.inspect("shape.svg")

            self.assertEqual(first, second)
            self.assertEqual(len(provider.calls), 1)

    def test_configured_reasoning_effort_is_forwarded_to_the_vision_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "shape.svg").write_bytes(SIMPLE_SVG)
            provider = FakeVisionProvider()

            VisualArtifactReviewer(root, provider, reasoning_effort="none").inspect("shape.svg")

            self.assertEqual(provider.calls[0]["reasoning_effort"], "none")

    def test_external_svg_resources_are_rejected_without_a_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "external.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.invalid/x.png"/></svg>',
                encoding="utf-8",
            )
            provider = FakeVisionProvider()
            reviewer = VisualArtifactReviewer(root, provider)

            with self.assertRaisesRegex(VisualReviewError, "external SVG resources"):
                reviewer.inspect("external.svg")
            self.assertEqual(provider.calls, [])

    def test_path_escape_and_unsupported_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "workspace"
            root.mkdir()
            (base / "outside.svg").write_bytes(SIMPLE_SVG)
            (root / "notes.txt").write_text("not an image", encoding="utf-8")
            reviewer = VisualArtifactReviewer(root, FakeVisionProvider())

            with self.assertRaisesRegex(VisualReviewError, "inside the workspace"):
                reviewer.inspect(base / "outside.svg")
            with self.assertRaisesRegex(VisualReviewError, "supported visual artifacts"):
                reviewer.inspect("notes.txt")

    def test_svg_write_returns_advisory_review_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = EventLedger(root)
            provider = FakeVisionProvider()
            toolbox = CodingToolbox(
                root,
                ledger,
                visual_reviewer=VisualArtifactReviewer(root, provider),
            )

            result = toolbox.write_file("shape.svg", SIMPLE_SVG.decode("utf-8"))

            self.assertIn("SVG 结构预检", result)
            self.assertIn("独立视觉盲审", result)
            events = [
                json.loads(line)
                for line in (root / ".research" / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(event["event_type"] == "visual_review_completed" for event in events))

    def test_disabled_visual_review_keeps_svg_tool_result_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            toolbox = CodingToolbox(root, EventLedger(root))

            result = toolbox.write_file("shape.svg", SIMPLE_SVG.decode("utf-8"))

            self.assertIn("SVG 结构预检", result)
            self.assertNotIn("视觉盲审", result)
            self.assertNotIn("inspect_visual_artifact", {item["function"]["name"] for item in toolbox.definitions()})
            self.assertIn(
                "inspect_visual_artifact",
                {item["function"]["name"] for item in toolbox.definitions(include_visual_review=True)},
            )


if __name__ == "__main__":
    unittest.main()
