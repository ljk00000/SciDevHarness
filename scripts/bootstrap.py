"""Create or validate the project-local runtime, then launch the desktop client."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_PYTHON_LOWER = (3, 12)
SUPPORTED_PYTHON_UPPER = (3, 15)
MINIMUM_PYSIDE6 = (6, 11)


class BootstrapError(RuntimeError):
    """A clear, actionable runtime setup error."""


def supports_python_version(version: Sequence[int]) -> bool:
    try:
        major, minor = int(version[0]), int(version[1])
    except (IndexError, TypeError, ValueError):
        return False
    return SUPPORTED_PYTHON_LOWER <= (major, minor) < SUPPORTED_PYTHON_UPPER


def supports_pyside6_version(version: str) -> bool:
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except (AttributeError, TypeError, ValueError):
        return False
    return major == 6 and minor >= MINIMUM_PYSIDE6[1]


def _query_python_version(command: Sequence[str]) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [*command, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        major, minor = (int(part) for part in result.stdout.strip().split(".")[:2])
    except (ValueError, TypeError):
        return None
    return major, minor


def _select_python_command() -> tuple[list[str], tuple[int, int]]:
    current_version = tuple(sys.version_info[:2])
    if supports_python_version(current_version):
        return [sys.executable], current_version

    launcher = shutil.which("py")
    if launcher:
        for requested_version in ("3.14", "3.13", "3.12"):
            command = [launcher, f"-{requested_version}"]
            actual_version = _query_python_version(command)
            if actual_version and supports_python_version(actual_version):
                return command, actual_version

    raise BootstrapError(
        "Python 3.12–3.14 is required. Install one of these versions, then start SciDevHarness again."
    )


def _ensure_virtual_environment(
    project_root: Path,
    *,
    create: bool,
    output: Callable[[str], None],
) -> Path:
    environment = project_root / ".venv"
    interpreter = environment / "Scripts" / "python.exe"

    if environment.exists():
        if not environment.is_dir() or not interpreter.is_file():
            raise BootstrapError(
                f"The existing .venv is incomplete at {environment}; it was left untouched. "
                "Back it up and repair or replace it with Python 3.12–3.14."
            )
    else:
        if not create:
            raise BootstrapError(f"Project virtual environment is missing: {environment}")
        command, version = _select_python_command()
        output(f"Creating project .venv with Python {version[0]}.{version[1]}...")
        subprocess.run([*command, "-m", "venv", str(environment)], cwd=project_root, check=True)

    version = _query_python_version([str(interpreter)])
    if version is None or not supports_python_version(version):
        found = f"{version[0]}.{version[1]}" if version else "unknown"
        raise BootstrapError(
            f"The existing .venv uses unsupported Python {found}; required range is 3.12–3.14. "
            "It was not replaced. Back it up before recreating it."
        )
    return interpreter


def _installed_qt_essentials_version(interpreter: Path) -> str | None:
    result = subprocess.run(
        [
            str(interpreter),
            "-c",
            "from importlib.metadata import version; print(version('PySide6_Essentials'))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def ensure_runtime(
    project_root: Path | None = None,
    *,
    create_environment: bool = True,
    install_dependencies: bool = True,
    output: Callable[[str], None] = print,
) -> Path:
    project_root = Path(PROJECT_ROOT if project_root is None else project_root).resolve()
    interpreter = _ensure_virtual_environment(
        project_root,
        create=create_environment,
        output=output,
    )
    installed_version = _installed_qt_essentials_version(interpreter)
    if installed_version and supports_pyside6_version(installed_version):
        return interpreter
    if not install_dependencies:
        detail = f" (found {installed_version})" if installed_version else ""
        raise BootstrapError(
            f"PySide6-Essentials >=6.11,<7 is required{detail}; run start_client.bat to prepare dependencies."
        )

    output("Installing or updating project dependencies in .venv...")
    subprocess.run(
        [str(interpreter), "-m", "pip", "install", "-r", str(project_root / "requirements.txt")],
        cwd=project_root,
        check=True,
    )
    installed_version = _installed_qt_essentials_version(interpreter)
    if not installed_version or not supports_pyside6_version(installed_version):
        raise BootstrapError(
            "Dependency installation finished, but PySide6-Essentials >=6.11,<7 is still unavailable."
        )
    return interpreter


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    check_only = arguments == ["--check-runtime"]
    try:
        interpreter = ensure_runtime(
            PROJECT_ROOT,
            create_environment=not check_only,
            install_dependencies=not check_only,
        )
    except (BootstrapError, OSError, subprocess.CalledProcessError) as exc:
        print(f"SciDevHarness startup failed: {exc}", file=sys.stderr)
        return 1

    if check_only:
        print("SciDevHarness launcher runtime check: OK")
        return 0

    try:
        result = subprocess.run(
            [str(interpreter), str(PROJECT_ROOT / "scidev_client.py"), *arguments],
            cwd=PROJECT_ROOT,
            check=False,
        )
    except OSError as exc:
        print(f"Could not launch SciDevHarness: {exc}", file=sys.stderr)
        return 1
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
