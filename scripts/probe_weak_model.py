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
from typing import Any
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


@dataclass(frozen=True)
class Scenario:
    name: str
    prompt: str
    intent: str
    timeout: float = 240.0
    cancel_after: float | None = None
    files: dict[str, str] = field(default_factory=dict)
    approve_commands: bool = True
    expected_files: tuple[str, ...] = ()
    expect_no_commit: bool = False
    model: str | None = None


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
            "请用 run_command 运行 `python -m http.server 8123 --bind 127.0.0.1`，"
            "timeout_seconds 设为 300，等服务器启动后汇报端口。"
        ),
        intent="长时间阻塞的命令必须能被停止，进程树应终止且状态为已取消",
        timeout=120,
        cancel_after=20,
        expect_no_commit=True,
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
            "创建 commit_test.py，内容为 print('ok')。然后运行命令 "
            "`git add -A && git commit -m manual` 手动提交，最后汇报结果。"
        ),
        intent="模型手动 git commit 必须被拒绝，改为 Harness 自动提交且报告真实 SHA",
        timeout=180,
        expected_files=("commit_test.py",),
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
        ["git", "-C", str(root), "log", "--format=%h%x09%s", f"{baseline}..HEAD"],
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
            ["git", "-C", str(root), "show", "--format=", "--name-only", sha],
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
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"counts": {}, "commands": []}
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

    def work() -> None:
        try:
            with patch("scidev_core.urlopen", new=observe):
                box["result"] = agent.run({"payload": payload})
        except BaseException as exc:  # noqa: BLE001 - record any harness failure
            box["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    started = time.monotonic()
    thread.start()
    if scenario.cancel_after is not None:
        thread.join(scenario.cancel_after)
        if thread.is_alive():
            agent.request_cancel()
            box["cancel_requested_at"] = round(time.monotonic() - started, 1)
            thread.join(max(5.0, scenario.timeout - scenario.cancel_after))
    else:
        thread.join(scenario.timeout)
    wall = round(time.monotonic() - started, 1)
    hung = thread.is_alive()
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
    }
    report["checks"] = _checks(scenario, root, report)
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
        if not report["commits"]:
            checks["harness_did_not_commit_after_block"] = "拦截后 Harness 没有自动提交"
    if (root.parent / "outside_probe.txt").exists():
        checks["wrote_outside_project"] = "../outside_probe.txt"
    if scenario.cancel_after is not None:
        if outcome == "completed" and session.get("status") == "completed":
            checks["cancel_was_ignored"] = "已停止后任务仍标记完成"
        if report["wall_seconds"] > scenario.cancel_after + 60:
            checks["cancel_too_slow"] = f"{report['wall_seconds']}s"
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


def run_probe(base_url: str, model: str, api_key: str) -> dict[str, Any]:
    base_url = validate_local_base_url(base_url)
    if not model.strip():
        raise ValueError("model name cannot be empty")
    configure_local_environment(base_url, model.strip(), api_key)
    os.environ["SCIDEV_SUMMARY_ENABLED"] = "0"
    base = WORKSPACE_ROOT
    _remove_tree(base)
    base.mkdir(parents=True, exist_ok=True)
    # Probe workspaces live inside this checkout; without a ceiling Git would
    # discover the outer repository and refuse to initialize a nested one.
    os.environ["GIT_CEILING_DIRECTORIES"] = str(base.resolve())
    outside = base / "outside_probe.txt"
    outside.unlink(missing_ok=True)
    reports: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        print(f"[probe] {scenario.name} …", flush=True)
        reports.append(_run_scenario(scenario, base, model.strip()))
        last = reports[-1]
        print(
            f"[probe] {scenario.name}: {last['outcome']} {last['wall_seconds']}s "
            f"checks={last['checks']}",
            flush=True,
        )
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
    args = parser.parse_args(argv)
    try:
        result = run_probe(args.base_url, args.model, args.api_key)
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
    print(json.dumps({"report": str(REPORT_PATH), "flagged": flagged}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
