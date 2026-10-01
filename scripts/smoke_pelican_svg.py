"""Generate and render the canonical pelican SVG task through the local coding Harness.

This opt-in smoke never downloads model weights or executes generated code. It
accepts only a loopback Ollama endpoint, uses a disposable Git workspace, and
copies the generated SVG plus its rendered preview to a temporary artifact folder.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch
from urllib.request import Request

from PySide6.QtCore import QByteArray, QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import scidev_core
from scidev_core import CodingAgent, EventLedger, GitManager, SummarySettings
from smoke_local_ollama import (
    DEFAULT_MODEL,
    MAX_TOKENS,
    _record_local_request,
    configure_local_environment,
    validate_local_base_url,
)

PELICAN_PROMPT = "Generate an SVG of a pelican riding a bicycle"
SMOKE_SESSION_PREFIX = "live_pelican_svg_smoke"
REQUEST_METRIC_FIELDS = (
    "phase",
    "streaming",
    "max_tokens",
    "tools",
    "http_status",
    "headers_seconds",
    "first_body_byte_seconds",
    "first_event_seconds",
    "first_content_seconds",
    "body_seconds",
    "elapsed_seconds",
    "request_bytes",
    "response_bytes",
    "sse_content_bytes",
    "sse_reasoning_bytes",
    "tool_argument_bytes",
)
MAX_SVG_BYTES = 2_000_000
RENDER_WIDTH = 1200
RENDER_HEIGHT = 800


def smoke_session_id(attempt: int) -> str:
    """Give each repair a new Coding Session instead of resuming a completed one."""
    if attempt < 0:
        raise ValueError("smoke attempt cannot be negative")
    suffix = "" if attempt == 0 else f"_repair_{attempt}"
    return f"{SMOKE_SESSION_PREFIX}{suffix}"


def pelican_repair_prompt(filename: str, failure_reason: str) -> str:
    """Choose a repair scope that matches the reported failure, minimizing tokens and rewrites."""
    missing_match = re.search(r"missing=\[([^\]]*)\]", failure_reason)
    missing_parts = re.findall(r"'([^']+)'", missing_match.group(1)) if missing_match else []
    malformed_match = re.search(r"malformed=\[([^\]]*)\]", failure_reason)
    malformed_parts = re.findall(r"'([^']+)'", malformed_match.group(1)) if malformed_match else []
    duplicate_match = re.search(r"duplicate_ids=\[([^\]]*)\]", failure_reason)
    duplicate_ids = re.findall(r"'([^']+)'", duplicate_match.group(1)) if duplicate_match else []
    target = Path(filename).name
    color_or_wheel_fill_only = (
        not missing_parts
        and not duplicate_ids
        and bool(malformed_parts)
        and all(
            item.startswith("invalid paint ") or "must be an unfilled, visibly outlined tire" in item
            for item in malformed_parts
        )
    )
    if color_or_wheel_fill_only:
        return f"""Original task: {PELICAN_PROMPT}
Repair only SVG paint/fill errors in {target}. Validator: {failure_reason}
Replace unsupported color names with valid hex colors: teal `#168b83`, ochre `#e4b35b`, coral `#e9785b`, navy `#18324b`. Set both wheel circles to `fill="none"` and a visible navy stroke. Preserve every path, position, and other style. Use the smallest exact `replace_in_file` edit(s), then inspect `git_diff`; do not use shell."""
    missing_guidance = {
        "left-wheel": "Give the rear wheel its own unfilled `<circle id=\"left-wheel\">` at the rear frame hub.",
        "right-wheel": "Give the front wheel its own unfilled `<circle id=\"right-wheel\">` at the front frame hub.",
        "pelican-wing": "Add a distinct curved, filled wing inside the pelican body silhouette.",
        "pelican-wing-reaching": "Add a distinct closed, filled curved wing from the shoulder to the existing handlebar.",
        "bicycle-frame": "Identify the connected frame polyline through both wheel hubs and the crank.",
        "bicycle-fork": "Identify the visible fork joining the front frame to the front wheel hub.",
        "bicycle-spokes": "Use one `<g id=\"bicycle-spokes\">` containing radial lines in both wheels.",
        "bicycle-saddle": "Identify the visible saddle above the frame.",
        "bicycle-handlebar": "Identify the handlebar ahead of the saddle.",
        "bicycle-pedals": "Identify the pedal/crank at the frame's crank joint.",
        "pelican-body": "Identify the bird's organic body silhouette.",
        "pelican-head": "Identify the distinct filled head shape.",
        "pelican-eye": "Identify the small dark circular eye.",
        "pelican-beak": "Identify the long forward-pointing polygon beak.",
        "pelican-pouch": "Identify the closed curved throat pouch below the bill.",
        "pelican-leg-near": "Identify a leg visibly joining the body to the saddle.",
        "pelican-leg-far": "Identify a leg visibly joining the body to the pedal.",
    }
    if missing_parts and len(missing_parts) <= 6 and not malformed_parts and not duplicate_ids:
        guidance_lines = []
        for part in missing_parts:
            detail = missing_guidance.get(part, f"Add one visible shape with exact ID `{part}`.")
            guidance_lines.append(f"- {detail}")
        guidance = "\n".join(guidance_lines)
        return f"""Original task: {PELICAN_PROMPT}
Repair only the missing SVG parts in {target}; preserve the existing drawing. Validator: {failure_reason}
Missing-part instructions:
{guidance}
Read the file. If a correct visible shape exists, add its missing ID; otherwise insert only the missing shape(s) before `</svg>`. Keep IDs unique, use existing geometry/color style, and preserve every passing element. Make minimal `replace_in_file` edit(s), then inspect `git_diff`; do not rewrite the full SVG or use shell."""
    if len(missing_parts) >= 7 or len(malformed_parts) >= 3:
        return f"""Original task: {PELICAN_PROMPT}
