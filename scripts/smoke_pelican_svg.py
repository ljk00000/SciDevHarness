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
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
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
    "model",
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
SMOKE_BACKGROUND = QColor("#f4f7fb")

_QT_GUI_APPLICATION: QGuiApplication | None = None


def _ensure_qt_gui_application() -> QGuiApplication:
    """Keep standalone SVG rendering on Qt's supported GUI application path."""
    global _QT_GUI_APPLICATION
    application = QGuiApplication.instance()
    if application is not None:
        return application
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _QT_GUI_APPLICATION = QGuiApplication(["SciDevHarness SVG validator"])
    return _QT_GUI_APPLICATION


def smoke_session_id(attempt: int) -> str:
    """Give each repair a new Coding Session instead of resuming a completed one."""
    if attempt < 0:
        raise ValueError("smoke attempt cannot be negative")
    suffix = "" if attempt == 0 else f"_attempt_{attempt}"
    return f"{SMOKE_SESSION_PREFIX}{suffix}"


def fixed_prompt_task(session_id: str) -> dict[str, Any]:
    """Build the only user task allowed by this benchmark."""
    return {"payload": {"session_id": session_id, "prompt": PELICAN_PROMPT}}


def pelican_repair_prompt(filename: str, failure_reason: str) -> str:
    """Choose a repair scope that matches the reported failure, minimizing tokens and rewrites."""
    missing_match = re.search(r"missing=\[([^\]]*)\]", failure_reason)
    missing_parts = re.findall(r"'([^']+)'", missing_match.group(1)) if missing_match else []
    malformed_match = re.search(r"malformed=\[([^\]]*)\]", failure_reason)
    malformed_parts = re.findall(r"'([^']+)'", malformed_match.group(1)) if malformed_match else []
    duplicate_match = re.search(r"duplicate_ids=\[([^\]]*)\]", failure_reason)
    duplicate_ids = re.findall(r"'([^']+)'", duplicate_match.group(1)) if duplicate_match else []
    target = Path(filename).name
    local_paint_or_layout_only = (
        not missing_parts
        and not duplicate_ids
        and bool(malformed_parts)
        and all(
            item.startswith("invalid paint ")
            or "must be an unfilled, visibly outlined tire" in item
            or item.endswith(" is clipped by the viewBox")
            for item in malformed_parts
        )
    )
    if local_paint_or_layout_only:
        repair_items = []
        if any(item.startswith("invalid paint ") for item in malformed_parts):
            repair_items.append(
                "Replace each unsupported paint value with a valid hex color; use teal `#168b83`, "
                "ochre `#e4b35b`, coral `#e9785b`, or navy `#18324b` as appropriate."
        )
        if any("must be an unfilled, visibly outlined tire" in item for item in malformed_parts):
            repair_items.append(
                "In each `<g id=\"left-wheel\">` and `<g id=\"right-wheel\">`, "
                "edit only its child circle: preserve `cx`, `cy`, and `r`, set `fill=\"none\"`, "
                "and add `stroke=\"#18324b\" stroke-width=\"8\"`."
            )
        clipped = [item.removesuffix(" is clipped by the viewBox") for item in malformed_parts if item.endswith(" is clipped by the viewBox")]
        if clipped:
            repair_items.append(
                "Expand the viewBox just enough to contain these clipped shapes with a clear margin; "
                "preserve their coordinates and connections, and do not translate unrelated shapes: "
                + ", ".join(clipped)
                + "."
            )
        guidance = "\n".join(f"- {item}" for item in repair_items)
        return f"""Repair only these localized SVG issues in {target}; preserve the existing drawing and all passing geometry. Validator: {failure_reason}
{guidance}
Read the file first. Then call `replace_in_file` with one `edits` array; each item must use the tool's exact `old_text` and `new_text` keys and match one original circle tag. Call `git_diff` after the edit. Do not print a Markdown diff or ask for confirmation instead of invoking the tools. Do not redraw the whole SVG or use shell."""
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
        "pelican-leg-near": "Identify a distinct leg visibly joining the lower body to one pedal.",
        "pelican-leg-far": "Identify a leg visibly joining the body to the pedal.",
    }
    if (
        len(missing_parts) >= 7
        and not malformed_parts
        and not duplicate_ids
        and "viewBox dimensions are adequate" in failure_reason
    ):
        ids = ", ".join(f"`{part}`" for part in missing_parts)
        return f"""The first SVG preflight found missing semantic IDs only; detailed geometry checks run after IDs are present. Repair only {target} using a targeted label-only edit for these already-visible matching shapes: {ids}.
Read the file first. Preserve the existing drawing, coordinates, viewBox, colors, and layer order. Add each ID only to its matching visible shape; never label an unrelated shape. Use one `replace_in_file` call with an `edits` array; every item must use `old_text` and `new_text`, and `old_text` must be a complete unique SVG element tag copied exactly from the file. Do not use generic fragments such as `<circle>` or `fill=`. Do not add, move, resize, or redraw artwork in this pass. Do not output a full SVG or Markdown diff, and do not claim geometry is verified."""
    if missing_parts and len(missing_parts) <= 6 and not malformed_parts and not duplicate_ids:
        guidance_lines = []
        for part in missing_parts:
            detail = missing_guidance.get(part, f"Add one visible shape with exact ID `{part}`.")
            guidance_lines.append(f"- {detail}")
        guidance = "\n".join(guidance_lines)
        return f"""Repair only the missing SVG parts in {target}; preserve the existing drawing. Validator: {failure_reason}
Missing-part instructions:
{guidance}
Read the file. If a correct visible shape exists, add its missing ID; otherwise insert only the missing shape(s) before `</svg>`. Keep IDs unique, use existing geometry/color style, and preserve every passing element. Make minimal `replace_in_file` edit(s), then inspect `git_diff`; do not rewrite the full SVG or use shell."""
    geometry_guidance = {
        "left-wheel": "Keep the rear wheel circular, unfilled, and centered on the rear hub.",
        "right-wheel": "Keep the front wheel circular, unfilled, and centered on the front hub.",
        "bicycle-frame": "Reshape the connected frame so its rear endpoint meets the rear hub and one vertex meets the pedal/crank center.",
        "bicycle-fork": "Move the fork so one endpoint touches the front wheel hub and the other joins the front frame/head tube.",
        "bicycle-spokes": "Add at least four radial spoke segments inside each wheel; keep them centered on the hubs.",
        "bicycle-saddle": "Use a compact, clearly visible saddle directly above the frame's seat joint; keep it small relative to a wheel.",
        "bicycle-handlebar": "Use a compact grip ahead of the saddle, not a wheel-sized circle; connect it to the reaching wing tip.",
        "bicycle-pedals": "Keep the crank at the frame vertex and show two separate small pedal circles at distinct positions.",
        "pelican-head": "Keep a distinct filled head touching the front of the torso, with enough room for the eye and bill.",
        "pelican-eye": "Keep one small dark eye inside the head.",
        "pelican-beak": "Use a tapered polygon; lengthen the bill from the head so it projects forward at least three head radii, not a tiny triangle.",
        "pelican-body": "Use a compact organic torso behind and overlapping the head; keep its width below roughly half the wheel spacing and leave the wing as a separate feature.",
        "pelican-wing": "Place the filled curved wing mostly inside the torso, not as a second body-sized silhouette.",
        "pelican-pouch": "Make a small closed, filled curved throat pouch beneath the long bill; keep it narrower than three head radii.",
        "pelican-wing-reaching": "Draw a closed filled curve from the shoulder to the handlebar, ending at the actual grip while overlapping the torso at its base.",
        "pelican-leg-near": "Connect a distinct near leg continuously from the lower torso to one pedal circle.",
        "pelican-leg-far": "Connect the far leg continuously from the lower torso to the other pedal circle.",
    }
    targeted_geometry: dict[str, str] = {}
    unmatched_geometry = []
    for issue in malformed_parts:
        normalized_issue = issue.replace("bicycle frame", "bicycle-frame")
        part = next(
            (identifier for identifier in sorted(geometry_guidance, key=len, reverse=True) if identifier in normalized_issue),
            None,
        )
        if part is None:
            unmatched_geometry.append(issue)
        else:
            targeted_geometry[part] = geometry_guidance[part]
    complex_structural_failure = len(missing_parts) >= 4 and len(malformed_parts) >= 3
    if (
        (malformed_parts or missing_parts)
        and len(missing_parts) <= 6
        and not duplicate_ids
        and not unmatched_geometry
        and not complex_structural_failure
    ):
        edits = []
        for identifier in missing_parts:
            instruction = missing_guidance.get(identifier, f"add a visible shape with this exact ID: {identifier}")
            edits.append(f"- Add missing `{identifier}`: {instruction}")
        edits.extend(f"- {identifier}: {instruction}" for identifier, instruction in targeted_geometry.items())
        edit_instructions = "\n".join(edits)
        return f"""Make a targeted localized repair in {target} using only exact `replace_in_file` edits. Preserve all passing artwork, IDs, classes, and layer order. Validator: {failure_reason}
Targeted shape edits:
{edit_instructions}
Read the existing SVG first. When an ID is on a `<g>`, edit only its child shape and keep the wrapper. Batch independent changes in one `edits` array with the tool's `old_text`/`new_text` fields, then inspect `git_diff`; do not rewrite the file or use shell."""
    if (
        len(missing_parts) >= 7
        or complex_structural_failure
        or (len(malformed_parts) >= 3 and (duplicate_ids or unmatched_geometry))
    ):
        return f"""Repair the existing SVG in {target} once. Replace it once with a complete, polished pelican riding a bicycle; preserve valid artwork and layer order. Validator: {failure_reason}
Use `viewBox="0 0 640 420"`; palette navy `#18324b`, teal `#168b83`, ochre `#e4b35b`, coral `#e9785b` on `#f4f7fb`. Put unfilled wheels (130,325)/(500,325), radius 60. A triangular frame connects hubs, crank (390,265), seat (275,230), and head joint (452,205); saddle/grip sit on their joints. Torso near x=275..395/y=135..210 rests on saddle; head (370,140), radius 20; long tapered bill projects right 60+ units, pouch hangs below. Two distinct legs reach different pedal circles; reaching wing touches grip. Add radial spokes to both wheels and a fork to the front hub; keep shapes in bounds.
Use each ID once: left-wheel, right-wheel, bicycle-frame, bicycle-fork, bicycle-spokes, bicycle-saddle, bicycle-handlebar, bicycle-pedals, pelican-body, pelican-head, pelican-eye, pelican-wing, pelican-wing-reaching, pelican-beak, pelican-pouch, pelican-leg-near, pelican-leg-far. Wheels are `<circle>`; frame is `<polyline>`; spokes are one `<g>`; pedals are one `<g>` with two circles; beak is `<polygon>`; body is a curved `<path>` or `<ellipse>`; head/eye are circles; pouch is a closed path. Call `write_file` once and inspect `git_diff`; no shell."""
    if duplicate_ids:
        return f"""Repair duplicate SVG IDs in {target} only. Validator: {failure_reason}
For each listed duplicate, preserve the ID on the correct primary shape and rename redundant copies to unique descriptive IDs; for `bicycle-spokes`, prefer one `<g id="bicycle-spokes">` around its spoke lines. Read the file, make the smallest exact `replace_in_file` edit(s), then inspect `git_diff`. Preserve all other artwork; do not use shell."""
    if "left-wheel" in missing_parts or "right-wheel" in missing_parts or "must be a circle" in failure_reason:
        return f"""Repair only the localized wheel error in {target}. Validator: {failure_reason}
Read the file, preserve all correct artwork, and use exact `replace_in_file` edits only. Replace both defective/prefixed wheel elements with these exact circles, then inspect `git_diff`; do not rewrite the whole SVG or use shell:
`<circle id="left-wheel" cx="125" cy="326" r="72" fill="none" stroke="#18324B" stroke-width="8"/>`
`<circle id="right-wheel" cx="515" cy="326" r="72" fill="none" stroke="#18324B" stroke-width="8"/>`"""
    if "not well-formed xml" in failure_reason.casefold():
        return f"""Repair only this localized SVG XML syntax error in {target}. Validator: {failure_reason}
Read the indicated lines and preserve the drawing. Fix only the mismatched or malformed tag with one `replace_in_file` call; use the exact complete current tag as unique `old_text` and a properly closed `new_text`. Never set `replace_all`, rewrite the whole SVG, or return a code block instead of invoking the tool. Inspect `git_diff` after the edit."""
    if "must define a four-number viewbox" in failure_reason.casefold():
        return f"""Repair only this localized missing root `viewBox` in {target}. Validator: {failure_reason}
Read the current root `<svg ...>` opening tag. Preserve its existing dimensions, namespace, and all other attributes; add `viewBox="0 0 WIDTH HEIGHT"` using the positive numeric width and height already present in that tag. Make one exact `replace_in_file` call with the full opening tag as `old_text` and the minimally updated tag as `new_text`, then inspect `git_diff`. Do not output an SVG/code block or claim a save instead of calling the tool."""
    if "viewbox is too small" in failure_reason.casefold():
        return f"""Repair only the localized root `viewBox` issue in {target}. Validator: {failure_reason}
Read the current SVG. Preserve the drawing; adjust the root viewBox to fully contain it with dimensions at least 300x180, scaling the artwork uniformly if required. Use one precise `replace_in_file`, inspect `git_diff`, and do not use shell."""
    return f"""Repair only the reported issue in {target}. Validator: {failure_reason}
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
    """Track only successful initial writes and targeted replacements of SVG artifacts."""
    pending: dict[tuple[str, str], list[dict[str, Any]]] = {}
    successful: list[dict[str, Any]] = []
    for event_name, data in events:
        name = str(data.get("name", ""))
        session_id = str(data.get("session_id", ""))
        key = (session_id, name)
        if (
            event_name == "tool_started"
            and data.get("source") != "harness_precommit"
            and name in {"write_file", "replace_in_file"}
            and isinstance(data.get("arguments"), dict)
            and str(data["arguments"].get("path", "")).casefold().endswith(".svg")
        ):
            pending.setdefault(key, []).append(data)
        elif event_name == "tool_result" and pending.get(key):
            call = pending[key].pop(0)
            result = str(data.get("result", ""))
            if data.get("success") is not False and not result.startswith("工具执行失败："):
                successful.append(call)
    return successful


def repair_guidance_for_failed_edit(events: list[tuple[str, dict[str, Any]]], session_id: str) -> str:
    """Return bounded, actionable guidance only for a failed exact SVG replacement."""
    for event_name, data in reversed(events):
        if (
            event_name != "tool_result"
            or str(data.get("session_id", "")) != session_id
            or data.get("name") != "replace_in_file"
        ):
            continue
        result = str(data.get("result", ""))
        if "old_text matched" in result:
            return (
                "The previous `replace_in_file` call failed because `old_text` was not unique. "
                "Reread the file and use the complete exact SVG element tag as `old_text`; do not "
                "use a generic fragment or set `replace_all`."
            )
        if "Exact old_text not found" in result:
            return (
                "The previous `replace_in_file` call failed because `old_text` did not match. "
                "Reread the file and copy the complete current SVG element exactly before retrying."
            )
    return ""


def repair_guidance_after_read_without_edit(
    events: list[tuple[str, dict[str, Any]]], session_id: str
) -> str:
    """Give one bounded follow-up when a repair turn read the SVG but made no edit."""
    session_events = [
        (name, data)
        for name, data in events
        if str(data.get("session_id", "")) == session_id
    ]
    if not any(name == "tool_result" and data.get("name") == "read_file" for name, data in session_events):
        return ""
    if any(str(call.get("session_id", "")) == session_id for call in collect_svg_mutations(session_events)):
        return ""
    return (
        "Your last turn only read the SVG and made no edit. Now carry out the requested repair above "
        "with one `replace_in_file` call, using the tool's exact `path`, `old_text`, and `new_text` "
        "fields copied from the current file. Every `old_text` must be unique; never set `replace_all`. "
        "Do not repeat the file contents or claim success; "
        "inspect `git_diff` only after an actual edit."
    )


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


def _svg_tag_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].casefold() if isinstance(element.tag, str) else ""


def _svg_presentation_value(
    element: ET.Element,
    property_name: str,
    parents: dict[ET.Element, ET.Element],
    css_rules: list[tuple[str, dict[str, str]]],
) -> str:
    """Resolve an inline SVG presentation property through simple group inheritance."""
    current: ET.Element | None = element
    while current is not None:
        style_values: dict[str, str] = {}
        for declaration in current.attrib.get("style", "").split(";"):
            name, separator, value = declaration.partition(":")
            if separator:
                style_values[name.strip().casefold()] = value.strip().removesuffix("!important").strip()
        value = style_values.get(property_name.casefold(), "").strip()
        if not value:
            class_names = set(current.attrib.get("class", "").split())
            element_id = current.attrib.get("id", "")
            tag_name = _svg_tag_name(current)
            for selector, declarations in css_rules:
                selector = selector.strip()
                if selector.startswith(".") and selector[1:] in class_names:
                    value = declarations.get(property_name.casefold(), value)
                elif selector.startswith("#") and selector[1:] == element_id:
                    value = declarations.get(property_name.casefold(), value)
                elif selector.casefold() == tag_name:
                    value = declarations.get(property_name.casefold(), value)
        if not value:
            value = current.attrib.get(property_name, "").strip()
        if value and value.casefold() != "inherit":
            return value
        current = parents.get(current)
    return ""


def _resolve_svg_semantic_shape(element: ET.Element, allowed_tags: set[str]) -> ET.Element | None:
    """Accept a semantic ID on its actual shape or on a legal SVG group wrapper."""
    if _svg_tag_name(element) in allowed_tags:
        return element
    if _svg_tag_name(element) not in {"g", "a", "svg"}:
        return None
    return next(
        (child for child in element.iter() if child is not element and _svg_tag_name(child) in allowed_tags),
        None,
    )


def _svg_css_rules(root: ET.Element) -> list[tuple[str, dict[str, str]]]:
    """Read only simple local class/id/tag rules; external or active CSS is rejected elsewhere."""
    rules: list[tuple[str, dict[str, str]]] = []
    for element in root.iter():
        if _svg_tag_name(element) != "style":
            continue
        css = re.sub(r"/\*.*?\*/", "", element.text or "", flags=re.S)
        for selector_list, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            declarations: dict[str, str] = {}
            for declaration in body.split(";"):
                name, separator, value = declaration.partition(":")
                if separator:
                    declarations[name.strip().casefold()] = value.strip().removesuffix("!important").strip()
            for selector in selector_list.split(","):
                normalized = selector.strip()
                if re.fullmatch(r"(?:\.[A-Za-z_][\w-]*|#[A-Za-z_][\w-]*|[A-Za-z_][\w-]*)", normalized):
                    rules.append((normalized, declarations))
    return rules


def _is_valid_svg_paint(value: str) -> bool:
    paint = value.strip()
    if paint.casefold() in {"none", "currentcolor", "inherit", "context-fill", "context-stroke"}:
        return True
    if re.fullmatch(r"url\(\s*['\"]?#[-A-Za-z0-9_.]+['\"]?\s*\)", paint):
        return True
    return QColor(paint).isValid()


def _paint_is_visible_against_canvas(value: str) -> bool:
    """Reject fills that disappear into the smoke preview's fixed canvas color."""
    color = QColor(value)
    if not color.isValid():
        return False
    alpha = color.alphaF()
    effective = (
        round(color.red() * alpha + SMOKE_BACKGROUND.red() * (1 - alpha)),
        round(color.green() * alpha + SMOKE_BACKGROUND.green() * (1 - alpha)),
        round(color.blue() * alpha + SMOKE_BACKGROUND.blue() * (1 - alpha)),
    )
    delta = math.sqrt(
        sum((channel - background) ** 2 for channel, background in zip(effective, SMOKE_BACKGROUND.getRgb()[:3]))
    )
    return delta >= 24


