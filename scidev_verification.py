"""Deterministic bookkeeping for approved commands and current-code check evidence."""

from __future__ import annotations

import ast
import configparser
import hashlib
import json
import os
import re
import shlex
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable


class VerificationTracker:
    """Classify checks and bind their exit status to a bounded workspace fingerprint."""

    MAX_FINGERPRINT_FILES = 500
    MAX_FINGERPRINT_FILE_BYTES = 16_000_000
    MAX_FINGERPRINT_TOTAL_BYTES = 64_000_000
    CODE_SUFFIXES = frozenset({
        ".py", ".pyi", ".ipynb", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
        ".vue", ".svelte", ".c", ".h", ".cc", ".cpp", ".hpp", ".cu", ".cuh",
        ".cs", ".java", ".kt", ".kts", ".scala", ".go", ".rs", ".rb", ".php",
        ".swift", ".m", ".mm", ".r", ".jl", ".lua", ".pl", ".ex", ".exs",
        ".f", ".f90", ".sql", ".sh", ".bat", ".ps1", ".qml", ".html", ".css",
        ".scss", ".toml", ".json", ".yaml", ".yml", ".xml", ".ini", ".cfg",
        ".cmake", ".csproj", ".sln", ".gradle", ".tf",
    })
    CHECK_PATTERNS = (
        ("test", re.compile(
            r"(?:^|[\s;&|])(?:pytest|tox|nox|jest|vitest|ctest)(?:\s|$)"
            r"|(?:^|[\s;&|])(?:\"(?:(?:[A-Za-z]:)?[^\"]*[\\/])?(?:python|py)(?:\.exe)?\"|"
            r"(?:(?:[A-Za-z]:)?[^\s;&|]*[\\/])?(?:python|py)(?:\.exe)?)\s+-m\s+(?:pytest|unittest|doctest)\b"
            r"|(?:^|[\s;&|])(?:npm|pnpm|yarn)\s+(?:run\s+)?tests?\b"
            r"|(?:^|[\s;&|])(?:cargo|go|dotnet)\s+test\b"
            r"|(?:^|[\s;&|])mvn\s+(?:test|verify)\b"
            r"|(?:^|[\s;&|])gradle\s+(?:test|check)\b", re.IGNORECASE,
        )),
        ("lint", re.compile(r"(?:^|[\s;&|])(?:ruff|flake8|pylint|eslint|stylelint)\b|\blint(?:\s|$)", re.IGNORECASE)),
        ("typecheck", re.compile(r"(?:^|[\s;&|])(?:mypy|pyright|tsc)\b|type[- ]?check", re.IGNORECASE)),
        ("compile", re.compile(
            r"(?:^|[\s;&|])(?:\"(?:(?:[A-Za-z]:)?[^\"]*[\\/])?(?:python|py)(?:\.exe)?\"|"
            r"(?:(?:[A-Za-z]:)?[^\s;&|]*[\\/])?(?:python|py)(?:\.exe)?)\s+-m\s+(?:py_compile|compileall)\b"
            r"|\bcompile(?:r)?\b", re.IGNORECASE,
        )),
        ("build", re.compile(r"(?:^|[\s;&|])(?:ninja|make|cmake|msbuild|nuitka)\b|\bbuild(?:ing)?\b", re.IGNORECASE)),
        ("check", re.compile(r"(?:^|[\s;&|])git\s+diff\s+--check\b|\b(?:verify|validation)\b", re.IGNORECASE)),
    )

    @classmethod
    def classify(cls, command: str) -> str:
        text = str(command or "").strip()
        for category, pattern in cls.CHECK_PATTERNS:
            if pattern.search(text):
                return category
        return "command"

    @classmethod
    def requires_check(cls, paths: Iterable[str]) -> bool:
        return any(Path(str(path)).suffix.casefold() in cls.CODE_SUFFIXES for path in paths)

    @classmethod
    def fingerprint(
        cls,
        project_root: Path,
        paths: Iterable[str],
        protected: Callable[[str], bool],
        *,
        scope_complete: bool,
    ) -> dict[str, Any]:
        root = Path(project_root).resolve()
        digest = hashlib.sha256()
        complete = bool(scope_complete)
        total_bytes = 0
        safe_files = 0
        path_fingerprints: dict[str, str] = {}
        candidates = sorted({str(path).replace("\\", "/") for path in paths if isinstance(path, str)})
        if len(candidates) > cls.MAX_FINGERPRINT_FILES:
            candidates = candidates[:cls.MAX_FINGERPRINT_FILES]
            complete = False
        for relative in candidates:
            digest.update(relative.encode("utf-8", errors="surrogatepass") + b"\0")
            path = root.joinpath(*relative.split("/"))
            try:
                if Path(relative).is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")):
                    complete = False
                    digest.update(b"unsafe-path\0")
                    path_fingerprints[relative] = "unsafe-path"
                    continue
                if protected(relative):
                    complete = False
                    digest.update(b"protected\0")
                    path_fingerprints[relative] = "protected"
                    continue
                unsafe_link = False
                for candidate in (path, *path.parents):
                    if candidate == root:
                        break
                    junction = getattr(candidate, "is_junction", None)
                    if candidate.is_symlink() or (callable(junction) and junction()):
                        unsafe_link = True
                        break
                if unsafe_link or not path.resolve().is_relative_to(root):
                    complete = False
                    digest.update(b"linked-path\0")
                    path_fingerprints[relative] = "linked-path"
                    continue
                if not path.exists():
                    digest.update(b"deleted\0")
                    path_fingerprints[relative] = "deleted"
                    continue
                if not path.is_file():
                    complete = False
                    digest.update(b"not-file\0")
                    path_fingerprints[relative] = "not-file"
                    continue
                before = path.stat()
                if (before.st_size > cls.MAX_FINGERPRINT_FILE_BYTES or
                        total_bytes + before.st_size > cls.MAX_FINGERPRINT_TOTAL_BYTES):
                    complete = False
                    digest.update(f"size:{before.st_size};mtime:{before.st_mtime_ns};ctime:{before.st_ctime_ns}".encode())
                    path_fingerprints[relative] = f"oversize:{before.st_size}:{before.st_mtime_ns}:{before.st_ctime_ns}"
                    continue
                raw = path.read_bytes()
                after = path.stat()
                total_bytes += len(raw)
                safe_files += 1
                content_hash = hashlib.sha256(raw).hexdigest()
                path_fingerprints[relative] = content_hash
                digest.update(bytes.fromhex(content_hash))
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns,
                ):
                    complete = False
                    digest.update(b"changed-during-read\0")
            except (OSError, ValueError, RuntimeError):
                complete = False
                digest.update(b"unavailable\0")
                path_fingerprints[relative] = "unavailable"
        return {
            "fingerprint": digest.hexdigest(),
            "complete": complete,
            "file_count": safe_files,
            "total_bytes": total_bytes,
            "path_fingerprints": path_fingerprints,
        }

    @classmethod
    def record(
        cls,
        execution: dict[str, Any],
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        edit_revision: int,
        turn: int,
    ) -> dict[str, Any]:
        command = str(execution.get("command") or "")[:1000]
        category = cls.classify(command)
        execution_status = str(execution.get("status") or "not_run")
        stable = before.get("fingerprint") == after.get("fingerprint")
        if category == "command":
            check_status = "not_a_check"
        elif execution_status == "passed":
            if not stable:
                check_status = "workspace_changed"
            elif not before.get("complete") or not after.get("complete"):
                check_status = "scope_limited"
            else:
                check_status = "passed"
        elif execution_status == "failed":
            check_status = "failed"
        elif execution_status == "timed_out":
            check_status = "timed_out"
        elif execution_status == "start_failed":
            check_status = "start_failed"
        else:
            check_status = "not_run"
        return {
            "command_id": str(execution.get("command_id") or ""),
            "command": command,
            "kind": category,
            "execution_status": execution_status,
            "approval_status": str(execution.get("approval_status") or ""),
            "check_status": check_status,
            "exit_code": execution.get("exit_code"),
            "timeout_seconds": execution.get("timeout_seconds"),
            "elapsed_ms": execution.get("elapsed_ms"),
            "started_at": execution.get("started_at", ""),
            "finished_at": execution.get("finished_at", ""),
            "edit_revision": int(edit_revision),
            "turn": int(turn),
            "fingerprint_before": before.get("fingerprint", ""),
            "fingerprint_after": after.get("fingerprint", ""),
            "scope_complete": bool(before.get("complete") and after.get("complete")),
            "changed_during_check": not stable,
        }

    @classmethod
    def summarize(
        cls,
        records: Iterable[dict[str, Any]],
        current: dict[str, Any],
        *,
        edit_revision: int,
    ) -> dict[str, Any]:
        all_records = [record for record in records if isinstance(record, dict)]
        checks = [record for record in all_records if record.get("kind") != "command"]
        latest = checks[-1] if checks else None
        matching_pass = any(
            record.get("check_status") == "passed"
            and record.get("scope_complete")
            and record.get("edit_revision") == edit_revision
            and record.get("fingerprint_after") == current.get("fingerprint")
            and current.get("complete")
            for record in checks
        )
        latest_failure = bool(
            latest
            and latest.get("edit_revision") == edit_revision
            and latest.get("check_status") in {"failed", "timed_out", "start_failed"}
        )
        if latest_failure:
            status = str(latest["check_status"])
        elif matching_pass:
            status = "passed"
        elif latest and latest.get("edit_revision", -1) != edit_revision:
            status = "stale"
        elif latest and latest.get("check_status") == "scope_limited":
            status = "scope_limited"
        elif latest and latest.get("check_status") == "workspace_changed":
            status = "stale"
        elif (latest and latest.get("check_status") == "passed"
              and latest.get("fingerprint_after") != current.get("fingerprint")):
            status = "stale"
        elif latest and latest.get("check_status") == "passed" and not current.get("complete"):
            status = "scope_limited"
        elif latest and latest.get("check_status") == "not_run":
            status = "not_run"
        else:
            status = "not_run"
        return {
            "status": status,
            "command_count": len(all_records),
            "check_count": len(checks),
            "failed_attempts": sum(record.get("check_status") in {"failed", "timed_out", "start_failed"} for record in checks),
            "latest": latest,
            "scope_complete": bool(current.get("complete")),
            "current_fingerprint": current.get("fingerprint", ""),
            "edit_revision": int(edit_revision),
        }

    @staticmethod
    def label(status: str) -> str:
        return {
            "passed": "检查通过（对应当前代码）",
            "failed": "检查失败",
            "timed_out": "检查超时",
            "start_failed": "检查未能启动",
            "stale": "检查结果已过时（之后有代码变化）",
            "scope_limited": "检查命令通过，但当前代码范围无法完整核验",
            "workspace_changed": "检查过程中代码发生变化，结果不适用于当前代码",
            "not_run": "未运行项目检查",
        }.get(str(status), "未运行项目检查")


