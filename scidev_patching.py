"""Strict Codex-style patch parsing, preview and reviewed application."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class PatchError(ValueError):
    """The patch is invalid, stale, unsafe, or could not be applied."""


class PatchReviewRejected(PatchError):
    """The user rejected the preview or no interactive review surface exists."""


@dataclass(frozen=True)
class PatchHunk:
    number: int
    path: str
    operation: str
    body: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PatchFile:
    path: str
    operation: str
    target: Path
    original: bytes | None
    original_sha256: str
    newline: str
    final_newline: bool
    lines: tuple[str, ...]
    hunks: tuple[PatchHunk, ...]


@dataclass(frozen=True)
class PatchPlan:
    patch_id: str
    files: tuple[PatchFile, ...]
    hunks: tuple[dict[str, object], ...]

    def review_payload(self) -> dict[str, object]:
        return {
            "patch_id": self.patch_id,
            "file_count": len(self.files),
            "hunk_count": len(self.hunks),
            "hunks": [dict(hunk) for hunk in self.hunks],
        }


class PatchEngine:
    MAX_PATCH_CHARS = 500_000
    MAX_FILES = 20
    MAX_HUNKS = 100
    MAX_TOTAL_SOURCE_BYTES = 4_000_000

    def __init__(
        self,
        project_root: Path,
        resolve: Callable[[str], Path],
        *,
        max_file_bytes: int,
        validate_content: Callable[[Path, bytes], object] | None = None,
    ):
        self.root = Path(project_root).resolve()
        self.resolve = resolve
        self.max_file_bytes = int(max_file_bytes)
        self.validate_content = validate_content

    @staticmethod
    def _directive(line: str) -> tuple[str, str] | None:
        for operation in ("Update", "Add", "Delete"):
            prefix = f"*** {operation} File:"
            if line.startswith(prefix):
                return operation.casefold(), line[len(prefix):].strip()
        return None

    def _resolve_patch_path(self, raw_path: str) -> tuple[str, Path]:
        if not raw_path or "\x00" in raw_path:
            raise PatchError("补丁文件路径不能为空或包含 NUL")
        normalized = raw_path.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise PatchError(f"补丁只能使用项目相对路径：{raw_path}")
        pieces = normalized.split("/")
        if any(piece in {"", ".", ".."} for piece in pieces):
            raise PatchError(f"补丁路径包含非法片段：{raw_path}")
        if any(":" in piece for piece in pieces):
            raise PatchError(f"补丁路径不能包含冒号：{raw_path}")
        candidate = self.root.joinpath(*pieces)
        cursor = candidate
        while cursor != self.root:
            junction = getattr(cursor, "is_junction", None)
            try:
                if cursor.is_symlink() or (callable(junction) and junction()):
                    raise PatchError(f"补丁不跟随符号链接或目录联接：{raw_path}")
            except OSError as exc:
                raise PatchError(f"无法安全检查补丁路径：{raw_path}") from exc
            cursor = cursor.parent
        try:
            target = self.resolve(normalized)
            relative = target.relative_to(self.root).as_posix()
        except Exception as exc:
            raise PatchError(f"补丁路径不可访问：{raw_path}（{exc}）") from exc
        return relative, target

    @staticmethod
    def _normalise_file(raw: bytes, path: str) -> tuple[list[str], str, bool]:
        if b"\x00" in raw:
            raise PatchError(f"拒绝将二进制文件作为补丁目标：{path}")
        if not raw:
            return [], "\n", True
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PatchError(f"补丁目标不是 UTF-8 文本：{path}") from exc
        newline = "\r\n" if "\r\n" in text else "\r" if "\r" in text and "\n" not in text else "\n"
        normalised = text.replace("\r\n", "\n").replace("\r", "\n")
        final_newline = normalised.endswith("\n")
        lines = normalised.split("\n")
        if final_newline:
            lines.pop()
        return lines, newline, final_newline

    @staticmethod
    def _old_and_new(body: tuple[tuple[str, str], ...], path: str) -> tuple[list[str], list[str]]:
        old_lines: list[str] = []
        new_lines: list[str] = []
        changed = False
        for prefix, line in body:
            if prefix in {" ", "-"}:
                old_lines.append(line)
            if prefix in {" ", "+"}:
                new_lines.append(line)
            changed |= prefix != " "
        if not changed:
            raise PatchError(f"{path} 的修改块缺少变更或唯一上下文；请提供原文上下文行")
        return old_lines, new_lines

    @staticmethod
    def _find_unique(lines: list[str], needle: list[str], path: str) -> int:
        if not needle:
            if not lines:
                return 0
            raise PatchError(f"{path} 的插入补丁缺少上下文，无法安全定位")
        matches = [
            start for start in range(0, len(lines) - len(needle) + 1)
            if lines[start:start + len(needle)] == needle
        ]
        if len(matches) != 1:
            reason = "未找到" if not matches else f"匹配 {len(matches)} 处，位置不唯一"
            raise PatchError(f"{path} 补丁上下文{reason}；请重新读取文件并生成更精确补丁")
        return matches[0]

    def _apply_hunk(self, lines: list[str], hunk: PatchHunk) -> tuple[list[str], list[str], list[str], int]:
        old_lines, new_lines = self._old_and_new(hunk.body, hunk.path)
        if not old_lines and lines:
            raise PatchError(f"{hunk.path} 的插入补丁缺少上下文，无法安全定位")
        start = self._find_unique(lines, old_lines, hunk.path)
        return lines[:start] + new_lines + lines[start + len(old_lines):], old_lines, new_lines, start

    @staticmethod
    def _diff(path: str, old_lines: list[str], new_lines: list[str], operation: str) -> str:
        from_name = "/dev/null" if operation == "add" else f"a/{path}"
        to_name = "/dev/null" if operation == "delete" else f"b/{path}"
        return "".join(difflib.unified_diff(
            [line + "\n" for line in old_lines],
            [line + "\n" for line in new_lines],
            fromfile=from_name,
            tofile=to_name,
            n=3,
        ))

    def prepare(self, patch_text: str) -> PatchPlan:
        if not isinstance(patch_text, str) or not patch_text.strip():
            raise PatchError("补丁不能为空")
        if len(patch_text) > self.MAX_PATCH_CHARS:
            raise PatchError(f"补丁超过 {self.MAX_PATCH_CHARS:,} 字符上限")
        lines = patch_text.splitlines()
        if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
            raise PatchError("补丁必须使用 *** Begin Patch / *** End Patch 包围")

        parsed: list[tuple[str, str, list[tuple[str, ...] | None]]] = []
        index = 1
        hunk_count = 0
        seen_paths: set[str] = set()
        while index < len(lines) - 1:
            if not lines[index].strip():
                index += 1
                continue
            directive = self._directive(lines[index])
            if directive is None:
                raise PatchError(f"不支持的补丁指令：{lines[index][:120]}")
            operation, raw_path = directive
            relative, target = self._resolve_patch_path(raw_path)
            folded = relative.casefold()
            if folded in seen_paths:
                raise PatchError(f"补丁重复包含同一路径：{relative}")
            seen_paths.add(folded)
            if len(seen_paths) > self.MAX_FILES:
                raise PatchError(f"补丁最多允许 {self.MAX_FILES} 个文件")
            index += 1
            blocks: list[tuple[str, ...] | None] = []
            if operation == "add":
                added: list[str] = []
                while index < len(lines) - 1 and self._directive(lines[index]) is None:
                    line = lines[index]
                    if line.startswith("*** "):
                        if line == "*** End of File":
                            index += 1
                        break
                    if not line.startswith("+"):
                        raise PatchError(f"新增文件 {relative} 的每行必须以 + 开头")
                    added.append(line[1:])
                    index += 1
                blocks.append(tuple("+" + line for line in added))
            elif operation == "delete":
                if index < len(lines) - 1 and self._directive(lines[index]) is None and lines[index].strip():
                    raise PatchError(f"删除文件 {relative} 不应包含内容行")
                blocks.append(None)
            else:
                while index < len(lines) - 1 and self._directive(lines[index]) is None:
                    if lines[index].startswith("*** "):
                        break
                    if not lines[index].startswith("@@"):
                        raise PatchError(f"{relative} 的修改内容必须从 @@ 修改块开始")
                    index += 1  # Hunk labels (when present) are hints, never trusted as content.
                    body: list[str] = []
                    while index < len(lines) - 1:
                        line = lines[index]
                        if line.startswith("@@") or self._directive(line) or line.startswith("*** "):
                            if line == "*** End of File":
                                index += 1
                            break
                        if line.startswith("\\ No newline at end of file"):
                            index += 1
                            continue
                        if not line or line[0] not in " +-":
                            raise PatchError(f"{relative} 的修改块含无效行，必须以空格、+ 或 - 开始")
                        body.append(line)
                        index += 1
                    if not body:
                        raise PatchError(f"{relative} 含空修改块")
                    blocks.append(tuple(body))
                    hunk_count += 1
                if not blocks:
                    raise PatchError(f"{relative} 没有包含 @@ 修改块")
            if operation != "update":
                hunk_count += 1
            parsed.append((operation, relative, blocks))
            if hunk_count > self.MAX_HUNKS:
                raise PatchError(f"补丁最多允许 {self.MAX_HUNKS} 个修改块")

        total_bytes = 0
        file_plans: list[PatchFile] = []
        preview_hunks: list[dict[str, object]] = []
        next_hunk = 1
        for operation, relative, blocks in parsed:
            target = self.resolve(relative)
            exists = target.exists()
            if operation == "add" and exists:
                raise PatchError(f"新增文件已存在：{relative}")
            if operation != "add" and (not exists or not target.is_file()):
                raise PatchError(f"补丁目标不是现有普通文件：{relative}")
            original = target.read_bytes() if exists else None
            if original is not None:
                total_bytes += len(original)
                if len(original) > self.max_file_bytes:
                    raise PatchError(f"补丁目标超过 {self.max_file_bytes:,} bytes：{relative}")
            original_sha = hashlib.sha256(original).hexdigest() if original is not None else ""
            if original is None:
                file_lines, newline, final_newline = [], "\n", True
            else:
                file_lines, newline, final_newline = self._normalise_file(original, relative)
            hunks: list[PatchHunk] = []
            working = list(file_lines)
            if operation == "add":
                added_lines = tuple(("+", line[1:]) for line in (blocks[0] or ()))
                hunks.append(PatchHunk(next_hunk, relative, operation, added_lines))
                new_lines = [line for _prefix, line in added_lines]
                preview_diff = self._diff(relative, [], new_lines, operation)
                preview_hunks.append(self._preview_record(next_hunk, relative, operation, preview_diff, [], new_lines))
                working = new_lines
                next_hunk += 1
            elif operation == "delete":
                hunk = PatchHunk(next_hunk, relative, operation)
                hunks.append(hunk)
                preview_diff = self._diff(relative, working, [], operation)
                preview_hunks.append(self._preview_record(next_hunk, relative, operation, preview_diff, working, []))
                working = []
                next_hunk += 1
            else:
                for block in blocks:
                    body = tuple((line[0], line[1:]) for line in (block or ()))
                    hunk = PatchHunk(next_hunk, relative, operation, body)
                    updated, old_lines, new_lines, start = self._apply_hunk(working, hunk)
                    old_context = working[max(0, start - 3):min(len(working), start + len(old_lines) + 3)]
                    prefix_count = min(3, start)
                    suffix_start = start + len(old_lines)
                    suffix_count = min(3, len(working) - suffix_start)
                    local_old = old_context
                    local_new = (
                        working[max(0, start - prefix_count):start]
                        + new_lines
                        + working[suffix_start:suffix_start + suffix_count]
                    )
                    preview_diff = self._diff(relative, local_old, local_new, operation)
                    if not preview_diff:
                        raise PatchError(f"{relative} 的修改块没有实际差异")
                    hunks.append(hunk)
                    preview_hunks.append(self._preview_record(next_hunk, relative, operation, preview_diff, old_lines, new_lines))
                    working = updated
                    next_hunk += 1
            if operation == "update" and working == file_lines:
                raise PatchError(f"{relative} 的补丁不会改变文件")
            if operation == "update" and len("\n".join(working).encode("utf-8")) > self.max_file_bytes:
                raise PatchError(f"补丁结果超过 {self.max_file_bytes:,} bytes：{relative}")
            file_plans.append(PatchFile(
                path=relative,
                operation=operation,
                target=target,
                original=original,
                original_sha256=original_sha,
                newline=newline,
                final_newline=final_newline,
                lines=tuple(file_lines),
                hunks=tuple(hunks),
            ))
        if not file_plans:
            raise PatchError("补丁没有文件修改")
        if total_bytes > self.MAX_TOTAL_SOURCE_BYTES:
            raise PatchError(f"补丁目标总计超过 {self.MAX_TOTAL_SOURCE_BYTES:,} bytes")
        return PatchPlan(
            patch_id=hashlib.sha256(patch_text.encode("utf-8")).hexdigest(),
            files=tuple(file_plans),
            hunks=tuple(preview_hunks),
        )

    @staticmethod
    def _preview_record(
        number: int, path: str, operation: str, diff: str, old: list[str], new: list[str],
    ) -> dict[str, object]:
        return {
            "id": number,
            "path": path,
            "operation": operation,
            "diff": diff,
            "added_lines": sum(1 for line in new if line not in old),
            "removed_lines": sum(1 for line in old if line not in new),
        }

    def _recheck(self, file_plan: PatchFile) -> None:
        relative, current_target = self._resolve_patch_path(file_plan.path)
        if relative.casefold() != file_plan.path.casefold() or current_target != file_plan.target:
            raise PatchError(f"预览后路径解析发生变化：{file_plan.path}；请重新生成补丁")
        if file_plan.original is None:
            if file_plan.target.exists():
                raise PatchError(f"预览后新文件路径已被占用：{file_plan.path}；请重新生成补丁")
            return
        try:
            raw = file_plan.target.read_bytes()
        except OSError as exc:
            raise PatchError(f"预览后无法读取补丁目标 {file_plan.path}：{exc}") from exc
        if hashlib.sha256(raw).hexdigest() != file_plan.original_sha256:
            raise PatchError(f"预览后文件已变化：{file_plan.path}；为避免覆盖用户修改，请重新生成补丁")

    def _selected_bytes(self, file_plan: PatchFile, selected: set[int]) -> bytes | None:
        selected_hunks = [hunk for hunk in file_plan.hunks if hunk.number in selected]
        if not selected_hunks:
            return None
        if file_plan.operation == "delete":
            return b""
        current = list(file_plan.lines)
        for hunk in selected_hunks:
            if hunk.operation == "add":
                current = [line for _prefix, line in hunk.body]
            else:
                current, _old, _new, _start = self._apply_hunk(current, hunk)
        if file_plan.operation == "add":
            final_newline = True
        else:
            final_newline = file_plan.final_newline
        text = file_plan.newline.join(current)
        if current and final_newline:
            text += file_plan.newline
        return text.encode("utf-8")

    def apply(self, plan: PatchPlan, selected_hunk_ids: set[int]) -> dict[str, object]:
        valid_ids = {int(hunk["id"]) for hunk in plan.hunks}
        if not selected_hunk_ids <= valid_ids:
            raise PatchError("审核结果包含未知补丁块")
        if not selected_hunk_ids:
            return {"patch_id": plan.patch_id, "applied_paths": [], "applied_hunks": [], "declined_hunks": sorted(valid_ids)}

        changes: list[dict[str, object]] = []
        output_bytes = 0
        for file_plan in plan.files:
            selected = {hunk.number for hunk in file_plan.hunks} & selected_hunk_ids
            if not selected:
                continue
            self._recheck(file_plan)
            result = self._selected_bytes(file_plan, selected)
            if result is None:
                continue
            if file_plan.operation == "delete":
                result_value = None
            else:
                if len(result) > self.max_file_bytes:
                    raise PatchError(f"补丁结果超过 {self.max_file_bytes:,} bytes：{file_plan.path}")
                if b"\x00" in result:
                    raise PatchError(f"补丁结果含 NUL 字节：{file_plan.path}")
                if file_plan.original == result:
                    continue
                if self.validate_content is not None:
                    self.validate_content(file_plan.target, result)
                result_value = result
                output_bytes += len(result)
            changes.append({
                "file": file_plan,
                "bytes": result_value,
                "selected_hunks": sorted(selected),
            })
        if output_bytes > self.MAX_TOTAL_SOURCE_BYTES:
            raise PatchError(f"补丁结果总计超过 {self.MAX_TOTAL_SOURCE_BYTES:,} bytes")
        if not changes:
            raise PatchError("选中的补丁块未产生文件变化")

        created_directories: set[Path] = set()
        temporary_files: dict[Path, Path] = {}
        applied: list[dict[str, object]] = []
        try:
            for change in changes:
                file_plan = change["file"]
                assert isinstance(file_plan, PatchFile)
                if change["bytes"] is None:
                    continue
                target = file_plan.target
                missing = []
                parent = target.parent
                while parent != self.root and not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                target.parent.mkdir(parents=True, exist_ok=True)
                created_directories.update(path for path in missing if path.is_dir())
                fd, temp_name = tempfile.mkstemp(prefix=".scidev-patch-", dir=target.parent)
                temp_path = Path(temp_name)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(change["bytes"])
                    stream.flush()
                    os.fsync(stream.fileno())
                if file_plan.original is not None:
                    os.chmod(temp_path, stat.S_IMODE(target.stat().st_mode))
                temporary_files[target] = temp_path

            for change in changes:
                file_plan = change["file"]
                assert isinstance(file_plan, PatchFile)
                self._recheck(file_plan)
            for change in changes:
                file_plan = change["file"]
                assert isinstance(file_plan, PatchFile)
                target = file_plan.target
                if change["bytes"] is None:
                    target.unlink()
                else:
                    os.replace(temporary_files[target], target)
                applied.append(change)
        except Exception as exc:
            rollback_errors = []
            for change in reversed(applied):
                file_plan = change["file"]
                assert isinstance(file_plan, PatchFile)
                try:
                    if file_plan.original is None:
                        file_plan.target.unlink(missing_ok=True)
                    else:
                        fd, temp_name = tempfile.mkstemp(prefix=".scidev-rollback-", dir=file_plan.target.parent)
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(file_plan.original)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.chmod(temp_name, stat.S_IMODE(file_plan.target.stat().st_mode) if file_plan.target.exists() else 0o600)
                        os.replace(temp_name, file_plan.target)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{file_plan.path}: {rollback_exc}")
            detail = f"补丁应用失败，已尝试回滚：{type(exc).__name__}: {exc}"
            if rollback_errors:
                detail += "；回滚仍失败：" + "；".join(rollback_errors[:5])
            raise PatchError(detail) from exc
        finally:
            for temp_path in temporary_files.values():
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            for directory in sorted(created_directories, key=lambda path: len(path.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass

        applied_hunks = sorted(
            hunk_id for change in changes for hunk_id in change["selected_hunks"]
        )
        applied_paths = [change["file"].path for change in changes if isinstance(change["file"], PatchFile)]
        declined = sorted(valid_ids - set(applied_hunks))
        return {
            "patch_id": plan.patch_id,
            "applied_paths": applied_paths,
            "applied_hunks": applied_hunks,
            "declined_hunks": declined,
        }
