"""Fail CI if optional PySide6 Addons enter the minimal desktop runtime."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from importlib import metadata


def validate_minimal_qt_distributions(distribution_names: Iterable[str]) -> None:
    normalized = {
        name.strip().casefold().replace("_", "-")
        for name in distribution_names
        if name and name.strip()
    }
    if "pyside6-essentials" not in normalized:
        raise RuntimeError("PySide6-Essentials is not installed")
    if "pyside6-addons" in normalized:
        raise RuntimeError("Unexpected optional distribution PySide6-Addons is installed")


def main() -> int:
    names = [distribution.metadata.get("Name", "") for distribution in metadata.distributions()]
    try:
        validate_minimal_qt_distributions(names)
    except RuntimeError as exc:
        print(f"Minimal Qt runtime check failed: {exc}", file=sys.stderr)
        return 1
    print("Minimal Qt runtime check: Essentials present; Addons absent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
