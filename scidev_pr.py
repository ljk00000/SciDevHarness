"""Create a GitHub pull request from an isolated task branch.

This module never stores credentials. It shells out to the authenticated
GitHub CLI and git, and callers must explicitly opt into network access.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

_GITHUB_REMOTE = re.compile(
    r"(?:github\.com[:/])(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?(?:/)?$"
)
_BASE_BRANCHES = frozenset({"main", "master"})


class PullRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullRequestPlan:
    workspace: Path
    repo: str
    branch: str
    base: str
    title: str
    body: str
    draft: bool
    remote: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run(
    cwd: Path,
    args: list[str],
    *,
    timeout: float = 120.0,
    runner: Runner | None = None,
) -> subprocess.CompletedProcess[str]:
    command = args
    try:
        return (runner or subprocess.run)(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PullRequestError(f"命令启动失败：{exc}") from exc


def _output(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stdout or "").strip()


def parse_github_remote(url: str) -> tuple[str, str] | None:
    match = _GITHUB_REMOTE.search(str(url or "").strip())
    if not match:
        return None
    owner = match.group("owner")
    repo = match.group("repo")
    if not owner or not repo:
        return None
    return owner, repo


def _git(workspace: Path, *args: str, runner: Runner | None = None) -> str:
    completed = _run(workspace, ["git", *args], runner=runner)
    if completed.returncode != 0:
        raise PullRequestError((completed.stderr or completed.stdout or "git 失败").strip()[-500:])
    return _output(completed)


def _network_denied(workspace: Path) -> bool:
    try:
        settings = json.loads((workspace / ".research" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    policy = str(os.getenv("SCIDEV_NETWORK_POLICY") or settings.get("network_policy") or "ask").strip().lower()
    return policy in {"deny", "denied", "off", "block"}


def build_plan(
    workspace: Path,
    *,
    base: str = "",
    title: str = "",
    body: str = "",
    draft: bool = False,
    runner: Runner | None = None,
) -> PullRequestPlan:
    workspace = Path(workspace).resolve()
    if not workspace.is_dir():
        raise PullRequestError(f"工作区不存在：{workspace}")
    top = _git(workspace, "rev-parse", "--show-toplevel", runner=runner)
    if Path(top).resolve() != workspace:
        raise PullRequestError("请在仓库根目录创建 PR")
    branch = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD", runner=runner)
    if not branch or branch == "HEAD":
        raise PullRequestError("当前处于 detached HEAD，无法确定 PR 分支")
    if branch in _BASE_BRANCHES:
        raise PullRequestError(f"当前在 {branch} 分支；请先在任务 worktree 或功能分支上运行")
    remote = _git(workspace, "remote", "get-url", "origin", runner=runner)
    parsed = parse_github_remote(remote)
    if parsed is None:
        raise PullRequestError(f"origin 不是 GitHub 仓库：{remote}")
    repo = f"{parsed[0]}/{parsed[1]}"
    selected_base = str(base or "").strip()
    if not selected_base:
        try:
            selected_base = _git(
                workspace,
                "symbolic-ref", "--short", "refs/remotes/origin/HEAD", runner=runner,
            ).removeprefix("origin/")
        except PullRequestError:
            selected_base = "main"
    if selected_base == branch:
        raise PullRequestError("PR base 不能与当前分支相同")
    title_text = " ".join(str(title or "").strip().split())[:120] or branch
    body_text = str(body or "").strip()[:8000]
    return PullRequestPlan(
        workspace=workspace,
        repo=repo,
        branch=branch,
        base=selected_base,
        title=title_text,
        body=body_text,
        draft=bool(draft),
        remote=remote,
    )


def create_pull_request(
    plan: PullRequestPlan,
    *,
    runner: Runner | None = None,
    allow_network: bool = False,
) -> dict[str, Any]:
    if _network_denied(plan.workspace) and not allow_network:
        raise PullRequestError("网络策略为 deny；使用 --allow-network 显式放行 PR 创建")
    push = _run(plan.workspace, ["git", "push", "-u", "origin", plan.branch], timeout=300, runner=runner)
    if push.returncode != 0:
        raise PullRequestError((push.stderr or push.stdout or "git push 失败").strip()[-800:])
    command = [
        "gh", "pr", "create",
        "--repo", plan.repo,
        "--head", plan.branch,
        "--base", plan.base,
        "--title", plan.title,
    ]
    if plan.body:
        command.extend(["--body", plan.body])
    else:
        command.append("--fill")
    if plan.draft:
        command.append("--draft")
    created = _run(plan.workspace, command, timeout=180, runner=runner)
    if created.returncode != 0:
        raise PullRequestError((created.stderr or created.stdout or "gh pr create 失败").strip()[-800:])
    return {
        "repo": plan.repo,
        "branch": plan.branch,
        "base": plan.base,
        "url": _output(created),
        "draft": plan.draft,
    }
