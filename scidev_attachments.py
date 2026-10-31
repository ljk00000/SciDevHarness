"""Bounded image attachments for coding tasks."""
from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 12 * 1024 * 1024
MIME_BY_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
SUPPORTED_MIME = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


class AttachmentError(ValueError):
    pass


def _detect_mime(data: bytes) -> str:
    for magic, mime in MIME_BY_MAGIC:
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise AttachmentError("只支持 PNG/JPEG/GIF/WebP 图片")


def _safe_name(raw: object, fallback: str) -> str:
    name = Path(str(raw or "")).name.strip()
    if not name or name in {".", ".."}:
        return fallback
    return name[:80]


def _normalize_data_url(raw: str, name: str = "image") -> dict[str, Any]:
    if not raw.startswith("data:") or ";base64," not in raw:
        raise AttachmentError("图片必须是 base64 data URL")
    header, encoded = raw.split(";base64,", 1)
    mime = header[5:].strip().casefold()
    if mime not in SUPPORTED_MIME:
        raise AttachmentError(f"不支持的图片类型：{mime or '(none)'}")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("图片 base64 无效") from exc
    if not data:
        raise AttachmentError("图片为空")
    if len(data) > MAX_IMAGE_BYTES:
        raise AttachmentError(f"单张图片不能超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    detected = _detect_mime(data)
    if detected != mime:
        raise AttachmentError("图片 MIME 与实际内容不一致")
    return {
        "name": _safe_name(name, "image"),
        "mime": mime,
        "size_bytes": len(data),
        "data_url": f"data:{mime};base64,{encoded}",
    }


def normalize_images(raw: object) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AttachmentError("images 必须是数组")
    if len(raw) > MAX_IMAGES:
        raise AttachmentError(f"最多附加 {MAX_IMAGES} 张图片")
    images: list[dict[str, Any]] = []
    total = 0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AttachmentError("图片条目必须是对象")
        data_url = str(item.get("data_url") or "")
        image = _normalize_data_url(data_url, _safe_name(item.get("name"), f"image-{index + 1}"))
        total += int(image["size_bytes"])
        if total > MAX_TOTAL_BYTES:
            raise AttachmentError(f"图片总大小不能超过 {MAX_TOTAL_BYTES // (1024 * 1024)} MB")
        images.append(image)
    return images


def load_image_file(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AttachmentError(f"无法读取图片：{exc}") from exc
    if size <= 0 or size > MAX_IMAGE_BYTES:
        raise AttachmentError(f"图片大小必须在 1 字节到 {MAX_IMAGE_BYTES // (1024 * 1024)} MB 之间")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AttachmentError(f"无法读取图片：{exc}") from exc
    mime = _detect_mime(data)
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "name": _safe_name(path.name, "image"),
        "mime": mime,
        "size_bytes": len(data),
        "data_url": f"data:{mime};base64,{encoded}",
    }


def image_content_parts(images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": "image_url", "image_url": {"url": image["data_url"]}}
        for image in images
        if isinstance(image, dict) and isinstance(image.get("data_url"), str)
    ]