The existing {target} has several structural errors. Replace it once with a complete, polished pelican riding a bicycle; do not preserve broken geometry. Validator: {failure_reason}
Use viewBox="0 0 640 420", balanced whitespace, navy outlines `#18324b`, teal `#168b83`, ochre `#e4b35b`, coral `#e9785b`, and background `#f4f7fb`; use only valid CSS/SVG color values, preferably these hex codes. Fixed layout: wheel centers (130,325) and (500,325), radius 60; frame polyline passes through both hubs and crank (390,265), with seat joint (275,230); saddle near (275,225), handlebar near (452,205); bird body around x=275..390/y=122..213, head centered (370,140) with radius 20, bill projecting right to x=468, and pouch hanging below. Connect both legs to the saddle/pedal and the reaching wing tip to the handlebar. Draw radial spokes, a fork and coherent anatomy; keep every shape inside the canvas.
Use each exact ID once: left-wheel, right-wheel, bicycle-frame, bicycle-fork, bicycle-spokes, bicycle-saddle, bicycle-handlebar, bicycle-pedals, pelican-body, pelican-head, pelican-eye, pelican-wing, pelican-wing-reaching, pelican-beak, pelican-pouch, pelican-leg-near, pelican-leg-far. Wheels are `<circle>`; frame is `<polyline>`; spokes are one `<g>` containing radial lines; beak is `<polygon>`; body is a curved `<path>` or `<ellipse>`; head/eye are circles; pouch is a closed curved path. Call `write_file` once and inspect `git_diff`. Do not use shell."""
    if duplicate_ids:
        return f"""Original task: {PELICAN_PROMPT}
Repair duplicate SVG IDs in {target} only. Validator: {failure_reason}
For each listed duplicate, preserve the ID on the correct primary shape and rename redundant copies to unique descriptive IDs; for `bicycle-spokes`, prefer one `<g id="bicycle-spokes">` around its spoke lines. Read the file, make the smallest exact `replace_in_file` edit(s), then inspect `git_diff`. Preserve all other artwork; do not use shell."""
    if "left-wheel" in missing_parts or "right-wheel" in missing_parts or "must be a circle" in failure_reason:
        return f"""Original task: {PELICAN_PROMPT}
Repair only the wheel error in {target}. Validator: {failure_reason}
Read the file, preserve all correct artwork, and use exact `replace_in_file` edits only. Replace both defective/prefixed wheel elements with these exact circles, then inspect `git_diff`; do not rewrite the whole SVG or use shell:
`<circle id="left-wheel" cx="125" cy="326" r="72" fill="none" stroke="#18324B" stroke-width="8"/>`
`<circle id="right-wheel" cx="515" cy="326" r="72" fill="none" stroke="#18324B" stroke-width="8"/>`"""
    if "viewBox is too small" in failure_reason:
        return f"""Original task: {PELICAN_PROMPT}
Repair only the root `viewBox` of {target}. Validator: {failure_reason}
Read the current SVG. Preserve the drawing; adjust the root viewBox to fully contain it with dimensions at least 300x180, scaling the artwork uniformly if required. Use one precise `replace_in_file`, inspect `git_diff`, and do not use shell."""
    return f"""Original task: {PELICAN_PROMPT}
Repair only the reported issue in {target}. Validator: {failure_reason}
Read the current SVG, preserve every passing element, and use the smallest exact `replace_in_file` edit(s). Inspect `git_diff`; do not rewrite the full file or use shell."""


def record_smoke_event(
    events: list[tuple[str, dict[str, Any]]],
    event_name: str,
    data: dict[str, Any],
) -> None:
    """Coalesce adjacent SSE deltas so diagnostics retain meaningful events."""
    if event_name != "assistant_delta":
        events.append((event_name, data))
        return
    text = str(data.get("text", ""))
    session_id = str(data.get("session_id", ""))
    turn = int(data.get("turn", 0) or 0)
    if events:
        last_name, last_data = events[-1]
        if (
            last_name == "assistant_delta"
            and last_data.get("session_id") == session_id
            and last_data.get("turn") == turn
        ):
            last_data["characters"] = int(last_data.get("characters", 0)) + len(text)
            last_data["tail"] = (str(last_data.get("tail", "")) + text)[-1200:]
            return
    events.append(
        (
            "assistant_delta",
            {"session_id": session_id, "turn": turn, "characters": len(text), "tail": text[-1200:]},
        )
    )


def latest_svg_write_path(file_calls: list[dict[str, Any]]) -> str | None:
    """Choose the newest SVG artifact when a model retries under a new filename."""
    for call in reversed(file_calls):
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            continue
        path = str(arguments.get("path", ""))
        if path.casefold().endswith(".svg"):
            return path
    return None


def collect_svg_mutations(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Track both initial writes and targeted replacements of SVG artifacts."""
    return [
        data
        for event_name, data in events
        if event_name == "tool_started"
        and data.get("source") != "harness_precommit"
        and data.get("name") in {"write_file", "replace_in_file"}
        and isinstance(data.get("arguments"), dict)
        and str(data["arguments"].get("path", "")).casefold().endswith(".svg")
    ]


def collect_shell_requests(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, str]]:
    """Record that shell was requested without copying potentially sensitive commands."""
    return [
        {
            "session_id": str(data.get("session_id", ""))[:160],
            "source": str(data.get("source", ""))[:160],
        }
        for event_name, data in events
        if event_name == "tool_started"
        and data.get("name") == "run_command"
        and data.get("source") != "harness_precommit"
    ]


