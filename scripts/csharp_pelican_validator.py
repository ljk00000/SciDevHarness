"""Offline bridge for the original strict SVG acceptance test (stdin SVG, stdout JSON)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.smoke_pelican_svg import MAX_SVG_BYTES, validate_and_render_svg  # noqa: E402
from scripts.pelican_repair_guidance import repair_guidance  # noqa: E402


def verify(source: bytes) -> dict[str, Any]:
    with TemporaryDirectory(prefix="scidev-strict-svg-") as directory:
        try:
            validate_and_render_svg(source, Path(directory) / "preview.png")
        except RuntimeError as exc:
            detail = str(exc)
            return {"status": "failed", "detail": detail[:1800], "guidance": repair_guidance(detail[:4000])}
    return {"status": "passed", "detail": "The SVG passed the original structural and geometric checks."}


def main() -> None:
    source = sys.stdin.buffer.read(MAX_SVG_BYTES + 1)
    print(json.dumps(verify(source), ensure_ascii=True))


if __name__ == "__main__":
    main()
