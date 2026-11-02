"""Check GitHub releases and optionally download a verified Windows asset.

The download path is deliberately separate from installation: it verifies the
SHA-256 and size, writes the file atomically, and never executes it. Installing
remains an explicit user action.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
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


@dataclass(frozen=True)
class DownloadedAsset:
    path: Path
    sha256: str
    size: int

    def as_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "sha256": self.sha256, "size": self.size}


def download_asset(
    info: UpdateInfo,
    destination: Path,
    *,
    expected_sha256: str = "",
    max_bytes: int = 1_500_000_000,
    timeout: float = 60.0,
    overwrite: bool = False,
    open_url: Callable[[str, float], Any] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> DownloadedAsset:
    """Download one HTTPS release asset, verify it, and stop before execution.

    The caller supplies the destination file, or an existing directory in which
    the release asset name is used. A digest is mandatory: either the caller's
    explicit value or the GitHub asset digest. Downloads stream to a temporary
    file in the same directory and are atomically renamed only after both size
    and SHA-256 checks pass.
    """
    url = str(info.asset_url or "")
    if not url.startswith("https://"):
        raise UpdateError("安装包地址必须是 HTTPS")
    if cancel is not None and cancel():
        raise UpdateError("下载已取消")
    target = Path(destination).expanduser()
    if target.is_dir():
        if not info.asset_name:
            raise UpdateError("下载目录需要安装包文件名")
        target = target / info.asset_name
    if target.exists() and not overwrite:
        raise UpdateError(f"目标文件已存在，拒绝覆盖：{target}")
    expected = str(expected_sha256 or info.asset_digest or "").strip().lower().removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise UpdateError("Release 没有可用的 SHA-256 摘要；请用 --sha256 显式提供")
    target.parent.mkdir(parents=True, exist_ok=True)

    request_timeout = max(5.0, min(float(timeout), 600.0))
    try:
        from urllib import request as urllib_request

        def default_open(asset_url: str, seconds: float) -> Any:
            request = urllib_request.Request(
                asset_url,
                headers={"User-Agent": f"SciDevHarness/{APP_VERSION}"},
            )
            return urllib_request.urlopen(request, timeout=seconds)

        response = (open_url or default_open)(url, request_timeout)
    except UpdateError:
        raise
    except Exception as exc:  # urllib raises several unrelated exception types
        raise UpdateError(f"下载安装包失败：{exc}") from exc

    temporary_name = ""
    try:
        with response:
            final_url = str(getattr(response, "geturl", lambda: url)() or url)
            if not final_url.startswith("https://"):
                raise UpdateError("下载被重定向到非 HTTPS 地址，拒绝继续")
            content_length = 0
            try:
                content_length = int(getattr(response, "headers", {}).get("Content-Length") or 0)
            except (TypeError, ValueError):
                content_length = 0
            if content_length > max_bytes:
                raise UpdateError(f"安装包声明大小超过上限：{content_length} 字节")
            if info.asset_size and content_length and content_length != info.asset_size:
                raise UpdateError("安装包声明大小与 Release 不一致")
            handle = tempfile.NamedTemporaryFile(
                mode="wb",
                dir=str(target.parent),
                prefix=f".{target.name}.",
                suffix=".part",
                delete=False,
            )
            temporary_name = handle.name
            digest = hashlib.sha256()
            size = 0
            try:
                with handle:
                    while True:
                        if cancel is not None and cancel():
                            raise UpdateError("下载已取消")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise UpdateError(f"安装包超过大小上限：{max_bytes} 字节")
                        digest.update(chunk)
                        handle.write(chunk)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                temporary_name = ""
                raise
        if size <= 0:
            raise UpdateError("下载到的安装包为空")
        if info.asset_size and size != info.asset_size:
            raise UpdateError(f"安装包大小不匹配：{size} != {info.asset_size}")
        actual = digest.hexdigest()
        if actual != expected:
            raise UpdateError("安装包 SHA-256 不匹配，拒绝更新")
        os.replace(temporary_name, target)
        temporary_name = ""
        return DownloadedAsset(path=target, sha256=actual, size=size)
    except UpdateError:
        raise
    except OSError as exc:
        raise UpdateError(f"写入安装包失败：{exc}") from exc
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def launch_installer(
    path: Path,
    *,
    expected_sha256: str,
    open_process: Callable[[list[str]], Any] | None = None,
) -> int:
    """Launch a verified installer without silently installing it.

    The user explicitly asked for this flag; the installer's own UI remains in
    control. The digest is checked again immediately before launch to close the
    window between download and execution.
    """
    target = Path(path)
    verify_downloaded_asset(target, expected_sha256)
    suffix = target.suffix.lower()
    if suffix not in {".exe", ".msi"}:
        raise UpdateError("只支持启动 .exe 或 .msi 安装包")
    if os.name != "nt":
        raise UpdateError("安装器启动仅支持 Windows")
    command = [str(target)]
    if suffix == ".msi":
        command = ["msiexec.exe", "/i", str(target)]
    try:
        process = (open_process or (lambda argv: subprocess.Popen(argv)))(command)
    except OSError as exc:
        raise UpdateError(f"无法启动安装器：{exc}") from exc
    return int(getattr(process, "pid", 0) or 0)


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
