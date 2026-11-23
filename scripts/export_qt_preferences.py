"""Export the old desktop appearance without changing its QSettings."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import sys

from PySide6.QtCore import QSettings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scidev.runtime.preferences import AppearancePreferences

PROFILES = ("studio", "paper", "focus")


def _ratio(value: object) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        result = [float(part) for part in value]
    except (TypeError, ValueError, OverflowError):
        return None
    return result if all(math.isfinite(part) for part in result) else None


def export_settings(settings: QSettings) -> dict:
    settings.setFallbacksEnabled(False)
    current = settings.value("uiProfile", "studio")
    layouts = {}
    for profile in PROFILES:
        saved_v2 = f"workbenchRatios/v2/{profile}"
        key = saved_v2 if settings.contains(saved_v2) else f"workbenchRatios/{profile}"
        value = _ratio(settings.value(key))
        if value is not None:
            layouts[profile] = value
    payload = {"version": 1, "profile": current if current in PROFILES else "studio", "qt_layouts": layouts}
    appearance = AppearancePreferences.load(settings)
    if appearance != AppearancePreferences():
        payload["appearance"] = asdict(appearance)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ini", type=Path, help="Read an explicitly selected Qt INI copy")
    args = parser.parse_args()

    if args.ini is not None:
        if not args.ini.is_file():
            parser.error("The selected Qt INI does not exist.")
        settings = QSettings(str(args.ini), QSettings.Format.IniFormat)
    else:
        settings = QSettings(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, "SciDevHarness", "SciDevHarness")
    payload = export_settings(settings)
    if settings.status() != QSettings.Status.NoError:
        parser.error("Qt could not read the selected settings.")
    with args.output.open("x", encoding="utf-8") as out:
        json.dump(payload, out, ensure_ascii=False, allow_nan=False, indent=2)
        out.write("\n")


if __name__ == "__main__":
    main()
