"""Project-local lifecycle hooks.

Hooks are opt-in through ``.research/hooks.json``::

    {
      "enabled": true,
      "hooks": {
        "task_start":  [{"command": "python", "args": ["tools/on_start.py"]}],
        "before_tool": [{"command": "python", "args": ["tools/check_tool.py"]}],
        "after_tool":  [{"command": "python", "args": ["tools/log_tool.py"]}],
        "before_commit": [{"command": "python", "args": ["tools/check_commit.py"]}]
      }
    }

Commands run without a shell, inside the workspace, with a bounded timeout.
The event and a bounded JSON context are passed through environment variables.
A non-zero exit from ``task_start``, ``before_tool`` or ``before_commit`` is
blocking; ``after_tool`` failures are recorded but do not mask the tool result.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any

from scidev_process import background_process_options


class HookError(RuntimeError):
    pass


@dataclass(frozen=True)
class HookSpec:
    command: str
    args: list[str]
    timeout: float = 10.0
    cwd: str = "."


@dataclass(frozen=True)
class HookResult:
    event: str
    command: str
    exit_code: int
    stdout: str
    stderr: str


BLOCKING_EVENTS = frozenset({"task_start", "before_tool", "before_commit"})
EVENTS = frozenset({"task_start", "before_tool", "after_tool", "before_commit"})
MAX_OUTPUT = 8_000


class HookRunner:
    def __init__(
        self,
        project_root: Path,
        *,
        ledger: Any = None,
        config_path: Path | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.ledger = ledger
        self.config_path = (
            Path(config_path) if config_path is not None
            else self.project_root / ".research" / "hooks.json"
        )
        self.errors: list[str] = []

    def _log(self, event: str, payload: dict[str, Any]) -> None:
        append = getattr(self.ledger, "append", None)
        if callable(append):
            try:
                append(event, payload)
            except Exception:  # noqa: BLE001 - audit failures must not change hook semantics
                pass

    def _load(self) -> dict[str, list[HookSpec]]:
        if not self.config_path.is_file():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.errors.append(f"无法读取 hooks 配置：{exc}")
            return {}
        if not isinstance(data, dict) or data.get("enabled") is not True:
            return {}
        raw_hooks = data.get("hooks")
        if not isinstance(raw_hooks, dict):
            return {}
        result: dict[str, list[HookSpec]] = {}
        for event, entries in raw_hooks.items():
            if event not in EVENTS or not isinstance(entries, list):
                continue
            specs: list[HookSpec] = []
            for raw in entries[:8]:
                if isinstance(raw, str):
                    parts = shlex.split(raw, posix=False)
                    if not parts:
                        continue
                    command, args = parts[0], parts[1:]
                    timeout = 10.0
                    cwd = "."
                elif isinstance(raw, dict):
                    command = str(raw.get("command") or "").strip()
                    args_raw = raw.get("args") or []
                    args = [str(value) for value in args_raw] if isinstance(args_raw, list) else []
                    try:
                        timeout = float(raw.get("timeout", 10) or 10)
                    except (TypeError, ValueError):
                        timeout = 10.0
                    cwd = str(raw.get("cwd") or ".")
                else:
                    continue
                if not command:
                    continue
                specs.append(HookSpec(
                    command=command,
                    args=args,
                    timeout=max(0.5, min(timeout, 60.0)),
                    cwd=cwd,
                ))
            if specs:
                result[event] = specs
        return result

    def run(self, event: str, context: dict[str, Any] | None = None, *, blocking: bool | None = None) -> list[HookResult]:
        if event not in EVENTS:
            raise HookError(f"未知 hook 事件：{event}")
        specs = self._load().get(event, [])
        if not specs:
            return []
        is_blocking = event in BLOCKING_EVENTS if blocking is None else bool(blocking)
        safe_context = {
            str(key): value
            for key, value in (context or {}).items()
            if isinstance(key, str)
        }
        encoded_context = json.dumps(safe_context, ensure_ascii=False, default=str)
        if len(encoded_context.encode("utf-8")) > 16_000:
            encoded_context = encoded_context[:16_000]
        results: list[HookResult] = []
        for spec in specs:
            cwd = (self.project_root / spec.cwd).resolve()
            try:
                cwd.relative_to(self.project_root)
            except ValueError:
                message = f"hook cwd 必须在工作区内：{spec.cwd}"
                self.errors.append(message)
                if is_blocking:
                    raise HookError(message)
                continue
            environment = os.environ.copy()
            environment["SCIDEV_HOOK_EVENT"] = event
            environment["SCIDEV_HOOK_CONTEXT"] = encoded_context
            try:
                completed = subprocess.run(
                    [spec.command, *spec.args],
                    cwd=cwd,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=spec.timeout,
                    check=False,
                    **background_process_options(),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                message = f"hook {event} 执行失败：{exc}"
                self.errors.append(message)
                self._log("hook_failed", {"event": event, "command": spec.command, "error": message})
                if is_blocking:
                    raise HookError(message) from exc
                continue
            result = HookResult(
                event=event,
                command=spec.command,
                exit_code=int(completed.returncode),
                stdout=str(completed.stdout or "")[-MAX_OUTPUT:],
                stderr=str(completed.stderr or "")[-MAX_OUTPUT:],
            )
            results.append(result)
            self._log("hook_completed", {
                "event": event,
                "command": spec.command,
                "exit_code": result.exit_code,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
            })
            if result.exit_code != 0 and is_blocking:
                raise HookError(
                    f"hook {event} 被拒绝（exit {result.exit_code}）：{result.stderr[-500:] or result.stdout[-500:]}"
                )
        return results
