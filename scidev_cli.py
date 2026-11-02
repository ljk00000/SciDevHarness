"""Headless command-line entry point for SciDevHarness.

This module intentionally avoids importing PySide6 so it can run in CI,
scripts and evaluation harnesses.  It uses the same CodingAgent, execution
policy, Git integration and audit ledger as the desktop client.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from scidev_core import CodingAgent, EventLedger, GitManager, PermanentError, SummarySettings, new_id
from scidev_policy import ExecutionMode
from scidev_attachments import AttachmentError, load_image_file
from scidev_eval import EvalError, EvalRunner
from scidev_worktree import WorktreeError, WorktreeManager
from scidev_pr import (
    PullRequestError,
    build_plan,
    create_pull_request,
    pull_request_diff,
    review_pull_request,
    view_pull_request,
)
from scidev_migrations import migrate_project
from scidev_update import APP_VERSION, UpdateError, check_for_update, detect_repo, download_asset, launch_installer
from scidev_mcp_server import MCPServer, serve_stdio
from scidev_errors import classify_message
from scidev_mcp import MCPError, MCPManager
from scidev_sandbox import DockerSandbox, SandboxError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scidev",
        description="Run a SciDevHarness coding task without the desktop UI.",
    )
    parser.add_argument("--version", action="version", version=f"SciDevHarness {APP_VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    exec_parser = subparsers.add_parser(
        "exec",
        help="execute one coding task",
        description="Execute one coding task in a workspace and print the result.",
    )
    exec_parser.add_argument("--workspace", required=True, help="project directory to edit")
    exec_parser.add_argument("--task", required=True, help="coding task for the Agent")
    exec_parser.add_argument("--json", action="store_true", help="emit one JSON object instead of prose")
    exec_parser.add_argument(
        "--result-file",
        default="",
        help="also write the final JSON result to this workspace-relative file (atomic)",
    )
    exec_parser.add_argument(
        "--execution-mode",
        choices=[mode.value for mode in ExecutionMode],
        default=ExecutionMode.WORKSPACE_WRITE.value,
        help="command policy for this run (default: workspace-write)",
    )
    exec_parser.add_argument(
        "--sandbox",
        choices=["local", "docker"],
        default=os.getenv("SCIDEV_SANDBOX_BACKEND", "local"),
        help="command isolation backend (default: SCIDEV_SANDBOX_BACKEND or local)",
    )
    exec_parser.add_argument(
        "--sandbox-image",
        default=os.getenv("SCIDEV_SANDBOX_IMAGE", ""),
        help="Docker image with the project toolchain (required for docker)",
    )
    exec_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="do not require a passing current-code check before completion",
    )
    exec_parser.add_argument(
        "--patch-approval",
        choices=["auto", "reject"],
        default="auto",
        help=(
            "auto applies every patch hunk headlessly (still limited to the workspace); "
            "reject leaves existing files untouched (default: auto)"
        ),
    )
    exec_parser.add_argument(
        "--max-turns",
        type=int,
        default=0,
        help="per-task model turn cap (0 uses the project setting, max 64)",
    )
    exec_parser.add_argument(
        "--max-tokens",
        type=int,
        default=0,
        help="per-task token cap (0 uses the project setting or no cap)",
    )
    exec_parser.add_argument(
        "--restore-on-failure",
        action="store_true",
        help="restore files changed by the task when it fails",
    )
    exec_parser.add_argument(
        "--allow-no-changes",
        action="store_true",
        help="treat a completed run with no file changes as success (for questions/analysis)",
    )
    exec_parser.add_argument("--session-id", default="", help="stable session id for retries")
    exec_parser.add_argument("--api-base", default="", help="OpenAI-compatible base URL")
    exec_parser.add_argument("--api-key", default="", help="API key (prefer the environment variable)")
    exec_parser.add_argument("--model", default="", help="model id")
    exec_parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="attach a PNG/JPEG/GIF/WebP image (repeatable, max 4)",
    )
    exec_parser.add_argument(
        "--worktree",
        action="store_true",
        help="run in an isolated git worktree branch instead of the current checkout",
    )
    exec_parser.add_argument(
        "--worktree-id",
        default="",
        help="stable worktree id (default: session id)",
    )
    exec_parser.add_argument(
        "--request-timeout",
        type=float,
        default=0.0,
        help="provider request timeout in seconds (default: environment value)",
    )

    sessions_parser = subparsers.add_parser(
        "sessions",
        help="list recent coding sessions",
        description="List recent coding sessions so one can be resumed with --session-id.",
    )
    sessions_parser.add_argument("--workspace", required=True, help="project directory")
    sessions_parser.add_argument("--json", action="store_true", help="emit a JSON array")
    sessions_parser.add_argument("--limit", type=int, default=20, help="maximum sessions (default: 20)")
    eval_parser = subparsers.add_parser(
        "eval",
        help="run a real-repository evaluation suite",
        description="Run isolated fixed tasks and deterministic checks, then write a JSON report.",
    )
    eval_parser.add_argument("--suite", required=True, help="eval suite JSON path")
    eval_parser.add_argument("--output", default="", help="report JSON path (default: timestamped under .research/evals)")
    eval_parser.add_argument("--jobs", type=int, default=1, help="parallel tasks (1-8, default: 1)")
    eval_parser.add_argument("--json", action="store_true", help="print the report as JSON")
    pr_parser = subparsers.add_parser(
        "pr",
        help="create a GitHub pull request from a task branch",
        description="Push the current branch and create a GitHub PR with the authenticated gh CLI.",
    )
    pr_parser.add_argument("--workspace", required=True, help="repository or task worktree")
    pr_parser.add_argument("--base", default="", help="base branch (default: origin/HEAD, then main)")
    pr_parser.add_argument("--title", default="", help="PR title (default: branch name)")
    pr_parser.add_argument("--body", default="", help="PR body; omitted means --fill")
    pr_parser.add_argument("--draft", action="store_true", help="create a draft PR")
    pr_parser.add_argument("--allow-network", action="store_true", help="override a deny network policy")
    pr_parser.add_argument("--dry-run", action="store_true", help="print the plan without pushing")
    pr_parser.add_argument("--json", action="store_true", help="emit one JSON object")
    pr_view_parser = subparsers.add_parser(
        "pr-view",
        help="show GitHub pull request metadata",
        description="Read a pull request with the authenticated gh CLI.",
    )
    pr_view_parser.add_argument("--workspace", required=True)
    pr_view_parser.add_argument("--number", required=True, help="pull request number")
    pr_view_parser.add_argument("--allow-network", action="store_true")
    pr_view_parser.add_argument("--json", action="store_true")
    pr_diff_parser = subparsers.add_parser(
        "pr-diff",
        help="show a bounded GitHub pull request diff",
        description="Read a pull request diff with the authenticated gh CLI.",
    )
    pr_diff_parser.add_argument("--workspace", required=True)
    pr_diff_parser.add_argument("--number", required=True, help="pull request number")
    pr_diff_parser.add_argument("--max-chars", type=int, default=300_000)
    pr_diff_parser.add_argument("--allow-network", action="store_true")
    pr_diff_parser.add_argument("--json", action="store_true")
    pr_review_parser = subparsers.add_parser(
        "pr-review",
        help="approve, comment on, or request changes on a GitHub PR",
        description="Submit a PR review with the authenticated gh CLI.",
    )
    pr_review_parser.add_argument("--workspace", required=True)
    pr_review_parser.add_argument("--number", required=True, help="pull request number")
    pr_review_parser.add_argument(
        "--action", required=True,
        choices=("approve", "comment", "request-changes"),
    )
    pr_review_parser.add_argument("--body", default="", help="review body")
    pr_review_parser.add_argument("--allow-network", action="store_true")
    pr_review_parser.add_argument("--json", action="store_true")
    migrate_parser = subparsers.add_parser(
        "migrate",
        help="migrate project settings, sessions and event DB to the current schema",
        description="Idempotent, backup-first migration of .research state.",
    )
    migrate_parser.add_argument("--workspace", required=True, help="project directory")
    migrate_parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    migrate_parser.add_argument("--json", action="store_true", help="emit one JSON object")
    update_parser = subparsers.add_parser(
        "update",
        help="check GitHub releases for a newer build",
        description="Report a newer GitHub release; --download verifies it without installing.",
    )
    update_parser.add_argument("--workspace", default=".", help="project used to detect the GitHub origin")
    update_parser.add_argument("--repo", default="", help="override owner/repo")
    update_parser.add_argument("--json", action="store_true")
    update_parser.add_argument(
        "--download", default="", metavar="PATH",
        help="download the release asset to PATH (or an existing directory) and verify its SHA-256",
    )
    update_parser.add_argument(
        "--sha256", default="",
        help="expected SHA-256 when the Release does not expose a digest",
    )
    update_parser.add_argument(
        "--install", action="store_true",
        help="after a verified --download, launch the installer UI (never silent)",
    )
    update_parser.add_argument(
        "--fail-if-available", action="store_true",
        help="exit 1 when a newer release exists",
    )
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="inspect MCP tools, resources and prompts",
        description="Start configured MCP servers and list or read their capabilities.",
    )
    mcp_parser.add_argument("--workspace", default=".", help="project directory")
    mcp_parser.add_argument("--read", nargs=2, metavar=("SERVER", "URI"), help="read one resource")
    mcp_parser.add_argument("--prompt", nargs=2, metavar=("SERVER", "NAME"), help="render one prompt")
    mcp_parser.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE", help="prompt argument")
    mcp_parser.add_argument("--json", action="store_true")
    mcp_serve_parser = subparsers.add_parser(
        "mcp-serve",
        help="serve read-only project tools over MCP stdio",
        description="Expose search/read/list/context/git-diff tools to another MCP client.",
    )
    mcp_serve_parser.add_argument("--workspace", default=".", help="project directory")
    sandbox_parser = subparsers.add_parser(
        "sandbox-check",
        help="report whether the selected command sandbox is available",
        description="Check local/docker command isolation before running a task.",
    )
    sandbox_parser.add_argument("--backend", choices=("local", "docker"), default="")
    sandbox_parser.add_argument("--image", default="")
    sandbox_parser.add_argument("--json", action="store_true")
    return parser


def _configure_provider_environment(args: argparse.Namespace) -> None:
    """Apply explicit CLI provider flags without printing or persisting secrets."""
    if args.api_base:
        os.environ["SCIDEV_API_BASE"] = args.api_base
    if args.api_key:
        os.environ["SCIDEV_API_KEY"] = args.api_key
    if args.model:
        os.environ["SCIDEV_MODEL"] = args.model
    if args.request_timeout:
        os.environ["SCIDEV_REQUEST_TIMEOUT_SECONDS"] = str(args.request_timeout)


def _result_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "session_id": result.get("session_id", ""),
        "status": result.get("status", "completed"),
        "text": result.get("text", ""),
        "summary": result.get("summary", ""),
        "git_result_sha": result.get("git_result_sha", ""),
        "verification_report": result.get("verification_report"),
        "no_changes_detected": bool(result.get("no_changes_detected")),
        "checkpoint_id": result.get("checkpoint_id", ""),
        "turn_count": int(result.get("turn_count", 0) or 0),
        "usage": result.get("usage") or {},
    }


def _print_human(payload: dict[str, Any]) -> None:
    print(payload.get("text") or "(Agent returned no final text)")
    print()
    print(f"session: {payload.get('session_id') or '-'}")
    print(f"git: {payload.get('git_result_sha') or 'no commit'}")
    if payload.get("worktree"):
        print(f"worktree: {payload['worktree']} ({payload.get('worktree_branch', '')})")
    verification = payload.get("verification_report") or {}
    if verification:
        print(f"verification: {verification.get('status', 'unknown')}")


def _execute(agent: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return _result_payload(agent.run(payload))


def _restore_if_requested(agent: Any, args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.restore_on_failure or agent is None:
        return None
    try:
        return agent.restore_last_checkpoint()
    except Exception as exc:  # noqa: BLE001 - report restore failure with the task failure
        return {"failed": {"checkpoint": f"{type(exc).__name__}: {exc}"}}


def _write_result_file(path_text: str, payload: dict[str, Any], workspace: Path) -> Path:
    """Atomically persist one result inside the workspace; never follow it outside."""
    workspace = Path(workspace).resolve()
    target = Path(path_text).expanduser()
    if not target.is_absolute():
        target = workspace / target
    target = target.resolve()
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("结果文件必须位于工作区内") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _emit_result(
    payload: dict[str, Any],
    args: argparse.Namespace,
    workspace: Path,
    *,
    human_error: bool = False,
) -> bool:
    written = True
    if args.result_file:
        try:
            _write_result_file(args.result_file, payload, workspace)
        except (OSError, ValueError) as exc:
            print(f"result file not written: {exc}", file=sys.stderr)
            written = False
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    elif human_error:
        print(payload.get("error", "failed"), file=sys.stderr)
    else:
        _print_human(payload)
    return written


def _failure_payload(error: str, args: argparse.Namespace, session_id: str, agent: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "session_id": session_id,
        "error": error,
        **classify_message(error).as_dict(),
    }
    restore = _restore_if_requested(agent, args)
    if restore is not None:
        payload["checkpoint_restore"] = restore
    return payload


def _sessions_command(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"workspace is not a directory: {workspace}", file=sys.stderr)
        return 2
    limit = max(1, min(100, int(args.limit)))
    try:
        records = EventLedger(workspace).list_sessions(limit=limit)
    except Exception as exc:  # noqa: BLE001 - CLI boundary must report, not traceback
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    payload = [
        {
            "session_id": str(item.get("session_id") or ""),
            "status": str(item.get("status") or ""),
            "prompt": str(item.get("prompt") or ""),
            "updated_at": str(item.get("updated_at") or ""),
        }
        for item in records
    ]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for item in payload:
            prompt = " ".join(item["prompt"].split())[:80]
            print(f"{item['session_id']}\t{item['status']}\t{item['updated_at']}\t{prompt}")
    return 0


def _eval_command(args: argparse.Namespace) -> int:
    suite_path = Path(args.suite).expanduser().resolve()
    if not suite_path.is_file():
        print(f"suite not found: {suite_path}", file=sys.stderr)
        return 2
    output_path = Path(args.output).expanduser().resolve() if args.output else None
    try:
        runner = EvalRunner(Path.cwd(), suite_path, output_path=output_path, jobs=args.jobs)
        report = runner.run()
    except EvalError as exc:
        print(f"eval suite rejected: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(
            f"{report['suite']}: {report['passed']}/{report['total']} passed "
            f"({report['pass_rate']:.1%}), avg turns {report['average_turns']}, "
            f"tokens {report['total_tokens']}, {report['elapsed_seconds']:.1f}s"
        )
        if report.get("estimated_cost_usd"):
            print(f"estimated cost: ${report['estimated_cost_usd']:.4f}")
        if report.get("regression") is not None:
            print(f"regression vs previous: {report['regression']:+.1%}")
        print(f"report: {runner.output_path}")
        for item in report["results"]:
            if not item["success"]:
                print(f"FAIL {item['task_id']}: {item['error'] or item['check_output'][:200]}")
    return 0 if report["failed"] == 0 else 1


def _mcp_serve_command(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"workspace is not a directory: {workspace}", file=sys.stderr)
        return 2
    try:
        server = MCPServer(workspace)
    except (OSError, ValueError) as exc:
        print(f"cannot start MCP server: {exc}", file=sys.stderr)
        return 1
    serve_stdio(server, sys.stdin, sys.stdout)
    return 0


def _mcp_command(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"workspace is not a directory: {workspace}", file=sys.stderr)
        return 2
    manager = MCPManager(workspace)
    try:
        if args.read:
            server, uri = args.read
            try:
                value = manager.read_resource(server, uri)
            except MCPError as exc:
                print(f"mcp read failed: {exc}", file=sys.stderr)
                return 1
            payload: dict[str, object] = {"server": server, "uri": uri, "text": value}
        elif args.prompt:
            server, name = args.prompt
            arguments: dict[str, str] = {}
            for raw in args.arg:
                key, _, value = str(raw).partition("=")
                if key.strip():
                    arguments[key.strip()] = value
            try:
                value = manager.get_prompt(server, name, arguments)
            except MCPError as exc:
                print(f"mcp prompt failed: {exc}", file=sys.stderr)
                return 1
            payload = {"server": server, "prompt": name, "text": value}
        else:
            payload = {
                "tools": [
                    {"name": tool.public_name, "description": tool.description}
                    for tool in manager.start()
                ],
                "resources": manager.list_resources(),
                "prompts": manager.list_prompts(),
                "errors": list(manager.errors),
            }
    finally:
        manager.close()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if "text" in payload:
            print(str(payload["text"]))
        else:
            print(f"tools: {len(payload.get('tools') or [])}")
            print(f"resources: {len(payload.get('resources') or [])}")
            print(f"prompts: {len(payload.get('prompts') or [])}")
            for error in payload.get("errors") or []:
                print(f"error: {error}", file=sys.stderr)
    return 0


def _sandbox_check_command(args: argparse.Namespace) -> int:
    backend = (args.backend or os.getenv("SCIDEV_SANDBOX_BACKEND", "local")).strip().lower()
    image = (args.image or os.getenv("SCIDEV_SANDBOX_IMAGE", "")).strip()
    payload: dict[str, Any] = {
        "backend": backend,
        "ready": False,
        "os_sandbox": False,
        "image": image,
        "detail": "",
    }
    if backend in {"", "local", "none", "off"}:
        payload["backend"] = "local"
        payload["ready"] = True
        payload["detail"] = "本机模式：命令以当前用户权限运行，批准后不受文件/网络沙箱限制。"
    elif backend != "docker":
        payload["detail"] = f"不认识的沙箱后端：{backend}（支持 local/docker）"
    else:
        try:
            sandbox = DockerSandbox.from_environment(backend="docker", image=image)
            if sandbox is None:
                raise SandboxError("Docker 沙箱没有配置镜像")
            version = sandbox.ensure_available()
        except SandboxError as exc:
            payload["detail"] = str(exc)
        else:
            payload.update({
                "ready": True,
                "os_sandbox": True,
                "version": version,
                "image": sandbox.image,
                "network": sandbox.network,
                "detail": "Docker 容器执行：只挂载工作区，按配置限制网络和资源。",
            })
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"backend: {payload['backend']}")
        print(f"ready: {'yes' if payload['ready'] else 'no'}")
        print(f"os_sandbox: {'yes' if payload['os_sandbox'] else 'no'}")
        if payload.get("version"):
            print(f"server: {payload['version']}")
        print(str(payload["detail"]))
    return 0 if payload["ready"] else 1


def _update_command(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    repo = args.repo or detect_repo(workspace if workspace.is_dir() else None)
    if args.install and not args.download:
        print("--install requires --download", file=sys.stderr)
        return 2
    try:
        info = check_for_update(repo=repo)
    except UpdateError as exc:
        print(f"update check failed: {exc}", file=sys.stderr)
        return 1
    downloaded = None
    installer_pid = 0
    if args.download:
        if not info.available:
            print("no newer release; nothing was downloaded", file=sys.stderr)
            return 1
        try:
            downloaded = download_asset(
                info,
                Path(args.download).expanduser(),
                expected_sha256=args.sha256,
            )
            if args.install:
                installer_pid = launch_installer(downloaded.path, expected_sha256=downloaded.sha256)
        except UpdateError as exc:
            print(f"update download failed: {exc}", file=sys.stderr)
            return 1
    if args.json:
        payload = info.as_dict()
        if downloaded is not None:
            payload["downloaded"] = downloaded.as_dict()
        if installer_pid:
            payload["installer_pid"] = installer_pid
        print(json.dumps(payload, ensure_ascii=False))
    elif info.available:
        print(f"update available: {info.current} -> {info.latest}")
        print(info.url)
        if info.asset_url:
            print(info.asset_url)
        if downloaded is not None:
            print(f"downloaded and verified: {downloaded.path}")
            print(f"sha256: {downloaded.sha256}")
            if installer_pid:
                print(f"installer launched (pid {installer_pid}); follow its UI to install")
            else:
                print("not installed or executed")
    else:
        print(f"up to date: {info.current}")
    if args.fail_if_available and info.available:
        return 1
    return 0


def _pr_view_command(args: argparse.Namespace) -> int:
    try:
        payload = view_pull_request(
            Path(args.workspace), args.number, allow_network=args.allow_network,
        )
    except PullRequestError as exc:
        print(f"pr-view failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"#{payload.get('number')} {payload.get('title')} [{payload.get('state')}]")
        print(payload.get("url") or "")
    return 0


def _pr_diff_command(args: argparse.Namespace) -> int:
    try:
        payload = pull_request_diff(
            Path(args.workspace),
            args.number,
            allow_network=args.allow_network,
            max_chars=args.max_chars,
        )
    except (PullRequestError, ValueError) as exc:
        print(f"pr-diff failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(payload["diff"])
        if payload["truncated"]:
            print("[diff truncated]", file=sys.stderr)
    return 0


def _pr_review_command(args: argparse.Namespace) -> int:
    try:
        payload = review_pull_request(
            Path(args.workspace),
            args.number,
            args.action,
            body=args.body,
            allow_network=args.allow_network,
        )
    except PullRequestError as exc:
        print(f"pr-review failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"#{payload['number']} {payload['action']}: {payload['url']}")
    return 0


def _migrate_command(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"workspace is not a directory: {workspace}", file=sys.stderr)
        return 2
    report = migrate_project(workspace, dry_run=args.dry_run)
    payload = report.as_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"schema: {report.schema_version} (dry-run={report.dry_run})")
        print(f"changed: {len(report.changed)}")
        for path in report.changed:
            print(f"  {path}")
        print(f"backups: {len(report.backups)}")
        for error in report.errors:
            print(f"error: {error}", file=sys.stderr)
    return 0 if not report.errors else 1


def _pr_command(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"workspace is not a directory: {workspace}", file=sys.stderr)
        return 2
    try:
        plan = build_plan(
            workspace,
            base=args.base,
            title=args.title,
            body=args.body,
            draft=args.draft,
        )
    except PullRequestError as exc:
        print(f"pr rejected: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        payload = {
            "dry_run": True,
            "repo": plan.repo,
            "branch": plan.branch,
            "base": plan.base,
            "title": plan.title,
            "draft": plan.draft,
            "commands": [
                ["git", "push", "-u", "origin", plan.branch],
                ["gh", "pr", "create", "--repo", plan.repo, "--head", plan.branch, "--base", plan.base],
            ],
        }
    else:
        try:
            payload = create_pull_request(plan, allow_network=args.allow_network)
        except PullRequestError as exc:
            print(f"pr failed: {exc}", file=sys.stderr)
            return 1
        payload["dry_run"] = False
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    elif args.dry_run:
        print(f"{plan.repo}: {plan.branch} -> {plan.base} (dry run)")
        print(f"title: {plan.title}")
    else:
        print(payload.get("url") or "PR created")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "sessions":
        return _sessions_command(args)
    if args.command == "eval":
        return _eval_command(args)
    if args.command == "pr":
        return _pr_command(args)
    if args.command == "pr-view":
        return _pr_view_command(args)
    if args.command == "pr-diff":
        return _pr_diff_command(args)
    if args.command == "pr-review":
        return _pr_review_command(args)
    if args.command == "migrate":
        return _migrate_command(args)
    if args.command == "update":
        return _update_command(args)
    if args.command == "mcp":
        return _mcp_command(args)
    if args.command == "mcp-serve":
        return _mcp_serve_command(args)
    if args.command == "sandbox-check":
        return _sandbox_check_command(args)
    if args.command != "exec":
        parser.print_help()
        return 2
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"workspace is not a directory: {workspace}", file=sys.stderr)
        return 2
    _configure_provider_environment(args)
    if len(args.image) > 4:
        print("at most 4 images may be attached", file=sys.stderr)
        return 2
    images: list[dict[str, Any]] = []
    for raw_path in args.image:
        try:
            images.append(load_image_file(Path(raw_path)))
        except AttachmentError as exc:
            print(f"image rejected: {exc}", file=sys.stderr)
            return 2
    session_id = args.session_id.strip() or new_id("cli")
    workspace_effective = workspace
    worktree_meta: dict[str, Any] = {}
    if args.worktree:
        try:
            worktree = WorktreeManager(workspace).create(args.worktree_id or session_id)
        except WorktreeError as exc:
            print(f"worktree rejected: {exc}", file=sys.stderr)
            return 2
        workspace_effective = worktree.path
        worktree_meta = {
            "workspace": str(worktree.path),
            "worktree": str(worktree.path),
            "worktree_branch": worktree.branch,
            "worktree_base_sha": worktree.base_sha,
        }

    def with_worktree(data: dict[str, Any]) -> dict[str, Any]:
        data.update(worktree_meta)
        return data

    payload = {
        "payload": {
            "session_id": session_id,
            "prompt": args.task,
            "planning_mode": "off",
            "images": images,
        }
    }
    agent: CodingAgent | None = None
    try:
        settings = SummarySettings.load(workspace_effective)
        settings.execution_mode = ExecutionMode.parse(args.execution_mode).value
        if args.max_turns:
            settings.max_agent_turns = max(1, min(64, int(args.max_turns)))
        if args.max_tokens:
            settings.max_task_tokens = max(1, int(args.max_tokens))
        ledger = EventLedger(workspace_effective)
        git = GitManager(workspace_effective)
        patch_review = None
        if args.patch_approval == "auto":
            def approve_all_hunks(preview: dict) -> set[int]:
                """Headless runs have no hunk UI; apply the validated hunks."""
                selected: set[int] = set()
                for hunk in preview.get("hunks") or []:
                    if isinstance(hunk, dict):
                        try:
                            selected.add(int(hunk["id"]))
                        except (KeyError, TypeError, ValueError):
                            continue
                return selected
            patch_review = approve_all_hunks
        agent = CodingAgent(
            workspace_effective,
            ledger,
            git,
            summary_settings=settings,
            require_verification=not args.no_verify,
            command_approval=None,
            patch_review=patch_review,
            prefer_environment_provider=bool(args.api_base or args.api_key or args.model),
            sandbox_backend=args.sandbox,
            sandbox_image=args.sandbox_image,
        )
        result = with_worktree(_execute(agent, payload))
        if not result.get("session_id"):
            result["session_id"] = session_id
        if (
            result.get("success")
            and result.get("no_changes_detected")
            and not args.allow_no_changes
        ):
            result["success"] = False
            result["status"] = "no_changes"
            result["error_kind"] = "no_changes"
            result["user_message"] = "任务结束但没有修改文件；如只是问答/分析请加 --allow-no-changes。"
            result["retryable"] = False
            result["detail"] = "no_changes_detected"
    except PermanentError as exc:
        payload = with_worktree(_failure_payload(str(exc), args, session_id, agent))
        _emit_result(payload, args, workspace_effective, human_error=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary must report, not traceback
        payload = with_worktree(_failure_payload(f"{type(exc).__name__}: {exc}", args, session_id, agent))
        _emit_result(payload, args, workspace_effective, human_error=True)
        return 1
    except KeyboardInterrupt:
        payload = with_worktree(_failure_payload("cancelled", args, session_id, agent))
        _emit_result(payload, args, workspace_effective, human_error=True)
        return 130
    written = _emit_result(result, args, workspace_effective)
    return 0 if result.get("success") and written else 1


if __name__ == "__main__":
    raise SystemExit(main())
