"""Python completion and definition lookup via Jedi.

Ruff's LSP is used for diagnostics; it does not provide completions. Jedi is a
local Python library, so it works without a long-running language server and
does not send source code over the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any

try:  # pragma: no cover - optional import diagnostics
    import jedi
    import jedi.settings as jedi_settings
except ImportError:  # pragma: no cover
    jedi = None
    jedi_settings = None


class IntelUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Completion:
    name: str
    complete: str
    kind: str
    signature: str = ""
    description: str = ""


@dataclass(frozen=True)
class Definition:
    path: Path
    line: int
    column: int
    name: str


class PythonIntel:
    _active_subprocesses: set = set()

    @classmethod
    def shutdown(cls) -> None:
        """Stop Jedi's compiled-environment subprocess before interpreter exit."""
        if jedi is None:
            return
        try:
            from jedi.api.environment import get_default_environment

            environments = [get_default_environment(), *getattr(cls, "_active_subprocesses", set())]
            for item in environments:
                subprocess = item if hasattr(item, "_kill") else getattr(item, "_subprocess", None)
                if subprocess is not None and not getattr(subprocess, "is_crashed", True):
                    subprocess._kill()
        except Exception:  # noqa: BLE001 - best-effort interpreter cleanup
            pass
        getattr(cls, "_active_subprocesses", set()).clear()

    @classmethod
    def _remember_subprocess(cls, script: Any) -> None:
        environment = getattr(getattr(script, "_inference_state", None), "environment", None)
        subprocess = getattr(environment, "_subprocess", None)
        if subprocess is not None and not getattr(subprocess, "is_crashed", True):
            getattr(cls, "_active_subprocesses", set()).add(subprocess)

    def __init__(self, project_root: Path):
        if jedi is None or jedi_settings is None:
            raise IntelUnavailable("jedi 未安装")
        self.project_root = Path(project_root).resolve()
        cache = self.project_root / ".research" / "jedi-cache"
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            cache = Path(tempfile.gettempdir()) / "scidev-jedi-cache"
            cache.mkdir(parents=True, exist_ok=True)
        jedi_settings.cache_directory = str(cache)

    def _script(self, text: str, path: Path | None = None) -> Any:
        kwargs: dict[str, Any] = {"environment": None}
        if path is not None:
            kwargs["path"] = str(path)
        return jedi.Script(text, **kwargs)

    def complete(self, text: str, line: int, column: int, path: Path | None = None, limit: int = 50) -> list[Completion]:
        script = self._script(text, path)
        try:
            raw = script.complete(max(1, int(line)), max(0, int(column)))
        except Exception:  # noqa: BLE001 - completion is advisory
            return []
        self._remember_subprocess(script)
        seen: set[str] = set()
        result: list[Completion] = []
        for item in raw:
            name = str(getattr(item, "name", "") or "")
            if not name or name in seen:
                continue
            seen.add(name)
            signature = ""
            signatures = getattr(item, "get_signatures", None)
            if callable(signatures):
                try:
                    signature = str(getattr(signatures()[0], "to_string", lambda: "")() or "")[:200]
                except (IndexError, TypeError, ValueError):
                    signature = ""
            result.append(Completion(
                name=name,
                complete=str(getattr(item, "complete", name) or name)[:200],
                kind=str(getattr(item, "type", "") or ""),
                signature=signature,
                description=str(getattr(item, "description", "") or "")[:300],
            ))
            if len(result) >= max(1, int(limit)):
                break
        return result

    def goto(self, text: str, line: int, column: int, path: Path | None = None, limit: int = 20) -> list[Definition]:
        script = self._script(text, path)
        try:
            raw = script.goto(max(1, int(line)), max(0, int(column)))
        except Exception:  # noqa: BLE001 - definition lookup is advisory
            return []
        self._remember_subprocess(script)
        result: list[Definition] = []
        seen: set[tuple[str, int, int]] = set()
        for item in raw:
            module_path = getattr(item, "module_path", None)
            if module_path is None:
                continue
            definition_path = Path(module_path).resolve()
            try:
                definition_path.relative_to(self.project_root)
            except ValueError:
                continue
            definition_line = max(1, int(getattr(item, "line", 1) or 1))
            definition_column = max(1, int(getattr(item, "column", 0) or 0) + 1)
            key = (str(definition_path), definition_line, definition_column)
            if key in seen:
                continue
            seen.add(key)
            result.append(Definition(
                path=definition_path,
                line=definition_line,
                column=definition_column,
                name=str(getattr(item, "name", "") or ""),
            ))
            if len(result) >= max(1, int(limit)):
                break
        return result
