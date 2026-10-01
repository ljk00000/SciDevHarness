from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.smoke_pelican_svg import PELICAN_PROMPT, validate_and_render_svg


VALID_PELICAN_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200">
  <title>Pelican riding a bicycle</title>
  <circle id="left-wheel" cx="90" cy="150" r="30" fill="none" stroke="#27374a" stroke-width="5"/>
  <circle id="right-wheel" cx="230" cy="150" r="30" fill="none" stroke="#27374a" stroke-width="5"/>
  <polyline id="bicycle-frame" points="90,150 135,95 180,150 90,150 230,150 180,150 195,105 230,150" fill="none" stroke="#27374a" stroke-width="5"/>
  <path id="pelican-body" d="M145 83 Q166 45 200 65 Q186 110 155 107Z" fill="#f4a261"/>
  <circle id="pelican-head" cx="195" cy="62" r="12" fill="#f4a261"/>
  <circle id="pelican-eye" cx="199" cy="60" r="3" fill="#27374a"/>
  <path id="pelican-wing" d="M154 77 Q174 58 190 78 Q175 90 154 77Z" fill="#e9c46a"/>
  <polygon id="pelican-beak" points="198,66 240,76 198,80" fill="#e76f51"/>
  <path id="pelican-pouch" d="M198 79 Q207 103 188 104" fill="none" stroke="#e76f51" stroke-width="4"/>
</svg>'''


class PelicanSvgTests(unittest.TestCase):
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

    def test_short_pelican_beak_is_rejected(self) -> None:
        short_beak = VALID_PELICAN_SVG.replace(
            b'points="198,66 240,76 198,80"',
            b'points="198,66 210,70 198,80"',
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "too short to read as a pelican bill"):
                validate_and_render_svg(short_beak, Path(temporary) / "preview.png")


if __name__ == "__main__":
    unittest.main()
