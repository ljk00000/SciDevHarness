"""Workspace-scoped file, command and integration tools for coding agents."""

from __future__ import annotations

import hashlib
import inspect
import locale
import math
import os
import re
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from scidev.integrations.browser import BrowserAgent
from scidev.workspace.checkpoint import WorkspaceCheckpoint
from scidev.workspace.context import ProjectContext
from scidev.runtime.errors import PermanentError, TaskCancelled
from scidev.workspace.git import GitManager
from scidev.runtime.ledger import EventLedger
from scidev.integrations.mcp import MCPError, MCPManager
from scidev.workspace.patching import PatchEngine, PatchError, PatchReviewRejected
from scidev.runtime.paths import (
    DEFAULT_EXCLUDED_PATH_NAMES,
    DEFAULT_SAFE_ENV_TEMPLATES,
    DEFAULT_SENSITIVE_PATH_NAMES,
    _is_protected_workspace_path,
    _is_sensitive_component,
)
from scidev.runtime.policy import (
    ExecutionMode,
    ExecutionPolicy,
    NetworkPolicy,
    assess_command,
    format_verification_diagnostics,
    normalize_command_prefixes,
)
from scidev.runtime.process import background_process_options, shell_command_arguments, terminate_process_tree
from scidev.runtime.helpers import new_id, now_iso
from scidev.runtime.sandbox import DockerSandbox, SandboxError
from scidev.agent.tool_registry import ToolRegistry
from scidev.agent.tool_schema import tool_definitions
from scidev.integrations.visual import VisualArtifactReviewer


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace a file atomically so a crash cannot leave a truncated source file."""
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


_DESTRUCTIVE_COMMAND_PATTERNS = (
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+clean\s+-[a-z]*f[a-z]*\b"),
    re.compile(r"\bgit\s+checkout\s+--\s+\."),
    re.compile(r"\brm\s+-(?:[a-z]*r[a-z]*f|[a-z]*f[a-z]*r)\b"),
    re.compile(r"\brmdir\s+/s\b"),
    re.compile(r"\bdel\s+/[a-z]*[sq][a-z]*\b"),
    re.compile(r"\bformat\s+[a-z]:"),
)

# Git mutations belong to the Harness: it stages only verified paths in a
# throwaway index and filters sensitive/large files. A model running
# ``git add -A && git commit`` would bypass that filter (and can sweep in the
# user's unrelated work), so those commands are refused before execution.
_GIT_GLOBAL_OPTIONS = (
    r"(?:\s+(?:-[cC]\s+\S+"
    r"|--(?:git-dir|work-tree|exec-path|config-env)=\S+"
    r"|--(?:no-pager|no-optional-locks|literal-pathspecs|bare|paginate)))"
)
_MANUAL_GIT_MUTATION_PATTERN = re.compile(
    r"\bgit" + _GIT_GLOBAL_OPTIONS + r"*\s+"
    r"(?:add|commit|reset|restore|stash|revert|cherry-pick|rebase|merge|checkout|switch)\b"
)


def manual_git_mutation_reason(command: str) -> str | None:
    """Return the own-Git-command pattern the Harness must keep for itself."""
    normalized = " ".join(str(command).strip().lower().split())
    match = _MANUAL_GIT_MUTATION_PATTERN.search(normalized)
    return match.group(0) if match else None


def destructive_command_reason(command: str) -> str | None:
    """Return the matched pattern for a destructive command, if any.

    Matching is normalised (case and internal whitespace) so ``git  reset
    --hard`` and reordered flags cannot slip past the guard. The user approval
    prompt remains the actual security boundary; this only blocks obvious
    self-destructive commands before they reach the user.
    """
    normalized = " ".join(str(command).strip().lower().split())
    for pattern in _DESTRUCTIVE_COMMAND_PATTERNS:
        if pattern.search(normalized):
            return pattern.pattern
    return None




def decode_process_output(raw: bytes) -> str:
    """Decode captured command output without losing non-ASCII text.

    Python children are forced to UTF-8 by ``run_command``; native Windows
    tools such as ``echo``/``pytest`` may still emit the OEM code page (for
    example cp936 on Chinese Windows). Prefer UTF-8, then the console code
    page, and only then replace undecodable bytes.
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    candidates: list[str] = []
    if os.name == "nt":
        try:
            import ctypes

            candidates.append(f"cp{ctypes.windll.kernel32.GetOEMCP()}")
        except (AttributeError, OSError, ValueError):
            pass
    try:
        preferred = locale.getpreferredencoding(False)
    except (LookupError, ValueError):
        preferred = ""
    if preferred:
        candidates.append(preferred)
    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


