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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scidev",
        description="Run a SciDevHarness coding task without the desktop UI.",
    )
    parser.add_argument("--version", action="version", version="SciDevHarness 0.1.0")
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
        "--execution-mode",
        choices=[mode.value for mode in ExecutionMode],
        default=ExecutionMode.WORKSPACE_WRITE.value,
        help="command policy for this run (default: workspace-write)",
    )
    exec_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="do not require a passing current-code check before completion",
    )
    exec_parser.add_argument(
        "--restore-on-failure",
        action="store_true",
        help="restore files changed by the task when it fails",
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
    eval_parser.add_argument("--json", action="store_true", help="print the report as JSON")
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


def _failure_payload(error: str, args: argparse.Namespace, session_id: str, agent: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "session_id": session_id,
        "error": error,
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
        runner = EvalRunner(Path.cwd(), suite_path, output_path=output_path)
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
        if report.get("regression") is not None:
            print(f"regression vs previous: {report['regression']:+.1%}")
        print(f"report: {runner.output_path}")
        for item in report["results"]:
            if not item["success"]:
                print(f"FAIL {item['task_id']}: {item['error'] or item['check_output'][:200]}")
    return 0 if report["failed"] == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "sessions":
        return _sessions_command(args)
    if args.command == "eval":
        return _eval_command(args)
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
        settings = SummarySettings.load(workspace)
        settings.execution_mode = ExecutionMode.parse(args.execution_mode).value
        ledger = EventLedger(workspace)
        git = GitManager(workspace)
        agent = CodingAgent(
            workspace,
            ledger,
            git,
            summary_settings=settings,
            require_verification=not args.no_verify,
            command_approval=None,
            prefer_environment_provider=bool(args.api_base or args.api_key or args.model),
        )
        result = _execute(agent, payload)
        if not result.get("session_id"):
            result["session_id"] = session_id
    except PermanentError as exc:
        payload = _failure_payload(str(exc), args, session_id, agent)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(payload["error"], file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary must report, not traceback
        payload = _failure_payload(f"{type(exc).__name__}: {exc}", args, session_id, agent)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(payload["error"], file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        payload = _failure_payload("cancelled", args, session_id, agent)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print("cancelled", file=sys.stderr)
        return 130
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        _print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