_SVG_PATH_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?")


def _svg_path_endpoints(path_data: str) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Return the first and final points for the common SVG path commands."""
    tokens = _SVG_PATH_TOKEN.findall(path_data)
    current = (0.0, 0.0)
    subpath_start = current
    first: tuple[float, float] | None = None
    command = ""
    index = 0
    arity = {"M": 2, "L": 2, "T": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "A": 7}
    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command.upper() == "Z":
                current = subpath_start
                continue
        if not command or command.upper() not in arity:
            return None
        upper = command.upper()
        relative = command.islower()
        size = arity[upper]
        if index + size > len(tokens) or any(token.isalpha() for token in tokens[index:index + size]):
            return None
        values = [float(token) for token in tokens[index:index + size]]
        index += size
        if upper in {"M", "L", "T"}:
            point = (values[-2], values[-1])
        elif upper == "H":
            point = (values[0], current[1])
        elif upper == "V":
            point = (current[0], values[0])
        elif upper == "A":
            point = (values[5], values[6])
        else:
            point = (values[-2], values[-1])
        if relative:
            point = (current[0] + point[0], current[1] + point[1])
        current = point
        if upper == "M":
            subpath_start = point
            command = "l" if relative else "L"
        if first is None:
            first = point
    if first is None:
        return None
    return first, current


def _pedal_circles(element: ET.Element | None) -> list[tuple[float, float, float]]:
    """Read visible circle/ellipse pedal centers from the semantic group."""
    pedals: list[tuple[float, float, float]] = []
    if element is None:
        return pedals
    for shape in element.iter():
        tag = _svg_tag_name(shape)
        if tag not in {"circle", "ellipse"}:
            continue
        try:
            x = float(shape.attrib.get("cx", "0"))
            y = float(shape.attrib.get("cy", "0"))
            rx = float(shape.attrib.get("r", shape.attrib.get("rx", "0")))
            ry = float(shape.attrib.get("r", shape.attrib.get("ry", "0")))
        except ValueError:
            continue
        if rx > 0 and ry > 0:
            pedals.append((x, y, max(rx, ry)))
    return pedals


def _collect_rider_pose_issues(
    identified_elements: dict[str, ET.Element],
    bounds: dict[str, QRectF],
) -> list[str]:
    """Validate the rider's seat, leg, and distinct pedal relationships."""
    issues: list[str] = []
    body = bounds.get("pelican-body")
    saddle = bounds.get("bicycle-saddle")
    left = bounds.get("left-wheel")
    right = bounds.get("right-wheel")
    wheel_radius = min(left.width(), left.height(), right.width(), right.height()) / 2 if left and right else 0.0
    tolerance = max(3.0, wheel_radius * 0.2)
    if body and saddle and not body.adjusted(-tolerance, -tolerance, tolerance, tolerance).intersects(saddle):
        issues.append("pelican-body must rest on or closely touch the bicycle-saddle")

    pedal_positions = _pedal_circles(identified_elements.get("bicycle-pedals"))
    distinct_pedals: list[tuple[float, float, float]] = []
    for pedal in pedal_positions:
        if all(
            math.hypot(pedal[0] - other[0], pedal[1] - other[1]) >= max(3.0, wheel_radius * 0.2)
            for other in distinct_pedals
        ):
            distinct_pedals.append(pedal)
    pedals_element = identified_elements.get("bicycle-pedals")
    if pedals_element is not None and len(distinct_pedals) < 2:
        issues.append("bicycle-pedals must contain two distinct visible pedal circles")

    legs = [identified_elements.get("pelican-leg-near"), identified_elements.get("pelican-leg-far")]
    leg_bounds = [bounds.get("pelican-leg-near"), bounds.get("pelican-leg-far")]
    leg_ends: list[tuple[float, float]] = []
    for leg, rect, label in zip(legs, leg_bounds, ("near", "far")):
        if leg is None or rect is None:
            continue
        endpoints = _svg_path_endpoints(leg.attrib.get("d", "")) if _svg_tag_name(leg) == "path" else None
        if endpoints is None:
            issues.append(f"pelican-leg-{label} must be a path with a clear body-to-pedal endpoint")
            continue
        start, end = endpoints
        leg_ends.append(end)
        if body and not body.adjusted(-tolerance, -tolerance, tolerance, tolerance).contains(*start):
            issues.append(f"pelican-leg-{label} must visibly begin at the pelican body")
        if len(distinct_pedals) >= 2:
            distances = [math.hypot(end[0] - x, end[1] - y) for x, y, _ in distinct_pedals]
            if min(distances) > tolerance + min(radius for _, _, radius in distinct_pedals):
                issues.append(f"pelican-leg-{label} must visibly reach a pedal position")
    if len(leg_ends) == 2 and len(distinct_pedals) >= 2:
        direct = (
            math.dist(leg_ends[0], distinct_pedals[0][:2]),
            math.dist(leg_ends[1], distinct_pedals[1][:2]),
        )
        crossed = (
            math.dist(leg_ends[0], distinct_pedals[1][:2]),
            math.dist(leg_ends[1], distinct_pedals[0][:2]),
        )
        matching = min((direct, crossed), key=sum)
        if any(distance > tolerance + distinct_pedals[index][2] for index, distance in enumerate(matching)):
            issues.append("pelican-leg-near and pelican-leg-far must reach separate pedal positions")
        if math.dist(leg_ends[0], leg_ends[1]) < max(3.0, wheel_radius * 0.15):
            issues.append("pelican-leg-near and pelican-leg-far must be visibly distinct")

    frame = identified_elements.get("bicycle-frame")
    if frame is not None and saddle is not None and _svg_tag_name(frame) == "polyline":
        numbers = [float(value) for value in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)", frame.attrib.get("points", ""))]
        points = list(zip(numbers[::2], numbers[1::2]))
        seat_joint = saddle.center()
        seat_tolerance = max(3.0, wheel_radius * 0.35)
        if not any(math.hypot(x - seat_joint.x(), y - seat_joint.y()) <= seat_tolerance for x, y in points):
            issues.append("bicycle-frame must meet the seat joint directly beneath the saddle")
    return issues


