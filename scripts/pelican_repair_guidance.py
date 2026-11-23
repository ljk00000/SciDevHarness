"""Turn known SVG validator findings into bounded, tool-independent repair guidance."""

from __future__ import annotations

import ast
import re

PART_ADVICE = {
    "left-wheel": "Use an unfilled, outlined circular tire at the rear hub.",
    "right-wheel": "Use an unfilled, outlined circular tire at the front hub.",
    "bicycle-frame": "Use a single visible polyline through the rear hub, seat joint, head tube and crank (the midpoint of the two pedal circles).",
    "bicycle-fork": "Join the head tube to the front hub with the visible fork.",
    "bicycle-spokes": "Put both wheels' radial segments inside ONE g with this ID; use at least four actual line elements or distinct M-started path segments per wheel. Do not leave the front spokes in a separate group outside this one.",
    "bicycle-saddle": "Use a compact visible saddle just above the frame's seat joint, behind the handlebar.",
    "bicycle-handlebar": "Use a compact visible grip forward of the saddle and mark its actual coordinates as the reaching-wing target.",
    "bicycle-pedals": "Within this group put two small, visible, non-concentric circles at distinct positions near the crank. Use exactly those centers as the two leg endpoints.",
    "pelican-body": "Keep a curved torso behind the head, about half as wide as the distance between wheel centers. Keep enough height, keep the inner wing mostly inside it, and avoid rotation if it makes the rendered bounds too wide.",
    "pelican-head": "Overlap the filled head vertically with the upper torso: a separate neck between disjoint head and torso is insufficient. Move the eye, beak and throat pouch together with the head.",
    "pelican-eye": "Keep one small dark circle inside the filled head.",
    "pelican-wing": "Keep a closed, visibly filled wing mostly inside the torso.",
    "pelican-wing-reaching": "Use a filled path beginning at a shoulder inside the torso: M shoulderX shoulderY C control1X control1Y control2X control2Y gripX gripY, then return with a curve and close with Z. The first cubic endpoint must lie on the handlebar grip.",
    "pelican-beak": "Use a visibly long tapered polygon pointing forward from the head.",
    "pelican-pouch": "Use a visibly filled closed curved path under the bill. Its top must be below the head center, and its width must be less than three head radii.",
    "pelican-leg-near": "Use a separate path starting inside the torso and ending at the near pedal circle center.",
    "pelican-leg-far": "Use a separate path starting inside the torso and ending at the far pedal circle center.",
}

_PART_PATTERN = re.compile(
    r"(?<![\w-])(" + "|".join(sorted(map(re.escape, PART_ADVICE), key=len, reverse=True)) + r")(?![\w-])"
)


def _reported_list(detail: str, label: str, next_label: str) -> list[str]:
    match = re.search(rf"\b{label}=(\[.*?\]); {next_label}=", detail)
    if not match:
        return []
    try:
        values = ast.literal_eval(match[1])
    except (ValueError, SyntaxError, RecursionError):
        return []
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


def repair_guidance(detail: str) -> list[str]:
    """Select fixed advice by part; never copy model-generated text into an instruction."""
    missing = set(_reported_list(detail, "missing", "malformed"))
    malformed = _reported_list(detail, "malformed", "duplicate_ids")
    affected = {part for issue in malformed for part in _PART_PATTERN.findall(issue)}
    if any("bicycle frame" in issue for issue in malformed):
        affected.add("bicycle-frame")
    if any("wheels overlap" in issue or "wheels are not aligned" in issue for issue in malformed):
        affected.update(("left-wheel", "right-wheel"))
    if "pelican-leg-near" in affected or "pelican-leg-far" in affected:
        affected.update(("pelican-leg-near", "pelican-leg-far", "bicycle-pedals"))
    return [
        f"{part}: " + ("Assign this ID to the actual visible part. " if part in missing else "") + advice
        for part, advice in PART_ADVICE.items() if part in missing or part in affected
    ]
