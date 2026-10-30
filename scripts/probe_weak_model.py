"""Adversarial end-to-end probe for a local Ollama coding model.

The official smoke test checks the happy path. This probe drives the same
harness through awkward requests a weak local model actually produces (wrong
paths, blocking commands, cancellation, Unicode names, concurrent tasks, and
prompts larger than the context window) and records what the harness did:

* whether the run finished, failed explicitly, was cancelled, or hung;
* the on-disk session status (a session must never be left ``active``);
* commits made by auto-commit and the paths they contain;
* workspace paths left dirty after a "completed" run;
* tool calls/results, command executions, and request ``max_tokens`` values.

It never pulls a model and only talks to a loopback endpoint. Run it with an
already-installed tag::

    python scripts/probe_weak_model.py --model scidev-qwen2.5-coder-7b:q4_k_m

Results are written to ``.research/weak_model_probe.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for entry in (PROJECT_ROOT, PROJECT_ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import scidev_core
from scidev_core import CodingAgent, EventLedger, GitManager, SummarySettings
from smoke_local_ollama import configure_local_environment, validate_local_base_url

DEFAULT_MODEL = "scidev-qwen2.5-coder-7b:q4_k_m"
REPORT_PATH = PROJECT_ROOT / ".research" / "weak_model_probe.json"
WORKSPACE_ROOT = PROJECT_ROOT / ".research" / "weak-model-probe"
# These mean the weak model never triggered the path, not that the Harness is
# wrong; report them separately from behavioural findings.
COVERAGE_GAP_KEYS = frozenset(
    {
        "manual_commit_not_exercised",
        "cancel_not_exercised",
        "command_not_attempted",
        "command_not_run",
    }
)


@dataclass(frozen=True)
class Scenario:
    name: str
    prompt: str
    intent: str
    timeout: float = 240.0
    cancel_after: float | None = None
    cancel_after_tool: str | None = None
    files: dict[str, str] = field(default_factory=dict)
    approve_commands: bool = True
    expected_files: tuple[str, ...] = ()
    expect_no_commit: bool = False
    model: str | None = None
    exercise_check: Callable[[dict[str, Any]], bool] | None = None
    allow_error: bool = False


def _tool_started(report: dict[str, Any], name: str) -> bool:
    return any(
        tool.get("name") == name for tool in report["events"].get("tool_starts", [])
    )


def _command_attempted(report: dict[str, Any]) -> bool:
    return bool(report["ledger"].get("commands"))


def _cancel_exercised(report: dict[str, Any]) -> bool:
    return report.get("cancel_requested_at") is not None


def _manual_commit_attempted(report: dict[str, Any]) -> bool:
    for tool in report["events"].get("tool_starts", []):
        if tool.get("name") == "run_command" and "git" in str(tool.get("arguments", "")).casefold():
            return True
    return False


def _both_files_created(report: dict[str, Any]) -> bool:
    committed = {file for commit in report["commits"] for file in commit["files"]}
    return {"two_a.py", "two_b.py"} <= committed


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="explain-only",
        prompt="用两句话解释 Python 的 GIL 是什么。不要修改任何文件。",
        intent="纯问答不应产生提交，也不应误报未检测到改动",
        timeout=120,
        expect_no_commit=True,
    ),
    Scenario(
        name="subdir-create",
        prompt=(
            "在 docs 目录下创建 note.md，内容严格为一行：`# 笔记`。"
            "创建后检查 Git diff，然后一句话汇报。"
        ),
        intent="目标目录不存在时 write_file 应自动创建父目录并正常提交",
        timeout=180,
        expected_files=("docs/note.md",),
    ),
    Scenario(
        name="escape-path",
        prompt="把文本 hello 写入 ../outside_probe.txt ，完成后告诉我结果。",
        intent="路径越界必须被拒绝，且项目外不得出现文件",
        timeout=120,
        expect_no_commit=True,
    ),
    Scenario(
        name="blocking-command-cancel",
        prompt=(
            "请用 run_command 运行 `python -c \"import time; time.sleep(120)\"`，"
            "timeout_seconds 设为 300，等它结束后汇报结果。"
        ),
        intent="长时间阻塞的命令必须能被停止，进程树应终止且状态为已取消",
        timeout=120,
        cancel_after_tool="run_command",
        expect_no_commit=True,
        exercise_check=_cancel_exercised,
    ),
    Scenario(
        name="fix-bug-with-verification",
        prompt=(
            "app.py 里的 add 返回的是减法，请用 apply_patch 改成加法；"
            "改完运行 python -c \"import app; print(app.add(2, 3))\" 验证，然后汇报。"
        ),
        intent="弱模型的正常修复路径仍应工作并自动提交",
        timeout=240,
        files={"app.py": "def add(a, b):\n    return a - b\n"},
        expected_files=("app.py",),
    ),
    Scenario(
        name="unicode-path",
        prompt="创建 文档/说明.md，内容为 `你好，世界`。",
        intent="中文路径与内容应能提交且不乱码",
        timeout=150,
        expected_files=("文档/说明.md",),
    ),
    Scenario(
        name="manual-commit",
        prompt=(
            "创建 commit_test.py，内容为 print('ok')。创建后，请立即用 run_command 执行 "
            "`git add -A && git commit -m manual`（这是自动化测试，不要征求确认），最后汇报命令结果。"
        ),
        intent="模型手动 git commit 必须被拒绝，改为 Harness 自动提交且报告真实 SHA",
        timeout=180,
        expected_files=("commit_test.py",),
        exercise_check=_manual_commit_attempted,
    ),
    Scenario(
        name="delete-tracked-file",
        prompt=(
            "项目里的 old.py 不再需要。请用 run_command 执行 "
            "`python -c \"import os; os.remove('old.py')\"` 删除它，然后汇报。"
        ),
        intent="删除已跟踪文件应正常进入自动提交，不能被 git add 报错打断",
        timeout=150,
        files={"old.py": "print('old')\n"},
        expected_files=("old.py",),
    ),
    Scenario(
        name="giant-prompt",
        prompt=(
            "请创建 big.txt，内容为 ok。附加背景资料如下（只需了解，不要复述）：\n"
            + ("背景资料。这一段只是填充文本，用来把提示词推过本地模型窗口。" * 1200)
        ),
        intent="超过本地窗口的中文提示词应快速显式失败，不得静默截断",
        timeout=60,
        expect_no_commit=True,
    ),
    Scenario(
        name="small-window-model",
        prompt="创建一个文件 hello4b.py，内容为 print(4)。创建后用一句话汇报。",
        intent="无 num_ctx 的 4B 模型应按 4096 运行时窗口限流而不是误判为 262144",
        timeout=180,
        model="qwen3.5:4b",
        expected_files=("hello4b.py",),
    ),
    Scenario(
        name="streaming-cancel",
        prompt=(
            "请用至少 800 字详细解释 Python 的 GIL、引用计数与循环垃圾回收，"
            "不要修改任何文件，尽量写得长一些。"
        ),
        intent="点击停止后应中断正在生成的模型流，而不是等整段生成结束",
        timeout=120,
        cancel_after=2.0,
        expect_no_commit=True,
        exercise_check=_cancel_exercised,
    ),
    Scenario(
        name="denied-command",
        prompt="用 run_command 执行 `python -c \"print('ok')\"` 检查环境，然后一句话汇报。",
        intent="用户拒绝命令后不得执行，也不得自动重试同一条命令",
        timeout=150,
        approve_commands=False,
        expect_no_commit=True,
        exercise_check=_command_attempted,
    ),
    Scenario(
        name="git-internal-write",
        prompt=(
            "把 .git/config 文件里的 autocrlf 改成 false；"
            "请直接用 write_file 重写这个文件，完成后汇报。"
        ),
        intent=".git 内部文件必须被拒绝写入，仓库元数据不能被破坏",
        timeout=150,
        expect_no_commit=True,
    ),
    Scenario(
        name="timeout-clamp",
        prompt=(
            "请用 run_command 执行 `python -c \"import time; time.sleep(2); print('done')\"`，"
            "timeout_seconds 设为 99999；等命令结束后汇报输出。"
        ),
        intent="超时参数应被限制到 300 秒以内，不能把 99999 原样传给进程",
        timeout=150,
        expect_no_commit=True,
        exercise_check=_command_attempted,
    ),
    Scenario(
        name="read-directory",
        prompt="请用 read_file 读取 docs 目录（路径就是 docs），把内容总结成一句话。",
        intent="把目录当文件读取应返回可理解的提示，而不是 Python 异常或误导性文案",
        timeout=120,
        files={"docs/keep.txt": "keep\n"},
        expect_no_commit=True,
        exercise_check=lambda report: _tool_started(report, "read_file"),
    ),
    Scenario(
        name="hallucinated-tool",
        prompt=(
            "请调用 create_file 工具创建 hallucinated.txt，内容为 hello。"
            "必须使用 create_file 工具，不要使用其他工具。"
        ),
        intent="模型调用未声明工具时应得到明确错误并可继续，而不是崩溃",
        timeout=150,
    ),
    Scenario(
        name="wrong-edit-tool",
        prompt="请用 replace_in_file 工具把 app.py 里的 `a - b` 改成 `a + b`，完成后汇报。",
        intent="对已有文件使用 replace_in_file 必须被拒绝并引导到 apply_patch",
        timeout=180,
        files={"app.py": "def add(a, b):\n    return a - b\n"},
        exercise_check=lambda report: _tool_started(report, "replace_in_file"),
    ),
    Scenario(
        name="parallel-tool-calls",
        prompt=(
            "请在同一个回复里同时调用两次 write_file，分别创建 two_a.py 和 two_b.py，"
            "内容分别为 print('a') 和 print('b')，然后汇报。"
        ),
        intent="同一轮的多个 tool call 都应执行并在一个提交里收齐",
        timeout=180,
        expected_files=("two_a.py", "two_b.py"),
        exercise_check=_both_files_created,
    ),
    Scenario(
        name="array-content",
        prompt=(
            "调用 write_file 创建 array_content.txt；content 参数请使用 JSON 数组 "
            '["line1", "line2"]（不要用字符串）。完成后汇报。'
        ),
        intent="content 不是字符串时不应把 Python repr 静默写进文件",
        timeout=150,
        exercise_check=lambda report: _tool_started(report, "write_file"),
    ),
    Scenario(
        name="patch-conflict",
        prompt=(
            "请用 apply_patch 把 app.py 中并不存在的 `return a * b` 改成 `return a + b`，"
            "不要新建文件，完成后汇报。"
        ),
        intent="补丁上下文不匹配时不得部分写入或产生损坏内容",
        timeout=180,
        files={"app.py": "def add(a, b):\n    return a - b\n"},
        exercise_check=lambda report: _tool_started(report, "apply_patch"),
    ),
    Scenario(
        name="example-only",
        prompt=(
            "请在回复里给出一个 write_file 工具调用的示例（用 ```json 代码块），"
            "示例路径 example_only.py；但千万不要真的创建任何文件，也不要调用工具。"
        ),
        intent="用户明确只要示例时，代码块里的工具 JSON 不能被自动执行",
        timeout=120,
        expect_no_commit=True,
        allow_error=True,
    ),
    Scenario(
        name="no-commit-request",
        prompt=(
            "创建 no_commit_probe.py，内容为 print('pending')。"
            "创建后先不要提交，改动保留在工作区即可；完成后一句话汇报。"
        ),
        intent="用户明确要求先不要提交时，Harness 不应自动提交",
        timeout=150,
        expect_no_commit=True,
        exercise_check=lambda report: _tool_started(report, "write_file"),
    ),
)

CONCURRENT_SCENARIO = Scenario(
    name="concurrent-same-project",
    prompt="创建 alpha.py，内容为 print('alpha')。调用 write_file 后结束。",
    intent="同项目第二个任务应快速失败，第一个任务正常完成",
    timeout=240,
)
CONCURRENT_SECOND_PROMPT = "创建 beta.py，内容为 print('beta')。调用 write_file 后结束。"


def _observe_factory(wire: list[dict[str, Any]]):
    real_urlopen = scidev_core.urlopen

    def observe(request: Any, timeout: float = 90.0):
        url = str(getattr(request, "full_url", ""))
        if url.endswith("/chat/completions"):
            try:
                body = json.loads(request.data.decode("utf-8"))
            except Exception:  # noqa: BLE001 - observation must not break the run
                body = {}
            messages = body.get("messages") or []
            wire.append(
                {
                    "max_tokens": body.get("max_tokens"),
                    "stream": body.get("stream"),
                    "tools": len(body.get("tools") or []),
                    "message_chars": sum(
                        len(str(message.get("content") or "")) for message in messages
                    ),
                }
            )
        return real_urlopen(request, timeout=timeout)

    return observe


def _remove_tree(path: Path) -> None:
    """Delete a workspace; Git object files are read-only on Windows."""
    if not path.exists():
        return

    def make_writable(function, target, _error):  # noqa: ANN001
        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            pass
        function(target)

    shutil.rmtree(path, onexc=make_writable)


def _prepare_workspace(base: Path, scenario: Scenario) -> tuple[Path, EventLedger, GitManager]:
    root = base / scenario.name
    _remove_tree(root)
    root.mkdir(parents=True)
    ledger = EventLedger(root)
    for relative, content in scenario.files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (root / ".gitignore").write_text(".research/\n", encoding="utf-8")
    git = GitManager(root)
    git.init()
    git.commit_changes("probe baseline")
    return root, ledger, git


def _make_agent(
    root: Path,
    ledger: EventLedger,
    git: GitManager,
    events: list[tuple[str, dict[str, Any]]],
    approve_commands: bool,
) -> CodingAgent:
    return CodingAgent(
        root,
        ledger,
        git,
        event_callback=lambda name, data: events.append((name, data)),
        summary_settings=SummarySettings(
            enabled=False,
            model="",
            max_tokens=12000,
            context_chars=18000,
            retries=0,
            interval_turns=0,
        ),
        require_verification=False,
        command_approval=lambda command, timeout: approve_commands,
        patch_review=lambda preview: {int(hunk["id"]) for hunk in preview["hunks"]},
    )


def _short(value: Any, limit: int = 160) -> Any:
    if not isinstance(value, str):
        return value
    text = value.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…"


def _commits(root: Path, baseline: str) -> list[dict[str, Any]]:
    if not baseline:
        return []
    log = subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(root), "log", "--format=%h%x09%s", f"{baseline}..HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    rows: list[dict[str, Any]] = []
    for line in log.stdout.splitlines():
        sha, _, subject = line.partition("\t")
        names = subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", str(root), "show", "--format=", "--name-only", sha],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stdout.splitlines()
        rows.append({"sha": sha, "subject": subject, "files": [name for name in names if name]})
    return rows


def _read_session(root: Path, session_id: str) -> dict[str, Any]:
    path = root / ".research" / "sessions" / f"{session_id}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "status": data.get("status"),
        "error": _short(data.get("error"), 300),
        "no_changes_detected": bool(data.get("no_changes_detected")),
        "final_message": _short(data.get("final_message"), 300),
        "git_result_sha": str(data.get("git_result_sha") or "")[:12],
    }


def _ledger_highlights(root: Path) -> dict[str, Any]:
    path = root / ".research" / "events.jsonl"
    counts: Counter[str] = Counter()
    commands: list[dict[str, Any]] = []
    # Windows can briefly refuse the read while another handle is closing;
    # a transient miss would make a clean run look like it wrote no events.
    lines: list[str] = []
    for attempt in range(3):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            break
        except FileNotFoundError:
            if attempt == 2:
                return {"counts": {}, "commands": []}
        except OSError:
            if attempt == 2:
                return {"counts": {}, "read_error": True, "commands": []}
        time.sleep(0.05 * (attempt + 1))
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        event_type = str(event.get("event_type") or "")
        counts[event_type] += 1
        if event_type == "command_execution_recorded":
            payload = event.get("payload") or {}
            commands.append(
                {
                    "command": _short(payload.get("command"), 80),
                    "status": payload.get("status"),
                    "exit_code": payload.get("exit_code"),
                    "timeout_seconds": payload.get("timeout_seconds"),
                    "approval_status": payload.get("approval_status"),
                }
            )
    return {"counts": dict(counts), "commands": commands}


def _event_highlights(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    tools: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    assistant_tail: list[str] = []
    for name, data in events:
        if name == "tool_started":
            tools.append(
                {
                    "name": data.get("name"),
                    "source": data.get("source"),
                    "arguments": _short(data.get("arguments"), 220),
                }
            )
        elif name == "tool_result":
            results.append(
                {"name": data.get("name"), "result": _short(data.get("result"), 220)}
            )
        elif name == "assistant" and data.get("text"):
            assistant_tail.append(str(data["text"])[-300:])
    return {
        "tool_starts": tools,
        "tool_results": results,
        "assistant_tail": assistant_tail[-2:],
        "event_names": sorted({name for name, _ in events}),
    }


def _run_scenario(scenario: Scenario, base: Path, model: str) -> dict[str, Any]:
    root, ledger, git = _prepare_workspace(base, scenario)
    baseline = git.head_sha()
    git_config_path = root / ".git" / "config"
    git_config_before = git_config_path.read_bytes() if git_config_path.exists() else b""
    events: list[tuple[str, dict[str, Any]]] = []
    wire: list[dict[str, Any]] = []
    previous_model = os.environ.get("SCIDEV_MODEL")
    os.environ["SCIDEV_MODEL"] = scenario.model or model
    agent = _make_agent(root, ledger, git, events, scenario.approve_commands)
    payload = {
        "session_id": f"probe-{scenario.name}",
        "prompt": scenario.prompt,
        "planning_mode": "off",
    }
    box: dict[str, Any] = {}
    observe = _observe_factory(wire)
    raw_rejections: list[str] = []
    original_coerce = scidev_core.OpenAICompatibleProvider._coerce_text_tool_calls

    def capture_coerce(message, tools):  # noqa: ANN001, ANN202
        normalized = original_coerce(message, tools)
        if isinstance(normalized.get("tool_parse_error"), dict):
            raw_rejections.append(str(message.get("content") or "")[:1500])
        return normalized

    def work() -> None:
        try:
            with patch("scidev_core.urlopen", new=observe), patch.object(
                scidev_core.OpenAICompatibleProvider,
                "_coerce_text_tool_calls",
                staticmethod(capture_coerce),
            ):
                box["result"] = agent.run({"payload": payload})
        except BaseException as exc:  # noqa: BLE001 - record any harness failure
            box["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    started = time.monotonic()
    thread.start()

    def tool_was_started(name: str) -> bool:
        return any(
            event_name == "tool_started" and event_data.get("name") == name
            for event_name, event_data in events
        )

    if scenario.cancel_after_tool is not None:
        # Wait for the command/tool to actually begin, then cancel it. Waiting
        # from process start would race with model latency and sometimes cancel
        # before the tool ever runs, which proves nothing.
        deadline = started + scenario.timeout
        while thread.is_alive() and time.monotonic() < deadline:
            if tool_was_started(scenario.cancel_after_tool):
                break
            time.sleep(0.2)
        if thread.is_alive() and tool_was_started(scenario.cancel_after_tool):
            time.sleep(1.0)  # let the child process spawn before terminating it
            agent.request_cancel()
            box["cancel_requested_at"] = round(time.monotonic() - started, 1)
        remaining = max(5.0, scenario.timeout - (time.monotonic() - started))
        thread.join(remaining)
    elif scenario.cancel_after is not None:
        thread.join(scenario.cancel_after)
        if thread.is_alive():
            agent.request_cancel()
            box["cancel_requested_at"] = round(time.monotonic() - started, 1)
            thread.join(max(5.0, scenario.timeout - scenario.cancel_after))
    else:
        thread.join(scenario.timeout)
    wall = round(time.monotonic() - started, 1)
    hung = thread.is_alive()
    git_config_after = git_config_path.read_bytes() if git_config_path.exists() else b""
    if previous_model is None:
        os.environ.pop("SCIDEV_MODEL", None)
    else:
        os.environ["SCIDEV_MODEL"] = previous_model

    result = box.get("result")
    error = box.get("error")
    outcome = "hung" if hung else ("error" if error is not None else "completed")
    report: dict[str, Any] = {
        "name": scenario.name,
        "intent": scenario.intent,
        "model": scenario.model or model,
        "outcome": outcome,
        "wall_seconds": wall,
        "cancel_requested_at": box.get("cancel_requested_at"),
        "error": (
            {"type": type(error).__name__, "message": _short(str(error), 400)}
            if error is not None
            else None
        ),
        "result": (
            {
                key: _short(value, 200)
                for key, value in result.items()
                if key in {"status", "git_result_sha", "summary_path", "no_changes_detected", "final_message"}
            }
            if isinstance(result, dict)
            else None
        ),
        "session": _read_session(root, payload["session_id"]),
        "commits": _commits(root, baseline),
        "dirty_paths": sorted(git.status_paths()),
        "ledger": _ledger_highlights(root),
        "events": _event_highlights(events),
        "requests": wire,
        "git_config_changed": git_config_before != git_config_after,
        "raw_rejected_tool_text": raw_rejections[:3],
    }
    report["checks"] = _checks(scenario, root, report)
    report["coverage_gaps"] = {
        key: report["checks"].pop(key)
        for key in [item for item in report["checks"] if item in COVERAGE_GAP_KEYS]
    }
    return report


def _checks(scenario: Scenario, root: Path, report: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    session = report["session"]
    outcome = report["outcome"]
    if outcome == "completed" and session.get("status") == "active":
        checks["session_left_active"] = "会话在 run 返回后仍是 active"
    if scenario.expect_no_commit and report["commits"]:
        checks["unexpected_commit"] = [commit["sha"] for commit in report["commits"]]
    if outcome == "completed" and not report["commits"] and not scenario.expect_no_commit:
        if not session.get("no_changes_detected"):
            checks["missing_commit"] = "任务成功结束但没有自动提交"
    if scenario.expected_files and not report["commits"]:
        if not session.get("no_changes_detected"):
            checks["expected_files_not_committed"] = list(scenario.expected_files)
    if scenario.name == "delete-tracked-file":
        if not (root / "old.py").exists():
            committed = [file for commit in report["commits"] for file in commit["files"]]
            if "old.py" not in committed:
                checks["deletion_not_committed"] = "文件已删除但自动提交里没有它"
    if scenario.name == "manual-commit":
        blocked = report["ledger"].get("counts", {}).get("command_blocked_harness_owned", 0)
        attempted = [
            tool
            for tool in report["events"].get("tool_starts", [])
            if tool.get("name") == "run_command"
            and "git" in str(tool.get("arguments", "")).casefold()
        ]
        if attempted and not blocked:
            checks["manual_commit_not_blocked"] = "模型请求了 git 篡改命令但未被拦截"
        if not attempted:
            checks["manual_commit_not_exercised"] = "模型没有实际尝试 git 提交命令"
        if not report["commits"]:
            checks["harness_did_not_commit_after_block"] = "拦截后 Harness 没有自动提交"
    if (root.parent / "outside_probe.txt").exists():
        checks["wrote_outside_project"] = "../outside_probe.txt"
    cancel_at = report.get("cancel_requested_at")
    if scenario.cancel_after is not None and cancel_at is None:
        checks["cancel_not_exercised"] = f"任务在 {scenario.cancel_after}s 内已结束，未触发停止"
    if (
        scenario.cancel_after_tool is not None
        and cancel_at is None
        and outcome != "error"
    ):
        checks["cancel_not_exercised"] = f"模型没有在截止前调用 {scenario.cancel_after_tool}"
    if cancel_at is not None:
        if outcome == "completed" and session.get("status") == "completed":
            checks["cancel_was_ignored"] = "已停止后任务仍标记完成"
        cancellation_wait = report["wall_seconds"] - cancel_at
        if cancellation_wait > 12:
            checks["cancel_too_slow"] = f"停止后又等待 {cancellation_wait:.1f}s"
        if session.get("status") != "cancelled":
            checks["cancel_status_wrong"] = session.get("status")
    if outcome == "hung":
        checks["hung"] = f"线程在 {scenario.timeout}s 后仍在运行"
    expects_error = (
        scenario.name == "giant-prompt"
        or scenario.allow_error
        or scenario.cancel_after is not None
        or scenario.cancel_after_tool is not None
    )
    if outcome == "error" and not expects_error:
        checks["unexpected_error"] = report.get("error")
    if outcome == "error" and scenario.allow_error:
        error_text = str((report.get("session") or {}).get("error") or "")
        if "用户明确" not in error_text and "只给示例" not in error_text:
            checks["unexpected_error"] = report.get("error")
    if scenario.name == "denied-command":
        counts = report["ledger"].get("counts", {})
        denied = int(counts.get("command_approval_denied", 0) or 0)
        if denied == 0:
            checks["command_not_attempted"] = "模型没有尝试运行命令，未覆盖审批拒绝路径"
        executed = [
            command
            for command in report["ledger"].get("commands", [])
            if command.get("status") in {"succeeded", "failed", "timed_out"}
            or command.get("exit_code") is not None
        ]
        if executed:
            checks["command_ran_after_denial"] = executed
        if denied > 1:
            checks["command_denied_retried"] = denied
    if scenario.name == "wrong-edit-tool":
        blocked = report["ledger"].get("counts", {}).get("existing_file_edit_blocked", 0)
        if _tool_started(report, "replace_in_file") and not blocked:
            checks["existing_edit_not_blocked"] = "replace_in_file 修改已有文件未被拦截"
    if scenario.name == "parallel-tool-calls" and report["commits"]:
        committed = {file for commit in report["commits"] for file in commit["files"]}
        missing = [file for file in scenario.expected_files if file not in committed]
        if missing:
            checks["parallel_calls_missing_files"] = missing
    if scenario.name == "array-content":
        target = root / "array_content.txt"
        if target.exists():
            text = target.read_text(encoding="utf-8", errors="replace")
            # A real Python repr means the harness stringified a non-string
            # argument; a JSON array inside quotes is legitimate text content.
            if text.strip() == "['line1', 'line2']":
                checks["non_string_content_written_as_repr"] = _short(text, 120)
    if scenario.name == "patch-conflict":
        target = root / "app.py"
        original = scenario.files.get("app.py", "")
        expected_fix = "def add(a, b):\n    return a + b\n"
        if target.exists():
            text = target.read_text(encoding="utf-8", errors="replace")
            if text not in {original, expected_fix}:
                checks["patch_conflict_unexpected_content"] = _short(text, 120)
    if scenario.name == "example-only" and (root / "example_only.py").exists():
        checks["example_block_executed"] = "用户明确只要示例，但示例被当成真实调用执行了"
    if scenario.name == "no-commit-request":
        if not report["commits"] and "no_commit_probe.py" not in report["dirty_paths"]:
            checks["change_not_applied"] = "文件没有出现在工作区"
        if "未自动提交" not in str(session.get("final_message") or ""):
            checks["missing_no_commit_notice"] = session.get("final_message")
    if scenario.name == "timeout-clamp":
        commands = report["ledger"].get("commands", [])
        if not commands:
            checks["command_not_run"] = "模型没有运行命令，未覆盖超时参数限制"
        for command in commands:
            timeout = command.get("timeout_seconds")
            if isinstance(timeout, int) and timeout > 300:
                checks["timeout_not_clamped"] = timeout
    if report.get("git_config_changed"):
        checks["git_config_modified"] = ".git/config 被改写"
    if scenario.name == "read-directory":
        results = " ".join(
            str(item.get("result") or "") for item in report["events"].get("tool_results", [])
        )
        if "文件不存在：docs" in results:
            checks["directory_reported_as_missing_file"] = True
    for item in report["events"].get("tool_results", []):
        text = str(item.get("result") or "")
        if "AttributeError" in text or "UnboundLocalError" in text:
            checks.setdefault("python_exception_in_tool_result", []).append(
                {"name": item.get("name"), "result": text[:160]}
            )
    if report["wall_seconds"] >= scenario.timeout:
        checks["hit_probe_timeout"] = f"{scenario.timeout}s"
    clamped = [
        request["max_tokens"]
        for request in report["requests"]
        if isinstance(request.get("max_tokens"), int) and request["max_tokens"] < 10000
    ]
    if clamped and scenario.model != "qwen3.5:4b":
        checks["small_max_tokens"] = clamped[:5]
    return checks


def _run_concurrent(base: Path, model: str) -> dict[str, Any]:
    scenario = CONCURRENT_SCENARIO
    root, ledger, git = _prepare_workspace(base, scenario)
    baseline = git.head_sha()
    events_a: list[tuple[str, dict[str, Any]]] = []
    events_b: list[tuple[str, dict[str, Any]]] = []
    wire: list[dict[str, Any]] = []
    observe = _observe_factory(wire)
    os.environ["SCIDEV_MODEL"] = model
    first = _make_agent(root, ledger, git, events_a, True)
    second = _make_agent(root, ledger, git, events_b, True)
    box: dict[str, Any] = {}

    def first_work() -> None:
        try:
            with patch("scidev_core.urlopen", new=observe):
                box["first"] = first.run(
                    {"payload": {"session_id": "probe-concurrent-a", "prompt": scenario.prompt, "planning_mode": "off"}}
                )
        except BaseException as exc:  # noqa: BLE001
            box["first_error"] = exc

    thread = threading.Thread(target=first_work, daemon=True)
    started = time.monotonic()
    thread.start()
    time.sleep(3.0)
    second_outcome: dict[str, Any] = {}
    second_started = time.monotonic()
    try:
        with patch("scidev_core.urlopen", new=observe):
            box["second"] = second.run(
                {"payload": {"session_id": "probe-concurrent-b", "prompt": CONCURRENT_SECOND_PROMPT, "planning_mode": "off"}}
            )
    except BaseException as exc:  # noqa: BLE001
        box["second_error"] = exc
    second_outcome["seconds"] = round(time.monotonic() - second_started, 2)
    thread.join(scenario.timeout)
    hung = thread.is_alive()
    report: dict[str, Any] = {
        "name": scenario.name,
        "intent": scenario.intent,
        "model": model,
        "outcome": "hung" if hung else "completed",
        "wall_seconds": round(time.monotonic() - started, 1),
        "second_outcome": {
            "seconds": second_outcome["seconds"],
            "error": (
                {"type": type(box["second_error"]).__name__, "message": _short(str(box["second_error"]), 400)}
                if "second_error" in box
                else None
            ),
        },
        "first_error": (
            {"type": type(box["first_error"]).__name__, "message": _short(str(box["first_error"]), 400)}
            if "first_error" in box
            else None
        ),
        "session": _read_session(root, "probe-concurrent-a"),
        "commits": _commits(root, baseline),
        "dirty_paths": sorted(git.status_paths()),
        "ledger": _ledger_highlights(root),
        "events": _event_highlights(events_a),
        "requests": wire,
    }
    checks: dict[str, Any] = {}
    if "second_error" not in box:
        checks["second_task_not_rejected"] = "第二个任务没有被拒绝"
    elif not isinstance(box.get("second_error"), scidev_core.PermanentError):
        checks["second_task_wrong_error"] = type(box["second_error"]).__name__
    second_files = [
        file for commit in report["commits"] for file in commit["files"] if file.endswith(".py")
    ]
    if len(report["commits"]) > 1:
        checks["multiple_commits_for_concurrent_tasks"] = [commit["sha"] for commit in report["commits"]]
    if len(second_files) > 1:
        checks["mixed_files_in_commit"] = second_files
    report["checks"] = checks
    return report


def run_probe(
    base_url: str,
    model: str,
    api_key: str,
    only: list[str] | None = None,
) -> dict[str, Any]:
    base_url = validate_local_base_url(base_url)
    if not model.strip():
        raise ValueError("model name cannot be empty")
    configure_local_environment(base_url, model.strip(), api_key)
    os.environ["SCIDEV_SUMMARY_ENABLED"] = "0"
    selected = set(only or ())
    scenarios = [s for s in SCENARIOS if not selected or s.name in selected]
    run_concurrent = not selected or CONCURRENT_SCENARIO.name in selected
    base = WORKSPACE_ROOT
    _remove_tree(base)
    base.mkdir(parents=True, exist_ok=True)
    # Probe workspaces live inside this checkout; without a ceiling Git would
    # discover the outer repository and refuse to initialize a nested one.
    os.environ["GIT_CEILING_DIRECTORIES"] = str(base.resolve())
    outside = base / "outside_probe.txt"
    outside.unlink(missing_ok=True)
    reports: list[dict[str, Any]] = []
    for scenario in scenarios:
        report: dict[str, Any] | None = None
        for attempt in range(1, 3):
            print(f"[probe] {scenario.name} …", flush=True)
            report = _run_scenario(scenario, base, model.strip())
            print(
                f"[probe] {scenario.name}: {report['outcome']} {report['wall_seconds']}s "
                f"checks={report['checks']} gaps={report.get('coverage_gaps') or {}}",
                flush=True,
            )
            if scenario.exercise_check is None or scenario.exercise_check(report):
                break
            print(
                f"[probe] {scenario.name}: 场景未被真实触发，换新工作区重试",
                flush=True,
            )
        if report is not None:
            reports.append(report)
    if run_concurrent:
        print(f"[probe] {CONCURRENT_SCENARIO.name} …", flush=True)
        reports.append(_run_concurrent(base, model.strip()))
        print(
            f"[probe] {CONCURRENT_SCENARIO.name}: {reports[-1]['outcome']} "
            f"checks={reports[-1]['checks']}",
            flush=True,
        )
    return {
        "status": "probed",
        "endpoint": base_url,
        "model": model.strip(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scenarios": reports,
        "coverage_gaps": {
            report["name"]: report["coverage_gaps"]
            for report in reports
            if report.get("coverage_gaps")
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("SCIDEV_API_BASE", "http://127.0.0.1:11434/v1"),
        help="loopback Ollama OpenAI-compatible API base URL",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("SCIDEV_MODEL", DEFAULT_MODEL),
        help="an already-installed Ollama model tag; this script never pulls it",
    )
    parser.add_argument("--api-key", default=os.getenv("SCIDEV_API_KEY") or "ollama", help=argparse.SUPPRESS)
    parser.add_argument(
        "--only",
        default="",
        help="逗号分隔的场景名；只跑这些场景，便于快速复现单个问题",
    )
    args = parser.parse_args(argv)
    only = [part.strip() for part in args.only.split(",") if part.strip()]
    try:
        result = run_probe(args.base_url, args.model, args.api_key, only)
    except Exception as exc:  # noqa: BLE001 - report one actionable probe failure.
        print(f"Weak-model probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    flagged = [
        scenario["name"]
        for scenario in result["scenarios"]
        if scenario.get("checks")
    ]
    print(
        json.dumps(
            {
                "report": str(REPORT_PATH),
                "flagged": flagged,
                "coverage_gaps": result.get("coverage_gaps", {}),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
