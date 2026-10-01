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
from typing import Any
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
MAX_SVG_BYTES = 2_000_000
RENDER_WIDTH = 1200
RENDER_HEIGHT = 800


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
    if view_width < 300 or view_height < 180:
        raise RuntimeError("SVG viewBox is too small for the requested two-object illustration")

    counts: Counter[str] = Counter()
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
            if name in {"href", "src"} and value.strip() and not value.strip().startswith("#"):
                raise RuntimeError("external SVG resources are not allowed")
            if re.search(r"url\s*\(\s*(?!['\"]?#)", value, re.I) or (
                name == "style" and "@import" in value.casefold()
            ):
                raise RuntimeError("external SVG stylesheets and resources are not allowed")
    description = " ".join(
        (element.text or "")
        for element in root.iter()
        if isinstance(element.tag, str) and element.tag.rsplit("}", 1)[-1].casefold() in {"title", "desc"}
    ).casefold()
    identified_elements = {
        str(element.attrib.get("id", "")).casefold(): element
        for element in root.iter()
        if isinstance(element.tag, str) and element.attrib.get("id")
    }

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
    if "pelican" not in description or "bicycle" not in description:
        raise RuntimeError("SVG title/description does not identify both a pelican and a bicycle")
    required_parts = {
        "left-wheel",
        "right-wheel",
        "bicycle-frame",
        "pelican-body",
        "pelican-head",
        "pelican-wing",
        "pelican-beak",
        "pelican-pouch",
    }
    missing_parts = sorted(required_parts - identified_elements.keys())
    if missing_parts:
        raise RuntimeError(f"SVG lacks separately identified visual parts: {missing_parts}")
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
    wheel_distance = math.hypot(right_x - left_x, right_y - left_y)
    if wheel_distance < 0.9 * (left_radius + right_radius):
        raise RuntimeError("SVG bicycle wheels overlap instead of forming a readable bicycle")
    if abs(right_y - left_y) > 0.35 * (left_radius + right_radius):
        raise RuntimeError("SVG bicycle wheels are not aligned at a readable height")
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
    frame_numbers = [float(value) for value in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.?\d*)", frame.attrib.get("points", ""))]
    if len(frame_numbers) < 6 or len(frame_numbers) % 2:
        raise RuntimeError("SVG bicycle frame polyline has too few valid points")
    frame_points = list(zip(frame_numbers[::2], frame_numbers[1::2]))
    hub_tolerance = max(1.0, min(left_radius, right_radius) * 0.25)
    for wheel_x, wheel_y, _wheel_radius in wheel_data:
        if not any(math.hypot(x - wheel_x, y - wheel_y) <= hub_tolerance for x, y in frame_points):
            raise RuntimeError("SVG bicycle frame does not meet both wheel hubs")
    if not any(
        min(left_x, right_x) < x < max(left_x, right_x)
        and abs(y - (left_y + right_y) / 2) > min(left_radius, right_radius) * 0.5
        for x, y in frame_points
    ):
        raise RuntimeError("SVG bicycle frame has no readable triangular center vertex")
    return {
        "svg_bytes": len(source),
        "intrinsic_width": default_size.width(),
        "intrinsic_height": default_size.height(),
        "element_counts": dict(sorted(counts.items())),
        "render_size": [RENDER_WIDTH, RENDER_HEIGHT],
        "non_background_samples": changed_samples,
        "preview_png": str(preview_path),
    }


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

    def observe_request(request: Request, timeout: float = 90.0):
        _record_local_request(request, model=model, requests=wire_requests)
        started = time.perf_counter()
        try:
            return real_urlopen(request, timeout=timeout)
        finally:
            wire_requests[-1]["elapsed_seconds"] = round(time.perf_counter() - started, 3)

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
            event_callback=lambda name, data: events.append((name, data)),
            summary_settings=summary_settings,
        )

        def run_agent(task_prompt: str) -> dict[str, Any]:
            task = {"payload": {"session_id": "live_pelican_svg_smoke", "prompt": task_prompt}}
            with patch("scidev_core.urlopen", new=observe_request):
                return agent.run(task)

        def collect_file_calls() -> list[dict[str, Any]]:
            return [
                data
                for event_name, data in events
                if event_name == "tool_started"
                and data.get("source") != "harness_precommit"
                and data.get("name") == "write_file"
            ]

        def current_svg_path() -> tuple[str, Path]:
            paths = {
                str(data.get("arguments", {}).get("path", ""))
                for data in collect_file_calls()
                if str(data.get("arguments", {}).get("path", "")).casefold().endswith(".svg")
            }
            if len(paths) != 1:
                raise RuntimeError(f"expected exactly one SVG output from write_file, got {sorted(paths)}")
            relative = paths.pop()
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise RuntimeError("the generated SVG is missing or outside the disposable workspace")
            return relative, path

        started = time.perf_counter()
        result = run_agent(PELICAN_PROMPT)
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
            trace = [
                {
                    "event": event_name,
                    "name": data.get("name"),
                    "text": str(data.get("text", data.get("result", "")))[:1200],
                }
                for event_name, data in events
                if event_name in {"assistant", "tool_result"}
            ]
            raise RuntimeError(
                "Qwen/Harness did not create a file for the exact pelican prompt: "
                f"tools={tool_names}, requests={len(wire_requests)}, trace={json.dumps(trace, ensure_ascii=True)}"
            )
        if any(
            data.get("name") == "run_command"
            for event_name, data in events
            if event_name == "tool_started" and data.get("source") != "harness_precommit"
        ):
            raise RuntimeError("Qwen unexpectedly requested shell execution")

        preview_png = artifacts / "pelican_bicycle_preview.png"
        saved_svg = artifacts / "pelican_bicycle.svg"
        validation: dict[str, Any] | None = None
        relative_svg = ""
        svg_path = root
        for repair_attempt in range(2):
            relative_svg, svg_path = current_svg_path()
            shutil.copyfile(svg_path, saved_svg)
            try:
                validation = validate_and_render_svg(svg_path.read_bytes(), preview_png)
                break
            except RuntimeError as exc:
                if repair_attempt == 1:
                    raise RuntimeError(f"pelican SVG still failed after one repair: {exc}") from exc
                previous_write_count = len(file_calls)
                repair_prompt = (
                    "Create a corrected version of the existing pelican_bicycle.svg; the original request remains: "
                    f"{PELICAN_PROMPT}. The rendered SVG failed a structural visual check: {exc}. "
                    "Rewrite the SVG using write_file; do not use shell commands. Use viewBox=\"0 0 480 320\". "
                    "Draw two unfilled bicycle tire circles with centers near (100,245) and (380,245), radius about 35. "
                    "The bicycle-frame must be a polyline that passes through both wheel centers and a higher center "
                    "vertex; add visible saddle and handlebar. Draw a recognizable pelican perched above the saddle: "
                    "separate body, head, wing, a long pointed bill extending forward, and a throat pouch below the bill. "
                    "Use the exact semantic IDs left-wheel, right-wheel, bicycle-frame, pelican-body, pelican-head, "
                    "pelican-wing, pelican-beak, and pelican-pouch on visible shapes. Keep the entire scene inside the "
                    "viewBox with margin. Then inspect the Git diff and briefly report completion."
                )
                result = run_agent(repair_prompt)
                file_calls = collect_file_calls()
                if len(file_calls) <= previous_write_count:
                    raise RuntimeError("Qwen did not revise the SVG after structural visual feedback") from exc
                if any(
                    data.get("name") == "run_command"
                    for event_name, data in events
                    if event_name == "tool_started" and data.get("source") != "harness_precommit"
                ):
                    raise RuntimeError("Qwen unexpectedly requested shell execution during SVG repair")
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
