from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.smoke_pelican_svg import (
    PELICAN_PROMPT,
    latest_svg_write_path,
    pelican_repair_prompt,
    persist_failure_diagnostic,
    record_smoke_event,
    smoke_session_id,
    validate_and_render_svg,
)


VALID_PELICAN_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200">
  <title>Pelican riding a bicycle</title>
  <circle id="left-wheel" cx="90" cy="150" r="30" fill="none" stroke="#27374a" stroke-width="5"/>
  <circle id="right-wheel" cx="230" cy="150" r="30" fill="none" stroke="#27374a" stroke-width="5"/>
  <polyline id="bicycle-frame" points="90,150 135,95 195,100 180,150 90,150 135,95" fill="none" stroke="#27374a" stroke-width="5"/>
  <path id="bicycle-fork" d="M195 100 L230 150" fill="none" stroke="#27374a" stroke-width="5"/>
  <g id="bicycle-spokes" stroke="#91a7b5" stroke-width="1.5"><path d="M90 120 V180 M60 150 H120 M68 128 L112 172 M112 128 L68 172"/><path d="M230 120 V180 M200 150 H260 M208 128 L252 172 M252 128 L208 172"/></g>
  <path id="bicycle-saddle" d="M128 96 Q135 90 142 96" fill="none" stroke="#27374a" stroke-width="4"/>
  <path id="bicycle-handlebar" d="M215 100 Q220 88 228 94" fill="none" stroke="#27374a" stroke-width="4"/>
  <path id="pelican-wing-reaching" d="M190 80 C200 80 215 88 220 94 Q215 96 210 94 Q200 87 188 88Z" fill="#e9c46a" stroke="#27374a" stroke-width="2"/>
  <path id="bicycle-pedals" d="M172 150 H188 M180 145 V155" fill="none" stroke="#27374a" stroke-width="4"/>
  <path id="pelican-body" d="M145 83 Q166 45 200 65 Q186 110 155 107Z" fill="#f4a261"/>
  <circle id="pelican-head" cx="195" cy="62" r="12" fill="#f4a261"/>
  <circle id="pelican-eye" cx="199" cy="60" r="3" fill="#27374a"/>
  <path id="pelican-wing" d="M154 77 Q174 58 190 78 Q175 90 154 77Z" fill="#e9c46a"/>
  <polygon id="pelican-beak" points="198,66 270,76 198,80" fill="#e76f51"/>
  <path id="pelican-pouch" d="M198 79 Q207 103 188 104 Q202 100 198 79Z" fill="#e76f51" stroke="#e76f51" stroke-width="2"/>
  <path id="pelican-leg-near" d="M155 100 Q145 95 138 94" fill="none" stroke="#27374a" stroke-width="4"/>
  <path id="pelican-leg-far" d="M175 102 Q180 125 180 150" fill="none" stroke="#27374a" stroke-width="4"/>
