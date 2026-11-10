"""Workspace path classification shared by tools and Git review."""
from __future__ import annotations

from pathlib import Path
import sys


def application_base_dir() -> Path:
    """Return the source or deployed app directory for bundled UI resources."""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parent


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