def _collect_illustration_geometry_issues(
    semantic_elements: dict[str, ET.Element],
    identified_elements: dict[str, ET.Element],
    renderer: QSvgRenderer,
    parents: dict[ET.Element, ET.Element],
    css_rules: list[tuple[str, dict[str, str]]],
) -> list[str]:
    """Collect independent geometry defects together so one repair can address them."""
    issues: list[str] = []
    bounds = {identifier: renderer.boundsOnElement(identifier) for identifier in identified_elements}
    wheels: list[tuple[float, float, float]] = []
    for wheel_id in ("left-wheel", "right-wheel"):
        rect = bounds[wheel_id]
        if rect.width() <= 0 or rect.height() <= 0 or abs(rect.width() - rect.height()) > max(
            2.0, max(rect.width(), rect.height()) * 0.08
        ):
            issues.append(f"SVG {wheel_id} must render as a circle")
        wheels.append((rect.center().x(), rect.center().y(), min(rect.width(), rect.height()) / 2))
    (left_x, left_y, left_radius), (right_x, right_y, right_radius) = wheels
    if left_radius <= 0 or right_radius <= 0:
        issues.append("SVG bicycle wheels must have positive radii")
    wheel_distance = math.hypot(right_x - left_x, right_y - left_y)
    if left_radius > 0 and right_radius > 0:
        if wheel_distance < 0.9 * (left_radius + right_radius):
            issues.append("SVG bicycle wheels overlap instead of forming a readable bicycle")
        if abs(right_y - left_y) > 0.35 * (left_radius + right_radius):
            issues.append("SVG bicycle wheels are not aligned at a readable height")

    head = semantic_elements["pelican-head"]
    head_bounds = bounds["pelican-head"]
    head_size = min(head_bounds.width(), head_bounds.height()) / 2
    if head_size < 8:
        issues.append("pelican-head is too small to read as a distinct bird head")
    head_fill = _svg_presentation_value(head, "fill", parents, css_rules)
    head_stroke = _svg_presentation_value(head, "stroke", parents, css_rules)
    if (
        head_fill.casefold() in {"", "none", "transparent"}
        or not _paint_is_visible_against_canvas(head_fill)
        and not _paint_is_visible_against_canvas(head_stroke)
    ):
        issues.append("pelican-head needs a filled silhouette that contrasts with the canvas")

    eye = semantic_elements["pelican-eye"]
    eye_bounds = bounds["pelican-eye"]
    if min(eye_bounds.width(), eye_bounds.height()) < 4:
        issues.append("pelican-eye must be a visible small circle, not omitted decoration")
    eye_fill = _svg_presentation_value(eye, "fill", parents, css_rules).casefold() or "#000000"
    if eye_fill in {"none", "transparent", "white", "#fff", "#ffffff"}:
        issues.append("pelican-eye must use a visible dark fill")

    beak = semantic_elements["pelican-beak"]
    beak_numbers = [
        float(value)
        for value in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)", beak.attrib.get("points", ""))
    ]
    if len(beak_numbers) < 6 or len(beak_numbers) % 2:
        issues.append("pelican-beak polygon has too few valid points")
    elif max(beak_numbers[::2]) - min(beak_numbers[::2]) < max(60, head_size * 2.5):
        issues.append("pelican-beak is too short relative to the head to read as a long pelican bill")

    body = semantic_elements["pelican-body"]
    body_tag = _svg_tag_name(body)
    if body_tag == "path" and not re.search(r"[cqst]", body.attrib.get("d", ""), re.I):
        issues.append("pelican-body path must use curved geometry rather than a rectangle-like polygon")
    body_fill = _svg_presentation_value(body, "fill", parents, css_rules)
    body_stroke = _svg_presentation_value(body, "stroke", parents, css_rules)
    if not _paint_is_visible_against_canvas(body_fill) and not _paint_is_visible_against_canvas(body_stroke):
        issues.append("pelican-body has too little contrast against the blank canvas")
    body_bounds = bounds["pelican-body"]
    if min(body_bounds.width(), body_bounds.height(), head_bounds.width(), head_bounds.height()) <= 0:
        issues.append("pelican body/head must have non-empty rendered silhouettes")
    elif wheel_distance > 0:
        if body_bounds.height() < wheel_distance * 0.19:
            issues.append("pelican-body is too flat to read as a bird torso")
        if body_bounds.width() > wheel_distance * 0.58:
            issues.append("pelican-body is too wide and overwhelms the bicycle")
        if body_bounds.center().x() >= head_bounds.center().x() + head_size * 0.25:
            issues.append("pelican-body must sit behind and connect visually to the forward-facing head")
    vertical_gap = max(
        0.0,
        max(body_bounds.top(), head_bounds.top()) - min(body_bounds.bottom(), head_bounds.bottom()),
    )
    if vertical_gap > head_size * 0.25:
        issues.append("pelican-head is detached from the body instead of forming one rider silhouette")

    wing = semantic_elements["pelican-wing"]
    wing_fill = _svg_presentation_value(wing, "fill", parents, css_rules)
    wing_stroke = _svg_presentation_value(wing, "stroke", parents, css_rules)
    if (
        wing_fill.casefold() in {"", "none", "transparent"}
        or not _paint_is_visible_against_canvas(wing_fill)
        and not _paint_is_visible_against_canvas(wing_stroke)
    ):
        issues.append("pelican-wing must have a visible fill or outline distinct from the canvas")
    wing_bounds = bounds["pelican-wing"]
    wing_area = max(1.0, wing_bounds.width() * wing_bounds.height())
    wing_overlap = body_bounds.intersected(wing_bounds)
    if wing_overlap.width() * wing_overlap.height() < wing_area * 0.5:
        issues.append("pelican-wing must sit mostly inside the body silhouette")

    pouch = semantic_elements["pelican-pouch"]
    pouch_path = pouch.attrib.get("d", "")
    if not re.search(r"[cqst]", pouch_path, re.I) or not re.search(r"z\s*$", pouch_path, re.I):
        issues.append("pelican-pouch must be a closed curved shape")
    pouch_fill = _svg_presentation_value(pouch, "fill", parents, css_rules)
    pouch_stroke = _svg_presentation_value(pouch, "stroke", parents, css_rules)
    if (
        pouch_fill.casefold() in {"", "none", "transparent"}
        or not _paint_is_visible_against_canvas(pouch_fill)
        and not _paint_is_visible_against_canvas(pouch_stroke)
    ):
        issues.append("pelican-pouch must have a visible fill or outline distinct from the canvas")
    pouch_bounds = bounds["pelican-pouch"]
    if pouch_bounds.width() > head_size * 3.0 or pouch_bounds.top() <= head_bounds.center().y():
        issues.append("pelican-pouch must be a compact throat shape hanging below the head/bill")

    saddle_bounds = bounds["bicycle-saddle"]
    handlebar_bounds = bounds["bicycle-handlebar"]
    pedal_bounds = bounds["bicycle-pedals"]
    near_leg_bounds = bounds["pelican-leg-near"]
    far_leg_bounds = bounds["pelican-leg-far"]
    rider_parts = {
        "bicycle-saddle": saddle_bounds,
        "bicycle-handlebar": handlebar_bounds,
        "bicycle-pedals": pedal_bounds,
        "pelican-leg-near": near_leg_bounds,
        "pelican-leg-far": far_leg_bounds,
    }
    for part_id, rect in rider_parts.items():
        if rect.width() <= 0 or rect.height() <= 0:
            issues.append(f"{part_id} must be a visible non-empty shape")
    if handlebar_bounds.center().x() <= saddle_bounds.center().x():
        issues.append("bicycle-handlebar must be ahead of the saddle")

    reaching_wing = semantic_elements["pelican-wing-reaching"]
    reaching_wing_path = reaching_wing.attrib.get("d", "")
    reaching_wing_fill = _svg_presentation_value(reaching_wing, "fill", parents, css_rules)
    reaching_wing_stroke = _svg_presentation_value(reaching_wing, "stroke", parents, css_rules)
    svg_number = r"([-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?)"
    wing_tip_match = re.match(
        rf"\s*M\s*{svg_number}[\s,]+{svg_number}\s+C\s*"
        rf"{svg_number}[\s,]+{svg_number}[\s,]+{svg_number}[\s,]+{svg_number}[\s,]+{svg_number}[\s,]+{svg_number}",
        reaching_wing_path,
        re.I,
    )
    wing_tip = (float(wing_tip_match.group(7)), float(wing_tip_match.group(8))) if wing_tip_match else None
    reaching_wing_bounds = bounds["pelican-wing-reaching"]
    handlebar_contact_area = handlebar_bounds.adjusted(-2, -2, 2, 2)
    if (
        _svg_tag_name(reaching_wing) != "path"
        or not re.search(r"[cq]", reaching_wing_path, re.I)
        or not re.search(r"z\s*$", reaching_wing_path, re.I)
        or reaching_wing_fill.casefold() in {"", "none", "transparent"}
        or not _paint_is_visible_against_canvas(reaching_wing_fill)
        and not _paint_is_visible_against_canvas(reaching_wing_stroke)
        or wing_tip is None
        or not handlebar_contact_area.contains(*wing_tip)
        or reaching_wing_bounds.intersected(body_bounds).isEmpty()
    ):
        issues.append(
            "pelican-wing-reaching must be a closed filled curve that starts at the shoulder "
            "and visibly reaches the handlebar"
        )

    frame = semantic_elements["bicycle-frame"]
    frame_stroke = _svg_presentation_value(frame, "stroke", parents, css_rules)
    if not frame_stroke or frame_stroke.casefold() in {"none", "transparent"}:
        issues.append("SVG bicycle frame needs a visible stroke")
    frame_numbers = [
        float(value)
        for value in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)", frame.attrib.get("points", ""))
    ]
    frame_points = list(zip(frame_numbers[::2], frame_numbers[1::2]))
    if len(frame_numbers) < 6 or len(frame_numbers) % 2:
        issues.append("SVG bicycle frame polyline has too few valid points")
    elif left_radius > 0 and right_radius > 0:
        hub_tolerance = max(1.0, min(left_radius, right_radius) * 0.25)
        if not any(math.hypot(x - left_x, y - left_y) <= hub_tolerance for x, y in frame_points):
            issues.append("SVG bicycle frame must meet the rear wheel hub")
        if not any(
            min(left_x, right_x) < x < max(left_x, right_x)
            and abs(y - (left_y + right_y) / 2) > min(left_radius, right_radius) * 0.5
            for x, y in frame_points
        ):
            issues.append("SVG bicycle frame has no readable triangular center vertex")
        crank_bounds = pedal_bounds
        crank_x, crank_y = crank_bounds.center().x(), crank_bounds.center().y()
        if not any(math.hypot(x - crank_x, y - crank_y) <= hub_tolerance for x, y in frame_points):
            issues.append("SVG bicycle frame must meet the visible crank/pedals")

        fork_bounds = bounds["bicycle-fork"]
        point_to_rect_x = max(fork_bounds.left() - right_x, 0.0, right_x - fork_bounds.right())
        point_to_rect_y = max(fork_bounds.top() - right_y, 0.0, right_y - fork_bounds.bottom())
        if math.hypot(point_to_rect_x, point_to_rect_y) > hub_tolerance:
            issues.append("bicycle-fork must visibly connect the front frame to the front wheel hub")
        if fork_bounds.intersected(bounds["bicycle-frame"].adjusted(-hub_tolerance, -hub_tolerance, hub_tolerance, hub_tolerance)).isEmpty():
            issues.append("bicycle-fork must join the front frame near the head tube")

    spokes = semantic_elements["bicycle-spokes"]
    spoke_segments = 0
    for element in spokes.iter():
        spoke_tag = _svg_tag_name(element)
        if spoke_tag == "path":
            spoke_segments += len(re.findall(r"[Mm]", element.attrib.get("d", "")))
        elif spoke_tag == "line":
            spoke_segments += 1
        elif spoke_tag == "polyline":
            coordinates = re.findall(
                r"[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?",
                element.attrib.get("points", ""),
            )
            spoke_segments += max(0, len(coordinates) // 2 - 1)
    if _svg_tag_name(spokes) != "g" or spoke_segments < 8:
        issues.append("bicycle-spokes must contain visible radial spoke lines for both wheels")
    issues.extend(_collect_rider_pose_issues(identified_elements, bounds))
    return issues


def _collect_partial_illustration_geometry_issues(
    identified_elements: dict[str, ET.Element],
    renderer: QSvgRenderer,
    parents: dict[ET.Element, ET.Element],
    css_rules: list[tuple[str, dict[str, str]]],
) -> list[str]:
    """Check visible core relationships even while other semantic IDs are missing."""
    issues: list[str] = []
    bounds = {
        identifier: renderer.boundsOnElement(identifier)
        for identifier in identified_elements
    }
    wheel_ids = ("left-wheel", "right-wheel")
    if all(identifier in bounds for identifier in wheel_ids):
        left, right = (bounds[identifier] for identifier in wheel_ids)
        left_radius = min(left.width(), left.height()) / 2
        right_radius = min(right.width(), right.height()) / 2
        wheel_distance = math.hypot(right.center().x() - left.center().x(), right.center().y() - left.center().y())
        if left_radius > 0 and right_radius > 0:
            if wheel_distance < 0.9 * (left_radius + right_radius):
                issues.append("SVG bicycle wheels overlap instead of forming a readable bicycle")
            if abs(right.center().y() - left.center().y()) > 0.35 * (left_radius + right_radius):
                issues.append("SVG bicycle wheels are not aligned at a readable height")

            saddle = bounds.get("bicycle-saddle")
            if saddle and saddle.width() > 1.5 * min(left_radius, right_radius):
                issues.append("bicycle-saddle must be a compact sitting pad, not a large block")
            handlebar = bounds.get("bicycle-handlebar")
            if handlebar and max(handlebar.width(), handlebar.height()) > 1.5 * min(left_radius, right_radius):
                issues.append("bicycle-handlebar must be a compact grip, not a wheel-sized circle")

            body = bounds.get("pelican-body")
            if body and body.width() > wheel_distance * 0.58:
                issues.append("pelican-body is too wide and overwhelms the bicycle")
            if body and body.height() < wheel_distance * 0.19:
                issues.append("pelican-body is too flat to read as a bird torso")
            if wheel_distance > 0:
                frame = identified_elements.get("bicycle-frame")
                if frame is not None and _svg_tag_name(frame) == "polyline":
                    numbers = [
                        float(value)
                        for value in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)", frame.attrib.get("points", ""))
                    ]
                    points = list(zip(numbers[::2], numbers[1::2]))
                    tolerance = max(1.0, min(left_radius, right_radius) * 0.25)
                    if not any(math.hypot(x - left.center().x(), y - left.center().y()) <= tolerance for x, y in points):
                        issues.append("SVG bicycle frame must meet the rear wheel hub")
                    pedals = bounds.get("bicycle-pedals")
                    if pedals:
                        crank = pedals.center()
                        if not any(math.hypot(x - crank.x(), y - crank.y()) <= tolerance for x, y in points):
                            issues.append("SVG bicycle frame must meet the visible crank/pedals")

    issues.extend(_collect_rider_pose_issues(identified_elements, bounds))

    pouch = identified_elements.get("pelican-pouch")
    if pouch is not None:
        fill = _svg_presentation_value(pouch, "fill", parents, css_rules)
        stroke = _svg_presentation_value(pouch, "stroke", parents, css_rules)
        if not _paint_is_visible_against_canvas(fill) and not _paint_is_visible_against_canvas(stroke):
            issues.append("pelican-pouch must have a visible fill or outline distinct from the blank canvas")
    return issues


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
    parents = {child: parent for parent in root.iter() for child in parent}
    css_rules = _svg_css_rules(root)
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
        if tag == "style":
            for _, body in re.findall(r"([^{}]+)\{([^{}]*)\}", element.text or ""):
                for declaration in body.split(";"):
                    property_name, separator, paint = declaration.partition(":")
                    if separator and property_name.strip().casefold() in {"fill", "stroke", "color"}:
                        paint = paint.strip().removesuffix("!important").strip()
                        if not _is_valid_svg_paint(paint):
                            invalid_paints.append(f"{property_name.strip().casefold()}={paint}")
        for raw_name, value in element.attrib.items():
            name = raw_name.rsplit("}", 1)[-1].casefold()
            if name.startswith("on"):
                raise RuntimeError(f"SVG event handler is not allowed: {name}")
            if name in {"fill", "stroke", "color"}:
                paint = value.strip()
                if not _is_valid_svg_paint(paint):
                    invalid_paints.append(f"{name}={paint}")
            if name == "style":
                for declaration in value.split(";"):
                    property_name, separator, paint = declaration.partition(":")
                    if separator and property_name.strip().casefold() in {"fill", "stroke", "color"}:
                        paint = paint.strip().removesuffix("!important").strip()
                        if not _is_valid_svg_paint(paint):
                            invalid_paints.append(f"{property_name.strip().casefold()}={paint}")
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

    _ensure_qt_gui_application()
    renderer = QSvgRenderer(QByteArray(source))
    if not renderer.isValid():
        raise RuntimeError("Qt could not parse the generated SVG")
    default_size = renderer.defaultSize()
    if default_size.width() < 1 or default_size.height() < 1:
        raise RuntimeError("SVG has no usable intrinsic dimensions or viewBox")
    if default_size.width() > 8192 or default_size.height() > 8192:
        raise RuntimeError("SVG dimensions exceed the 8192-pixel smoke-test limit")

    background = SMOKE_BACKGROUND
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
        "bicycle-fork": ({"path", "polyline", "line"}, "a visible path or line"),
        "bicycle-spokes": ({"g"}, "a group containing spoke shapes"),
        "bicycle-pedals": ({"g"}, "a group containing two separate pedal circles"),
        "pelican-head": ({"circle", "ellipse"}, "a circle or ellipse"),
        "pelican-eye": ({"circle"}, "a circle"),
        "pelican-beak": ({"polygon"}, "a polygon"),
        "pelican-body": ({"path", "ellipse"}, "an organic path or ellipse"),
        "pelican-wing": ({"path", "ellipse", "polygon"}, "a filled path, ellipse, or polygon"),
        "pelican-pouch": ({"path"}, "a curved path"),
        "pelican-wing-reaching": ({"path"}, "a curved path"),
    }
    malformed_parts = []
    semantic_elements: dict[str, ET.Element] = {}
    for identifier, (allowed_tags, description) in expected_tags.items():
        element = identified_elements.get(identifier)
        if element is None:
            continue
        shape = _resolve_svg_semantic_shape(element, allowed_tags)
        if shape is None:
            malformed_parts.append(f"{identifier} must be {description} (got {_svg_tag_name(element)})")
        else:
            semantic_elements[identifier] = shape
    malformed_parts.extend(f"invalid paint {paint}" for paint in invalid_paints[:6])
    for wheel_id in ("left-wheel", "right-wheel"):
        wheel = semantic_elements.get(wheel_id)
        if wheel is None:
            continue
        fill = _svg_presentation_value(wheel, "fill", parents, css_rules).casefold()
        stroke = _svg_presentation_value(wheel, "stroke", parents, css_rules)
        if fill not in {"none", "white", "#fff", "#ffffff"} or not stroke or stroke.casefold() in {"none", "transparent"}:
            malformed_parts.append(f"{wheel_id} must be an unfilled, visibly outlined tire")
    clipped_parts = []
    for identifier in sorted(required_parts & identified_elements.keys()):
        bounds = renderer.boundsOnElement(identifier)
        if bounds.width() <= 0 or bounds.height() <= 0:
            continue
        if (
            bounds.left() < view_x - 0.5
            or bounds.top() < view_y - 0.5
            or bounds.right() > view_x + view_width + 0.5
            or bounds.bottom() > view_y + view_height + 0.5
        ):
            clipped_parts.append(f"{identifier} is clipped by the viewBox")
    malformed_parts.extend(clipped_parts)
    if "bicycle-pedals" in identified_elements and len(_pedal_circles(identified_elements["bicycle-pedals"])) < 2:
        malformed_parts.append("bicycle-pedals must contain two distinct visible pedal circles")
    if viewbox_problem or missing_parts or malformed_parts or duplicate_ids:
        malformed_parts.extend(
            issue
            for issue in _collect_partial_illustration_geometry_issues(
                identified_elements,
                renderer,
                parents,
                css_rules,
            )
            if issue not in malformed_parts
        )
    if viewbox_problem or missing_parts or malformed_parts or duplicate_ids:
        raise RuntimeError(
            f"SVG preflight failed; {viewbox_problem or 'viewBox dimensions are adequate'}; "
            "lacks separately identified visual parts or uses incorrect core shapes: "
            f"invalid paint colors={invalid_paints[:6]}; missing={missing_parts}; "
            f"malformed={malformed_parts}; duplicate_ids={duplicate_ids}; "
            f"near_matches={near_matches}"
        )
    geometry_issues = _collect_illustration_geometry_issues(
        semantic_elements,
        identified_elements,
        renderer,
        parents,
        css_rules,
    )
    if geometry_issues:
        raise RuntimeError(
            "SVG visual checks failed; invalid paint colors=[]; missing=[]; "
            f"malformed={geometry_issues}; duplicate_ids=[]; near_matches={{}}"
        )
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
        # Retain rejection metadata for debugging without persisting tool arguments.
        for field in ("session_id", "name", "source", "tool_name", "reason", "detail", "turn"):
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
    agent_turn_limit: int = CodingAgent.MAX_SVG_ARTIFACT_TURNS,
) -> dict[str, Any]:
    if not 2 <= agent_turn_limit <= CodingAgent.MAX_SVG_ARTIFACT_TURNS:
        raise ValueError(f"agent_turn_limit must be between 2 and {CodingAgent.MAX_SVG_ARTIFACT_TURNS}")
    endpoint = validate_local_base_url(base_url)
    model = model.strip()
    if not model:
        raise ValueError("model name cannot be empty")
    configure_local_environment(endpoint, model, api_key)
    vision_model = (os.getenv("SCIDEV_VISION_MODEL") or "").strip()
    allowed_models = {model} | ({vision_model} if vision_model else set())
    artifacts = (artifact_dir or Path(tempfile.mkdtemp(prefix="scidev-pelican-svg-"))).expanduser().resolve()
    artifacts.mkdir(parents=True, exist_ok=True)

    wire_requests: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    real_urlopen = scidev_core.urlopen
    request_phase = {"value": "initial"}

    def observe_request(request: Request, timeout: float = 90.0):
        payload = json.loads(request.data.decode("utf-8"))
        requested_model = str(payload.get("model") or "")
        _record_local_request(
            request,
            model=model,
            requests=wire_requests,
            allowed_models=allowed_models,
        )
        started = time.perf_counter()
        record = wire_requests[-1]
        record["request_bytes"] = len(request.data or b"")
        record["phase"] = (
            f"{request_phase['value']}_visual_review" if requested_model == vision_model and vision_model else request_phase["value"]
        )
        streaming = payload.get("stream") is True
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
            enabled=False,
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
        agent.MAX_SVG_ARTIFACT_TURNS = agent_turn_limit

        def run_agent(session_id: str) -> dict[str, Any]:
            if session_id != SMOKE_SESSION_PREFIX:
                raise RuntimeError("the canonical SVG smoke accepts one exact user prompt per run")
            request_phase["value"] = "initial"
            task = fixed_prompt_task(session_id)
            try:
                with patch("scidev_core.urlopen", new=observe_request):
                    return agent.run(task)
            except Exception as exc:
                diagnostic_path = persist_failure_diagnostic(
                    artifacts,
                    model=model,
                    attempt=0,
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
        result = run_agent(smoke_session_id(0))
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
        relative_svg, svg_path = current_svg_path()
        shutil.copyfile(svg_path, saved_svg)
        try:
            validation = validate_and_render_svg(svg_path.read_bytes(), preview_png)
        except RuntimeError as exc:
            diagnostic_path = persist_failure_diagnostic(
                artifacts,
                model=model,
                attempt=0,
                session_id=smoke_session_id(0),
                reason=str(exc),
                events=events,
                request_count=len(wire_requests),
                request_metrics=wire_requests,
            )
            raise RuntimeError(
                "the exact one-sentence SVG task failed visual validation; "
                f"diagnostic saved to {diagnostic_path}"
            ) from exc
        total_elapsed = round(time.perf_counter() - started, 3)

        model_tools = [
            str(data.get("name"))
            for event_name, data in events
            if event_name == "tool_started"
            and data.get("source") not in {"harness_precommit", "harness_svg_artifact_recovery"}
        ]
        ledger_events = [
            json.loads(line)
            for line in (root / ".research" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        event_types = [event["event_type"] for event in ledger_events]
        for required in ("git_commit_created", "coding_session_completed"):
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
        if not wire_requests:
            raise RuntimeError("the fixed SVG task completed without a recorded model request")

        return {
            "status": "passed",
            "prompt": PELICAN_PROMPT,
            "endpoint": endpoint,
            "model": model,
            "vision_review_model": vision_model or None,
            "svg_file": relative_svg,
            "svg_artifact": str(saved_svg),
            "commit": commit_sha,
            "elapsed_seconds": total_elapsed,
            "model_tool_calls": model_tools,
            "harness_artifact_recoveries": artifact_recoveries,
            "conversation_summary_enabled": summary_settings.enabled,
            "agent_turn_limit": agent_turn_limit,
            "task_prompt_count": 1,
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
    parser.add_argument(
        "--agent-turn-limit",
        type=int,
        default=CodingAgent.MAX_SVG_ARTIFACT_TURNS,
        help=f"maximum model turns per SVG session (2-{CodingAgent.MAX_SVG_ARTIFACT_TURNS})",
    )
    parser.add_argument("--api-key", default=os.getenv("SCIDEV_API_KEY") or "ollama", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        result = run_smoke(
            args.base_url,
            args.model,
            args.api_key,
            args.artifact_dir,
            agent_turn_limit=args.agent_turn_limit,
        )
    except Exception as exc:  # noqa: BLE001 - report one actionable local SVG smoke error.
        print(f"Pelican SVG smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
