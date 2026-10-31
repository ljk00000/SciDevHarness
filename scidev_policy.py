"""Execution policy and verification-output diagnostics.

This module is deliberately a *policy* layer, not an operating-system sandbox.
It decides which commands may run unattended and whether file writes are
allowed. Approved arbitrary commands still inherit the current user's
privileges, so callers must describe the limitation accurately in the UI.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class ExecutionMode(str, Enum):
    """How much autonomy the Agent has for writes and shell commands."""

    READ_ONLY = "read-only"
    MANUAL = "manual"
    WORKSPACE_WRITE = "workspace-write"
    FULL_ACCESS = "full-access"

    @classmethod
    def parse(cls, value: object, default: "ExecutionMode | None" = None) -> "ExecutionMode":
        fallback = default or cls.WORKSPACE_WRITE
        if isinstance(value, cls):
            return value
        normalized = str(value or "").strip().lower().replace("_", "-")
        aliases = {
            "readonly": cls.READ_ONLY,
            "read-only": cls.READ_ONLY,
            "read": cls.READ_ONLY,
            "manual": cls.MANUAL,
            "ask": cls.MANUAL,
            "on-request": cls.MANUAL,
            "suggest": cls.MANUAL,
            "workspace": cls.WORKSPACE_WRITE,
            "workspace-write": cls.WORKSPACE_WRITE,
            "workspacewrite": cls.WORKSPACE_WRITE,
            "full": cls.FULL_ACCESS,
            "full-access": cls.FULL_ACCESS,
            "fullaccess": cls.FULL_ACCESS,
            "danger-full-access": cls.FULL_ACCESS,
        }
        return aliases.get(normalized, fallback)


@dataclass(frozen=True)
class CommandAssessment:
    """Classification used to decide whether a command may run unattended."""

    category: str
    auto_approvable: bool
    reason: str = ""


@dataclass(frozen=True)
class ExecutionPolicy:
    """Policy selected by the user for one coding session."""

    mode: ExecutionMode = ExecutionMode.WORKSPACE_WRITE

    @property
    def can_write_files(self) -> bool:
        return self.mode is not ExecutionMode.READ_ONLY

    @property
    def can_run_commands(self) -> bool:
        return self.mode is not ExecutionMode.READ_ONLY

    def auto_approves(self, assessment: CommandAssessment) -> bool:
        if self.mode in {ExecutionMode.READ_ONLY, ExecutionMode.MANUAL}:
            return False
        if self.mode is ExecutionMode.FULL_ACCESS:
            return True
        return bool(assessment.auto_approvable)

    def label(self) -> str:
        return {
            ExecutionMode.READ_ONLY: "只读",
            ExecutionMode.MANUAL: "手动审批",
            ExecutionMode.WORKSPACE_WRITE: "工作区可写",
            ExecutionMode.FULL_ACCESS: "完全访问",
        }[self.mode]


# Commands that are commonly used to validate a change.  The patterns are
# intentionally narrow and anchored: a command is auto-approved only when the
# whole command is one of these forms, without shell chaining or redirection.
_VERIFICATION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("test", re.compile(
        r"^(?:(?:python|py)(?:\.exe)?(?:\s+-\S+)*\s+-m\s+)?(?:pytest|unittest)(?:\s|$)",
        re.IGNORECASE,
    )),
    ("test", re.compile(r"^(?:npm|pnpm|yarn)\s+(?:run\s+)?test(?::[A-Za-z0-9_.-]+)?(?:\s|$)", re.IGNORECASE)),
    ("test", re.compile(r"^(?:cargo\s+test|go\s+test|dotnet\s+test|mvn\s+test|gradle\s+test)(?:\s|$)", re.IGNORECASE)),
    ("test", re.compile(r"^(?:tox|nox)(?:\s|$)", re.IGNORECASE)),
    ("test", re.compile(r"^\./mvnw\s+test(?:\s|$)", re.IGNORECASE)),
    ("test", re.compile(r"^\./gradlew\s+test(?:\s|$)", re.IGNORECASE)),
    ("lint", re.compile(
        r"^(?:(?:python|py)(?:\.exe)?(?:\s+-\S+)*\s+-m\s+)?(?:ruff\s+check|flake8|pylint)(?:\s|$)",
        re.IGNORECASE,
    )),
    ("lint", re.compile(r"^(?:eslint|npx\s+eslint|cargo\s+clippy|go\s+vet)(?:\s|$)", re.IGNORECASE)),
    ("lint", re.compile(r"^(?:npm|pnpm|yarn)\s+run\s+lint(?::[A-Za-z0-9_.-]+)?(?:\s|$)", re.IGNORECASE)),
    ("typecheck", re.compile(
        r"^(?:(?:python|py)(?:\.exe)?(?:\s+-\S+)*\s+-m\s+)?(?:mypy|pyright)(?:\s|$)",
        re.IGNORECASE,
    )),
    ("typecheck", re.compile(r"^(?:npx\s+)?tsc\s+--noEmit(?:\s|$)", re.IGNORECASE)),
    ("typecheck", re.compile(r"^(?:npm|pnpm|yarn)\s+run\s+typecheck(?::[A-Za-z0-9_.-]+)?(?:\s|$)", re.IGNORECASE)),
)

_UNSAFE_SHELL = re.compile(r"[;&|<>`\r\n]")
_UNSAFE_SUBSTITUTION = re.compile(r"\$\(|\$\{")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_ABSOLUTE_PATH = re.compile(r"(^|\s)(?:[A-Za-z]:[\\/]|/|~[\\/])")
_PARENT_PATH = re.compile(r"(^|[\s/\\])\.\.(?:[\s/\\]|$)")
_NETWORK_BINARY = re.compile(
    r"(^|[\s/\\])(?:curl|wget|invoke-webrequest|iwr|invoke-restmethod|irm|ssh|scp|sftp|nc|ncat|telnet)(?:\s|$)",
    re.IGNORECASE,
)


def _unsafe_reason(command: str) -> str:
    if _UNSAFE_SHELL.search(command):
        return "包含 shell 链接、管道或重定向"
    if _UNSAFE_SUBSTITUTION.search(command):
        return "包含命令替换"
    if _ENV_ASSIGNMENT.match(command):
        return "包含环境变量赋值"
    if _ABSOLUTE_PATH.search(command):
        return "包含绝对路径"
    if _PARENT_PATH.search(command):
        return "包含上级目录路径"
    if _NETWORK_BINARY.search(command):
        return "包含网络下载或远程连接命令"
    return ""


def assess_command(command: object) -> CommandAssessment:
    """Classify a shell command without executing it.

    Auto-approval is a convenience for verification commands, not a security
    boundary. Anything that can compose arbitrary shell behavior is excluded.
    """
    if not isinstance(command, str):
        return CommandAssessment("invalid", False, "命令必须是字符串")
    text = command.strip()
    if not text:
        return CommandAssessment("empty", False, "命令为空")
    unsafe = _unsafe_reason(text)
    if unsafe:
        return CommandAssessment("shell", False, unsafe)
    normalized = " ".join(text.split())
    for category, pattern in _VERIFICATION_PATTERNS:
        if pattern.match(normalized):
            return CommandAssessment(category, True, f"识别的 {category} 命令")
    return CommandAssessment("other", False, "不是可自动批准的验证命令")


@dataclass(frozen=True)
class Diagnostic:
    file: str
    line: int
    column: int
    message: str
    severity: str = "error"
    code: str = ""

    def render(self) -> str:
        location = f"{self.file}:{self.line}" if self.line else self.file
        if self.column:
            location += f":{self.column}"
        prefix = f"[{self.code}] " if self.code else ""
        severity = "" if self.severity == "error" else f"[{self.severity}] "
        return f"- {location} {severity}{prefix}{self.message[:240]}".rstrip()


_PYTEST_FAILED = re.compile(r"^FAILED\s+(?P<file>[^\s:]+)(?:::|\s|$)(?P<rest>.*)$")
_PYTEST_ERROR = re.compile(r"^ERROR\s+(?P<file>[^\s:]+)(?:::|\s|$)(?P<rest>.*)$")
_PYTEST_LOCATION = re.compile(r"^(?P<file>[^\s:]+\.py):(?P<line>\d+):\s*(?P<message>.+)$")
_TSC_LOCATION = re.compile(
    r"^(?P<file>[^(]+)\((?P<line>\d+),(?P<column>\d+)\):\s*(?:error\s+)?(?P<code>TS\d+)?:?\s*(?P<message>.+)$"
)
_GENERIC_LOCATION = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+)(?::(?P<column>\d+))?:\s*"
    r"(?:(?P<code>[A-Z]+[0-9]+)\s+)?(?P<message>.+)$"
)


def _clean_file(value: str) -> str:
    return value.strip().strip("'\"").replace("\\", "/")[:300]


def parse_verification_diagnostics(category: str, output: str, limit: int = 20) -> list[Diagnostic]:
    """Extract bounded file/line diagnostics from test, lint or typecheck output."""
    if category not in {"test", "lint", "typecheck"} or not output:
        return []
    diagnostics: list[Diagnostic] = []
    seen: set[tuple[str, int, int, str]] = set()

    def add(item: Diagnostic) -> None:
        key = (item.file.casefold(), item.line, item.column, item.message.casefold())
        if item.file and key not in seen and len(diagnostics) < max(1, min(50, limit)):
            seen.add(key)
            diagnostics.append(item)

    for raw_line in str(output).splitlines():
        line = raw_line.strip()
        if not line or len(line) > 800:
            continue
        match = _PYTEST_FAILED.match(line) or _PYTEST_ERROR.match(line)
        if match and category == "test":
            rest = match.group("rest").strip()
            message = rest.lstrip(":").strip() or "测试失败"
            add(Diagnostic(_clean_file(match.group("file")), 0, 0, message, "error"))
            continue
        match = _PYTEST_LOCATION.match(line)
        if match and category == "test":
            add(Diagnostic(
                _clean_file(match.group("file")), int(match.group("line")), 0,
                match.group("message").strip(), "error",
            ))
            continue
        match = _TSC_LOCATION.match(line)
        if match:
            add(Diagnostic(
                _clean_file(match.group("file")), int(match.group("line")),
                int(match.group("column")), match.group("message").strip(),
                "error", match.group("code") or "",
            ))
            continue
        match = _GENERIC_LOCATION.match(line)
        if match:
            add(Diagnostic(
                _clean_file(match.group("file")), int(match.group("line")),
                int(match.group("column") or 0), match.group("message").strip(),
                "warning" if "warning" in line.lower() else "error",
                match.group("code") or "",
            ))
    return diagnostics


def format_verification_diagnostics(category: str, output: str, limit: int = 20) -> str:
    """Render structured diagnostics for the model, or return an empty string."""
    diagnostics = parse_verification_diagnostics(category, output, limit)
    if not diagnostics:
        return ""
    heading = {
        "test": "结构化测试失败",
        "lint": "结构化 lint 问题",
        "typecheck": "结构化类型检查问题",
    }.get(category, "结构化诊断")
    lines = [f"{heading}（{len(diagnostics)} 条）："]
    lines.extend(item.render() for item in diagnostics)
    return "\n".join(lines)
