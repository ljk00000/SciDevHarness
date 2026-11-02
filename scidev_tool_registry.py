"""Central binding table for built-in tools.

Schemas live in ``scidev_tool_schema``; this module owns the name -> handler
mapping and the read-only metadata. ``CodingToolbox`` still implements each
handler and its path/policy checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolBinding:
    name: str
    handler: Callable[..., Any]
    read_only: bool = False


READ_ONLY_TOOL_NAMES = frozenset({
    "search_code",
    "get_project_context",
    "list_files",
    "read_file",
    "git_diff",
})


class ToolRegistry:
    def __init__(self, bindings: dict[str, ToolBinding]):
        self._bindings = dict(bindings)

    @classmethod
    def from_toolbox(cls, toolbox: Any) -> "ToolRegistry":
        return cls({
            "search_code": ToolBinding("search_code", toolbox.search_code, True),
            "get_project_context": ToolBinding("get_project_context", toolbox.get_project_context, True),
            "list_files": ToolBinding("list_files", toolbox.list_files, True),
            "read_file": ToolBinding("read_file", toolbox.read_file, True),
            "write_file": ToolBinding("write_file", toolbox.write_file),
            "apply_patch": ToolBinding("apply_patch", toolbox.apply_patch),
            "replace_in_file": ToolBinding("replace_in_file", toolbox.replace_in_file),
            "run_command": ToolBinding("run_command", toolbox.run_command),
            "browser_search": ToolBinding("browser_search", toolbox.browser_agent.search),
            "browser_open": ToolBinding("browser_open", toolbox.browser_agent.open),
            "browser_click": ToolBinding("browser_click", toolbox.browser_agent.click),
            "browser_screenshot": ToolBinding("browser_screenshot", toolbox.browser_agent.screenshot),
            "git_diff": ToolBinding("git_diff", toolbox.git_diff, True),
            "inspect_visual_artifact": ToolBinding(
                "inspect_visual_artifact", toolbox.inspect_visual_artifact,
            ),
        })

    def get(self, name: str) -> ToolBinding | None:
        return self._bindings.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._bindings))

    def read_only_names(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, binding in self._bindings.items() if binding.read_only))