class TestCommandDiscovery:
    """Infer bounded test-command suggestions from project files without executing them."""

    MAX_PROJECT_FILES = 5000
    MAX_TEST_FILES = 160
    MAX_FILE_BYTES = 512_000
    MAX_TOTAL_BYTES = 2_000_000
    MAX_CANDIDATES = 8
    PYTHON_TEST = re.compile(r"(?:^|/)test_[^/]+\.py$|(?:^|/)[^/]+_test\.py$", re.IGNORECASE)
    PYTHON_TEST_CASE = re.compile(
        r"class\s+[A-Za-z_]\w*\s*\([^)]*(?:unittest\.)?TestCase\s*\)", re.MULTILINE,
    )
    PYTEST_IMPORT = re.compile(r"^\s*(?:import\s+pytest|from\s+pytest\s+import\s+)", re.MULTILINE)
    PYTEST_FUNCTION = re.compile(r"^(?:async\s+)?def\s+test_[A-Za-z_]\w*\s*\(", re.MULTILINE)
    PYTEST_CLASS = re.compile(r"^class\s+Test[A-Za-z_]\w*\s*(?:\(([^)]*)\))?\s*:", re.MULTILINE)

    @staticmethod
    def _parent(relative: str) -> str:
        parent = PurePosixPath(relative).parent.as_posix()
        return "" if parent == "." else parent

    @staticmethod
    def _under(relative: str, directory: str) -> bool:
        return directory == "" or relative == directory or relative.startswith(directory + "/")

    @staticmethod
    def _shell_path(relative: str) -> str | None:
        """Quote a workspace-relative path for the configured shell, failing closed on metacharacters."""
        if relative and TestCommandDiscovery._relative(relative) is None:
            return None
        if os.name == "nt":
            if any(character in relative for character in '%!^&|<>()"\r\n'):
                return None
            return f'"{relative}"' if any(character.isspace() for character in relative) else relative
        return shlex.quote(relative) if relative else "."

    @classmethod
    def _at_directory(cls, directory: str, command: str) -> str | None:
        if not directory:
            return command
        quoted = cls._shell_path(directory)
        if quoted is None:
            return None
        change_directory = f"cd /d {quoted}" if os.name == "nt" else f"cd {quoted}"
        return f"{change_directory} && {command}"

    @classmethod
    def _node_manager(cls, package: dict[str, Any], directory: str, file_paths: dict[str, str]) -> str | None:
        declared = package.get("packageManager")
        if isinstance(declared, str):
            match = re.match(r"\s*(npm|pnpm|yarn)(?:@|$)", declared, re.IGNORECASE)
            if match:
                return match.group(1).casefold()
            if declared.strip():
                return None
        current = directory
        while True:
            managers = set()
            for filename, manager in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
                                      ("package-lock.json", "npm"), ("npm-shrinkwrap.json", "npm")):
                key = f"{current}/{filename}" if current else filename
                if key.casefold() in file_paths:
                    managers.add(manager)
            if managers:
                return next(iter(managers)) if len(managers) == 1 else None
            if not current:
                return "npm"
            current = cls._parent(current)

    @classmethod
    def _node_command(cls, manager: str, directory: str, script: str) -> str | None:
        quoted = cls._shell_path(directory) if directory else ""
        if quoted is None:
            return None
        if manager == "pnpm":
            prefix = f"pnpm --dir {quoted} " if directory else "pnpm "
        elif manager == "yarn":
            prefix = f"yarn --cwd {quoted} " if directory else "yarn "
        else:
            prefix = f"npm --prefix {quoted} " if directory else "npm "
        return f"{prefix}{script}"

    @staticmethod
    def _relative(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        raw = value.replace("\\", "/")
        parts = raw.split("/")
        if (not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw)
                or any(part in {"", ".", ".."} for part in parts)):
            return None
        return PurePosixPath(*parts).as_posix()

    @staticmethod
    def _python_prefix(root: Path) -> str:
        windows = os.name == "nt"
        relative_candidates = [
            (".venv/Scripts/python.exe" if windows else ".venv/bin/python"),
            ("venv/Scripts/python.exe" if windows else "venv/bin/python"),
            ("env/Scripts/python.exe" if windows else "env/bin/python"),
        ]
        for relative in relative_candidates:
            path = root.joinpath(*PurePosixPath(relative).parts)
            try:
                current = root
                unsafe = False
                for part in PurePosixPath(relative).parts:
                    current = current / part
                    junction = getattr(current, "is_junction", None)
                    if current.is_symlink() or (callable(junction) and junction()):
                        unsafe = True
                        break
                resolved = path.resolve(strict=True)
                if unsafe or not resolved.is_relative_to(root) or not resolved.is_file():
                    continue
            except (OSError, ValueError, RuntimeError):
                continue
            return ".\\" + relative.replace("/", "\\") if windows else "./" + relative
        return "python"

    @classmethod
    def discover(
        cls,
        project_root: Path,
        project_files: Iterable[str],
        *,
        content_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return commands supported by static evidence; this function never runs them."""
        root = Path(project_root).resolve()
        python_prefix = cls._python_prefix(root)
        limited = False
        file_paths: dict[str, str] = {}
        for raw in project_files:
            relative = cls._relative(raw)
            if relative is not None:
                file_paths.setdefault(relative.casefold(), relative)
            if len(file_paths) > cls.MAX_PROJECT_FILES:
                limited = True
                file_paths = dict(list(file_paths.items())[:cls.MAX_PROJECT_FILES])
                break

        overrides: dict[str, str] = {}
        for raw, text in (content_overrides or {}).items():
            relative = cls._relative(raw)
            if relative is not None and isinstance(text, str) and "\x00" not in text:
                overrides[relative.casefold()] = text

        cache: dict[str, str | None] = {}
        total_bytes = 0

        def read_text(relative: str) -> str | None:
            nonlocal total_bytes, limited
            key = relative.casefold()
            if key in cache:
                return cache[key]
            if key in overrides:
                text = overrides[key]
                size = len(text.encode("utf-8"))
                if size > cls.MAX_FILE_BYTES or total_bytes + size > cls.MAX_TOTAL_BYTES:
                    limited = True
                    cache[key] = None
                    return None
                total_bytes += size
                cache[key] = text
                return text
            listed = file_paths.get(key)
            if listed is None:
                cache[key] = None
                return None
            path = root.joinpath(*PurePosixPath(listed).parts)
            try:
                current = root
                for part in PurePosixPath(listed).parts:
                    current = current / part
                    junction = getattr(current, "is_junction", None)
                    if current.is_symlink() or (callable(junction) and junction()):
                        cache[key] = None
                        return None
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    cache[key] = None
                    return None
                before = resolved.stat()
                if before.st_size > cls.MAX_FILE_BYTES or total_bytes + before.st_size > cls.MAX_TOTAL_BYTES:
                    limited = True
                    cache[key] = None
                    return None
                raw = resolved.read_bytes()
                after = resolved.stat()
                if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                        (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    limited = True
                    cache[key] = None
                    return None
                text = raw.decode("utf-8")
                total_bytes += len(raw)
                cache[key] = text
                return text
            except (OSError, UnicodeError, ValueError, RuntimeError):
                cache[key] = None
                return None

        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add(name: str, command: str, reason: str, evidence: Iterable[str]) -> None:
            nonlocal limited
            key = (name, command)
            if key in seen:
                return
            if len(candidates) >= cls.MAX_CANDIDATES:
                limited = True
                return
            seen.add(key)
            candidates.append({
                "name": name,
                "command": command,
                "reason": reason,
                "evidence": sorted(set(evidence))[:4],
            })

        all_files = sorted(file_paths.values(), key=str.casefold)
        python_tests = [
            path for path in all_files
            if path.casefold().endswith(".py") and (
                cls.PYTHON_TEST.search(path) or any(part.casefold() == "tests" for part in path.split("/"))
            )
        ]
        if len(python_tests) > cls.MAX_TEST_FILES:
            limited = True
            python_tests = python_tests[:cls.MAX_TEST_FILES]
        python_texts = [(path, read_text(path)) for path in python_tests]
        python_texts = [(path, text) for path, text in python_texts if text is not None]
        unittest_evidence = [path for path, text in python_texts if cls.PYTHON_TEST_CASE.search(text)]

        pytest_dirs: dict[str, list[str]] = {}
        declared_pytest_dirs: dict[str, list[str]] = {}
        for manifest in all_files:
            basename = PurePosixPath(manifest).name.casefold()
            directory = cls._parent(manifest)
            if basename == "pyproject.toml":
                text = read_text(manifest) or ""
                try:
                    data = tomllib.loads(text)
                    tool = data.get("tool", {})
                    project = data.get("project", {})
                    dependencies: list[str] = []
                    if isinstance(project, dict):
                        dependencies.extend(value for value in project.get("dependencies", []) if isinstance(value, str))
                        optional = project.get("optional-dependencies", {})
                        if isinstance(optional, dict):
                            dependencies.extend(
                                value for group in optional.values() if isinstance(group, list)
                                for value in group if isinstance(value, str)
                            )
                    if ((isinstance(tool, dict) and isinstance(tool.get("pytest"), dict)) or
                            any(re.match(r"\s*pytest(?:[\s<>=!~;\[]|$)", value, re.IGNORECASE)
                                for value in dependencies)):
                        declared_pytest_dirs[directory] = [manifest]
                except (tomllib.TOMLDecodeError, TypeError, ValueError, RecursionError):
                    pass
            elif basename in {"pytest.ini", "setup.cfg"}:
                text = read_text(manifest) or ""
                if re.search(r"(?im)^\s*\[(?:tool:pytest|pytest)\]\s*$", text):
                    declared_pytest_dirs[directory] = [manifest]
            elif basename in {"requirements.txt", "requirements-dev.txt", "requirements-test.txt"}:
                text = read_text(manifest) or ""
                if any(re.match(r"\s*pytest(?:[\s<>=!~;\[]|$)", line.split("#", 1)[0], re.IGNORECASE)
                       for line in text.splitlines()):
                    declared_pytest_dirs[directory] = [manifest]

        for path, text in python_texts:
            has_pytest_style = bool(
                cls.PYTEST_IMPORT.search(text) or cls.PYTEST_FUNCTION.search(text) or
                any("testcase" not in (match.group(1) or "").casefold()
                    for match in cls.PYTEST_CLASS.finditer(text))
            )
            applicable = [directory for directory in declared_pytest_dirs if cls._under(path, directory)]
            if has_pytest_style or applicable:
                directory = max(applicable, key=len) if applicable else ""
                pytest_dirs.setdefault(directory, []).append(path)
        for directory, evidence in sorted(pytest_dirs.items()):
            scoped_tests = sorted(set(evidence))
            manifest_evidence = declared_pytest_dirs.get(directory, [])
            command = f"{python_prefix} -m pytest"
            if directory:
                path_arg = cls._shell_path(directory)
                if path_arg is None:
                    limited = True
                    continue
                command += f" {path_arg}"
            add("Python · pytest" + (f" ({directory})" if directory else ""), command,
                "检测到 Python 测试用法或 pytest 配置/依赖；嵌套项目候选限定到该目录。",
                [*scoped_tests[:2], *manifest_evidence])

        if unittest_evidence:
            roots: dict[str, list[str]] = {}
            for path in unittest_evidence:
                parts = path.split("/")[:-1]
                test_index = next((index for index, part in enumerate(parts) if part.casefold() == "tests"), None)
                test_root = "/".join(parts[:test_index + 1]) if test_index is not None else "/".join(parts)
                test_root = test_root or "."
                roots.setdefault(test_root, []).append(path)
            for test_root, evidence in sorted(roots.items()):
                if test_root != "." and not re.fullmatch(r"[A-Za-z0-9._/-]+", test_root):
                    limited = True
                    continue
                command = f"{python_prefix} -m unittest discover -s {test_root} -v"
                add(f"Python · unittest ({test_root})", command,
                    "检测到 unittest.TestCase 测试类；仅覆盖该测试根目录。", evidence)

        package_files = [path for path in all_files if PurePosixPath(path).name.casefold() == "package.json"]
        for manifest in package_files:
            try:
                package = json.loads(read_text(manifest) or "")
                scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
                if not isinstance(scripts, dict):
                    continue
                directory = cls._parent(manifest)
                manager = cls._node_manager(package, directory, file_paths)
                if manager is None:
                    limited = True
                    continue
                lock_evidence = []
                current = directory
                while True:
                    lock_evidence = [f"{current}/{name}" if current else name for name in
                                     ("pnpm-lock.yaml", "yarn.lock", "package-lock.json", "npm-shrinkwrap.json")
                                     if (f"{current}/{name}" if current else name).casefold() in file_paths]
                    if lock_evidence or not current:
                        break
                    current = cls._parent(current)
                test_script = scripts.get("test")
                if (isinstance(test_script, str) and test_script.strip() and
                        "no test specified" not in test_script.casefold()):
                    command = cls._node_command(manager, directory, "test")
                    if command is None:
                        limited = True
                    else:
                        add("JavaScript · package test" + (f" ({directory})" if directory else ""), command,
                            "该 package.json 定义了 test 脚本；脚本正文不会执行或展开。",
                            [manifest, *lock_evidence])
                custom_scripts = sorted(
                    name for name, body in scripts.items()
                    if isinstance(name, str) and re.fullmatch(r"test(?::|-|_)[A-Za-z0-9:_-]+", name)
                    and isinstance(body, str) and body.strip()
                )
                for script in custom_scripts[:3]:
                    command = cls._node_command(manager, directory, f"run {script}")
                    if command is None:
                        limited = True
                    else:
                        add(f"JavaScript · {script}" + (f" ({directory})" if directory else ""), command,
                            "发现明确命名的测试脚本；不会读取或展开脚本正文。", [manifest, *lock_evidence])
            except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
                continue

        go_tests = [path for path in all_files if path.casefold().endswith("_test.go")]
        go_manifests = [path for path in all_files if PurePosixPath(path).name.casefold() == "go.mod"]
        go_module_dirs = [(manifest, cls._parent(manifest)) for manifest in go_manifests]
        for manifest, directory in go_module_dirs:
            module_tests = [path for path in go_tests if cls._under(path, directory)]
            if any(other_dir != directory and cls._under(path, other_dir)
                   for path in module_tests for _, other_dir in go_module_dirs):
                module_tests = [path for path in module_tests if not any(
                    other_dir != directory and cls._under(path, other_dir)
                    for _, other_dir in go_module_dirs
                )]
            if module_tests:
                command = cls._at_directory(directory, "go test ./...")
                if command is None:
                    limited = True
                else:
                    add("Go" + (f" ({directory})" if directory else ""), command,
                        "检测到同一 Go module 内的 go.mod 和 *_test.go。", [manifest, *module_tests[:2]])

        rust_files = [path for path in all_files if path.casefold().endswith(".rs") and
                      ("/src/" in f"/{path.casefold()}/" or "/tests/" in f"/{path.casefold()}/")]
        if len(rust_files) > cls.MAX_TEST_FILES:
            limited = True
            rust_files = rust_files[:cls.MAX_TEST_FILES]
        rust_manifests = [path for path in all_files if PurePosixPath(path).name.casefold() == "cargo.toml"]
        cargo_data: dict[str, dict[str, Any]] = {}
        for manifest in rust_manifests:
            try:
                parsed = tomllib.loads(read_text(manifest) or "")
                if isinstance(parsed, dict):
                    cargo_data[manifest] = parsed
            except (tomllib.TOMLDecodeError, TypeError, ValueError, RecursionError):
                continue
        workspace_roots = [manifest for manifest, data in cargo_data.items()
                           if isinstance(data.get("workspace"), dict)]
        rust_groups: dict[str, list[str]] = {}
        for path in rust_files:
            source = read_text(path) or ""
            if "/tests/" not in f"/{path.casefold()}/" and not re.search(
                    r"(?m)^\s*#\[(?:cfg\(test\)|test)\]", source):
                continue
            ancestors = [(manifest, cls._parent(manifest)) for manifest in rust_manifests
                         if cls._under(path, cls._parent(manifest))]
            if not ancestors:
                continue
            workspace_ancestors = [(manifest, directory) for manifest, directory in ancestors
                                   if manifest in workspace_roots]
            manifest, directory = max(workspace_ancestors or ancestors, key=lambda item: len(item[1]))
            rust_groups.setdefault(manifest, []).append(path)
        for manifest, evidence in sorted(rust_groups.items()):
            directory = cls._parent(manifest)
            if directory:
                manifest_arg = cls._shell_path(manifest)
                if manifest_arg is None:
                    limited = True
                    continue
                command = f"cargo test --manifest-path {manifest_arg}"
            else:
                command = "cargo test"
            add("Rust · Cargo" + (f" ({directory})" if directory else ""), command,
                "检测到 Cargo 清单及同一 package/workspace 内的测试目录或测试属性。",
                [manifest, *sorted(set(evidence))[:2]])

        java_tests = [path for path in all_files if path.casefold().endswith((".java", ".kt", ".scala")) and
                      ("/src/test/" in f"/{path.casefold()}/" or
                       re.search(r"(?:^|/)[^/]*(?:test|tests)[^/]*\.(?:java|kt|scala)$", path, re.IGNORECASE))]
        maven_manifests = [path for path in all_files if PurePosixPath(path).name.casefold() == "pom.xml"]
        for manifest in maven_manifests:
            directory = cls._parent(manifest)
            module_tests = [path for path in java_tests if path.casefold().endswith(".java") and
                            cls._under(path, directory) and not any(
                                cls._parent(other) != directory and cls._under(path, cls._parent(other))
                                and len(cls._parent(other)) > len(directory) for other in maven_manifests
                            )]
            if module_tests:
                manifest_arg = cls._shell_path(manifest)
                if manifest_arg is None:
                    limited = True
                    continue
                command = "mvn test" if not directory else f"mvn -f {manifest_arg} test"
                add("Java · Maven" + (f" ({directory})" if directory else ""), command,
                    "检测到同一 Maven 项目内的 pom.xml 和 src/test Java 源码。",
                    [manifest, *module_tests[:2]])

        gradle_manifests = [path for path in all_files if PurePosixPath(path).name.casefold() in
                            {"build.gradle", "build.gradle.kts"}]
        for manifest in gradle_manifests:
            directory = cls._parent(manifest)
            module_tests = [path for path in java_tests if cls._under(path, directory) and not any(
                cls._parent(other) != directory and cls._under(path, cls._parent(other))
                and len(cls._parent(other)) > len(directory) for other in gradle_manifests
            )]
            if not module_tests:
                continue
            wrapper_dir = ""
            wrapper_name = ""
            ancestor = directory
            while True:
                available = [name for name in ("gradlew.bat", "gradlew")
                             if (f"{ancestor}/{name}" if ancestor else name).casefold() in file_paths]
                if available:
                    wrapper_dir = ancestor
                    wrapper_name = "gradlew.bat" if os.name == "nt" and "gradlew.bat" in available else available[0]
                    break
                if not ancestor:
                    break
                ancestor = cls._parent(ancestor)
            project_arg = cls._shell_path(directory) if directory else ""
            if directory and project_arg is None:
                limited = True
                continue
            if wrapper_name:
                wrapper_relative = f"{wrapper_dir}/{wrapper_name}" if wrapper_dir else wrapper_name
                wrapper_arg = cls._shell_path(wrapper_relative)
                if wrapper_arg is None:
                    limited = True
                    continue
                if os.name == "nt":
                    launcher = f".\\{wrapper_relative.replace('/', chr(92))}"
                else:
                    launcher = f"./{wrapper_relative}"
                command = launcher + (f" -p {project_arg}" if directory else "") + " test"
            else:
                command = f"gradle -p {project_arg} test" if directory else "gradle test"
            add("JVM · Gradle" + (f" ({directory})" if directory else ""), command,
                "检测到同一 Gradle 项目内的构建清单和 JVM 测试源码。",
                [manifest, *module_tests[:2]])

        dotnet_solutions = [path for path in all_files if path.casefold().endswith((".sln", ".slnx"))]
        dotnet_test_projects = [path for path in all_files if path.casefold().endswith(".csproj") and
                                re.search(r"(?:^|[./_-])tests?(?:[./_-]|$)", path, re.IGNORECASE)]
        if len(dotnet_solutions) == 1 and dotnet_test_projects:
            solution = dotnet_solutions[0]
            solution_arg = cls._shell_path(solution)
            if solution_arg is not None:
                command = "dotnet test" if "/" not in solution else f"dotnet test {solution_arg}"
                add(".NET", command, "检测到解决方案和测试项目清单。",
                    [solution, *dotnet_test_projects[:2]])
        elif dotnet_test_projects:
            for project in dotnet_test_projects:
                project_arg = cls._shell_path(project)
                if project_arg is None:
                    limited = True
                    continue
                add(".NET" + (f" ({project})"), f"dotnet test {project_arg}",
                    "检测到名称符合测试约定的 .NET 项目清单。", [project])

        make_targets = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*:(?:\s|$)", re.MULTILINE)
        for manifest in (path for path in all_files if PurePosixPath(path).name.casefold() in
                         {"makefile", "gnumakefile"}):
            directory = cls._parent(manifest)
            text = read_text(manifest) or ""
            for target in sorted(set(make_targets.findall(text))):
                if not re.fullmatch(r"(?:test|tests|check)(?:[:_-][A-Za-z0-9_-]+)?", target, re.IGNORECASE):
                    continue
                directory_arg = cls._shell_path(directory) if directory else ""
                if directory and directory_arg is None:
                    limited = True
                    continue
                command = f"make -C {directory_arg} {target}" if directory else f"make {target}"
                add("Make · " + target + (f" ({directory})" if directory else ""), command,
                    "Makefile 声明了明确的 test/check 目标；目标配方不会执行或展开。", [manifest])

        for manifest in (path for path in all_files if PurePosixPath(path).name.casefold() == "justfile"):
            directory = cls._parent(manifest)
            text = read_text(manifest) or ""
            for target in sorted(set(re.findall(r"(?m)^\s*([A-Za-z0-9_-]+)(?:\s+[^:\n]+)?\s*:", text))):
                if not re.fullmatch(r"(?:test|tests|check)(?:[-_][A-Za-z0-9_-]+)?", target, re.IGNORECASE):
                    continue
                manifest_arg = cls._shell_path(manifest)
                if manifest_arg is None:
                    limited = True
                    continue
                command = f"just --justfile {manifest_arg} {target}"
                add("Just · " + target + (f" ({directory})" if directory else ""), command,
                    "Justfile 声明了明确的 test/check recipe；recipe 正文不会执行或展开。", [manifest])

        tox_files = [path for path in all_files if PurePosixPath(path).name.casefold() == "tox.ini"]
        for manifest in tox_files:
            config = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                config.read_string(read_text(manifest) or "")
            except (configparser.Error, ValueError):
                continue
            for section in config.sections():
                match = re.fullmatch(r"testenv(?::(.+))?", section, re.IGNORECASE)
                commands_text = config.get(section, "commands", fallback="")
                if not match or not re.search(r"\b(?:pytest|unittest|doctest|nose2?)\b", commands_text, re.IGNORECASE):
                    continue
                directory = cls._parent(manifest)
                manifest_arg = cls._shell_path(manifest)
                if manifest_arg is None:
                    limited = True
                    continue
                env = match.group(1)
                if env and not re.fullmatch(r"[A-Za-z0-9._-]+", env):
                    limited = True
                    continue
                command = f"tox -c {manifest_arg}" + (f" -e {env}" if env else "")
                add("Python · tox" + (f" ({env})" if env else ""), command,
                    "tox 的 testenv commands 静态包含测试运行器；配置命令不会执行或展开。", [manifest])

        for manifest in (path for path in all_files if PurePosixPath(path).name.casefold() == "noxfile.py"):
            source = read_text(manifest) or ""
            try:
                tree = ast.parse(source)
            except (SyntaxError, ValueError):
                continue
            directory = cls._parent(manifest)
            manifest_arg = cls._shell_path(manifest)
            if manifest_arg is None:
                limited = True
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                is_session = any(
                    isinstance(target, ast.Attribute) and target.attr == "session" and
                    isinstance(target.value, ast.Name) and target.value.id == "nox"
                    for decorator in node.decorator_list
                    for target in ([decorator.func] if isinstance(decorator, ast.Call) else [decorator])
                )
                body = ast.get_source_segment(source, node) or ""
                if not is_session or not (re.search(r"test", node.name, re.IGNORECASE) or
                                          re.search(r"\b(?:pytest|unittest|doctest|nose2?)\b", body, re.IGNORECASE)):
                    continue
                noxfile_arg = f" -f {manifest_arg}" if directory else ""
                add("Python · nox (" + node.name + ")", f"nox{noxfile_arg} -s {node.name}",
                    "nox session 名称或源码显示测试意图；session 不会执行或展开。", [manifest])

        test_file_count = len(set(python_tests + go_tests + rust_files + java_tests + dotnet_test_projects))
        return {
            "candidates": candidates,
            "test_file_count": test_file_count,
            "limited": limited,
        }

    @staticmethod
    def format(result: dict[str, Any]) -> str:
        candidates = result.get("candidates", [])
        lines = ["静态测试命令建议（仅从文件/配置推断；不会执行命令或安装依赖；执行仍需逐条用户批准）："]
        if candidates:
            for candidate in candidates:
                evidence = ", ".join(candidate.get("evidence", []))
                lines.append(
                    f"- {candidate['name']}: `{candidate['command']}`；{candidate['reason']} 证据：{evidence}。"
                )
        elif result.get("test_file_count"):
            lines.append(f"发现 {result['test_file_count']} 个测试相关文件，但无法可靠推断运行器；请先检查项目说明/配置。")
        else:
            lines.append("未发现足以生成运行命令的测试配置或测试源码；这不代表项目没有测试。")
        if result.get("limited"):
            lines.append("发现过程达到文件数或读取预算上限，候选可能不完整。")
        lines.append("候选命令未实际验证；退出码为 0 也不代表测试覆盖了本次改动。")
        return "\n".join(lines)