</svg>'''


class PelicanSvgTests(unittest.TestCase):
    def test_each_visual_repair_uses_a_fresh_completed_session_id(self) -> None:
        session_ids = [smoke_session_id(attempt) for attempt in range(3)]
        self.assertEqual(len(session_ids), len(set(session_ids)))
        self.assertEqual(session_ids[0], "live_pelican_svg_smoke")
        self.assertEqual(session_ids[1:], ["live_pelican_svg_smoke_repair_1", "live_pelican_svg_smoke_repair_2"])

    def test_latest_svg_output_wins_when_repair_creates_a_new_filename(self) -> None:
        calls = [
            {"arguments": {"path": "pelican_bicycle.svg"}},
            {"arguments": {"path": "notes.md"}},
            {"arguments": {"path": "pelican_bicycle-1.svg"}},
        ]
        self.assertEqual(latest_svg_write_path(calls), "pelican_bicycle-1.svg")
        self.assertIsNone(latest_svg_write_path([{ "arguments": {"path": "notes.md"} }]))

    def test_repair_prompt_gives_a_concrete_unique_shape_blueprint(self) -> None:
        prompt = pelican_repair_prompt("pelican_bicycle.svg", "missing a visible pelican eye")
        self.assertIn(PELICAN_PROMPT, prompt)
        self.assertIn('viewBox="0 0 640 420"', prompt)
        self.assertIn('125,326 270,245 420,250 335,285', prompt)
        self.assertIn('<circle id="left-wheel" cx="125" cy="326" r="72"', prompt)
        self.assertIn("bicycle-fork", prompt)
        self.assertIn("pelican-wing-reaching", prompt)
        self.assertIn('d="M310 180 C350 180 390 210 419 230', prompt)
        self.assertIn("left-wheel, right-wheel, bicycle-frame", prompt)
        self.assertIn("must each occur exactly once", prompt)
        self.assertIn('stroke="#18324B"', prompt)
        self.assertIn('id="pelican-leg-near"', prompt)

    def test_stream_deltas_are_coalesced_without_hiding_tool_events(self) -> None:
        events: list[tuple[str, dict]] = []
        record_smoke_event(events, "assistant_delta", {"session_id": "s1", "turn": 2, "text": "partial "})
        record_smoke_event(events, "assistant_delta", {"session_id": "s1", "turn": 2, "text": "tail"})
        record_smoke_event(
            events,
            "tool_started",
            {"session_id": "s1", "name": "write_file", "arguments": {"path": "art.svg", "content": "<svg/>"}},
        )

        with tempfile.TemporaryDirectory() as temporary:
            path = persist_failure_diagnostic(
                Path(temporary),
                model="local-model",
                attempt=1,
                session_id="s1",
                reason="visual repair failed",
                events=events,
                request_count=3,
            )
            report = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(report["recent_events"]), 2)
        self.assertEqual(report["recent_events"][0]["text_characters"], len("partial tail"))
        self.assertEqual(report["recent_events"][0]["text_preview"], "partial tail")
        self.assertEqual(report["recent_events"][1]["name"], "write_file")

    def test_failure_diagnostic_is_bounded_and_omits_credential_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = persist_failure_diagnostic(
                Path(temporary),
                model="local-model",
                attempt=1,
                session_id="repair-1",
                reason="missing eye",
                events=[
                    (
                        "tool_started",
                        {
                            "session_id": "repair-1",
                            "name": "write_file",
                            "arguments": {
                                "path": "pelican.svg",
                                "content": "<svg/>" * 1000,
                                "api_key": "must-not-be-written",
                            },
                        },
                    ),
                    ("assistant", {"session_id": "repair-1", "text": "fixing the eye"}),
                ],
                request_count=3,
            )
            report = json.loads(path.read_text(encoding="utf-8"))
            raw = path.read_text(encoding="utf-8")

        self.assertEqual(report["failure_reason"], "missing eye")
        self.assertEqual(report["local_request_count"], 3)
        self.assertEqual(report["recent_events"][0]["arguments"]["content_characters"], len("<svg/>" * 1000))
        self.assertTrue(report["credential_fields_omitted"])
        self.assertNotIn("must-not-be-written", raw)

    def test_canonical_task_prompt_is_exact_and_svg_renders_to_visible_png(self) -> None:
        self.assertEqual(PELICAN_PROMPT, "Generate an SVG of a pelican riding a bicycle")
        with tempfile.TemporaryDirectory(prefix="scidev-svg-render-test-") as temporary:
            preview = Path(temporary) / "preview.png"
            result = validate_and_render_svg(VALID_PELICAN_SVG, preview)

            self.assertTrue(preview.is_file())
            self.assertGreater(preview.stat().st_size, 1000)
            self.assertGreater(result["non_background_samples"], 25)
            self.assertEqual(result["intrinsic_width"], 320)
            self.assertEqual(result["element_counts"]["circle"], 4)

    def test_visible_illustration_does_not_depend_on_title_or_description_labels(self) -> None:
        unlabeled = VALID_PELICAN_SVG.replace(b"  <title>Pelican riding a bicycle</title>\n", b"")
        with tempfile.TemporaryDirectory() as temporary:
            result = validate_and_render_svg(unlabeled, Path(temporary) / "preview.png")
        self.assertGreater(result["non_background_samples"], 25)

    def test_invalid_or_active_svg_content_is_rejected(self) -> None:
        invalid_documents = (
            b"<svg><path></svg>",
            b'<!DOCTYPE svg [<!ENTITY x "boom">]><svg xmlns="http://www.w3.org/2000/svg"/>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/a.png"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><image href="local.png"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10" onload="alert(1)"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(https://example.com/a.svg#x)"/></svg>',
        )
        for document in invalid_documents:
            with self.subTest(document=document[:48]), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(RuntimeError):
                    validate_and_render_svg(document, Path(temporary) / "preview.png")

    def test_blank_svg_is_rejected_as_a_failed_visual_output(self) -> None:
        blank = b'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200"/>'
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "no visible pixels"):
                validate_and_render_svg(blank, Path(temporary) / "preview.png")

    def test_abstract_but_visible_svg_is_not_mistaken_for_the_requested_illustration(self) -> None:
        abstract = b'''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200">
          <title>Pelican riding a bicycle</title><circle cx="50" cy="50" r="40"/>
        </svg>'''
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "lacks separately identified visual parts"):
                validate_and_render_svg(abstract, Path(temporary) / "preview.png")

    def test_overlapping_bicycle_wheels_are_rejected(self) -> None:
        overlapping = VALID_PELICAN_SVG.replace(b'cx="230" cy="150"', b'cx="120" cy="150"')
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "wheels overlap"):
                validate_and_render_svg(overlapping, Path(temporary) / "preview.png")

    def test_wheel_shape_and_missing_saddle_are_reported_together_for_repair(self) -> None:
        malformed = VALID_PELICAN_SVG.replace(
            b'<circle id="left-wheel" cx="90" cy="150" r="30" fill="none" stroke="#27374a" stroke-width="5"/>',
            b'<path id="left-wheel" d="M60 150H120" fill="none" stroke="#27374a"/>',
        ).replace(b'  <path id="bicycle-saddle" d="M128 96 Q135 90 142 96" fill="none" stroke="#27374a" stroke-width="4"/>\n', b"")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, r"missing=.*bicycle-saddle.*malformed=.*left-wheel must be a circle"):
                validate_and_render_svg(malformed, Path(temporary) / "preview.png")

    def test_short_pelican_beak_is_rejected(self) -> None:
        short_beak = VALID_PELICAN_SVG.replace(
            b'points="198,66 270,76 198,80"',
            b'points="198,66 250,70 198,80"',
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "too short relative to the head"):
                validate_and_render_svg(short_beak, Path(temporary) / "preview.png")

    def test_invalid_named_svg_paint_colors_are_rejected(self) -> None:
        invalid_paint = VALID_PELICAN_SVG.replace(b'fill="#f4a261"', b'fill="golden-tan"', 1)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "invalid paint colors"):
                validate_and_render_svg(invalid_paint, Path(temporary) / "preview.png")

    def test_empty_head_ring_is_not_accepted_as_a_filled_bird_head(self) -> None:
        empty_head = VALID_PELICAN_SVG.replace(
            b'<circle id="pelican-head" cx="195" cy="62" r="12" fill="#f4a261"/>',
            b'<circle id="pelican-head" cx="195" cy="62" r="12" fill="none" stroke="#27374a"/>',
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "filled silhouette"):
                validate_and_render_svg(empty_head, Path(temporary) / "preview.png")

    def test_flat_rectangular_body_is_rejected_by_rendered_geometry(self) -> None:
        flat_body = VALID_PELICAN_SVG.replace(
            b'd="M145 83 Q166 45 200 65 Q186 110 155 107Z"',
            b'd="M145 83 Q166 80 200 83 Q186 86 155 86Z"',
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "too flat"):
                validate_and_render_svg(flat_body, Path(temporary) / "preview.png")

    def test_floating_rider_leg_is_rejected_when_it_misses_the_saddle(self) -> None:
        floating_leg = VALID_PELICAN_SVG.replace(
            b'd="M155 100 Q145 95 138 94"',
            b'd="M40 20 Q50 30 60 40"',
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "connect the body to the saddle"):
                validate_and_render_svg(floating_leg, Path(temporary) / "preview.png")

    def test_reaching_wing_must_be_closed_filled_and_touch_the_handlebar(self) -> None:
        detached = VALID_PELICAN_SVG.replace(
            b'd="M190 80 C200 80 215 88 220 94 Q215 96 210 94 Q200 87 188 88Z" fill="#e9c46a"',
            b'd="M190 80 C205 100 215 130 225 150" fill="#e9c46a"',
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "closed filled curve.*reaches the handlebar"):
                validate_and_render_svg(detached, Path(temporary) / "preview.png")


if __name__ == "__main__":
    unittest.main()
