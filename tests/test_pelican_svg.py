from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.smoke_pelican_svg import (
    PELICAN_PROMPT,
    _TimedResponse,
    collect_svg_mutations,
    collect_shell_requests,
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
    def test_stream_timing_covers_sse_body_and_first_event(self) -> None:
        class FakeResponse:
            status = 200

            def __init__(self) -> None:
                self.lines = iter(
                    (
                        b": keepalive\n",
                        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n',
                        b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n',
                        b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n',
                        b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"write_file","arguments":"{\\"path\\":\\"pelican.svg\\"}"}}]}}]}\n',
                        b"data: [DONE]\n",
                    )
                )
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True
                return None

            def __iter__(self):
                return self

            def __next__(self):
                return next(self.lines)

        clock_values = iter((0.8, 1.0, 1.25, 2.0))
        record: dict = {}
        response = FakeResponse()
        timed = _TimedResponse(
            response,
            record,
            started_at=0.0,
            headers_at=0.1,
            streaming=True,
            clock=lambda: next(clock_values),
        )

        with timed as stream:
            self.assertEqual(
                list(stream),
                [
                    b": keepalive\n",
                    b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n',
                    b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n',
                    b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n',
                    b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"write_file","arguments":"{\\"path\\":\\"pelican.svg\\"}"}}]}}]}\n',
                    b"data: [DONE]\n",
                ],
            )

        self.assertTrue(response.closed)
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["headers_seconds"], 0.1)
        self.assertEqual(record["first_body_byte_seconds"], 0.8)
        self.assertEqual(record["first_event_seconds"], 1.0)
        self.assertEqual(record["first_content_seconds"], 1.25)
        self.assertEqual(record["sse_content_bytes"], len(b"Hi"))
        self.assertEqual(record["sse_reasoning_bytes"], len(b"think"))
        self.assertEqual(
            record["tool_argument_bytes"],
            len(b"write_file") + len(b'{"path":"pelican.svg"}'),
        )
        self.assertEqual(
            record["response_bytes"],
            sum(
                map(
                    len,
                    (
                        b": keepalive\n",
                        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n',
                        b'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}\n',
                        b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n',
                        b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"write_file","arguments":"{\\"path\\":\\"pelican.svg\\"}"}}]}}]}\n',
                        b"data: [DONE]\n",
                    ),
                )
            ),
        )
        self.assertEqual(record["body_seconds"], 1.9)
        self.assertEqual(record["elapsed_seconds"], 2.0)

    def test_non_stream_timing_does_not_label_json_body_as_first_sse_event(self) -> None:
        class FakeResponse:
            status = 200

            def __init__(self) -> None:
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True

            def read(self):
                return b'{"choices": []}'

        clock_values = iter((0.5, 0.8))
        record: dict = {}
        response = FakeResponse()
        timed = _TimedResponse(
            response,
            record,
            started_at=0.0,
            headers_at=0.1,
            streaming=False,
            clock=lambda: next(clock_values),
        )

        with timed as body:
            self.assertEqual(body.read(), b'{"choices": []}')

        self.assertTrue(response.closed)
        self.assertFalse(record["streaming"])
        self.assertEqual(record["first_body_byte_seconds"], 0.5)
        self.assertIsNone(record["first_event_seconds"])
        self.assertIsNone(record["first_content_seconds"])
        self.assertEqual(record["response_bytes"], len(b'{"choices": []}'))
        self.assertEqual(record["body_seconds"], 0.7)
        self.assertEqual(record["elapsed_seconds"], 0.8)

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

    def test_svg_mutation_tracking_counts_write_and_replace_but_not_reads(self) -> None:
        events = [
            ("tool_started", {"name": "write_file", "arguments": {"path": "pelican.svg"}}),
            (
                "tool_started",
                {
                    "name": "write_file",
                    "source": "harness_svg_artifact_recovery",
                    "arguments": {"path": "recovered.svg"},
                },
            ),
            ("tool_started", {"name": "read_file", "arguments": {"path": "pelican.svg"}}),
            ("tool_started", {"name": "replace_in_file", "arguments": {"path": "pelican.svg"}}),
            (
                "tool_started",
                {
                    "name": "replace_in_file",
                    "source": "harness_precommit",
                    "arguments": {"path": "pelican.svg"},
                },
            ),
            ("tool_started", {"name": "replace_in_file", "arguments": {"path": "notes.md"}}),
        ]
        self.assertEqual(
            [item["name"] for item in collect_svg_mutations(events)],
            ["write_file", "write_file", "replace_in_file"],
        )

    def test_shell_request_trace_omits_the_command_text(self) -> None:
        events = [
            (
                "tool_started",
                {
                    "session_id": "repair_1",
                    "name": "run_command",
                    "arguments": {"command": "echo private-value"},
                },
            )
        ]
        self.assertEqual(collect_shell_requests(events), [{"session_id": "repair_1", "source": ""}])

    def test_repair_prompt_requests_a_minimal_focused_edit(self) -> None:
        prompt = pelican_repair_prompt("pelican_bicycle.svg", "missing a visible pelican eye")
        self.assertIn(PELICAN_PROMPT, prompt)
        self.assertIn("Read the current SVG", prompt)
        self.assertIn("smallest exact `replace_in_file`", prompt)
        self.assertNotIn('id="left-wheel"', prompt)
        self.assertLess(len(prompt), 600)

    def test_wheel_repair_prompt_contains_only_wheel_specific_edits(self) -> None:
        prompt = pelican_repair_prompt(
            "pelican_bicycle.svg",
            "missing=['left-wheel', 'right-wheel']; near_matches={'left-wheel': ['bicycle-left-wheel']} ",
        )
        self.assertIn("rear wheel its own unfilled `<circle id=\"left-wheel\">`", prompt)
        self.assertIn("front wheel its own unfilled `<circle id=\"right-wheel\">`", prompt)
        self.assertIn("preserve the existing drawing", prompt)
        self.assertNotIn("pelican-body", prompt)
        self.assertLess(len(prompt), 900)

    def test_four_missing_parts_get_a_targeted_repair_instead_of_a_full_redraw(self) -> None:
        prompt = pelican_repair_prompt(
            "pelican_bicycle.svg",
            "missing=['left-wheel', 'right-wheel', 'pelican-wing', 'pelican-wing-reaching']; "
            "malformed=[]; duplicate_ids=[]",
        )
        self.assertIn("Repair only the missing SVG parts", prompt)
        self.assertIn("rear frame hub", prompt)
        self.assertIn("inside the pelican body silhouette", prompt)
        self.assertIn("to the existing handlebar", prompt)
        self.assertNotIn("complete, polished", prompt)
        self.assertLess(len(prompt), 1300)

    def test_full_repair_prompt_is_selected_for_many_missing_parts(self) -> None:
        prompt = pelican_repair_prompt(
            "pelican_bicycle.svg",
            "SVG preflight failed; SVG viewBox is too small; missing=['left-wheel', 'right-wheel', "
            "'bicycle-frame', 'bicycle-fork', 'pelican-body', 'pelican-head', 'pelican-beak']; malformed=[]",
        )
        self.assertIn("complete, polished pelican riding a bicycle", prompt)
        self.assertIn("viewBox=\"0 0 640 420\"", prompt)
        self.assertIn("pelican-wing-reaching", prompt)
        self.assertIn("Call `write_file` once", prompt)
        self.assertNotIn("preserve every passing element", prompt)
        self.assertLess(len(prompt), 1800)

    def test_repair_prompt_uses_one_rebuild_for_multiple_shape_errors(self) -> None:
        prompt = pelican_repair_prompt(
            "pelican_bicycle.svg",
            "missing=[]; malformed=['bicycle-frame must be a connected polyline', "
            "'pelican-beak must be a polygon', 'pelican-body must be an organic path or ellipse']; "
            "duplicate_ids=['bicycle-spokes']",
        )
        self.assertIn("complete, polished pelican riding a bicycle", prompt)
        self.assertIn("frame is `<polyline>`", prompt)
        self.assertIn("spokes are one `<g>`", prompt)
        self.assertIn("beak is `<polygon>`", prompt)
        self.assertIn("body is a curved `<path>` or `<ellipse>`", prompt)
        self.assertIn("ochre `#e4b35b`", prompt)

    def test_duplicate_id_only_uses_a_targeted_repair_prompt(self) -> None:
        prompt = pelican_repair_prompt(
            "pelican_bicycle.svg",
            "missing=[]; malformed=[]; duplicate_ids=['bicycle-spokes']",
        )
        self.assertIn("Repair duplicate SVG IDs", prompt)
        self.assertIn("one `<g id=\"bicycle-spokes\">`", prompt)
        self.assertIn("smallest exact `replace_in_file`", prompt)
        self.assertNotIn("complete, polished", prompt)

    def test_color_and_wheel_fill_errors_use_a_localized_repair(self) -> None:
        prompt = pelican_repair_prompt(
            "pelican_bicycle.svg",
            "invalid paint colors=['fill=ochre']; missing=[]; "
            "malformed=['invalid paint fill=ochre', 'left-wheel must be an unfilled, visibly outlined tire', "
            "'right-wheel must be an unfilled, visibly outlined tire']; duplicate_ids=[]",
        )
        self.assertIn("Repair only SVG paint/fill errors", prompt)
        self.assertIn('fill="none"', prompt)
        self.assertIn("ochre `#e4b35b`", prompt)
        self.assertIn("Preserve every path, position", prompt)
        self.assertNotIn("complete, polished", prompt)

    def test_missing_wheel_feedback_shows_prefixed_id_near_matches(self) -> None:
        aliased = VALID_PELICAN_SVG.replace(b'id="left-wheel"', b'id="bicycle-left-wheel"').replace(
            b'id="right-wheel"', b'id="bicycle-right-wheel"'
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, r"near_matches=.*bicycle-left-wheel"):
                validate_and_render_svg(aliased, Path(temporary) / "preview.png")

    def test_small_viewbox_and_missing_parts_are_reported_together(self) -> None:
        incomplete = VALID_PELICAN_SVG.replace(b'viewBox="0 0 320 200"', b'viewBox="0 0 100 200"')
        incomplete = incomplete.replace(b'id="left-wheel"', b'id="bicycle-left-wheel"').replace(
            b'id="right-wheel"', b'id="bicycle-right-wheel"'
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, r"viewBox is too small.*missing=.*left-wheel.*near_matches"):
                validate_and_render_svg(incomplete, Path(temporary) / "preview.png")

    def test_duplicate_ids_and_core_shape_errors_are_reported_together(self) -> None:
        malformed = VALID_PELICAN_SVG.replace(
            b'  <path id="bicycle-saddle"',
            b'  <line id="bicycle-spokes" x1="90" y1="150" x2="120" y2="120"/>\n'
            b'  <path id="bicycle-saddle"',
        )
        malformed = malformed.replace(b'<polyline id="bicycle-frame"', b'<path id="bicycle-frame"')
        malformed = malformed.replace(b'<polygon id="pelican-beak"', b'<path id="pelican-beak"')
        malformed = malformed.replace(b'<path id="pelican-body"', b'<rect id="pelican-body"')
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(RuntimeError) as caught:
                validate_and_render_svg(malformed, Path(temporary) / "preview.png")
        message = str(caught.exception)
        self.assertIn("duplicate_ids=['bicycle-spokes']", message)
        for issue in (
            "bicycle-frame must be a connected polyline",
            "pelican-beak must be a polygon",
            "pelican-body must be an organic path or ellipse",
        ):
            self.assertIn(issue, message)

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
                    (
                        "tool_started",
                        {
                            "session_id": "repair-1",
                            "name": "run_command",
                            "arguments": {"command": "echo private-command-must-not-be-written"},
                        },
                    ),
                ],
                request_count=3,
                request_metrics=[
                    {
                        "phase": "repair_1",
                        "streaming": True,
                        "max_tokens": 12_000,
                        "tools": True,
                        "headers_seconds": 0.4,
                        "first_event_seconds": 3.2,
                        "first_content_seconds": 3.3,
                        "elapsed_seconds": 19.5,
                        "request_bytes": 8192,
                        "response_bytes": 2048,
                        "api_key": "must-not-be-written",
                        "prompt": "private prompt must not be written",
                    }
                ],
            )
            report = json.loads(path.read_text(encoding="utf-8"))
            raw = path.read_text(encoding="utf-8")

        self.assertEqual(report["failure_reason"], "missing eye")
        self.assertEqual(report["local_request_count"], 3)
        self.assertEqual(report["recent_events"][0]["arguments"]["content_characters"], len("<svg/>" * 1000))
        self.assertTrue(report["credential_fields_omitted"])
        self.assertNotIn("must-not-be-written", raw)
        self.assertNotIn("private prompt", raw)
        self.assertNotIn("private-command-must-not-be-written", raw)
        self.assertEqual(report["blocked_shell_requests"], [{"session_id": "repair-1", "source": ""}])
        self.assertTrue(report["recent_events"][-1]["arguments"]["command_omitted"])
        self.assertEqual(
            report["request_metrics"],
            [
                {
                    "phase": "repair_1",
                    "streaming": True,
                    "max_tokens": 12_000,
                    "tools": True,
                    "headers_seconds": 0.4,
                    "first_event_seconds": 3.2,
                    "first_content_seconds": 3.3,
                    "elapsed_seconds": 19.5,
                    "request_bytes": 8192,
                    "response_bytes": 2048,
                }
            ],
        )

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

    def test_invalid_paints_and_filled_wheels_are_reported_together(self) -> None:
        invalid = VALID_PELICAN_SVG.replace(b'fill="#f4a261"', b'fill="ochre"', 1)
        invalid = invalid.replace(b'fill="none" stroke="#27374a"', b'fill="teal" stroke="#27374a"')
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(RuntimeError) as caught:
                validate_and_render_svg(invalid, Path(temporary) / "preview.png")
        message = str(caught.exception)
        self.assertIn("invalid paint colors", message)
        self.assertIn("left-wheel must be an unfilled, visibly outlined tire", message)
        self.assertIn("right-wheel must be an unfilled, visibly outlined tire", message)

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
