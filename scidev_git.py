"""Git status, diff, staging and bounded auto-commit helpers."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import difflib
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from typing import Any

from scidev_errors import PermanentError
from scidev_filelock import FileLock, FileLockTimeout
from scidev_paths import _is_protected_workspace_path
from scidev_process import background_process_options
from scidev_runtime import shared_thread_lock


GIT_COMMAND_TIMEOUT_SECONDS = 10.0
GIT_MUTATION_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class GitStatusEntry:
    """One parsed ``git status --porcelain=v1 -z`` record."""

    code: str
    path: str
    original: str | None = None


def parse_porcelain_z(output: str) -> list[GitStatusEntry]:
    """Parse NUL-delimited porcelain once so every Git view agrees."""
    records = output.split("\x00")
    entries: list[GitStatusEntry] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        code = record[:2]
        original = None
        if "R" in code or "C" in code:
            if index < len(records) and records[index]:
                original = records[index]
                index += 1
        entries.append(GitStatusEntry(code=code, path=record[3:], original=original))
    return entries


class GitManager:
    MAX_UNTRACKED_DIFF_BYTES = 240_000
    MAX_AUTO_COMMIT_FILE_BYTES = MAX_UNTRACKED_DIFF_BYTES
    COMMAND_TIMEOUT_SECONDS = GIT_COMMAND_TIMEOUT_SECONDS
    MUTATION_TIMEOUT_SECONDS = GIT_MUTATION_TIMEOUT_SECONDS
    MUTATING_COMMANDS = frozenset({
        "add", "am", "apply", "checkout", "cherry-pick", "clean", "commit",
        "init", "merge", "mv", "rebase", "reset", "restore", "revert", "rm", "stash",
        "switch", "update-index",
    })

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root).resolve()
        self._toplevel_known = False
        self._toplevel_cache: Path | None = None
        self._toplevel_returncode = 0
        # Every GitManager for the same repository shares one lock; separate
        # instances (UI refresh, toolbox diff, auto-commit) otherwise collide
        # on .git/index.lock.
        self._run_lock = shared_thread_lock("git", self.project_root)
        self._repo_lock = threading.RLock()
        self._mutation_state = threading.local()
        # Set when rename detection had to fail closed; exposed for diagnostics.
        self.last_rename_detection_degraded: list[str] = []

    @staticmethod
    def _is_mutating(args: list[str]) -> bool:
        for arg in args:
            if arg.startswith("-"):
                continue
            return arg in GitManager.MUTATING_COMMANDS
        return False

    def _git_lock_path(self) -> Path:
        lock_dir = self.project_root / ".git"
        if not lock_dir.is_dir():
            lock_dir = self.project_root / ".research" / "locks"
        return lock_dir / "scidev-git.lock"

    def _mutation_depth(self) -> int:
        return int(getattr(self._mutation_state, "depth", 0))

    @contextmanager
    def _mutation_session(self):
        """Hold one file lock across a multi-command commit sequence."""
        depth = self._mutation_depth()
        if depth == 0:
            lock = FileLock(self._git_lock_path(), timeout=10.0)
            lock.__enter__()
            self._mutation_state.lock = lock
        self._mutation_state.depth = depth + 1
        try:
            yield
        finally:
            self._mutation_state.depth = depth
            if depth == 0:
                lock = getattr(self._mutation_state, "lock", None)
                self._mutation_state.lock = None
                if lock is not None:
                    lock.__exit__(None, None, None)

    def _run(
        self,
        args: list[str],
        check: bool = False,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["git", *args]
        limit = self.COMMAND_TIMEOUT_SECONDS if timeout is None else timeout
        lock = None
        if self._is_mutating(args) and self._mutation_depth() == 0:
            lock = FileLock(self._git_lock_path(), timeout=10.0)
        for attempt in range(4):
            try:
                with self._run_lock:
                    if lock is None:
                        return subprocess.run(
                            command,
                            cwd=self.project_root,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            check=check,
                            env=env,
                            timeout=limit,
                            **background_process_options(),
                        )
                    with lock:
                        return subprocess.run(
                            command,
                            cwd=self.project_root,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            check=check,
                            env=env,
                            timeout=limit,
                            **background_process_options(),
                        )
            except FileLockTimeout as exc:
                raise PermanentError(
                    f"项目 Git 操作被另一个进程占用：{' '.join(args[:3])}"
                ) from exc
            except subprocess.CalledProcessError as exc:
                # Another process (user's terminal, IDE) can hold the index
                # briefly; retry instead of failing the coding task.
                stderr = exc.stderr or ""
                if "index.lock" in stderr and attempt < 3:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise
            except subprocess.TimeoutExpired as exc:
                if check:
                    raise PermanentError(
                        f"Git 操作超时（{limit:.0f} 秒）：{' '.join(args[:3])}"
                    ) from exc
                return subprocess.CompletedProcess(
                    command, 124, "", f"git 超时（{limit:.0f} 秒）"
                )
            except OSError as exc:
                # A desktop client should still open when Git is not installed.
                # Read-only probes then behave like an unavailable repository;
                # mutating calls keep a useful, conventional command error.
                if check:
                    raise subprocess.CalledProcessError(
                        127,
                        command,
                        output="",
                        stderr=str(exc),
                    ) from exc
                return subprocess.CompletedProcess(command, 127, "", str(exc))
        raise PermanentError(f"Git 索引持续被占用：{' '.join(args[:3])}")

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        return os.path.normcase(os.fspath(left)) == os.path.normcase(os.fspath(right))

    def _invalidate_repo_cache(self) -> None:
        with self._repo_lock:
            self._toplevel_known = False
            self._toplevel_cache = None

    def repo_toplevel(self) -> Path | None:
        """Return the work-tree root that actually owns this workspace."""
        if not self._toplevel_known:
            with self._repo_lock:
                if not self._toplevel_known:
                    probe = self._run(["rev-parse", "--show-toplevel"])
                    text = probe.stdout.strip()
                    self._toplevel_returncode = probe.returncode
                    self._toplevel_cache = (
                        Path(text).resolve() if probe.returncode == 0 and text else None
                    )
                    self._toplevel_known = True
        return self._toplevel_cache

    def enclosing_repo_root(self) -> Path | None:
        """Return the outer repository when the workspace is only a subfolder of it."""
        toplevel = self.repo_toplevel()
        if toplevel is None or self._same_path(toplevel, self.project_root):
            return None
        return toplevel

    def _require_own_repo(self) -> None:
        """Refuse Git mutations that would land in an enclosing repository."""
        enclosing = self.enclosing_repo_root()
        if enclosing is not None:
            raise PermanentError(
                "工作区位于更大的 Git 仓库内，为避免误改外层仓库，已停止 Git 操作："
                f"\n{enclosing}\n请在仓库根目录打开工作区，或先为该目录初始化独立仓库。"
            )

    def is_repo(self) -> bool:
        """True only when this workspace is the repository root itself."""
        toplevel = self.repo_toplevel()
        return toplevel is not None and self._same_path(toplevel, self.project_root)

    def init(self) -> str:
        self._require_own_repo()
        if not self.is_repo():
            self._run(["init"], check=True, timeout=self.MUTATION_TIMEOUT_SECONDS)
            self._invalidate_repo_cache()
        if self._run(["config", "--get", "user.name"]).returncode != 0:
            self._run(["config", "user.name", "SciDevHarness"], check=True)
        if self._run(["config", "--get", "user.email"]).returncode != 0:
            self._run(["config", "user.email", "scidev@localhost"], check=True)
        return self.status()

    def head_sha(self) -> str:
        if not self.is_repo():
            return ""
        result = self._run(["rev-parse", "HEAD"])
        return result.stdout.strip() if result.returncode == 0 else ""

    def _tracked_at_head(self, relative: str) -> bool:
        """True when the path exists in HEAD, so a missing file is a deletion."""
        if not relative or not self.is_repo():
            return False
        result = self._run(
            ["--literal-pathspecs", "ls-tree", "-r", "--name-only", "HEAD", "--", relative]
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def branch(self) -> str:
        if not self.is_repo():
            return "-"
        result = self._run(["branch", "--show-current"])
        return result.stdout.strip() or "(detached)"

    def status(self, *, protect_sensitive: bool = False) -> str:
        toplevel = self.repo_toplevel()
        if toplevel is None:
            if self._toplevel_returncode == 127:
                return "Git 不可用：请安装 Git CLI"
            if self._toplevel_returncode == 124:
                return "Git 响应超时；请检查仓库是否被锁定"
            return "未初始化 Git 仓库"
        if not self._same_path(toplevel, self.project_root):
            return f"工作区在更大的 Git 仓库内：{toplevel}（不在此处执行 Git 操作）"
        if protect_sensitive:
            paths = self.status_paths()
            safe_paths = sorted(path for path in paths if not _is_protected_workspace_path(path))
            hidden_count = len(paths) - len(safe_paths)
            lines = [f"已变更：{path}" for path in safe_paths]
            if hidden_count:
                lines.append(f"[{hidden_count} 个敏感/内部路径已隐藏]")
            return "\n".join(lines) or "工作区干净"
        result = self._run(["status", "--short"])
        if result.returncode == 124:
            return "Git 状态读取超时；请检查仓库是否被锁定"
        return result.stdout.strip() or "工作区干净"

    def _status_records(self) -> list[GitStatusEntry]:
        """Single porcelain query shared by every status view."""
        if not self.is_repo():
            return []
        result = self._run(["--literal-pathspecs", "status", "--porcelain=v1", "-z", "-uall"])
        if result.returncode != 0:
            return []
        return parse_porcelain_z(result.stdout)

    def status_entries(self) -> list[dict[str, str]]:
        """Return machine-readable porcelain entries for the source-control view."""
        return [
            {"code": entry.code, "path": entry.path, "raw": entry.path}
            for entry in self._status_records()
        ]

    def status_path_groups(self) -> list[set[str]]:
        """Return changed paths grouped by status record, keeping renames together."""
        groups: list[set[str]] = []
        for entry in self._status_records():
            group = {entry.path}
            if entry.original:
                group.add(entry.original)
            groups.append(group)
        return groups

    def snapshot(self) -> dict[str, Any]:
        """Return one coherent Git view for the UI with a single ``status`` call."""
        toplevel = self.repo_toplevel()
        if toplevel is None:
            if self._toplevel_returncode == 127:
                return {"state": "unavailable", "text": "Git 不可用：请安装 Git CLI", "entries": []}
            if self._toplevel_returncode == 124:
                return {"state": "timeout", "text": "Git 响应超时；请检查仓库是否被锁定", "entries": []}
            return {"state": "no_repo", "text": "未初始化 Git 仓库", "entries": []}
        if not self._same_path(toplevel, self.project_root):
            return {
                "state": "nested",
                "text": f"工作区在更大的 Git 仓库内：{toplevel}（不在此处执行 Git 操作）",
                "entries": [],
                "enclosing": str(toplevel),
            }
        result = self._run(["--literal-pathspecs", "status", "--porcelain=v1", "-z", "-uall"])
        if result.returncode == 124:
            return {"state": "timeout", "text": "Git 状态读取超时；请检查仓库是否被锁定", "entries": []}
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            reason = detail[0] if detail else f"退出码 {result.returncode}"
            return {"state": "error", "text": f"Git 状态读取失败：{reason}", "entries": []}
        entries = parse_porcelain_z(result.stdout)
        head = self.head_sha()
        payload = [
            {"code": entry.code, "path": entry.path, "original": entry.original}
            for entry in entries
        ]
        if not entries:
            return {"state": "clean", "text": "工作区干净", "entries": payload, "head": head}
        lines = [f"{entry.code} {entry.path}" for entry in entries]
        return {"state": "changes", "text": "\n".join(lines), "entries": payload, "head": head}

    def status_paths(self) -> set[str]:
        """Return exact changed paths, using NUL-delimited porcelain for unusual filenames."""
        groups = self.status_path_groups()
        return set().union(*groups) if groups else set()

    def _sensitive_rename_groups(self) -> list[set[str]]:
        """Detect protected renames without staging unrelated dirty workspace files."""
        if not self.head_sha():
            return []

        status = self._run(["--literal-pathspecs", "status", "--porcelain=v1", "-z", "-uall"])
        protected_deletions: list[str] = []
        untracked_additions: list[str] = []
        detected: list[set[str]] = []
        for entry in parse_porcelain_z(status.stdout):
            if entry.code == "??":
                untracked_additions.append(entry.path)
            elif "D" in entry.code and _is_protected_workspace_path(entry.path):
                protected_deletions.append(entry.path)
            if entry.original:
                group = {entry.path, entry.original}
                if len(group) > 1 and any(_is_protected_workspace_path(path) for path in group):
                    detected.append(group)

        # Internal Harness files (for example .research databases and screenshots)
        # are protected too, but do not justify staging every dirty path merely to
        # look for a rename. Only this delete/add pair needs Git's rename detector.
        if not protected_deletions or not untracked_additions:
            return detected

        candidates: list[str] = []
        for relative in sorted(set(protected_deletions + untracked_additions)):
            unresolved = self.project_root / Path(relative)
            try:
                target = unresolved.resolve(strict=False)
                if not target.is_relative_to(self.project_root):
                    continue
                if unresolved.is_symlink() or unresolved.is_junction():
                    continue
                if target.exists() and target.is_file() and target.stat().st_size > self.MAX_AUTO_COMMIT_FILE_BYTES:
                    continue
            except (OSError, RuntimeError, ValueError):
                continue
            candidates.append(relative)
        if len(candidates) < 2:
            return detected

        try:
            with tempfile.TemporaryDirectory(prefix="scidev-git-rename-check-") as temp_dir:
                environment = os.environ.copy()
                environment["GIT_INDEX_FILE"] = str(Path(temp_dir) / "index")
                self._run(["read-tree", self.head_sha()], check=True, env=environment)
                self._run(
                    ["--literal-pathspecs", "add", "-A", "--", *candidates],
                    check=True,
                    env=environment,
                )
                result = self._run(
                    ["--literal-pathspecs", "diff", "--cached", "--find-renames", "--name-status", "-z", "HEAD"],
                    env=environment,
                )
                records = result.stdout.split("\x00")
                index = 0
                while index < len(records):
                    status_code = records[index]
                    index += 1
                    if not status_code:
                        continue
                    path_count = 2 if status_code.startswith(("R", "C")) else 1
                    group = {path for path in records[index:index + path_count] if path}
                    index += path_count
                    if len(group) > 1 and any(_is_protected_workspace_path(path) for path in group):
                        detected.append(group)
        except (subprocess.CalledProcessError, PermanentError):
            # Fail closed: when rename detection cannot run (vanished path,
            # locked index, timeout) hide every candidate so a protected
            # rename can never leak into the review diff.
            detected.append(set(candidates))
            self.last_rename_detection_degraded = sorted(candidates)[:50]
        return detected

    def stage_all(self) -> None:
        self._require_own_repo()
        self.init()
        self._run(["add", "-A"], check=True, timeout=self.MUTATION_TIMEOUT_SECONDS)

    def stage_path(self, path: Path) -> None:
        self._require_own_repo()
        self.init()
        relative = self._relative_path(path)
        self._run(["add", "--", relative], check=True, timeout=self.MUTATION_TIMEOUT_SECONDS)

    def unstage_all(self) -> None:
        self._require_own_repo()
        if not self.is_repo():
            return
        if self.head_sha():
            self._run(["reset", "HEAD", "--"], check=True, timeout=self.MUTATION_TIMEOUT_SECONDS)
        else:
            self._run(["reset"], check=True, timeout=self.MUTATION_TIMEOUT_SECONDS)

    def unstage_path(self, path: Path) -> None:
        self._require_own_repo()
        if not self.is_repo():
            return
        relative = self._relative_path(path)
        if self.head_sha():
            self._run(["reset", "HEAD", "--", relative], check=True, timeout=self.MUTATION_TIMEOUT_SECONDS)
        else:
            self._run(["reset", "--", relative], check=True, timeout=self.MUTATION_TIMEOUT_SECONDS)

    def _relative_path(self, path: Path) -> str:
        target = Path(path).resolve()
        if not target.is_relative_to(self.project_root):
            raise ValueError("path must be inside the project")
        return target.relative_to(self.project_root).as_posix()

    def log(self, limit: int = 20) -> str:
        if not self.is_repo() or not self.head_sha():
            return "暂无提交"
        result = self._run(["log", f"-{limit}", "--oneline", "--decorate"])
        return result.stdout.strip()

    def diff(self, path: Path | None = None, *, protect_sensitive: bool = False) -> str:
        """Return a reviewable diff for the whole worktree or one file."""
        if not self.is_repo():
            return "Git 尚未初始化"
        protected_changes: set[str] = set()
        selected_paths: list[str] = []
        if path is None and protect_sensitive:
            path_groups = self.status_path_groups()
            sensitive_rename_groups = self._sensitive_rename_groups()
            sensitive_rename_paths = set().union(*sensitive_rename_groups) if sensitive_rename_groups else set()
            changed_paths = set().union(*path_groups) if path_groups else set()
            selected_paths = sorted(
                relative
                for group in path_groups
                if not any(
                    _is_protected_workspace_path(item) or item in sensitive_rename_paths
                    for item in group
                )
                for relative in group
            )
            protected_changes = (changed_paths - set(selected_paths)) | sensitive_rename_paths
            if not selected_paths:
                return "敏感/内部路径的差异已隐藏" if protected_changes else ""

        args = ["--literal-pathspecs", "diff", "--no-ext-diff"]
        has_head = bool(self.head_sha())
        if has_head:
            # Comparing with HEAD includes both staged and unstaged changes,
            # which is what an IDE's file diff should show.
            args.append("HEAD")
        relative = ""
        if path is not None:
            target = Path(path).resolve()
            if not target.is_relative_to(self.project_root):
                return "拒绝读取项目目录之外的文件"
            relative = target.relative_to(self.project_root).as_posix()
            if protect_sensitive and _is_protected_workspace_path(relative):
                return "敏感/内部路径的差异已隐藏"
            args.extend(["--", relative])
        elif protect_sensitive:
            args.extend(["--", *selected_paths])
        result = self._run(args)
        diff_text = result.stdout
        if not has_head:
            staged_args = ["--literal-pathspecs", "diff", "--cached", "--no-ext-diff"]
            if relative:
                staged_args.extend(["--", relative])
            elif protect_sensitive:
                staged_args.extend(["--", *selected_paths])
            staged = self._run(staged_args)
            diff_text += staged.stdout
        if path is None:
            # ``git diff`` does not include untracked files. The Agent needs
            # their content in its review context, otherwise a newly-created
            # file can be invisible until the first commit.
            untracked = self._run(["ls-files", "--others", "--exclude-standard", "-z"])
            for raw_relative in untracked.stdout.split("\x00"):
                relative_path = raw_relative
                if not relative_path:
                    continue
                if protect_sensitive and _is_protected_workspace_path(relative_path):
                    continue
                unresolved = self.project_root / Path(relative_path)
                if unresolved.is_symlink():
                    continue
                candidate = unresolved.resolve()
                if (
                    not candidate.is_relative_to(self.project_root)
                    or not candidate.is_file()
                ):
                    continue
                try:
                    raw_content = candidate.read_bytes()
                except OSError:
                    continue
                if len(raw_content) > self.MAX_UNTRACKED_DIFF_BYTES or b"\x00" in raw_content:
                    continue
                content = raw_content.decode("utf-8", errors="replace").splitlines(keepends=True)
                diff_text += "".join(
                    difflib.unified_diff(
                        [],
                        content,
                        fromfile="/dev/null",
                        tofile=relative_path,
                    )
                )
        if diff_text.strip():
            return diff_text
        if path is not None and relative:
            status = self._run(["status", "--short", "--", relative]).stdout.strip()
            if status.startswith("??"):
                try:
                    content = Path(path).read_text(encoding="utf-8").splitlines(keepends=True)
                except (OSError, UnicodeError):
                    return f"{relative}\n\n新文件无法以文本 diff 展示"
                return "".join(
                    difflib.unified_diff(
                        [],
                        content,
                        fromfile="/dev/null",
                        tofile=relative,
                    )
                )
        return ""

    def commit_changes(self, message: str) -> str:
        self.init()
        self._run(["add", "-A"], check=True, timeout=self.MUTATION_TIMEOUT_SECONDS)
        staged = self._run(["diff", "--cached", "--quiet"])
        if staged.returncode == 0:
            return self.head_sha()
        self._run(["commit", "-m", message], check=True, timeout=self.MUTATION_TIMEOUT_SECONDS)
        return self.head_sha()

    def filter_auto_commit_paths(
        self,
        paths: set[str] | list[str] | tuple[str, ...],
    ) -> tuple[set[str], dict[str, dict[str, str | int]]]:
        """Keep unsafe, sensitive, linked, and oversized artifacts out of Agent commits."""
        included: set[str] = set()
        skipped: dict[str, dict[str, str | int]] = {}
        sensitive_rename_paths = {
            path
            for group in self._sensitive_rename_groups()
            if len(group) > 1 and any(_is_protected_workspace_path(path) for path in group)
            for path in group
        }
        for relative in sorted({str(path) for path in paths}):
            if _is_protected_workspace_path(relative) or relative in sensitive_rename_paths:
                skipped[relative] = {"reason": "protected_path"}
                continue

            unresolved = self.project_root / Path(relative)
            try:
                target = unresolved.resolve(strict=False)
                if not target.is_relative_to(self.project_root):
                    skipped[relative] = {"reason": "outside_workspace"}
                    continue
                if not unresolved.exists() and not self._tracked_at_head(relative):
                    # The model created a file and then removed it again; there
                    # is nothing to commit, and passing the vanished path to
                    # ``git add`` would abort the whole auto-commit.
                    continue
                if unresolved.is_symlink() or unresolved.is_junction():
                    skipped[relative] = {"reason": "linked_path"}
                    continue
                if target.exists() and target.is_file():
                    size_bytes = target.stat().st_size
                    if size_bytes > self.MAX_AUTO_COMMIT_FILE_BYTES:
                        skipped[relative] = {"reason": "file_too_large", "size_bytes": size_bytes}
                        continue
                    try:
                        content = target.read_bytes()
                        content.decode("utf-8")
                    except (OSError, UnicodeDecodeError):
                        skipped[relative] = {"reason": "non_text_file"}
                        continue
                    if b"\x00" in content:
                        skipped[relative] = {"reason": "non_text_file"}
                        continue
            except (OSError, RuntimeError, ValueError):
                skipped[relative] = {"reason": "path_unavailable"}
                continue
            included.add(relative)
        return included, skipped

    def commit_paths(self, message: str, paths: set[str] | list[str] | tuple[str, ...]) -> str:
        """Commit only the given paths without consuming the user's existing index."""
        relative_paths = sorted({self._relative_path(self.project_root / Path(path)) for path in paths})
        if not relative_paths:
            return ""
        with self._mutation_session():
            return self._commit_paths_locked(message, relative_paths)

    def _commit_paths_locked(self, message: str, relative_paths: list[str]) -> str:
        self.init()
        base_sha = self.head_sha()
        with tempfile.TemporaryDirectory(prefix="scidev-git-index-") as temp_dir:
            environment = os.environ.copy()
            environment["GIT_INDEX_FILE"] = str(Path(temp_dir) / "index")
            read_tree = ["read-tree", base_sha] if base_sha else ["read-tree", "--empty"]
            self._run(read_tree, check=True, env=environment)

            included_paths = []
            for relative in relative_paths:
                if not (self.project_root / Path(relative)).exists() and not self._tracked_at_head(relative):
                    # A path that was created and removed during the run has no
                    # index entry; ``git add`` would fail the entire commit.
                    continue
                ignored = self._run(["check-ignore", "-q", "--", relative])
                if ignored.returncode != 0:
                    included_paths.append(relative)
            if not included_paths:
                return ""

            self._run(
                ["--literal-pathspecs", "add", "-A", "--", *included_paths],
                check=True,
                env=environment,
            )
            staged = self._run(["diff", "--cached", "--quiet"], env=environment)
            if staged.returncode == 0:
                return ""
            if staged.returncode != 1:
                raise subprocess.CalledProcessError(staged.returncode, staged.args, staged.stdout, staged.stderr)
            self._run(["commit", "-m", message], check=True, env=environment)

        result_sha = self.head_sha()
        if result_sha:
            # The selected paths were clean at session start; refresh only those entries
            # so unrelated user staging and worktree edits remain untouched.
            self._run(
                ["--literal-pathspecs", "reset", "--quiet", "HEAD", "--", *included_paths],
                check=True,
            )
        return result_sha
