"""Opt-in end-to-end coding test for an already-installed local Ollama model.

This script never pulls a model. It only accepts a loopback HTTP endpoint and
uses a disposable Git workspace, then verifies a real tool call, audit events,
small Python coding and bug-fix tasks, automatic commits, and final conversation summaries.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request
from unittest.mock import patch
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import scidev_core
from scidev_core import CodingAgent, EventLedger, GitManager, SummarySettings


DEFAULT_MODEL = "scidev-qwen2.5-coder-7b:q4_k_m"
MAX_TOKENS = 12_000
EXPECTED_FILE_CONTENT = 'print("Qwen2.5-Coder-7B works")\n'
EXPECTED_STDOUT = "Qwen2.5-Coder-7B works\n"
TASK_PROMPT = (
    "Create exactly one file named hello_qwen.py with exactly this one line and a final newline: "
    'print("Qwen2.5-Coder-7B works"). Use the declared write_file tool. '
    "Do not call run_command and do not create any other source file. Inspect the Git diff before finishing, "
    "then briefly report completion."
)
BUGGY_FIRST_DUPLICATE_SOURCE = (
    "def first_duplicate(values):\n"
    "    seen = []\n"
    "    for value in values:\n"
    "        if value in seen:\n"
    "            return value\n"
    "        seen = seen + [value]\n"
    "    return 0\n"
)
MAX_CODING_TASK_SOURCE_BYTES = 16_000


@dataclass(frozen=True)
class CodingTask:
    name: str
    filename: str
    function_name: str
    arguments: tuple[str, ...]
    specification: str
    examples: tuple[tuple[tuple[Any, ...], Any], ...]
    mode: str = "create"
    required_tool: str = "write_file"


CODING_TASKS = (
    CodingTask(
        name="parity",
        filename="is_even.py",
        function_name="is_even",
        arguments=("value",),
        specification="返回 value 是否为偶数；负数和 0 也按通常整数规则处理。",
        examples=(((0,), True), ((-4,), True), ((3,), False), ((-7,), False)),
    ),
    CodingTask(
        name="filter-and-accumulate",
        filename="sum_positive.py",
        function_name="sum_positive",
        arguments=("values",),
        specification="返回 values 中所有严格大于 0 的整数之和；忽略 0 和负数，空列表结果为 0。",
        examples=(
            (([2, -1, 5, 0],), 7),
            (([],), 0),
            (([-9, -3],), 0),
            (([4],), 4),
        ),
    ),
    CodingTask(
        name="boundary-conditions",
        filename="clamp_value.py",
        function_name="clamp",
        arguments=("value", "lower", "upper"),
        specification=(
            "假定 lower <= upper；value 低于范围时返回 lower，高于范围时返回 upper，"
            "在范围内（含边界）则原样返回 value。"
        ),
        examples=(
            ((-2, 0, 10), 0),
            ((0, 0, 10), 0),
            ((6, 0, 10), 6),
            ((10, 0, 10), 10),
            ((15, 0, 10), 10),
        ),
    ),
    CodingTask(
        name="bug-fix-first-duplicate",
        filename="first_duplicate.py",
        function_name="first_duplicate",
        arguments=("values",),
        specification=(
            "修复已有函数：从左到右扫描 values，返回第一个再次出现的值；如果没有重复值，必须返回 None。"
            "保留重复值为 0 时返回 0 的行为。"
        ),
        examples=(
            (([3, 1, 3, 2, 1],), 3),
            (([0, 1, 0],), 0),
            (([],), None),
            (([1, 2, 3],), None),
            (([1, 1, 2, 2],), 1),
        ),
        mode="fix",
        required_tool="replace_in_file",
    ),
)

SAFE_CODING_AST_NODES = frozenset(
    {
        ast.Module,
        ast.FunctionDef,
        ast.arguments,
        ast.arg,
        ast.Return,
        ast.If,
        ast.For,
        ast.Assign,
        ast.AugAssign,
        ast.Compare,
        ast.BinOp,
        ast.UnaryOp,
        ast.BoolOp,
        ast.IfExp,
        ast.List,
        ast.Tuple,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.Store,
        ast.Add,
        ast.Sub,
        ast.FloorDiv,
        ast.Mod,
        ast.USub,
        ast.UAdd,
        ast.Not,
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
        ast.In,
        ast.NotIn,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
    }
)


def validate_local_base_url(value: str) -> str:
    """Reject remote endpoints so this smoke test cannot spend cloud tokens."""
    candidate = str(value or "").strip()
    parsed = urlsplit(candidate)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "http"
        or hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("local model smoke requires an unauthenticated loopback http:// endpoint")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("local model endpoint has an invalid port") from exc
    if not parsed.netloc:
        raise ValueError("local model endpoint is missing a host")
    return candidate.rstrip("/")


def configure_local_environment(base_url: str, model: str, api_key: str) -> None:
    """Set provider and proxy-bypass settings in this smoke process only."""
    os.environ["SCIDEV_API_BASE"] = base_url
    os.environ["SCIDEV_API_KEY"] = api_key
    os.environ["SCIDEV_MODEL"] = model
    os.environ["SCIDEV_TEXT_TOOL_CALL_FALLBACK"] = "1"
    os.environ["SCIDEV_SUMMARY_ENABLED"] = "1"
    os.environ["SCIDEV_SUMMARY_INTERVAL_TURNS"] = "0"
    for variable in ("NO_PROXY", "no_proxy"):
        existing = [item.strip() for item in os.environ.get(variable, "").split(",") if item.strip()]
        present = {item.casefold() for item in existing}
        existing.extend(host for host in ("127.0.0.1", "localhost", "::1") if host.casefold() not in present)
        os.environ[variable] = ",".join(existing)


def _record_local_request(
    request: Request,
    *,
    model: str,
    requests: list[dict[str, Any]],
) -> None:
    """Record and validate wire metadata without exposing prompts or secrets."""
    payload = json.loads(request.data.decode("utf-8"))
    record = {
        "endpoint": request.full_url,
        "model": payload.get("model"),
        "max_tokens": payload.get("max_tokens"),
        "tools": bool(payload.get("tools")),
    }
    request_url = urlsplit(request.full_url)
    if request_url.scheme != "http" or (request_url.hostname or "").lower() not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise RuntimeError("provider request did not use the local loopback endpoint")
    if record["model"] != model:
        raise RuntimeError("provider request used a model other than the selected local model")
    if not isinstance(record["max_tokens"], int) or record["max_tokens"] < 10_000:
        raise RuntimeError("provider request max_tokens was below 10000")
    requests.append(record)


def _execute_verified_sample(target: Path, cwd: Path) -> str:
    """Run only the fixed smoke sample, never arbitrary model-generated code."""
    if not target.is_file() or target.read_text(encoding="utf-8") != EXPECTED_FILE_CONTENT:
        raise RuntimeError("the model did not create the exact requested Python file")

    execution = subprocess.run(
        [sys.executable, str(target)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if execution.returncode != 0 or execution.stdout != EXPECTED_STDOUT or execution.stderr:
        raise RuntimeError(
            "the verified Python sample did not run as expected: "
            f"returncode={execution.returncode}, stdout={execution.stdout!r}, stderr={execution.stderr!r}"
        )
    return execution.stdout


def _coding_task_prompt(task: CodingTask) -> str:
    signature = f"{task.function_name}({', '.join(task.arguments)})"
    if task.mode == "fix":
        intro = (
            f"已有文件 {task.filename} 中的函数有 bug。请读取该文件后，直接修改唯一的顶层函数；准确签名为 "
            f"def {signature}:\n"
        )
        tool_instruction = (
            "请按顺序立即实际调用工具：1) read_file 读取此文件；2) replace_in_file 将 old_text 精确设为 "
            "`    return 0\\n`，new_text 精确设为 `    return None\\n`。read_file 输出的行号只是显示标记，"
            "绝不是文件原文，old_text 不得包含行号。此请求已授权文件编辑：不要只展示代码、不要询问或等待确认、"
            "不要调用 write_file。修复后重复值 0 仍须返回 0。 "
        )
    else:
        intro = (
            f"Create exactly one file named {task.filename} defining exactly one top-level function "
            f"with this exact signature: def {signature}:\n"
        )
        tool_instruction = (
            f"此请求已授权创建 {task.filename}。现在立即调用 write_file，写入完整实现；不要只回复计划、不要询问确认，"
            "不要创建其他文件。随后实际调用 git_diff 检查改动。 "
        )
    return (
        intro
        + f"Task: {task.specification}\n"
        + "Use only arithmetic, comparisons, membership tests, if/for, assignment and return. For this "
        "safety-checked exercise, do not use imports, function calls, attributes, comprehensions, decorators, "
        "type hints, docstrings, or top-level code. "
        + tool_instruction
        + "Do not call run_command. Inspect the Git diff before finishing, then briefly report completion."
    )


def _verify_coding_task(target: Path, task: CodingTask) -> dict[str, Any]:
    """AST-restrict and execute a tiny generated function against deterministic examples."""
    if not target.is_file():
        raise RuntimeError(f"the model did not create {task.filename}")
    raw = target.read_bytes()
    if len(raw) > MAX_CODING_TASK_SOURCE_BYTES or b"\x00" in raw:
        raise RuntimeError(f"generated coding task is too large or contains NUL bytes: {task.filename}")
    try:
        source = raw.decode("utf-8")
        tree = ast.parse(source, filename=task.filename, mode="exec")
    except (UnicodeDecodeError, SyntaxError, ValueError, RecursionError) as exc:
        raise RuntimeError(f"generated coding task is not valid UTF-8 Python: {task.filename}") from exc

    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise RuntimeError("generated coding task must contain exactly one top-level function")
    function = tree.body[0]
    actual_arguments = tuple(argument.arg for argument in function.args.args)
    if (
        function.name != task.function_name
        or actual_arguments != task.arguments
        or function.decorator_list
        or function.returns is not None
        or any(argument.annotation is not None or argument.type_comment for argument in function.args.args)
        or function.args.posonlyargs
        or function.args.vararg is not None
        or function.args.kwonlyargs
        or function.args.kwarg is not None
        or function.args.defaults
        or function.args.kw_defaults
        or sum(isinstance(node, ast.FunctionDef) for node in ast.walk(tree)) != 1
    ):
        raise RuntimeError(f"generated coding task does not match the required signature: {task.function_name}")

    unsupported = sorted(
        {type(node).__name__ for node in ast.walk(tree) if type(node) not in SAFE_CODING_AST_NODES}
    )
    if unsupported:
        raise RuntimeError(f"generated coding task uses unsupported/unsafe syntax: {', '.join(unsupported)}")
    loops = [node for node in ast.walk(function) if isinstance(node, ast.For)]
    if len(loops) > 1 or any(
        not isinstance(loop.iter, ast.Name) or loop.iter.id not in task.arguments
        for loop in loops
    ):
        raise RuntimeError("generated coding task loops must iterate directly over a provided argument")

    namespace: dict[str, Any] = {"__builtins__": {}}
    try:
        exec(compile(tree, str(target), "exec"), namespace, namespace)
        generated = namespace[task.function_name]
        for arguments, expected in task.examples:
            actual = generated(*arguments)
            if type(actual) is not type(expected) or actual != expected:
                raise RuntimeError(
                    f"{task.name} failed for {arguments!r}: expected {expected!r}, got {actual!r}"
                )
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - report the generated function's failing case.
        raise RuntimeError(f"{task.name} raised {type(exc).__name__}: {exc}") from exc

    return {"status": "passed", "function": task.function_name, "examples_passed": len(task.examples)}


def run_smoke(base_url: str, model: str, api_key: str) -> dict[str, Any]:
    base_url = validate_local_base_url(base_url)
    if not model.strip():
        raise ValueError("model name cannot be empty")
    configure_local_environment(base_url, model.strip(), api_key)

    wire_requests: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    real_urlopen = scidev_core.urlopen

    def observe_request(request: Request, timeout: float = 90.0):
        _record_local_request(request, model=model.strip(), requests=wire_requests)
        return real_urlopen(request, timeout=timeout)

    with tempfile.TemporaryDirectory(prefix="scidev-live-ollama-") as temporary:
        root = Path(temporary)
        (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
        bug_fix_task = next(task for task in CODING_TASKS if task.mode == "fix")
        (root / bug_fix_task.filename).write_text(BUGGY_FIRST_DUPLICATE_SOURCE, encoding="utf-8")
        try:
            _verify_coding_task(root / bug_fix_task.filename, bug_fix_task)
        except RuntimeError as exc:
            if "failed for" not in str(exc):
                raise RuntimeError(f"bug-fix fixture failed for an unexpected reason: {exc}") from exc
        else:
            raise RuntimeError("bug-fix fixture unexpectedly passes before Qwen edits it")
        ledger = EventLedger(root)
        git = GitManager(root)
        git.commit_changes("local model smoke baseline")
        summary_settings = SummarySettings(
            enabled=True,
            model="",
            max_tokens=MAX_TOKENS,
            context_chars=18_000,
            retries=0,
            interval_turns=0,
        )
        agent = CodingAgent(
            root,
            ledger,
            git,
            event_callback=lambda name, data: events.append((name, data)),
            summary_settings=summary_settings,
        )
        task = {"payload": {"session_id": "live_local_model_smoke", "prompt": TASK_PROMPT}}
        with patch("scidev_core.urlopen", new=observe_request):
            result = agent.run(task)

        target = root / "hello_qwen.py"
        generated_stdout = _execute_verified_sample(target, root)

        model_tools = [
            str(data.get("name"))
            for event_name, data in events
            if event_name == "tool_started" and data.get("source") != "harness_precommit"
        ]
        if "write_file" not in model_tools:
            raise RuntimeError(f"the model did not call write_file: {model_tools}")
        if "run_command" in model_tools:
            raise RuntimeError("the model unexpectedly requested a shell command")

        ledger_events = [
            json.loads(line)
            for line in (root / ".research" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        event_types = [event["event_type"] for event in ledger_events]
        precommit_diff_verified = any(
            event["event_type"] == "tool_result"
            and event["payload"].get("name") == "git_diff"
            and event["payload"].get("source") == "harness_precommit"
            for event in ledger_events
        )
        if not precommit_diff_verified and "git_diff" not in model_tools:
            raise RuntimeError("neither the model nor the Harness performed a Git diff inspection")
        for required_event in ("git_commit_created", "coding_session_completed", "conversation_summary_created"):
            if required_event not in event_types:
                raise RuntimeError(f"event ledger is missing {required_event}")
        if not result.get("summary") or not result.get("summary_path"):
            raise RuntimeError("automatic final conversation summary was not produced")

        committed_paths = subprocess.run(
            ["git", "show", "--format=", "--name-only", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        if committed_paths != ["hello_qwen.py"]:
            raise RuntimeError(f"unexpected paths in the model-created commit: {committed_paths}")
        commit_sha = str(result.get("git_result_sha") or "")
        if len(commit_sha) != 40:
            raise RuntimeError("coding run did not return a full Git commit SHA")
        if git.status_paths():
            raise RuntimeError(f"temporary workspace was not clean after auto-commit: {sorted(git.status_paths())}")
        if len(wire_requests) < 3:
            raise RuntimeError(f"too few LLM requests reached Ollama: {len(wire_requests)}")

        baseline_result = {
            "created_file": target.name,
            "model_tool_calls": model_tools,
            "precommit_diff_verified": precommit_diff_verified,
            "commit": commit_sha,
            "summary_characters": len(str(result["summary"])),
        }
        coding_task_results: list[dict[str, Any]] = []
        for coding_task in CODING_TASKS:
            task_events: list[tuple[str, dict[str, Any]]] = []
            request_start = len(wire_requests)
            ledger_path = root / ".research" / "events.jsonl"
            previous_ledger_lines = ledger_path.read_text(encoding="utf-8").splitlines()
            task_agent = CodingAgent(
                root,
                ledger,
                git,
                event_callback=lambda name, data: task_events.append((name, data)),
                summary_settings=summary_settings,
            )
            task_result = {
                "payload": {
                    "session_id": f"live_local_model_{coding_task.name}",
                    "prompt": _coding_task_prompt(coding_task),
                }
            }
            with patch("scidev_core.urlopen", new=observe_request):
                task_output = task_agent.run(task_result)

            target = root / coding_task.filename
            if not target.is_file():
                requested_tools = [
                    str(data.get("name"))
                    for event_name, data in task_events
                    if event_name == "tool_started" and data.get("source") != "harness_precommit"
                ]
                assistant_tail = [
                    str(data.get("text") or "")[-500:]
                    for event_name, data in task_events
                    if event_name == "assistant" and data.get("text")
                ]
                raise RuntimeError(
                    f"the model did not create {coding_task.filename}; "
                    f"tool_calls={requested_tools}, assistant_tail={assistant_tail[-2:]}"
                )
            model_tools = [
                str(data.get("name"))
                for event_name, data in task_events
                if event_name == "tool_started" and data.get("source") != "harness_precommit"
            ]
            try:
                code_check = _verify_coding_task(target, coding_task)
            except RuntimeError as exc:
                tool_calls = [
                    {
                        "name": str(data.get("name")),
                        "arguments": data.get("arguments", {}),
                    }
                    for event_name, data in task_events
                    if event_name == "tool_started" and data.get("source") != "harness_precommit"
                ]
                tool_results = [
                    {
                        "name": str(data.get("name")),
                        "result": str(data.get("result") or "")[:600],
                    }
                    for event_name, data in task_events
                    if event_name == "tool_result" and data.get("source") != "harness_precommit"
                ]
                assistant_tail = [
                    str(data.get("text") or "")[-400:]
                    for event_name, data in task_events
                    if event_name == "assistant" and data.get("text")
                ]
                raise RuntimeError(
                    f"{exc}; final_source={target.read_text(encoding='utf-8')!r}; "
                    f"tool_calls={tool_calls}; tool_results={tool_results}; "
                    f"assistant_tail={assistant_tail[-2:]}"
                ) from exc
            if coding_task.required_tool not in model_tools:
                raise RuntimeError(
                    f"Qwen did not use {coding_task.required_tool} for {coding_task.name}: {model_tools}"
                )
            if coding_task.mode == "fix" and "read_file" not in model_tools:
                raise RuntimeError(f"Qwen did not inspect the existing {coding_task.filename} before editing")
            if "run_command" in model_tools:
                raise RuntimeError(f"Qwen unexpectedly requested shell execution for {coding_task.name}")

            task_ledger_events = [
                json.loads(line)
                for line in ledger_path.read_text(encoding="utf-8").splitlines()[len(previous_ledger_lines):]
            ]
            task_event_types = [event["event_type"] for event in task_ledger_events]
            for required_event in (
                "git_commit_created",
                "coding_session_completed",
                "conversation_summary_created",
            ):
                if required_event not in task_event_types:
                    raise RuntimeError(f"{coding_task.name} ledger is missing {required_event}")
            precommit_diff_verified = any(
                event_name == "tool_result"
                and data.get("name") == "git_diff"
                and data.get("source") == "harness_precommit"
                for event_name, data in task_events
            )
            if not precommit_diff_verified and "git_diff" not in model_tools:
                raise RuntimeError(f"neither Qwen nor the Harness checked the {coding_task.name} Git diff")
            if not task_output.get("summary") or not task_output.get("summary_path"):
                raise RuntimeError(f"{coding_task.name} did not produce a final conversation summary")

            committed_paths = subprocess.run(
                ["git", "show", "--format=", "--name-only", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            if committed_paths != [coding_task.filename]:
                raise RuntimeError(
                    f"{coding_task.name} commit contains unexpected paths: {committed_paths}"
                )
            commit_sha = str(task_output.get("git_result_sha") or "")
            if len(commit_sha) != 40:
                raise RuntimeError(f"{coding_task.name} did not return a full Git commit SHA")
            if git.status_paths():
                raise RuntimeError(
                    f"temporary workspace became dirty after {coding_task.name}: {sorted(git.status_paths())}"
                )
            task_requests = wire_requests[request_start:]
            if len(task_requests) < 2:
                raise RuntimeError(f"too few Ollama requests reached the {coding_task.name} task")
            coding_task_results.append(
                {
                    "task": coding_task.name,
                    "mode": coding_task.mode,
                    "file": coding_task.filename,
                    **code_check,
                    "commit": commit_sha,
                    "model_tool_calls": model_tools,
                    "precommit_diff_verified": precommit_diff_verified,
                    "summary_characters": len(str(task_output["summary"])),
                    "requests": task_requests,
                }
            )

        return {
            "status": "passed",
            "endpoint": base_url,
            "model": model.strip(),
            **baseline_result,
            "generated_script_execution": "passed",
            "generated_script_stdout": generated_stdout.rstrip("\n"),
            "coding_tasks_passed": len(coding_task_results),
            "coding_tasks": coding_task_results,
            "requests": wire_requests,
            "temporary_workspace_clean": True,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("SCIDEV_API_BASE", "http://127.0.0.1:11434/v1"),
        help="loopback Ollama OpenAI-compatible API base URL (no remote hosts)",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("SCIDEV_MODEL", DEFAULT_MODEL),
        help="an already-installed Ollama model tag; this script never pulls it",
    )
    parser.add_argument("--api-key", default=os.getenv("SCIDEV_API_KEY") or "ollama", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        result = run_smoke(args.base_url, args.model, args.api_key)
    except Exception as exc:  # noqa: BLE001 - report one actionable opt-in smoke-test failure.
        print(f"Local Ollama smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