def validate_and_render_svg(source: bytes, preview_path: Path) -> dict[str, Any]:
    """Validate static SVG safety and render it into a non-empty PNG preview."""
    if not source or len(source) > MAX_SVG_BYTES:
        raise RuntimeError(f"SVG is empty or exceeds {MAX_SVG_BYTES} bytes")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("SVG is not valid UTF-8") from exc
    lowered = text.casefold()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise RuntimeError("SVG document types and entities are not allowed")

    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise RuntimeError(f"SVG is not well-formed XML: {exc}") from exc
    local_name = root.tag.rsplit("}", 1)[-1].casefold() if isinstance(root.tag, str) else ""
    if local_name != "svg":
        raise RuntimeError("document root is not an SVG element")
    view_box = root.attrib.get("viewBox", "").replace(",", " ").split()
    try:
        view_x, view_y, view_width, view_height = (float(value) for value in view_box)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("SVG must define a four-number viewBox") from exc
    viewbox_problem = None
    if view_width < 300 or view_height < 180:
        viewbox_problem = (
            "SVG viewBox is too small for the requested two-object illustration "
            f"(got {view_width:g}x{view_height:g}; need at least 300x180)"
        )

    counts: Counter[str] = Counter()
    invalid_paints: list[str] = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        tag = element.tag.rsplit("}", 1)[-1].casefold()
        counts[tag] += 1
        if tag in {"script", "foreignobject", "iframe", "object", "embed"}:
            raise RuntimeError(f"active or embedded content is not allowed in SVG: {tag}")
        if tag == "style" and (
            "@import" in (element.text or "").casefold()
            or re.search(r"url\s*\(\s*(?!['\"]?#)", element.text or "", re.I)
        ):
            raise RuntimeError("external SVG stylesheets and resources are not allowed")
        for raw_name, value in element.attrib.items():
            name = raw_name.rsplit("}", 1)[-1].casefold()
            if name.startswith("on"):
                raise RuntimeError(f"SVG event handler is not allowed: {name}")
            if name in {"fill", "stroke", "color"}:
                paint = value.strip()
                if paint.casefold() not in {"none", "currentcolor", "inherit", "context-fill", "context-stroke"}:
                    if not re.fullmatch(r"url\(\s*['\"]?#[-A-Za-z0-9_.]+['\"]?\s*\)", paint) and not QColor(paint).isValid():
                        invalid_paints.append(f"{name}={paint}")
            if name in {"href", "src"} and value.strip() and not value.strip().startswith("#"):
                raise RuntimeError("external SVG resources are not allowed")
            if re.search(r"url\s*\(\s*(?!['\"]?#)", value, re.I) or (
                name == "style" and "@import" in value.casefold()
            ):
                raise RuntimeError("external SVG stylesheets and resources are not allowed")
    identified_ids = [
        str(element.attrib["id"]).casefold()
        for element in root.iter()
        if isinstance(element.tag, str) and element.attrib.get("id")
    ]
    id_counts = Counter(identified_ids)
    duplicate_ids = sorted(identifier for identifier, count in id_counts.items() if count > 1)
    identified_elements: dict[str, ET.Element] = {}
    for element in root.iter():
        if isinstance(element.tag, str) and element.attrib.get("id"):
            identified_elements.setdefault(str(element.attrib["id"]).casefold(), element)

    renderer = QSvgRenderer(QByteArray(source))
    if not renderer.isValid():
        raise RuntimeError("Qt could not parse the generated SVG")
    default_size = renderer.defaultSize()
    if default_size.width() < 1 or default_size.height() < 1:
        raise RuntimeError("SVG has no usable intrinsic dimensions or viewBox")
    if default_size.width() > 8192 or default_size.height() > 8192:
        raise RuntimeError("SVG dimensions exceed the 8192-pixel smoke-test limit")

    background = QColor("#f4f7fb")
    image = QImage(RENDER_WIDTH, RENDER_HEIGHT, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(background)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, RENDER_WIDTH, RENDER_HEIGHT))
    painter.end()

    changed_samples = 0
    step = 4
    for y in range(0, RENDER_HEIGHT, step):
        for x in range(0, RENDER_WIDTH, step):
            if image.pixelColor(x, y) != background:
                changed_samples += 1
    if changed_samples < 25:
        raise RuntimeError("SVG rendered almost no visible pixels")

    preview_path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(preview_path), "PNG"):
        raise RuntimeError(f"failed to save SVG preview: {preview_path}")
    required_parts = {
        "left-wheel",
        "right-wheel",
        "bicycle-frame",
        "bicycle-fork",
        "bicycle-spokes",
        "bicycle-saddle",
        "bicycle-handlebar",
        "bicycle-pedals",
        "pelican-body",
        "pelican-head",
        "pelican-eye",
        "pelican-wing",
        "pelican-wing-reaching",
        "pelican-beak",
        "pelican-pouch",
        "pelican-leg-near",
        "pelican-leg-far",
    }
    missing_parts = sorted(required_parts - identified_elements.keys())
    near_matches = {
        missing: sorted(
            candidate
            for candidate in identified_elements
            if candidate.endswith(f"-{missing}") or candidate.startswith(f"{missing}-")
        )
        for missing in missing_parts
    }
    near_matches = {missing: candidates for missing, candidates in near_matches.items() if candidates}
    expected_tags = {
        "left-wheel": ({"circle"}, "a circle"),
        "right-wheel": ({"circle"}, "a circle"),
        "bicycle-frame": ({"polyline"}, "a connected polyline"),
        "bicycle-spokes": ({"g"}, "a group containing spoke shapes"),
        "pelican-head": ({"circle", "ellipse"}, "a circle or ellipse"),
        "pelican-eye": ({"circle"}, "a circle"),
        "pelican-beak": ({"polygon"}, "a polygon"),
        "pelican-body": ({"path", "ellipse"}, "an organic path or ellipse"),
        "pelican-wing": ({"path", "ellipse", "polygon"}, "a filled path, ellipse, or polygon"),
        "pelican-pouch": ({"path"}, "a curved path"),
        "pelican-wing-reaching": ({"path"}, "a curved path"),
    }
    malformed_parts = []
    for identifier, (allowed_tags, description) in expected_tags.items():
        element = identified_elements.get(identifier)
        if element is None:
            continue
        actual_tag = element.tag.rsplit("}", 1)[-1].casefold()
        if actual_tag not in allowed_tags:
            malformed_parts.append(f"{identifier} must be {description} (got {actual_tag})")
    malformed_parts.extend(f"invalid paint {paint}" for paint in invalid_paints[:6])
    for wheel_id in ("left-wheel", "right-wheel"):
        wheel = identified_elements.get(wheel_id)
        if wheel is None or wheel.tag.rsplit("}", 1)[-1].casefold() != "circle":
            continue
        fill = wheel.attrib.get("fill", "").strip().casefold()
        stroke = wheel.attrib.get("stroke", "").strip()
        if fill not in {"none", "white", "#fff", "#ffffff"} or not stroke or stroke.casefold() in {"none", "transparent"}:
            malformed_parts.append(f"{wheel_id} must be an unfilled, visibly outlined tire")
    if viewbox_problem or missing_parts or malformed_parts or duplicate_ids:
        raise RuntimeError(
            f"SVG preflight failed; {viewbox_problem or 'viewBox dimensions are adequate'}; "
            "lacks separately identified visual parts or uses incorrect core shapes: "
            f"invalid paint colors={invalid_paints[:6]}; missing={missing_parts}; "
            f"malformed={malformed_parts}; duplicate_ids={duplicate_ids}; "
            f"near_matches={near_matches}"
        )
    wheel_data = []
    for wheel_id in ("left-wheel", "right-wheel"):
        wheel = identified_elements[wheel_id]
        if wheel.tag.rsplit("}", 1)[-1].casefold() != "circle":
            raise RuntimeError(f"SVG {wheel_id} must be drawn as a circle")
        try:
            wheel_data.append(tuple(float(wheel.attrib[key]) for key in ("cx", "cy", "r")))
        except (KeyError, ValueError) as exc:
            raise RuntimeError(f"SVG {wheel_id} has invalid circle geometry") from exc
    (left_x, left_y, left_radius), (right_x, right_y, right_radius) = wheel_data
    if left_radius <= 0 or right_radius <= 0:
        raise RuntimeError("SVG bicycle wheels must have positive radii")
    for wheel_id in ("left-wheel", "right-wheel"):
        wheel = identified_elements[wheel_id]
        fill = wheel.attrib.get("fill", "").strip().casefold()
        stroke = wheel.attrib.get("stroke", "").strip()
        if fill not in {"none", "white", "#fff", "#ffffff"} or not stroke or stroke.casefold() in {"none", "transparent"}:
            raise RuntimeError(f"SVG {wheel_id} must be a visibly outlined, unfilled tire")
    wheel_distance = math.hypot(right_x - left_x, right_y - left_y)
    if wheel_distance < 0.9 * (left_radius + right_radius):
        raise RuntimeError("SVG bicycle wheels overlap instead of forming a readable bicycle")
    if abs(right_y - left_y) > 0.35 * (left_radius + right_radius):
        raise RuntimeError("SVG bicycle wheels are not aligned at a readable height")
    head = identified_elements["pelican-head"]
    head_tag = head.tag.rsplit("}", 1)[-1].casefold()
    if head_tag == "circle":
        head_size = float(head.attrib.get("r", "0"))
    elif head_tag == "ellipse":
        head_size = min(float(head.attrib.get("rx", "0")), float(head.attrib.get("ry", "0")))
    else:
        raise RuntimeError("pelican-head must be a distinct circle or ellipse, not an abstract path")
    if head_size < 8:
        raise RuntimeError("pelican-head is too small to read as a head")
    eye = identified_elements["pelican-eye"]
    if eye.tag.rsplit("}", 1)[-1].casefold() != "circle" or float(eye.attrib.get("r", "0")) < 2:
        raise RuntimeError("pelican-eye must be a visible small circle, not omitted decoration")
    eye_fill = eye.attrib.get("fill", "#000000").strip().casefold()
    if eye_fill in {"none", "transparent", "white", "#fff", "#ffffff"}:
        raise RuntimeError("pelican-eye must use a visible dark fill")
    beak = identified_elements["pelican-beak"]
    if beak.tag.rsplit("}", 1)[-1].casefold() != "polygon":
        raise RuntimeError("pelican-beak must be a distinct pointed polygon")
    beak_numbers = [float(value) for value in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)", beak.attrib.get("points", ""))]
    if len(beak_numbers) < 6 or len(beak_numbers) % 2:
        raise RuntimeError("pelican-beak polygon has too few valid points")
    if max(beak_numbers[::2]) - min(beak_numbers[::2]) < max(60, head_size * 2.5):
        raise RuntimeError("pelican-beak is too short relative to the head to read as a long pelican bill")
    head_fill = head.attrib.get("fill", "black").strip().casefold()
    if head_fill in {"none", "transparent"}:
        raise RuntimeError("pelican-head needs a filled silhouette, not an empty outlined ring")
    body = identified_elements["pelican-body"]
    body_tag = body.tag.rsplit("}", 1)[-1].casefold()
    if body_tag not in {"path", "ellipse"}:
        raise RuntimeError("pelican-body must be an organic path or ellipse, not a group or box")
    if body_tag == "path" and not re.search(r"[cqst]", body.attrib.get("d", ""), re.I):
        raise RuntimeError("pelican-body path must use curved geometry rather than a rectangle-like polygon")
    body_fill = body.attrib.get("fill", "").strip().casefold()
    if body_fill in {"", "none", "white", "#fff", "#ffffff"} and not body.attrib.get("stroke"):
        raise RuntimeError("pelican-body has too little contrast against the blank canvas")
    wing = identified_elements["pelican-wing"]
    if wing.tag.rsplit("}", 1)[-1].casefold() not in {"path", "ellipse", "polygon"}:
        raise RuntimeError("pelican-wing must be a filled, recognizable shape")
    if wing.attrib.get("fill", "").strip().casefold() in {"", "none", "transparent"}:
        raise RuntimeError("pelican-wing must have a visible fill")
    pouch = identified_elements["pelican-pouch"]
    if pouch.tag.rsplit("}", 1)[-1].casefold() != "path":
        raise RuntimeError("pelican-pouch must be a curved filled path, not a box")
    if not re.search(r"[cqst]", pouch.attrib.get("d", ""), re.I) or not re.search(r"z\s*$", pouch.attrib.get("d", ""), re.I):
        raise RuntimeError("pelican-pouch must be a closed curved shape")
    if pouch.attrib.get("fill", "").strip().casefold() in {"", "none", "transparent"}:
        raise RuntimeError("pelican-pouch must have a visible fill")
    body_bounds = renderer.boundsOnElement("pelican-body")
    head_bounds = renderer.boundsOnElement("pelican-head")
    wing_bounds = renderer.boundsOnElement("pelican-wing")
    pouch_bounds = renderer.boundsOnElement("pelican-pouch")
    if min(body_bounds.width(), body_bounds.height(), head_bounds.width(), head_bounds.height()) <= 0:
        raise RuntimeError("pelican body/head must have non-empty rendered silhouettes")
    if body_bounds.height() < wheel_distance * 0.19:
        raise RuntimeError("pelican-body is too flat to read as a bird torso")
    if body_bounds.width() > wheel_distance * 0.58:
        raise RuntimeError("pelican-body is too wide and overwhelms the bicycle")
    if body_bounds.center().x() >= head_bounds.center().x() + head_size * 0.25:
        raise RuntimeError("pelican-body must sit behind and connect visually to the forward-facing head")
    vertical_gap = max(
        0.0,
        max(body_bounds.top(), head_bounds.top()) - min(body_bounds.bottom(), head_bounds.bottom()),
    )
    if vertical_gap > head_size * 0.25:
        raise RuntimeError("pelican-head is detached from the body instead of forming one rider silhouette")
    wing_area = max(1.0, wing_bounds.width() * wing_bounds.height())
    wing_overlap = body_bounds.intersected(wing_bounds)
    if wing_overlap.width() * wing_overlap.height() < wing_area * 0.5:
        raise RuntimeError("pelican-wing must sit mostly inside the body silhouette")
    if pouch_bounds.width() > head_size * 3.0 or pouch_bounds.top() <= head_bounds.center().y():
        raise RuntimeError("pelican-pouch must be a compact throat shape hanging below the head/bill")
    saddle_bounds = renderer.boundsOnElement("bicycle-saddle")
    handlebar_bounds = renderer.boundsOnElement("bicycle-handlebar")
    pedal_bounds = renderer.boundsOnElement("bicycle-pedals")
    near_leg_bounds = renderer.boundsOnElement("pelican-leg-near")
    far_leg_bounds = renderer.boundsOnElement("pelican-leg-far")
    rider_parts = {
        "bicycle-saddle": saddle_bounds,
        "bicycle-handlebar": handlebar_bounds,
        "bicycle-pedals": pedal_bounds,
        "pelican-leg-near": near_leg_bounds,
        "pelican-leg-far": far_leg_bounds,
    }
    for part_id, bounds in rider_parts.items():
        if bounds.width() <= 0 or bounds.height() <= 0:
            raise RuntimeError(f"{part_id} must be a visible non-empty shape")
    if handlebar_bounds.center().x() <= saddle_bounds.center().x():
        raise RuntimeError("bicycle-handlebar must be ahead of the saddle")
    if (
        near_leg_bounds.intersected(body_bounds).isEmpty()
        or near_leg_bounds.intersected(saddle_bounds).isEmpty()
    ):
        raise RuntimeError("pelican-leg-near must visibly connect the body to the saddle")
    if (
        far_leg_bounds.intersected(body_bounds).isEmpty()
        or far_leg_bounds.intersected(pedal_bounds).isEmpty()
    ):
        raise RuntimeError("pelican-leg-far must visibly connect the body to the pedals")
    reaching_wing_bounds = renderer.boundsOnElement("pelican-wing-reaching")
    reaching_wing = identified_elements["pelican-wing-reaching"]
    reaching_wing_path = reaching_wing.attrib.get("d", "")
    reaching_wing_fill = reaching_wing.attrib.get("fill", "").strip().casefold()
    svg_number = r"([-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?)"
    wing_tip_match = re.match(
        rf"\s*M\s*{svg_number}[\s,]+{svg_number}\s+C\s*"
        rf"{svg_number}[\s,]+{svg_number}[\s,]+{svg_number}[\s,]+{svg_number}[\s,]+{svg_number}[\s,]+{svg_number}",
        reaching_wing_path,
        re.I,
    )
    wing_tip = (float(wing_tip_match.group(7)), float(wing_tip_match.group(8))) if wing_tip_match else None
    handlebar_contact_area = handlebar_bounds.adjusted(-2, -2, 2, 2)
    if (
        reaching_wing.tag.rsplit("}", 1)[-1].casefold() != "path"
        or not re.search(r"[cq]", reaching_wing_path, re.I)
        or not re.search(r"z\s*$", reaching_wing_path, re.I)
        or reaching_wing_fill in {"", "none", "transparent"}
        or wing_tip is None
        or not handlebar_contact_area.contains(*wing_tip)
        or reaching_wing_bounds.intersected(body_bounds).isEmpty()
    ):
        raise RuntimeError(
            "pelican-wing-reaching must be a closed filled curve that starts at the shoulder "
            "and visibly reaches the handlebar"
        )
    for wheel_id, (wheel_x, wheel_y, wheel_radius) in zip(("left-wheel", "right-wheel"), wheel_data):
        if (
            wheel_x - wheel_radius < view_x
            or wheel_x + wheel_radius > view_x + view_width
            or wheel_y - wheel_radius < view_y
            or wheel_y + wheel_radius > view_y + view_height
        ):
            raise RuntimeError(f"SVG {wheel_id} is clipped by the viewBox")

    frame = identified_elements["bicycle-frame"]
    if frame.tag.rsplit("}", 1)[-1].casefold() != "polyline":
        raise RuntimeError("SVG bicycle frame must be a connected polyline through both wheel hubs")
    frame_stroke = frame.attrib.get("stroke", "").strip()
    if not frame_stroke or frame_stroke.casefold() in {"none", "transparent"}:
        raise RuntimeError("SVG bicycle frame needs a visible stroke")
    frame_numbers = [float(value) for value in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)", frame.attrib.get("points", ""))]
    if len(frame_numbers) < 6 or len(frame_numbers) % 2:
        raise RuntimeError("SVG bicycle frame polyline has too few valid points")
    frame_points = list(zip(frame_numbers[::2], frame_numbers[1::2]))
    hub_tolerance = max(1.0, min(left_radius, right_radius) * 0.25)
    if not any(math.hypot(x - left_x, y - left_y) <= hub_tolerance for x, y in frame_points):
        raise RuntimeError("SVG bicycle frame must meet the rear wheel hub")
    crank_bounds = renderer.boundsOnElement("bicycle-pedals")
    if not any(
        min(left_x, right_x) < x < max(left_x, right_x)
        and abs(y - (left_y + right_y) / 2) > min(left_radius, right_radius) * 0.5
        for x, y in frame_points
    ):
        raise RuntimeError("SVG bicycle frame has no readable triangular center vertex")
    crank_x = crank_bounds.center().x()
    crank_y = crank_bounds.center().y()
    if not any(math.hypot(x - crank_x, y - crank_y) <= hub_tolerance for x, y in frame_points):
        raise RuntimeError("SVG bicycle frame must meet the visible crank/pedals")
    fork = identified_elements["bicycle-fork"]
    fork_bounds = renderer.boundsOnElement("bicycle-fork")
    if fork.tag.rsplit("}", 1)[-1].casefold() not in {"path", "polyline", "line"}:
        raise RuntimeError("bicycle-fork must be a visible path/line from head tube to front hub")
    if (
        fork_bounds.right() < right_x - hub_tolerance
        or fork_bounds.bottom() < right_y - hub_tolerance
        or fork_bounds.left() > right_x - right_radius * 0.35
    ):
        raise RuntimeError("bicycle-fork must visibly connect the front frame to the front wheel hub")
    spokes = identified_elements["bicycle-spokes"]
    spoke_segments = 0
    for element in spokes.iter():
        if not isinstance(element.tag, str):
            continue
        spoke_tag = element.tag.rsplit("}", 1)[-1].casefold()
        if spoke_tag == "path":
            spoke_segments += len(re.findall(r"[Mm]", element.attrib.get("d", "")))
        elif spoke_tag == "line":
            spoke_segments += 1
        elif spoke_tag == "polyline":
            coordinates = re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?", element.attrib.get("points", ""))
            spoke_segments += max(0, len(coordinates) // 2 - 1)
    if spokes.tag.rsplit("}", 1)[-1].casefold() != "g" or spoke_segments < 8:
        raise RuntimeError("bicycle-spokes must contain visible radial spoke lines for both wheels")
    return {
        "svg_bytes": len(source),
        "intrinsic_width": default_size.width(),
        "intrinsic_height": default_size.height(),
        "element_counts": dict(sorted(counts.items())),
        "render_size": [RENDER_WIDTH, RENDER_HEIGHT],
        "non_background_samples": changed_samples,
        "preview_png": str(preview_path),
    }


def persist_failure_diagnostic(
    artifact_dir: Path,
    *,
    model: str,
    attempt: int,
    session_id: str,
    reason: str,
    events: list[tuple[str, dict[str, Any]]],
    request_count: int,
    request_metrics: list[dict[str, Any]] | None = None,
) -> Path:
    """Keep a bounded, credential-free trace when the live visual task fails."""
    trace: list[dict[str, Any]] = []
    for event_name, data in events[-16:]:
        item: dict[str, Any] = {"event": event_name}
        for field in ("session_id", "name", "source"):
            if data.get(field) is not None:
                item[field] = str(data[field])[:160]
        arguments = data.get("arguments")
        if isinstance(arguments, dict):
            if data.get("name") == "run_command":
                item["arguments"] = {"command_omitted": True}
            elif data.get("name") in {"write_file", "replace_in_file"}:
                item["arguments"] = {
                    "path": str(arguments.get("path", ""))[:240],
                    "content_characters": len(str(arguments.get("content", ""))),
                }
        if event_name == "assistant_delta":
            item["text_characters"] = int(data.get("characters", 0))
            response_text = data.get("tail", "")
        else:
            response_text = data.get("text", data.get("result"))
        if response_text is not None:
            rendered_text = str(response_text)
            item["text_preview"] = rendered_text[:2400]
            if len(rendered_text) > 2400:
                item["text_tail"] = rendered_text[-1200:]
        trace.append(item)

    metrics = request_metrics or []
    safe_request_metrics = [
        {field: request[field] for field in REQUEST_METRIC_FIELDS if field in request}
        for request in metrics[-64:]
    ]
    diagnostic = {
        "prompt": PELICAN_PROMPT,
        "model": model,
        "attempt": attempt,
        "session_id": session_id,
        "failure_reason": reason[:1000],
        "local_request_count": request_count,
        "request_metrics": safe_request_metrics,
        "request_metrics_truncated": max(0, len(metrics) - len(safe_request_metrics)),
        "blocked_shell_requests": collect_shell_requests(events),
        "recent_events": trace,
        "credential_fields_omitted": True,
    }
    path = artifact_dir / "pelican_failure_diagnostics.json"
    path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class _TimedResponse:
    """Measure response-body consumption, not just the time to receive headers."""

    def __init__(
        self,
        response: Any,
        record: dict[str, Any],
        *,
        started_at: float,
        headers_at: float,
        streaming: bool,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._response = response
        self._record = record
        self._started_at = started_at
        self._headers_at = headers_at
        self._streaming = streaming
        self._clock = clock
        self._finished = False
        record.update(
            streaming=streaming,
            headers_seconds=round(headers_at - started_at, 3),
            first_body_byte_seconds=None,
            first_event_seconds=None,
            first_content_seconds=None,
            body_seconds=None,
            elapsed_seconds=None,
            response_bytes=0,
            sse_content_bytes=0,
            sse_reasoning_bytes=0,
            tool_argument_bytes=0,
        )
        status = getattr(response, "status", None)
        if status is None and callable(getattr(response, "getcode", None)):
            status = response.getcode()
        if status is not None:
            record["http_status"] = status

    def _observe_chunk(self, chunk: bytes | bytearray | memoryview | str) -> None:
        if not chunk:
            return
        if isinstance(chunk, str):
            raw = chunk.encode("utf-8", errors="replace")
            text = chunk
        else:
            raw = bytes(chunk)
            text = raw.decode("utf-8", errors="replace")
        self._record["response_bytes"] += len(raw)
        content_bytes, reasoning_bytes, tool_argument_bytes = self._sse_output_sizes(text)
        self._record["sse_content_bytes"] += content_bytes
        self._record["sse_reasoning_bytes"] += reasoning_bytes
        self._record["tool_argument_bytes"] += tool_argument_bytes

        is_first_body = self._record["first_body_byte_seconds"] is None
        is_first_event = (
            self._streaming
            and self._record["first_event_seconds"] is None
            and any(
                line.strip().startswith("data:") and line.strip() != "data: [DONE]"
                for line in text.splitlines()
            )
        )
        is_first_content = (
            self._streaming
            and self._record["first_content_seconds"] is None
            and self._contains_model_output(text)
        )
        if is_first_body or is_first_event or is_first_content:
            elapsed = round(self._clock() - self._started_at, 3)
            if is_first_body:
                self._record["first_body_byte_seconds"] = elapsed
            if is_first_event:
                self._record["first_event_seconds"] = elapsed
            if is_first_content:
                self._record["first_content_seconds"] = elapsed

    @staticmethod
    def _contains_model_output(text: str) -> bool:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload_text = line[5:].strip()
            if not payload_text or payload_text == "[DONE]":
                continue
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                continue
            choices = payload.get("choices", []) if isinstance(payload, dict) else []
            for choice in choices if isinstance(choices, list) else []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                if delta.get("content") or delta.get("tool_calls") or delta.get("function_call"):
                    return True
        return False

    @staticmethod
    def _sse_output_sizes(text: str) -> tuple[int, int, int]:
        content_bytes = 0
        reasoning_bytes = 0
        tool_argument_bytes = 0
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload_text = line[5:].strip()
            if not payload_text or payload_text == "[DONE]":
                continue
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                continue
            choices = payload.get("choices", []) if isinstance(payload, dict) else []
            for choice in choices if isinstance(choices, list) else []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    delta = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                for key in ("content",):
                    value = delta.get(key)
                    if isinstance(value, str):
                        content_bytes += len(value.encode("utf-8"))
                    elif isinstance(value, list):
                        content_bytes += sum(
                            len(str(part.get("text", "")).encode("utf-8"))
                            for part in value
                            if isinstance(part, dict)
                        )
                for key in ("reasoning", "reasoning_content"):
                    value = delta.get(key)
                    if isinstance(value, str):
                        reasoning_bytes += len(value.encode("utf-8"))
                calls = delta.get("tool_calls")
                if isinstance(calls, list):
                    for call in calls:
                        function = call.get("function") if isinstance(call, dict) else None
                        if isinstance(function, dict):
                            tool_argument_bytes += sum(
                                len(str(function.get(key, "")).encode("utf-8"))
                                for key in ("name", "arguments")
                            )
                legacy_call = delta.get("function_call")
                if isinstance(legacy_call, dict):
                    tool_argument_bytes += sum(
                        len(str(legacy_call.get(key, "")).encode("utf-8"))
                        for key in ("name", "arguments")
                    )
        return content_bytes, reasoning_bytes, tool_argument_bytes

    def _finish(self) -> None:
        if self._finished:
            return
        finished_at = self._clock()
        self._record["body_seconds"] = round(max(0.0, finished_at - self._headers_at), 3)
        self._record["elapsed_seconds"] = round(max(0.0, finished_at - self._started_at), 3)
        self._finished = True

    def __enter__(self) -> _TimedResponse:
        enter = getattr(self._response, "__enter__", None)
        if callable(enter):
            entered = enter()
            if entered is not None:
                self._response = entered
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool | None:
        try:
            exit_response = getattr(self._response, "__exit__", None)
            if callable(exit_response):
                return exit_response(exc_type, exc_value, traceback)
            close = getattr(self._response, "close", None)
            if callable(close):
                close()
            return None
        finally:
            self._finish()

    def __iter__(self) -> _TimedResponse:
        return self

    def __next__(self) -> bytes | str:
        chunk = next(self._response)
        self._observe_chunk(chunk)
        return chunk

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        chunk = self._response.read(*args, **kwargs)
        self._observe_chunk(chunk)
        return chunk

    def readline(self, *args: Any, **kwargs: Any) -> bytes | str:
        chunk = self._response.readline(*args, **kwargs)
        self._observe_chunk(chunk)
        return chunk

    def readinto(self, buffer: Any) -> int:
        count = self._response.readinto(buffer)
        if count:
            self._observe_chunk(memoryview(buffer)[:count])
        return count

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._finish()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


def run_smoke(
    base_url: str,
    model: str,
    api_key: str,
    artifact_dir: Path | None = None,
) -> dict[str, Any]:
    endpoint = validate_local_base_url(base_url)
    model = model.strip()
    if not model:
        raise ValueError("model name cannot be empty")
    configure_local_environment(endpoint, model, api_key)
    artifacts = (artifact_dir or Path(tempfile.mkdtemp(prefix="scidev-pelican-svg-"))).expanduser().resolve()
    artifacts.mkdir(parents=True, exist_ok=True)

    wire_requests: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    real_urlopen = scidev_core.urlopen
    request_phase = {"value": "initial"}

    def observe_request(request: Request, timeout: float = 90.0):
        _record_local_request(request, model=model, requests=wire_requests)
        started = time.perf_counter()
        record = wire_requests[-1]
        record["request_bytes"] = len(request.data or b"")
        record["phase"] = request_phase["value"]
        streaming = json.loads(request.data.decode("utf-8")).get("stream") is True
        try:
            response = real_urlopen(request, timeout=timeout)
        except Exception:
            record.update(
                streaming=streaming,
                headers_seconds=None,
                first_body_byte_seconds=None,
                first_event_seconds=None,
                first_content_seconds=None,
                body_seconds=None,
                elapsed_seconds=round(time.perf_counter() - started, 3),
                response_bytes=0,
                sse_content_bytes=0,
                sse_reasoning_bytes=0,
                tool_argument_bytes=0,
            )
            raise
        headers_at = time.perf_counter()
        return _TimedResponse(
            response,
            record,
            started_at=started,
            headers_at=headers_at,
            streaming=streaming,
        )

    with tempfile.TemporaryDirectory(prefix="scidev-pelican-workspace-") as temporary:
        root = Path(temporary)
        (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
        ledger = EventLedger(root)
        git = GitManager(root)
        git.commit_changes("pelican SVG smoke baseline")
        summary_settings = SummarySettings(
            enabled=True,
            model="",
            max_tokens=MAX_TOKENS,
            context_chars=18_000,
            retries=0,
            interval_turns=0,
        )
        agent = CodingAgent(
            root,
            ledger,
            git,
            event_callback=lambda name, data: record_smoke_event(events, name, data),
            summary_settings=summary_settings,
        )

        def run_agent(task_prompt: str, session_id: str) -> dict[str, Any]:
            request_phase["value"] = (
                "initial"
                if session_id == SMOKE_SESSION_PREFIX
                else session_id.removeprefix(f"{SMOKE_SESSION_PREFIX}_")
            )
            task = {"payload": {"session_id": session_id, "prompt": task_prompt}}
            try:
                with patch("scidev_core.urlopen", new=observe_request):
                    return agent.run(task)
            except Exception as exc:
                repair_suffix = session_id.removeprefix(f"{SMOKE_SESSION_PREFIX}_repair_")
                attempt = int(repair_suffix) if repair_suffix.isdigit() else 0
                diagnostic_path = persist_failure_diagnostic(
                    artifacts,
                    model=model,
                    attempt=attempt,
                    session_id=session_id,
                    reason=f"{type(exc).__name__}: {exc}",
                    events=events,
                    request_count=len(wire_requests),
                    request_metrics=wire_requests,
                )
                raise RuntimeError(
                    f"local Harness call failed in {session_id}: {type(exc).__name__}: {exc}; "
                    f"diagnostic saved to {diagnostic_path}"
                ) from exc

        def collect_file_calls() -> list[dict[str, Any]]:
            return collect_svg_mutations(events)

        def current_svg_path() -> tuple[str, Path]:
            relative = latest_svg_write_path(collect_file_calls())
            if not relative:
                raise RuntimeError("expected at least one SVG output from write_file")
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise RuntimeError("the generated SVG is missing or outside the disposable workspace")
            return relative, path

        def reject_shell_if_requested(attempt: int, session_id: str) -> None:
            if not collect_shell_requests(events):
                return
            relative = latest_svg_write_path(collect_file_calls())
            if relative:
                candidate = (root / relative).resolve()
                if candidate.is_relative_to(root) and candidate.is_file():
                    shutil.copyfile(candidate, artifacts / "pelican_unverified.svg")
            diagnostic_path = persist_failure_diagnostic(
                artifacts,
                model=model,
                attempt=attempt,
                session_id=session_id,
                reason="The model requested run_command; the approval gate denied execution.",
                events=events,
                request_count=len(wire_requests),
                request_metrics=wire_requests,
            )
            raise RuntimeError(
                "Qwen requested shell execution; no command was executed because no user approval was configured. "
                f"Diagnostic saved to {diagnostic_path}"
            )

        started = time.perf_counter()
        result = run_agent(PELICAN_PROMPT, smoke_session_id(0))
        file_calls = collect_file_calls()
        tool_names = [
            str(data.get("name"))
            for event_name, data in events
            if event_name == "tool_started"
            and data.get("source") not in {"harness_precommit", "harness_svg_artifact_recovery"}
        ]
        artifact_recoveries = sum(
            event_name == "tool_started" and data.get("source") == "harness_svg_artifact_recovery"
            for event_name, data in events
        )
        if not file_calls:
            diagnostic_path = persist_failure_diagnostic(
                artifacts,
                model=model,
                attempt=0,
                session_id=smoke_session_id(0),
                reason="Qwen/Harness did not create a file for the exact pelican prompt",
                events=events,
                request_count=len(wire_requests),
                request_metrics=wire_requests,
            )
            raise RuntimeError(
                "Qwen/Harness did not create a file for the exact pelican prompt: "
                f"tools={tool_names}, requests={len(wire_requests)}, diagnostic saved to {diagnostic_path}"
            )
        reject_shell_if_requested(0, smoke_session_id(0))

        preview_png = artifacts / "pelican_bicycle_preview.png"
        saved_svg = artifacts / "pelican_bicycle.svg"
        validation: dict[str, Any] | None = None
        relative_svg = ""
        svg_path = root
        for repair_attempt in range(3):
            relative_svg, svg_path = current_svg_path()
            shutil.copyfile(svg_path, saved_svg)
            try:
                validation = validate_and_render_svg(svg_path.read_bytes(), preview_png)
                break
            except RuntimeError as exc:
                if repair_attempt == 2:
                    diagnostic_path = persist_failure_diagnostic(
                        artifacts,
                        model=model,
                        attempt=repair_attempt,
                        session_id=smoke_session_id(repair_attempt),
                        reason=str(exc),
                        events=events,
                        request_count=len(wire_requests),
                        request_metrics=wire_requests,
                    )
                    raise RuntimeError(
                        f"pelican SVG still failed after two repairs: {exc}; "
                        f"diagnostic saved to {diagnostic_path}"
                    ) from exc
                previous_write_count = len(file_calls)
                repair_prompt = pelican_repair_prompt(Path(relative_svg).name, str(exc))
                # CodingAgent intentionally resumes an existing session ID.
                # A repair prompt is a new task, so it must never reuse the
                # completed session that produced the invalid SVG.
                result = run_agent(repair_prompt, smoke_session_id(repair_attempt + 1))
                file_calls = collect_file_calls()
                if len(file_calls) <= previous_write_count:
                    diagnostic_path = persist_failure_diagnostic(
                        artifacts,
                        model=model,
                        attempt=repair_attempt + 1,
                        session_id=smoke_session_id(repair_attempt + 1),
                        reason=str(exc),
                        events=events,
                        request_count=len(wire_requests),
                        request_metrics=wire_requests,
                    )
                    raise RuntimeError(
                        "Qwen did not revise the SVG after structural visual feedback; "
                        f"diagnostic saved to {diagnostic_path}"
                    ) from exc
                reject_shell_if_requested(repair_attempt + 1, smoke_session_id(repair_attempt + 1))
        if validation is None:
            raise RuntimeError("pelican SVG visual validation did not complete")
        total_elapsed = round(time.perf_counter() - started, 3)

        model_tools = [
            str(data.get("name"))
            for event_name, data in events
            if event_name == "tool_started"
            and data.get("source") not in {"harness_precommit", "harness_svg_artifact_recovery"}
        ]
        if not result.get("summary") or not result.get("summary_path"):
            raise RuntimeError("automatic final conversation summary was not produced")
        ledger_events = [
            json.loads(line)
            for line in (root / ".research" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        event_types = [event["event_type"] for event in ledger_events]
        for required in ("git_commit_created", "coding_session_completed", "conversation_summary_created"):
            if required not in event_types:
                raise RuntimeError(f"event ledger is missing {required}")
        if not any(
            event["event_type"] == "tool_result"
            and event["payload"].get("name") == "git_diff"
            and event["payload"].get("source") == "harness_precommit"
            for event in ledger_events
        ) and "git_diff" not in model_tools:
            raise RuntimeError("neither the model nor Harness inspected the pre-commit Git diff")

        committed_paths = subprocess.run(
            ["git", "show", "--format=", "--name-only", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        if committed_paths != [relative_svg.replace("\\", "/")]:
            raise RuntimeError(f"unexpected paths in the SVG auto-commit: {committed_paths}")
        commit_sha = str(result.get("git_result_sha") or "")
        if len(commit_sha) != 40:
            raise RuntimeError("SVG coding run did not return a full Git commit SHA")
        if git.status_paths():
            raise RuntimeError(f"temporary workspace is not clean: {sorted(git.status_paths())}")
        if len(wire_requests) < 2:
            raise RuntimeError(f"too few local model requests: {len(wire_requests)}")

        return {
            "status": "passed",
            "prompt": PELICAN_PROMPT,
            "endpoint": endpoint,
            "model": model,
            "svg_file": relative_svg,
            "svg_artifact": str(saved_svg),
            "commit": commit_sha,
            "elapsed_seconds": total_elapsed,
            "model_tool_calls": model_tools,
            "harness_artifact_recoveries": artifact_recoveries,
            "summary_characters": len(str(result["summary"])),
            "requests": wire_requests,
            "temporary_workspace_clean": True,
            **validation,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("SCIDEV_API_BASE", "http://127.0.0.1:11434/v1"),
        help="loopback Ollama OpenAI-compatible API base URL (no remote hosts)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("SCIDEV_MODEL", DEFAULT_MODEL),
        help="an already-installed Ollama model tag; no model is downloaded",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        help="where to save the generated SVG and PNG preview (defaults to a new temp folder)",
    )
    parser.add_argument("--api-key", default=os.getenv("SCIDEV_API_KEY") or "ollama", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        result = run_smoke(args.base_url, args.model, args.api_key, args.artifact_dir)
    except Exception as exc:  # noqa: BLE001 - report one actionable local SVG smoke error.
        print(f"Pelican SVG smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