class CodingToolbox:
    """Tools exposed to the model; every path is constrained to the project root."""

    BROWSER_TOOL_NAMES = frozenset({"browser_search", "browser_open", "browser_click", "browser_screenshot"})
    MAX_READ_BYTES = 240_000
    MAX_CONTEXT_FILE_BYTES = 2_000_000
    MAX_WRITE_BYTES = 360_000
    SVG_GEOMETRY_TAGS = frozenset(
        {"circle", "ellipse", "image", "line", "path", "polygon", "polyline", "rect", "text", "use"}
    )
    SVG_DEFINITION_CONTAINERS = frozenset(
        {"clippath", "defs", "filter", "marker", "mask", "pattern", "symbol"}
    )
    EXCLUDED_NAMES = set(DEFAULT_EXCLUDED_PATH_NAMES)
    SENSITIVE_NAMES = set(DEFAULT_SENSITIVE_PATH_NAMES)
    SAFE_ENV_TEMPLATES = DEFAULT_SAFE_ENV_TEMPLATES

    def __init__(
        self,
        project_root: Path,
        ledger: EventLedger,
        command_approval: Callable[[str, int], bool] | None = None,
        visual_reviewer: VisualArtifactReviewer | None = None,
        patch_review: Callable[[dict[str, Any]], set[int] | None] | None = None,
        enforce_patch_review_for_existing: bool = False,
        browser_approval: Callable[[str, str], bool] | None = None,
        execution_mode: str | ExecutionMode = ExecutionMode.WORKSPACE_WRITE,
        network_policy: str | NetworkPolicy | None = None,
        allowed_commands: object = None,
        sandbox_backend: str = "",
        sandbox_image: str = "",
    ):
        self.project_root = Path(project_root).resolve()
        self.ledger = ledger
        self.command_approval = command_approval
        self.visual_reviewer = visual_reviewer
        self.patch_review = patch_review
        self.browser_agent = BrowserAgent(project_root, ledger, approval=browser_approval)
        self.mcp = MCPManager(self.project_root, ledger=ledger)
        self.enforce_patch_review_for_existing = bool(enforce_patch_review_for_existing)
        self.execution_policy = ExecutionPolicy(
            ExecutionMode.parse(execution_mode),
            NetworkPolicy.parse(
                network_policy if network_policy is not None else os.getenv("SCIDEV_NETWORK_POLICY"),
                NetworkPolicy.ASK,
            ),
            tuple(normalize_command_prefixes(allowed_commands)),
        )
        self.checkpoint: WorkspaceCheckpoint | None = None
        self.sandbox = DockerSandbox.from_environment(
            backend=sandbox_backend,
            image=sandbox_image,
        )
        self._task_created_file_hashes: dict[str, str] = {}
        self.last_command_execution: dict[str, Any] | None = None
        self._active_command_lock = threading.Lock()
        self._active_command: subprocess.Popen[str] | None = None
        self._command_cancel = threading.Event()
        # Shared with CodingAgent.request_cancel so a review dialog cannot apply
        # a patch after the user pressed Stop.
        self.cancel_event: threading.Event | None = None
        self.last_patch_result: dict[str, Any] | None = None
        self.context_overrides: dict[str, str] = {}
        self._context_overrides_by_key: dict[str, str] = {}
        self._context_readonly_paths: set[str] = set()
        self._context_readonly_path_names: dict[str, str] = {}
        self.project_context = ProjectContext(
            self.project_root, self.is_protected_path, max_file_bytes=self.MAX_CONTEXT_FILE_BYTES,
        )
        self.patch_engine = PatchEngine(
            self.project_root,
            self._resolve_patch_target,
            max_file_bytes=self.MAX_WRITE_BYTES,
            validate_content=self._validate_svg_document,
        )
        self.tool_registry = ToolRegistry.from_toolbox(self)

    def set_execution_mode(self, mode: str | ExecutionMode) -> None:
        """Apply a new mode to the next tool call without restarting the session."""
        self.execution_policy = ExecutionPolicy(
            ExecutionMode.parse(mode),
            self.execution_policy.network_policy,
            self.execution_policy.allowed_commands,
        )

    def set_network_policy(self, policy: str | NetworkPolicy) -> None:
        """Apply a network policy without discarding the execution mode."""
        self.execution_policy = ExecutionPolicy(
            self.execution_policy.mode,
            NetworkPolicy.parse(policy, self.execution_policy.network_policy),
            self.execution_policy.allowed_commands,
        )

    def set_sandbox(self, backend: str, image: str = "") -> None:
        """Switch command isolation for the next call; invalid choices fail closed."""
        self.sandbox = DockerSandbox.from_environment(backend=backend, image=image)

    def set_allowed_commands(self, commands: object) -> None:
        """Replace the explicit command allowlist without changing other policy."""
        self.execution_policy = ExecutionPolicy(
            self.execution_policy.mode,
            self.execution_policy.network_policy,
            tuple(normalize_command_prefixes(commands)),
        )

    def set_checkpoint(self, checkpoint: WorkspaceCheckpoint | None) -> None:
        self.checkpoint = checkpoint

    def _capture_checkpoint(self, target: Path) -> None:
        if self.checkpoint is None:
            return
        try:
            self.checkpoint.capture(target)
        except (OSError, ValueError) as exc:
            raise PermanentError(f"创建任务回滚检查点失败：{exc}") from exc

    def _require_write_allowed(self, action: str = "修改文件") -> None:
        if not self.execution_policy.can_write_files:
            raise PermanentError(
                f"当前执行策略为只读，禁止{action}。请在 Agent 设置中切换为“工作区可写”后再试。"
            )

    def _require_command_allowed(self) -> None:
        if not self.execution_policy.can_run_commands:
            raise PermanentError(
                "当前执行策略为只读，禁止执行终端命令。请在 Agent 设置中切换为“工作区可写”后再试。"
            )

    def set_editor_context(
        self,
        content_overrides: Any = None,
        readonly_paths: Any = None,
    ) -> None:
        """Install bounded, task-scoped editor snapshots without writing them to disk."""
        if content_overrides is None:
            content_overrides = {}
        if not isinstance(content_overrides, dict) or len(content_overrides) > ProjectContext.MAX_CONTENT_OVERRIDES:
            raise PermanentError("编辑器上下文格式无效或打开文件过多")
        if readonly_paths is None:
            readonly_paths = []
        if not isinstance(readonly_paths, list) or len(readonly_paths) > ProjectContext.MAX_EXCLUDED_PATHS:
            raise PermanentError("编辑器只读路径格式无效或路径过多")

        normalized_readonly: dict[str, str] = {}
        for raw_path in readonly_paths:
            if not isinstance(raw_path, str):
                raise PermanentError("编辑器只读路径必须是字符串")
            target = self._resolve(raw_path, allow_root=False)
            if not target.is_file():
                raise PermanentError(f"编辑器只读路径不是文件：{raw_path}")
            relative = target.relative_to(self.project_root).as_posix()
            normalized_readonly[relative.casefold()] = relative

        normalized_overrides: dict[str, str] = {}
        total_bytes = 0
        for raw_path, text in content_overrides.items():
            if not isinstance(raw_path, str) or not isinstance(text, str):
                raise PermanentError("编辑器上下文路径和内容必须是字符串")
            target = self._resolve(raw_path, allow_root=False)
            if not target.is_file():
                raise PermanentError(f"编辑器上下文路径不是文件：{raw_path}")
            encoded = text.encode("utf-8")
            relative = target.relative_to(self.project_root).as_posix()
            if b"\x00" in encoded or len(encoded) > self.MAX_CONTEXT_FILE_BYTES:
                raise PermanentError(f"编辑器上下文无效或超过大小限制：{relative}")
            total_bytes += len(encoded)
            if total_bytes > ProjectContext.MAX_OVERRIDE_BYTES:
                raise PermanentError("编辑器上下文超过总大小限制")
            normalized_overrides[relative] = text
            normalized_readonly[relative.casefold()] = relative
        self.context_overrides = normalized_overrides
        self._context_overrides_by_key = {
            path.casefold(): text for path, text in normalized_overrides.items()
        }
        self._context_readonly_paths = set(normalized_readonly)
        self._context_readonly_path_names = normalized_readonly

    def clear_editor_context(self) -> None:
        self.context_overrides.clear()
        self._context_overrides_by_key.clear()
        self._context_readonly_paths.clear()
        self._context_readonly_path_names.clear()

    def reset_task_write_scope(self) -> None:
        """Forget which files this Agent created; retries must review existing files."""
        self._task_created_file_hashes.clear()

    def _task_file_key(self, target: Path) -> str:
        return target.relative_to(self.project_root).as_posix().casefold()

    def _require_task_created_file(self, target: Path, raw: bytes | None = None) -> bytes | None:
        if not self.enforce_patch_review_for_existing:
            return raw
        key = self._task_file_key(target)
        expected = self._task_created_file_hashes.get(key)
        if expected is None:
            relative = target.relative_to(self.project_root).as_posix()
            self.ledger.append("existing_file_edit_blocked", {
                "path": relative,
                "reason": "apply_patch_review_required",
            })
            raise PermanentError(
                f"{relative} 是已有文件；禁止通过 write_file/replace_in_file 直接修改。"
                "请使用 apply_patch，让用户逐块审核后再应用。"
            )
        if raw is None:
            try:
                raw = target.read_bytes()
            except OSError as exc:
                raise PermanentError(f"本任务新建文件无法读取：{target.name}；请重新读取并使用 apply_patch") from exc
        if hashlib.sha256(raw).hexdigest() != expected:
            relative = target.relative_to(self.project_root).as_posix()
            raise PermanentError(
                f"{relative} 在本任务创建后又发生变化；为避免覆盖新内容，请重新读取并使用 apply_patch。"
            )
        return raw

    def _remember_task_created_file(self, target: Path, raw: bytes) -> None:
        if self.enforce_patch_review_for_existing:
            self._task_created_file_hashes[self._task_file_key(target)] = hashlib.sha256(raw).hexdigest()

    @property
    def context_excluded_paths(self) -> list[str]:
        return [self._context_readonly_path_names[key] for key in sorted(self._context_readonly_path_names)
                if key not in self._context_overrides_by_key]

    def _assert_editor_context_readonly(self, target: Path) -> None:
        relative = target.relative_to(self.project_root).as_posix()
        if relative.casefold() in self._context_readonly_paths:
            raise PermanentError(
                f"{relative} 在本次任务开始时有未保存的编辑器内容。为避免覆盖草稿，Agent 不会修改该文件；"
                "请先保存文件，再重新发起任务。"
            )

    def _resolve_patch_target(self, raw_path: str) -> Path:
        target = self._resolve(raw_path, allow_root=False)
        self._assert_editor_context_readonly(target)
        return target

    @classmethod
    def _is_sensitive_name(cls, name: str) -> bool:
        return _is_sensitive_component(name, cls.SENSITIVE_NAMES, cls.SAFE_ENV_TEMPLATES)

    @classmethod
    def is_protected_path(cls, raw_path: str | Path) -> bool:
        return _is_protected_workspace_path(
            raw_path,
            cls.EXCLUDED_NAMES,
            cls.SENSITIVE_NAMES,
            cls.SAFE_ENV_TEMPLATES,
        )

    @staticmethod
    def definitions(
        *, include_visual_review: bool = False, include_browser: bool = False,
    ) -> list[dict[str, Any]]:
        return tool_definitions(
            include_visual_review=include_visual_review,
            include_browser=include_browser,
        )
    def all_definitions(
        self,
        *,
        include_visual_review: bool = False,
        include_browser: bool = False,
    ) -> list[dict[str, Any]]:
        definitions = self.definitions(
            include_visual_review=include_visual_review,
            include_browser=include_browser,
        )
        try:
            definitions.extend(self.mcp.tool_definitions())
        except MCPError as exc:
            self.mcp.errors.append(str(exc))
        return definitions

    def _resolve(self, raw_path: str, allow_root: bool = True) -> Path:
        raw_path = str(raw_path or ".").strip()
        if "\x00" in raw_path:
            raise PermanentError("路径不能包含 NUL 字节")
        candidate = (self.project_root / raw_path).resolve()
        try:
            relative = candidate.relative_to(self.project_root)
        except ValueError as exc:
            raise PermanentError(f"路径越过项目根目录：{raw_path}") from exc
        if not allow_root and candidate == self.project_root:
            raise PermanentError("这里需要文件路径，不能使用项目根目录")
        excluded_names = {name.casefold() for name in self.EXCLUDED_NAMES}
        for component in relative.parts:
            if component.casefold() in excluded_names:
                raise PermanentError(f"禁止访问内部目录：{component}")
            if self._is_sensitive_name(component):
                raise PermanentError(f"禁止通过 Agent 文件工具访问敏感路径：{component}")
        return candidate

    @staticmethod
    def _decode_utf8_text(raw: bytes, path: Path) -> str:
        if b"\x00" in raw:
            raise PermanentError(f"拒绝将包含 NUL 字节的文件作为文本读取或编辑：{path.name}")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PermanentError(f"文件不是有效 UTF-8 文本，拒绝读取或编辑：{path.name}") from exc

    @staticmethod
    def _copied_read_file_line_numbers(old_text: str, source_text: str) -> bool:
        lines = [line for line in old_text.splitlines() if line.strip()]
        if not lines or any(not re.match(r"^[ \t]*\d+:[ \t]?", line) for line in lines):
            return False
        unnumbered = re.sub(r"(?m)^[ \t]*\d+:[ \t]?", "", old_text)
        unnumbered = unnumbered.replace("\r\n", "\n").replace("\r", "\n")
        source = source_text.replace("\r\n", "\n").replace("\r", "\n")
        return bool(unnumbered and unnumbered in source)

    @staticmethod
    def _validate_svg_document(path: Path, content: str | bytes) -> ET.Element | None:
        """Keep SVG writes and edits well-formed before they reach disk or auto-commit."""
        if path.suffix.casefold() != ".svg":
            return None
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        lowered = raw.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise PermanentError(f"拒绝写入包含 DTD 或实体声明的 SVG：{path.name}")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise PermanentError(f"拒绝写入格式错误的 SVG：{exc}") from exc
        local_name = root.tag.rsplit("}", 1)[-1].casefold() if isinstance(root.tag, str) else ""
        if local_name != "svg":
            raise PermanentError(f"拒绝写入根元素不是 svg 的文件：{path.name}")
        return root

    @classmethod
    def _svg_structure_issues(cls, root: ET.Element) -> list[str]:
        """Check broadly useful SVG structure without claiming visual correctness."""
        issues: list[str] = []
        view_box = root.attrib.get("viewBox", "").replace(",", " ").split()
        try:
            values = [float(value) for value in view_box]
        except ValueError:
            values = []
        if (
            len(values) != 4
            or not all(math.isfinite(value) for value in values)
            or values[2] <= 0
            or values[3] <= 0
        ):
            issues.append("根元素缺少有效 viewBox；请提供四个有限数值且宽、高均为正数。")

        elements = [element for element in root.iter() if isinstance(element.tag, str)]
        parents = {child: parent for parent in elements for child in parent}
        drawable: list[ET.Element] = []
        for element in elements:
            tag = element.tag.rsplit("}", 1)[-1].casefold()
            if tag not in cls.SVG_GEOMETRY_TAGS:
                continue
            ancestor = parents.get(element)
            inside_definition = False
            while ancestor is not None:
                ancestor_tag = ancestor.tag.rsplit("}", 1)[-1].casefold()
                if ancestor_tag in cls.SVG_DEFINITION_CONTAINERS:
                    inside_definition = True
                    break
                ancestor = parents.get(ancestor)
            if not inside_definition:
                drawable.append(element)

        if not drawable:
            issues.append("没有找到可见绘图几何元素；检查 SVG 是否只有标题、描述或 defs。")
        else:
            identified = [element for element in drawable if element.get("id", "").strip()]
            if (len(drawable) == 1 and not identified) or (len(drawable) > 1 and len(identified) < 2):
                group_ids = [
                    element
                    for element in elements
                    if element.tag.rsplit("}", 1)[-1].casefold() == "g" and element.get("id", "").strip()
                ]
                if group_ids and not identified:
                    issues.append(
                        "语义 ID 只标在 g 分组上；请将主体和关键部件的唯一 ID 直接放在对应可见几何元素上。"
                    )
                else:
                    issues.append(
                        "可见绘图几何缺少足够的直接语义 ID；请标记主体及每个关键部件，装饰细节可不标。"
                    )

        identifiers = [element.get("id", "").strip() for element in elements if element.get("id", "").strip()]
        duplicates = sorted(identifier for identifier, count in Counter(identifiers).items() if count > 1)
        if duplicates:
            sample = ", ".join(repr(identifier[:48]) for identifier in duplicates[:4])
            issues.append(f"存在重复 SVG id：{sample}。每个 id 必须唯一。")
        return issues

    @classmethod
    def _format_svg_structure_feedback(cls, root: ET.Element) -> str:
        issues = cls._svg_structure_issues(root)
        if not issues:
            return "SVG 结构预检通过（只检查 XML/基础结构，未执行视觉渲染）。"
        return "SVG 结构预检提示（文件已保存；尚未通过基础结构检查）：\n- " + "\n- ".join(issues[:5])

    def svg_structure_issues_for_file(self, raw_path: str) -> list[str]:
        target = self._resolve(raw_path, allow_root=False)
        if target.suffix.casefold() != ".svg":
            return []
        if not target.exists() or not target.is_file():
            return [f"文件不存在：{target.name}"]
        raw = target.read_bytes()
        if len(raw) > self.MAX_READ_BYTES:
            return [f"文件过大，无法执行 SVG 结构预检：{target.name}"]
        root = self._validate_svg_document(target, raw)
        return self._svg_structure_issues(root) if root is not None else []

    def list_files(self, path: str = ".", depth: int = 3) -> str:
        if not isinstance(path, str) or not path.strip():
            raise PermanentError("list_files 的 path 必须是非空字符串")
        if depth is None or depth == "":
            requested_depth = 3
        elif isinstance(depth, bool):
            raise PermanentError("list_files 的 depth 必须是整数")
        else:
            try:
                requested_depth = int(depth)
            except (TypeError, ValueError, OverflowError) as exc:
                raise PermanentError("list_files 的 depth 必须是整数") from exc
        base = self._resolve(path)
        if not base.exists():
            return f"路径不存在：{path}"
        if base.is_file():
            return str(base.relative_to(self.project_root))
        max_depth = max(1, min(requested_depth, 5))
        result: list[str] = []
        queue_items: list[tuple[Path, int, str]] = [(base, 0, "")]
        while queue_items and len(result) < 500:
            directory, current_depth, prefix = queue_items.pop(0)
            try:
                children = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
            except OSError as exc:
                result.append(f"{prefix}[无法读取：{exc}]")
                continue
            excluded_names = {name.casefold() for name in self.EXCLUDED_NAMES}
            for child in children:
                if (
                    child.name.casefold() in excluded_names
                    or self._is_sensitive_name(child.name)
                ):
                    continue
                try:
                    child.resolve(strict=False).relative_to(self.project_root)
                except (OSError, RuntimeError, ValueError):
                    # Do not expose names or metadata from links/junctions that
                    # resolve outside the workspace, even when is_symlink() is false.
                    continue
                if child.is_symlink() or child.is_junction():
                    continue
                marker = "目录" if child.is_dir() else f"文件 {child.stat().st_size} bytes"
                result.append(f"{prefix}{child.name}{'/' if child.is_dir() else ''}  [{marker}]")
                if child.is_dir() and current_depth + 1 < max_depth:
                    queue_items.append((child, current_depth + 1, prefix + "  "))
        if len(result) >= 500:
            result.append("[输出已截断：项目文件超过 500 项]")
        return "\n".join(result) or "（空目录）"

    def read_file(self, path: str, start_line: int = 1, end_line: int = 400) -> str:
        if not isinstance(path, str) or not path.strip():
            raise PermanentError("read_file 的 path 必须是非空字符串")

        def _line(value: Any, default: int) -> int:
            if value is None or value == "":
                return default
            if isinstance(value, bool):
                raise PermanentError("read_file 的 start_line/end_line 必须是整数")
            try:
                return int(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise PermanentError("read_file 的 start_line/end_line 必须是整数") from exc

        start = max(1, _line(start_line, 1))
        requested_end = _line(end_line, 400)
        target = self._resolve(path, allow_root=False)
        if not target.exists():
            return f"文件不存在：{path}"
        if target.is_dir():
            return f"路径是目录，不能按文件读取：{path}；请用 list_files 查看目录内容。"
        if not target.is_file():
            return f"路径不是普通文件，不能读取：{path}"
        relative = target.relative_to(self.project_root).as_posix()
        key = relative.casefold()
        if key in self._context_readonly_paths and key not in self._context_overrides_by_key:
            raise PermanentError(
                f"{relative} 有未保存的编辑器内容，但快照超出本次上下文预算；"
                "为避免读取磁盘旧版本，请先保存文件后重试。"
            )
        override = self._context_overrides_by_key.get(key)
        if override is not None:
            text = override
            note = "\n\n[以上内容来自任务开始时的未保存编辑器缓冲；该文件本次只读，保存后可重新发起修改。]"
        else:
            if target.stat().st_size > self.MAX_CONTEXT_FILE_BYTES:
                raise PermanentError(f"源文件过大（上限 {self.MAX_CONTEXT_FILE_BYTES} bytes）")
            with target.open("rb") as stream:
                raw = stream.read(self.MAX_CONTEXT_FILE_BYTES + 1)
            if len(raw) > self.MAX_CONTEXT_FILE_BYTES:
                raise PermanentError("源文件超过读取上限")
            text = self._decode_utf8_text(raw, target)
            note = ""
        if len(text.encode("utf-8")) > self.MAX_CONTEXT_FILE_BYTES:
            raise PermanentError(f"源文件过大（上限 {self.MAX_CONTEXT_FILE_BYTES} bytes）")
        lines = text.splitlines()
        if not lines:
            return "（空文件）" + note
        if start > len(lines):
            return (
                f"起始行 {start} 超出文件总行数 {len(lines)}；"
                f"请从 1 到 {len(lines)} 之间读取。" + note
            )
        end = min(len(lines), max(start, requested_end), start + 399)
        selected = []
        output_chars = 0
        for number in range(start, end + 1):
            line = f"{number}: {lines[number - 1]}"
            if output_chars + len(line) > 25000:
                selected.append(f"[输出达到长度预算，请从第 {number} 行继续读取；若该行过长可用 search_code 定位片段。]")
                break
            selected.append(line)
            output_chars += len(line) + 1
        result = ("\n".join(selected) or "（空文件）") + note
        scoped = self.project_context.instructions_for(
            [relative], limit=3500, include_root=False,
        )
        if scoped and target.name != "AGENTS.md":
            result += f"\n\n适用目录指令（以下不是文件原文）：\n{scoped}"
        return result

    def _write_file_bytes(self, target: Path, data: bytes) -> None:
        if len(target.name) > 255:
            raise PermanentError("文件名过长（上限 255 个字符）")
        self._capture_checkpoint(target)
        try:
            atomic_write_bytes(target, data)
        except (OSError, ValueError) as exc:
            raise PermanentError(f"写入文件失败：{exc}") from exc

    def write_file(self, path: str, content: str) -> str:
        self._require_write_allowed("写入文件")
        if not isinstance(path, str) or not path.strip():
            raise PermanentError("write_file 的 path 必须是非空字符串")
        if not isinstance(content, str):
            raise PermanentError(
                "write_file 的 content 必须是字符串；数组或对象请先序列化为文本，"
                "避免把 Python repr 静默写进文件。"
            )
        target = self._resolve(path, allow_root=False)
        if len(target.name) > 255:
            raise PermanentError("文件名过长（上限 255 个字符）")
        self._assert_editor_context_readonly(target)
        encoded = str(content).encode("utf-8")
        if len(encoded) > self.MAX_WRITE_BYTES:
            raise PermanentError(f"拒绝写入过大文件（上限 {self.MAX_WRITE_BYTES} bytes）")
        if b"\x00" in encoded:
            raise PermanentError(f"拒绝写入包含 NUL 字节的文本文件：{path}")
        svg_root = self._validate_svg_document(target, encoded)
        exists = target.exists()
        task_key = self._task_file_key(target)
        if exists:
            if not target.is_file():
                raise PermanentError(f"目标不是普通文件，拒绝覆盖：{path}")
            original = self._require_task_created_file(target)
            if target.stat().st_size > self.MAX_WRITE_BYTES:
                raise PermanentError(f"目标文件过大，拒绝完整重写：{path}")
            self._decode_utf8_text(original if original is not None else target.read_bytes(), target)
        elif self.enforce_patch_review_for_existing and task_key in self._task_created_file_hashes:
            raise PermanentError(f"本任务新建文件已被移除，拒绝 write_file 重新创建：{path}；请使用 apply_patch。")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PermanentError(f"创建目标目录失败：{exc}") from exc
        if self.enforce_patch_review_for_existing and not exists:
            self._capture_checkpoint(target)
            try:
                with target.open("xb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError as exc:
                raise PermanentError(f"创建期间目标文件已出现，未覆盖：{path}；请使用 apply_patch。") from exc
            except (OSError, ValueError) as exc:
                raise PermanentError(f"写入文件失败：{exc}") from exc
        else:
            self._write_file_bytes(target, encoded)
        self._remember_task_created_file(target, encoded)
        relative = target.relative_to(self.project_root).as_posix()
        self.ledger.append("file_changed", {"operation": "write", "path": relative, "bytes": len(encoded)})
        result = f"已写入 {relative}（{len(encoded)} bytes）"
        if svg_root is not None:
            result += "\n" + self._format_svg_structure_feedback(svg_root)
        return self._append_visual_review(target, relative, result)

    def replace_in_file(
        self,
        path: str,
        old_text: str = "",
        new_text: str = "",
        replace_all: bool = False,
        edits: list[dict[str, Any]] | None = None,
    ) -> str:
        self._require_write_allowed("替换文件内容")
        if edits is not None:
            if old_text or new_text or replace_all:
                raise PermanentError("不能同时使用 edits 与 old_text/new_text/replace_all")
            return self.replace_many_in_file(path, edits)
        if not isinstance(path, str) or not path.strip():
            raise PermanentError("replace_in_file 的 path 必须是非空字符串")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise PermanentError("replace_in_file 的 old_text/new_text 必须是字符串")
        target = self._resolve(path, allow_root=False)
        self._assert_editor_context_readonly(target)
        if not target.exists() or not target.is_file():
            raise PermanentError(f"文件不存在：{path}")
        if not old_text:
            raise PermanentError("拒绝使用空 old_text，避免意外插入或扩展整份文件")
        raw = self._require_task_created_file(target)
        if raw is None:
            raw = target.read_bytes()
        if len(raw) > self.MAX_WRITE_BYTES:
            raise PermanentError(f"文件过大（{len(raw)} bytes），请使用更小范围的编辑")
        text = self._decode_utf8_text(raw, target)
        matched_old_text = old_text
        count = text.count(matched_old_text)
        if count == 0 and "\n" in old_text and "\r" not in old_text:
            # read_file presents normalized lines, while Windows source files
            # commonly contain CRLF bytes. Match the source's actual newline
            # style when the model supplies an otherwise exact LF snippet.
            for newline in ("\r\n", "\r"):
                candidate = old_text.replace("\n", newline)
                candidate_count = text.count(candidate)
                if candidate_count:
                    matched_old_text = candidate
                    count = candidate_count
                    if "\n" in new_text and "\r" not in new_text:
                        new_text = new_text.replace("\n", newline)
                    break
        if count == 0:
            if self._copied_read_file_line_numbers(old_text, text):
                raise PermanentError(
                    f"old_text 在 {path} 中未找到：read_file 每行前的 N: 是显示行号，不是文件内容；"
                    "请删除这些前缀后重试。"
                )
            raise PermanentError(f"old_text 在 {path} 中未找到，请先重新读取文件")
        if count > 1 and not replace_all:
            raise PermanentError(f"old_text 在 {path} 中匹配 {count} 次，请提供更精确文本或明确 replace_all=true")
        updated = text.replace(matched_old_text, new_text, -1 if replace_all else 1)
        svg_root = self._validate_svg_document(target, updated)
        encoded = updated.encode("utf-8")
        if len(encoded) > self.MAX_WRITE_BYTES:
            raise PermanentError(f"拒绝写入过大文件（上限 {self.MAX_WRITE_BYTES} bytes）")
        if b"\x00" in encoded:
            raise PermanentError(f"拒绝写入包含 NUL 字节的文本文件：{path}")
        self._write_file_bytes(target, encoded)
        self._remember_task_created_file(target, encoded)
        relative = target.relative_to(self.project_root).as_posix()
        self.ledger.append("file_changed", {"operation": "replace", "path": relative, "matches": count})
        result = f"已修改 {relative}（匹配 {count} 次）"
        if svg_root is not None:
            result += "\n" + self._format_svg_structure_feedback(svg_root)
        return self._append_visual_review(target, relative, result)

    def replace_many_in_file(self, path: str, edits: list[dict[str, Any]]) -> str:
        self._require_write_allowed("批量替换文件内容")
        if not isinstance(path, str) or not path.strip():
            raise PermanentError("replace_in_file 的 path 必须是非空字符串")
        if not isinstance(edits, list) or not 1 <= len(edits) <= 50:
            raise PermanentError("edits 必须是 1–50 个替换对象的数组")
        normalized: list[tuple[str, str, bool]] = []
        input_characters = 0
        for edit in edits:
            if not isinstance(edit, dict):
                raise PermanentError("每个 edits 项必须是对象")
            if set(edit) - {"old_text", "new_text", "replace_all"}:
                raise PermanentError("edits 项包含不支持的字段")
            before = edit.get("old_text")
            after = edit.get("new_text")
            replace_all = edit.get("replace_all", False)
            if not isinstance(before, str) or not before or not isinstance(after, str):
                raise PermanentError("每个 edits 项都必须提供非空 old_text 和字符串 new_text")
            if not isinstance(replace_all, bool):
                raise PermanentError("replace_all 必须是布尔值")
            input_characters += len(before) + len(after)
            if input_characters > self.MAX_WRITE_BYTES:
                raise PermanentError("批量替换文本总长度超过文件编辑上限")
            normalized.append((before, after, replace_all))

        target = self._resolve(path, allow_root=False)
        self._assert_editor_context_readonly(target)
        if not target.exists() or not target.is_file():
            raise PermanentError(f"File not found: {path}")
        raw = self._require_task_created_file(target)
        if raw is None:
            raw = target.read_bytes()
        if len(raw) > self.MAX_WRITE_BYTES:
            raise PermanentError(f"File is too large to edit: {path}")
        original = self._decode_utf8_text(raw, target)
        updated = original
        total_matches = 0

        for before, after, replace_all in normalized:
            matched_before = before
            matched_after = after
            count = updated.count(matched_before)
            if count == 0 and "\n" in before and "\r" not in before:
                for newline in ("\r\n", "\r"):
                    candidate = before.replace("\n", newline)
                    candidate_count = updated.count(candidate)
                    if candidate_count:
                        matched_before = candidate
                        count = candidate_count
                        if "\n" in after and "\r" not in after:
                            matched_after = after.replace("\n", newline)
                        break
            if count == 0:
                if self._copied_read_file_line_numbers(before, original):
                    raise PermanentError(
                        f"Exact old_text not found in {path}: read_file line-number prefixes (N:) "
                        "are display-only; remove them and retry."
                    )
                raise PermanentError(f"Exact old_text not found in {path}; reread the file before retrying")
            if count > 1 and not replace_all:
                raise PermanentError(
                    f"old_text matched {count} times in {path}; provide more context or set replace_all=true"
                )
            updated = updated.replace(matched_before, matched_after, -1 if replace_all else 1)
            if "\x00" in updated or len(updated.encode("utf-8")) > self.MAX_WRITE_BYTES:
                raise PermanentError("Batch edit would create a binary or oversized text file")
            total_matches += count

        svg_root = self._validate_svg_document(target, updated)
        encoded = updated.encode("utf-8")
        self._write_file_bytes(target, encoded)
        self._remember_task_created_file(target, encoded)
        relative = target.relative_to(self.project_root).as_posix()
        self.ledger.append(
            "file_changed",
            {"operation": "replace", "path": relative, "matches": total_matches, "edits": len(normalized)},
        )
        result = f"Applied {len(normalized)} exact replacements to {relative} ({total_matches} match(es))"
        if svg_root is not None:
            result += "\n" + self._format_svg_structure_feedback(svg_root)
        return self._append_visual_review(target, relative, result)

    def apply_patch(self, patch: str) -> str:
        self._require_write_allowed("应用补丁")
        if not isinstance(patch, str) or not patch.strip():
            raise PermanentError("apply_patch 的 patch 必须是非空字符串")
        self.last_patch_result = None
        try:
            plan = self.patch_engine.prepare(patch)
        except PatchError as exc:
            if re.search(r"(?m)^\s*\d+:\s", patch):
                raise PatchError(
                    f"{exc}\n提示：补丁里出现了 read_file 的行号前缀（如 `1: `）。"
                    "行号只用于定位，不能粘贴进补丁；请去掉行号和冒号，直接使用文件原文。"
                ) from exc
            raise
        for file_plan in plan.files:
            target = (self.project_root / file_plan.path).resolve()
            try:
                relative = target.relative_to(self.project_root).as_posix()
            except ValueError:
                continue
            if relative.casefold() in self._context_readonly_paths:
                raise PatchError(
                    f"{relative} 在本次任务开始时有未保存的编辑器内容；"
                    "为避免补丁覆盖草稿，请先保存文件再重新发起任务。"
                )
        preview = plan.review_payload()
        self.ledger.append("patch_previewed", {
            "patch_id": plan.patch_id,
            "file_count": preview["file_count"],
            "hunk_count": preview["hunk_count"],
            "paths": [item.path for item in plan.files],
        })
        if self.patch_review is None:
            reason = "当前没有可用的补丁审核界面，未修改文件。"
            self.ledger.append("patch_review_rejected", {"patch_id": plan.patch_id, "reason": reason})
            raise PatchReviewRejected(reason)
        try:
            selected = self.patch_review(preview)
        except Exception as exc:
            reason = f"补丁审核界面失败，未修改文件：{type(exc).__name__}: {exc}"
            self.ledger.append("patch_review_rejected", {"patch_id": plan.patch_id, "reason": reason})
            raise PatchReviewRejected(reason) from exc
        if self.cancel_event is not None and self.cancel_event.is_set():
            # Stop was pressed while the review dialog was open; never apply
            # the approved hunks after that.
            self.ledger.append(
                "patch_cancelled_before_apply",
                {"patch_id": plan.patch_id, "paths": [item.path for item in plan.files]},
            )
            raise TaskCancelled("任务已取消，未应用补丁。")
        if selected is None:
            reason = "用户拒绝了补丁，未修改文件；必须停止，不得使用其他写文件工具绕过。"
            self.ledger.append("patch_review_rejected", {"patch_id": plan.patch_id, "reason": reason})
            raise PatchReviewRejected(reason)
        if not isinstance(selected, set) or any(type(item) is not int for item in selected):
            reason = "补丁审核返回值无效，未修改文件。"
            self.ledger.append("patch_review_rejected", {"patch_id": plan.patch_id, "reason": reason})
            raise PatchReviewRejected(reason)
        if not selected:
            reason = "没有批准任何补丁块，未修改文件；必须停止，不得使用其他写文件工具绕过。"
            self.ledger.append("patch_review_rejected", {"patch_id": plan.patch_id, "reason": reason})
            raise PatchReviewRejected(reason)

        for file_plan in plan.files:
            target = (self.project_root / file_plan.path).resolve()
            try:
                target.relative_to(self.project_root)
            except ValueError:
                continue
            self._capture_checkpoint(target)
        result = self.patch_engine.apply(plan, selected)
        if not result["applied_paths"]:
            reason = "所选补丁块没有产生文件变化。"
            self.ledger.append("patch_review_rejected", {"patch_id": plan.patch_id, "reason": reason})
            raise PatchReviewRejected(reason)
        self.last_patch_result = result
        file_by_path = {file_plan.path: file_plan for file_plan in plan.files}
        for relative in result["applied_paths"]:
            file_plan = file_by_path[relative]
            selected_for_file = sorted(set(result["applied_hunks"]) & {hunk.number for hunk in file_plan.hunks})
            self.ledger.append("file_changed", {
                "operation": "patch",
                "path": relative,
                "patch_id": plan.patch_id,
                "hunks": selected_for_file,
            })
        self.ledger.append("patch_applied", {
            "patch_id": plan.patch_id,
            "paths": result["applied_paths"],
            "applied_hunks": result["applied_hunks"],
            "declined_hunks": result["declined_hunks"],
        })
        summary = (
            f"已应用 {len(result['applied_hunks'])} 个补丁块，修改 {len(result['applied_paths'])} 个文件："
            + ", ".join(str(path) for path in result["applied_paths"])
        )
        for relative in result["applied_paths"]:
            if not str(relative).casefold().endswith(".svg"):
                continue
            issues = self.svg_structure_issues_for_file(str(relative))
            if issues:
                summary += (
                    f"\n{relative} SVG 结构预检提示（文件已保存；尚未通过基础结构检查）：\n- "
                    + "\n- ".join(issues[:5])
                )
            else:
                summary += f"\n{relative} SVG 结构预检通过（只检查 XML/基础结构，未执行视觉渲染）。"
        if result["declined_hunks"]:
            summary += (
                f"。另有 {len(result['declined_hunks'])} 个补丁块未获批准；任务将停止且不会自动提交，"
                "避免后续操作绕过用户选择。"
            )
        return summary

    def _append_visual_review(self, target: Path, relative: str, result: str) -> str:
        if self.visual_reviewer is None or target.suffix.casefold() != ".svg":
            return result
        try:
            review = self.visual_reviewer.inspect(target)
        except Exception as exc:  # Visual critique is advisory; never roll back a successful file edit.
            detail = f"{type(exc).__name__}: {exc}"[:300]
            self.ledger.append(
                "visual_review_failed",
                {"path": relative, "model": self.visual_reviewer.model, "error": detail},
            )
            return result + f"\n独立视觉盲审不可用（文件已保存，未据此判断成败）：{detail}"
        self.ledger.append(
            "visual_review_completed",
            {"path": relative, "model": self.visual_reviewer.model, "review_characters": len(review)},
        )
        return result + "\n" + review

    def inspect_visual_artifact(self, path: str) -> str:
        if self.visual_reviewer is None:
            raise PermanentError("未配置 SCIDEV_VISION_MODEL，视觉盲审工具不可用")
        target = self._resolve(path, allow_root=False)
        if not target.exists() or not target.is_file():
            raise PermanentError(f"视觉文件不存在：{path}")
        try:
            review = self.visual_reviewer.inspect(target)
        except Exception as exc:
            raise PermanentError(f"视觉盲审失败（文件未修改）：{type(exc).__name__}: {str(exc)[:300]}") from exc
        relative = target.relative_to(self.project_root).as_posix()
        self.ledger.append(
            "visual_review_completed",
            {"path": relative, "model": self.visual_reviewer.model, "review_characters": len(review)},
        )
        return review

    def _finish_command_execution(
        self,
        execution: dict[str, Any],
        status: str,
        *,
        exit_code: int | None = None,
        elapsed_ms: int = 0,
    ) -> None:
        execution.update({
            "status": status,
            "exit_code": exit_code,
            "elapsed_ms": max(0, int(elapsed_ms)),
            "finished_at": now_iso(),
        })
        self.last_command_execution = dict(execution)
        self.ledger.append("command_execution_recorded", dict(execution))

    def cancel_running_command(self) -> None:
        """Terminate the command tree started by run_command, if any."""
        self._command_cancel.set()
        with self._active_command_lock:
            process = self._active_command
        if process is not None and process.poll() is None:
            terminate_process_tree(process)
            if self.sandbox is not None:
                self.sandbox.cleanup()

    def reset_command_cancel(self) -> None:
        """Clear a previous task's cancellation before a new task starts."""
        self._command_cancel.clear()

    def run_command(self, command: str, timeout_seconds: int = 120) -> str:
        if not isinstance(command, str):
            raise PermanentError("run_command 的 command 必须是字符串")
        command = command.strip()
        execution = {
            "command_id": new_id("cmd"),
            "command": command[:1000],
            "status": "not_run",
            "exit_code": None,
            "timeout_seconds": None,
            "elapsed_ms": 0,
            "started_at": now_iso(),
            "finished_at": "",
            "approval_status": "pending",
            "approval_source": "",
        }
        self.last_command_execution = dict(execution)
        method_started = time.monotonic()
        if not command:
            execution["approval_status"] = "rejected"
            self._finish_command_execution(execution, "rejected")
            raise PermanentError("命令不能为空")
        assessment = assess_command(command)
        execution["command_category"] = assessment.category
        if not self.execution_policy.can_run_commands:
            execution["approval_status"] = "rejected"
            self._finish_command_execution(execution, "rejected")
            raise PermanentError(
                "当前执行策略为只读，禁止执行终端命令。请在 Agent 设置中切换为“工作区可写”后再试。"
            )
        if (
            assessment.category == "network"
            and self.execution_policy.network_policy is NetworkPolicy.DENY
        ):
            execution["approval_status"] = "rejected"
            self._finish_command_execution(execution, "rejected")
            self.ledger.append(
                "command_blocked_network_policy",
                {"command": command[:1000], "policy": self.execution_policy.network_policy.value},
            )
            raise PermanentError(
                "网络策略为 deny，已拒绝网络下载或远程连接命令；"
                "如需临时放行，请把 SCIDEV_NETWORK_POLICY 设为 ask 或 allow。"
            )
        if destructive_command_reason(command):
            execution["approval_status"] = "rejected"
            self._finish_command_execution(execution, "rejected")
            raise PermanentError("为保护项目，拒绝执行破坏性命令")
        manual_git = manual_git_mutation_reason(command)
        if manual_git:
            execution["approval_status"] = "rejected"
            self._finish_command_execution(execution, "rejected")
            self.ledger.append(
                "command_blocked_harness_owned",
                {"command": command[:1000], "pattern": manual_git},
            )
            raise PermanentError(
                "Harness 负责在验证后自动提交限定路径的改动；请勿手动运行 git add/commit/reset"
                " 等改写索引或历史的命令。直接完成改动并汇报即可。"
            )
        if timeout_seconds is None or timeout_seconds == "":
            requested_timeout = 120
        elif isinstance(timeout_seconds, bool):
            execution["approval_status"] = "rejected"
            self._finish_command_execution(execution, "rejected")
            raise PermanentError("命令超时参数无效")
        else:
            try:
                requested_timeout = int(timeout_seconds)
            except (TypeError, ValueError, OverflowError) as exc:
                execution["approval_status"] = "rejected"
                self._finish_command_execution(execution, "rejected")
                raise PermanentError("命令超时参数无效") from exc
        timeout = max(1, min(requested_timeout, 300))
        execution["timeout_seconds"] = timeout
        auto_approved = self.execution_policy.auto_approves(assessment, command)
        approved = False
        reason = "未配置用户审批回调"
        if auto_approved:
            approved = True
            reason = f"执行策略自动批准：{assessment.reason}"
            execution["approval_source"] = "policy"
            self.ledger.append(
                "command_auto_approved",
                {
                    "command": command,
                    "timeout_seconds": timeout,
                    "category": assessment.category,
                    "mode": self.execution_policy.mode.value,
                    "cwd": str(self.project_root),
                },
            )
        else:
            execution["approval_source"] = "user"
            self.ledger.append(
                "command_approval_requested",
                {"command": command, "timeout_seconds": timeout, "cwd": str(self.project_root)},
            )
            if self.command_approval is not None:
                try:
                    approved = bool(self.command_approval(command, timeout))
                    reason = "用户拒绝或审批超时"
                except Exception as exc:
                    reason = f"审批界面异常：{type(exc).__name__}: {exc}"
        if not approved:
            execution["approval_status"] = "denied"
            self._finish_command_execution(
                execution, "not_run", elapsed_ms=int((time.monotonic() - method_started) * 1000),
            )
            self.ledger.append(
                "command_approval_denied",
                {"command": command, "reason": reason},
            )
            raise PermanentError(f"命令未执行：{reason}。Agent 不应重试该命令。")
        if self._command_cancel.is_set():
            # Stop was pressed while the approval dialog was open; never spawn
            # the child after that, even if the dialog eventually approved it.
            execution["approval_status"] = "cancelled"
            self._finish_command_execution(
                execution, "cancelled", elapsed_ms=int((time.monotonic() - method_started) * 1000),
            )
            self.ledger.append(
                "command_cancelled_before_start",
                {"command": command[:1000]},
            )
            raise TaskCancelled("任务已取消，命令未执行。")
        execution["approval_status"] = "approved"
        self.ledger.append(
            "command_approval_granted",
            {
                "command": command,
                "timeout_seconds": timeout,
                "source": execution["approval_source"],
                "category": assessment.category,
            },
        )
        self.ledger.append("tool_called", {"tool": "run_command", "command": command})
        execution["started_at"] = now_iso()
        execution_started = time.monotonic()
        # Force UTF-8 for captured children: cmd.exe defaults to the OEM code
        # page (for example cp936), so Chinese pytest/build output would be
        # decoded as replacement characters by the UTF-8 reader below.
        child_environment = dict(os.environ)
        child_environment["PYTHONIOENCODING"] = "utf-8"
        child_environment["PYTHONUTF8"] = "1"
        if self.sandbox is not None:
            if (
                self.sandbox.network != "none"
                and self.execution_policy.network_policy is not NetworkPolicy.ALLOW
            ):
                execution["approval_status"] = "rejected"
                self._finish_command_execution(execution, "rejected")
                self.ledger.append(
                    "command_blocked_sandbox_network",
                    {
                        "command": command[:1000],
                        "sandbox_network": self.sandbox.network,
                        "policy": self.execution_policy.network_policy.value,
                    },
                )
                raise PermanentError(
                    "Docker 沙箱请求了 bridge 网络，但宿主网络策略不是 allow；拒绝执行。"
                )
            try:
                launch_command = self.sandbox.wrap(command, self.project_root)
            except SandboxError as exc:
                execution["approval_status"] = "rejected"
                self._finish_command_execution(execution, "rejected")
                self.ledger.append(
                    "command_blocked_sandbox",
                    {"command": command[:1000], "sandbox": self.sandbox.name, "error": str(exc)},
                )
                raise PermanentError(f"沙箱不可用，拒绝执行：{exc}") from exc
            launch_extra = {}
        elif os.name == "nt":
            # cmd.exe needs an explicit UTF-8 code page for native tools.
            launch_command: Any = shell_command_arguments(command)
            launch_extra: dict[str, Any] = {}
        else:
            # Preserve the previous POSIX behaviour (plain shell) instead of
            # switching to the terminal's login-shell wrapper.
            launch_command = command
            launch_extra = {"shell": True}
        if self._command_cancel.is_set():
            execution["approval_status"] = "cancelled"
            self._finish_command_execution(
                execution, "cancelled", elapsed_ms=int((time.monotonic() - method_started) * 1000),
            )
            self.ledger.append(
                "command_cancelled_before_start",
                {"command": command[:1000]},
            )
            raise TaskCancelled("任务已取消，命令未执行。")
        try:
            process = subprocess.Popen(
                launch_command,
                cwd=self.project_root,
                env=child_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **launch_extra,
                **background_process_options(),
            )
            with self._active_command_lock:
                self._active_command = process
            # Cancellation can arrive between the pre-spawn check and Popen.
            if self._command_cancel.is_set():
                terminate_process_tree(process)
            try:
                output_bytes, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                terminate_process_tree(process)
                if self.sandbox is not None:
                    self.sandbox.cleanup()
                output_bytes, _ = process.communicate()
                output = decode_process_output(output_bytes or b"")
                self._finish_command_execution(
                    execution,
                    "timed_out",
                    exit_code=process.returncode,
                    elapsed_ms=int((time.monotonic() - execution_started) * 1000),
                )
                return (
                    f"命令超时（{timeout}s），已终止。\n执行状态：timed_out；退出码：{process.returncode}"
                    f"\n{output[-20000:]}"
                )
            finally:
                with self._active_command_lock:
                    if self._active_command is process:
                        self._active_command = None
            output = decode_process_output(output_bytes or b"")
            if self._command_cancel.is_set():
                self._finish_command_execution(
                    execution,
                    "cancelled",
                    exit_code=process.returncode,
                    elapsed_ms=int((time.monotonic() - execution_started) * 1000),
                )
                return (
                    "命令已取消，进程树已终止。\n"
                    f"执行状态：cancelled；退出码：{process.returncode}\n"
                    f"{output[-4000:]}"
                )
        except OSError as exc:
            self._finish_command_execution(
                execution,
                "start_failed",
                elapsed_ms=int((time.monotonic() - execution_started) * 1000),
            )
            raise PermanentError(f"命令启动失败：{exc}") from exc
        output = output[-24000:]
        status = "passed" if process.returncode == 0 else "failed"
        diagnostics = format_verification_diagnostics(assessment.category, output)
        if diagnostics:
            execution["diagnostics_count"] = diagnostics.count("\n- ")
        self._finish_command_execution(
            execution,
            status,
            exit_code=process.returncode,
            elapsed_ms=int((time.monotonic() - execution_started) * 1000),
        )
        result = f"退出码：{process.returncode}\n执行状态：{status}\n{output}".rstrip()
        if diagnostics:
            result += f"\n\n{diagnostics}"
        return result

    def git_diff(self) -> str:
        manager = GitManager(self.project_root)
        status = manager.status(protect_sensitive=True)
        changes = manager.diff(protect_sensitive=True).strip() or "（工作区没有可展示的文本 diff）"
        return f"状态：\n{status}\n\nDiff：\n{changes[-30000:]}"

    def search_code(
        self, query: str, path: str = ".", glob: str = "", case_sensitive: bool = False,
        max_results: int = 20, context_lines: int = 1,
    ) -> str:
        try:
            record = self.project_context.search(
                query, path, glob, case_sensitive, max_results, context_lines,
                content_overrides=self.context_overrides,
                excluded_paths=self.context_excluded_paths,
            )
        except ValueError as exc:
            raise PermanentError(str(exc)) from exc
        self.ledger.append("project_code_searched", {
            "query": query, "path": path, "matches": len(record["results"]),
            "scanned_files": record["scanned_files"], "limited": record["limited"],
        })
        return self.project_context.format_search(record)

    def get_project_context(self, query: str = "", path: str = ".") -> str:
        if not isinstance(query, str) or len(query) > 4000:
            raise PermanentError("上下文 query 必须是最多 4000 字符的字符串")
        try:
            self.project_context.resolve_scope(path)
        except ValueError as exc:
            raise PermanentError(str(exc)) from exc
        snapshot = self.project_context.build(
            query,
            GitManager(self.project_root).status(protect_sensitive=True),
            [path],
            content_overrides=self.context_overrides,
            excluded_paths=self.context_excluded_paths,
        )
        return str(snapshot["text"])

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if name.startswith("mcp__"):
            if not isinstance(arguments, dict):
                raise PermanentError(f"工具 {name} 的参数必须是 JSON 对象")
            if not self.execution_policy.can_run_commands:
                raise PermanentError("只读执行策略不允许调用 MCP 工具")
            if self.command_approval is None:
                raise PermanentError("MCP 工具需要审批回调；当前会话没有可用的审批入口")
            try:
                approved = self.command_approval(f"MCP {name}", 30)
            except Exception as exc:  # noqa: BLE001 - approval errors fail closed
                raise PermanentError(f"MCP 工具审批失败：{exc}") from exc
            if not approved:
                raise PermanentError(f"MCP 工具调用未获批准：{name}")
            try:
                return self.mcp.call(name, arguments)
            except MCPError as exc:
                raise PermanentError(str(exc)) from exc
        binding = self.tool_registry.get(name)
        if binding is None:
            raise PermanentError(f"未知工具：{name}")
        handler = binding.handler
        if not isinstance(arguments, dict):
            raise PermanentError(f"工具 {name} 的参数必须是 JSON 对象")
        try:
            parameters: dict[str, inspect.Parameter] | None = inspect.signature(handler).parameters
        except (TypeError, ValueError):
            parameters = None
        if parameters is not None and not any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            allowed_hint = ", ".join(parameters) or "（无）"
            unknown = sorted(set(arguments) - set(parameters))
            if unknown:
                raise PermanentError(
                    f"工具 {name} 不支持参数：{', '.join(unknown)}；"
                    f"可用参数：{allowed_hint}"
                )
            missing = sorted(
                parameter_name
                for parameter_name, parameter in parameters.items()
                if parameter.default is inspect.Parameter.empty
                and parameter.kind
                in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                and parameter_name not in arguments
            )
            if missing:
                raise PermanentError(
                    f"工具 {name} 缺少必填参数：{', '.join(missing)}；"
                    f"可用参数：{allowed_hint}"
                )
        return str(handler(**arguments))

    def close(self) -> None:
        self.browser_agent.close()
        self.mcp.close()
