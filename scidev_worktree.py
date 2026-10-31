"""Git worktree isolation for headless coding tasks."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess

MAX_TASK_ID = 40
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentWorktree:
    path: Path
    branch: str
    base_sha: str
    repo_root: Path


def _git(repo: Path, *args: str, timeout: float = 60.0) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"Git 不可用：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-500:]
        raise WorktreeError(detail or f"git {' '.join(args)} 失败")
    return completed.stdout.strip()


class WorktreeManager:
    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root).resolve()
        self.worktrees_root = self.repo_root / ".research" / "worktrees"

    def _require_repo(self) -> None:
        try:
            top = _git(self.repo_root, "rev-parse", "--show-toplevel")
        except WorktreeError as exc:
            raise WorktreeError("当前工作区不是 Git 仓库，无法创建 worktree") from exc
        if Path(top).resolve() != self.repo_root:
            raise WorktreeError(f"工作区不是仓库根目录：{top}")

    @staticmethod
    def _safe_task_id(task_id: str) -> str:
        cleaned = _SAFE_ID.sub("-", str(task_id or "").strip()).strip("-.")
        if not cleaned:
            cleaned = datetime.now(timezone.utc).strftime("task-%Y%m%d%H%M%S")
        return cleaned[:MAX_TASK_ID]

    def create(self, task_id: str) -> AgentWorktree:
        self._require_repo()
        base_sha = _git(self.repo_root, "rev-parse", "HEAD")
        safe_id = self._safe_task_id(task_id)
        branch = f"scidev/agent-{safe_id}"
        path = self.worktrees_root / safe_id
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise WorktreeError(f"worktree 目录已存在：{path}")
        try:
            _git(self.repo_root, "worktree", "add", "-b", branch, str(path), base_sha)
        except WorktreeError:
            # A stale directory can remain after a crash; never delete it blindly.
            raise
        return AgentWorktree(path=path.resolve(), branch=branch, base_sha=base_sha, repo_root=self.repo_root)

    def remove(self, worktree: AgentWorktree, *, force: bool = False) -> None:
        if not force:
            dirty = _git(worktree.path, "status", "--porcelain").strip()
            if dirty:
                raise WorktreeError("worktree 有未提交改动；确认后再用 force 删除")
        _git(self.repo_root, "worktree", "remove", "--force" if force else "--", str(worktree.path))
        _git(self.repo_root, "worktree", "prune")
        try:
            _git(self.repo_root, "branch", "-D", worktree.branch)
        except WorktreeError:
            # The branch may already be checked out elsewhere or intentionally kept.
            pass

    def list(self) -> list[AgentWorktree]:
        self._require_repo()
        output = _git(self.repo_root, "worktree", "list", "--porcelain")
        result: list[AgentWorktree] = []
        current: dict[str, str] = {}
        for line in output.splitlines() + [""]:
            if not line:
                if current.get("worktree") and current.get("branch"):
                    result.append(AgentWorktree(
                        path=Path(current["worktree"]).resolve(),
                        branch=current["branch"].removeprefix("refs/heads/"),
                        base_sha=current.get("HEAD", ""),
                        repo_root=self.repo_root,
                    ))
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        return result

    def cleanup_empty_root(self) -> None:
        if not self.worktrees_root.is_dir():
            return
        try:
            next(self.worktrees_root.iterdir())
        except StopIteration:
            shutil.rmtree(self.worktrees_root, ignore_errors=True)
        except OSError:
            pass
