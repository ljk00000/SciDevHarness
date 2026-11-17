"""Workspace path classification shared by tools and Git review."""
from __future__ import annotations

from pathlib import Path
import sys


def application_base_dir() -> Path:
    """Return the source or deployed app directory for bundled UI resources."""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parents[2]


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(callable(is_junction) and is_junction())
    except OSError:
        # A path can disappear while QFileSystemModel refreshes; keep filesystem
        # errors out of the Qt filter callback.
        return True


DEFAULT_EXCLUDED_PATH_NAMES = frozenset(
    {".git", ".research", "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules"}
)
DEFAULT_SENSITIVE_PATH_NAMES = frozenset(
    {
        ".aws",
        ".azure",
        ".envrc",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".ssh",
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "secrets",
        "secrets.json",
    }
)
DEFAULT_SAFE_ENV_TEMPLATES = (".example", ".sample", ".template", ".dist")


def _is_sensitive_component(
    name: str,
    sensitive_names: set[str] | frozenset[str] = DEFAULT_SENSITIVE_PATH_NAMES,
    safe_env_templates: tuple[str, ...] = DEFAULT_SAFE_ENV_TEMPLATES,
) -> bool:
    folded = name.casefold()
    if folded in sensitive_names or folded == ".env":
        return True
    return folded.startswith(".env.") and not folded.endswith(safe_env_templates)


def _is_protected_workspace_path(
    raw_path: str | Path,
    excluded_names: set[str] | frozenset[str] = DEFAULT_EXCLUDED_PATH_NAMES,
    sensitive_names: set[str] | frozenset[str] = DEFAULT_SENSITIVE_PATH_NAMES,
    safe_env_templates: tuple[str, ...] = DEFAULT_SAFE_ENV_TEMPLATES,
) -> bool:
    components = str(raw_path).replace("\\", "/").split("/")
    excluded = {name.casefold() for name in excluded_names}
    return any(
        component.casefold() in excluded
        or _is_sensitive_component(component, sensitive_names, safe_env_templates)
        for component in components
        if component and component not in {".", ".."}
    )


def resolve_initial_workspace(
    explicit_workspace: str | Path | None,
    last_workspace: str | Path | None,
    fallback: str | Path,
) -> Path:
    """Resolve a requested, remembered, or development workspace in priority order."""
    if explicit_workspace is not None:
        requested = Path(explicit_workspace).expanduser().resolve()
        if not requested.is_dir():
            raise ValueError(f"工作区目录不存在：{requested}")
        return requested

    if last_workspace:
        remembered = Path(last_workspace).expanduser()
        if remembered.is_dir():
            return remembered.resolve()

    default = Path(fallback).expanduser().resolve()
    if not default.is_dir():
        raise ValueError(f"默认工作区目录不存在：{default}")
    return default


def default_sample_workspace(application_dir: str | Path) -> Path:
    """Return the bundled Tetris sample, falling back to the app directory."""
    base = Path(application_dir).expanduser().resolve()
    sample = base / "examples" / "tetris"
    return sample.resolve() if sample.is_dir() else base


def resolve_startup_workspace(
    explicit_workspace: str | Path | None,
    last_workspace: str | Path | None,
    application_dir: str | Path,
) -> Path:
    """Prefer explicit and remembered projects, migrating the legacy app-root default."""
    base = Path(application_dir).expanduser().resolve()
    default = default_sample_workspace(base)
    if explicit_workspace is not None:
        return resolve_initial_workspace(explicit_workspace, None, default)

    remembered = last_workspace
    if remembered:
        remembered_path = Path(remembered).expanduser()
        if remembered_path.is_dir() and remembered_path.resolve() == base and default != base:
            remembered = default
    return resolve_initial_workspace(None, remembered, default)
