"""Validated local appearance preferences, independent of Qt widgets."""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
import re


@dataclass(frozen=True)
class AppearancePreferences:
    font_family: str = ""
    font_size: int = 11
    font_weight: int = 400
    control_weight: int = 600
    zoom: int = 0
    accent_color: str = ""
    density: str = "comfortable"
    rail_width: int = 76
    icon_size: int = 30
    explorer_mode: str = "auto"
    editor_font_family: str = "Consolas"
    editor_font_size: int = 12
    editor_tab_size: int = 4
    editor_wrap: bool = False
    line_numbers: bool = True
    highlight_line: bool = True

    @classmethod
    def load(cls, settings) -> "AppearancePreferences":
        defaults = cls()
        values = {field.name: cls.validate(field.name, settings.value(
            f"appearance/{field.name}", getattr(defaults, field.name)
        )) for field in fields(cls)}
        return cls(**values)

    def save(self, settings) -> None:
        for field in fields(self):
            settings.setValue(f"appearance/{field.name}", getattr(self, field.name))
        settings.sync()

    def with_value(self, name: str, value) -> "AppearancePreferences":
        return replace(self, **{name: self.validate(name, value)})

    @staticmethod
    def validate(name: str, value):
        defaults = AppearancePreferences()
        if name not in {field.name for field in fields(defaults)}:
            raise ValueError(f"unknown appearance setting: {name}")
        default = getattr(defaults, name)
        ranges = {
            "font_size": (10, 16), "rail_width": (64, 104), "icon_size": (24, 40),
            "editor_font_size": (10, 28), "editor_tab_size": (2, 8),
        }
        choices = {
            "font_weight": (400, 500, 600), "control_weight": (500, 600, 700),
            "zoom": (0, 90, 100, 110, 125, 150),
            "density": ("comfortable", "compact"), "explorer_mode": ("auto", "show", "hide"),
        }
        if name in ranges or name in ("font_weight", "control_weight", "zoom"):
            try:
                value = int(value)
            except (ValueError, TypeError, OverflowError):
                return default
        if name in ranges:
            low, high = ranges[name]
            return min(high, max(low, value))
        if name in choices:
            return value if value in choices[name] else default
        if isinstance(default, bool):
            if isinstance(value, str):
                return {"true": True, "1": True, "false": False, "0": False}.get(value.lower(), default)
            return value if isinstance(value, bool) else default
        value = str(value or "").strip()
        if name == "accent_color":
            return value.lower() if not value or re.fullmatch(r"#[0-9a-fA-F]{6}", value) else default
        if len(value) > 128 or re.search(r'[\x00-\x1f"\\]', value):
            return default
        return value or default

    @property
    def button_size(self) -> int:
        return self.rail_width - 16

    @property
    def effective_icon_size(self) -> int:
        return min(self.icon_size, self.button_size - 8)
