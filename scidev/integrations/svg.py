"""Recover safe SVG artifacts from explicit model creation and repair responses."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from scidev.runtime.helpers import new_id
from scidev.agent.toolbox import CodingToolbox


class SvgArtifactAdapter:
    """Recover an explicitly requested SVG when a model returns it as fenced XML."""

    _REQUEST_VERBS = re.compile(r"\b(generate|create|draw|make|produce)\b|生成|绘制|制作|画", re.IGNORECASE)
    _REPAIR_DIRECTIVE = re.compile(
        r"\b(?:repair|fix|edit|revise|adjust)\s+(?:only|the|this|existing|current|listed|reported|duplicate)\b"
        r"|(?:修复|修改|编辑)(?:现有|当前|指定|列出|此|该)?",
        re.IGNORECASE,
    )
    _LOCAL_REPAIR_SCOPE = re.compile(r"\b(?:localized|targeted|minimal|smallest exact)\b", re.IGNORECASE)
    _SVG_BLOCK = re.compile(r"```(?:svg|xml)\s*(.*?)```", re.IGNORECASE | re.DOTALL)
    _TOOL_RESPONSE = re.compile(r"\s*<tool_response>\s*(.*?)\s*</tool_response>\s*", re.IGNORECASE | re.DOTALL)
    _RAW_SVG = re.compile(r"\A(?:<\?xml\s+[^?]*\?>\s*)?<svg\b.*</svg\s*>\Z", re.IGNORECASE | re.DOTALL)
    _SAFE_FILENAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.svg\Z", re.IGNORECASE)
    _ACTIVE_ELEMENTS = {"script", "foreignobject", "iframe", "object", "embed"}

    @classmethod
    def is_repair_request(cls, prompt: str) -> bool:
        return bool("svg" in prompt.casefold() and cls._REPAIR_DIRECTIVE.search(prompt))

    @classmethod
    def is_local_repair_request(cls, prompt: str) -> bool:
        return bool(
            cls.is_repair_request(prompt)
            and cls._LOCAL_REPAIR_SCOPE.search(prompt)
            and "replace_in_file" in prompt.casefold()
        )

    @classmethod
    def is_creation_request(cls, prompt: str) -> bool:
        return bool(
            not cls.is_repair_request(prompt)
            and cls._REQUEST_VERBS.search(prompt)
            and "svg" in prompt.casefold()
        )

    @classmethod
    def is_svg_artifact_request(cls, prompt: str) -> bool:
        return cls.is_creation_request(prompt) or cls.is_repair_request(prompt)

    @classmethod
    def _recover_complete_svg_from_tool_text(cls, candidate: str) -> tuple[str, str] | None:
        """Recover a complete SVG from a malformed but explicit write_file envelope."""
        if not re.search(r'"name"\s*:\s*"write_file"', candidate):
            return None
        path_match = re.search(
            r'"path"\s*:\s*"([A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.svg)"',
            candidate,
            re.IGNORECASE,
        )
        content_match = re.search(r'"content"\s*:\s*"', candidate)
        if not path_match or not content_match:
            return None
        content_start = content_match.end()
        svg_start = candidate.find("<svg", content_start)
        svg_end = candidate.find("</svg>", svg_start)
        if svg_start < 0 or svg_end < 0 or candidate.find("<svg", svg_start + 4) >= 0:
            return None
        encoded = candidate[svg_start : svg_end + len("</svg>")]
        try:
            source = json.loads('"' + encoded + '"')
        except json.JSONDecodeError:
            # The closing root proves the artifact is complete; normalize only
            # JSON's two common string escapes, then retain strict SVG checks.
            source = encoded.replace(r"\n", "\n").replace(r"\t", "\t").replace(r'\"', '"')
        if not cls._RAW_SVG.fullmatch(source):
            return None
        return source, path_match.group(1)

    @classmethod
    def _extract_svg_payload(cls, response: str) -> tuple[str, str | None] | None:
        """Accept one SVG or a narrowly validated textual write_file envelope."""
        matches = list(cls._SVG_BLOCK.finditer(response))
        if matches:
            if len(matches) != 1:
                return None
            candidate = matches[0].group(1).strip()
            payload_filename: str | None = None
            try:
                envelope = json.loads(candidate)
            except json.JSONDecodeError:
                recovered = cls._recover_complete_svg_from_tool_text(candidate)
                if recovered:
                    return recovered
                envelope = None
            if isinstance(envelope, dict):
                arguments = envelope.get("arguments")
                if (
                    envelope.get("name") != "write_file"
                    or not isinstance(arguments, dict)
                    or set(arguments) != {"path", "content"}
                    or not isinstance(arguments.get("path"), str)
                    or not cls._SAFE_FILENAME.fullmatch(arguments["path"])
                    or not isinstance(arguments.get("content"), str)
                ):
                    return None
                candidate = arguments["content"].strip()
                payload_filename = arguments["path"]
            if cls._RAW_SVG.fullmatch(candidate):
                return candidate, payload_filename
            return None

        candidate = response.strip()
        wrapped = cls._TOOL_RESPONSE.fullmatch(candidate)
        if wrapped:
            candidate = wrapped.group(1)
            # Some local models mimic a tool result by prefixing each XML line
            # with a display line number. Normalize only this narrowly matched
            # wrapper; arbitrary prose around an SVG is never recovered.
            candidate = re.sub(r"(?m)^\s*\d+\s*:\s?", "", candidate)
        candidate = candidate.strip()
        payload_filename = None
        try:
            envelope = json.loads(candidate)
        except json.JSONDecodeError:
            recovered = cls._recover_complete_svg_from_tool_text(candidate)
            if recovered:
                return recovered
            envelope = None
        if isinstance(envelope, dict):
            arguments = envelope.get("arguments")
            if (
                envelope.get("name") != "write_file"
                or not isinstance(arguments, dict)
                or set(arguments) != {"path", "content"}
                or not isinstance(arguments.get("path"), str)
                or not cls._SAFE_FILENAME.fullmatch(arguments["path"])
                or not isinstance(arguments.get("content"), str)
            ):
                return None
            candidate = arguments["content"].strip()
            payload_filename = arguments["path"]
        return (candidate, payload_filename) if cls._RAW_SVG.fullmatch(candidate) else None

    @classmethod
    def _extract_svg_source(cls, response: str) -> str | None:
        payload = cls._extract_svg_payload(response)
        return payload[0] if payload else None

    @classmethod
    def create_tool_call(cls, prompt: str, response: str, project_root: Path) -> dict[str, Any] | None:
        """Recover one safe SVG creation or an explicitly scoped edit of an existing SVG."""
        repair_request = cls.is_repair_request(prompt)
        if cls.is_local_repair_request(prompt):
            return None
        if not cls.is_creation_request(prompt) and not repair_request:
            return None
        payload = cls._extract_svg_payload(response)
        if payload is None:
            return None
        source, payload_filename = payload
        if not source or len(source.encode("utf-8")) > CodingToolbox.MAX_WRITE_BYTES:
            return None
        lowered = source.casefold()
        if "<!doctype" in lowered or "<!entity" in lowered:
            return None
        try:
            root = ET.fromstring(source)
        except ET.ParseError:
            return None
        if not isinstance(root.tag, str) or root.tag.rsplit("}", 1)[-1].casefold() != "svg":
            return None
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            if element.tag.rsplit("}", 1)[-1].casefold() in cls._ACTIVE_ELEMENTS:
                return None
            if element.tag.rsplit("}", 1)[-1].casefold() == "style" and (
                "@import" in (element.text or "").casefold()
                or re.search(r"url\s*\(\s*(?!['\"]?#)", element.text or "", re.I)
            ):
                return None
            for raw_name, value in element.attrib.items():
                name = raw_name.rsplit("}", 1)[-1].casefold()
                if name.startswith("on"):
                    return None
                if name in {"href", "src"} and value.strip() and not value.strip().startswith("#"):
                    return None
                if re.search(r"url\s*\(\s*(?!['\"]?#)", value, re.I) or (
                    name == "style" and "@import" in value.casefold()
                ):
                    return None

        filename_pattern = r"(?<![A-Za-z0-9_.-])([A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.svg)(?![A-Za-z0-9_.-])"
        names = re.findall(
            filename_pattern,
            response,
            re.I,
        )
        existing_targets = re.findall(filename_pattern, prompt, re.I)
        workspace = Path(project_root).resolve()
        overwrite_target = ""
        if repair_request:
            target_match = re.search(
                r"\bin\s+`?([A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.svg)`?\b",
                prompt,
                re.IGNORECASE,
            )
            if not target_match:
                return None
            overwrite_target = target_match.group(1)
            target_path = (workspace / overwrite_target).resolve()
            if (
                not cls._SAFE_FILENAME.fullmatch(overwrite_target)
                or workspace not in target_path.parents
                or not target_path.is_file()
                or (payload_filename and payload_filename.casefold() != overwrite_target.casefold())
            ):
                return None
        else:
            overwrite_target = next(
                (
                    name
                    for name in existing_targets
                    if cls._SAFE_FILENAME.fullmatch(name)
                    and re.search(rf"\bexisting\s+`?{re.escape(name)}\b`?", prompt, re.I)
                ),
                "",
            )
        filename = overwrite_target or payload_filename or next(
            (name for name in reversed(names) if cls._SAFE_FILENAME.fullmatch(name)),
            "generated.svg",
        )
        candidate = filename
        suffix = 1
        while (
            (not overwrite_target and (workspace / candidate).exists())
            or CodingToolbox.is_protected_path(candidate)
        ):
            candidate = f"{Path(filename).stem}-{suffix}.svg"
            suffix += 1
        return {
            "id": new_id("call"),
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": candidate, "content": source}, ensure_ascii=False),
            },
        }
