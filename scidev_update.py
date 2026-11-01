"""Check GitHub releases for a newer SciDevHarness build.

This module only reports updates. It never downloads or executes an installer;
installing remains an explicit user action.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

APP_VERSION = "0.1.0"
DEFAULT_REPO = "ljk00000/SciDevHarness"
MAX_BODY = 12_000
_VERSION = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateInfo:
    current: str
    latest: str
    url: str
    published_at: str
    notes: str
    asset_url: str = ""
    asset_name: str = ""
    asset_size: int = 0
    asset_digest: str = ""

    @property
    def available(self) -> bool:
        return compare_versions(self.latest, self.current) > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "current": self.current,
            "latest": self.latest,
            "available": self.available,
            "url": self.url,
            "published_at": self.published_at,
            "notes": self.notes,
            "asset_url": self.asset_url,
            "asset_name": self.asset_name,
            "asset_size": self.asset_size,
            "asset_digest": self.asset_digest,
        }


def verify_downloaded_asset(
    path: Path,
    expected_sha256: str,
    *,
    max_bytes: int = 1_500_000_000,
) -> str:
    """Verify a locally downloaded installer without executing it."""
    target = Path(path)
    if not target.is_file():
        raise UpdateError(f"安装包不存在：{target}")
    expected = str(expected_sha256 or "").strip().lower().removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise UpdateError("安装包摘要必须是 64 位 SHA-256")
    size = target.stat().st_size
    if size <= 0 or size > max_bytes:
        raise UpdateError(f"安装包大小异常：{size} 字节")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise UpdateError("安装包 SHA-256 不匹配，拒绝更新")
    return actual


def parse_version(value: str) -> tuple[int, int, int] | None:
    match = _VERSION.match(str(value or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def compare_versions(left: str, right: str) -> int:
    left_parts = parse_version(left)
    right_parts = parse_version(right)
    if left_parts is None or right_parts is None:
        return 0
    return (left_parts > right_parts) - (left_parts < right_parts)


def detect_repo(workspace: Path | None = None) -> str:
    configured = str(os.getenv("SCIDEV_UPDATE_REPO") or "").strip()
    if configured:
        return configured
    if workspace is None:
        return DEFAULT_REPO
    try:
        completed = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return DEFAULT_REPO
    remote = (completed.stdout or "").strip()
    match = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$", remote)
    if not match:
        return DEFAULT_REPO
    return f"{match.group(1)}/{match.group(2)}"


def _default_fetch(url: str, timeout: float) -> dict[str, Any]:
    from urllib import error as urllib_error
    from urllib import request as urllib_request

    request = urllib_request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"SciDevHarness/{APP_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_001)
    except urllib_error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("仓库没有可用的 GitHub Release（或仓库不可见）") from exc
        raise UpdateError(f"GitHub API 返回 HTTP {exc.code}") from exc
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"检查更新失败：{exc}") from exc
    if len(raw) > 2_000_000:
        raise UpdateError("GitHub Release 响应过大")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise UpdateError("GitHub Release 响应不是 JSON") from exc
    if not isinstance(data, dict):
        raise UpdateError("GitHub Release 响应不是对象")
    return data


def check_for_update(
    *,
    current: str = APP_VERSION,
    repo: str = "",
    timeout: float = 10.0,
    fetch: Callable[[str, float], dict[str, Any]] | None = None,
) -> UpdateInfo:
    selected_repo = str(repo or DEFAULT_REPO).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", selected_repo):
        raise UpdateError(f"无效的 GitHub 仓库：{selected_repo}")
    url = f"https://api.github.com/repos/{selected_repo}/releases/latest"
    data = (fetch or _default_fetch)(url, max(2.0, min(float(timeout), 30.0)))
    tag = str(data.get("tag_name") or "").strip()
    if parse_version(tag) is None:
        raise UpdateError(f"无法识别 Release 版本：{tag or '(empty)'}")
    assets_raw = data.get("assets")
    assets: list[Any] = assets_raw if isinstance(assets_raw, list) else []
    asset_url = ""
    asset_name = ""
    asset_size = 0
    asset_digest = ""
    for suffix in (".exe", ".msi", ".zip"):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "").lower()
            if not name.endswith(suffix):
                continue
            candidate = str(asset.get("browser_download_url") or "")
            if not candidate.startswith("https://"):
                continue
            asset_url = candidate
            asset_name = str(asset.get("name") or "")
            try:
                asset_size = max(0, int(asset.get("size") or 0))
            except (TypeError, ValueError):
                asset_size = 0
            asset_digest = str(asset.get("digest") or "").strip()
            break
        if asset_url:
            break
    return UpdateInfo(
        current=str(current),
        latest=tag.lstrip("vV"),
        url=str(data.get("html_url") or ""),
        published_at=str(data.get("published_at") or ""),
        notes=str(data.get("body") or "")[:MAX_BODY],
        asset_url=asset_url,
        asset_name=asset_name,
        asset_size=asset_size,
        asset_digest=asset_digest,
    )
